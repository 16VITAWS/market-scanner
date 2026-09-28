"""
Unusual-activity detector (end-of-day and intraday).

Flags, per symbol, on the latest bar vs its own trailing 60-bar history (never including the bar itself):
  - volume z-score >= 3  (log volume, robust)
  - return z-score >= 3  (|return| vs 60-day std)
  - gap >= 3 x ATR% at the open
  - sector divergence: stock return minus its sector median return beyond 3 sector-sds
Each flag carries the numbers that produced it. Nothing here is a trade signal on its own.
"""
import numpy as np
import pandas as pd


def symbol_flags(df, n=60):
    if len(df) < n + 2:
        return []
    last, hist = df.iloc[-1], df.iloc[-n - 1:-1]
    flags = []
    lv = np.log(hist["Volume"].replace(0, np.nan)).dropna()
    if len(lv) > 20 and last.Volume > 0 and lv.std() > 0:
        z = (np.log(last.Volume) - lv.mean()) / lv.std()
        if z >= 3:
            flags.append({"type": "volume spike", "z": round(float(z), 1), "detail": f"volume {int(last.Volume):,} vs 60-day typical {int(np.exp(lv.mean())):,}"})
    r = hist["Close"].pct_change().dropna()
    ret = float(last.Close / hist["Close"].iloc[-1] - 1)
    if r.std() > 0:
        z = ret / r.std()
        if abs(z) >= 3:
            flags.append({"type": "big move", "z": round(float(z), 1), "detail": f"{ret*100:+.2f}% vs 60-day daily sd {r.std()*100:.2f}%"})
    tr = pd.concat([hist["High"] - hist["Low"], (hist["High"] - hist["Close"].shift()).abs(), (hist["Low"] - hist["Close"].shift()).abs()], axis=1).max(axis=1)
    atrp = float(tr.tail(14).mean() / hist["Close"].iloc[-1])
    gap = float(last.Open / hist["Close"].iloc[-1] - 1)
    if atrp > 0 and abs(gap) >= 3 * atrp:
        flags.append({"type": "gap", "z": round(gap / atrp, 1), "detail": f"opened {gap*100:+.2f}% vs ATR {atrp*100:.2f}%"})
    for f in flags:
        f["return_pct"] = round(ret * 100, 2)
    return flags


def run(frames, sectors=None, universe=None):
    out = []
    rets = {}
    for k, df in frames.items():
        if universe is not None and k not in universe:
            continue
        try:
            fl = symbol_flags(df)
        except Exception:
            continue
        if len(df) > 2:
            rets[k] = float(df["Close"].iloc[-1] / df["Close"].iloc[-2] - 1)
        for f in fl:
            out.append({"symbol": k, "date": str(df.index[-1].date()), **f})
    if sectors:
        s = pd.Series(rets)
        sec = pd.Series({k: sectors.get(k) for k in s.index}).dropna()
        for name, members in sec.groupby(sec).groups.items():
            m = s.reindex(members).dropna()
            if len(m) < 5 or m.std() == 0:
                continue
            med, sd = m.median(), m.std()
            for k, v in m.items():
                z = (v - med) / sd
                if abs(z) >= 3:
                    out.append({"symbol": k, "date": str(frames[k].index[-1].date()), "type": "sector divergence", "z": round(float(z), 1),
                                "detail": f"{v*100:+.2f}% vs {name} median {med*100:+.2f}%", "return_pct": round(v * 100, 2)})
    out.sort(key=lambda x: -abs(x["z"]))
    return {"items": out[:150], "count": len(out), "note": "Statistical outliers vs each symbol's own 60-day history (and its sector). Context for investigation, not trade signals."}
