"""
Cross-market linkage monitor.

For each pair (source -> NIFTY/BANKNIFTY/USDINR ...) we compute on daily returns:
  - rolling 60-day correlation now vs its 1-year median (link strengthening / breaking)
  - lead-lag: correlation of source(t) with target(t+1)  (does yesterday's source move line up with today's target?)
  - Granger F-test (lag 1..2): do past source returns add explanatory power for target returns beyond the target's own past?
Everything is statistical association on past data. It is labelled "predictive precedence", never causation.
Alerts fire when a link's current correlation moves > 2 standard deviations from its own 1-year history.
"""
import math
import numpy as np
import pandas as pd

PAIRS = [("SPX", "NIFTY"), ("NDX", "NIFTY"), ("NIKKEI", "NIFTY"), ("HSI", "NIFTY"), ("BRENT", "NIFTY"), ("USDINR", "NIFTY"),
         ("US10Y", "NIFTY"), ("VIX", "INDIAVIX"), ("DXY", "USDINR"), ("BRENT", "USDINR"), ("GOLD", "NIFTY"), ("SPX", "BANKNIFTY"),
         ("NDX", "NIFTY"), ("FTSE", "NIFTY"), ("DAX", "NIFTY")]
PAIRS = list(dict.fromkeys(PAIRS))


def _ret(df, level=False):
    s = df["Close"].copy(); s.index = pd.to_datetime(s.index.date); s = s[~s.index.duplicated(keep="last")]
    return (s.diff() if level else s.pct_change()).dropna()


def _fdist_sf(F, d1, d2):
    """Survival function of the F distribution (scipy if present, else normal approximation)."""
    try:
        from scipy import stats
        return float(stats.f.sf(F, d1, d2))
    except Exception:
        z = ((F ** (1 / 3)) * (1 - 2 / (9 * d2)) - (1 - 2 / (9 * d1))) / math.sqrt(2 / (9 * d1) + (F ** (2 / 3)) * 2 / (9 * d2))
        return 0.5 * math.erfc(z / math.sqrt(2))


def granger(src, tgt, lags=2):
    df = pd.concat([tgt.rename("y"), src.rename("x")], axis=1).dropna()
    for L in range(1, lags + 1):
        df[f"y{L}"] = df["y"].shift(L); df[f"x{L}"] = df["x"].shift(L)
    df = df.dropna()
    if len(df) < 60:
        return None
    y = df["y"].values
    Xr = np.c_[np.ones(len(df)), df[[f"y{L}" for L in range(1, lags + 1)]].values]
    Xu = np.c_[Xr, df[[f"x{L}" for L in range(1, lags + 1)]].values]
    rss = lambda X: float(((y - X @ np.linalg.lstsq(X, y, rcond=None)[0]) ** 2).sum())
    rr, ru = rss(Xr), rss(Xu)
    d1, d2 = lags, len(y) - Xu.shape[1]
    F = ((rr - ru) / d1) / (ru / d2) if ru > 0 else 0.0
    return {"F": round(F, 3), "p": round(_fdist_sf(F, d1, d2), 4), "n": int(len(y)), "lags": lags}


def run(frames, window=60, year=250):
    out, alerts = [], []
    for s, t in PAIRS:
        if s not in frames or t not in frames:
            continue
        rs = _ret(frames[s], level=(s == "US10Y")); rt = _ret(frames[t])
        j = pd.concat([rs.rename("s"), rt.rename("t")], axis=1).dropna()
        if len(j) < window + 30:
            continue
        roll = j["s"].rolling(window).corr(j["t"]).dropna()
        now = float(roll.iloc[-1]); hist = roll.tail(year)
        med, sd = float(hist.median()), float(hist.std() or 1e-9)
        z = (now - float(hist.mean())) / sd
        lead = pd.concat([j["s"].shift(1), j["t"]], axis=1).dropna().tail(year)
        lead_corr = float(lead.corr().iloc[0, 1]) if len(lead) > 30 else None
        g = granger(j["s"].tail(year * 2), j["t"].tail(year * 2))
        row = {"source": s, "target": t, "corr_now": round(now, 3), "corr_1y_median": round(med, 3), "z": round(z, 2),
               "lead1_corr": round(lead_corr, 3) if lead_corr is not None else None, "granger": g,
               "precedence": bool(g and g["p"] < 0.05), "series": [round(float(v), 3) for v in roll.tail(250).values],
               "series_dates": [d.strftime("%Y-%m-%d") for d in roll.tail(250).index]}
        out.append(row)
        if abs(z) >= 2:
            alerts.append({"pair": f"{s}->{t}", "kind": "link strengthening" if abs(now) > abs(med) else "link breaking",
                           "text": f"{s}-{t} 60-day correlation {now:+.2f} vs 1-year median {med:+.2f} ({z:+.1f} sd)", "z": round(z, 2)})
    return {"links": out, "alerts": alerts, "window": window,
            "note": "Correlation and Granger tests measure statistical association and predictive precedence on past daily data. They are not proof of cause and they change over time."}
