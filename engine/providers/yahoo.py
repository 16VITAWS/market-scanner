"""
Yahoo Finance via yfinance. Free, UNOFFICIAL, no redistribution licence, no SLA.
NSE quotes are ~15 min delayed; US quotes delayed by exchange rules. Intraday history is
limited (1m: last 7 days, 5m/15m: last 60 days). Suitable for research and paper trading only.
"""
import pandas as pd
from .base import Provider, meta, standardise, ProviderError

DELAY = {"NSE": 15, "US": 15, "FX": 0, "default": 15}


class Yahoo(Provider):
    id = "yahoo"
    name = "Yahoo Finance (yfinance, unofficial)"
    licence = "Personal, non-commercial; unofficial API; no redistribution"
    env_keys = []

    def capabilities(self):
        c = super().capabilities()
        c.update(candles=True, quotes=True, intraday=True, options=False, news=False,
                 markets=["NSE", "BSE", "US", "UK", "EU", "JP", "HK", "CN", "SG", "AU", "CA", "KR", "FX", "commodities"],
                 delay_minutes=15, note="Free, unofficial. Blocks some IPs; retry with backoff.")
        return c

    def candles(self, ticker, interval="1d", period="2y"):
        import yfinance as yf
        df = yf.download(ticker, period=period, interval=interval, auto_adjust=True, progress=False, threads=False)
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        df = standardise(df.dropna(subset=["Close"]))
        if len(df) == 0:
            raise ProviderError(f"no data for {ticker}")
        return df, meta(self.id, ticker, interval, 15, self.licence, note="auto_adjust=True (splits/dividends adjusted by Yahoo)")

    def candles_many(self, tickers, interval="1d", period="2y"):
        """Batch download. Returns dict ticker -> DataFrame, plus list of failures."""
        import yfinance as yf
        out, failed = {}, []
        for i in range(0, len(tickers), 100):
            chunk = tickers[i:i + 100]
            raw = yf.download(chunk, period=period, interval=interval, auto_adjust=True, group_by="ticker",
                              threads=True, progress=False)
            for t in chunk:
                try:
                    df = raw[t] if isinstance(raw.columns, pd.MultiIndex) else raw
                    df = standardise(df.dropna(subset=["Close"]))
                    if len(df):
                        out[t] = df
                    else:
                        failed.append(t)
                except KeyError:
                    failed.append(t)
        return out, failed

    def quote(self, ticker):
        import yfinance as yf
        fi = yf.Ticker(ticker).fast_info
        return {"price": float(fi.last_price), "prev_close": float(fi.previous_close),
                "currency": getattr(fi, "currency", None), "meta": meta(self.id, ticker, "quote", 15, self.licence)}
