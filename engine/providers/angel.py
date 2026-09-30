"""
Angel One SmartAPI adapter - real-time NSE/BSE quotes from the exchange feed (the only realtime path this portal has).

Enabled ONLY when all four GitHub Secrets exist:
    ANGEL_API_KEY      SmartAPI app key            (smartapi.angelone.in -> My Apps)
    ANGEL_CLIENT_ID    your Angel One client code
    ANGEL_PASSWORD     your 4-digit login PIN (MPIN)
    ANGEL_TOTP_SECRET  the TOTP secret shown when you enable TOTP (smartapi.angelone.in/enable-totp)
Secrets live in GitHub Secrets / the VPS env only - never in the repo or the browser; errors are scrubbed before logging.

Status: written from the published SmartAPI REST documentation; UNVERIFIED until the first successful call, which
the Data Health page shows (provider 'angelone': last_ok / last_error). Without keys it does nothing and says so.
Quotes: POST market/v1/quote (mode FULL, up to 50 tokens per request). Throttled to <= 1 request / second.
"""
import os, time, hmac, base64, struct, hashlib, datetime as dt
import requests
from .base import Provider, ProviderError

BASE = "https://apiconnect.angelone.in"
SCRIP_MASTER = "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json"
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
# Index tokens as published in Angel's scrip master (instrumenttype AMXIDX); looked up from the master first, these are fallbacks.
INDEX_FALLBACK = {"NIFTY": "99926000", "BANKNIFTY": "99926009", "FINNIFTY": "99926037", "INDIAVIX": "99926017"}
INDEX_NAMES = {"NIFTY": "Nifty 50", "BANKNIFTY": "Nifty Bank", "FINNIFTY": "Nifty Fin Service", "INDIAVIX": "India VIX"}


def totp(secret_b32, t=None, step=30, digits=6):
    """RFC 6238 TOTP (SHA-1), same algorithm as authenticator apps."""
    key = base64.b32decode(secret_b32.replace(" ", "").upper() + "=" * (-len(secret_b32.replace(" ", "")) % 8))
    counter = int((t if t is not None else time.time()) // step)
    h = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    o = h[-1] & 0x0F
    code = (struct.unpack(">I", h[o:o + 4])[0] & 0x7FFFFFFF) % (10 ** digits)
    return str(code).zfill(digits)


def parse_exch_time(s):
    """Angel returns e.g. '30-Sep-2026 15:29:59' (exchange time, IST). Returns aware UTC datetime or None."""
    for fmt in ("%d-%b-%Y %H:%M:%S", "%d-%m-%Y %H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            return dt.datetime.strptime(str(s).strip(), fmt).replace(tzinfo=IST).astimezone(dt.timezone.utc)
        except (ValueError, TypeError):
            continue
    return None


def to_quote(row):
    """Normalise one 'fetched' record of the FULL quote response into the portal quote shape (no provenance fields)."""
    ltp = row.get("ltp")
    t = parse_exch_time(row.get("exchFeedTime")) or parse_exch_time(row.get("exchTradeTime"))
    if ltp in (None, "") or t is None:
        return None
    f = lambda k: (float(row[k]) if row.get(k) not in (None, "") else None)
    pc = f("close")
    q = {"p": round(float(ltp), 2), "pc": round(pc, 2) if pc else None,
         "chg_pct": round((float(ltp) / pc - 1) * 100, 2) if pc else None,
         "t": t.isoformat(), "d": t.astimezone(IST).date().isoformat(), "intraday": True, "age_days": 0,
         "day_o": f("open"), "day_h": f("high"), "day_l": f("low"), "day_v": f("tradeVolume") or 0.0}
    if row.get("avgPrice") not in (None, ""):
        q["vwap"] = f("avgPrice")
    depth = row.get("depth") or {}
    b, s = (depth.get("buy") or [{}])[0], (depth.get("sell") or [{}])[0]
    if b.get("price"):
        q.update(bid=float(b["price"]), bid_qty=b.get("quantity"))
    if s.get("price"):
        q.update(ask=float(s["price"]), ask_qty=s.get("quantity"))
    return q


class AngelOne(Provider):
    id = "angelone"
    name = "Angel One SmartAPI"
    licence = "Official broker API; personal use under Angel One terms (no redistribution)"
    env_keys = ["ANGEL_API_KEY", "ANGEL_CLIENT_ID", "ANGEL_PASSWORD", "ANGEL_TOTP_SECRET"]

    def __init__(self, session=None):
        self.s = session or requests.Session()
        self.jwt = None
        self.tokens = None
        self._last_call = 0.0

    def capabilities(self):
        c = super().capabilities()
        c.update(candles=False, quotes=True, intraday=True, options=False, news=False, markets=["NSE", "BSE"],
                 delay_minutes=0, order_api=False, streaming=False,
                 note="Realtime NSE quotes (LTP, OHLC, volume, VWAP, best bid/ask) polled every live cycle once the 4 ANGEL_* "
                      "secrets are set. Unverified until the first successful call shows on the Data Health page.")
        return c

    def _headers(self):
        h = {"Content-Type": "application/json", "Accept": "application/json", "X-UserType": "USER", "X-SourceID": "WEB",
             "X-ClientLocalIP": "127.0.0.1", "X-ClientPublicIP": "127.0.0.1", "X-MACAddress": "00:00:00:00:00:00",
             "X-PrivateKey": os.environ.get("ANGEL_API_KEY", "")}
        if self.jwt:
            h["Authorization"] = "Bearer " + self.jwt
        return h

    def _throttle(self):
        wait = 1.05 - (time.time() - self._last_call)
        if wait > 0:
            time.sleep(wait)
        self._last_call = time.time()

    def login(self):
        if not self.configured():
            raise ProviderError("Angel One: awaiting secrets " + ", ".join(k for k in self.env_keys if not os.environ.get(k)))
        self._throttle()
        r = self.s.post(BASE + "/rest/auth/angelbroking/user/v1/loginByPassword", headers=self._headers(), timeout=15,
                        json={"clientcode": os.environ["ANGEL_CLIENT_ID"], "password": os.environ["ANGEL_PASSWORD"],
                              "totp": totp(os.environ["ANGEL_TOTP_SECRET"])})
        j = r.json() if r.content else {}
        if r.status_code != 200 or not j.get("status") or not (j.get("data") or {}).get("jwtToken"):
            raise ProviderError(f"Angel One login failed: HTTP {r.status_code} {j.get('errorcode', '')} {j.get('message', '')}".strip())
        self.jwt = j["data"]["jwtToken"]
        return True

    def load_tokens(self, wanted):
        """wanted: {portal_id: angel_symbol} -> {portal_id: token} using Angel's public scrip master."""
        r = self.s.get(SCRIP_MASTER, timeout=60)
        r.raise_for_status()
        rows = r.json()
        eq = {x["symbol"]: x["token"] for x in rows if x.get("exch_seg") == "NSE" and str(x.get("symbol", "")).endswith("-EQ")}
        idx = {x.get("name"): x["token"] for x in rows if x.get("exch_seg") == "NSE" and x.get("instrumenttype") == "AMXIDX"}
        idx_by_symbol = {x.get("symbol"): x["token"] for x in rows if x.get("exch_seg") == "NSE" and x.get("instrumenttype") == "AMXIDX"}
        out = {}
        for pid, sym in wanted.items():
            if not sym:
                continue
            if sym in eq:
                out[pid] = eq[sym]
            elif pid in INDEX_FALLBACK:
                out[pid] = idx.get(sym) or idx_by_symbol.get(INDEX_NAMES[pid]) or INDEX_FALLBACK[pid]
        self.tokens = out
        return out

    def quotes(self, ids):
        """ids: portal ids that have tokens -> ({portal_id: quote}, errors[list])."""
        if not self.jwt:
            self.login()
        by_tok = {self.tokens[i]: i for i in ids if i in (self.tokens or {})}
        toks = list(by_tok)
        out, errs = {}, []
        for k in range(0, len(toks), 50):
            self._throttle()
            r = self.s.post(BASE + "/rest/secure/angelbroking/market/v1/quote/", headers=self._headers(), timeout=15,
                            json={"mode": "FULL", "exchangeTokens": {"NSE": toks[k:k + 50]}})
            j = r.json() if r.content else {}
            if r.status_code == 429 or j.get("errorcode") == "AB1019":
                errs.append("rate limited"); break
            if r.status_code != 200 or not j.get("status"):
                if r.status_code in (401, 403) or j.get("errorcode") in ("AG8001", "AG8002"):
                    self.jwt = None                                   # token expired: log in again next cycle
                errs.append(f"HTTP {r.status_code} {j.get('errorcode', '')} {j.get('message', '')}".strip()); continue
            for row in (j.get("data") or {}).get("fetched") or []:
                pid = by_tok.get(str(row.get("symbolToken")))
                q = to_quote(row) if pid else None
                if q:
                    out[pid] = q
            for row in (j.get("data") or {}).get("unfetched") or []:
                errs.append(f"unfetched {row.get('symbolToken')}: {row.get('message', '')}")
        return out, errs

    def candles(self, ticker, interval="1d", period="2y"):
        raise ProviderError("Angel One candles are not wired (Yahoo supplies history); quotes only")

    def quote(self, ticker):
        raise ProviderError("use quotes([ids]) after load_tokens()")
