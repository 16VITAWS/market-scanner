#!/usr/bin/env python3
"""
VISION LIVE - realtime NSE prices (every tick, from the exchange via your broker's free WebSocket: Shoonya/Finvasia or
Angel One SmartAPI) and the VISION AI algorithm acting on them second by second. Runs on YOUR laptop/PC while you trade. Open http://127.0.0.1:8765 to watch.

What it does, every tick:
  * updates the price, day OHLC, volume, VWAP of every watched symbol (exchange timestamp shown, age in seconds)
  * checks every open position's stop-loss and target against the LIVE price (not a 2-minute or 15-minute old one)
  * checks today's BUY signals from the portal: enters only when the live price is within 0.5% of the signal's entry price
  * exits positions the portal now marks SELL

Three modes (settings.env: MODE=...):
  PAPER  (default) simulated fills at the live price - no money moves. Builds the track record the REAL mode needs.
  ALERT  PAPER + a phone notification "BUY 12 TCS at 3,412" so you can place it yourself in your broker app.
  REAL   sends LIMIT orders to Angel One. Refuses to start trading unless ALL of these are true:
           - CONSENT=I ACCEPT REAL MONEY RISK
           - STATIC_IP_REGISTERED=yes   (SEBI rule from 1 Apr 2026: API orders only from your registered static IP)
           - the live-paper record passes the gate: >= 30 closed trades, profit factor >= 1.2, positive expectancy
           - kill switch off (button on the live screen, or the portal's kill switch)
         and every order passes the limits: orders/day, value/order, open positions, daily loss.
         LIMIT orders only (live price +/- 0.5%), delivery product, never market orders.

Secrets stay on this computer only (~/vision_live/settings.env), are never sent anywhere except Angel One, never logged.
The live screen listens on 127.0.0.1 only - nobody else on your network can open it.

Brokers (settings.env BROKER=...):
  SHOONYA  Finvasia Shoonya - free API. Login is OAuth: once a day you click "Login to Shoonya" on the live screen and log in on
           Shoonya's own page (password + authenticator code there). Shoonya returns a one-day access token to this program;
           your password is never stored here. Needs SHOONYA_UID, SHOONYA_CLIENT_ID, SHOONYA_SECRET (from Shoonya's API page).
           Endpoints and message formats follow the NorenRestApiOAuth SDK (by Kambala, the maker of Shoonya's Noren OMS).
  ANGEL    Angel One SmartAPI - free API; login with API key + client id + PIN + TOTP secret (fully automatic).
Status: written against the official SDKs. NOT YET TESTED against a live account. Start in PAPER mode; the screen shows exactly what is connected.
Nothing here is investment advice.
"""
import os, sys, json, time, math, threading, datetime as dt, traceback, hmac, base64, struct, hashlib
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
HOME = os.path.join(os.path.expanduser("~"), "vision_live")
SETTINGS = os.path.join(HOME, "settings.env")
PAPER_FILE = os.path.join(HOME, "live_paper.json")
REAL_FILE = os.path.join(HOME, "real_orders.json")
OPT_FILE = os.path.join(HOME, "options_paper.json")
AUDIT = os.path.join(HOME, "audit.jsonl")
KILL_FILE = os.path.join(HOME, "KILL")
CONSENT_PHRASE = "I ACCEPT REAL MONEY RISK"
GATE = {"min_closed": 30, "min_pf": 1.2}
WS_URL = "wss://smartapisocket.angelone.in/smart-stream"
SCRIP_MASTER = "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json"
INDEX_TOKENS = {"NIFTY": "99926000", "BANKNIFTY": "99926009", "FINNIFTY": "99926037", "INDIAVIX": "99926017"}
INDEX_NAMES = set(INDEX_TOKENS)
SH_HOST = "https://api.shoonya.com/NorenWClientAPI"
SH_WS = "wss://api.shoonya.com/NorenWSAPI/"
SH_OAUTH = "https://api.shoonya.com/OAuthlogin/authorize/oauth"
SH_SYMBOLS = "https://api.shoonya.com/NSE_symbols.txt.zip"
SH_NFO = "https://api.shoonya.com/NFO_symbols.txt.zip"
SH_INDEX_TOKENS = {"NIFTY": "26000", "BANKNIFTY": "26009", "FINNIFTY": "26037", "INDIAVIX": "26017"}
SH_SESSION = os.path.join(HOME, "shoonya_session.json")
BACKOFF = [1, 2, 5, 10, 30, 60]

TEMPLATE = """# VISION LIVE settings - this file stays on your computer. Never share it or upload it.
# Which broker streams your live prices and (later) takes orders: SHOONYA or ANGEL
BROKER=SHOONYA
# Shoonya / Finvasia (free API). From Shoonya's API key page: your user id, the API client id and the secret code.
# In that page set the Redirect URL to  http://127.0.0.1:8765/shoonya/callback
SHOONYA_UID=
SHOONYA_CLIENT_ID=
SHOONYA_SECRET=
# Optional: the Primary IP Address you saved on Shoonya's Api Key page (lets the screen say MATCH / MISMATCH).
# Leave empty: it is learned automatically after the first successful login.
SHOONYA_REGISTERED_IP=
SHOONYA_OAUTH_URL=https://api.shoonya.com/OAuthlogin/authorize/oauth
# Angel One SmartAPI (free) - only if BROKER=ANGEL: create an app at smartapi.angelone.in, enable TOTP
ANGEL_API_KEY=
ANGEL_CLIENT_ID=
ANGEL_PASSWORD=
ANGEL_TOTP_SECRET=
# PAPER (safe, default) | ALERT (paper + phone alerts to place yourself) | REAL (real orders - see rules in vision_live.py)
MODE=PAPER
PAPER_CAPITAL=100000
MAX_ORDERS_PER_DAY=3
MAX_ORDER_VALUE=25000
MAX_DAILY_LOSS=2000
MAX_OPEN_POSITIONS=3
# NIFTY options (debit spreads on LIVE Shoonya option prices): PAPER | ALERT | REAL
OPTIONS_MODE=PAPER
OPTIONS_LOTS=1
MAX_OPTION_RISK=6000
# REAL options also need OPTIONS_REAL=yes and 20+ closed options paper trades with profit factor >= 1.2
OPTIONS_REAL=no
# Only for MODE=REAL:
CONSENT=
STATIC_IP_REGISTERED=no
# Extra symbols to always watch (NSE, comma separated)
WATCH=RELIANCE,HDFCBANK,ICICIBANK,INFY,TCS,SBIN,ITC,LT
VISION_SITE=https://16vitaws.github.io/market-scanner
NTFY_TOPIC=vision-ai-16vitaws-k7q2m9x4
PORT=8765
"""


# ------------------------------------------------------------------ helpers
def now():
    return dt.datetime.now(IST)


def load_settings(path=SETTINGS):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if not os.path.exists(path):
        open(path, "w").write(TEMPLATE)
    cfg = {}
    for line in open(path, encoding="utf-8"):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            cfg[k.strip()] = v.strip()
    for k in list(cfg):
        if os.environ.get(k):
            cfg[k] = os.environ[k]
    return cfg


SECRET_KEYS = ("ANGEL_API_KEY", "ANGEL_PASSWORD", "ANGEL_TOTP_SECRET", "SHOONYA_SECRET", "SHOONYA_ACCESS_TOKEN")


def scrub(text, cfg):
    s = str(text)[:400]
    for k in SECRET_KEYS:
        if cfg.get(k) and len(cfg[k]) >= 4:
            s = s.replace(cfg[k], "***")
    return s


def audit(event, **kw):
    row = {"time": now().isoformat(timespec="seconds"), "event": event, **kw}
    try:
        with open(AUDIT, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, default=str) + "\n")
    except OSError:
        pass
    print(json.dumps(row, default=str), flush=True)
    return row


def totp(secret_b32, t=None, step=30, digits=6):
    s = secret_b32.replace(" ", "").upper()
    key = base64.b32decode(s + "=" * (-len(s) % 8))
    h = hmac.new(key, struct.pack(">Q", int((t if t is not None else time.time()) // step)), hashlib.sha1).digest()
    o = h[-1] & 0x0F
    return str((struct.unpack(">I", h[o:o + 4])[0] & 0x7FFFFFFF) % (10 ** digits)).zfill(digits)


def tick_round(x, up):
    """NSE equity tick size 0.05."""
    n = x / 0.05
    return round((math.ceil(n) if up else math.floor(n)) * 0.05, 2)


def market_state(cal=None):
    """Portal calendar is the authority (holidays); local clock is the fallback."""
    if cal and cal.get("state"):
        return cal["state"]
    t = now()
    if t.weekday() >= 5:
        return "CLOSED"
    if dt.time(9, 15) <= t.time() <= dt.time(15, 30):
        return "OPEN"
    return "CLOSED"


# ------------------------------------------------------------------ live prices
class Book:
    """Latest validated tick per symbol. Impossible ticks are rejected, the previous one is kept."""

    def __init__(self):
        self.q, self.lock = {}, threading.Lock()
        self.ticks = 0
        self.rejected = []

    def update(self, sym, d):
        p = d.get("p")
        with self.lock:
            old = self.q.get(sym, {})
            reason = None
            if p is None or not isinstance(p, (int, float)) or not math.isfinite(p) or p <= 0:
                reason = "bad price"
            elif old.get("t") and d.get("t") and d["t"] < old["t"]:
                reason = "out of order"
            elif (d.get("pc") or old.get("pc")) and sym not in INDEX_NAMES and not sym.startswith("OPT ") and abs(p / (d.get("pc") or old.get("pc")) - 1) > 0.205:
                reason = "move beyond NSE price band"
            elif d.get("h") and d.get("l") and not (d["l"] * 0.9999 <= p <= d["h"] * 1.0001):
                reason = "outside day high/low"
            if reason:
                self.rejected = (self.rejected + [{"sym": sym, "reason": reason, "at": now().strftime("%H:%M:%S")}])[-20:]
                return False
            self.q[sym] = {**old, **{k: v for k, v in d.items() if v is not None}, "recv": time.time()}
            self.ticks += 1
            return True

    def get(self, sym):
        with self.lock:
            return dict(self.q.get(sym) or {})

    def snapshot(self):
        with self.lock:
            return {k: dict(v) for k, v in self.q.items()}


def parse_tick(m):
    """SmartWebSocketV2 parsed message -> portal fields. Prices arrive in paise."""
    p = lambda k: (m[k] / 100.0) if m.get(k) not in (None, 0) else None
    out = {"p": p("last_traded_price"), "t": (m.get("exchange_timestamp") or 0) / 1000.0 or None,
           "o": p("open_price_of_the_day"), "h": p("high_price_of_the_day"), "l": p("low_price_of_the_day"),
           "pc": p("closed_price"), "v": m.get("volume_trade_for_the_day"), "vwap": p("average_traded_price")}
    bb, bs = m.get("best_5_buy_data") or [], m.get("best_5_sell_data") or []
    if bb:
        out["bid"] = bb[0].get("price", 0) / 100.0
    if bs:
        out["ask"] = bs[0].get("price", 0) / 100.0
    return out


class Feed(threading.Thread):
    """Angel One login + WebSocket with auto-reconnect (exponential backoff, re-login, resubscribe, snapshot)."""
    daemon = True

    def __init__(self, cfg, book, tokens):
        super().__init__()
        self.cfg, self.book, self.tokens = cfg, book, tokens          # tokens: {token: sym}
        self.state, self.last_error, self.connected_at, self.reconnects = "CONNECTING", "", None, 0
        self.api = None
        self.stop_flag = False
        self.last_msg = None

    def login(self):
        from SmartApi import SmartConnect
        api = SmartConnect(api_key=self.cfg["ANGEL_API_KEY"])
        r = api.generateSession(self.cfg["ANGEL_CLIENT_ID"], self.cfg["ANGEL_PASSWORD"], totp(self.cfg["ANGEL_TOTP_SECRET"]))
        if not r or not r.get("status") or not (r.get("data") or {}).get("jwtToken"):
            raise RuntimeError(f"login failed: {(r or {}).get('errorcode', '')} {(r or {}).get('message', '')}")
        self.api, self.jwt, self.feed_token = api, r["data"]["jwtToken"], api.getfeedToken()
        audit("login_ok", client=self.cfg["ANGEL_CLIENT_ID"][:2] + "***")

    def snapshot_rest(self):
        """After (re)connect: one REST snapshot so prices missed while offline are filled at once."""
        toks = list(self.tokens)
        for i in range(0, len(toks), 50):
            try:
                r = self.api.getMarketData("FULL", {"NSE": toks[i:i + 50]})
                for row in ((r or {}).get("data") or {}).get("fetched") or []:
                    sym = self.tokens.get(str(row.get("symbolToken")))
                    if sym and row.get("ltp"):
                        self.book.update(sym, {"p": float(row["ltp"]), "pc": row.get("close"), "o": row.get("open"), "h": row.get("high"),
                                               "l": row.get("low"), "v": row.get("tradeVolume"), "t": None, "src": "angelone-rest"})
            except Exception as e:  # noqa
                self.last_error = scrub(e, self.cfg)
            time.sleep(1.1)                                            # stay under the documented rate limit

    def run(self):
        import websocket
        from SmartApi.smartWebSocketV2 import SmartWebSocketV2
        parser = SmartWebSocketV2.__new__(SmartWebSocketV2)          # only its binary parser is used
        attempt = 0
        while not self.stop_flag:
            try:
                self.state = "CONNECTING" if attempt == 0 else "RECONNECTING"
                self.login()
                self.snapshot_rest()

                def on_open(ws):
                    ws.send(json.dumps({"correlationID": "vision0001", "action": 1,
                                        "params": {"mode": 3, "tokenList": [{"exchangeType": 1, "tokens": list(self.tokens)}]}}))
                    self.state, self.connected_at = "LIVE", time.time()
                    audit("stream_connected", symbols=len(self.tokens))

                def on_data(ws, data, dtype, cont):
                    if dtype != 2 or len(data) < 51:
                        return
                    m = SmartWebSocketV2._parse_binary_data(parser, data)
                    sym = self.tokens.get(str(m.get("token")))
                    if sym:
                        self.last_msg = time.time()
                        d = parse_tick(m); d["src"] = "angelone-ws"
                        self.book.update(sym, d)

                def on_error(ws, err):
                    self.last_error = scrub(err, self.cfg)

                ws = websocket.WebSocketApp(WS_URL, on_open=on_open, on_data=on_data, on_error=on_error,
                                            header={"Authorization": self.jwt, "x-api-key": self.cfg["ANGEL_API_KEY"],
                                                    "x-client-code": self.cfg["ANGEL_CLIENT_ID"], "x-feed-token": self.feed_token})
                self.ws = ws
                hb = threading.Thread(target=self._heartbeat, args=(ws,), daemon=True); hb.start()
                ws.run_forever(ping_interval=25, ping_timeout=10)       # certificate verification stays ON
                self.state = "DISCONNECTED"
            except Exception as e:  # noqa
                self.state, self.last_error = "DISCONNECTED", scrub(e, self.cfg)
                audit("stream_error", error=self.last_error)
            if self.stop_flag:
                break
            delay = BACKOFF[min(attempt, len(BACKOFF) - 1)]
            attempt += 1; self.reconnects += 1
            self.state = "RECONNECTING"
            time.sleep(delay)
            if self.connected_at and time.time() - self.connected_at > 300:
                attempt = 0                                             # it was stable for 5 min: restart the backoff ladder

    def _heartbeat(self, ws):
        while ws.keep_running:
            try:
                ws.send("ping")
            except Exception:  # noqa
                return
            time.sleep(10)

    def place_limit(self, sym, side, qty, limit):
        tok = {s: t for t, s in self.tokens.items()}.get(sym)
        return self.api.placeOrder({"variety": "NORMAL", "tradingsymbol": f"{sym}-EQ", "symboltoken": tok, "transactiontype": side,
                                    "exchange": "NSE", "ordertype": "LIMIT", "producttype": "DELIVERY", "duration": "DAY",
                                    "price": f"{limit:.2f}", "quantity": str(int(qty))})

    def health(self):
        age = (time.time() - self.last_msg) if self.last_msg else None
        st = self.state
        if st == "LIVE" and age is not None and age > 30 and market_state() == "OPEN":
            st = "DEGRADED"                                             # socket open but no ticks for 30 s in market hours
        return {"state": st, "last_tick_age_s": round(age, 1) if age is not None else None, "reconnects": self.reconnects,
                "last_error": self.last_error, "connected_since": dt.datetime.fromtimestamp(self.connected_at, IST).strftime("%H:%M:%S") if self.connected_at else None}


# ------------------------------------------------------------------ Shoonya (Finvasia) - free API, OAuth login once a day
def sh_checksum(client_id, secret, code):
    """GenAcsTok checksum = sha256(client_id + secret + auth_code), as in NorenRestApiOAuth.getAccessToken."""
    return hashlib.sha256((client_id + secret + code).encode("utf-8")).hexdigest()


def sh_code_from(text):
    """Accept the auth code itself or the whole redirect URL the browser landed on."""
    import urllib.parse as up
    t = (text or "").strip()
    if "code=" in t:
        q = up.parse_qs(up.urlparse(t).query) or up.parse_qs(t.split("?", 1)[-1])
        t = (q.get("code") or [""])[0]
    return t.strip()


class ShoonyaAuth:
    """Holds the one-day OAuth session. The password never touches this program - only Shoonya's own login page sees it."""

    def __init__(self, cfg, http=None, path=None):
        import requests
        self.cfg, self.http, self.path = cfg, http or requests, path or SH_SESSION
        self.session = None
        self.fail_reason, self.fail_msg, self.fail_at, self.fail_count = None, None, None, 0
        try:
            s = json.load(open(self.path))
            if s.get("day") == now().date().isoformat() and s.get("access_token"):
                self.session = s                                         # today's token survives a program restart
        except Exception:  # noqa
            pass

    def login_url(self):
        import urllib.parse as up
        base = self.cfg.get("SHOONYA_OAUTH_URL") or SH_OAUTH
        return f"{base}?client_id={up.quote(self.cfg.get('SHOONYA_CLIENT_ID', ''))}"

    def exchange(self, code):
        code = sh_code_from(code)
        if not code:
            raise RuntimeError("no auth code found")
        cid, sec, uid = self.cfg.get("SHOONYA_CLIENT_ID", ""), self.cfg.get("SHOONYA_SECRET", ""), self.cfg.get("SHOONYA_UID", "")
        try:
            r = self.http.post(f"{SH_HOST}/GenAcsTok", timeout=20,
                               data="jData=" + json.dumps({"code": code, "checksum": sh_checksum(cid, sec, code), "uid": uid}))
        except Exception as e:  # noqa - Shoonya drops connections from an IP that is not registered for the API key
            self._fail("NETWORK_OR_IP", "Shoonya refused the connection. Most likely your internet address changed: this computer is now "
                       f"{ip_text(self.http)}. Put that number in 'Primary IP Address' on Shoonya's Api Key Generation page, "
                       "click Update, then click Login to Shoonya again. (If the internet itself is down, wait and try again.)", e)
        try:
            j = r.json() if getattr(r, "content", b"x") else {}
        except ValueError:
            j = {}
        if "access_token" not in j:
            why = str(j.get('emsg') or j.get('stat') or getattr(r, "status_code", "?"))
            kind = classify_login_error(why)
            if kind == "INVALID_IP":
                self._fail(kind, "Shoonya says INVALID_IP: your internet address changed. This computer is now "
                           f"{ip_text(self.http)}. Put exactly that number in 'Primary IP Address' on Shoonya's "
                           "Api Key Generation page, click Update, then click Login to Shoonya again. Automatic retry: OFF.")
            if kind == "CONFIG":
                self._fail(kind, f"Shoonya rejected the API settings ({why}). Check SHOONYA_CLIENT_ID / SHOONYA_SECRET in settings.env "
                           "and the Redirect URL http://127.0.0.1:8765/shoonya/callback on Shoonya's Api Key page. Automatic retry: OFF.")
            if kind == "AUTH_CODE":
                self._fail(kind, f"Shoonya did not accept this login code ({why}). Codes work once and expire fast - click Login to Shoonya again.")
            self._fail(kind, f"Shoonya token exchange failed: {why}")
        self.session = {"day": now().date().isoformat(), "access_token": j["access_token"], "uid": j.get("USERID") or uid,
                        "actid": j.get("actid") or uid, "at": now().isoformat(timespec="seconds")}
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        json.dump(self.session, open(self.path, "w"))
        self.fail_reason, self.fail_msg, self.fail_count = None, None, 0
        ok_ip = check_public_ip(self.http, max_age=30).get("ip")
        if ok_ip:                                                       # remember the address Shoonya accepted
            try:
                json.dump({"ip": ok_ip, "at": now().isoformat(timespec="seconds")}, open(os.path.join(os.path.dirname(self.path), "shoonya_ip.json"), "w"))
            except OSError:
                pass
        try:
            os.chmod(self.path, 0o600)                                    # today's token: readable by this user only
        except OSError:
            pass
        audit("shoonya_login_ok", uid=self.session["uid"][:2] + "***")
        return self.session

    def _fail(self, reason, msg, cause=None):
        self.fail_count = self.fail_count + 1 if reason == self.fail_reason else 1
        self.fail_reason, self.fail_msg, self.fail_at = reason, msg, now().strftime("%H:%M:%S")
        audit("shoonya_login_failed", reason=reason, retryable=False, attempt=self.fail_count)
        if cause is not None:
            raise RuntimeError(msg) from cause
        raise RuntimeError(msg)

    def status(self):
        if self.session:
            return "LOGGED IN"
        return "FAILED" if self.fail_reason else "NOT LOGGED IN"

    def headers(self):
        return {"Authorization": f"Bearer {self.session['access_token']}", "Content-Type": "application/json; charset=utf-8"}

    def post(self, route, values):
        r = self.http.post(f"{SH_HOST}/{route}", data="jData=" + json.dumps(values), headers=self.headers(), timeout=15)
        j = r.json()
        if isinstance(j, dict) and j.get("stat") == "Not_Ok" and "session" in str(j.get("emsg", "")).lower():
            self.session = None                                            # expired: the screen asks for a new login
        return j


def sh_tick(m):
    """Shoonya touchline tick ('tk' full / 'tf' only-changed fields) -> portal fields. 'c' is the previous close."""
    f = lambda k: float(m[k]) if m.get(k) not in (None, "", "0", "0.00") else None
    out = {"p": f("lp"), "o": f("o"), "h": f("h"), "l": f("l"), "pc": f("c"), "vwap": f("ap"), "bid": f("bp1"), "ask": f("sp1")}
    if m.get("v") not in (None, ""):
        out["v"] = int(float(m["v"]))
    if m.get("ft"):
        out["t"] = float(m["ft"])
    return out


class ShoonyaFeed(threading.Thread):
    """Shoonya WebSocket: auth with the day's OAuth token, touchline subscription, reconnect with backoff + REST snapshot."""
    daemon = True

    def __init__(self, cfg, book, tokens, auth):
        super().__init__()
        self.cfg, self.book, self.tokens, self.auth = cfg, book, tokens, auth   # tokens: {token: sym}
        self.state, self.last_error, self.connected_at, self.reconnects = "NEEDS LOGIN", "", None, 0
        self.stop_flag, self.last_msg, self.login_event = False, None, threading.Event()
        if auth.session:
            self.login_event.set()

    def snapshot_rest(self):
        for tok, sym in list(self.tokens.items()):
            try:
                ex, tk = tok.split("|") if "|" in tok else ("NSE", tok)
                j = self.auth.post("GetQuotes", {"uid": self.auth.session["uid"], "exch": ex, "token": tk})
                if isinstance(j, dict) and j.get("stat") == "Ok" and j.get("lp"):
                    d = sh_tick(j); d.pop("t", None); d["src"] = "shoonya-rest"
                    self.book.update(sym, d)
            except Exception as e:  # noqa
                self.last_error = scrub(e, self.cfg)
            time.sleep(0.25)

    def run(self):
        import websocket
        attempt = 0
        while not self.stop_flag:
            if not self.auth.session:
                self.state = "NEEDS LOGIN"
                self.login_event.clear(); self.login_event.wait(timeout=30)
                continue
            try:
                self.state = "CONNECTING" if attempt == 0 else "RECONNECTING"
                self.snapshot_rest()
                sess = self.auth.session

                def on_open(ws):
                    ws.send(json.dumps({"t": "a", "uid": sess["uid"], "actid": sess["uid"], "accesstoken": sess["access_token"], "source": "API"}))

                def on_message(ws, msg):
                    m = json.loads(msg)
                    t = m.get("t")
                    if t in ("ak", "ck"):
                        if m.get("s") == "OK":
                            ws.send(json.dumps({"t": "t", "k": "#".join(sh_key(tok) for tok in self.tokens)}))
                            self.state, self.connected_at = "LIVE", time.time()
                            audit("stream_connected", broker="shoonya", symbols=len(self.tokens))
                        else:
                            self.last_error = f"websocket auth refused: {m.get('s')}"
                            self.auth.session = None; ws.close()
                    elif t in ("tk", "tf"):
                        sym = self.tokens.get(f"{m.get('e')}|{m.get('tk')}") or self.tokens.get(str(m.get("tk")))
                        if sym:
                            self.last_msg = time.time()
                            d = sh_tick(m); d["src"] = "shoonya-ws"
                            self.book.update(sym, d)

                def on_error(ws, err):
                    self.last_error = scrub(err, self.cfg)

                ws = websocket.WebSocketApp(SH_WS, on_open=on_open, on_message=on_message, on_error=on_error)
                self.ws = ws
                ws.run_forever(ping_interval=3, ping_payload='{"t":"h"}')     # certificate verification stays ON
                self.state = "DISCONNECTED"
            except Exception as e:  # noqa
                self.state, self.last_error = "DISCONNECTED", scrub(e, self.cfg)
                audit("stream_error", broker="shoonya", error=self.last_error)
            if self.stop_flag:
                break
            delay = BACKOFF[min(attempt, len(BACKOFF) - 1)]
            attempt += 1; self.reconnects += 1
            time.sleep(delay)
            if self.connected_at and time.time() - self.connected_at > 300:
                attempt = 0

    def add_tokens(self, more):
        """Subscribe extra instruments (e.g. today's option legs) without reconnecting."""
        new = {k: v for k, v in more.items() if k not in self.tokens}
        self.tokens.update(new)
        ws = getattr(self, "ws", None)
        if new and ws is not None and self.state == "LIVE":
            try:
                ws.send(json.dumps({"t": "t", "k": "#".join(sh_key(k) for k in new)}))
            except Exception as e:  # noqa
                self.last_error = scrub(e, self.cfg)

    def place_limit(self, sym, side, qty, limit):
        return self.place_order("NSE", f"{sym}-EQ", side, qty, limit, "C")

    def place_order(self, exch, tsym, side, qty, limit, prd):
        import urllib.parse as up
        j = self.auth.post("PlaceOrder", {"ordersource": "API", "uid": self.auth.session["uid"], "actid": self.auth.session["actid"],
                                          "trantype": "B" if side == "BUY" else "S", "prd": prd, "exch": exch, "tsym": up.quote_plus(tsym),
                                          "qty": str(int(qty)), "dscqty": "0", "prctyp": "LMT", "prc": f"{limit:.2f}", "ret": "DAY",
                                          "remarks": "vision_live"})
        if isinstance(j, dict) and j.get("stat") == "Ok":
            return j.get("norenordno")
        raise RuntimeError(f"order rejected: {(j or {}).get('emsg') if isinstance(j, dict) else j}")

    def health(self):
        age = (time.time() - self.last_msg) if self.last_msg else None
        st = self.state
        if st == "LIVE" and age is not None and age > 30 and market_state() == "OPEN":
            st = "DEGRADED"
        return {"state": st, "last_tick_age_s": round(age, 1) if age is not None else None, "reconnects": self.reconnects,
                "last_error": self.last_error, "broker": "shoonya", "login_url": "/shoonya/login" if st == "NEEDS LOGIN" else None,
                "connected_since": dt.datetime.fromtimestamp(self.connected_at, IST).strftime("%H:%M:%S") if self.connected_at else None}


def sh_symbol_tokens(http, watch):
    """{sym: token} from Shoonya's public NSE symbol master (zip of a CSV: Exchange,Token,LotSize,Symbol,TradingSymbol,...)."""
    import io, zipfile, csv
    out = {s: SH_INDEX_TOKENS[s] for s in watch if s in SH_INDEX_TOKENS}
    r = http.get(SH_SYMBOLS, timeout=90)
    r.raise_for_status()
    z = zipfile.ZipFile(io.BytesIO(r.content))
    rows = csv.DictReader(io.TextIOWrapper(z.open(z.namelist()[0]), encoding="utf-8"))
    want = {s for s in watch if s not in out}
    for row in rows:
        ts = (row.get("TradingSymbol") or "").strip()
        if ts.endswith("-EQ") and ts[:-3] in want:
            out[ts[:-3]] = (row.get("Token") or "").strip()
    return out


RAW_SITE = "https://raw.githubusercontent.com/16VITAWS/market-scanner/gh-pages/"
PORTAL_QUOTES = "https://raw.githubusercontent.com/16VITAWS/market-scanner/live-data/api/quotes.json"


class PortalDelayedFeed(threading.Thread):
    """No working broker login (e.g. INVALID_IP)? Keep the PAPER engine useful with the portal's free quotes:
    ~15 minutes behind the exchange, refreshed about every 2 minutes in market hours. Always labelled DELAYED,
    never called live, and never used for real orders."""
    daemon = True

    def __init__(self, http, book, syms, needed, url=PORTAL_QUOTES):
        super().__init__()
        self.http, self.book, self.syms, self.needed, self.url = http, book, syms, needed, url
        self.last_ok, self.last_error, self.count = None, "", 0

    def pull(self):
        j = self.http.get(f"{self.url}?t={int(time.time())}", timeout=20).json()
        n = 0
        for sym in list(self.syms()):
            q = (j.get("quotes") or {}).get(sym) or {}
            if not q.get("p") or not q.get("t"):
                continue
            try:
                t = dt.datetime.fromisoformat(str(q["t"]).replace("Z", "+00:00")).timestamp()
            except ValueError:
                continue
            if (self.book.get(sym).get("t") or 0) >= t:
                continue                                                  # nothing newer
            if self.book.update(sym, {"p": q["p"], "pc": q.get("pc"), "o": q.get("day_o"), "h": q.get("day_h"), "l": q.get("day_l"),
                                      "v": q.get("day_v"), "t": t, "src": "portal-delayed"}):
                n += 1
        self.last_ok, self.count = now().strftime("%H:%M:%S"), n
        return n

    def run(self):
        while True:
            if self.needed():
                try:
                    self.pull()
                except Exception as e:  # noqa
                    self.last_error = str(e)[:200]
            time.sleep(60)


IP_SOURCES = ("https://api.ipify.org", "https://checkip.amazonaws.com", "https://icanhazip.com", "https://ifconfig.me/ip")
_IP_CACHE = {"at": 0.0, "res": None}
SH_IP_FILE = os.path.join(HOME, "shoonya_ip.json")


def _public(s):
    """Only a real internet (public) address counts - never 127.0.0.1 / 192.168.x / 10.x / 172.16-31.x."""
    import ipaddress
    try:
        a = ipaddress.ip_address((s or "").strip())
    except ValueError:
        return None
    return str(a) if a.is_global else None


def check_public_ip(http, max_age=120, sources=IP_SOURCES):
    """This computer's outbound internet address, asked from several services. Cached; never polled continuously.
    status OK = services agree; UNCERTAIN = they disagree (we do not guess); LOOKUP_FAILED = none answered."""
    if max_age and _IP_CACHE["res"] and time.time() - _IP_CACHE["at"] < max_age:
        return _IP_CACHE["res"]
    v4, v6, asked = [], [], 0
    for u in sources:
        asked += 1
        try:
            ip = _public(http.get(u, timeout=6).text)
        except Exception:  # noqa
            ip = None
        if ip:
            (v6 if ":" in ip else v4).append(ip)
        if len(v4) >= 2 and len(set(v4)) == 1:
            break                                                     # two services agree - enough
    if len(set(v4)) == 1 or (not v4 and len(set(v6)) == 1):
        ip = (v4 or v6)[0]
        res = {"ip": ip, "status": "OK", "agree": len(v4 or v6), "asked": asked}
    elif v4 or v6:
        res = {"ip": None, "status": "UNCERTAIN", "seen": sorted(set(v4 + v6)), "asked": asked}
    else:
        res = {"ip": None, "status": "LOOKUP_FAILED", "asked": asked}
    res["checked"] = now().strftime("%H:%M:%S")
    _IP_CACHE.update(at=time.time(), res=res)
    return res


def public_ip(http):
    return check_public_ip(http, max_age=30)["ip"]


def ip_text(http):
    r = check_public_ip(http, max_age=30)
    if r["ip"]:
        return r["ip"]
    if r["status"] == "UNCERTAIN":
        return "not certain (services disagree: " + ", ".join(r.get("seen", [])) + ")"
    return "unknown - the address check did not answer (is the internet working?)"


def accepted_ip():
    """The last internet address Shoonya actually accepted a login from (saved after every successful login)."""
    try:
        return json.load(open(SH_IP_FILE)).get("ip")
    except Exception:  # noqa
        return None


def registered_ip(cfg):
    return (cfg.get("SHOONYA_REGISTERED_IP") or "").strip() or accepted_ip()


def classify_login_error(msg):
    """CONFIG/IP problems need YOU to change something - never retried automatically. Others: start a fresh login."""
    m = (msg or "").upper()
    if "INVALID_IP" in m or "IP_NOT" in m:
        return "INVALID_IP"
    if any(w in m for w in ("CLIENT", "SECRET", "CHECKSUM", "NOT ENABLED", "REDIRECT", "APP KEY", "APPKEY")):
        return "CONFIG"
    if any(w in m for w in ("CODE", "EXPIRED", "TOKEN", "SESSION")):
        return "AUTH_CODE"
    return "BROKER"


def sh_key(tok):
    return tok if "|" in tok else f"NSE|{tok}"


def _expiry_date(txt):
    txt = (txt or "").strip().upper()
    for f in ("%d-%b-%Y", "%d%b%Y", "%Y-%m-%d", "%d-%m-%Y", "%d%b%y", "%d-%b-%y"):
        try:
            return dt.datetime.strptime(txt, f).date()
        except ValueError:
            pass
    return None


def sh_option_legs(http, sig):
    """Find today's two option contracts in Shoonya's public NFO symbol master.
    Returns {'long': {token, tsym, lot, strike}, 'short': {...}} or raises with the reason."""
    import io, zipfile, csv
    exp = dt.date.fromisoformat(sig["expiry"])
    ot = "PE" if sig["kind"] == "P" else "CE"
    want = {int(round(float(sig["k_long"]))): "long", int(round(float(sig["k_short"]))): "short"}
    r = http.get(SH_NFO, timeout=120)
    r.raise_for_status()
    z = zipfile.ZipFile(io.BytesIO(r.content))
    rows = csv.DictReader(io.TextIOWrapper(z.open(z.namelist()[0]), encoding="utf-8"))
    out = {}
    for row in rows:
        g = {k.strip().lower(): (v or "").strip() for k, v in row.items() if k}
        if g.get("symbol") != sig.get("underlying", "NIFTY") or g.get("optiontype") != ot or not g.get("instrument", "").startswith("OPTIDX"):
            continue
        if _expiry_date(g.get("expiry")) != exp:
            continue
        try:
            k = int(round(float(g.get("strikeprice") or 0)))
        except ValueError:
            continue
        if k in want:
            out[want[k]] = {"token": g["token"], "tsym": g.get("tradingsymbol"), "lot": int(float(g.get("lotsize") or sig.get("lot") or 0)), "strike": k}
    if len(out) != 2:
        raise RuntimeError(f"option contracts not found in Shoonya's list ({sig['name']})")
    return out


class OptionsTrader:
    """Today's NIFTY debit spread from the portal, priced on LIVE Shoonya option quotes (not modeled).
    PAPER records it; ALERT also phones you both legs; REAL places both legs as LIMIT orders - only behind its own lock."""
    P = {"take_profit": 0.5, "stop_loss": 0.5, "exit_dte": 2}

    def __init__(self, cfg, book, safety, notify, path=None):
        self.cfg, self.book, self.safety, self.notify = cfg, book, safety, notify
        self.path = path or OPT_FILE
        self.sig, self.legs, self.active, self.feed, self.events = None, None, None, None, []
        try:
            self.s = json.load(open(self.path))
        except Exception:  # noqa
            self.s = {"open": None, "trades": [], "entered": {}}
        o = self.s.get("open")
        if o and o.get("legs_full"):                       # an open spread survives a restart: keep pricing its own legs
            self.sig = {k: o[k] for k in ("name", "expiry", "kind", "width")}
            self.legs = o["legs_full"]

    def save(self):
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        tmp = self.path + ".tmp"; json.dump(self.s, open(tmp, "w"), indent=1); os.replace(tmp, self.path)

    def log(self, kind, msg):
        self.events = (self.events + [{"time": now().strftime("%H:%M:%S"), "kind": kind, "msg": msg}])[-100:]
        audit("opt_" + kind, msg=msg)

    # names used in the Book for the two legs
    def leg_syms(self, legs=None, sig=None):
        legs, sig = legs or self.legs, sig or self.sig
        if not legs or not sig:
            return {}
        ot = "PE" if sig["kind"] == "P" else "CE"
        e = dt.date.fromisoformat(sig["expiry"]).strftime("%d%b%y").upper()
        return {r: f"OPT NIFTY {e} {legs[r]['strike']} {ot}" for r in ("long", "short")}

    def set_signal(self, sig, legs=None):
        """sig: the portal's options signal (may be 'no trade'). legs: its contracts when known.
        While a spread is open we keep pricing THAT spread, whatever the portal says now."""
        self.active = sig
        o = self.s.get("open")
        if legs and not (o and sig and o["name"] != sig.get("name")):
            self.sig, self.legs = sig, legs

    def mode(self):
        m = (self.cfg.get("OPTIONS_MODE") or self.cfg.get("MODE") or "PAPER").upper()
        if m == "REAL" and self.real_blockers():
            return "PAPER"
        return m if m in ("PAPER", "ALERT", "REAL") else "PAPER"

    def record(self):
        tr = self.s["trades"]
        wins = sum(t["pnl"] for t in tr if t["pnl"] > 0); losses = -sum(t["pnl"] for t in tr if t["pnl"] < 0)
        pf = (wins / losses) if losses else (None if not wins else float("inf"))
        n = int(self.cfg.get("OPTIONS_MIN_TRADES") or 20)
        ok = len(tr) >= n and pf is not None and pf >= GATE["min_pf"] and sum(t["pnl"] for t in tr) > 0
        return {"passed": ok, "closed": len(tr), "need": n, "pf": None if pf is None else (round(pf, 2) if pf != float("inf") else "no losses")}

    def real_blockers(self):
        b = list(self.safety.real_blockers_base())
        if not self.record()["passed"]:
            b.append("options live-paper record not passed yet")
        if (self.cfg.get("OPTIONS_REAL") or "no").lower() not in ("yes", "y", "true", "1"):
            b.append("OPTIONS_REAL is not set to yes")
        return b

    def _px(self, sym, side):
        """Executable price: ask to buy, bid to sell; last traded price only if no depth."""
        q = self.book.get(sym)
        v = q.get("ask" if side == "BUY" else "bid") or q.get("p")
        return v if v and v > 0 else None

    def values(self):
        L = self.leg_syms()
        if not L:
            return None
        lb, ss = self._px(L["long"], "BUY"), self._px(L["short"], "SELL")         # cost to open
        ls, sb = self._px(L["long"], "SELL"), self._px(L["short"], "BUY")         # value to close
        if None in (lb, ss, ls, sb):
            return None
        return {"open_debit": round(lb - ss, 2), "close_value": round(ls - sb, 2), "long": self.book.get(L["long"]).get("p"), "short": self.book.get(L["short"]).get("p")}

    def on_tick(self, state="OPEN"):
        if state != "OPEN" or not self.sig:
            return
        v = self.values()
        if not v:
            return
        today = now().date()
        o = self.s["open"]
        if o:
            exp = dt.date.fromisoformat(o["expiry"])
            width, debit, val = o["width"], o["debit"], v["close_value"]
            why = None
            if val - debit >= self.P["take_profit"] * (width - debit):
                why = f"take-profit: spread {val:.2f} >= debit {debit:.2f} + 50% of max profit"
            elif val <= debit * (1 - self.P["stop_loss"]):
                why = f"stop: spread {val:.2f} <= 50% of debit {debit:.2f}"
            elif (exp - today).days <= self.P["exit_dte"] and now().time() >= dt.time(9, 20):
                why = f"time exit: {(exp - today).days} days to expiry"
            elif not self.active or self.active.get("action") != "BUY" or self.active.get("name") != o["name"]:
                why = "portal signal no longer active (trend changed)"
            if why:
                self.exit(val, why)
            return
        if not self.active or self.active.get("action") != "BUY" or self.active.get("name") != self.sig.get("name"):
            return
        if self.s["entered"].get(self.sig["name"]) == today.isoformat():
            return
        if not (dt.time(9, 20) <= now().time() <= dt.time(15, 0)):
            return
        if (dt.date.fromisoformat(self.sig["expiry"]) - today).days <= self.P["exit_dte"]:
            return
        d, width = v["open_debit"], float(self.sig["width"])
        if not (0 < d < width):
            return
        lots = max(1, int(self.cfg.get("OPTIONS_LOTS") or 1))
        qty = lots * int(self.legs["long"]["lot"] or self.sig["lot"])
        risk = d * qty
        if risk > float(self.cfg.get("MAX_OPTION_RISK") or 6000):
            self.s["entered"][self.sig["name"]] = today.isoformat(); self.save()
            self.log("skip", f"{self.sig['name']}: max loss Rs {risk:,.0f} above MAX_OPTION_RISK"); return
        if self.safety.killed():
            self.log("blocked", "options entry blocked: kill switch ON"); self.s["entered"][self.sig["name"]] = today.isoformat(); self.save(); return
        self.enter(d, qty)

    def enter(self, debit, qty):
        sig = self.sig
        self.s["open"] = {"name": sig["name"], "expiry": sig["expiry"], "kind": sig["kind"], "width": float(sig["width"]), "debit": debit, "qty": qty,
                          "legs": {r: self.legs[r]["tsym"] for r in ("long", "short")}, "legs_full": self.legs, "at": now().isoformat(timespec="seconds")}
        self.s["entered"][sig["name"]] = now().date().isoformat(); self.save()
        L = self.leg_syms()
        msg = (f"BUY {qty} {L['long']} + SELL {qty} {L['short']} - net debit {debit:.2f} (max loss Rs {debit*qty:,.0f}, "
               f"max profit Rs {(float(sig['width'])-debit)*qty:,.0f}) - {'; '.join(sig.get('why') or [])}")
        self.log("paper_entry", "PAPER " + msg)
        m = self.mode()
        if m == "ALERT":
            self.notify("Options: open spread now", msg + ". Place both legs in the Shoonya app (BUY leg first).")
        elif m == "REAL":
            self.real_legs([("BUY", "long"), ("SELL", "short")], qty)

    def exit(self, value, why):
        o = self.s["open"]
        pnl = round((value - o["debit"]) * o["qty"] - 4 * 20, 2)          # ~Rs 20 per leg per side, both ways
        self.s["trades"].append({"name": o["name"], "qty": o["qty"], "debit": o["debit"], "exit": value, "pnl": pnl,
                                 "opened": o["at"], "closed": now().isoformat(timespec="seconds"), "why": why})
        self.s["open"] = None; self.save()
        self.log("paper_exit", f"PAPER EXIT {o['name']} at {value:.2f} (debit {o['debit']:.2f}) - P&L Rs {pnl:+,.0f} - {why}")
        m = self.mode()
        if m == "ALERT":
            self.notify("Options: close spread now", f"Close {o['name']}: BUY back the short leg, then SELL the long leg. {why}")
        elif m == "REAL":
            self.real_legs([("BUY", "short"), ("SELL", "long")], o["qty"])    # buy back the short first: never left naked short

    def real_legs(self, steps, qty):
        if self.real_blockers() or not self.feed:
            self.log("real_blocked", "REAL options orders not sent: " + "; ".join(self.real_blockers() or ["no broker"])); return
        L = self.leg_syms()
        for side, role in steps:
            px = self._px(L[role], side)
            q = self.book.get(L[role])
            if not px or not q.get("recv") or time.time() - q["recv"] > 5:
                self.log("real_blocked", f"REAL {side} {role} leg not sent: live price older than 5 s - check the Shoonya app"); return
            limit = tick_round(px * (1.02 if side == "BUY" else 0.98), side == "BUY")
            try:
                oid = self.feed.place_order("NFO", self.legs[role]["tsym"], side, qty, limit, "M")
                self.log("real_order", f"REAL {side} {qty} {self.legs[role]['tsym']} LIMIT {limit:.2f} -> order {oid}")
                self.notify(f"REAL options {side}", f"{side} {qty} {self.legs[role]['tsym']} @ {limit:.2f}, order {oid}")
            except Exception as e:  # noqa
                self.log("real_error", f"REAL {side} {role} leg failed: {scrub(e, self.cfg)} - check positions in the Shoonya app now")
                self.notify("REAL options order FAILED", f"{side} {role} leg failed - open the Shoonya app and check positions")
                return

    def state(self):
        L, v, o = self.leg_syms(), self.values(), self.s["open"]
        return {"signal": self.active, "legs": {r: {"sym": L.get(r), "tsym": (self.legs or {}).get(r, {}).get("tsym"), "ltp": self.book.get(L[r]).get("p") if L else None} for r in ("long", "short")} if L else None,
                "live": v, "open": o, "open_pnl": round((v["close_value"] - o["debit"]) * o["qty"], 2) if (o and v) else None,
                "trades": self.s["trades"][-20:], "record": self.record(), "mode": self.mode(), "blockers": self.real_blockers(), "events": self.events[-30:]}


# ------------------------------------------------------------------ accounts
class Paper:
    def __init__(self, capital, path=None):
        self.path = path or PAPER_FILE
        try:
            self.s = json.load(open(self.path))                        # the track record survives restarts
        except Exception:  # noqa
            self.s = {"cash": float(capital), "start": float(capital), "pos": {}, "trades": [], "fills": []}

    def save(self):
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        tmp = self.path + ".tmp"
        json.dump(self.s, open(tmp, "w"), indent=1)
        os.replace(tmp, self.path)                      # atomic: a crash never leaves a half-written account

    def buy(self, sym, qty, price, stop, target, why):
        cost = qty * price * 1.0012                                      # ~0.12% delivery costs (STT, charges)
        if cost > self.s["cash"] or sym in self.s["pos"]:
            return None
        self.s["cash"] -= cost
        self.s["pos"][sym] = {"qty": qty, "avg": price, "stop": stop, "target": target, "at": now().isoformat(timespec="seconds"), "why": why}
        f = {"time": now().isoformat(timespec="seconds"), "side": "BUY", "sym": sym, "qty": qty, "price": price, "why": why}
        self.s["fills"].append(f); self.save()
        return f

    def sell(self, sym, price, why):
        p = self.s["pos"].pop(sym, None)
        if not p:
            return None
        proceeds = p["qty"] * price * (1 - 0.0012)
        self.s["cash"] += proceeds
        pnl = proceeds - p["qty"] * p["avg"] * 1.0012
        self.s["trades"].append({"sym": sym, "qty": p["qty"], "entry": p["avg"], "exit": price, "pnl": round(pnl, 2),
                                 "opened": p["at"], "closed": now().isoformat(timespec="seconds"), "why": why})
        f = {"time": now().isoformat(timespec="seconds"), "side": "SELL", "sym": sym, "qty": p["qty"], "price": price, "why": why, "pnl": round(pnl, 2)}
        self.s["fills"].append(f); self.save()
        return f

    def equity(self, book):
        return self.s["cash"] + sum(p["qty"] * (book.get(s).get("p") or p["avg"]) for s, p in self.s["pos"].items())

    def realized_today(self):
        d = now().date().isoformat()
        return sum(t["pnl"] for t in self.s["trades"] if t["closed"][:10] == d)

    def record(self):
        tr = self.s["trades"]
        wins = sum(t["pnl"] for t in tr if t["pnl"] > 0); losses = -sum(t["pnl"] for t in tr if t["pnl"] < 0)
        pf = (wins / losses) if losses else (None if not wins else float("inf"))
        exp = (sum(t["pnl"] for t in tr) / len(tr)) if tr else None
        checks = [{"check": f">= {GATE['min_closed']} closed live-paper trades", "value": len(tr), "ok": len(tr) >= GATE["min_closed"]},
                  {"check": f"profit factor >= {GATE['min_pf']}", "value": None if pf is None else (round(pf, 2) if pf != float("inf") else "no losses"),
                   "ok": pf is not None and pf >= GATE["min_pf"]},
                  {"check": "positive expectancy after costs", "value": None if exp is None else round(exp, 2), "ok": exp is not None and exp > 0}]
        return {"passed": all(c["ok"] for c in checks), "checks": checks}


class Safety:
    def __init__(self, cfg):
        self.cfg = cfg
        self.site_kill = False
        self.orders_today, self.day = 0, now().date()

    def killed(self):
        return os.path.exists(KILL_FILE) or self.site_kill

    def real_blockers_base(self):
        b = []
        if self.cfg.get("CONSENT") != CONSENT_PHRASE:
            b.append("CONSENT phrase not set")
        if self.cfg.get("STATIC_IP_REGISTERED", "no").lower() not in ("yes", "y", "true", "1"):
            b.append("static IP not registered with your broker (SEBI rule)")
        if self.killed():
            b.append("kill switch ON")
        return b

    def real_blockers(self, paper):
        b = []
        if self.cfg.get("MODE", "PAPER").upper() != "REAL":
            b.append("MODE is not REAL")
        b += [x for x in self.real_blockers_base() if x != "kill switch ON"]
        if not paper.record()["passed"]:
            b.append("live-paper track record gate not passed")
        if self.killed():
            b.append("kill switch ON")
        return b

    def check_order(self, side, value, open_positions, realized_today):
        if now().date() != self.day:
            self.day, self.orders_today = now().date(), 0
        lim = lambda k, d: float(self.cfg.get(k) or d)
        if self.killed():
            return "kill switch ON"
        if side == "BUY":
            if self.orders_today >= lim("MAX_ORDERS_PER_DAY", 3):
                return "daily order limit reached"
            if value > lim("MAX_ORDER_VALUE", 25000):
                return "order value above limit"
            if open_positions >= lim("MAX_OPEN_POSITIONS", 3):
                return "max open positions reached"
            if -realized_today >= lim("MAX_DAILY_LOSS", 2000):
                return "daily loss limit reached - no new buys today"
        return None


# ------------------------------------------------------------------ the algorithm, acting on live ticks
class Trader:
    def __init__(self, cfg, book, paper, safety, tokens_by_sym, feed=None, notify=None):
        self.cfg, self.book, self.paper, self.safety, self.tokens, self.feed = cfg, book, paper, safety, tokens_by_sym, feed
        self.notify = notify or (lambda t, m: None)
        self.signals = {"buys": [], "sells": [], "regime": None, "data_status": None}
        self.events = []
        self.done_today = set()
        self.real = {}

    def set_signals(self, sig):
        self.signals = {"buys": [b for b in sig.get("buys") or [] if b.get("symbol")], "sells": [s.get("symbol") for s in sig.get("sells") or []],
                        "regime": (sig.get("regime") or {}).get("state"), "data_status": sig.get("data_status"), "bar": sig.get("generated_at")}

    def log(self, kind, msg, **kw):
        e = {"time": now().strftime("%H:%M:%S"), "kind": kind, "msg": msg, **kw}
        self.events = (self.events + [e])[-200:]
        audit(kind, msg=msg, **kw)

    def mode(self):
        m = self.cfg.get("MODE", "PAPER").upper()
        if m == "REAL" and self.safety.real_blockers(self.paper):
            return "PAPER"                                                # REAL requested but locked -> behave as PAPER
        return m if m in ("PAPER", "ALERT", "REAL") else "PAPER"

    def on_tick(self, sym, state="OPEN"):
        if state != "OPEN":
            return
        if getattr(self, "day", None) != now().date():
            self.day, self.done_today = now().date(), set()
        q = self.book.get(sym)
        px = q.get("p")
        if not px:
            return
        delayed = q.get("src") == "portal-delayed"
        if delayed and self.mode() == "REAL":
            return                                                        # real money never acts on ~15-min-old prices
        lv = "delayed" if delayed else "live"
        pos = self.paper.s["pos"].get(sym)
        if pos:
            why = None
            if px <= pos["stop"]:
                why = f"stop-loss hit at {lv} {px:.2f} (stop {pos['stop']:.2f})"
            elif pos.get("target") and px >= pos["target"]:
                why = f"target hit at {lv} {px:.2f} (target {pos['target']:.2f})"
            elif sym in self.signals["sells"]:
                why = "portal signal turned SELL"
            if why:
                self.exit(sym, px, why)
            return
        t = now().time()
        if not (dt.time(9, 20) <= t <= dt.time(15, 0)) or sym in self.done_today or self.signals.get("data_status") not in (None, "OK"):
            return
        for b in self.signals["buys"]:
            if b["symbol"] != sym or not b.get("entry") or not b.get("stop"):
                continue
            if not (b["entry"] * 0.995 <= px <= b["entry"] * 1.005):
                continue                                                  # never chase: only near the signal's entry
            risk_per_share = px - b["stop"]
            if risk_per_share <= 0:
                continue
            max_val = float(self.cfg.get("MAX_ORDER_VALUE") or 25000)
            risk_budget = float(self.cfg.get("MAX_DAILY_LOSS") or 2000) / 2
            qty = int(min(max_val // px, risk_budget // risk_per_share))
            if qty < 1:
                self.log("skip", f"{sym}: position size < 1 share within limits"); self.done_today.add(sym); return
            self.enter(sym, qty, px, b["stop"], b.get("target"), f"signal BUY (score {b.get('score')}) - live {px:.2f} near entry {b['entry']:.2f}")
            return

    def enter(self, sym, qty, px, stop, target, why):
        self.done_today.add(sym)
        block = self.safety.check_order("BUY", qty * px, len(self.paper.s["pos"]), self.paper.realized_today())
        if block:
            self.log("blocked", f"BUY {qty} {sym} blocked: {block}"); return
        m = self.mode()
        f = self.paper.buy(sym, qty, px, stop, target, why)
        if not f:
            self.log("skip", f"{sym}: not enough paper cash"); return
        self.safety.orders_today += 1
        self.log("paper_buy", f"PAPER BUY {qty} {sym} @ {px:.2f} · stop {stop:.2f}" + (f" · target {target:.2f}" if target else "") + f" · {why}")
        if m == "ALERT":
            self.notify(f"BUY {sym} now", f"BUY {qty} {sym} around {px:.2f}. Stop {stop:.2f}. Place it in your app (VISION LIVE alert).")
        elif m == "REAL":
            self.place_real(sym, "BUY", qty, px, why)

    def exit(self, sym, px, why):
        f = self.paper.sell(sym, px, why)
        if not f:
            return
        self.log("paper_sell", f"PAPER SELL {f['qty']} {sym} @ {px:.2f} · P&L {f['pnl']:+.2f} · {why}")
        m = self.mode()
        if m == "ALERT":
            self.notify(f"SELL {sym} now", f"SELL {f['qty']} {sym} around {px:.2f}: {why}. Do it in your app.")
        elif m == "REAL" and sym in self.real:
            self.place_real(sym, "SELL", self.real[sym]["qty"], px, why)

    def place_real(self, sym, side, qty, px, why):
        blockers = self.safety.real_blockers(self.paper)
        if blockers:
            self.log("real_blocked", f"REAL {side} {qty} {sym} not sent: {'; '.join(blockers)}"); return
        q = self.book.get(sym)
        if not q.get("recv") or time.time() - q["recv"] > 5:
            self.log("real_blocked", f"REAL {side} {sym} not sent: live price older than 5 s"); return
        limit = tick_round(px * 1.005, True) if side == "BUY" else tick_round(px * 0.995, False)
        try:
            oid = self.feed.place_limit(sym, side, qty, limit)
            rec = {"time": now().isoformat(timespec="seconds"), "side": side, "sym": sym, "qty": qty, "limit": limit, "order_id": oid, "why": why}
            if oid and side == "BUY":
                self.real[sym] = rec
            elif side == "SELL":
                self.real.pop(sym, None)
            try:
                hist = json.load(open(REAL_FILE))
            except Exception:  # noqa
                hist = []
            json.dump(hist + [rec], open(REAL_FILE, "w"), indent=1)
            self.log("real_order", f"REAL {side} {qty} {sym} LIMIT {limit:.2f} -> order id {oid}")
            self.notify(f"REAL {side} {sym}", f"Sent LIMIT {side} {qty} {sym} @ {limit:.2f}. Order id {oid}.")
        except Exception as e:  # noqa
            self.log("real_error", f"REAL {side} {sym} failed: {scrub(e, self.cfg)}")


# ------------------------------------------------------------------ local live screen
PAGE = r"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>16VITAWS LIVE</title><style>
:root{--bg:#0B1220;--card:#121B2E;--ink:#E8EEF8;--mut:#8FA3BF;--up:#2FE39A;--dn:#FF5C7A;--line:#22304A;--warn:#FFC857}
body{margin:0;background:var(--bg);color:var(--ink);font:15px system-ui,Segoe UI,Roboto,sans-serif}
header{display:flex;gap:10px;align-items:center;flex-wrap:wrap;padding:12px 16px;border-bottom:1px solid var(--line);position:sticky;top:0;background:var(--bg)}
b.brand{font-size:18px}.chip{padding:4px 10px;border-radius:999px;background:#1B2842;font-size:13px}.LIVE{background:#0F3D2C;color:var(--up)}
.RECONNECTING,.CONNECTING,.DEGRADED{background:#3A2E16;color:var(--warn)}.DISCONNECTED,.STALE{background:#4A1620;color:#FFB4B4}
main{padding:12px 16px;display:grid;gap:12px;grid-template-columns:repeat(auto-fit,minmax(340px,1fr))}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:12px;overflow:auto}
table{width:100%;border-collapse:collapse;font-variant-numeric:tabular-nums}td,th{padding:6px;border-bottom:1px solid var(--line);text-align:left;font-size:14px}
th{color:var(--mut);font-weight:500}.num{text-align:right}.up{color:var(--up)}.dn{color:var(--dn)}.mut{color:var(--mut)}
button{background:#8B1E2E;color:#fff;border:0;border-radius:8px;padding:8px 14px;font-weight:600;cursor:pointer}button.g{background:#1F3A6B}
.flash{animation:f .6s}@keyframes f{from{background:#23406b}to{background:transparent}}ul{margin:0;padding-left:18px}li{margin:3px 0;font-size:13px}
</style></head><body><header><b class="brand">16VITAWS LIVE</b><span id="feed" class="chip">…</span><span id="mode" class="chip">…</span><span id="mkt" class="chip">…</span>
<span id="clock" class="chip mut"></span><span style="flex:1"></span><a id="login" class="chip" href="/shoonya/login" target="_blank" style="display:none;background:#1F6A4A;color:#fff;text-decoration:none;font-weight:600">🔑 Login to Shoonya</a><button id="kill">■ KILL SWITCH</button></header>
<div id="codebox" style="display:none;padding:10px 16px;background:#1B2842"><b>After logging in:</b> if Shoonya's page did not come back here by itself, copy the full address from that tab and paste it: <input id="code" style="width:50%;padding:6px" placeholder="https://...?code=..."> <button class="g" id="codebtn">Use this login</button> <span id="codemsg" class="mut"></span></div>
<main><div class="card" style="grid-column:1/-1;border-color:#2A4A7A"><h3>Connection check</h3><div id="diag" class="mut">…</div></div><div class="card" style="grid-column:1/-1"><table id="q"></table><p class="mut" style="font-size:12px">Prices: your broker's exchange feed, updated on every trade (tick). "Age" = seconds since that symbol's last exchange tick. Nothing is estimated: a symbol without a tick shows "—".</p></div>
<div class="card"><h3>Positions (live P&amp;L)</h3><table id="pos"></table><p id="acct" class="mut"></p></div>
<div class="card"><h3>REAL trading lock</h3><div id="gate"></div></div>
<div class="card" style="grid-column:1/-1;border-color:#3E2A5C"><h3>&#127919; NIFTY options - live Shoonya prices</h3><div id="opt"></div></div>
<div class="card"><h3>Today's signals from the portal</h3><div id="sig"></div></div>
<div class="card"><h3>Activity</h3><ul id="ev"></ul></div></main>
<script>
const $=s=>document.querySelector(s),f2=x=>x==null?'—':Number(x).toLocaleString('en-IN',{minimumFractionDigits:2,maximumFractionDigits:2}),e=s=>String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
let last={};
function draw(S){const nl=S.feed.state==='NEEDS LOGIN';$('#login').style.display=nl?'':'none';$('#codebox').style.display=nl?'':'none';$('#feed').className='chip '+S.feed.state;$('#feed').textContent='● '+S.feed.state+(S.feed.last_tick_age_s!=null?' · last tick '+S.feed.last_tick_age_s+' s ago':'')+(S.feed.reconnects?' · reconnects '+S.feed.reconnects:'');
 $('#mode').textContent='MODE '+S.mode+(S.mode_requested!==S.mode?' (REAL locked → PAPER)':'');$('#mkt').textContent='NSE '+S.market;$('#clock').textContent=S.time+' IST';
 $('#kill').textContent=S.killed?'KILLED - click to resume':'■ KILL SWITCH';$('#kill').className=S.killed?'g':'';
 $('#q').innerHTML='<tr><th>Symbol</th><th class=num>LTP</th><th class=num>Chg %</th><th class=num>Open</th><th class=num>High</th><th class=num>Low</th><th class=num>VWAP</th><th class=num>Bid / Ask</th><th class=num>Volume</th><th>Exchange time</th><th class=num>Age</th></tr>'+
  S.quotes.map(q=>{const c=q.pc?(q.p/q.pc-1)*100:null,ch=last[q.sym]!==q.p;last[q.sym]=q.p;return `<tr class="${ch?'flash':''}"><td><b>${e(q.sym)}</b></td><td class=num><b>${f2(q.p)}</b></td><td class="num ${c>0?'up':c<0?'dn':''}">${c==null?'—':(c>0?'+':'')+c.toFixed(2)+'%'}</td><td class=num>${f2(q.o)}</td><td class=num>${f2(q.h)}</td><td class=num>${f2(q.l)}</td><td class=num>${f2(q.vwap)}</td><td class=num>${q.bid?f2(q.bid)+' / '+f2(q.ask):'—'}</td><td class=num>${q.v==null?'—':Number(q.v).toLocaleString('en-IN')}</td><td>${e(q.time||'—')}</td><td class=num>${q.age==null?'—':q.age+' s'}</td></tr>`}).join('');
 $('#pos').innerHTML='<tr><th>Symbol</th><th class=num>Qty</th><th class=num>Avg</th><th class=num>LTP</th><th class=num>Stop</th><th class=num>Target</th><th class=num>P&amp;L</th></tr>'+(S.positions.map(p=>`<tr><td><b>${e(p.sym)}</b></td><td class=num>${p.qty}</td><td class=num>${f2(p.avg)}</td><td class=num>${f2(p.ltp)}</td><td class=num>${f2(p.stop)}</td><td class=num>${f2(p.target)}</td><td class="num ${p.pnl>=0?'up':'dn'}">${f2(p.pnl)}</td></tr>`).join('')||'<tr><td colspan=7 class=mut>No open positions</td></tr>');
 $('#acct').textContent=`Paper equity ₹${f2(S.account.equity)} · cash ₹${f2(S.account.cash)} · closed trades ${S.account.closed} · realised today ₹${f2(S.account.today)}`;
 $('#gate').innerHTML=(S.real_blockers.length?'<p class=dn><b>REAL orders locked</b></p><ul>'+S.real_blockers.map(b=>`<li>${e(b)}</li>`).join('')+'</ul>':'<p class=up><b>REAL orders enabled (limits still apply)</b></p>')+'<ul>'+S.gate.checks.map(c=>`<li class="${c.ok?'up':'mut'}">${c.ok?'✓':'○'} ${e(c.check)}: ${e(c.value??'—')}</li>`).join('')+'</ul>';
 $('#sig').innerHTML=`<p class=mut>Regime ${e(S.signals.regime||'?')} · data ${e(S.signals.data_status||'?')}</p><p><b>BUY</b>: ${S.signals.buys.map(b=>e(b.symbol)+' near '+f2(b.entry)).join(', ')||'none today'}</p><p><b>SELL</b>: ${S.signals.sells.map(e).join(', ')||'none'}</p>`;
 $('#ev').innerHTML=S.events.slice().reverse().slice(0,40).map(x=>`<li><span class=mut>${e(x.time)}</span> ${e(x.msg)}</li>`).join('')||'<li class=mut>Nothing yet</li>';}
function connect(){const es=new EventSource('/stream');es.onmessage=m=>draw(JSON.parse(m.data));es.onerror=()=>{$('#feed').className='chip DISCONNECTED';$('#feed').textContent='● screen lost connection to VISION LIVE - retrying';}}
$('#kill').onclick=async()=>{const k=$('#kill').className!=='g';if(!k&&prompt('Type RESUME to switch the kill switch off')!=='RESUME')return;await fetch(k?'/kill':'/resume',{method:'POST',headers:{'X-Vision':'1'}})};
$('#codebtn').onclick=async()=>{const r=await fetch('/shoonya/code',{method:'POST',headers:{'X-Vision':'1'},body:$('#code').value});const j=await r.json();$('#codemsg').textContent=j.msg};
function drawOpt(O){if(!O){$('#opt').innerHTML='<p class=mut>Not loaded yet.</p>';return}const s=O.signal||{},L=O.legs,v=O.live,o=O.open;
 $('#opt').innerHTML=`<p><b>${s.action==='BUY'?e(s.name):'No options trade today'}</b> ${s.action==='BUY'?`<span class=mut>(${e((s.why||[]).join(' · '))})</span>`:''} · mode <b>${e(O.mode)}</b></p>`+
 (L?`<table><tr><th>Leg</th><th>Contract</th><th class=num>Live price</th></tr><tr><td class=up>BUY</td><td>${e(L.long.tsym)}</td><td class=num>${f2(L.long.ltp)}</td></tr><tr><td class=dn>SELL</td><td>${e(L.short.tsym)}</td><td class=num>${f2(L.short.ltp)}</td></tr></table>
  <p>Spread cost now: <b>${v?f2(v.open_debit):'—'}</b> · value if closed now: <b>${v?f2(v.close_value):'—'}</b> · width ${e(s.width??'—')}</p>`:'<p class=mut>Waiting for the two contracts / live prices (login to Shoonya first).</p>')+
 (o?`<p>OPEN: ${e(o.name)} ×${o.qty} · paid ${f2(o.debit)} · P&L now <b class="${O.open_pnl>=0?'up':'dn'}">₹${f2(O.open_pnl)}</b></p>`:'')+
 `<p class=mut>Closed options trades: ${O.record.closed} (need ${O.record.need} with profit factor ≥ 1.2 before REAL) · ${(O.trades||[]).slice(-3).map(t=>e(t.name)+' ₹'+f2(t.pnl)).join(' · ')}</p>`+
 (O.events||[]).slice(-4).reverse().map(x=>`<div class=mut style="font-size:12px">${e(x.time)} ${e(x.msg)}</div>`).join('')}
function drawDiag(H){if(!H)return;const c=(ok,t)=>`<b class=${ok?'up':'dn'}>${e(t)}</b>`;
 $('#diag').innerHTML=`<table><tr><td>Shoonya login</td><td>${c(H.auth==='LOGGED IN',H.auth+(H.auth_reason?' - '+H.auth_reason:''))}${H.auth_attempts>1?' <span class=mut>('+H.auth_attempts+' tries, last '+e(H.auth_failed_at)+')</span>':''}</td></tr>
 <tr><td>This computer's internet address</td><td>${c(!!H.public_ip,H.public_ip||H.public_ip_status)} <span class=mut>checked ${e(H.public_ip_checked||'—')}</span></td></tr>
 <tr><td>Address Shoonya knows</td><td>${e(H.registered_ip||'unknown')} <span class=mut>${e(H.registered_ip_source||'')}</span> ${H.ip_status==='UNKNOWN'?'':c(H.ip_status==='MATCH',H.ip_status)}</td></tr>
 <tr><td>Live prices</td><td>${c(H.market_data==='LIVE',H.market_data)} <span class=mut>${H.symbols_live}/${H.symbols_requested} symbols ticking</span></td></tr>
 <tr><td>Paper engine · Real orders</td><td><b class=up>${e(H.paper_engine)}</b> · ${c(H.real_orders==='BLOCKED',H.real_orders)}</td></tr></table>${H.auth_detail?`<p class=dn style="font-size:13px">${e(H.auth_detail)}</p>`:''}`}
const _draw=draw;draw=S=>{_draw(S);drawOpt(S.options);drawDiag(S.broker_health)};
connect();
</script></body></html>"""


def state_json(app):
    snap = app.book.snapshot()
    order = ["NIFTY", "BANKNIFTY"] + [s for s in app.watch if s not in ("NIFTY", "BANKNIFTY")]
    quotes = []
    for s in order:
        q = snap.get(s, {})
        t = q.get("t")
        quotes.append({"sym": s, "p": q.get("p"), "pc": q.get("pc"), "o": q.get("o"), "h": q.get("h"), "l": q.get("l"), "vwap": q.get("vwap"),
                       "bid": q.get("bid"), "ask": q.get("ask"), "v": q.get("v"), "src": q.get("src"),
                       "time": dt.datetime.fromtimestamp(t, IST).strftime("%H:%M:%S") if t else None,
                       "age": round(time.time() - t, 1) if t else None})
    pos = [{"sym": s, "qty": p["qty"], "avg": p["avg"], "stop": p["stop"], "target": p.get("target"), "ltp": snap.get(s, {}).get("p"),
            "pnl": ((snap.get(s, {}).get("p") or p["avg"]) - p["avg"]) * p["qty"]} for s, p in app.paper.s["pos"].items()]
    return {"time": now().strftime("%H:%M:%S"), "feed": app.feed.health() if app.feed else {"state": "DISCONNECTED", "last_error": "no credentials"},
            "mode": app.trader.mode(), "mode_requested": app.cfg.get("MODE", "PAPER").upper(), "market": app.market,
            "killed": app.safety.killed(), "quotes": quotes, "positions": pos,
            "account": {"equity": app.paper.equity(app.book), "cash": app.paper.s["cash"], "closed": len(app.paper.s["trades"]), "today": app.paper.realized_today()},
            "real_blockers": app.safety.real_blockers(app.paper), "gate": app.paper.record(), "signals": app.trader.signals,
            "events": app.trader.events[-60:], "rejected": app.book.rejected, "options": app.opt.state(), "broker_health": broker_health(app)}


def broker_health(app):
    """One honest status object: is the login OK, is my internet address right, are prices really live, are real orders blocked?"""
    cfg, a = app.cfg, app.sh_auth
    feed = app.feed.health() if app.feed else {"state": "NOT STARTED"}
    snap = app.book.snapshot()
    fresh = sum(1 for q in snap.values() if q.get("recv") and time.time() - q["recv"] < 60)
    ip = getattr(app, "ipinfo", None) or {}
    reg = registered_ip(cfg)
    ipst = "UNKNOWN" if not (ip.get("ip") and reg) else ("MATCH" if ip["ip"] == reg else "MISMATCH")
    live = feed.get("state") == "LIVE" and fresh > 0
    dly = getattr(app, "delayed", None)
    delayed_on = bool(dly and dly.last_ok and not live)
    return {"broker": app.broker, "auth_mode": "OAuth (Shoonya login page)" if app.broker == "SHOONYA" else "API key + TOTP",
            "client_id": "configured" if cfg.get("SHOONYA_CLIENT_ID") else "MISSING",
            "secret": "configured" if cfg.get("SHOONYA_SECRET") else "MISSING",
            "redirect_uri": "http://127.0.0.1:8765/shoonya/callback",
            "auth": a.status() if a else "n/a", "auth_reason": getattr(a, "fail_reason", None), "auth_detail": getattr(a, "fail_msg", None),
            "auth_failed_at": getattr(a, "fail_at", None), "auth_attempts": getattr(a, "fail_count", 0),
            "token": "present (today)" if a and a.session else "absent",
            "public_ip": ip.get("ip"), "public_ip_status": ip.get("status", "NOT CHECKED"), "public_ip_checked": ip.get("checked"),
            "registered_ip": reg, "registered_ip_source": "settings.env" if cfg.get("SHOONYA_REGISTERED_IP") else ("last accepted login" if reg else None),
            "ip_status": ipst, "market_data": "LIVE" if live else ("DELAYED ~15 min (portal, last pull " + dly.last_ok + ") - Shoonya " + str(feed.get("state")) if delayed_on else "NOT LIVE - " + str(feed.get("state"))),
            "last_tick_age_s": feed.get("last_tick_age_s"), "symbols_requested": len(app.tokens), "symbols_live": fresh,
            "paper_engine": "RUNNING", "mode": app.trader.mode(),
            "real_orders": "BLOCKED" if app.trader.mode() != "REAL" else "ALLOWED (limits apply)"}


class QuietServer(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        # the browser closing a page or giving up on a slow answer is normal - no scary tracebacks on screen
        if isinstance(sys.exc_info()[1], (ConnectionError, TimeoutError)):
            return
        super().handle_error(request, client_address)


PORTAL_ORIGIN = "https://16vitaws.github.io"
LOCAL_ORIGINS = ("http://127.0.0.1:8765", "http://localhost:8765")


def make_handler(app):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _cors(self):
            # only the 16VITAWS portal may read the state or press STOP/RESUME from the browser
            if self.headers.get("Origin") == PORTAL_ORIGIN:
                self.send_header("Access-Control-Allow-Origin", PORTAL_ORIGIN)
                self.send_header("Vary", "Origin")

        def do_OPTIONS(self):
            if not self._local() or self.headers.get("Origin") != PORTAL_ORIGIN or self.path not in ("/api/state", "/kill", "/resume"):
                self.send_error(403); return
            self.send_response(204); self._cors()
            self.send_header("Access-Control-Allow-Methods", "GET, POST")
            self.send_header("Access-Control-Allow-Headers", "X-Vision")
            self.send_header("Access-Control-Allow-Private-Network", "true")
            self.send_header("Access-Control-Max-Age", "600")
            self.end_headers()

        def _local(self):
            return self.client_address[0] in ("127.0.0.1", "::1")

        def do_GET(self):
            if not self._local():
                self.send_error(403); return
            if self.path == "/":
                b = PAGE.encode(); self.send_response(200); self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)
            elif self.path == "/shoonya/login" and app.sh_auth:
                self.send_response(302); self.send_header("Location", app.sh_auth.login_url()); self.end_headers()
            elif self.path.startswith("/shoonya/callback") and app.sh_auth:
                ok, msg = app.shoonya_code(self.path)
                b = (f"<html><body style='font:18px system-ui;background:#0B1220;color:#E8EEF8;padding:40px'><h2>{'Logged in to Shoonya' if ok else 'Login failed'}</h2>"
                     f"<p>{msg}</p><p><a style='color:#9FD0FF' href='/'>Back to VISION LIVE</a></p></body></html>").encode()
                self.send_response(200); self.send_header("Content-Type", "text/html; charset=utf-8"); self.end_headers(); self.wfile.write(b)
            elif self.path == "/api/state":
                b = json.dumps(state_json(app), default=str).encode(); self.send_response(200); self.send_header("Content-Type", "application/json"); self._cors()
                self.end_headers(); self.wfile.write(b)
            elif self.path == "/stream":
                self.send_response(200); self.send_header("Content-Type", "text/event-stream"); self.send_header("Cache-Control", "no-cache"); self.end_headers()
                try:
                    while True:
                        self.wfile.write(("data: " + json.dumps(state_json(app), default=str) + "\n\n").encode()); self.wfile.flush()
                        time.sleep(1)                                               # the screen updates every second
                except (BrokenPipeError, ConnectionResetError):
                    return
            else:
                self.send_error(404)

        def do_POST(self):
            # local-only + custom header: a web page elsewhere cannot trigger these (CSRF protection)
            if not self._local() or self.headers.get("X-Vision") != "1":
                self.send_error(403); return
            origin = self.headers.get("Origin")
            if origin and origin not in LOCAL_ORIGINS and not (origin == PORTAL_ORIGIN and self.path in ("/kill", "/resume")):
                self.send_error(403); return                # the portal can only STOP / RESUME, nothing else
            if self.path == "/kill":
                open(KILL_FILE, "w").write(now().isoformat()); app.trader.log("kill", "KILL SWITCH ON - no new orders")
            elif self.path == "/shoonya/code" and app.sh_auth:
                n = int(self.headers.get("Content-Length") or 0)
                ok, msg = app.shoonya_code(self.rfile.read(min(n, 4000)).decode("utf-8", "ignore"))
                b = json.dumps({"ok": ok, "msg": msg}).encode()
                self.send_response(200); self.send_header("Content-Type", "application/json"); self.end_headers(); self.wfile.write(b); return
            elif self.path == "/quit":                      # a newer copy is starting: hand over (local screen only)
                app.trader.log("start", "a new copy of 16VITAWS LIVE is starting - this one closes")
                self.send_response(204); self.end_headers()
                threading.Thread(target=lambda: (time.sleep(0.5), os._exit(0)), daemon=True).start(); return
            elif self.path == "/resume":
                if os.path.exists(KILL_FILE):
                    os.remove(KILL_FILE)
                app.trader.log("kill", "kill switch off")
            self.send_response(204); self._cors(); self.end_headers()
    return H


# ------------------------------------------------------------------ app
class App:
    def __init__(self, cfg):
        import requests
        self.cfg, self.req = cfg, requests
        self.site = (cfg.get("VISION_SITE") or "https://16vitaws.github.io/market-scanner").rstrip("/")
        self.book, self.paper, self.safety = Book(), Paper(cfg.get("PAPER_CAPITAL") or 100000), Safety(cfg)
        self.market, self.feed = "…", None
        self.broker = (cfg.get("BROKER") or "SHOONYA").upper()
        self.sh_auth = ShoonyaAuth(cfg) if self.broker == "SHOONYA" else None
        self.watch = []
        self.tokens = {}
        self.trader = Trader(cfg, self.book, self.paper, self.safety, {}, None, self.notify)
        self.opt = OptionsTrader(cfg, self.book, self.safety, self.notify)
        self.opt_loaded = None
        self.ipinfo = None
        self.delayed = None

    def refresh_ip(self, force=False):
        try:
            self.ipinfo = check_public_ip(self.req, max_age=0 if force else 600)
        except Exception:  # noqa
            pass

    def _ip_loop(self):
        while True:                                                     # every 10 minutes - never a continuous poll
            self.refresh_ip(force=True); time.sleep(600)

    def shoonya_code(self, text):
        try:
            self.sh_auth.exchange(text)
            if self.feed:
                self.feed.login_event.set()
            self.trader.log("login", "Shoonya login OK - live prices starting")
            return True, "Live prices are starting. You can close this tab."
        except Exception as e:  # noqa
            m = scrub(e, self.cfg)
            self.refresh_ip(force=True)
            n = self.sh_auth.fail_count or 1
            prev = next((x for x in reversed(self.trader.events) if x.get("kind") == "login"), None)
            if prev and prev.get("base") == m and n > 1:                    # same error again: one line with a counter, not 100 lines
                prev.update(time=now().strftime("%H:%M:%S"), msg=f"Shoonya login failed ({n} times, last {now().strftime('%H:%M')}): {m}")
            else:
                self.trader.log("login", f"Shoonya login failed: {m}", base=m)
            return False, m

    def check_ready(self):
        """Tell the user (phone + screen) the moment a paper track record earns the right to real money - once.
        This is the 'good time' signal: proven on live paper trades, not a promise of profit."""
        regime = (self.trader.signals or {}).get("regime") or "?"
        for key, name, rec in (("stocks", "Stocks algorithm", self.paper.record()), ("options", "NIFTY options algorithm", self.opt.record())):
            flag = os.path.join(HOME, f"ready_{key}.flag")
            if rec.get("passed") and not os.path.exists(flag):
                open(flag, "w").write(now().isoformat(timespec="seconds"))
                if key == "stocks":
                    detail = "; ".join(f"{c['check']}: {c['value']}" for c in rec.get("checks", []))
                else:
                    detail = f"{rec.get('closed')} closed trades, profit factor {rec.get('pf')}"
                msg = (f"{name} passed its paper test ({detail}). Market regime today: {regime}. If you want, you can now switch it to "
                       "real money in settings.env (CONSENT, STATIC_IP_REGISTERED, MODE=REAL). Start small. Past paper results are not a "
                       "guarantee - real trading can still lose money.")
                self.trader.log("ready", "READY FOR REAL MONEY: " + msg)
                self.notify("16VITAWS: ready for real money", msg)
            elif not rec.get("passed") and os.path.exists(flag):
                os.remove(flag)                                           # record fell below the bar: warn again next time it passes
                self.trader.log("ready", f"{name}: paper record fell below the bar again - stay on paper / reduce real size.")
                self.notify("16VITAWS: paper record weakened", f"{name} no longer passes its paper test. Consider stopping real trading.")

    def notify(self, title, msg):
        try:
            self.req.post(f"https://ntfy.sh/{self.cfg.get('NTFY_TOPIC') or 'vision-ai-16vitaws-k7q2m9x4'}", data=msg.encode(),
                          headers={"Title": title, "Priority": "5", "Tags": "chart_with_upwards_trend"}, timeout=10)
        except Exception:  # noqa
            pass

    def fetch(self, name):
        """Portal data file. The website copy can be missing for a while after a code update, so fall back to the
        same file on the gh-pages branch (identical data) - the algorithm never runs without today's signals."""
        try:
            r = self.req.get(f"{self.site}/api/{name}.json?t={int(time.time())}", timeout=20)
            r.raise_for_status()
            return r.json()
        except Exception:  # noqa
            r = self.req.get(f"{RAW_SITE}api/{name}.json?t={int(time.time()) // 60}", timeout=20)
            r.raise_for_status()
            return r.json()

    def refresh_portal(self):
        try:
            sig = self.fetch("signals"); self.trader.set_signals(sig)
        except Exception as e:  # noqa
            self.trader.log("warn", f"could not read portal signals: {e}")
        try:
            cal = self.fetch("calendar"); self.market = market_state((cal.get("markets") or {}).get("NSE"))
        except Exception:  # noqa
            self.market = market_state()
        try:
            self.safety.site_kill = bool(self.fetch("live_control").get("kill"))
        except Exception:  # noqa
            pass
        self.load_options()

    def load_options(self):
        """Today's options signal -> its two real contracts on Shoonya -> live prices for both legs."""
        try:
            sig = (self.fetch("options_model") or {}).get("signal") or {}
        except Exception as e:  # noqa
            self.opt.log("warn", f"could not read the portal's options signal: {e}"); return
        legs = None
        if self.broker == "SHOONYA" and sig.get("action") == "BUY" and sig.get("name") != self.opt_loaded:
            try:
                legs = sh_option_legs(self.req, sig); self.opt_loaded = sig["name"]
                self.opt.log("signal", f"today's options signal: {sig['name']} - legs {legs['long']['tsym']} / {legs['short']['tsym']}")
            except Exception as e:  # noqa
                self.opt.log("warn", f"{scrub(e, self.cfg)}"); self.opt_loaded = sig.get("name")
        self.opt.set_signal(sig, legs)
        L = self.opt.leg_syms()
        if L:
            more = {f"NFO|{self.opt.legs[r]['token']}": L[r] for r in ("long", "short")}
            if self.feed and hasattr(self.feed, "add_tokens"):
                self.feed.add_tokens(more)
            else:
                self.tokens.update(more)

    def build_watch(self):
        extra = [s.strip().upper() for s in (self.cfg.get("WATCH") or "").split(",") if s.strip()]
        sig = [b["symbol"] for b in self.trader.signals["buys"]] + list(self.trader.signals["sells"])
        self.watch = list(dict.fromkeys(["NIFTY", "BANKNIFTY"] + list(self.paper.s["pos"]) + sig + extra))[:200]
        self.tokens = {}
        if self.broker == "SHOONYA":
            for s, tok in sh_symbol_tokens(self.req, self.watch).items():
                if tok:
                    self.tokens[tok] = s
        else:
            rows = self.req.get(SCRIP_MASTER, timeout=90).json()
            eq = {r["symbol"][:-3]: r["token"] for r in rows if r.get("exch_seg") == "NSE" and str(r.get("symbol", "")).endswith("-EQ")}
            for s in self.watch:
                tok = INDEX_TOKENS.get(s) or eq.get(s)
                if tok:
                    self.tokens[tok] = s
        self.trader.tokens = {s: t for t, s in self.tokens.items()}
        missing = [s for s in self.watch if s not in self.trader.tokens]
        if missing:
            self.trader.log("warn", f"no {self.broker.title()} symbol code for: {', '.join(missing[:10])}")

    def run(self):
        os.makedirs(HOME, exist_ok=True)
        port = int(self.cfg.get("PORT") or 8765)
        srv = None
        for attempt in range(2):
            try:
                srv = QuietServer(("127.0.0.1", port), make_handler(self))
                break
            except OSError:
                if attempt == 0:                                          # an older copy is running: ask it to close, take over
                    try:
                        self.req.post(f"http://127.0.0.1:{port}/quit", headers={"X-Vision": "1", "Origin": f"http://127.0.0.1:{port}"}, timeout=5)
                    except Exception:  # noqa
                        pass
                    time.sleep(3)
        if srv is None:
            print(f"\n  16VITAWS LIVE is already running on this computer (port {port}). Nothing to do - open the 16VITAWS portal.\n", flush=True)
            return
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        threading.Thread(target=self._ip_loop, daemon=True).start()
        print(f"\n  VISION LIVE screen:  http://127.0.0.1:{port}\n", flush=True)
        self.refresh_portal()
        need = ("SHOONYA_UID", "SHOONYA_CLIENT_ID", "SHOONYA_SECRET") if self.broker == "SHOONYA" else \
            ("ANGEL_API_KEY", "ANGEL_CLIENT_ID", "ANGEL_PASSWORD", "ANGEL_TOTP_SECRET")
        missing = [k for k in need if not self.cfg.get(k)]
        if missing:
            self.trader.log("setup", f"Add your {self.broker.title()} details to {SETTINGS} (missing: {', '.join(missing)}), then restart. No prices until then.")
        else:
            try:
                self.build_watch()
            except Exception as e:  # noqa
                self.trader.log("error", f"could not load the symbol list: {scrub(e, self.cfg)} - restart to retry")
            self.load_options()                                            # add today's two option legs to the stream
            if self.broker == "SHOONYA":
                self.feed = ShoonyaFeed(self.cfg, self.book, self.tokens, self.sh_auth)
                if not self.sh_auth.session:
                    self.trader.log("login", "Click 'Login to Shoonya' at the top of this screen (once a day).")
            else:
                self.feed = Feed(self.cfg, self.book, self.tokens)
            self.trader.feed = self.feed; self.opt.feed = self.feed; self.feed.start()
            self.delayed = PortalDelayedFeed(self.req, self.book, lambda: list(self.watch),
                                             lambda: self.feed is None or self.feed.health().get("state") != "LIVE")
            self.delayed.start()
            if self.broker == "SHOONYA" and not self.sh_auth.session:
                self.trader.log("start", f"PAPER engine running ({self.trader.mode()} mode) on the portal's DELAYED prices (~15 min) until the "
                                         f"Shoonya login works; then it switches to live ticks for {len(self.tokens)} symbols. Real orders: BLOCKED.")
            else:
                self.trader.log("start", f"{self.broker}: connecting to {len(self.tokens)} symbols in {self.trader.mode()} mode")
        seen = {}
        last_portal = time.time()
        while True:
            if time.time() - last_portal > 60:
                self.refresh_portal(); last_portal = time.time()
                self.check_ready()
            for s, q in self.book.snapshot().items():                     # act on every symbol that ticked since last pass
                if q.get("recv") != seen.get(s):
                    seen[s] = q.get("recv")
                    try:
                        if s.startswith("OPT "):
                            self.opt.on_tick(self.market)
                        else:
                            self.trader.on_tick(s, self.market)
                    except Exception as e:  # noqa
                        self.trader.log("error", f"{s}: {e}")
            time.sleep(0.25)


def diagnose(cfg, http=None, out=print):
    """python vision_live.py --diagnose-shoonya : a plain report. Never prints a secret, token or login code."""
    import platform, socket
    http = http or __import__("requests")
    ok = lambda b: "OK" if b else "PROBLEM"
    out("16VITAWS LIVE - Shoonya connection check")
    out(f"  Python              {platform.python_version()} ({sys.executable})")
    out(f"  Shoonya library     none needed - this program talks to Shoonya's official REST/WebSocket API directly (OAuth)")
    for k in ("SHOONYA_UID", "SHOONYA_CLIENT_ID", "SHOONYA_SECRET"):
        out(f"  {k:<19} {'configured' if cfg.get(k) else 'MISSING - add it to ' + SETTINGS}")
    out(f"  Redirect URL        must be exactly http://127.0.0.1:8765/shoonya/callback on Shoonya's Api Key page")
    ip = check_public_ip(http, max_age=0)
    out(f"  Internet address    {ip.get('ip') or ip['status']}" + (f" (services disagree: {', '.join(ip.get('seen', []))})" if ip["status"] == "UNCERTAIN" else ""))
    reg = registered_ip(cfg)
    out(f"  Registered address  {reg or 'unknown - set SHOONYA_REGISTERED_IP in settings.env, or it is learned after the first good login'}")
    if ip.get("ip") and reg:
        out(f"  Address check       {'MATCH' if ip['ip'] == reg else 'MISMATCH - put ' + ip['ip'] + ' in Primary IP Address on Shoonya, click Update'}")
    try:
        socket.getaddrinfo("api.shoonya.com", 443); dns = True
    except OSError:
        dns = False
    out(f"  DNS api.shoonya.com {ok(dns)}")
    try:
        http.get("https://api.shoonya.com/NSE_symbols.txt.zip", timeout=15, stream=True); web = True
    except Exception:  # noqa
        web = False
    out(f"  HTTPS to Shoonya    {ok(web)}")
    a = ShoonyaAuth(cfg, http)
    out(f"  Today's login       {'YES - token saved for today' if a.session else 'NO - click Login to Shoonya in the portal'}")
    out(f"  Mode                {(cfg.get('MODE') or 'PAPER').upper()} (real orders need MODE=REAL plus every safety lock)")
    out("  Note: Shoonya requires a STATIC registered address. Mobile internet changes it, so logins fail until you update it.")


def main():
    cfg = load_settings()
    if "--diagnose-shoonya" in sys.argv:
        diagnose(cfg); return
    try:
        App(cfg).run()
    except KeyboardInterrupt:
        print("stopped")
    except Exception:  # noqa
        audit("crash", error=scrub(traceback.format_exc(), cfg)); raise


if __name__ == "__main__":
    main()
