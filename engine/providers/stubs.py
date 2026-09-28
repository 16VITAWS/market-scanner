"""
Adapters that are DESIGNED but AWAITING CREDENTIALS. They never fabricate data:
without a key they raise ProviderError and report status "awaiting key".

Angel One SmartAPI  - free with an Angel One account; official NSE/BSE data, option chains, order API.
Twelve Data         - free tier 800 credits/day, 8/min; strong for US/global/FX; NSE limited on free plan.
NSE website         - public JSON (option chain, FII/DII) protected by bot checks; best-effort only.
"""
import os, json, datetime as dt
import requests
import pandas as pd
from .base import Provider, meta, standardise, ProviderError

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124 Safari/537.36",
      "Accept": "application/json, text/plain, */*", "Accept-Language": "en-IN,en;q=0.9"}


class AngelOne(Provider):
    id = "angelone"
    name = "Angel One SmartAPI"
    licence = "Official broker API; personal use under Angel One terms"
    env_keys = ["ANGEL_API_KEY", "ANGEL_CLIENT_ID", "ANGEL_PASSWORD", "ANGEL_TOTP_SECRET"]

    def capabilities(self):
        c = super().capabilities()
        c.update(candles=True, quotes=True, intraday=True, options=True, news=False, markets=["NSE", "BSE", "NFO", "MCX", "CDS"],
                 delay_minutes=0, order_api=True, paper_safe=True,
                 note="Free; needs an Angel One account. Login = API key + client id + password + TOTP. Not wired until keys exist.")
        return c

    def candles(self, ticker, interval="1d", period="2y"):
        if not self.configured():
            raise ProviderError("Angel One: awaiting API key (set ANGEL_* secrets)")
        # Wiring point: pip install smartapi-python, login with TOTP, call getCandleData(exchange, token, interval, from, to).
        raise ProviderError("Angel One adapter: credentials present but adapter not yet tested against the live API")

    def quote(self, ticker):
        raise ProviderError("Angel One: awaiting API key")


class TwelveData(Provider):
    id = "twelvedata"
    name = "Twelve Data"
    licence = "Free tier: personal use; 800 credits/day, 8 requests/min"
    env_keys = ["TWELVEDATA_API_KEY"]
    BASE = "https://api.twelvedata.com"

    def capabilities(self):
        c = super().capabilities()
        c.update(candles=True, quotes=True, intraday=True, options=False, news=False,
                 markets=["US", "UK", "EU", "JP", "FX", "crypto", "NSE (limited on free plan)"], delay_minutes=0,
                 note="Free key at twelvedata.com. Indices need ETF proxies (SPY, QQQ, DIA).")
        return c

    def candles(self, ticker, interval="1d", period="2y"):
        if not self.configured():
            raise ProviderError("Twelve Data: awaiting API key (set TWELVEDATA_API_KEY)")
        iv = {"1m": "1min", "5m": "5min", "15m": "15min", "30m": "30min", "1h": "1h", "4h": "4h", "1d": "1day", "1wk": "1week", "1mo": "1month"}[interval]
        r = requests.get(f"{self.BASE}/time_series", params={"symbol": ticker, "interval": iv, "outputsize": 5000,
                         "apikey": os.environ["TWELVEDATA_API_KEY"], "timezone": "UTC"}, timeout=30)
        j = r.json()
        if j.get("status") == "error":
            raise ProviderError(f"Twelve Data: {j.get('message')}")
        df = pd.DataFrame(j["values"]).rename(columns={"datetime": "time"}).set_index("time").astype(float)
        return standardise(df), meta(self.id, ticker, interval, 0, self.licence)

    def quote(self, ticker):
        if not self.configured():
            raise ProviderError("Twelve Data: awaiting API key")
        r = requests.get(f"{self.BASE}/quote", params={"symbol": ticker, "apikey": os.environ["TWELVEDATA_API_KEY"]}, timeout=30).json()
        if r.get("status") == "error":
            raise ProviderError(r.get("message"))
        return {"price": float(r["close"]), "prev_close": float(r["previous_close"]), "meta": meta(self.id, ticker, "quote", 0, self.licence)}


class NSEWeb(Provider):
    """Best-effort reads of public NSE JSON. Often blocked from cloud IPs; every failure is reported, never hidden."""
    id = "nseweb"
    name = "NSE India website (public JSON, best effort)"
    licence = "NSE terms of use; personal viewing only; no redistribution"
    env_keys = []

    def capabilities(self):
        c = super().capabilities()
        c.update(candles=False, quotes=False, intraday=False, options=True, news=False, markets=["NSE"], flows=True,
                 delay_minutes=15, note="Anti-bot protection; success not guaranteed. Falls back to manual CSV upload in the portal.")
        return c

    def _session(self):
        s = requests.Session()
        s.headers.update(UA)
        s.get("https://www.nseindia.com", timeout=15)
        s.get("https://www.nseindia.com/option-chain", timeout=15)
        return s

    def option_chain(self, underlying="NIFTY"):
        try:
            s = self._session()
            r = s.get(f"https://www.nseindia.com/api/option-chain-indices?symbol={underlying}", timeout=20)
            r.raise_for_status()
            j = r.json()
        except Exception as e:
            raise ProviderError(f"NSE option chain unavailable: {e}")
        rec = j["records"]
        rows = []
        for it in rec["data"]:
            ce, pe = it.get("CE") or {}, it.get("PE") or {}
            rows.append({"expiry": it["expiryDate"], "strike": it["strikePrice"],
                         "c": {k: ce.get(v) for k, v in [("oi", "openInterest"), ("chg", "changeinOpenInterest"), ("vol", "totalTradedVolume"),
                               ("iv", "impliedVolatility"), ("ltp", "lastPrice"), ("ch", "change"), ("bid", "bidprice"), ("ask", "askPrice"), ("bq", "bidQty"), ("aq", "askQty")]},
                         "p": {k: pe.get(v) for k, v in [("oi", "openInterest"), ("chg", "changeinOpenInterest"), ("vol", "totalTradedVolume"),
                               ("iv", "impliedVolatility"), ("ltp", "lastPrice"), ("ch", "change"), ("bid", "bidprice"), ("ask", "askPrice"), ("bq", "bidQty"), ("aq", "askQty")]}})
        return {"underlying": underlying, "spot": rec.get("underlyingValue"), "asof": rec.get("timestamp"),
                "expiries": rec.get("expiryDates", []), "rows": rows, "complete": True,
                "meta": meta(self.id, underlying, "chain", 15, self.licence)}

    def fii_dii(self):
        try:
            s = self._session()
            r = s.get("https://www.nseindia.com/api/fiidiiTradeReact", timeout=20)
            r.raise_for_status()
            j = r.json()
        except Exception as e:
            raise ProviderError(f"NSE FII/DII unavailable: {e}")
        out = []
        for row in j:
            out.append({"category": row.get("category"), "date": row.get("date"), "buy": float(row.get("buyValue", 0)),
                        "sell": float(row.get("sellValue", 0)), "net": float(row.get("netValue", 0)), "status": "provisional"})
        return {"rows": out, "meta": meta(self.id, "FIIDII", "daily", 0, self.licence)}


class ManualCSV(Provider):
    """Data typed or uploaded by the user. Always labelled MANUAL."""
    id = "manual"
    name = "Manual entry / uploaded file"
    licence = "User supplied"
    env_keys = []

    def capabilities(self):
        c = super().capabilities()
        c.update(candles=True, quotes=True, options=True, markets=["any"], delay_minutes=None, note="Whatever the user uploads; labelled MANUAL.")
        return c

    def candles_from_csv(self, path, tz="Asia/Kolkata"):
        df = pd.read_csv(path)
        df.columns = [c.strip().capitalize() for c in df.columns]
        df["Date"] = pd.to_datetime(df["Date"])
        df = df.set_index("Date")   # daily bars: the date is a label; standardise() stores it as UTC midnight
        return standardise(df), meta(self.id, os.path.basename(path), "1d", None, self.licence, note="MANUAL file")
