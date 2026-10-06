"""
Feature engine. Input: daily OHLCV DataFrame (Open, High, Low, Close, Volume), optional index close.
Every column at row t uses only rows <= t (breakout levels are shifted by one bar).
"""
import numpy as np
import pandas as pd
from .. import indicators as I

COLS = ["close", "open", "high", "low", "ema20", "ema50", "sma20", "sma200", "slope20", "slope200", "atr", "atr_pct", "rv20", "rv_rank",
        "roc10", "roc21", "roc63", "rsi", "adx", "don_hi", "don_lo", "dist20_atr", "z20", "vol_ratio", "turnover_cr", "updown_vol",
        "gap_pct", "hh_hl", "rs63", "bbw_rank", "hi52_dist", "wk_up", "mo_up", "close_pos"]


def compute(df, index_close=None):
    d = pd.DataFrame(index=df.index)
    c, h, l, o, v = df["Close"].astype(float), df["High"].astype(float), df["Low"].astype(float), df["Open"].astype(float), df["Volume"].astype(float)
    d["close"], d["open"], d["high"], d["low"] = c, o, h, l
    d["ema20"], d["ema50"] = I.ema(c, 20), I.ema(c, 50)
    d["sma20"], d["sma200"] = I.sma(c, 20), I.sma(c, 200)
    d["slope20"] = d["ema20"] / d["ema20"].shift(5) - 1
    d["slope200"] = d["sma200"] / d["sma200"].shift(20) - 1
    d["atr"] = I.atr(df, 14)
    d["atr_pct"] = d["atr"] / c
    d["rv20"] = I.realized_vol(c, 20)
    d["rv_rank"] = d["rv20"].rolling(252, min_periods=120).rank(pct=True)
    d["roc10"], d["roc21"], d["roc63"] = c.pct_change(10), c.pct_change(21), c.pct_change(63)
    d["rsi"] = I.rsi(c, 14)
    d["adx"] = I.adx(df, 14)["adx"]
    dc = I.donchian(df, 20)
    d["don_hi"], d["don_lo"] = dc["upper"], dc["lower"]
    d["dist20_atr"] = (c - d["ema20"]) / d["atr"]
    sd20 = c.rolling(20).std()
    d["z20"] = (c - d["sma20"]) / sd20
    av20 = v.rolling(20).mean()
    d["vol_ratio"] = v / av20
    d["turnover_cr"] = (c * v).rolling(20).mean() / 1e7
    up = (c > c.shift(1)).astype(float)
    d["updown_vol"] = (v * up).rolling(20).sum() / (v * (1 - up)).rolling(20).sum().replace(0, np.nan)
    d["gap_pct"] = o / c.shift(1) - 1
    hh = h.rolling(10).max() > h.shift(10).rolling(10).max()
    hl = l.rolling(10).min() > l.shift(10).rolling(10).min()
    d["hh_hl"] = (hh & hl).astype(float) - ((~hh) & (~hl)).astype(float)       # +1 higher highs & lows, -1 lower both
    if index_close is not None:
        ix = index_close.reindex(c.index).ffill()
        d["rs63"] = c.pct_change(63) - ix.pct_change(63)
    else:
        d["rs63"] = np.nan
    bbw = I.bollinger(c, 20)["width"]
    d["bbw_rank"] = bbw.rolling(252, min_periods=120).rank(pct=True)
    d["hi52_dist"] = c / h.rolling(252, min_periods=120).max() - 1
    # multi-timeframe: weekly close above its 10-week EMA (using completed daily data only), 6-month direction
    wk = c.rolling(5).mean()
    d["wk_up"] = (wk > wk.ewm(span=50, adjust=False).mean()).astype(float)
    d["mo_up"] = (c > c.shift(126)).astype(float)
    rng = (h - l).replace(0, np.nan)
    d["close_pos"] = (c - l) / rng                                                  # where the close sits inside today's range
    return d


def latest(feat):
    row = feat.iloc[-1]
    return {k: (None if pd.isna(row[k]) else float(row[k])) for k in COLS if k in row.index}
