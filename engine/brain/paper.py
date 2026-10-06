"""
IN-BRAIN paper account: executes TRADE decisions through the same ledger + order simulator as every other paper
account (next-open fills, slippage + impact, statutory charges), and manages positions with exactly the rules the
calibration measured (strategies.manage_step): stop, target 2, break-even after +1R, ATR trail after +1.5R, the
ledger's own percentage trail, time exit after max holding days, exit on market PANIC.
"""
from decimal import Decimal as D
from .. import simulator
from . import strategies as S

ACCOUNT = {"id": "IN-BRAIN", "name": "India Trading Brain (auto, multi-strategy)", "currency": "INR", "cash": "200000", "strategies": ["brain_ensemble"]}


def manage(L, stocks, feats, bar_date, regime_state):
    """Apply today's bar to pending orders / open positions, move stops, queue exits, mark to market."""
    aid = ACCOUNT["id"]
    a = L.account(aid)
    plans = a.setdefault("brain_plans", {})
    acts = []
    fresh = a.get("brain_last_bar") != str(bar_date)
    syms = set(a["positions"]) | {o["symbol"] for o in L.pending(aid)}
    if fresh:
        for sym in sorted(syms):
            df = stocks.get(sym)
            if df is None:
                acts.append(f"{sym}: no data today - untouched"); continue
            ts = [t for t in df.index if str(t.date()) == str(bar_date)]
            if not ts:
                acts.append(f"{sym}: no bar for {bar_date}"); continue
            acts += simulator.apply_bar(L, aid, sym, df.loc[ts[0]], bar_date)
        # close-based management for open positions (same order as strategies.manage_step)
        for sym, pos in list(a["positions"].items()):
            pl = plans.get(sym)
            f = feats.get(sym)
            if not pl or f is None or str(f.index[-1].date()) != str(bar_date):
                continue
            c, atr = float(f["close"].iloc[-1]), float(f["atr"].iloc[-1])
            e = float(pos["avg_price"])
            r = max(e - float(pl["initial_stop"]), 1e-9)
            stop = float(pos["stop"]) if pos.get("stop") else float(pl["initial_stop"])
            new = stop
            if c >= e + S.MGMT["be_after_R"] * r:
                new = max(new, e)
            if c >= e + S.MGMT["trail_after_R"] * r and atr == atr:
                new = max(new, c - S.MGMT["trail_atr"] * atr)
            if new > stop + 1e-9:
                pos["stop"] = str(round(new, 2))
                acts.append(f"{sym}: stop raised to {round(new, 2)} ({'trail' if new > e else 'break-even'})")
            why = None
            if int(pos.get("bars_held", 0)) >= int(pl["max_hold"]):
                why = f"time exit ({pl['max_hold']} sessions)"
            elif regime_state == "PANIC":
                why = "market regime PANIC"
            if why and not any(o["symbol"] == sym and o["side"] == "sell" for o in L.pending(aid)):
                o, st = L.submit(aid, sym, "sell", pos["qty"], "MARKET", bar_date=bar_date, strategy=pos.get("strategy"), reason=why)
                if st == "ok":
                    acts.append(f"Queued EXIT {sym} at next open - {why}")
        a["brain_last_bar"] = str(bar_date)
    _mark(L, stocks, bar_date)
    return acts


def _mark(L, stocks, bar_date):
    a = L.account(ACCOUNT["id"])
    prices = {s: float(stocks[s]["Close"].iloc[-1]) for s in a["positions"] if s in stocks}
    L.mark(ACCOUNT["id"], prices, bar_date)


def enter(L, stocks, bar_date, cards):
    """Queue the TRADE decisions (filled at the next open) and tidy plans."""
    aid = ACCOUNT["id"]
    a = L.account(aid)
    plans = a.setdefault("brain_plans", {})
    acts = []
    # new entries (decided on today's close, filled at the next open)
    for c in [c for c in cards if c["decision"] == "TRADE"]:
        sym, p = c["symbol"], c["plan"]
        o, st = L.submit(aid, sym, "buy", c["qty"], "MARKET", bar_date=bar_date, strategy="brain:" + c["strategy"],
                         signal_id=f"{c['date']}-{sym}-{c['strategy']}", reason=f"{c['strategy']} score {c['score']['score']}, p={c['probability'].get('p')}, net EV ₹{c['ev']['expected_net']}",
                         stop=p["stop"], target=p["t2"], dedupe_key=f"{aid}|{sym}|buy|{bar_date}", sector=c.get("sector"),
                         meta={"score": c["score"]["score"], "p": c["probability"].get("p"), "expected_net": c["ev"]["expected_net"], "regime": c["regime"]})
        if st == "ok":
            plans[sym] = {"strategy": c["strategy"], "initial_stop": p["stop"], "max_hold": p["max_hold"], "t1": p["t1"], "t2": p["t2"], "decided": str(bar_date)}
            acts.append(f"Queued BUY {c['qty']} {sym} at next open ({c['strategy']}; stop {p['stop']}, target {p['t2']}; expected net ₹{c['ev']['expected_net']})")
        elif st == "duplicate":
            acts.append(f"{sym}: already ordered for {bar_date} (duplicate blocked)")
    for sym in list(plans):
        if sym not in a["positions"] and not any(o["symbol"] == sym for o in L.pending(aid)):
            plans.pop(sym, None)
    _mark(L, stocks, bar_date)
    if not acts:
        acts.append("NO TRADE today: no opportunity passed every check. Waiting is a valid decision.")
    return acts


def account_view(L, feats):
    """Inputs for decide.evaluate: equity, cash, open positions with risk, protections."""
    aid = ACCOUNT["id"]
    a = L.account(aid)
    summ = L.summary(aid)
    eq = float(summ["equity"])
    plans = a.get("brain_plans", {})
    ops = []
    for sym, pos in a["positions"].items():
        last, stop = float(pos["last"]), float(pos.get("stop") or 0)
        ops.append({"symbol": sym, "sector": pos.get("sector"), "value": last * float(pos["qty"]),
                    "risk_rupees": max(0.0, (last - stop) * float(pos["qty"])) if stop else last * float(pos["qty"]) * 0.1, "beta": 1.0})
    eqs = a.get("equity") or []
    vals = [(p["date"], float(p["value"])) for p in eqs]
    start_day = vals[-2][1] if len(vals) >= 2 else float(a["start_cash"])
    start_week = vals[-6][1] if len(vals) >= 6 else float(a["start_cash"])
    month = [v for d, v in vals[-22:]] or [float(a["start_cash"])]
    trades = sorted([t for t in L.state["trades"] if t["account"] == aid], key=lambda t: t["exit_date"])
    streak = 0
    for t in reversed(trades):
        if float(t["pnl"]) <= 0:
            streak += 1
        else:
            break
    return {"equity": eq, "cash": float(summ["cash"]), "open_positions": ops, "pending": len(L.pending(aid)),
            "start_day_equity": start_day, "start_week_equity": start_week, "peak_month": max(month), "loss_streak": streak}
