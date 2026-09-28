"""
Provider abstraction. Every provider returns the same shapes:

    candles(ticker, interval, period)  -> DataFrame[Open, High, Low, Close, Volume], tz-aware UTC index
    quote(ticker)                      -> dict(price, prev_close, time_utc, ...)
    capabilities()                     -> dict describing what it can do and its status

`meta` on every result records provider, ticker, delay, licence and fetch time so the UI
can label LIVE / DELAYED / HISTORICAL / STALE / MANUAL / SIMULATED honestly.
"""
import os, datetime as dt
import pandas as pd

INTERVALS = ["1m", "3m", "5m", "15m", "30m", "1h", "4h", "1d", "1wk", "1mo"]


def utcnow():
    return dt.datetime.now(dt.timezone.utc)


def meta(provider, ticker, interval, delay_min, licence, status="OK", note=""):
    return {"provider": provider, "ticker": ticker, "interval": interval, "delay_minutes": delay_min,
            "licence": licence, "fetched_at_utc": utcnow().isoformat(timespec="seconds"), "status": status, "note": note}


class Provider:
    id = "base"
    name = "Base"
    licence = ""
    env_keys = []

    def configured(self):
        return all(os.environ.get(k) for k in self.env_keys)

    def capabilities(self):
        return {"id": self.id, "name": self.name, "configured": self.configured(),
                "needs_env": self.env_keys, "licence": self.licence,
                "candles": False, "quotes": False, "intraday": False, "options": False, "news": False,
                "markets": [], "status": "not configured" if self.env_keys and not self.configured() else "available"}

    def candles(self, ticker, interval="1d", period="2y"):
        raise NotImplementedError

    def quote(self, ticker):
        raise NotImplementedError


class ProviderError(Exception):
    pass


def standardise(df):
    """Force column names and UTC index."""
    if df is None or len(df) == 0:
        return pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume"])
    df = df.rename(columns={c: c.capitalize() for c in df.columns})
    keep = [c for c in ["Open", "High", "Low", "Close", "Volume"] if c in df.columns]
    df = df[keep].copy()
    if "Volume" not in df.columns:
        df["Volume"] = 0
    idx = pd.to_datetime(df.index)
    if idx.tz is None:
        idx = idx.tz_localize("UTC")
    else:
        idx = idx.tz_convert("UTC")
    df.index = idx
    df.index.name = "time"
    return df[~df.index.duplicated(keep="last")].sort_index()
