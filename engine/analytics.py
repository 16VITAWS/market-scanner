"""
Performance analytics and rule-based diagnosis for a paper account or backtest.
Input: equity curve [{date, value}], trades [{pnl, pnl_pct, reason, bars_held, entry_date, exit_date, strategy}], benchmark closes.
Output: charts data (monthly P&L, drawdown, rolling stats), diagnosis (what to change), and a
bootstrap range of outcomes IF the same trade statistics continued (labelled, not a forecast).
"""
import math, random
import numpy as np
import pandas as pd


def _f(x):
    try:
        return float(x)
    except Exception:
        return float("nan")


def equity_stats(equity, benchmark=None, start_cash=None):
    if not equity:
        return {}
    e = pd.Series([_f(p["value"]) for p in equity], index=pd.to_datetime([p["date"] for p in equity]))
    e = e[~e.index.duplicated(keep="last")].sort_index()
    start = _f(start_cash) if start_cash else e.iloc[0]
    peak = e.cummax(); dd = e / peak - 1
    out = {"dates": [d.strftime("%Y-%m-%d") for d in e.index], "equity": [round(v, 2) for v in e.values],
           "equity_pct": [round((v / start - 1) * 100, 2) for v in e.values], "drawdown_pct": [round(v * 100, 2) for v in dd.values],
           "max_drawdown_pct": round(float(dd.min() * 100), 2), "return_pct": round((e.iloc[-1] / start - 1) * 100, 2), "days": int(len(e))}
    m = e.resample("ME").last().pct_change().dropna()
    if len(e) > 1:
        first_m = e.resample("ME").last()
        out["monthly_pct"] = [{"month": d.strftime("%Y-%m"), "pct": round(v * 100, 2)} for d, v in m.items()]
        if len(first_m):
            out["monthly_pct"].insert(0, {"month": first_m.index[0].strftime("%Y-%m"), "pct": round((first_m.iloc[0] / start - 1) * 100, 2)})
    if benchmark is not None and len(benchmark):
        b = benchmark.reindex(e.index, method="ffill")
        b = b / b.iloc[0] * start
        out["benchmark_pct"] = [round((v / start - 1) * 100, 2) if v == v else None for v in b.values]
        out["benchmark_return_pct"] = round((b.iloc[-1] / start - 1) * 100, 2) if b.iloc[-1] == b.iloc[-1] else None
    return out


def trade_stats(trades):
    if not trades:
        return {"n": 0}
    t = pd.DataFrame(trades)
    t["pnl"] = t["pnl"].map(_f); t["pnl_pct"] = t.get("pnl_pct", 0).map(_f)
    t["bars_held"] = t.get("bars_held", 0).map(_f)
    w, l = t[t.pnl > 0], t[t.pnl <= 0]
    gp, gl = w.pnl.sum(), -l.pnl.sum()
    by_reason = t.groupby(t["reason"].fillna("other").str.replace(r"\(.*\)", "", regex=True).str.strip()).agg(n=("pnl", "size"), pnl=("pnl", "sum"), avg=("pnl", "mean")).round(2)
    roll = []
    for i in range(len(t)):
        win = t.iloc[max(0, i - 9):i + 1]
        roll.append({"i": i + 1, "win_rate": round((win.pnl > 0).mean() * 100, 1), "expectancy": round(win.pnl.mean(), 2), "cum_pnl": round(t.pnl.iloc[:i + 1].sum(), 2)})
    fees = t["fees"].map(_f).sum() if "fees" in t else None
    return {"n": int(len(t)), "wins": int(len(w)), "losses": int(len(l)), "win_rate": round(len(w) / len(t) * 100, 1),
            "avg_win": round(w.pnl.mean(), 2) if len(w) else None, "avg_loss": round(l.pnl.mean(), 2) if len(l) else None,
            "avg_win_pct": round(w.pnl_pct.mean(), 2) if len(w) else None, "avg_loss_pct": round(l.pnl_pct.mean(), 2) if len(l) else None,
            "profit_factor": round(gp / gl, 2) if gl > 0 else None, "expectancy": round(t.pnl.mean(), 2), "net": round(t.pnl.sum(), 2),
            "payoff_ratio": round(w.pnl.mean() / -l.pnl.mean(), 2) if len(w) and len(l) and l.pnl.mean() < 0 else None,
            "avg_bars_held": round(t.bars_held.mean(), 1), "largest_win": round(t.pnl.max(), 2), "largest_loss": round(t.pnl.min(), 2),
            "max_consecutive_losses": _max_streak(t.pnl <= 0), "fees_total": round(fees, 2) if fees is not None else None,
            "fee_drag_pct_of_gross": round(fees / gp * 100, 1) if fees is not None and gp > 0 else None,
            "by_exit_reason": [{"reason": k, **{c: (int(v) if c == "n" else round(float(v), 2)) for c, v in r.items()}} for k, r in by_reason.iterrows()],
            "rolling": roll[-100:]}


def _max_streak(mask):
    best = cur = 0
    for m in mask:
        cur = cur + 1 if m else 0
        best = max(best, cur)
    return int(best)


def diagnose(ts, es, regime_state=None, min_trades=10):
    """Rule-based 'what to change'. Each item: finding, evidence, suggestion, confidence."""
    out = []
    n = ts.get("n", 0)
    if n < min_trades:
        out.append({"finding": "Not enough finished trades to judge the strategy", "evidence": f"{n} closed trades; need {min_trades}+ (ideally 30+)",
                    "suggestion": "Keep running in paper. Do not change rules on fewer than 30 trades - that is fitting to noise.", "confidence": "high"})
        return out
    if ts["payoff_ratio"] is not None and ts["payoff_ratio"] < 1 and ts["win_rate"] < 55:
        out.append({"finding": "Losses are bigger than wins and win rate is below 55%", "evidence": f"avg win {ts['avg_win']}, avg loss {ts['avg_loss']}, win rate {ts['win_rate']}%",
                    "suggestion": "Stops are too wide relative to targets, or exits cut winners early. Compare exits by reason below.", "confidence": "medium"})
    reasons = {r["reason"]: r for r in ts.get("by_exit_reason", [])}
    tim = next((v for k, v in reasons.items() if k.startswith("time exit")), None)
    if tim and tim["n"] / n > 0.4:
        out.append({"finding": "Most trades end by the time limit, not by target or stop", "evidence": f"{tim['n']} of {n} trades; average P&L on those {tim['avg']}",
                    "suggestion": "Targets may be too far (3xATR) for a 20-bar hold; test 2xATR target or 30-bar hold in the Strategy Lab.", "confidence": "medium"})
    stp = next((v for k, v in reasons.items() if "stop" in k), None)
    if stp and stp["n"] / n > 0.6:
        out.append({"finding": "Most trades are stopped out", "evidence": f"{stp['n']} of {n} exits by stop-loss",
                    "suggestion": "Entries are chasing extended moves. Consider requiring the breakout close within 1xATR of the 20-day high, or a wider 2.5xATR stop with smaller size.", "confidence": "medium"})
    if ts.get("fee_drag_pct_of_gross") and ts["fee_drag_pct_of_gross"] > 25:
        out.append({"finding": "Charges eat a large share of gross profit", "evidence": f"fees are {ts['fee_drag_pct_of_gross']}% of gross wins",
                    "suggestion": "Fewer, larger trades or a broker with lower delivery charges; see Broker Desk comparison.", "confidence": "high"})
    if ts.get("max_consecutive_losses", 0) >= 6:
        out.append({"finding": "Long losing streaks", "evidence": f"{ts['max_consecutive_losses']} losses in a row",
                    "suggestion": "Normal for a 40-50% win-rate trend system; keep risk per trade at 1% so a streak costs ~6%.", "confidence": "high"})
    if es.get("max_drawdown_pct", 0) < -12:
        out.append({"finding": "Drawdown is deep", "evidence": f"max drawdown {es['max_drawdown_pct']}%",
                    "suggestion": "Reduce max open positions from 5 to 3 or halve size in SIDEWAYS regime.", "confidence": "medium"})
    if es.get("benchmark_return_pct") is not None and es.get("return_pct") is not None and es["return_pct"] < es["benchmark_return_pct"] - 5:
        out.append({"finding": "Lagging the index", "evidence": f"account {es['return_pct']}% vs NIFTY {es['benchmark_return_pct']}% over the same period",
                    "suggestion": "A regime-filtered swing system will lag in strong bull runs; judge it on drawdown and bear-market behaviour, not only on return.", "confidence": "medium"})
    if not out:
        out.append({"finding": "No structural problem detected by the rules", "evidence": f"{n} trades, PF {ts.get('profit_factor')}, win rate {ts['win_rate']}%",
                    "suggestion": "Keep the rules unchanged; re-check after every 20 trades.", "confidence": "medium"})
    return out


def outcome_range(trades, start_cash, horizon_trades=50, sims=2000, seed=7):
    """Bootstrap: resample historical trade P&L (in % of equity) to show a RANGE of outcomes if the
    same statistics continued. Labelled 'if history repeats' - it is not a forecast."""
    if len(trades) < 10:
        return {"available": False, "reason": "needs at least 10 closed trades"}
    pct = [_f(t.get("pnl", 0)) / _f(start_cash) for t in trades]
    rng = random.Random(seed)
    finals, maxdds = [], []
    for _ in range(sims):
        eq, peak, mdd = 1.0, 1.0, 0.0
        for _ in range(horizon_trades):
            eq *= 1 + rng.choice(pct)
            peak = max(peak, eq); mdd = min(mdd, eq / peak - 1)
        finals.append(eq - 1); maxdds.append(mdd)
    q = lambda a, p: round(float(np.percentile(a, p)) * 100, 1)
    return {"available": True, "horizon_trades": horizon_trades, "sims": sims, "based_on_trades": len(trades),
            "return_pct": {"p10": q(finals, 10), "p25": q(finals, 25), "p50": q(finals, 50), "p75": q(finals, 75), "p90": q(finals, 90)},
            "max_drawdown_pct": {"p50": q(maxdds, 50), "p90": q(maxdds, 10)},
            "prob_negative_pct": round(float(np.mean([f < 0 for f in finals])) * 100, 1),
            "label": "IF the last trades' statistics repeated (bootstrap). Not a forecast. Small samples make this very wide and unreliable."}


def full(equity, trades, start_cash, benchmark=None):
    es = equity_stats(equity, benchmark, start_cash)
    ts = trade_stats(trades)
    return {"equity": es, "trades": ts, "diagnosis": diagnose(ts, es), "outcome_range": outcome_range(trades, start_cash), "kelly": kelly(ts)}


def kelly(ts):
    """Kelly fraction from realised trade stats; informational only. The risk engine still caps risk at 1% per trade."""
    if not ts or ts.get("n", 0) < 30 or not ts.get("avg_win") or not ts.get("avg_loss"):
        return {"available": False, "reason": "needs 30+ closed trades"}
    p = ts["wins"] / ts["n"]; b = ts["avg_win"] / abs(ts["avg_loss"])
    f = p - (1 - p) / b
    return {"available": True, "full_kelly": round(f, 3), "half_kelly": round(f / 2, 3), "win_prob": round(p, 3), "payoff": round(b, 2),
            "note": "Growth-optimal fraction if the past win rate and payoff held exactly. Half-Kelly shown for context; the engine does NOT size by Kelly (1% risk cap applies)."}


def portfolio_risk(equity_curve, positions, frames, equity, bench_id="NIFTY", conf=0.95):
    """1-day VaR/CVaR. Historical from the account's own daily returns when >= 30 points,
    otherwise parametric from current positions' 60-day return history (historical simulation on holdings)."""
    out = {"confidence": conf, "equity": float(equity)}
    eq = [float(p["value"]) for p in equity_curve]
    if len(eq) >= 31:
        r = pd.Series(eq).pct_change().dropna()
        q = float(np.percentile(r, (1 - conf) * 100))
        out.update(method="historical (account returns)", var_pct=round(-q * 100, 2), cvar_pct=round(-float(r[r <= q].mean()) * 100, 2), n=int(len(r)))
    else:
        legs = []
        for sym, p in positions.items():
            df = frames.get(sym)
            val = float(p.get("last", p.get("avg_price", 0))) * float(p["qty"])
            if df is None or len(df) < 61 or val <= 0:
                continue
            legs.append((df["Close"].pct_change().tail(60).reset_index(drop=True), val))
        if not legs:
            out.update(method="no open exposure" if not positions else "insufficient history for held positions", var_pct=0.0, cvar_pct=0.0, n=0)
        else:
            pnl = sum(r * v for r, v in legs) / float(equity)
            q = float(np.percentile(pnl, (1 - conf) * 100))
            out.update(method="historical simulation on current holdings (60 days)", var_pct=round(-q * 100, 2), cvar_pct=round(-float(pnl[pnl <= q].mean()) * 100, 2), n=int(len(pnl)))
    # stress: index shocks both ways. Stocks via beta (1 if unknown); option spreads repriced (Black-Scholes) at the shocked spot.
    import datetime as _dt
    from . import options as _opt
    stress = {}
    b = frames.get(bench_id)
    for shock in (-0.20, -0.10, -0.05, 0.05, 0.10):
        pnl = 0.0
        for sym, p in positions.items():
            val = float(p.get("last", p.get("avg_price", 0))) * float(p["qty"])
            sp = p.get("spread")
            if sp:
                u = frames.get(sp["underlying"])
                if u is None or not len(u):
                    pnl -= min(val, float(p.get("max_loss", val))); continue
                S = float(u["Close"].iloc[-1]); today = u.index[-1].date()
                sig, _ = _opt.vol_input(sp["underlying"], frames)
                now, _, _ = _opt.spread_value(sp, S, today, sig)
                shocked, _, _ = _opt.spread_value(sp, S * (1 + shock), today, sig * (1.3 if shock < 0 else 0.9))
                pnl += (shocked - now) * float(p["qty"])
                continue
            beta = 1.0
            df = frames.get(sym)
            if df is not None and b is not None and len(df) > 61:
                j = pd.concat([df["Close"].pct_change(), b["Close"].pct_change()], axis=1).dropna().tail(120)
                if len(j) > 30 and j.iloc[:, 1].var() > 0:
                    beta = float(j.cov().iloc[0, 1] / j.iloc[:, 1].var())
            pnl += val * beta * shock
        stress[f"{int(shock*100):+d}%"] = {"pnl": round(pnl, 0), "pnl_pct": round(pnl / float(equity) * 100, 2) if equity else 0}
    out["stress"] = stress
    out["note"] = "Stress = P&L if the index moved instantly by the shown %, IV +30% on falls / -10% on rises for options. VaR = loss not exceeded on 95% of days; CVaR = average loss on the worst 5%. Estimates from past data; real losses can be larger (gaps, regime change)."
    return out
