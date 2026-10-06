"""
Monte Carlo risk test on the MEASURED trade distribution (net R after costs). Trades are resampled with
replacement into many alternative sequences, each with random extra slippage, to estimate drawdown and ruin.
"""
import numpy as np


def run(net_R, risk_pct=0.01, horizons=(100, 250), sims=3000, extra_slip_R=0.05, seed=11):
    r = np.asarray([x for x in net_R if np.isfinite(x)], float)
    if len(r) < 30:
        return {"available": False, "reason": f"only {len(r)} trades - Monte Carlo needs at least 30"}
    rng = np.random.default_rng(seed)
    out = {"available": True, "trades_sampled": int(len(r)), "risk_per_trade_pct": risk_pct * 100,
           "mean_net_R": round(float(r.mean()), 3), "median_net_R": round(float(np.median(r)), 3), "horizons": {}}
    for n in horizons:
        draws = rng.choice(r, size=(sims, n), replace=True) - rng.uniform(0, extra_slip_R, size=(sims, n))
        eq = np.cumprod(1 + draws * risk_pct, axis=1)
        peak = np.maximum.accumulate(eq, axis=1)
        mdd = (eq / peak - 1).min(axis=1)
        final = eq[:, -1]
        losing = draws <= 0
        streaks = np.zeros(sims, int)
        cur = np.zeros(sims, int)
        for j in range(n):
            cur = np.where(losing[:, j], cur + 1, 0)
            streaks = np.maximum(streaks, cur)
        out["horizons"][str(n)] = {
            "median_return_pct": round(float(np.median(final) - 1) * 100, 2), "p5_return_pct": round(float(np.percentile(final, 5) - 1) * 100, 2),
            "p95_return_pct": round(float(np.percentile(final, 95) - 1) * 100, 2), "prob_loss_pct": round(float((final < 1).mean()) * 100, 1),
            "prob_dd_10_pct": round(float((mdd <= -0.10).mean()) * 100, 1), "prob_dd_20_pct": round(float((mdd <= -0.20).mean()) * 100, 1),
            "prob_ruin_50_pct": round(float((eq.min(axis=1) <= 0.5).mean()) * 100, 2),
            "median_max_dd_pct": round(float(np.median(mdd)) * 100, 2), "worst_5pct_max_dd_pct": round(float(np.percentile(mdd, 5)) * 100, 2),
            "median_longest_losing_streak": int(np.median(streaks)), "p95_longest_losing_streak": int(np.percentile(streaks, 95))}
    h = out["horizons"][str(horizons[-1])]
    out["survives"] = bool(h["prob_ruin_50_pct"] < 1 and h["prob_dd_20_pct"] < 25)
    out["note"] = "Resampled from historical simulated trades with up to 0.05R extra random slippage each; it shows a range, not a promise."
    return out
