"""
0-100 TRADE QUALITY SCORE (a model score - NOT a probability; the probability comes from calibrate.Model).
Every point is itemised. Components that cannot be measured from the data this engine has are shown as
DATA UNAVAILABLE and excluded from the denominator, so the normalised score is not inflated or deflated.
"""
import math

WEIGHTS = {"trend": 15, "momentum": 10, "structure": 10, "volume": 10, "volatility": 10, "options_futures": 10,
           "multi_timeframe": 10, "breadth": 5, "sector": 5, "news_event": 5, "liquidity": 5, "history": 5}
TECH = ["trend", "momentum", "structure", "volume", "volatility", "multi_timeframe", "liquidity"]


def _v(r, k):
    try:
        x = r[k]
    except (KeyError, IndexError):
        return None
    if x is None:
        return None
    x = float(x)
    return None if math.isnan(x) else x


def components(r, ctx=None):
    """r: feature row. ctx: optional {breadth50, sector_rs, event_flags(list)|None, history: dict from Model.estimate}."""
    ctx = ctx or {}
    c, e20, e50, s200 = _v(r, "close"), _v(r, "ema20"), _v(r, "ema50"), _v(r, "sma200")
    out = {}
    pts = 0
    if None not in (c, e20, e50):
        pts += 5 if (c > e20 > e50) else 2 if c > e50 else 0
    if s200 is not None and c is not None:
        pts += 5 if c > s200 and (_v(r, "slope200") or 0) > 0 else 2 if c > s200 else 0
    adx = _v(r, "adx")
    pts += 5 if adx and adx >= 25 else 3 if adx and adx >= 20 else 0
    out["trend"] = (pts, "MA stack, 200-day slope, ADX")
    pts = (3 if (_v(r, "roc21") or 0) > 0 else 0) + (3 if (_v(r, "roc63") or 0) > 0 else 0)
    rsi = _v(r, "rsi")
    pts += 4 if rsi and 50 <= rsi <= 70 else 2 if rsi and 40 <= rsi < 50 else 0
    out["momentum"] = (pts, "1- and 3-month rate of change, RSI zone")
    d = _v(r, "dist20_atr")
    pts = (4 if (_v(r, "hh_hl") or 0) > 0 else 0) + (3 if c and _v(r, "don_hi") and c >= 0.98 * _v(r, "don_hi") else 0) + (3 if d is not None and d < 2.5 else 0)
    out["structure"] = (pts, "higher highs / lows, near 20-day high, not over-extended")
    pts = (5 if (_v(r, "vol_ratio") or 0) >= 1.2 else 2 if (_v(r, "vol_ratio") or 0) >= 0.9 else 0) + (5 if (_v(r, "updown_vol") or 0) > 1.1 else 2 if (_v(r, "updown_vol") or 0) > 0.9 else 0)
    out["volume"] = (pts, "volume vs 20-day average, up-day vs down-day volume")
    ap, rr = _v(r, "atr_pct"), _v(r, "rv_rank")
    pts = (5 if ap and 0.01 <= ap <= 0.04 else 2 if ap and ap <= 0.06 else 0) + (5 if rr is not None and rr < 0.85 else 0)
    out["volatility"] = (pts, "daily range 1-4% and volatility not in its top 15%")
    out["options_futures"] = (None, "DATA UNAVAILABLE in the daily engine (option-chain intelligence runs live on the laptop for NIFTY only)")
    out["multi_timeframe"] = ((5 if (_v(r, "wk_up") or 0) > 0 else 0) + (5 if (_v(r, "mo_up") or 0) > 0 else 0), "weekly trend and 6-month direction agree")
    b = ctx.get("breadth50")
    out["breadth"] = (None, "breadth not available") if b is None else ((5 if b >= 0.55 else 2 if b >= 0.45 else 0), f"{b:.0%} of stocks above their 50-day EMA")
    srs = ctx.get("sector_rs")
    out["sector"] = (None, "sector unknown") if srs is None else ((5 if srs > 0.03 else 3 if srs > 0 else 0), f"sector median 3-month relative strength {srs:+.1%}")
    ev = ctx.get("event_flags")
    out["news_event"] = (None, "event check not run for this symbol") if ev is None else ((0 if ev else 5), ("; ".join(ev) if ev else "no results / ex-date / ban / governance flag found"))
    t = _v(r, "turnover_cr")
    out["liquidity"] = ((5 if t and t >= 50 else 3 if t and t >= 10 else 0), f"20-day average traded value ₹{t:.0f} cr" if t else "turnover unknown")
    h = ctx.get("history")
    if not h or h.get("p") is None:
        out["history"] = (None, "no strategy history")
    else:
        e = h.get("exp_net_pct") or 0
        out["history"] = ((5 if e > 0.3 and h.get("evidence") == "OK" else 3 if e > 0 else 0), f"historical net expectancy {e:+.2f}% per trade (n={h.get('n_strategy')})")
    return out


def total(comp):
    got = sum(v[0] for k, v in comp.items() if v[0] is not None)
    avail = sum(WEIGHTS[k] for k, v in comp.items() if v[0] is not None)
    return {"raw": got, "available": avail, "score": round(got / avail * 100, 1) if avail else 0.0,
            "items": {k: {"points": v[0], "max": WEIGHTS[k], "detail": v[1]} for k, v in comp.items()}}


def technical(r):
    """Score from the technical components only (what the history can reproduce exactly) - used for calibration buckets."""
    comp = components(r)
    got = sum(comp[k][0] for k in TECH)
    return round(got / sum(WEIGHTS[k] for k in TECH) * 100, 1)


def band(s):
    return ("NO TRADE" if s < 40 else "WEAK / WATCH" if s < 55 else "LOW CONVICTION" if s < 65 else "VALID" if s < 75 else
            "STRONG" if s < 85 else "VERY STRONG" if s < 95 else "EXTREME CONFLUENCE")
