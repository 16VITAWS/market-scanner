"""
Market regime engine. Several independent signal groups vote; one state is chosen by a fixed priority so
the result is explainable. Computed for EVERY historical day with data up to that day only, so the
calibration can measure each strategy per regime without look-ahead.

States: STRONG_BULL, WEAK_BULL, STRONG_BEAR, WEAK_BEAR, SIDEWAYS, HIGH_VOL, LOW_VOL, BREAKOUT, BREAKDOWN,
        PANIC, RECOVERY, EVENT, UNSAFE, UNCERTAIN
"""
import math
import numpy as np
import pandas as pd
from .. import indicators as I

STATES = ["STRONG_BULL", "WEAK_BULL", "STRONG_BEAR", "WEAK_BEAR", "SIDEWAYS", "HIGH_VOL", "LOW_VOL", "BREAKOUT", "BREAKDOWN",
          "PANIC", "RECOVERY", "EVENT", "UNSAFE", "UNCERTAIN"]

# strategy permission matrix: which long setups may open new trades, position-size multiplier, required-edge multiplier
MATRIX = {
    "STRONG_BULL": {"allow": ["TREND_MOMENTUM", "BREAKOUT", "PULLBACK", "REL_STRENGTH", "VOL_SQUEEZE"], "size": 1.0, "edge": 1.0,
                    "note": "trend following, momentum and pullbacks"},
    "WEAK_BULL": {"allow": ["PULLBACK", "REL_STRENGTH", "TREND_MOMENTUM", "MEAN_REVERSION"], "size": 0.75, "edge": 1.2,
                  "note": "selective: pullbacks and leaders only"},
    "BREAKOUT": {"allow": ["BREAKOUT", "TREND_MOMENTUM", "VOL_SQUEEZE", "REL_STRENGTH"], "size": 1.0, "edge": 1.0,
                 "note": "volume-confirmed breakouts and momentum"},
    "RECOVERY": {"allow": ["REL_STRENGTH", "PULLBACK", "MEAN_REVERSION"], "size": 0.5, "edge": 1.5,
                 "note": "early recovery: leaders and reversions, small size"},
    "SIDEWAYS": {"allow": ["MEAN_REVERSION", "PULLBACK"], "size": 0.75, "edge": 1.2, "note": "range: mean reversion"},
    "LOW_VOL": {"allow": ["VOL_SQUEEZE", "BREAKOUT", "PULLBACK"], "size": 0.75, "edge": 1.2, "note": "quiet market: prepare for breakouts"},
    "HIGH_VOL": {"allow": ["MEAN_REVERSION", "REL_STRENGTH"], "size": 0.5, "edge": 1.75, "note": "high volatility: half size, bigger edge required"},
    "WEAK_BEAR": {"allow": ["REL_STRENGTH"], "size": 0.5, "edge": 1.75,
                  "note": "falling market: cash equity is long-only, so only the strongest leaders at half size"},
    "STRONG_BEAR": {"allow": [], "size": 0.0, "edge": 99, "note": "strong downtrend: no new long trades (cash equity cannot be shorted overnight)"},
    "BREAKDOWN": {"allow": [], "size": 0.0, "edge": 99, "note": "index breaking down: protect capital"},
    "PANIC": {"allow": [], "size": 0.0, "edge": 99, "note": "panic: capital preservation, exits only"},
    "EVENT": {"allow": [], "size": 0.0, "edge": 99, "note": "major scheduled event: wait"},
    "UNSAFE": {"allow": [], "size": 0.0, "edge": 99, "note": "data stale / illiquid: data-safety lock"},
    "UNCERTAIN": {"allow": [], "size": 0.0, "edge": 99, "note": "signals conflict: WAIT"},
}


def breadth_frame(closes):
    """closes: DataFrame date x symbol. Returns per-date breadth: % above EMA20/EMA50/SMA200, advance ratio, net new 52w highs."""
    c = closes.sort_index()
    e20 = c.ewm(span=20, adjust=False).mean()
    e50 = c.ewm(span=50, adjust=False).mean()
    s200 = c.rolling(200, min_periods=150).mean()
    valid = c.notna()
    n = valid.sum(axis=1).replace(0, np.nan)
    out = pd.DataFrame(index=c.index)
    out["above20"] = (c > e20).where(valid).sum(axis=1) / n
    out["above50"] = (c > e50).where(valid).sum(axis=1) / n
    out["above200"] = (c > s200).where(s200.notna()).sum(axis=1) / s200.notna().sum(axis=1).replace(0, np.nan)
    ch = c.pct_change()
    adv, dec = (ch > 0).sum(axis=1), (ch < 0).sum(axis=1)
    out["adv_ratio"] = adv / (adv + dec).replace(0, np.nan)
    hi = c.rolling(252, min_periods=120).max()
    lo = c.rolling(252, min_periods=120).min()
    out["net_new_hi"] = ((c >= hi).sum(axis=1) - (c <= lo).sum(axis=1)) / n
    out["n"] = n
    return out


def index_frame(idx, vix=None):
    c = idx["Close"].astype(float)
    d = pd.DataFrame(index=idx.index)
    d["close"] = c
    d["ema20"], d["ema50"], d["sma200"] = I.ema(c, 20), I.ema(c, 50), I.sma(c, 200)
    d["slope20"] = d["ema20"] / d["ema20"].shift(5) - 1
    d["adx"] = I.adx(idx, 14)["adx"]
    d["ret1"], d["ret5"], d["ret63"] = c.pct_change(), c.pct_change(5), c.pct_change(63)
    d["rv20"] = I.realized_vol(c, 20)
    d["rv_rank"] = d["rv20"].rolling(500, min_periods=120).rank(pct=True)
    dc = I.donchian(idx, 20)
    d["don_hi"], d["don_lo"] = dc["upper"], dc["lower"]
    d["dd63"] = c / c.rolling(63).max() - 1
    if vix is not None and len(vix):
        v = vix["Close"].astype(float).reindex(d.index).ffill(limit=3)
        d["vix"] = v
        d["vix_chg"] = v.pct_change()
        d["vix_rank"] = v.rolling(500, min_periods=120).rank(pct=True)
    else:
        d["vix"] = d["vix_chg"] = d["vix_rank"] = np.nan
    return d


def _f(x):
    return None if x is None or (isinstance(x, float) and math.isnan(x)) else float(x)


def classify(ix, br=None, event=False, data_ok=True):
    """ix: one row of index_frame (dict-like); br: one row of breadth_frame. Returns (state, signals dict, reasons)."""
    g = lambda k: _f(ix.get(k)) if hasattr(ix, "get") else None
    b = (lambda k: _f(br.get(k))) if br is not None else (lambda k: None)
    sig, why = {}, []
    if not data_ok:
        return "UNSAFE", sig, ["market data stale or missing - data-safety lock"]
    if event:
        return "EVENT", sig, ["major scheduled event today"]
    c, e20, e50, s200 = g("close"), g("ema20"), g("ema50"), g("sma200")
    if None in (c, e20, e50):
        return "UNCERTAIN", sig, ["not enough index history"]
    # 1. trend vote (-2..+2)
    t = 0
    t += 1 if c > e20 else -1
    t += 1 if e20 > e50 else -1
    if s200 is not None:
        t += 1 if c > s200 else -1
    if (g("slope20") or 0) > 0.002:
        t += 1
    elif (g("slope20") or 0) < -0.002:
        t -= 1
    sig["trend_vote"] = t
    adx = g("adx") or 0
    sig["adx"] = round(adx, 1)
    # 2. volatility
    rvr, vix, vixr, vixc = g("rv_rank"), g("vix"), g("vix_rank"), g("vix_chg")
    sig.update(rv_rank=rvr, vix=vix, vix_rank=vixr)
    high_vol = (rvr is not None and rvr >= 0.85) or (vixr is not None and vixr >= 0.85)
    low_vol = (rvr is not None and rvr <= 0.15) and not (vixr is not None and vixr >= 0.5)
    # 3. breadth
    a50, a200, adv = b("above50"), b("above200"), b("adv_ratio")
    sig.update(breadth50=a50, breadth200=a200, adv_ratio=adv)
    breadth_vote = 0
    if a50 is not None:
        breadth_vote = 1 if a50 >= 0.6 else -1 if a50 <= 0.4 else 0
    # 4. shock
    r1, r5 = g("ret1") or 0, g("ret5") or 0
    if r1 <= -0.03 or r5 <= -0.07 or (vix is not None and vix >= 30) or (vixc is not None and vixc >= 0.25):
        why.append(f"shock: 1-day {r1:+.1%}, 5-day {r5:+.1%}" + (f", India VIX {vix:.1f}" if vix else ""))
        return "PANIC", sig, why
    # 5. breakout / breakdown of the 20-day range
    dh, dl = g("don_hi"), g("don_lo")
    if dh and c > dh and t >= 1 and breadth_vote >= 0:
        why.append("index closed above its 20-day high with trend support")
        return "BREAKOUT", sig, why
    if dl and c < dl and t <= -1:
        why.append("index closed below its 20-day low")
        return "BREAKDOWN", sig, why
    if high_vol:
        why.append("volatility in the top 15% of its history")
        return "HIGH_VOL", sig, why
    # 6. recovery: after a >8% drawdown, price back above EMA20 with EMA20 turning up
    if (g("dd63") or 0) <= -0.08 and c > e20 and (g("slope20") or 0) > 0:
        why.append("recovering from a drawdown of more than 8%")
        return "RECOVERY", sig, why
    # 7. trend states (breadth must not strongly contradict)
    if t >= 3 and adx >= 20 and breadth_vote >= 0:
        why.append("price > EMA20 > EMA50 > SMA200, rising, ADX >= 20, breadth supportive")
        return "STRONG_BULL", sig, why
    if t <= -3 and adx >= 20 and breadth_vote <= 0:
        why.append("price < EMA20 < EMA50 < SMA200, falling, ADX >= 20, breadth weak")
        return "STRONG_BEAR", sig, why
    if t >= 3 and breadth_vote < 0:
        why.append("index trend up but most stocks below their 50-day average - conflict")
        return "UNCERTAIN", sig, why
    if t <= -3 and breadth_vote > 0:
        why.append("index trend down but most stocks above their 50-day average - conflict")
        return "UNCERTAIN", sig, why
    if adx < 18 and abs(t) <= 2:
        why.append(f"ADX {adx:.0f} < 18: no trend, range-bound")
        return "LOW_VOL" if low_vol else "SIDEWAYS", sig, why
    if t >= 1:
        why.append("mild uptrend")
        return "WEAK_BULL", sig, why
    if t <= -1:
        why.append("mild downtrend")
        return "WEAK_BEAR", sig, why
    if low_vol:
        return "LOW_VOL", sig, ["very low volatility"]
    return "UNCERTAIN", sig, ["mixed signals"]


def series(idx, breadth=None, vix=None):
    """Regime for every day (uses only data up to that day). Returns pd.Series of states."""
    ixf = index_frame(idx, vix)
    out = {}
    for t, row in ixf.iterrows():
        br = breadth.loc[t] if breadth is not None and t in breadth.index else None
        out[t] = classify(row, br)[0]
    return pd.Series(out)


def current(idx, breadth=None, vix=None, event=False, data_ok=True):
    ixf = index_frame(idx, vix)
    row = ixf.iloc[-1]
    br = breadth.iloc[-1] if breadth is not None and len(breadth) else None
    st, sig, why = classify(row, br, event, data_ok)
    return {"state": st, "date": str(ixf.index[-1].date()), "signals": {k: (round(v, 3) if isinstance(v, float) else v) for k, v in sig.items()},
            "reasons": why, "matrix": MATRIX[st]}


GLOBAL = [("SPX", 1.0, "S&P 500"), ("NDX", 1.0, "Nasdaq 100"), ("DJI", 0.5, "Dow"), ("NIKKEI", 0.75, "Nikkei"), ("DAX", 0.5, "DAX"),
          ("USDINR", -0.75, "USD/INR (rupee weakness is risk-off)"), ("BRENT", -0.5, "Brent crude (rise hurts India)"),
          ("VIX", -1.0, "CBOE VIX"), ("US10Y", -0.5, "US 10y yield"), ("GOLD", -0.25, "Gold (flight to safety)")]


def global_risk(frames):
    """-100 (risk-off) .. +100 (risk-on) from the latest daily moves, each normalised by its own 1-year volatility."""
    parts, tot, wsum = [], 0.0, 0.0
    for k, w, name in GLOBAL:
        df = frames.get(k)
        if df is None or len(df) < 60:
            parts.append({"id": k, "name": name, "status": "DATA UNAVAILABLE"})
            continue
        r = df["Close"].astype(float).pct_change().dropna()
        z = float(r.iloc[-1] / (r.tail(250).std() or np.nan))
        if math.isnan(z):
            continue
        z = max(-3.0, min(3.0, z))
        tot += w * z
        wsum += abs(w)
        parts.append({"id": k, "name": name, "move_pct": round(float(r.iloc[-1]) * 100, 2), "z": round(z, 2), "weight": w,
                      "date": str(df.index[-1].date())})
    score = round(max(-100, min(100, tot / wsum / 3 * 100)), 1) if wsum else None
    label = None if score is None else "RISK-ON" if score >= 25 else "RISK-OFF" if score <= -25 else "NEUTRAL"
    return {"score": score, "label": label, "parts": parts,
            "note": "GIFT NIFTY is not available from free sources; this score uses the last completed session of each market."}
