"""
Data quality. Every dataset gets a report: missing sessions, duplicates, bad OHLC,
jumps, staleness. Symbols with hard failures are excluded from trading and listed in
the health page. Nothing is interpolated.
"""
import datetime as dt
import numpy as np
import pandas as pd
from . import calendar as cal

HARD = {"duplicate bars", "non-positive price", "high/low/close inconsistent"}


def clean(df):
    return df[~df.index.duplicated(keep="last")].sort_index()


def check(df, cal_id=None, max_gap_days=6, jump_pct=0.25):
    """Return list of issue strings and a dict of stats."""
    issues, stats = [], {}
    if len(df) == 0:
        return ["empty"], stats
    if df.index.has_duplicates:
        issues.append("duplicate bars")
    px = df[["Open", "High", "Low", "Close"]]
    if (px <= 0).any().any():
        issues.append("non-positive price")
    if (df["High"] < df["Low"]).any() or (df["Close"] > df["High"] * 1.0001).any() or (df["Close"] < df["Low"] * 0.9999).any() \
            or (df["Open"] > df["High"] * 1.0001).any() or (df["Open"] < df["Low"] * 0.9999).any():
        issues.append("high/low/close inconsistent")
    gaps = df.index.to_series().diff().dt.days.fillna(1)
    if (gaps > max_gap_days).any():
        issues.append(f"missing bars (largest gap {int(gaps.max())} days)")
    ret = df["Close"].pct_change().abs()
    if (ret > jump_pct).any():
        issues.append(f"price jump > {int(jump_pct*100)}% on {ret.idxmax().date()} (corporate action or bad tick?)")
    if len(df) >= 20 and (df["Volume"].tail(20) == 0).sum() > 5:
        issues.append("volume unreliable (many zero days)")
    stats = {"bars": int(len(df)), "first": str(df.index[0].date()), "last": str(df.index[-1].date()),
             "zero_volume_bars": int((df["Volume"] == 0).sum())}
    if cal_id and cal_id in cal.CALENDARS:
        years = sorted({int(k[:4]) for k in cal.CALENDARS[cal_id]["holidays"]})
        first = max(df.index[0].date(), dt.date(years[0], 1, 1)) if years else df.index[-1].date()
        stats["session_check_from"] = first.isoformat()
        expected = cal.expected_sessions(cal_id, first, df.index[-1].date())
        have = set(d.date() for d in df.index)
        missing = [d.isoformat() for d in expected if d not in have]
        stats["missing_sessions"] = missing[-50:]
        stats["missing_count"] = len(missing)
        if missing:
            issues.append(f"{len(missing)} expected sessions absent (holiday list unverified or feed gap)")
    return issues, stats


def hard_fail(issues):
    return any(i in HARD for i in issues)


def age_days(df, now=None):
    now = now or dt.datetime.now(dt.timezone.utc)
    last = df.index[-1]
    return (pd.Timestamp(now).tz_convert("UTC").normalize() - last.tz_convert("UTC").normalize()).days


def status_label(meta_, age):
    """LIVE / DELAYED / HISTORICAL / STALE / MANUAL / SIMULATED."""
    if meta_.get("provider") == "manual":
        return "MANUAL"
    if meta_.get("provider") == "simulated":
        return "SIMULATED"
    if age is None:
        return "HISTORICAL"
    if age > 4:
        return "STALE"
    if age >= 1:
        return "HISTORICAL"
    d = meta_.get("delay_minutes")
    return "LIVE" if d == 0 else "DELAYED"
