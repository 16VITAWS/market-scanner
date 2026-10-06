"""
One end-of-day run of the Trading Brain: context -> calibration -> paper management -> decisions -> paper entries ->
reports. Returns the JSON written to api/brain.json. All inputs are passed in; no network here.
"""
import time
import datetime as dt
import numpy as np
from . import decide as DE, regime as RG, paper as PA, daybook as DB, strategies as S, score as SC
from .. import config as C

PIPELINE = ["DATA CHECK", "MARKET STATUS", "GLOBAL CONTEXT", "MARKET REGIME", "BREADTH", "VOLATILITY", "TREND", "MOMENTUM", "STRUCTURE",
            "VOLUME", "OPTIONS / FUTURES (not in daily engine)", "NEWS / EVENT RISK", "STRATEGY CANDIDATES", "PROBABILITY", "EXPECTED VALUE",
            "TRANSACTION COST", "SLIPPAGE", "RISK / REWARD", "POSITION SIZE", "PORTFOLIO CORRELATION", "DAILY RISK LIMIT", "LIQUIDITY",
            "FINAL TRADE SCORE", "TRADE / WAIT / REJECT"]


def run(L, stocks, idx, frames, sectors, bar_date, data_ok=True, event_check=None, intel_flags=None, trade=True):
    t0 = time.time()
    vix = frames.get("INDIAVIX")
    ctx = DE.build_context(stocks, idx, vix, frames, sectors)
    regime_now = RG.current(idx, ctx["breadth"], vix, data_ok=data_ok)
    t1 = time.time()
    cal = DE.calibrate(ctx, as_of=str(bar_date))
    t2 = time.time()
    if PA.ACCOUNT["id"] not in L.state["accounts"]:
        a = PA.ACCOUNT
        L.create_account(a["id"], a["name"], a["currency"], a["cash"], a["strategies"])
    acts = PA.manage(L, stocks, ctx["feats"], bar_date, regime_now["state"]) if trade else ["management skipped (data not OK)"]
    acct = PA.account_view(L, ctx["feats"])
    # events only for today's raw candidates (keeps the network calls small)
    flags = {}
    raw = []
    for sym, f in ctx["feats"].items():
        if f.index[-1] != ctx["regimes"].index[-1]:
            continue
        sig = S.signals(f.tail(260))
        if any(bool(v.iloc[-1]) for k, v in sig.items() if k != S.BENCHMARK):
            raw.append(sym)
    if event_check:
        try:
            flags.update(event_check(raw[:60]) or {})
        except Exception as e:  # noqa
            acts.append(f"event check failed: {e}")
    for s, fl in (intel_flags or {}).items():
        if s in raw:
            flags.setdefault(s, []).extend(fl)
    cards, meta = DE.evaluate(ctx, cal, acct, regime_now, data_ok=data_ok, event_flags=flags, sectors=sectors)
    if trade and data_ok:
        acts += PA.enter(L, stocks, bar_date, cards)
    else:
        acts.append("No new paper entries this run (data not OK).")
    caps = DE.capital_scenarios(cards, cal, regime_now)
    regs = {str(k.date()): v for k, v in ctx["regimes"].items()}
    book = DB.build(L.state, PA.ACCOUNT["id"], regs)
    # benchmark: NIFTY over the calibration span vs the strategies
    span = cal["span"]
    bench = {}
    try:
        ic = idx["Close"]
        s0 = ic[ic.index >= np.datetime64(span[0])].iloc[0] if span[0] else None
        bench["nifty_return_pct_over_span"] = round((float(ic.iloc[-1]) / float(s0) - 1) * 100, 2) if s0 is not None else None
    except Exception:
        bench["nifty_return_pct_over_span"] = None
    rep = cal["report"]
    act = [k for k, v in rep.items() if v["status"].startswith("ACTIVE")]
    base = cal["benchmark"]
    bench.update(span=span, baseline_trend=base, active_strategies=act,
                 verdict=("At least one strategy beats the simple trend benchmark per trade after costs" if any(rep[k].get("beats_benchmark") for k in act)
                          else "No strategy beats the simple trend benchmark after costs - complexity is NOT adding value yet"))
    # regime history (last 120 sessions) for the chart
    rh = [{"date": str(k.date()), "state": v} for k, v in list(ctx["regimes"].items())[-120:]]
    counts = {}
    for c in cards:
        counts[c["decision"]] = counts.get(c["decision"], 0) + 1
    rej = {}
    for c in cards:
        if c["decision"] == "REJECT":
            k = c["reject_reason"].split(":")[0]
            rej[k] = rej.get(k, 0) + 1
    trade_cards = [c for c in cards if c["decision"] == "TRADE"]
    headline = (f"{len(trade_cards)} trade(s) selected" if trade_cards else
                "NO TRADE today — no opportunity passed every check. Waiting is the mathematically better decision.")

    def slim(c):
        x = {k: c[k] for k in ("rank", "symbol", "strategy", "strategy_text", "regime", "date", "plan", "sector", "qty", "value", "risk_rupees",
                               "risk_pct_equity", "sizing", "costs", "ev", "capital_required", "stages", "decision", "reject_reason")}
        x["score"] = c["score"]
        x["probability"] = c["probability"]
        x["text"] = DE.card_text(c)
        return x
    shown = [slim(c) for c in cards if c["decision"] != "REJECT"][:40] + [slim(c) for c in cards if c["decision"] == "REJECT"][:60]
    for v in rep.values():
        v.pop("_", None)
    return {"as_of": str(bar_date), "generated_in_s": {"context": round(t1 - t0, 1), "calibration": round(t2 - t1, 1), "total": round(time.time() - t0, 1)},
            "headline": headline, "regime": regime_now, "regime_history": rh, "global": ctx["global"],
            "breadth": {k: (round(float(v), 4) if v == v else None) for k, v in (ctx["breadth"].iloc[-1].items() if ctx["breadth"] is not None else [])},
            "matrix": RG.MATRIX, "pipeline": PIPELINE, "decisions": shown, "counts": counts, "reject_reasons": rej,
            "account_meta": meta, "capital_scenarios": caps, "paper_actions": acts, "daybook": book,
            "strategies": rep, "score_threshold": cal["score_threshold"], "score_buckets": cal["score_buckets"], "score_weights": SC.WEIGHTS,
            "walk_forward": cal["walk_forward"], "montecarlo": cal["montecarlo"], "benchmark": bench,
            "history": {"trades_simulated": len(cal["trades"]), "closed": sum(1 for t in cal["trades"] if not t.get("open")), "span": span,
                        "typical_cost_pct": round(cal["cost_frac"] * 100, 3), "slippage_pct_each_side": S.MGMT["slippage"] * 100,
                        "symbols": len(ctx["feats"])},
            "honesty": [
                "Probabilities come from simulated historical trades of the same rules, fills and charges; they are estimates with the number of trades shown, not guarantees.",
                "Daily bars only: intraday VWAP, opening range and time-of-day analysis need live tick data (the laptop program) and are not part of this daily engine yet.",
                "Options / futures confirmation, implied volatility and Greeks are not in this daily engine; the laptop's live option-chain trader handles NIFTY options separately.",
                "Cash equity is long-only here (no overnight short selling). In falling markets the correct output is usually NO TRADE.",
                "Survivorship: the universe is today's Nifty 500 list, so stocks that dropped out in the past are missing - historical results can look somewhat better than reality.",
                "Strategy rules were written by hand, not optimised on this history; probabilities and weights only use trades that closed before the decision date.",
                "Real money stays locked behind the existing paper-record gates; this engine never sends a real order."]}
