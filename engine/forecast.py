"""
Next-day NIFTY direction forecast.

Features (all known BEFORE the NIFTY session they predict):
  - NIFTY's own last return, 5-day and 20-day momentum, RSI, 20-day realised vol
  - Overnight moves that finish before the Indian open: S&P 500, Nasdaq 100, Nikkei (same-day
    Tokyo close happens before NSE closes, so only the PREVIOUS Nikkei session is used), Brent,
    USD/INR, US 10Y, VIX, India VIX change
Target: sign of NIFTY's next close-to-close return; also its size (for a range).

Model: L2 logistic regression (numpy, no black box) trained walk-forward: every day's forecast
is produced by a model fit only on data before that day. We report the out-of-sample hit rate,
Brier score and a reliability table so the probability can be judged, and we compare against the
naive "always up" baseline. If the model does not beat the baseline out of sample, the portal says so.
"""
import math, datetime as dt
import numpy as np
import pandas as pd

FEATS_EXT = [("SPX", "spx"), ("NDX", "ndx"), ("NIKKEI", "nikkei"), ("BRENT", "brent"), ("USDINR", "usdinr"),
             ("US10Y", "us10y"), ("VIX", "vix"), ("INDIAVIX", "indiavix")]


def _dates(df):
    s = df["Close"].copy()
    s.index = pd.to_datetime(s.index.date)
    return s[~s.index.duplicated(keep="last")]


def build(frames):
    """Return (X, y, ret_next, index) aligned on NIFTY dates. Row t uses information available before session t+1."""
    n = _dates(frames["NIFTY"])
    r = n.pct_change()
    X = pd.DataFrame(index=n.index)
    X["r1"] = r
    X["m5"] = n / n.shift(5) - 1
    X["m20"] = n / n.shift(20) - 1
    d = r.clip(lower=0).ewm(alpha=1 / 14, adjust=False).mean() / (-r.clip(upper=0)).ewm(alpha=1 / 14, adjust=False).mean().replace(0, np.nan)
    X["rsi"] = (100 - 100 / (1 + d)) / 100
    X["vol20"] = r.rolling(20).std() * math.sqrt(252)
    used = []
    for k, name in FEATS_EXT:
        if k not in frames or len(frames[k]) < 60:
            continue
        s = _dates(frames[k])
        chg = s.pct_change() if k not in ("US10Y",) else s.diff() / 10
        # align: for NIFTY session t+1 we may use external closes dated <= t (US closes after India; fine for next day)
        X[name] = chg.reindex(X.index, method="ffill")
        used.append(k)
    y_ret = r.shift(-1)
    X = X.replace([np.inf, -np.inf], np.nan)
    return X, y_ret, used


def _fit_logit(X, y, l2=1.0, iters=300, lr=0.1):
    Xb = np.c_[np.ones(len(X)), X]
    w = np.zeros(Xb.shape[1])
    for _ in range(iters):
        p = 1 / (1 + np.exp(-Xb @ w))
        g = Xb.T @ (p - y) / len(y) + l2 * np.r_[0, w[1:]] / len(y)
        w -= lr * g
    return w


def _pred(w, X):
    return 1 / (1 + np.exp(-(np.c_[np.ones(len(X)), X] @ w)))


def run(frames, min_train=500, step=21):
    if "NIFTY" not in frames or len(frames["NIFTY"]) < min_train + 60:
        return {"available": False, "reason": f"needs >= {min_train + 60} NIFTY sessions"}
    X, yret, used = build(frames)
    data = X.join(yret.rename("y")).dropna(subset=list(X.columns))
    hist = data.dropna(subset=["y"])
    live_row = data.iloc[[-1]]
    cols = list(X.columns)
    preds = []
    # walk-forward: refit every `step` days on all prior data
    for start in range(min_train, len(hist), step):
        tr, te = hist.iloc[:start], hist.iloc[start:start + step]
        mu, sd = tr[cols].mean(), tr[cols].std().replace(0, 1)
        w = _fit_logit(((tr[cols] - mu) / sd).values, (tr["y"] > 0).astype(float).values)
        p = _pred(w, ((te[cols] - mu) / sd).values)
        preds.append(pd.DataFrame({"p_up": p, "y": te["y"].values}, index=te.index))
    oos = pd.concat(preds) if preds else pd.DataFrame(columns=["p_up", "y"])
    up = (oos["y"] > 0).astype(float)
    hit = float(((oos["p_up"] > 0.5) == (oos["y"] > 0)).mean()) if len(oos) else None
    base = float(max(up.mean(), 1 - up.mean())) if len(oos) else None
    brier = float(((oos["p_up"] - up) ** 2).mean()) if len(oos) else None
    brier_base = float(((up.mean() - up) ** 2).mean()) if len(oos) else None
    rel = []
    if len(oos):
        bins = pd.cut(oos["p_up"], [0, .4, .45, .5, .55, .6, 1])
        for b, g in oos.groupby(bins, observed=True):
            rel.append({"bin": str(b), "n": int(len(g)), "predicted": round(float(g["p_up"].mean()), 3), "actual_up": round(float((g["y"] > 0).mean()), 3)})
    # final model on all history -> tomorrow
    mu, sd = hist[cols].mean(), hist[cols].std().replace(0, 1)
    w = _fit_logit(((hist[cols] - mu) / sd).values, (hist["y"] > 0).astype(float).values)
    p_up = float(_pred(w, ((live_row[cols] - mu) / sd).values)[0])
    vol = float(hist["y"].tail(60).std())
    last_close = float(frames["NIFTY"]["Close"].iloc[-1])
    skill = (hit is not None and base is not None and hit > base + 0.01 and brier < brier_base)
    lean = "UP" if p_up > 0.55 else "DOWN" if p_up < 0.45 else "NO CLEAR EDGE"
    contrib = sorted([{"feature": c, "weight": round(float(wv), 3), "today_z": round(float(((live_row[c] - mu[c]) / sd[c]).iloc[0]), 2),
                       "push": round(float(wv * ((live_row[c] - mu[c]) / sd[c]).iloc[0]), 3)} for c, wv in zip(cols, w[1:])], key=lambda x: -abs(x["push"]))
    return {"available": True, "for_session_after": str(live_row.index[-1].date()), "p_up": round(p_up, 3), "p_down": round(1 - p_up, 3), "lean": lean,
            "expected_move_1sd_pct": round(vol * 100, 2), "range_1sd": [round(last_close * (1 - vol), 0), round(last_close * (1 + vol), 0)],
            "last_close": round(last_close, 2), "features_used": cols, "external_markets": used, "contributions": contrib[:8],
            "oos": {"n": int(len(oos)), "hit_rate": round(hit, 3) if hit is not None else None, "baseline_hit": round(base, 3) if base is not None else None,
                    "brier": round(brier, 4) if brier is not None else None, "brier_baseline": round(brier_base, 4) if brier_base is not None else None,
                    "reliability": rel, "has_skill": bool(skill)},
            "note": ("Walk-forward, out-of-sample. " + ("Beats the naive baseline so far." if skill else "Does NOT beat the naive baseline out of sample - treat as no edge.")
                     + " Direction forecasts for indices are rarely much better than a coin flip; this is context, not a trade trigger.")}
