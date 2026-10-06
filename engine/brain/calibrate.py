"""
Calibration = the probability engine's evidence. Every historical setup of every strategy is simulated with the
same fills, slippage and management as the paper account (strategies.simulate). Nothing is fitted to these
results except the transparent shrinkage below, so the statistics are honest estimates; the walk-forward check
then tests whether probabilities made only from PAST trades predicted later trades (Brier score vs base rate).

Probability of a net win for a candidate = hierarchical beta shrinkage:
   (strategy, regime, score bucket) -> (strategy, regime) -> (strategy) -> all strategies
Each level is pulled toward its parent with a prior worth K trades, so a bucket with 8 trades says little and a
bucket with 300 trades says a lot. The number of trades behind every estimate is always shown.
"""
import math
from collections import defaultdict
import numpy as np
import pandas as pd
from . import strategies as S

K = 20                     # prior strength (in trades) of each parent level
MIN_N = 30                 # fewer trades than this at strategy level -> INSUFFICIENT EVIDENCE (no trading)
BUCKETS = [(0, 55), (55, 65), (65, 75), (75, 101)]


def bucket(score):
    for lo, hi in BUCKETS:
        if lo <= score < hi:
            return f"{lo}-{hi - 1 if hi < 101 else 100}"
    return "?"


def collect(feats, regimes, tscore_fn, panic_dates=None, min_history=200):
    """feats: {symbol: feature frame}; regimes: Series date->state; tscore_fn(row)->0..100 technical score.
    Returns list of trade dicts (closed and still-open)."""
    trades = []
    for sym, f in feats.items():
        if len(f) < min_history + 5:
            continue
        sig = S.signals(f)
        panic = None
        if panic_dates is not None:
            panic = np.array([d in panic_dates for d in f.index])
        ok_hist = f["sma200"].notna().values
        idx = f.index
        for strat, mask in sig.items():
            m = mask.values & ok_hist
            nxt = 0
            for i in np.flatnonzero(m):
                if i < nxt:
                    continue                       # one open trade per symbol per strategy
                row = f.iloc[i]
                plan = S.setup(strat, row)
                if not plan:
                    continue
                r = S.simulate(f, i, plan, panic=panic)
                if r is None or r.get("skipped"):
                    continue
                d = idx[i]
                tr = {"symbol": sym, "strategy": strat, "date": str(d.date()), "regime": regimes.get(d, "UNCERTAIN") if regimes is not None else "UNCERTAIN",
                      "score": round(float(tscore_fn(row)), 1), "ret": float(r["ret"]), "R": float(r["R"]), "bars": int(r["bars"]),
                      "dow": int(d.dayofweek), "open": bool(r.get("open")),
                      "riskf": float((r["entry"] - plan["stop"]) / r["entry"])}
                if not r.get("open"):
                    tr.update(exit_date=str(idx[r["exit_row"]].date()), why=r["why"], mae_R=r["mae_R"], mfe_R=r["mfe_R"])
                    nxt = r["exit_row"] + 1
                else:
                    nxt = len(f)
                trades.append(tr)
    return trades


# ------------------------------------------------------------------ statistics
def wilson(k, n, z=1.645):
    if n == 0:
        return (None, None)
    p = k / n
    den = 1 + z * z / n
    c = (p + z * z / (2 * n)) / den
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / den
    return (round(c - h, 4), round(c + h, 4))


def stats(rets, cost):
    """rets: gross returns (price, after slippage); cost: round-trip charges as a fraction of position value."""
    r = np.asarray(rets, float) - cost
    n = len(r)
    if n == 0:
        return {"n": 0}
    w, l = r[r > 0], r[r <= 0]
    gp, gl = w.sum(), -l.sum()
    sd = float(r.std(ddof=1)) if n > 1 else 0.0
    lo, hi = wilson(len(w), n)
    eq = np.cumprod(1 + r * 0.1)            # 10% of equity per trade, only to express drawdown of the sequence
    dd = float((eq / np.maximum.accumulate(eq) - 1).min())
    streak = mx = 0
    for x in r:
        streak = streak + 1 if x <= 0 else 0
        mx = max(mx, streak)
    return {"n": n, "win_rate": round(len(w) / n, 4), "win_ci90": [lo, hi], "avg_win_pct": round(float(w.mean()) * 100, 3) if len(w) else None,
            "avg_loss_pct": round(float(l.mean()) * 100, 3) if len(l) else None, "expectancy_pct": round(float(r.mean()) * 100, 3),
            "t_stat": round(float(r.mean()) / (sd / math.sqrt(n)), 2) if sd > 0 and n > 1 else None,
            "profit_factor": round(float(gp / gl), 2) if gl > 0 else None, "max_losing_streak": mx, "seq_drawdown_pct_at_10pct_size": round(dd * 100, 1)}


class Model:
    """Holds closed trades and answers: P(net win), expected net return, avg win/loss - with the evidence behind them."""

    def __init__(self, trades, cost, before=None):
        self.cost = cost
        ts = [t for t in trades if not t.get("open") and t["strategy"] != S.BENCHMARK and (before is None or t["exit_date"] < before)]
        self.all = np.array([t["ret"] for t in ts]) if ts else np.array([])
        g = {"s": defaultdict(list), "sr": defaultdict(list), "srb": defaultdict(list)}
        for t in ts:
            g["s"][t["strategy"]].append(t["ret"])
            g["sr"][(t["strategy"], t["regime"])].append(t["ret"])
            g["srb"][(t["strategy"], t["regime"], bucket(t["score"]))].append(t["ret"])
        self.g = {k: {kk: np.array(vv) for kk, vv in v.items()} for k, v in g.items()}

    def _shrink(self, arr, prior_p, prior_m):
        n = len(arr)
        if n == 0:
            return prior_p, prior_m
        net = arr - self.cost
        p = ((net > 0).sum() + K * prior_p) / (n + K)
        m = (net.sum() + K * prior_m) / (n + K)
        return p, m

    def estimate(self, strategy, regime, score):
        a = self.all
        if len(a) == 0:
            return {"p": None, "evidence": "NO HISTORY", "n_strategy": 0}
        base_p = float(((a - self.cost) > 0).mean())
        base_m = float((a - self.cost).mean())
        s = self.g["s"].get(strategy, np.array([]))
        sr = self.g["sr"].get((strategy, regime), np.array([]))
        srb = self.g["srb"].get((strategy, regime, bucket(score)), np.array([]))
        p1, m1 = self._shrink(s, base_p, base_m)
        p2, m2 = self._shrink(sr, p1, m1)
        p3, m3 = self._shrink(srb, p2, m2)
        net_s = s - self.cost if len(s) else s
        wins, losses = net_s[net_s > 0] if len(s) else s, net_s[net_s <= 0] if len(s) else s
        ev = "OK" if len(s) >= MIN_N else "INSUFFICIENT EVIDENCE"
        return {"p": round(float(p3), 4), "exp_net_pct": round(float(m3) * 100, 3), "n_strategy": int(len(s)), "n_regime": int(len(sr)),
                "n_bucket": int(len(srb)), "evidence": ev,
                "avg_win_pct": round(float(wins.mean()) * 100, 3) if len(wins) else None,
                "avg_loss_pct": round(float(losses.mean()) * 100, 3) if len(losses) else None,
                "method": f"beta-shrinkage: bucket({len(srb)}) -> regime({len(sr)}) -> strategy({len(s)}) -> all({len(a)}), prior {K} trades"}


def walk_forward_check(trades, cost, start_after=120):
    """For each calendar month: predict trades decided that month from trades that EXITED before the month began.
    Returns Brier score of the model vs a constant base-rate forecast, reliability table, and the realised net
    expectancy of trades the model would have accepted (p-implied EV > 0) vs all trades."""
    ts = sorted([t for t in trades if not t.get("open") and t["strategy"] != S.BENCHMARK], key=lambda t: t["date"])
    if len(ts) < start_after + 50:
        return {"available": False, "reason": f"only {len(ts)} closed historical trades"}
    months = sorted({t["date"][:7] for t in ts})
    preds = []
    for m in months:
        first = m + "-01"
        past = [t for t in ts if t["exit_date"] < first]
        if len(past) < start_after:
            continue
        M = Model(past, cost)
        base = float(((M.all - cost) > 0).mean())
        for t in (t for t in ts if t["date"][:7] == m):
            e = M.estimate(t["strategy"], t["regime"], t["score"])
            if e["p"] is None:
                continue
            preds.append((e["p"], base, 1.0 if t["ret"] - cost > 0 else 0.0, t["ret"] - cost, e["exp_net_pct"], e["evidence"]))
    if len(preds) < 50:
        return {"available": False, "reason": "not enough out-of-sample predictions yet"}
    P = np.array(preds, dtype=object)
    p, b, y, r = (np.array(P[:, k], float) for k in range(4))
    ex = np.array(P[:, 4], float)
    brier, brier_base = float(np.mean((p - y) ** 2)), float(np.mean((b - y) ** 2))
    rel = []
    for lo in np.arange(0, 1, 0.1):
        m = (p >= lo) & (p < lo + 0.1)
        if m.sum() >= 10:
            rel.append({"predicted": f"{lo:.1f}-{lo + 0.1:.1f}", "n": int(m.sum()), "mean_pred": round(float(p[m].mean()), 3), "actual": round(float(y[m].mean()), 3)})
    acc = ex > 0
    return {"available": True, "oos_predictions": int(len(p)), "brier": round(brier, 4), "brier_base_rate": round(brier_base, 4),
            "skill_vs_base_pct": round((1 - brier / brier_base) * 100, 2) if brier_base else None, "reliability": rel,
            "accepted": {"n": int(acc.sum()), "net_expectancy_pct": round(float(r[acc].mean()) * 100, 3) if acc.any() else None,
                         "win_rate": round(float(y[acc].mean()), 3) if acc.any() else None},
            "all": {"n": int(len(r)), "net_expectancy_pct": round(float(r.mean()) * 100, 3), "win_rate": round(float(y.mean()), 3)},
            "verdict": ("Probabilities beat the base rate AND accepted trades did better out-of-sample" if acc.sum() >= 50 and brier < brier_base and r[acc].mean() > r.mean() and r[acc].mean() > 0 else
                        "NOT PROVEN out-of-sample - probabilities are no better than the base rate yet; treat them with caution")}


def strategy_report(trades, cost, recent=60):
    """Per strategy: overall, by regime, by day of week, holding time, MAE/MFE, rolling recent performance, status and weight."""
    out = {}
    by = defaultdict(list)
    for t in trades:
        if not t.get("open"):
            by[t["strategy"]].append(t)
    base = stats([t["ret"] for t in by.get(S.BENCHMARK, [])], cost) if by.get(S.BENCHMARK) else {"n": 0}
    for strat, ts in by.items():
        ts.sort(key=lambda t: t["exit_date"])
        allst = stats([t["ret"] for t in ts], cost)
        rec = stats([t["ret"] for t in ts[-recent:]], cost)
        reg = defaultdict(list)
        for t in ts:
            reg[t["regime"]].append(t["ret"])
        dow = defaultdict(list)
        for t in ts:
            dow[t["dow"]].append(t["ret"])
        exits = defaultdict(int)
        for t in ts:
            exits[t.get("why", "?")] += 1
        n, mean, tstat = allst["n"], allst.get("expectancy_pct") or 0, allst.get("t_stat") or 0
        rm, rt = rec.get("expectancy_pct") or 0, rec.get("t_stat") or 0
        if strat == S.BENCHMARK:
            status, weight = "BENCHMARK", 0.0
        elif n < MIN_N:
            status, weight = "INSUFFICIENT EVIDENCE", 0.0
        elif rec["n"] >= 40 and rt < -1.65:
            status, weight = "PAUSED / REVIEW", 0.0
        elif mean <= 0:
            status, weight = "REDUCED (negative expectancy)", 0.0
        elif rm < 0:
            status, weight = "REDUCED (recent trades negative)", 0.5
        else:
            status, weight = "ACTIVE", round(max(0.5, min(1.5, 0.75 + tstat / 6)), 2)
        out[strat] = {"description": S.STRATEGIES.get(strat, "simple benchmark"), "params": dict(zip(["stop_atr", "t1_R", "t2_R", "max_hold"], S.PARAMS[strat])),
                      "all": allst, f"last_{recent}": rec, "status": status, "weight": weight,
                      "by_regime": {k: stats(v, cost) for k, v in sorted(reg.items(), key=lambda kv: -len(kv[1]))},
                      "by_weekday": {["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"][k]: stats(v, cost).get("expectancy_pct") for k, v in sorted(dow.items())},
                      "avg_hold_bars": round(float(np.mean([t["bars"] for t in ts])), 1),
                      "avg_mae_R": round(float(np.mean([t.get("mae_R", 0) for t in ts])), 2), "avg_mfe_R": round(float(np.mean([t.get("mfe_R", 0) for t in ts])), 2),
                      "exit_reasons": dict(exits),
                      "beats_benchmark": (None if not base.get("n") else bool(mean > (base.get("expectancy_pct") or 0)))}
    return out, base
