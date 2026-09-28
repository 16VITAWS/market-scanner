"""
Market regime detection: 3-state Gaussian Hidden Markov Model on NIFTY (numpy only, no black box).

Observations per day: log return and 10-day realised volatility (both standardised).
States are named AFTER fitting by their average daily return: highest = BULL, lowest = BEAR,
middle = SIDEWAYS. The "current regime" uses FILTERED probabilities (forward pass only: each day uses
data up to that day), so the label shown for a past day is what the model would have said on that day.
Expected duration of a regime = 1 / (1 - p_stay) from the transition matrix.

Out-of-sample check: parameters fitted on all data except the last 252 sessions; the last 252 sessions are
then labelled with those frozen parameters and we report the average NEXT-5-DAY NIFTY return in each label.
If the labels do not separate future returns, the portal says the regime is descriptive, not predictive.
The trading engine's own simple regime rule (close vs 200-DMA etc.) is unchanged; this model is shown beside it.
"""
import math
import numpy as np
import pandas as pd

NAMES = ("BEAR", "SIDEWAYS", "BULL")


def _features(close):
    c = close.copy(); c.index = pd.to_datetime(c.index.date); c = c[~c.index.duplicated(keep="last")]
    r = np.log(c).diff()
    v = r.rolling(10).std() * math.sqrt(252)
    X = pd.DataFrame({"r": r, "v": v}).dropna()
    return X, c


def _logpdf(X, mu, var):
    # diagonal gaussian, X (T,d), mu (K,d), var (K,d) -> (T,K)
    return -0.5 * (np.log(2 * np.pi * var)[None, :, :] + (X[:, None, :] - mu[None, :, :]) ** 2 / var[None, :, :]).sum(-1)


def _forward(logB, A, pi):
    T, K = logB.shape
    alpha = np.zeros((T, K)); scale = np.zeros(T)
    B = np.exp(logB - logB.max(1, keepdims=True))
    a = pi * B[0]; scale[0] = a.sum(); alpha[0] = a / scale[0]
    for t in range(1, T):
        a = (alpha[t - 1] @ A) * B[t]
        scale[t] = a.sum() or 1e-300; alpha[t] = a / scale[t]
    ll = float(np.log(scale).sum() + logB.max(1).sum())
    return alpha, scale, B, ll


def fit(Z, K=3, iters=60, tol=1e-5):
    T, d = Z.shape
    q = np.quantile(Z[:, 0], np.linspace(0, 1, K + 1))
    lab = np.clip(np.searchsorted(q[1:-1], Z[:, 0]), 0, K - 1)
    mu = np.array([Z[lab == k].mean(0) if (lab == k).any() else Z.mean(0) for k in range(K)])
    var = np.array([Z[lab == k].var(0) + 1e-3 if (lab == k).sum() > 2 else Z.var(0) for k in range(K)])
    A = np.full((K, K), 0.05 / (K - 1)); np.fill_diagonal(A, 0.95)
    pi = np.full(K, 1 / K)
    prev = -np.inf
    for _ in range(iters):
        logB = _logpdf(Z, mu, var)
        alpha, scale, B, ll = _forward(logB, A, pi)
        beta = np.zeros((T, K)); beta[-1] = 1
        for t in range(T - 2, -1, -1):
            beta[t] = (A @ (B[t + 1] * beta[t + 1])) / scale[t + 1]
        gamma = alpha * beta; gamma /= gamma.sum(1, keepdims=True)
        xi = np.zeros((K, K))
        for t in range(T - 1):
            m = alpha[t][:, None] * A * (B[t + 1] * beta[t + 1])[None, :] / scale[t + 1]
            xi += m / (m.sum() or 1)
        A = xi / xi.sum(1, keepdims=True)
        pi = gamma[0]
        w = gamma.sum(0)
        mu = (gamma.T @ Z) / w[:, None]
        var = np.array([(gamma[:, k][:, None] * (Z - mu[k]) ** 2).sum(0) / w[k] for k in range(K)]) + 1e-4
        if abs(ll - prev) < tol * abs(ll):
            break
        prev = ll
    return {"mu": mu, "var": var, "A": A, "pi": pi, "ll": ll}


def filtered(Z, p):
    alpha, _, _, _ = _forward(_logpdf(Z, p["mu"], p["var"]), p["A"], p["pi"])
    return alpha


def run(nifty_df, min_obs=500, oos_days=252):
    X, c = _features(nifty_df["Close"])
    if len(X) < min_obs:
        return {"available": False, "reason": f"needs {min_obs}+ sessions (has {len(X)})"}
    m, s = X.mean().values, X.std().values
    Z = (X.values - m) / s
    p = fit(Z)
    order = np.argsort(p["mu"][:, 0])                       # low mean return -> BEAR ... high -> BULL
    name = {int(order[i]): NAMES[i] for i in range(3)}
    alpha = filtered(Z, p)
    lab = alpha.argmax(1)
    cur = int(lab[-1])
    days_in = 1
    for k in lab[-2::-1]:
        if k != cur:
            break
        days_in += 1
    states = []
    for k in order[::-1]:
        k = int(k)
        mret = p["mu"][k, 0] * s[0] + m[0]
        mvol = p["mu"][k, 1] * s[1] + m[1]
        stay = float(p["A"][k, k])
        states.append({"state": name[k], "character": ("volatile" if mvol > float(np.median(X["v"])) * 1.3 else "calm") + (" uptrend" if mret > 0.0003 else " downtrend" if mret < -0.0002 else " drift"), "avg_daily_return_pct": round(mret * 100, 3), "annualised_return_pct": round(mret * 252 * 100, 1),
                       "typical_vol_pct": round(mvol * 100, 1), "p_stay": round(stay, 3), "expected_duration_days": round(1 / max(1 - stay, 1e-3), 1),
                       "share_of_history_pct": round(float((lab == k).mean() * 100), 1)})
    trans = {name[i]: {name[j]: round(float(p["A"][i, j]), 3) for j in range(3)} for i in range(3)}
    # out-of-sample: freeze parameters fitted without the last oos_days
    oos = None
    if len(Z) > min_obs + oos_days:
        p2 = fit(Z[:-oos_days])
        o2 = np.argsort(p2["mu"][:, 0]); n2 = {int(o2[i]): NAMES[i] for i in range(3)}
        lab2 = filtered(Z, p2).argmax(1)[-oos_days:]
        cc = c.reindex(X.index)
        fwd = (cc.shift(-5) / cc - 1).values[-oos_days:]
        rows = []
        for k in range(3):
            msk = (lab2 == k) & ~np.isnan(fwd)
            rows.append({"state": n2[k], "days": int(msk.sum()), "avg_next5d_return_pct": round(float(np.nanmean(fwd[msk]) * 100), 2) if msk.any() else None,
                         "hit_up_pct": round(float((fwd[msk] > 0).mean() * 100), 1) if msk.any() else None})
        rows.sort(key=lambda r: NAMES.index(r["state"]), reverse=True)
        b = next((r for r in rows if r["state"] == "BULL"), None); br = next((r for r in rows if r["state"] == "BEAR"), None)
        sep = bool(b and br and b["avg_next5d_return_pct"] is not None and br["avg_next5d_return_pct"] is not None and b["days"] >= 15 and br["days"] >= 15
                   and b["avg_next5d_return_pct"] > br["avg_next5d_return_pct"])
        oos = {"window_sessions": oos_days, "by_state": rows, "bull_beats_bear_next5d": sep,
               "verdict": "Regime labels separated next-5-day returns in the hold-out year (weak evidence, one period)." if sep else
                          "Regime labels did NOT separate future returns in the hold-out year: treat the regime as a description of the recent past, not a forecast."}
    hist = [{"date": str(d.date()), "close": round(float(c.loc[d]), 2), "state": name[int(k)], "p": round(float(alpha[i].max()), 2)}
            for i, (d, k) in enumerate(zip(X.index, lab))][-500:]
    return {"available": True, "method": "3-state Gaussian HMM (return + 10d vol), filtered probabilities, numpy Baum-Welch",
            "as_of": str(X.index[-1].date()), "current": name[cur], "probabilities": {name[k]: round(float(alpha[-1, k]), 3) for k in range(3)},
            "days_in_regime": days_in, "expected_duration_days": next(x["expected_duration_days"] for x in states if x["state"] == name[cur]),
            "expected_remaining_days_note": "HMM durations are memoryless: expected remaining time does not shrink as the regime ages.",
            "states": states, "transition": trans, "oos": oos, "history": hist,
            "use": {"BULL": "trend-following entries allowed at normal size", "SIDEWAYS": "fewer, smaller entries; favour mean-reversion / defined-risk options",
                    "BEAR": "defensive: no new longs by the swing engine's own rule; hedges / cash"},
            "status": "MODEL (descriptive)"}
