"""Market-data integrity checks. A symbol that fails is reported, never traded."""
import pandas as pd

def check(df, max_gap_days=6):
    issues = []
    if df.index.has_duplicates:
        issues.append("duplicate bars")
    if (df[["Open", "High", "Low", "Close"]] <= 0).any().any():
        issues.append("non-positive price")
    if (df["High"] < df["Low"]).any() or (df["Close"] > df["High"]).any() or (df["Close"] < df["Low"]).any():
        issues.append("high/low/close inconsistent")
    gaps = df.index.to_series().diff().dt.days.fillna(1)
    if (gaps > max_gap_days).any():
        issues.append(f"missing bars (gap of {int(gaps.max())} days)")
    if len(df) >= 20 and (df["Volume"].tail(20) == 0).sum() > 5:
        issues.append("volume unreliable (many zero days)")
    return issues

def clean(df):
    df = df[~df.index.duplicated(keep="last")].sort_index()
    return df

def age_days(df, now):
    return (pd.Timestamp(now).normalize() - df.index[-1].normalize()).days
