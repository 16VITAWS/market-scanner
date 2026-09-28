"""
Indicator library. Pure pandas; every function returns a Series/DataFrame aligned to the input.
No indicator uses future bars: rolling windows end at the current bar; breakout levels are
shifted by one so today's bar never sets its own trigger.
"""
import math
import numpy as np
import pandas as pd


def sma(s, n): return s.rolling(n).mean()
def ema(s, n): return s.ewm(span=n, adjust=False).mean()


def rsi(close, n=14):
    delta = close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    return 100 - 100 / (1 + gain / loss.replace(0, np.nan))


def true_range(df):
    c = df["Close"].shift()
    return pd.concat([df["High"] - df["Low"], (df["High"] - c).abs(), (df["Low"] - c).abs()], axis=1).max(axis=1)


def atr(df, n=14): return true_range(df).ewm(alpha=1 / n, adjust=False).mean()


def macd(close, fast=12, slow=26, sig=9):
    m = ema(close, fast) - ema(close, slow)
    s = ema(m, sig)
    return pd.DataFrame({"macd": m, "signal": s, "hist": m - s})


def bollinger(close, n=20, k=2.0):
    m, sd = sma(close, n), close.rolling(n).std()
    return pd.DataFrame({"mid": m, "upper": m + k * sd, "lower": m - k * sd, "width": (2 * k * sd) / m})


def adx(df, n=14):
    up, dn = df["High"].diff(), -df["Low"].diff()
    plus = pd.Series(np.where((up > dn) & (up > 0), up, 0.0), index=df.index)
    minus = pd.Series(np.where((dn > up) & (dn > 0), dn, 0.0), index=df.index)
    tr = true_range(df).ewm(alpha=1 / n, adjust=False).mean()
    pdi = 100 * plus.ewm(alpha=1 / n, adjust=False).mean() / tr
    mdi = 100 * minus.ewm(alpha=1 / n, adjust=False).mean() / tr
    dx = 100 * (pdi - mdi).abs() / (pdi + mdi).replace(0, np.nan)
    return pd.DataFrame({"adx": dx.ewm(alpha=1 / n, adjust=False).mean(), "pdi": pdi, "mdi": mdi})


def donchian(df, n=20):
    """Previous-n-bar channel, shifted so the current bar cannot be its own breakout level."""
    return pd.DataFrame({"upper": df["High"].rolling(n).max().shift(1), "lower": df["Low"].rolling(n).min().shift(1)})


def vwap(df):
    """Session VWAP for intraday frames (resets each calendar day, in the frame's timezone)."""
    tp = (df["High"] + df["Low"] + df["Close"]) / 3
    day = df.index.date
    pv = (tp * df["Volume"]).groupby(day).cumsum()
    v = df["Volume"].groupby(day).cumsum()
    return pv / v.replace(0, np.nan)


def realized_vol(close, n=20, periods=252):
    return close.pct_change().rolling(n).std() * math.sqrt(periods)


def drawdown(equity):
    peak = equity.cummax()
    return equity / peak - 1


def rolling_beta(asset, bench, n=60):
    ra, rb = asset.pct_change(), bench.pct_change()
    return ra.rolling(n).cov(rb) / rb.rolling(n).var()


def rolling_corr(a, b, n=60):
    return a.pct_change().rolling(n).corr(b.pct_change())


def relative_strength(close, bench, n=63):
    return close / close.shift(n) - bench / bench.shift(n)


def enrich(df):
    """Standard indicator set used by the scanner. Adds columns; never drops bars."""
    d = df.copy()
    c = d["Close"]
    for n in (21, 50, 200):
        d[f"sma{n}"] = sma(c, n)
    d["ema21"] = ema(c, 21)
    d["rsi"] = rsi(c)
    d["atr"] = atr(d)
    m = macd(c); d["macd"], d["macd_sig"], d["macd_hist"] = m["macd"], m["signal"], m["hist"]
    a = adx(d); d["adx"] = a["adx"]
    dc = donchian(d); d["hi20"], d["lo20"] = dc["upper"], dc["lower"]
    d["vol20"] = d["Volume"].rolling(20).mean().shift(1)
    d["turnover_cr"] = (c * d["Volume"]).rolling(20).mean() / 1e7
    d["rvol20"] = realized_vol(c)
    return d


def support_resistance(df, n=120, tol=0.0075):
    """Swing highs/lows over the last n bars, clustered. Returns (supports, resistances) as sorted lists."""
    x = df.tail(n)
    h, l, c = x["High"].values, x["Low"].values, float(x["Close"].iloc[-1])
    piv_h = [h[i] for i in range(2, len(h) - 2) if h[i] == max(h[i - 2:i + 3])]
    piv_l = [l[i] for i in range(2, len(l) - 2) if l[i] == min(l[i - 2:i + 3])]

    def cluster(vals):
        out = []
        for v in sorted(vals):
            if out and abs(v - out[-1][-1]) / v < tol:
                out[-1].append(v)
            else:
                out.append([v])
        return [round(float(np.mean(g)), 2) for g in out if len(g) >= 1]
    res = [v for v in cluster(piv_h) if v > c][:3]
    sup = [v for v in cluster(piv_l) if v < c][-3:]
    return sup, res
