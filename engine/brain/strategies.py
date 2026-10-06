"""
Strategy ensemble (long setups in NSE cash equity, daily bars) and ONE shared trade-management rule set.

Each strategy = a vectorised condition on the feature frame (true on day t using data <= t) + a setup builder
(entry reference = close of t, executed at the NEXT open; stop; targets; max holding days).
The same `manage_step` is used by the historical calibration and by the IN-BRAIN paper account, so the
measured statistics describe exactly what the paper account does.
"""
import math
import numpy as np
import pandas as pd

STRATEGIES = {
    "TREND_MOMENTUM": "Trend + momentum: aligned moving averages, rising, strong relative strength, not over-extended",
    "BREAKOUT": "Breakout of the 20-day high on 1.5x volume, closing near the high, long-term trend up",
    "PULLBACK": "Pullback to the 20-day EMA inside an uptrend, buyers stepping back in",
    "MEAN_REVERSION": "Oversold (2 st.dev. below the 20-day mean / RSI < 32) inside a long-term uptrend",
    "REL_STRENGTH": "Market leader: top relative strength, within 5% of its 52-week high",
    "VOL_SQUEEZE": "Volatility contraction (narrowest bands in a year) resolving upward",
}
BENCHMARK = "BASELINE_TREND"      # simple trend benchmark: close > SMA200 and positive 3-month return, fixed 2-ATR stop
PARAMS = {  # stop in ATR, targets in R, max holding sessions
    "TREND_MOMENTUM": (2.0, 1.5, 3.0, 20),
    "BREAKOUT": (1.5, 1.5, 3.0, 15),
    "PULLBACK": (1.5, 1.5, 2.5, 15),
    "MEAN_REVERSION": (1.5, 1.0, 2.0, 7),
    "REL_STRENGTH": (2.5, 2.0, 4.0, 25),
    "VOL_SQUEEZE": (1.5, 2.0, 3.0, 10),
    BENCHMARK: (2.0, 1.5, 3.0, 20),
}
MGMT = {"slippage": 0.001, "be_after_R": 1.0, "trail_after_R": 1.5, "trail_atr": 2.5,
        "ledger_trail_after": 0.05, "ledger_trail": 0.04}   # last two mirror the paper simulator's own trailing rule


def signals(f):
    """f: feature frame. Returns dict strategy -> boolean Series."""
    up_stack = (f.close > f.ema20) & (f.ema20 > f.ema50) & (f.ema50 > f.sma200)
    lt_up = (f.close > f.sma200) & (f.slope200 > 0)
    s = {}
    s["TREND_MOMENTUM"] = up_stack & (f.slope20 > 0) & (f.roc63 > 0.05) & (f.rs63 > 0) & (f.adx > 20) & f.rsi.between(50, 70) & (f.dist20_atr < 2.5)
    s["BREAKOUT"] = (f.close > f.don_hi) & (f.vol_ratio >= 1.5) & (f.close_pos >= 0.7) & lt_up & (f.dist20_atr < 3.5)
    s["PULLBACK"] = up_stack & (f.dist20_atr.between(-0.6, 0.4)) & f.rsi.between(40, 55) & (f.close > f.open) & (f.roc63 > 0)
    s["MEAN_REVERSION"] = lt_up & ((f.z20 <= -2.0) | (f.rsi < 32)) & (f.close_pos >= 0.5)
    s["REL_STRENGTH"] = (f.rs63 > 0.10) & (f.hi52_dist >= -0.05) & (f.close > f.ema50) & (f.roc21 > 0)
    s["VOL_SQUEEZE"] = (f.bbw_rank.shift(1) <= 0.10) & (f.close > f.high.shift(1)) & (f.close > f.ema50) & (f.vol_ratio >= 1.2)
    s[BENCHMARK] = (f.close > f.sma200) & (f.roc63 > 0)
    return {k: v.fillna(False) for k, v in s.items()}


def setup(strategy, row):
    """row: feature values at the decision bar (dict-like). Returns the trade plan or None."""
    stop_atr, t1r, t2r, hold = PARAMS[strategy]
    c, atr = float(row["close"]), float(row["atr"] or 0)
    if not atr or atr <= 0 or not c:
        return None
    stop = c - stop_atr * atr
    if strategy == "BREAKOUT" and row.get("don_hi"):
        stop = max(stop, float(row["don_hi"]) - 1.0 * atr)          # a failed breakout falls back into the range
    if strategy == "PULLBACK" and row.get("low"):
        stop = min(stop, float(row["low"]) - 0.25 * atr)
    risk = c - stop
    if risk <= 0:
        return None
    t1, t2 = c + t1r * risk, c + t2r * risk
    if strategy == "MEAN_REVERSION" and row.get("sma20"):
        t1 = max(t1, float(row["sma20"]))
    return {"strategy": strategy, "entry_ref": round(c, 2), "stop": round(stop, 2), "t1": round(t1, 2), "t2": round(t2, 2),
            "risk_per_share": round(risk, 2), "rr_t1": round((t1 - c) / risk, 2), "rr_t2": round((t2 - c) / risk, 2), "max_hold": hold,
            "invalidation": f"close below {round(stop, 2)}", "trail": f"stop to entry after +{MGMT['be_after_R']}R; trail {MGMT['trail_atr']} ATR after +{MGMT['trail_after_R']}R"}


def manage_step(pos, bar, atr, slip=None):
    """One bar of position management. pos: {entry, stop, risk, t2, bars, max_hold, exit_next_open}. bar: o,h,l,c.
    Returns (exit_price or None, reason). Mutates pos (stop moves, bars). Order of checks = paper simulator:
    pending time-exit at open -> stop -> target -> close-based stop updates."""
    slip = MGMT["slippage"] if slip is None else slip
    o, h, l, c = bar
    if pos.get("exit_next_open"):
        return o * (1 - slip), pos["exit_next_open"]
    pos["bars"] += 1
    if l <= pos["stop"]:
        return min(o, pos["stop"]) * (1 - slip), "stop"
    if h >= pos["t2"]:
        return (max(o, pos["t2"]) if o > pos["t2"] else pos["t2"]) * (1 - slip), "target"
    # close-based updates (take effect from the next bar)
    e, r = pos["entry"], pos["risk"]
    if c >= e * (1 + MGMT["ledger_trail_after"]):
        pos["stop"] = max(pos["stop"], c * (1 - MGMT["ledger_trail"]))
    if c >= e + MGMT["be_after_R"] * r:
        pos["stop"] = max(pos["stop"], e)
    if c >= e + MGMT["trail_after_R"] * r and atr and not math.isnan(atr):
        pos["stop"] = max(pos["stop"], c - MGMT["trail_atr"] * atr)
    if pos["bars"] >= pos["max_hold"]:
        pos["exit_next_open"] = "time"
    return None, None


def simulate(f, i, plan, slip=None, panic=None):
    """Simulate one trade decided at row i of feature frame f (fill at open of i+1). panic: optional boolean array by row
    (market PANIC regime -> exit next open). Returns dict or None if no next bar."""
    slip = MGMT["slippage"] if slip is None else slip
    n = len(f)
    if i + 1 >= n:
        return None
    O, H, L, C, A = (f[k].values for k in ("open", "high", "low", "close", "atr"))
    entry = O[i + 1] * (1 + slip)
    risk = entry - plan["stop"]
    if risk <= 0:                      # gapped below the stop: do not enter
        return {"skipped": "gap below stop"}
    pos = {"entry": entry, "stop": plan["stop"], "risk": risk, "t2": max(plan["t2"], entry + 0.5 * risk), "bars": 0,
           "max_hold": plan["max_hold"], "exit_next_open": None}
    mae, mfe = 0.0, 0.0
    j = i + 1
    while j < n:
        bar = (O[j], H[j], L[j], C[j])
        px, why = manage_step(pos, bar, A[j], slip)      # the fill bar counts too: the paper simulator checks stop/target on it
        mae = min(mae, (L[j] - entry) / risk)
        mfe = max(mfe, (H[j] - entry) / risk)
        if px is not None:
            return {"entry": entry, "exit": px, "ret": px / entry - 1, "R": (px - entry) / risk, "bars": pos["bars"], "why": why,
                    "exit_row": j, "mae_R": round(mae, 2), "mfe_R": round(mfe, 2)}
        if panic is not None and j < len(panic) and panic[j]:
            pos["exit_next_open"] = "regime panic"
        j += 1
    return {"open": True, "entry": entry, "ret": C[n - 1] / entry - 1, "R": (C[n - 1] - entry) / risk, "bars": pos["bars"]}

