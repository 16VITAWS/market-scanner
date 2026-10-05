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
import os, sys, json, time, math, threading, datetime as dt, traceback, hmac, base64, struct, hashlib, collections, statistics
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
# Real-time data safety (milliseconds). No new automatic entry unless the feed is LIVE and the price is this fresh.
DATA_LIVE_MS=1500
DATA_STALE_MS=5000
ENTRY_MAX_AGE_MS=2000
MAX_SPREAD_PCT=0.5
MIN_RR=1.0
# Paper fills: REALISTIC (buy at ask / sell at bid + slippage) | INSTANT (at last price)
# Brokerage + STT + exchange + SEBI + GST + stamp duty come from charges.json in this folder (edit it if rates change).
# PAPER_COST_PCT is only the old flat fallback.
PAPER_FILL_MODEL=REALISTIC
PAPER_SLIPPAGE_BPS=5
PAPER_COST_PCT=0.12
# yes = allow PAPER entries on the portal's ~15-min DELAYED prices when Shoonya is down (not recommended)
PAPER_ON_DELAYED=no
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
LIVE_SRC = ("shoonya-ws", "angelone-ws")                     # streaming exchange ticks (REST snapshots / portal copies are not "live")
DATA_DEFAULTS = {"DATA_LIVE_MS": "1500", "DATA_STALE_MS": "5000", "ENTRY_MAX_AGE_MS": "2000", "MAX_SPREAD_PCT": "0.5", "MIN_RR": "1.0",
                 "PAPER_FILL_MODEL": "REALISTIC", "PAPER_SLIPPAGE_BPS": "5", "PAPER_COST_PCT": "0.12", "PAPER_ON_DELAYED": "no",
                 "RECONCILE_S": "60", "CLOCK_MAX_OFFSET_MS": "2500"}


def cfgv(cfg, k):
    return (cfg or {}).get(k) or DATA_DEFAULTS[k]


def ist_ms(ts):
    """epoch seconds -> 'HH:MM:SS.mmm' in IST (all comparisons happen in epoch/UTC; IST only for display)."""
    return dt.datetime.fromtimestamp(ts, IST).strftime("%H:%M:%S.%f")[:-3] if ts else None


class Book:
    """THE single real-time market state. Every quote carries: exchange time (t), received time (recv), sequence (seq).
    Impossible ticks are rejected (previous kept), partial updates merge, duplicates are counted, latency is measured,
    1-minute candles are built from the same ticks the algorithm uses. Readers can wait for the next change (event-driven)."""

    def __init__(self):
        self.q, self.lock = {}, threading.Condition()
        self.ticks, self.version = 0, 0
        self.rejected = []
        self.stats = {"rejected": 0, "duplicates": 0, "out_of_order": 0, "future_ts": 0, "last_live_recv": None, "last_any_recv": None, "live_ticks": 0}
        self.lat = collections.deque(maxlen=600)                    # exchange -> this computer, ms (live stream only)
        self.offsets = collections.deque(maxlen=300)                # recv - exchange time, s (clock check)
        self.bars = {}                                              # sym -> {minute_epoch: [o, h, l, c, v_start, v_last]}

    def update(self, sym, d):
        p = d.get("p")
        recv = time.time()
        with self.lock:
            old = self.q.get(sym, {})
            if p is None and old.get("p") and any(d.get(k) is not None for k in ("bid", "ask", "v", "oi", "vwap")):
                p = old["p"]; d = {**d, "p": p}                         # partial tick (only bid/ask/volume/OI changed): merge
            reason = None
            if p is None or not isinstance(p, (int, float)) or not math.isfinite(p) or p <= 0:
                reason = "bad price"
            elif old.get("t") and d.get("t") and d["t"] < old["t"]:
                reason = "out of order"; self.stats["out_of_order"] += 1
            elif (d.get("pc") or old.get("pc")) and sym not in INDEX_NAMES and not sym.startswith("OPT ") and abs(p / (d.get("pc") or old.get("pc")) - 1) > 0.205:
                reason = "move beyond NSE price band"
            elif d.get("h") and d.get("l") and not (d["l"] * 0.9999 <= p <= d["h"] * 1.0001):
                reason = "outside day high/low"
            if reason:
                self.stats["rejected"] += 1
                self.rejected = (self.rejected + [{"sym": sym, "reason": reason, "at": now().strftime("%H:%M:%S")}])[-20:]
                return False
            src = d.get("src")
            if src in LIVE_SRC and old.get("src") == src and d.get("t") and d.get("t") == old.get("t") and all(
                    d.get(k) is None or d.get(k) == old.get(k) for k in ("p", "v", "bid", "ask", "oi")):
                self.stats["duplicates"] += 1                         # identical repeat of the last tick: counted, not re-processed
                return False
            if src in LIVE_SRC and d.get("t"):
                off = recv - d["t"]
                if off < -5:
                    self.stats["future_ts"] += 1                      # exchange time ahead of this clock: clock problem
                self.offsets.append(off)
                self.lat.append(max(0.0, off) * 1000)
            self.q[sym] = {**old, **{k: v for k, v in d.items() if v is not None}, "recv": recv, "seq": old.get("seq", 0) + 1}
            self.ticks += 1; self.version += 1
            self.stats["last_any_recv"] = recv
            if src in LIVE_SRC:
                self.stats["last_live_recv"] = recv; self.stats["live_ticks"] += 1
            if src != "portal-delayed":
                self._bar(sym, self.q[sym])
            self.lock.notify_all()
            return True

    def _bar(self, sym, q):
        ts = q.get("t") or q["recv"]
        m = int(ts // 60 * 60)
        B = self.bars.setdefault(sym, {})
        b = B.get(m)
        v = q.get("v")
        if b is None:
            prev = B[max(B)] if B else None
            B[m] = [q["p"], q["p"], q["p"], q["p"], prev[5] if prev and prev[5] is not None else v, v]
            if len(B) > 420:
                del B[min(B)]
        elif m >= max(B):
            b[1], b[2], b[3], b[5] = max(b[1], q["p"]), min(b[2], q["p"]), q["p"], v if v is not None else b[5]

    def candles(self, sym):
        with self.lock:
            B = dict(self.bars.get(sym) or {})
        return [[m, b[0], b[1], b[2], b[3], (b[5] - b[4]) if b[4] is not None and b[5] is not None and b[5] >= b[4] else None] for m, b in sorted(B.items())]

    def wait_change(self, version, timeout):
        """Block until a new tick arrives (or timeout) - the algorithm and the screen react to ticks, not to a timer."""
        with self.lock:
            if self.version == version:
                self.lock.wait(timeout)
            return self.version

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
                    self.state, self.connected_at = "RESYNCING", time.time()     # LIVE only after the first fresh tick
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
                        if self.state == "RESYNCING":
                            self.state = "LIVE"

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
            attempt += 1; self.reconnects += 1; self.last_reconnect = now().strftime("%H:%M:%S")
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
    for k in ("oi", "poi"):                                    # open interest / previous OI (options)
        if m.get(k) not in (None, ""):
            out[k] = int(float(m[k]))
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
                            # subscribed - but NOT live until fresh data actually arrives (an open socket is not proof)
                            self.state, self.connected_at, self.subscribed_at = "RESYNCING", time.time(), time.time()
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
                            if self.state == "RESYNCING":
                                self.state = "LIVE"                       # first fresh tick after (re)subscribe

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
            attempt += 1; self.reconnects += 1; self.last_reconnect = now().strftime("%H:%M:%S")
            self.state = "RECONNECTING"
            time.sleep(delay)
            if self.connected_at and time.time() - self.connected_at > 300:
                attempt = 0

    # ---- broker account (used for REAL-mode resync / reconciliation; read-only except cancel)
    def _acct(self):
        return {"uid": self.auth.session["uid"], "actid": self.auth.session["actid"]}

    def order_book(self):
        j = self.auth.post("OrderBook", {"uid": self.auth.session["uid"]})
        return j if isinstance(j, list) else []

    def positions(self):
        j = self.auth.post("PositionBook", self._acct())
        return j if isinstance(j, list) else []

    def limits(self):
        j = self.auth.post("Limits", self._acct())
        return j if isinstance(j, dict) and j.get("stat") == "Ok" else None

    def cancel_order(self, oid):
        j = self.auth.post("CancelOrder", {"uid": self.auth.session["uid"], "norenordno": str(oid)})
        return isinstance(j, dict) and j.get("stat") == "Ok"

    def tpseries(self, exch, token, st, et):
        """Today's real 1-minute candles from Shoonya (chart history before this program started)."""
        j = self.auth.post("TPSeries", {"uid": self.auth.session["uid"], "exch": exch, "token": str(token), "st": str(int(st)), "et": str(int(et)), "intrv": "1"})
        out = []
        for r in j if isinstance(j, list) else []:
            try:
                ts = int(r["ssboe"]) if r.get("ssboe") else int(dt.datetime.strptime(r["time"], "%d-%m-%Y %H:%M:%S").replace(tzinfo=IST).timestamp())
                out.append([ts, float(r["into"]), float(r["inth"]), float(r["intl"]), float(r["intc"]), int(float(r.get("intv") or 0))])
            except (KeyError, ValueError, TypeError):
                continue
        return sorted(out)

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
                "last_reconnect": getattr(self, "last_reconnect", None), "method": "WebSocket (touchline)",
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


# ------------------------------------------------------------------ AI option chain (CE/PE auto-selection)
CHAIN_DEFAULTS = {"OPT_CHAIN_UNDERLYINGS": "NIFTY,BANKNIFTY", "OPT_CHAIN_STRIKES": "5", "OPT_MIN_DTE": "1", "OPT_MIN_SCORE": "75",
                  "OPT_MAX_SPREAD_PCT": "3", "OPT_SL_PCT": "30", "OPT_RR": "2", "OPT_MAX_TRADES_DAY": "2", "OPT_LOTS": "1",
                  "OPT_W_LIQ": "25", "OPT_W_SPREAD": "15", "OPT_W_MONEY": "30", "OPT_W_MOM": "20", "OPT_W_OI": "10",
                  "OPT_NO_ENTRY_BEFORE": "09:30", "OPT_NO_ENTRY_AFTER": "14:30", "OPT_FORCE_EXIT": "15:15", "OPT_STALE_S": "5"}


def nfo_index_options(http, underlyings):
    """All live index-option contracts for the given underlyings from Shoonya's NFO master (symbols, tokens, lot sizes
    come ONLY from this list - never guessed). -> {underlying: {expiry(date): {strike(float): {'CE': {...}, 'PE': {...}}}}}"""
    import io, zipfile, csv
    r = http.get(SH_NFO, timeout=120)
    r.raise_for_status()
    z = zipfile.ZipFile(io.BytesIO(r.content))
    out = {u: {} for u in underlyings}
    for row in csv.DictReader(io.TextIOWrapper(z.open(z.namelist()[0]), encoding="utf-8")):
        g = {k.strip().lower(): (v or "").strip() for k, v in row.items() if k}
        u = g.get("symbol")
        if u not in out or not g.get("instrument", "").startswith("OPTIDX") or g.get("optiontype") not in ("CE", "PE"):
            continue
        e = _expiry_date(g.get("expiry"))
        try:
            k = float(g.get("strikeprice") or 0)
        except ValueError:
            continue
        if not e or k <= 0:
            continue
        out[u].setdefault(e, {}).setdefault(k, {})[g["optiontype"]] = {
            "token": g["token"], "tsym": g.get("tradingsymbol"), "lot": int(float(g.get("lotsize") or 0)), "tick": float(g.get("ticksize") or 0.05)}
    return out


def chain_window(master, underlying, spot, n=5, min_dte=1, today=None):
    """Nearest expiry at least `min_dte` days away; ATM = the listed strike nearest to spot (never an assumed interval);
    ATM +/- n listed strikes that have BOTH a CE and a PE."""
    today = today or now().date()
    exps = sorted(e for e in (master.get(underlying) or {}) if (e - today).days >= min_dte)
    if not exps or not spot:
        return None
    exp = exps[0]
    strikes = sorted(k for k, v in master[underlying][exp].items() if "CE" in v and "PE" in v)
    if not strikes:
        return None
    atm = min(strikes, key=lambda k: abs(k - spot))
    i = strikes.index(atm)
    win = strikes[max(0, i - n): i + n + 1]
    return {"underlying": underlying, "expiry": exp.isoformat(), "dte": (exp - today).days, "atm": atm, "spot_at_build": spot,
            "rows": [{"strike": k, "CE": master[underlying][exp][k]["CE"], "PE": master[underlying][exp][k]["PE"]} for k in win]}


def chain_sym(u, expiry, strike, ot):
    return f"OPT {u} {expiry} {strike:g} {ot}"


def _pct_rank(vals):
    xs = sorted(v for v in vals if v is not None)
    return (lambda v: None if v is None or not xs else 100.0 * sum(1 for x in xs if x <= v) / len(xs))


def rank_side(book, ch, side, cfg, now_ts=None, stale_s=30):
    """Score every CE (or PE) in the window 0-100 with configurable weights. Hard filters: live two-sided quote, fresh, spread."""
    now_ts = now_ts or time.time()
    w = {k: float(cfg.get(f"OPT_W_{k}") or CHAIN_DEFAULTS[f"OPT_W_{k}"]) for k in ("LIQ", "SPREAD", "MONEY", "MOM", "OI")}
    max_sp = float(cfg.get("OPT_MAX_SPREAD_PCT") or CHAIN_DEFAULTS["OPT_MAX_SPREAD_PCT"]) / 100
    rows = ch["rows"]
    ks = [r["strike"] for r in rows]
    ai = ks.index(ch["atm"])
    c = []
    for j, r in enumerate(rows):
        q = book.get(chain_sym(ch["underlying"], ch["expiry"], r["strike"], side))
        d = {"strike": r["strike"], "type": side, "tsym": r[side]["tsym"], "token": r[side]["token"], "lot": r[side]["lot"],
             "ltp": q.get("p"), "bid": q.get("bid"), "ask": q.get("ask"), "volume": q.get("v"), "oi": q.get("oi"),
             "oi_chg": (q["oi"] - q["poi"]) if q.get("oi") is not None and q.get("poi") is not None else None,
             "prem_chg_pct": ((q["p"] / q["pc"] - 1) * 100) if q.get("p") and q.get("pc") else None,
             "age_s": round(now_ts - q["recv"], 1) if q.get("recv") else None}
        steps = j - ai                                         # + above ATM
        d["itm_steps"] = -steps if side == "CE" else steps     # >0 in the money, <0 out of the money
        d["moneyness"] = "ATM" if steps == 0 else ("ITM" if d["itm_steps"] > 0 else "OTM")
        why = None
        if not d["ltp"] or not d["bid"] or not d["ask"]:
            why = "no live two-sided quote"
        elif d["age_s"] is None or d["age_s"] > stale_s:
            why = f"quote older than {stale_s} s"
        else:
            mid = (d["bid"] + d["ask"]) / 2
            d["spread_pct"] = round((d["ask"] - d["bid"]) / mid * 100, 2) if mid else None
            if d["spread_pct"] is None or d["spread_pct"] / 100 > max_sp:
                why = f"spread {d['spread_pct']}% wider than {max_sp * 100:g}%"
        d["rejected"] = why
        c.append(d)
    ok = [d for d in c if not d["rejected"]]
    rv, ro, rm, rc = (_pct_rank([d[k] for d in ok]) for k in ("volume", "oi", "prem_chg_pct", "oi_chg"))
    for d in ok:
        liq = [x for x in (rv(d["volume"]), ro(d["oi"])) if x is not None]
        s = {"liquidity": sum(liq) / len(liq) if liq else 0.0,
             "spread": max(0.0, 100 * (1 - d["spread_pct"] / (max_sp * 100))),
             "moneyness": {0: 100, 1: 100, -1: 80, 2: 70, -2: 50}.get(d["itm_steps"], 0),   # near-ATM / 1 ITM preferred (delta ~0.5-0.6)
             "momentum": rm(d["prem_chg_pct"]) if d["prem_chg_pct"] is not None else 50.0,
             "oi": rc(d["oi_chg"]) if d["oi_chg"] is not None else 50.0}
        d["scores"] = {k: round(v, 1) for k, v in s.items()}
        d["score"] = round((w["LIQ"] * s["liquidity"] + w["SPREAD"] * s["spread"] + w["MONEY"] * s["moneyness"] + w["MOM"] * s["momentum"]
                            + w["OI"] * s["oi"]) / sum(w.values()), 1)
    return sorted(c, key=lambda d: -(d.get("score") or -1))


def market_bias(q, regime):
    """Stage 1: direction of the UNDERLYING from live facts only. Each check is +1 bullish / -1 bearish."""
    pts, why = 0, []
    p, vw, pc, o = q.get("p"), q.get("vwap"), q.get("pc"), q.get("o")
    if p and vw:
        pts += (p > vw) - (p < vw); why.append(("above" if p > vw else "below" if p < vw else "at") + f" VWAP {vw:,.2f}")
    if p and pc:
        ch = (p / pc - 1) * 100
        if abs(ch) >= 0.3:
            pts += 1 if ch > 0 else -1
        why.append(f"day change {ch:+.2f}%")
    if p and o:
        pts += (p > o) - (p < o); why.append(("above" if p > o else "below" if p < o else "at") + f" today's open {o:,.2f}")
    if regime in ("BULL", "BEAR"):
        pts += 1 if regime == "BULL" else -1; why.append(f"portal market regime {regime}")
    bias = "BULLISH" if pts >= 2 else "BEARISH" if pts <= -2 else "SIDEWAYS"
    return {"bias": bias, "points": pts, "confidence": round(min(abs(pts), 4) / 4 * 100), "why": why}


def decide(book, ch, cfg, regime, market="OPEN", now_ts=None):
    """Stage 2 + final decision. Returns BUY_CE / BUY_PE / WATCH_CE / WATCH_PE / NO_TRADE with every reason. Never forced."""
    now_ts = now_ts or time.time()
    stale = float(cfg.get("OPT_STALE_S") or CHAIN_DEFAULTS["OPT_STALE_S"])
    min_score = float(cfg.get("OPT_MIN_SCORE") or CHAIN_DEFAULTS["OPT_MIN_SCORE"])
    u = ch["underlying"]
    q = book.get(u)
    out = {"underlying": u, "spot": q.get("p"), "expiry": ch["expiry"], "dte": ch["dte"], "atm": ch["atm"], "min_score": min_score}
    ce, pe = rank_side(book, ch, "CE", cfg, now_ts, stale), rank_side(book, ch, "PE", cfg, now_ts, stale)
    out["ce"], out["pe"] = ce, pe
    out["best_ce"] = next((d for d in ce if not d["rejected"]), None)
    out["best_pe"] = next((d for d in pe if not d["rejected"]), None)
    # option-chain intelligence (from live OI/volume only)
    oi_ce = {d["strike"]: d["oi"] for d in ce if d.get("oi")}
    oi_pe = {d["strike"]: d["oi"] for d in pe if d.get("oi")}
    out["intel"] = {"pcr": round(sum(oi_pe.values()) / sum(oi_ce.values()), 2) if oi_ce and oi_pe and sum(oi_ce.values()) else None,
                    "resistance": max(oi_ce, key=oi_ce.get) if oi_ce else None, "support": max(oi_pe, key=oi_pe.get) if oi_pe else None,
                    "top_ce_volume": max(ce, key=lambda d: d.get("volume") or 0)["strike"] if any(d.get("volume") for d in ce) else None,
                    "top_pe_volume": max(pe, key=lambda d: d.get("volume") or 0)["strike"] if any(d.get("volume") for d in pe) else None}
    age = (now_ts - q["recv"]) if q.get("recv") else None
    if market != "OPEN":
        out.update(decision="NO_TRADE", reasons=[f"market is {market}"], action="WAIT"); return out
    if not q.get("p") or age is None or age > stale:
        out.update(decision="NO_TRADE", paused=True, reasons=[f"TRADING PAUSED - DATA SAFETY: {u} price is {'missing' if age is None else f'{age:.0f} s old'}"],
                   action="WAIT"); return out
    b = market_bias(q, regime)
    out["bias"] = b
    if b["bias"] == "SIDEWAYS":
        out.update(decision="NO_TRADE", action="WAIT", reasons=[f"market direction unclear ({'; '.join(b['why'])})",
                   f"best CE score {out['best_ce']['score'] if out['best_ce'] else '-'}, best PE score {out['best_pe']['score'] if out['best_pe'] else '-'}"]); return out
    side = "CE" if b["bias"] == "BULLISH" else "PE"
    best = out["best_" + side.lower()]
    if not best:
        out.update(decision="NO_TRADE", action="WAIT", reasons=[f"{b['bias']} but no {side} passes the liquidity/spread/freshness filters"]); return out
    score = round(0.5 * b["confidence"] + 0.5 * best["score"], 1)
    sl_pct = float(cfg.get("OPT_SL_PCT") or CHAIN_DEFAULTS["OPT_SL_PCT"]) / 100
    rr = float(cfg.get("OPT_RR") or CHAIN_DEFAULTS["OPT_RR"])
    entry = best["ask"]
    plan = {"entry": entry, "stop": round(entry * (1 - sl_pct), 2), "target": round(entry * (1 + sl_pct * rr), 2), "rr": f"1 : {rr:g}"}
    reasons = [f"underlying {b['bias'].lower()}: " + "; ".join(b["why"]),
               f"{best['tsym']}: {best['moneyness']}, spread {best['spread_pct']}%, liquidity {best['scores']['liquidity']:.0f}/100, "
               f"premium momentum {best['scores']['momentum']:.0f}/100, OI change {best['scores']['oi']:.0f}/100"]
    warn = []
    if ch["dte"] <= 1:
        warn.append("expiry is very close - premium decays fast")
    if best["spread_pct"] > 1.5:
        warn.append(f"spread {best['spread_pct']}% is on the wide side")
    if (out["intel"]["resistance"] and side == "CE" and q["p"] < out["intel"]["resistance"] <= q["p"] * 1.005):
        warn.append(f"big call OI just above at {out['intel']['resistance']:g} (possible resistance)")
    if (out["intel"]["support"] and side == "PE" and q["p"] > out["intel"]["support"] >= q["p"] * 0.995):
        warn.append(f"big put OI just below at {out['intel']['support']:g} (possible support)")
    dec = ("BUY_" if score >= min_score else "WATCH_" if score >= min_score - 15 else "NO_TRADE") + ("" if score < min_score - 15 else side)
    if dec == "NO_TRADE":
        reasons.append(f"combined score {score} is below the minimum {min_score:g}")
    out.update(decision=dec, side=side, contract=best, score=score, confidence="HIGH" if score >= 85 else "MEDIUM" if score >= min_score else "LOW",
               plan=plan, reasons=reasons, warnings=warn, action="BUY" if dec.startswith("BUY") else "WAIT",
               sell_open_note="Selling options to OPEN (short) is blocked: margin/SPAN cannot be verified from here.")
    return out


class ChainTrader:
    """PAPER-ONLY single-leg trades on the AI chain decision (BUY_OPEN at the ask, SELL_EXIT at the bid, configured slippage). Never sends a
    broker order: it has no feed/broker reference at all. One position at a time, stop + target + trailing stop, daily trade limit,
    time exits. Before every entry the LATEST quote is re-checked (age + price move) - an old decision is never executed blindly."""
    STRATEGY = "AI Option Chain (CE/PE selection)"

    def __init__(self, cfg, book, notify, path=None, journal=None):
        self.cfg, self.book, self.notify = cfg, book, notify
        self.path = path or os.path.join(HOME, "options_chain_paper.json")
        self.journal = journal or Journal(os.path.join(os.path.dirname(self.path) or ".", "journal.jsonl"))
        try:
            self.s = json.load(open(self.path))
        except Exception:  # noqa
            self.s = {"open": None, "trades": [], "signals": {}}
        self.events = []
        self.last_dec = {}

    def c(self, k):
        return self.cfg.get(k) or CHAIN_DEFAULTS[k]

    def save(self):
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        tmp = self.path + ".tmp"; json.dump(self.s, open(tmp, "w"), indent=1); os.replace(tmp, self.path)

    def log(self, msg, type_="OPTIONS", **kw):
        self.events = (self.events + [{"time": now().strftime("%H:%M:%S"), "msg": msg}])[-50:]
        self.journal.add(type_, msg, strategy=self.STRATEGY, **kw)
        audit("chain_paper", msg=msg)

    def today_trades(self):
        d = now().date().isoformat()
        return [t for t in self.s["trades"] if t["entry_time"][:10] == d]

    def on_decision(self, dec):
        """Called with each fresh decision. Entries only on BUY_CE / BUY_PE inside the entry window."""
        t = now().time()
        hm = lambda k: dt.time(*map(int, self.c(k).split(":")))
        o = self.s["open"]
        if o:
            return self.manage(dec)
        if not dec or not dec.get("decision", "").startswith("BUY_") or dec.get("paused"):
            return
        k = dec["contract"]
        sig_id = f"{now().date()}|{k['tsym']}|{dec['decision']}"
        if sig_id in self.s["signals"]:
            return                                                # duplicate protection: one entry per signal per day
        first = self.last_dec.get(sig_id) is None
        self.last_dec[sig_id] = time.time()
        sym = chain_sym(dec["underlying"], dec["expiry"], k["strike"], k["type"])
        if first:
            self.log(f"SIGNAL {dec['decision']} {k['tsym']} @ ask {k['ask']:.2f} - score {dec['score']}", "SIGNAL_GENERATED", symbol=sym,
                     signal_id=sig_id, price=k["ask"], contrib=k.get("scores"), why=dec.get("reasons"))
        why_not = None
        if not (hm("OPT_NO_ENTRY_BEFORE") <= t <= hm("OPT_NO_ENTRY_AFTER")):
            why_not = f"outside the entry window {self.c('OPT_NO_ENTRY_BEFORE')}-{self.c('OPT_NO_ENTRY_AFTER')}"
        elif len(self.today_trades()) >= int(self.c("OPT_MAX_TRADES_DAY")):
            why_not = f"daily limit of {self.c('OPT_MAX_TRADES_DAY')} option trades reached"
        qty = int(self.c("OPT_LOTS")) * int(k["lot"] or 0)
        if not why_not and qty <= 0:
            why_not = "lot size unknown"
        if why_not:
            if first:
                self.log(f"{k['tsym']} NOT executed: {why_not}", "SIGNAL_REJECTED", symbol=sym, signal_id=sig_id)
            return
        T = {"signal": time.time(), "risk": time.time(), "order": time.time()}
        q = self.book.get(sym)                                    # LATEST quote, not the decision's copy
        stale = float(self.c("OPT_STALE_S"))
        age = time.time() - q["recv"] if q.get("recv") else None
        if age is None or age > stale or not q.get("ask"):
            self.log(f"{k['tsym']} signal invalidated: quote {'missing' if age is None else f'{age:.1f} s old'} (max {stale:g} s)", "SIGNAL_INVALIDATED", symbol=sym, signal_id=sig_id); return
        if abs(q["ask"] / k["ask"] - 1) > 0.02:
            self.log(f"{k['tsym']} signal invalidated: ask moved {k['ask']:.2f} -> {q['ask']:.2f} since the decision", "SIGNAL_INVALIDATED", symbol=sym, signal_id=sig_id); return
        T["submit"] = time.time()
        fill, model = fill_model(self.cfg, q, "BUY", q.get("p") or q["ask"])
        T["fill"] = time.time()
        sl_pct = float(self.c("OPT_SL_PCT")) / 100; rr = float(self.c("OPT_RR"))
        stop, target = round(fill * (1 - sl_pct), 2), round(fill * (1 + sl_pct * rr), 2)
        tid = f"O-{now().strftime('%Y%m%d-%H%M%S')}-{len(self.s['trades']) + 1:03d}"
        self.s["signals"][sig_id] = now().isoformat(timespec="seconds")
        self.s["open"] = {"id": sig_id, "trade_id": tid, "sym": sym, "tsym": k["tsym"],
                          "type": k["type"], "underlying": dec["underlying"], "qty": qty, "entry": fill, "stop": stop,
                          "target": target, "high": fill, "risk": round(fill - stop, 2), "score": dec["score"], "reasons": dec["reasons"],
                          "contrib": k.get("scores"), "bias": (dec.get("bias") or {}).get("bias"), "signal_price": k["ask"], "fill_model": model,
                          "slippage": round(fill - k["ask"], 2), "execution": "SIMULATED", "mode": "PAPER", "strategy": self.STRATEGY, "t": T,
                          "entry_time": now().isoformat(timespec="seconds")}
        self.save()
        J = lambda typ, msg, **kw: self.log(msg, typ, symbol=sym, trade_id=tid, signal_id=sig_id, **kw)
        J("RISK_APPROVED", f"{k['tsym']}: window, daily limit, fresh quote ({age * 1000:.0f} ms) - PASS")
        J("POSITION_SIZED", f"{qty} = {self.c('OPT_LOTS')} lot x {k['lot']}")
        J("ORDER_FILLED", f"SIMULATED fill BUY {qty} {k['tsym']} @ {fill:.2f} ({model})", price=fill, execution="SIMULATED", latency=timeline(T))
        J("POSITION_OPENED", f"PAPER BUY_OPEN {qty} {k['tsym']} @ {fill:.2f} - score {dec['score']}, SL {stop}, target {target}", price=fill, stop=stop, target=target)
        self.notify("16VITAWS paper option BUY", f"{dec['decision']} {k['tsym']} @ {fill:.2f}, SL {stop}, T {target} (paper)")

    def manage(self, dec=None):
        o = self.s["open"]
        q = self.book.get(o["sym"])
        bid, p = q.get("bid"), q.get("p")
        if not bid or not p:
            return
        o["high"] = max(o["high"], p)
        risk = o.get("risk") or (o["entry"] - min(o["stop"], o["entry"]))      # the ORIGINAL 1R, fixed at entry
        old = o["stop"]
        if risk > 0 and o["high"] >= o["entry"] + risk and o["stop"] < o["entry"]:
            o["stop"] = o["entry"]
            self.log(f"{o['tsym']}: +1R reached - stop moved to break-even {o['entry']:.2f} (was {old:.2f})", "SL_UPDATED", symbol=o["sym"],
                     trade_id=o.get("trade_id"), old=old, new=o["stop"]); old = o["stop"]
        if risk > 0 and o["high"] >= o["entry"] + 1.5 * risk:
            trail = round(o["high"] - risk, 2)
            if trail > o["stop"]:
                o["stop"] = trail                                 # stops only ever move up, never loosen
                self.log(f"{o['tsym']}: TRAILING SL {old:.2f} -> {trail:.2f} (1R below the high {o['high']:.2f})", "SL_UPDATED", symbol=o["sym"],
                         trade_id=o.get("trade_id"), old=old, new=trail)
        why = kind = None
        if bid <= o["stop"]:
            why, kind = f"stop {o['stop']:.2f} hit", ("TRAILING_STOP" if o["stop"] > o["entry"] - risk + 0.001 else "STOP_LOSS")
        elif bid >= o["target"]:
            why, kind = f"target {o['target']:.2f} hit", "TARGET"
        elif now().time() >= dt.time(*map(int, self.c("OPT_FORCE_EXIT").split(":"))):
            why, kind = "end-of-day exit", "END_OF_DAY"
        elif dec and dec.get("decision", "").startswith("BUY_") and dec.get("side") and dec["side"] != o["type"]:
            why, kind = f"signal reversed to {dec['decision']}", "STRATEGY_REVERSAL"
        if why:
            self.exit(bid, why, kind, p)
        else:
            self.save()

    def exit(self, px, why, kind="SYSTEM_EXIT", signal_px=None):
        o = self.s.pop("open"); self.s["open"] = None
        q = self.book.get(o["sym"])
        fill = round(px * (1 - float(cfgv(self.cfg, "PAPER_SLIPPAGE_BPS")) / 1e4), 2) if cfgv(self.cfg, "PAPER_FILL_MODEL").upper() != "INSTANT" else (q.get("p") or px)
        tc = trade_costs(getattr(self, "rules", DEFAULT_CHARGES), getattr(self, "broker", "SHOONYA"), "OPT", o["entry"], fill, o["qty"])
        gross = tc["gross"]
        charges = tc["charges"] if tc["charges"] is not None else sum(v for v in tc["components"].values() if v)
        t = {**o, "costs": {"buy": tc["buy"], "sell": tc["sell"], "status": tc["status"]}, "t": {**(o.get("t") or {}), "exit": time.time()}, "exit": fill, "exit_time": now().isoformat(timespec="seconds"), "why": why, "exit_type": kind, "exit_signal_price": signal_px or px,
             "gross": round(gross, 2), "charges": round(charges, 2), "pnl": round(gross - charges, 2)}
        self.s["trades"].append(t); self.save()
        if kind in ("STOP_LOSS", "TRAILING_STOP", "TARGET"):
            self.log(f"{o['tsym']}: {why}", kind, symbol=o["sym"], trade_id=o.get("trade_id"), price=px)
        self.log(f"PAPER SELL_EXIT {o['qty']} {o['tsym']} @ {fill:.2f} (bid) - {why} - net P&L {t['pnl']:+.2f}", "POSITION_CLOSED",
                 symbol=o["sym"], trade_id=o.get("trade_id"), price=fill, pnl=t["pnl"], exit_type=kind)

    def close_all(self, why="closed by you (manual)"):
        o = self.s.get("open")
        if not o:
            return 0
        q = self.book.get(o["sym"])
        self.exit(q.get("bid") or q.get("p") or o["entry"], why, "MANUAL_EXIT"); return 1

    def record(self):
        tr = self.s["trades"]
        wins = sum(t["pnl"] for t in tr if t["pnl"] > 0); losses = -sum(t["pnl"] for t in tr if t["pnl"] < 0)
        return {"closed": len(tr), "net": round(sum(t["pnl"] for t in tr), 2), "win_rate": round(100 * sum(1 for t in tr if t["pnl"] > 0) / len(tr)) if tr else None,
                "pf": round(wins / losses, 2) if losses else None, "max_loss": min((t["pnl"] for t in tr), default=None)}

    def state(self):
        o = self.s["open"]
        if o:
            q = self.book.get(o["sym"])
            o = {**o, "ltp": q.get("p"), "bid": q.get("bid"), "pnl": round(((q.get("bid") or o["entry"]) - o["entry"]) * o["qty"], 2),
                 "age_ms": round((time.time() - q["recv"]) * 1000) if q.get("recv") else None}
        return {"mode": "PAPER", "open": o, "trades": self.s["trades"][-20:], "record": self.record(), "events": self.events[-20:],
                "live_note": "Single-leg LIVE option orders are not enabled. This engine is paper-only by design."}


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
# ------------------------------------------------------------------ one event log for everything the algorithm does
KIND_MAP = {"paper_buy": "POSITION_OPENED", "paper_sell": "POSITION_CLOSED", "blocked": "RISK_REJECTED", "skip": "SIGNAL_REJECTED",
            "real_blocked": "RISK_REJECTED", "real_order": "ORDER_SUBMITTED", "real_error": "BROKER_ERROR", "kill": "KILL_SWITCH",
            "login": "BROKER", "warn": "WARNING", "error": "ERROR", "start": "SYSTEM", "setup": "SYSTEM", "ready": "READY"}


class Journal:
    """Append-only event log (signals, rejections, risk, orders, fills, positions, data problems). Every event: timestamp (epoch ms +
    IST text), type, mode PAPER/LIVE, symbol, strategy, trade id, message, source. Saved to disk, so a restart / browser refresh
    shows the same day. The screen only READS this - it can never place an order."""

    def __init__(self, path=None, keep=800):
        self.path = path or os.path.join(HOME, "journal.jsonl")
        self.keep, self.lock, self.ev = keep, threading.Lock(), []
        day = now().date().isoformat()
        try:
            with open(self.path, encoding="utf-8") as f:
                for line in f:
                    try:
                        e = json.loads(line)
                    except ValueError:
                        continue
                    if e.get("date") == day:
                        self.ev.append(e)
        except OSError:
            pass
        self.ev = self.ev[-keep:]
        self.n = max([e.get("seq", 0) for e in self.ev] or [0])

    def add(self, type_, msg, mode="PAPER", symbol=None, strategy=None, trade_id=None, source="engine", **data):
        t, n = time.time(), now()
        with self.lock:
            self.n += 1
            e = {"seq": self.n, "ts": int(t * 1000), "date": n.date().isoformat(), "time": n.strftime("%H:%M:%S"),
                 "time_ms": n.strftime("%H:%M:%S") + f".{int(t * 1000) % 1000:03d}", "type": type_, "mode": mode, "symbol": symbol,
                 "strategy": strategy, "trade_id": trade_id, "msg": msg, "source": source, **data}
            self.ev.append(e)
            if len(self.ev) > self.keep:
                del self.ev[:len(self.ev) - self.keep]
            try:
                os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
                with open(self.path, "a", encoding="utf-8") as f:
                    f.write(json.dumps(e, default=str) + "\n")
            except OSError:
                pass
        return e

    def tail(self, n=200, mode=None):
        with self.lock:
            ev = [e for e in self.ev if mode is None or e.get("mode") == mode]
        return ev[-n:]


def ms_between(a, b):
    return round((b - a) * 1000, 1) if a and b else None


def timeline(t):
    """Actual timestamps of one trade -> the stage-to-stage latencies. Missing stage = None (shown as N/A), never invented."""
    keys = ["signal", "risk", "order", "submit", "ack", "fill"]
    out = {"at": {k: ist_ms(t.get(k)) for k in keys}}
    out["signal_to_risk_ms"] = ms_between(t.get("signal"), t.get("risk"))
    out["risk_to_order_ms"] = ms_between(t.get("risk"), t.get("order"))
    out["order_to_submit_ms"] = ms_between(t.get("order"), t.get("submit"))
    out["submit_to_ack_ms"] = ms_between(t.get("submit"), t.get("ack"))
    out["ack_to_fill_ms"] = ms_between(t.get("ack") or t.get("submit"), t.get("fill"))
    out["total_ms"] = ms_between(t.get("signal"), t.get("fill") or t.get("ack") or t.get("submit"))
    return out


def fill_model(cfg, q, side, last):
    """Paper execution model. REALISTIC: BUY pays the ask, SELL gets the bid (when a live two-sided quote exists), plus the
    configured slippage. INSTANT: the last price. Returns (price, description). Always SIMULATED - never a broker fill."""
    model = cfgv(cfg, "PAPER_FILL_MODEL").upper()
    if model == "INSTANT":
        return round(last, 2), "INSTANT (last traded price, no spread, no slippage)"
    bps = float(cfgv(cfg, "PAPER_SLIPPAGE_BPS"))
    base, what = last, "last price"
    if side == "BUY" and q.get("ask"):
        base, what = q["ask"], "ask"
    elif side == "SELL" and q.get("bid"):
        base, what = q["bid"], "bid"
    px = base * (1 + bps / 1e4) if side == "BUY" else base * (1 - bps / 1e4)
    return round(px, 2), f"REALISTIC ({what} {base:.2f} {'+' if side == 'BUY' else '-'} {bps:g} bps slippage)"


# ------------------------------------------------------------------ trade cost engine (brokerage + statutory charges)
# Rates are DATA, not code: written once to ~/vision_live/charges.json, which you can edit when a broker or the exchange changes
# its charges (each rule has an effective_from date). Results are labelled CALCULATED (from these rules) or ESTIMATED (open
# positions, exit not done yet) - never "ACTUAL": Shoonya's order API does not return the charges it debits; the contract note does.
DEFAULT_CHARGES = {
    "note": "Edit to match your contract note. Percentages are % of turnover. Statutory = same for every broker.",
    "checked": "2026-10-05",
    "sources": ["https://zerodha.com/charges/ (statutory rates list)", "https://cleartax.in/s/securities-transaction-tax-stt (STT from 1 Apr 2026)",
                "https://comparesharebrokers.com/review/finvasia (Shoonya brokerage since 2 Dec 2024)"],
    "statutory": [
        {"segment": "EQ_DELIVERY", "effective_from": "2026-04-01", "stt_buy_pct": 0.1, "stt_sell_pct": 0.1, "txn_pct": 0.00307,
         "sebi_per_crore": 10, "stamp_buy_pct": 0.015, "gst_pct": 18},
        {"segment": "EQ_INTRADAY", "effective_from": "2026-04-01", "stt_buy_pct": 0, "stt_sell_pct": 0.025, "txn_pct": 0.00307,
         "sebi_per_crore": 10, "stamp_buy_pct": 0.003, "gst_pct": 18},
        {"segment": "OPT", "effective_from": "2026-04-01", "stt_buy_pct": 0, "stt_sell_pct": 0.15, "txn_pct": 0.03553,
         "sebi_per_crore": 10, "stamp_buy_pct": 0.003, "gst_pct": 18, "basis": "premium"}],
    "brokers": {
        "SHOONYA": [
            {"segment": "EQ_DELIVERY", "effective_from": "2024-12-02", "per_order": 0, "dp_per_sell": 9},
            {"segment": "EQ_INTRADAY", "effective_from": "2024-12-02", "per_order": 5, "pct": 0.03, "rule": "lower"},
            {"segment": "OPT", "effective_from": "2024-12-02", "per_order": 5}],
        "ANGEL": []}}
CHARGES_FILE_NAME = "charges.json"


def load_charges(home=None):
    path = os.path.join(home or HOME, CHARGES_FILE_NAME)
    try:
        return json.load(open(path))
    except Exception:  # noqa
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            json.dump(DEFAULT_CHARGES, open(path, "w"), indent=1)
        except OSError:
            pass
        return DEFAULT_CHARGES


def _rule(rows, segment, day):
    ok = [r for r in rows or [] if r.get("segment") == segment and str(r.get("effective_from", "")) <= day
          and (not r.get("effective_to") or day <= str(r["effective_to"]))]
    return max(ok, key=lambda r: r["effective_from"]) if ok else None


def side_costs(rules, broker, segment, side, price, qty, day=None, orders=1):
    """Charges for ONE side of a trade. Missing broker rule -> brokerage None (DATA UNAVAILABLE), never a guess."""
    day = day or now().date().isoformat()
    st = _rule(rules.get("statutory"), segment, day)
    br = _rule((rules.get("brokers") or {}).get((broker or "").upper()), segment, day)
    turnover = round(price * qty, 2)
    if not st:
        return {"side": side, "price": price, "qty": qty, "turnover": turnover, "status": "DATA UNAVAILABLE", "total": None,
                "why": f"no statutory rule for {segment} on {day}"}
    if br is None:
        brokerage = None
    else:
        b = float(br.get("per_order") or 0) * orders
        if br.get("pct") is not None:
            p = turnover * float(br["pct"]) / 100
            b = min(b, p) if br.get("rule") == "lower" else max(b, p)
        brokerage = round(b, 2)
    stt = turnover * float(st.get("stt_buy_pct" if side == "BUY" else "stt_sell_pct") or 0) / 100
    txn = turnover * float(st.get("txn_pct") or 0) / 100
    sebi = turnover * float(st.get("sebi_per_crore") or 0) / 1e7
    stamp = turnover * float(st.get("stamp_buy_pct") or 0) / 100 if side == "BUY" else 0.0
    other = float((br or {}).get("dp_per_sell") or 0) * (1 + float(st.get("gst_pct") or 0) / 100) if side == "SELL" and br else 0.0
    gst = ((brokerage or 0) + txn + sebi) * float(st.get("gst_pct") or 0) / 100
    c = {"side": side, "price": price, "qty": qty, "turnover": turnover, "brokerage": brokerage, "stt": round(stt, 2), "exchange": round(txn, 2),
         "sebi": round(sebi, 2), "gst": round(gst, 2), "stamp": round(stamp, 2), "other": round(other, 2), "other_note": "DP charge (delivery sell) incl. GST" if other else ""}
    parts = [c[k] for k in ("brokerage", "stt", "exchange", "sebi", "gst", "stamp", "other")]
    c["total"] = round(sum(x for x in parts if x is not None), 2)
    c["status"] = "CALCULATED" if brokerage is not None else "INCOMPLETE - brokerage DATA UNAVAILABLE for this broker"
    c["rule_dates"] = {"statutory": st["effective_from"], "broker": (br or {}).get("effective_from")}
    return c


def trade_costs(rules, broker, segment, buy_px, sell_px, qty, day=None, estimated=False):
    """Complete long trade: buy side + sell side, gross, total charges, net. No double counting: gross uses fill prices
    (slippage is already inside them, shown separately for information only)."""
    b = side_costs(rules, broker, segment, "BUY", buy_px, qty, day)
    s = side_costs(rules, broker, segment, "SELL", sell_px, qty, day)
    gross = round((sell_px - buy_px) * qty, 2)
    tot = None if b["total"] is None or s["total"] is None else round(b["total"] + s["total"], 2)
    comp = {k: round((b.get(k) or 0) + (s.get(k) or 0), 2) if b.get(k) is not None and s.get(k) is not None else None
            for k in ("brokerage", "stt", "exchange", "sebi", "gst", "stamp", "other")}
    st = "ESTIMATED" if estimated else ("CALCULATED" if "CALCULATED" == b["status"] == s["status"] else "INCOMPLETE")
    return {"buy": b, "sell": s, "gross": gross, "charges": tot, "components": comp, "net": None if tot is None else round(gross - tot, 2),
            "status": st, "segment": segment, "broker": broker,
            "label": {"ESTIMATED": "ESTIMATED (exit not done yet)", "CALCULATED": "CALCULATED from configured rules (not broker-confirmed)"}.get(st, "INCOMPLETE")}


class Paper:
    def __init__(self, capital, path=None, cost_pct=0.12, costs=None):
        self.path = path or PAPER_FILE
        self.cost = float(cost_pct) / 100                                 # legacy flat estimate (only if no cost engine is given)
        # cost engine: (side, price, qty) -> itemised charges for ONE side (brokerage, STT, exchange, SEBI, GST, stamp, other)
        self.costs = costs or (lambda side, price, qty: side_costs(DEFAULT_CHARGES, "SHOONYA", "EQ_DELIVERY", side, price, qty))
        try:
            self.s = json.load(open(self.path))                        # the track record survives restarts
        except Exception:  # noqa
            self.s = {"cash": float(capital), "start": float(capital), "pos": {}, "trades": [], "fills": []}
        self.s.setdefault("done", {})

    def save(self):
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        tmp = self.path + ".tmp"
        json.dump(self.s, open(tmp, "w"), indent=1)
        os.replace(tmp, self.path)                      # atomic: a crash never leaves a half-written account

    def buy_cost(self, price, qty):
        c = self.costs("BUY", price, qty)
        return qty * price + (c.get("total") or 0), c

    def buy(self, sym, qty, price, stop, target, why, rec=None):
        cost, c = self.buy_cost(price, qty)
        if cost > self.s["cash"] or sym in self.s["pos"]:
            return None
        self.s["cash"] -= cost
        self.s["pos"][sym] = {"qty": qty, "avg": price, "stop": stop, "target": target, "at": now().isoformat(timespec="seconds"), "why": why,
                              "buy_costs": c, **({"rec": rec} if rec else {})}
        f = {"time": now().isoformat(timespec="seconds"), "side": "BUY", "sym": sym, "qty": qty, "price": price, "why": why,
             "trade_id": (rec or {}).get("trade_id"), "execution": "SIMULATED"}
        self.s["fills"].append(f); self.save()
        return f

    def sell(self, sym, price, why, exit_type=None, signal_price=None, t_fill=None):
        p = self.s["pos"].pop(sym, None)
        if not p:
            return None
        sc = self.costs("SELL", price, p["qty"])
        bc = p.get("buy_costs") or {"total": round(p["qty"] * p["avg"] * self.cost, 2), "status": "LEGACY flat estimate"}
        proceeds = p["qty"] * price - (sc.get("total") or 0)
        self.s["cash"] += proceeds
        gross = (price - p["avg"]) * p["qty"]
        charges = (bc.get("total") or 0) + (sc.get("total") or 0)
        pnl = gross - charges                                             # NET = GROSS - every charge, counted once
        rec = p.get("rec") or {}
        self.s["trades"].append({"sym": sym, "qty": p["qty"], "entry": p["avg"], "exit": price, "pnl": round(pnl, 2),
                                 "gross": round(gross, 2), "charges": round(charges, 2), "costs": {"buy": bc, "sell": sc},
                                 "opened": p["at"], "closed": now().isoformat(timespec="seconds"), "why": why,
                                 "exit_type": exit_type, "exit_signal_price": signal_price, "stop": p.get("stop"), "target": p.get("target"),
                                 **({"rec": {**rec, "t": {**rec.get("t", {}), "exit": t_fill}}} if rec else {})})
        f = {"time": now().isoformat(timespec="seconds"), "side": "SELL", "sym": sym, "qty": p["qty"], "price": price, "why": why, "pnl": round(pnl, 2),
             "trade_id": rec.get("trade_id"), "execution": "SIMULATED"}
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

    def paused(self):
        return os.path.exists(os.path.join(os.path.dirname(KILL_FILE), "PAUSED"))

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
    """Stocks algorithm on live ticks. Pipeline per signal: MARKET DATA -> SIGNAL -> VALIDATION (data freshness, risk, sizing, price)
    -> ORDER -> latest-price RE-CHECK -> FILL -> POSITION -> management -> EXIT -> realized P&L. Every step is a Journal event.
    PAPER and REAL share this exact pipeline; only the destination differs (paper book vs Shoonya LIMIT order)."""

    def __init__(self, cfg, book, paper, safety, tokens_by_sym, feed=None, notify=None, journal=None):
        self.cfg, self.book, self.paper, self.safety, self.tokens, self.feed = cfg, book, paper, safety, tokens_by_sym, feed
        self.notify = notify or (lambda t, m: None)
        self.journal = journal or Journal()
        self.signals = {"buys": [], "sells": [], "regime": None, "data_status": None, "strategy": {}, "blocked": []}
        self.watching, self.rej, self.seen_sig = {}, {}, set()
        self.data_check = lambda sym: []           # App: live-data checks (feed LIVE, quote age, resync, clock) - [{name, ok, detail}]
        self.real_check = lambda: []               # App: broker connected + positions reconciled (REAL only)
        self.last_scan = None
        self.real = {}

    @property
    def events(self):
        return self.journal.ev

    def strat(self):
        s = self.signals.get("strategy") or {}
        return {"id": s.get("id") or "portal_signals", "name": s.get("name") or "Portal BUY signals", "version": s.get("version") or "N/A"}

    def set_signals(self, sig):
        self.signals = {"buys": [b for b in sig.get("buys") or [] if b.get("symbol")], "sells": [s.get("symbol") for s in sig.get("sells") or []],
                        "regime": (sig.get("regime") or {}).get("state"), "data_status": sig.get("data_status"), "bar": sig.get("generated_at"),
                        "strategy": {k: (sig.get("strategy") or {}).get(k) for k in ("id", "name", "version", "timeframe")},
                        "blocked": [{k: b.get(k) for k in ("symbol", "score", "reason", "entry")} for b in sig.get("blocked") or []][:20]}

    def log(self, kind, msg, type_=None, **kw):
        kw.setdefault("mode", "LIVE" if kind.startswith("real") else "PAPER")
        e = self.journal.add(type_ or KIND_MAP.get(kind, kind.upper()), msg, kind=kind, **kw)
        audit(kind, msg=msg)
        return e

    def mode(self):
        m = self.cfg.get("MODE", "PAPER").upper()
        if m == "REAL" and self.safety.real_blockers(self.paper):
            return "PAPER"                                                # REAL requested but locked -> behave as PAPER
        return m if m in ("PAPER", "ALERT", "REAL") else "PAPER"

    def done(self):
        return self.paper.s.setdefault("done", {}).setdefault(now().date().isoformat(), {})

    def mark_done(self, sid, why):
        d = self.paper.s.setdefault("done", {})
        for k in [k for k in d if k != now().date().isoformat()]:
            d.pop(k)                                                      # keep only today
        self.done()[sid] = why; self.paper.save()

    def new_id(self, prefix):
        n = sum(1 for t in self.paper.s["trades"] if (t.get("rec") or {}).get("trade_id")) + len(self.paper.s["pos"]) + 1
        return f"{prefix}-{now().strftime('%Y%m%d-%H%M%S')}-{n:03d}"

    def on_tick(self, sym, state="OPEN"):
        q = self.book.get(sym)
        px = q.get("p")
        if not px:
            return
        self.last_scan = time.time()
        pos = self.paper.s["pos"].get(sym)
        if pos:
            if state != "OPEN" or (q.get("src") == "portal-delayed" and self.mode() == "REAL"):
                return                                                    # real money never acts on ~15-min-old prices
            return self.manage(sym, q, pos)
        b = next((b for b in self.signals["buys"] if b["symbol"] == sym and b.get("entry") and b.get("stop")), None)
        if b:
            self.consider(sym, q, b, state)

    # ---- entry
    def consider(self, sym, q, b, state):
        px, st = q["p"], self.strat()
        sid = b.get("signal_id") or f"{st['id']}|{sym}|{b.get('bar') or now().date().isoformat()}"
        age = round((time.time() - q["recv"]) * 1000) if q.get("recv") else None
        W = {"sym": sym, "signal_id": sid, "strategy": st["name"], "entry": b["entry"], "stop": b["stop"], "target": b.get("target"),
             "score": b.get("score"), "contrib": b.get("contrib"), "why": b.get("why"), "price": px, "src": q.get("src"), "age_ms": age,
             "checked": now().strftime("%H:%M:%S"), "indicators": {k: b.get(k) for k in ("rsi", "sma50", "sma200", "vol_ratio", "atr")}}
        W["indicators"].update(live_price=px, live_vwap=q.get("vwap"), bid=q.get("bid"), ask=q.get("ask"))
        self.watching[sym] = W
        wait = lambda *why: W.update(status="WAITING", why_not=list(why))
        if state != "OPEN":
            return wait(f"market is {state}")
        if sid in self.done():
            return W.update(status="DONE", why_not=[self.done()[sid]])
        if not (dt.time(9, 20) <= now().time() <= dt.time(15, 0)):
            return wait("outside the entry window 09:20-15:00 IST")
        if self.signals.get("data_status") not in (None, "OK"):
            return wait(f"portal data status is {self.signals.get('data_status')} - no entries on doubtful data")
        if not (b["entry"] * 0.995 <= px <= b["entry"] * 1.005):
            return wait(f"live price {px:,.2f} is {(px / b['entry'] - 1) * 100:+.2f}% from the entry {b['entry']:,.2f}; buys only within ±0.5% (never chases)")
        t_sig = time.time()
        if sid not in self.seen_sig:
            self.seen_sig.add(sid)
            self.journal.add("SIGNAL_GENERATED", f"BUY signal {sym} @ {px:,.2f} - {st['name']} score {b.get('score')}", symbol=sym, strategy=st["name"],
                             signal_id=sid, price=px, contrib=b.get("contrib"), why=b.get("why"), kind="signal")
        checks, qty = self.entry_checks(sym, q, b, px)
        W["checks"], W["qty"] = checks, qty
        fails = [c for c in checks if not c["ok"]]
        if fails:
            key = "|".join(c["name"] for c in fails)
            if self.rej.get(sid) != key:
                self.rej[sid] = key
                self.log("skip", f"BUY {sym} NOT executed: " + "; ".join(f"{c['name']} - {c['detail']}" for c in fails),
                         type_="SIGNAL_REJECTED", symbol=sym, strategy=st["name"], signal_id=sid, checks=checks, price=px)
            if any(c.get("final") for c in fails):
                self.mark_done(sid, "rejected: " + "; ".join(f"{c['name']} - {c['detail']}" for c in fails if c.get("final")))
            return W.update(status="REJECTED", why_not=[f"{c['name']}: {c['detail']}" for c in fails])
        W.update(status="EXECUTING", why_not=[])
        self.execute(sym, b, sid, px, qty, checks, t_sig, W)

    def entry_checks(self, sym, q, b, px):
        """Every gate before a NEW entry. final=True: not retried today (risk limits); otherwise retried on the next tick."""
        C = []
        add = lambda name, ok, detail, final=False: C.append({"name": name, "ok": bool(ok), "detail": detail, "final": final})
        for c in self.data_check(sym) or []:
            add(c["name"], c["ok"], c["detail"])
        if q.get("src") == "portal-delayed":
            ok = self.mode() != "REAL" and cfgv(self.cfg, "PAPER_ON_DELAYED").lower() in ("yes", "y", "true", "1")
            add("Live price (not delayed)", ok, "price is the portal's ~15-min DELAYED copy - new entries need live exchange ticks")
        add("Kill switch", not self.safety.killed(), "kill switch ON - no new orders" if self.safety.killed() else "off")
        add("Algorithm running", not self.safety.paused(), "PAUSED by you" if self.safety.paused() else "running")
        risk_ps = px - b["stop"]
        max_val = float(self.cfg.get("MAX_ORDER_VALUE") or 25000)
        budget = float(self.cfg.get("MAX_DAILY_LOSS") or 2000) / 2
        qty = int(min(max_val // px, budget // risk_ps)) if risk_ps > 0 else 0
        add("Stop below price", risk_ps > 0, f"stop {b['stop']:,.2f} vs price {px:,.2f}", True)
        add("Position size", qty >= 1, f"{qty} shares = min(max order ₹{max_val:,.0f} / price, risk budget ₹{budget:,.0f} / risk per share ₹{max(risk_ps, 0):,.2f})", True)
        if b.get("target") and risk_ps > 0:
            rr = (b["target"] - px) / risk_ps
            add("Risk/reward", rr >= float(cfgv(self.cfg, "MIN_RR")), f"1 : {rr:.2f} (minimum 1 : {float(cfgv(self.cfg, 'MIN_RR')):g})")
        if q.get("bid") and q.get("ask"):
            sp = (q["ask"] - q["bid"]) / ((q["ask"] + q["bid"]) / 2) * 100
            add("Spread", sp <= float(cfgv(self.cfg, "MAX_SPREAD_PCT")), f"{sp:.2f}% (max {cfgv(self.cfg, 'MAX_SPREAD_PCT')}%)")
        block = self.safety.check_order("BUY", qty * px, len(self.paper.s["pos"]), self.paper.realized_today()) if qty >= 1 else None
        if block != "kill switch ON":
            add("Risk limits", not block, block or "orders/day, order value, open positions, daily loss: PASS", True)
        need = self.paper.buy_cost(px, qty)[0] if qty >= 1 else 0
        add("Paper cash", need <= self.paper.s["cash"], f"₹{self.paper.s['cash']:,.0f} available, need ₹{need:,.0f} incl. charges", True)
        if self.mode() == "REAL":
            for c in self.real_check() or []:
                add(c["name"], c["ok"], c["detail"])
        return C, qty

    def revalidate(self, sym, signal_px, side):
        """Right before the order: re-read the LATEST quote. Too old or moved too far -> the signal is invalidated, never sent blind."""
        q = self.book.get(sym)
        mx = float(cfgv(self.cfg, "ENTRY_MAX_AGE_MS"))
        age = (time.time() - q["recv"]) * 1000 if q.get("recv") else None
        bad = [c for c in self.data_check(sym) or [] if not c["ok"]]
        if bad:
            return False, q, "; ".join(f"{c['name']} - {c['detail']}" for c in bad)
        if age is not None and q.get("src") in LIVE_SRC and age > mx:
            return False, q, f"price is {age:.0f} ms old (max {mx:.0f} ms)"
        if q.get("p") and abs(q["p"] / signal_px - 1) > 0.003:
            return False, q, f"price moved {(q['p'] / signal_px - 1) * 100:+.2f}% since the signal ({signal_px:,.2f} -> {q['p']:,.2f})"
        return True, q, "latest price confirmed"

    def execute(self, sym, b, sid, px, qty, checks, t_sig, W):
        st, why = self.strat(), f"signal BUY (score {b.get('score')}) - live {px:.2f} near entry {b['entry']:.2f}"
        t = {"signal": t_sig, "risk": time.time()}
        tid = self.new_id("S")
        J = lambda typ, msg, **kw: self.journal.add(typ, msg, symbol=sym, strategy=st["name"], trade_id=tid, signal_id=sid, **kw)
        J("RISK_APPROVED", f"risk checks passed for BUY {qty} {sym}", checks=checks)
        J("POSITION_SIZED", next(c["detail"] for c in checks if c["name"] == "Position size"), qty=qty)
        t["order"] = time.time()
        J("ORDER_CREATED", f"BUY {qty} {sym} (paper, {cfgv(self.cfg, 'PAPER_FILL_MODEL')} fill) · stop {b['stop']:,.2f}" + (f" · target {b['target']:,.2f}" if b.get("target") else ""))
        ok, q2, note = self.revalidate(sym, px, "BUY")
        if not ok:
            J("SIGNAL_INVALIDATED", f"BUY {sym} cancelled before sending: {note}")
            return W.update(status="INVALIDATED", why_not=[note])
        t["submit"] = time.time()
        fill, model = fill_model(self.cfg, q2, "BUY", q2.get("p") or px)
        rec = {"trade_id": tid, "mode": "PAPER", "execution": "SIMULATED", "fill_model": model, "strategy": st["name"], "strategy_id": st["id"],
               "strategy_version": st["version"], "signal_id": sid, "signal_type": "BUY", "instrument": f"{sym} · NSE equity (delivery)",
               "timeframe": "daily signal · entry on live tick", "direction": "LONG", "confidence": b.get("score"),
               "confidence_note": "engine rule score (points), not a probability", "contrib": b.get("contrib"), "entry_reason": b.get("why"),
               "indicators": W["indicators"], "risk_checks": checks, "order_type": "LIMIT (simulated)", "signal_price": px, "order_price": q2.get("p") or px,
               "fill_price": fill, "slippage": round(fill - px, 2), "slippage_pct": round((fill / px - 1) * 100, 3), "broker": "paper (no broker)"}
        t["fill"] = time.time(); rec["t"] = t
        f = self.paper.buy(sym, qty, fill, b["stop"], b.get("target"), why, rec=rec)
        if not f:
            self.mark_done(sid, "not enough paper cash")
            J("SIGNAL_REJECTED", f"BUY {sym}: not enough paper cash"); return W.update(status="REJECTED", why_not=["not enough paper cash"])
        self.safety.orders_today += 1
        self.mark_done(sid, f"executed - trade {tid}")
        J("ORDER_SUBMITTED", f"BUY {qty} {sym} sent to the PAPER simulator", execution="SIMULATED")
        J("ORDER_FILLED", f"SIMULATED fill BUY {qty} {sym} @ {fill:,.2f} ({model})", price=fill, execution="SIMULATED", latency=timeline(t))
        self.log("paper_buy", f"PAPER BUY {qty} {sym} @ {fill:.2f} · stop {b['stop']:.2f}" + (f" · target {b['target']:.2f}" if b.get("target") else "") + f" · {why}",
                 symbol=sym, strategy=st["name"], trade_id=tid, price=fill, stop=b["stop"], target=b.get("target"))
        W.update(status="IN POSITION", trade_id=tid)
        m = self.mode()
        if m == "ALERT":
            self.notify(f"BUY {sym} now", f"BUY {qty} {sym} around {px:.2f}. Stop {b['stop']:.2f}. Place it in your app (VISION LIVE alert).")
        elif m == "REAL":
            self.place_real(sym, "BUY", qty, px, why, tid=tid)

    # ---- position management + exits
    def manage(self, sym, q, pos):
        px = q["p"]
        lv = "delayed" if q.get("src") == "portal-delayed" else "live"
        why = kind = None
        if px <= pos["stop"]:
            why, kind = f"stop-loss hit at {lv} {px:.2f} (stop {pos['stop']:.2f})", "STOP_LOSS"
        elif pos.get("target") and px >= pos["target"]:
            why, kind = f"target hit at {lv} {px:.2f} (target {pos['target']:.2f})", "TARGET"
        elif sym in self.signals["sells"]:
            why, kind = "portal signal turned SELL", "STRATEGY_REVERSAL"
        if why:
            self.exit(sym, px, why, kind)

    def exit(self, sym, px, why, kind="SYSTEM_EXIT"):
        pos = self.paper.s["pos"].get(sym)
        if not pos:
            return
        q = self.book.get(sym)
        fill, model = fill_model(self.cfg, q, "SELL", px)
        tid = (pos.get("rec") or {}).get("trade_id")
        f = self.paper.sell(sym, fill, why, exit_type=kind, signal_price=px, t_fill=time.time())
        if not f:
            return
        st = (pos.get("rec") or {}).get("strategy") or self.strat()["name"]
        self.journal.add("ORDER_FILLED", f"SIMULATED fill SELL {f['qty']} {sym} @ {fill:,.2f} ({model})", symbol=sym, strategy=st, trade_id=tid,
                         price=fill, execution="SIMULATED")
        self.log("paper_sell", f"PAPER SELL {f['qty']} {sym} @ {fill:.2f} · P&L {f['pnl']:+.2f} · {why}", symbol=sym, strategy=st,
                 trade_id=tid, price=fill, pnl=f["pnl"], exit_type=kind)
        if kind in ("STOP_LOSS", "TARGET"):
            self.journal.add(kind, f"{sym}: {why}", symbol=sym, strategy=st, trade_id=tid, price=px)
        m = self.mode()
        if m == "ALERT":
            self.notify(f"SELL {sym} now", f"SELL {f['qty']} {sym} around {px:.2f}: {why}. Do it in your app.")
        elif m == "REAL" and sym in self.real:
            self.place_real(sym, "SELL", self.real[sym]["qty"], px, why, tid=tid)

    def close_all_paper(self, why="closed by you (manual)"):
        n = 0
        for sym in list(self.paper.s["pos"]):
            px = self.book.get(sym).get("p") or self.paper.s["pos"][sym]["avg"]
            self.exit(sym, px, why, "MANUAL_EXIT"); n += 1
        return n

    # ---- real orders (REAL mode only, every lock checked again here)
    def place_real(self, sym, side, qty, px, why, tid=None):
        blockers = self.safety.real_blockers(self.paper)
        if blockers:
            self.log("real_blocked", f"REAL {side} {qty} {sym} not sent: {'; '.join(blockers)}", symbol=sym, trade_id=tid); return
        bad = [c for c in (self.real_check() or []) if not c["ok"]] if side == "BUY" else []
        if bad:
            self.log("real_blocked", f"REAL {side} {qty} {sym} not sent: " + "; ".join(f"{c['name']} - {c['detail']}" for c in bad), symbol=sym, trade_id=tid); return
        q = self.book.get(sym)
        if not q.get("recv") or time.time() - q["recv"] > 5:
            self.log("real_blocked", f"REAL {side} {sym} not sent: live price older than 5 s", symbol=sym, trade_id=tid); return
        limit = tick_round(px * 1.005, True) if side == "BUY" else tick_round(px * 0.995, False)
        t = {"signal": time.time(), "order": time.time()}
        try:
            t["submit"] = time.time()
            oid = self.feed.place_limit(sym, side, qty, limit)
            t["ack"] = time.time()
            rec = {"time": now().isoformat(timespec="seconds"), "side": side, "sym": sym, "qty": qty, "limit": limit, "order_id": oid, "why": why,
                   "mode": "LIVE", "trade_id": tid, "signal_price": px, "status": "SUBMITTED", "filled": 0, "avg": None, "t": t,
                   "broker": "Shoonya" if isinstance(self.feed, ShoonyaFeed) else type(self.feed).__name__}
            if oid and side == "BUY":
                self.real[sym] = rec
            elif side == "SELL":
                self.real.pop(sym, None)
            try:
                hist = json.load(open(REAL_FILE))
            except Exception:  # noqa
                hist = []
            json.dump(hist + [rec], open(REAL_FILE, "w"), indent=1)
            self.log("real_order", f"REAL {side} {qty} {sym} LIMIT {limit:.2f} -> order id {oid}", symbol=sym, trade_id=tid, order_id=oid)
            self.journal.add("ORDER_ACCEPTED", f"broker accepted {side} {sym}: order id {oid} (status follows from the broker)", mode="LIVE",
                             symbol=sym, trade_id=tid, order_id=oid, latency=timeline(t))
            self.notify(f"REAL {side} {sym}", f"Sent LIMIT {side} {qty} {sym} @ {limit:.2f}. Order id {oid}.")
        except Exception as e:  # noqa
            self.log("real_error", f"REAL {side} {sym} failed: {scrub(e, self.cfg)}", symbol=sym, trade_id=tid)
            self.journal.add("ORDER_REJECTED", f"REAL {side} {sym} rejected: {scrub(e, self.cfg)}", mode="LIVE", symbol=sym, trade_id=tid)


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


# ------------------------------------------------------------------ real-time data integrity, resync, reconciliation, unified ledger
def _stats(xs):
    xs = [x for x in xs if x is not None]
    if not xs:
        return {"avg_ms": None, "max_ms": None, "p95_ms": None, "samples": 0}
    ys = sorted(xs)
    return {"avg_ms": round(sum(xs) / len(xs), 1), "max_ms": round(ys[-1], 1), "p95_ms": round(ys[int(0.95 * (len(ys) - 1))], 1), "samples": len(xs)}


def data_state(app):
    """Honest freshness of the live market data. LIVE only when ticks are really arriving now - an open socket is not enough."""
    cfg, st, nowt = app.cfg, app.book.stats, time.time()
    live_ms, stale_ms = float(cfgv(cfg, "DATA_LIVE_MS")), float(cfgv(cfg, "DATA_STALE_MS"))
    feed = app.feed.health() if app.feed else {"state": "NOT STARTED"}
    fs = feed.get("state")
    age = round((nowt - st["last_live_recv"]) * 1000) if st["last_live_recv"] else None
    if not app.feed or fs in ("NEEDS LOGIN", "NOT STARTED"):
        status, label = "UNAVAILABLE", "LIVE DATA UNAVAILABLE"
    elif fs in ("DISCONNECTED", "RECONNECTING", "CONNECTING"):
        status, label = "LOST", "DATA CONNECTION LOST"
    elif fs == "RESYNCING":
        status, label = "RESYNC", "RECONNECTED - WAITING FOR FRESH DATA"
    elif app.market != "OPEN":
        status, label = "MARKET CLOSED", "MARKET CLOSED - last prices shown"
    elif age is None or age > stale_ms:
        status, label = "STALE", "STALE"
    elif age > live_ms:
        status, label = "DELAYED", "DELAYED"
    else:
        status, label = "LIVE", "LIVE"
    offs = list(app.book.offsets)
    if offs:
        off_ms = round((statistics.median(offs) - 0.5) * 1000)        # exchange time is truncated to the second: centre it
        clock = {"status": "SYNCED" if abs(off_ms) <= float(cfgv(cfg, "CLOCK_MAX_OFFSET_MS")) and not st["future_ts"] else "OUT OF SYNC",
                 "offset_ms": off_ms, "note": "this computer's clock vs exchange timestamps (exchange gives whole seconds: ±500 ms)"}
    else:
        clock = {"status": "N/A", "offset_ms": None, "note": "no exchange timestamps received yet"}
    gaps = getattr(app, "gaps", [])
    rs = getattr(app, "resync", {}) or {}
    rec = getattr(app, "recon", {}) or {}
    quality = [
        {"name": "Connection", "status": "PASS" if fs == "LIVE" else "FAIL", "detail": fs},
        {"name": "Freshness", "status": "PASS" if status == "LIVE" else ("N/A" if status == "MARKET CLOSED" else "FAIL"), "detail": f"last tick {age} ms ago" if age is not None else "no live tick yet"},
        {"name": "Timestamps", "status": "PASS" if clock["status"] == "SYNCED" else ("N/A" if clock["status"] == "N/A" else "FAIL"),
         "detail": f"clock offset {clock['offset_ms']} ms · out-of-order rejected {st['out_of_order']} · future-dated {st['future_ts']}"},
        {"name": "Sequence", "status": "N/A", "detail": "Shoonya sends no sequence numbers; order is checked by exchange time instead"},
        {"name": "Missing data (gaps)", "status": "PASS" if not [g for g in gaps if g.get("to") is None] else "FAIL", "detail": f"{len(gaps)} gap(s) today"},
        {"name": "Duplicates", "status": "PASS", "detail": f"{st['duplicates']} identical repeat ticks ignored"},
        {"name": "Resync", "status": "PASS" if rs.get("status") == "COMPLETE" else ("FAIL" if rs.get("status") == "FAILED" else "N/A"), "detail": rs.get("status") or "not run yet"},
        {"name": "Broker sync", "status": {"OK": "PASS", "MISMATCH": "FAIL"}.get(rec.get("status"), "N/A"), "detail": rec.get("detail") or "paper only - nothing at the broker"}]
    return {"status": status, "label": label, "age_ms": age, "live_ms": live_ms, "stale_ms": stale_ms, "feed_state": fs,
            "source": f"{app.broker.title()} ({feed.get('method') or 'WebSocket'})" if app.feed else "none",
            "ticks": st["live_ticks"], "dropped": st["rejected"], "duplicates": st["duplicates"], "out_of_order": st["out_of_order"],
            "reconnects": feed.get("reconnects", 0), "last_reconnect": feed.get("last_reconnect"), "connected_since": feed.get("connected_since"),
            "exch_to_server": {**_stats(list(app.book.lat)), "note": "exchange timestamps are whole seconds, so this is accurate to about ±1000 ms"},
            "server_to_algo": _stats(list(getattr(app, "algo_lat", []))), "clock": clock, "gaps": gaps[-10:], "quality": quality,
            "sequence": "N/A (not provided by Shoonya)", "exchange_time": ist_ms(max((q.get("t") or 0) for q in app.book.q.values()) if app.book.q else None),
            "local_time": ist_ms(nowt), "server_now_ms": int(nowt * 1000)}


def data_check(app, sym):
    """Live-data part of the trading gate for one symbol (used before EVERY new automatic entry and again right before the order)."""
    q = app.book.get(sym)
    if q.get("src") == "portal-delayed" and cfgv(app.cfg, "PAPER_ON_DELAYED").lower() in ("yes", "y", "true", "1") and app.trader.mode() != "REAL":
        return []                                                     # opted-in delayed paper practice (labelled in entry_checks)
    D = data_state(app)
    mx = float(cfgv(app.cfg, "ENTRY_MAX_AGE_MS"))
    age = round((time.time() - q["recv"]) * 1000) if q.get("recv") else None
    rs = (getattr(app, "resync", {}) or {}).get("status")
    return [{"name": "Market data LIVE", "ok": D["status"] == "LIVE", "detail": f"{D['label']}" + (f" · last tick {D['age_ms']} ms ago" if D["age_ms"] is not None else "")},
            {"name": f"{sym} price fresh", "ok": age is not None and age <= mx and q.get("src") in LIVE_SRC,
             "detail": f"{'—' if age is None else f'{age} ms'} old from {q.get('src') or 'nowhere'} (max {mx:.0f} ms, live stream only)"},
            {"name": "Resync complete", "ok": rs == "COMPLETE", "detail": rs or "not run yet"},
            {"name": "Clock", "ok": D["clock"]["status"] != "OUT OF SYNC", "detail": f"{D['clock']['status']}" + (f" ({D['clock']['offset_ms']} ms)" if D["clock"]["offset_ms"] is not None else "")}]


def real_check(app):
    """REAL-mode extra gate: broker stream up, positions reconciled, no unresolved order."""
    fs = (app.feed.health() if app.feed else {}).get("state")
    rec = getattr(app, "recon", {}) or {}
    return [{"name": "Broker connected", "ok": fs == "LIVE", "detail": str(fs)},
            {"name": "Positions reconciled with broker", "ok": rec.get("status") == "OK", "detail": rec.get("detail") or "not checked yet"}]


def load_real():
    try:
        return json.load(open(REAL_FILE))
    except Exception:  # noqa
        return []


SH_STATUS = {"OPEN": "OPEN", "PENDING": "PENDING", "TRIGGER_PENDING": "PENDING", "COMPLETE": "COMPLETE", "REJECTED": "REJECTED",
             "CANCELED": "CANCELLED", "CANCELLED": "CANCELLED"}
FINAL = ("COMPLETE", "REJECTED", "CANCELLED", "FAILED")


def sync_orders(app):
    """Real orders: read the broker's order book, update each order's status/fill/rejection - never left 'pending' forever."""
    recs = load_real()
    live = [r for r in recs if r.get("order_id") and r.get("status") not in FINAL]
    if not live or not app.feed or not hasattr(app.feed, "order_book"):
        return 0
    book = {str(o.get("norenordno")): o for o in app.feed.order_book()}
    n = 0
    for r in live:
        o = book.get(str(r["order_id"]))
        if not o:
            continue
        st = SH_STATUS.get(str(o.get("status", "")).upper(), str(o.get("status")))
        filled = int(float(o.get("fillshares") or 0))
        if st == "OPEN" and 0 < filled < int(r["qty"]):
            st = "PARTIALLY FILLED"
        if st != r.get("status") or filled != r.get("filled"):
            r.update(status=st, filled=filled, avg=float(o["avgprc"]) if o.get("avgprc") else r.get("avg"), broker_msg=o.get("rejreason") or o.get("status"),
                     updated=now().isoformat(timespec="seconds"))
            if st in ("COMPLETE", "PARTIALLY FILLED"):
                r.setdefault("t", {})["fill"] = time.time()
            typ = {"COMPLETE": "ORDER_FILLED", "PARTIALLY FILLED": "ORDER_PARTIALLY_FILLED", "REJECTED": "ORDER_REJECTED",
                   "CANCELLED": "ORDER_CANCELLED", "OPEN": "ORDER_ACCEPTED", "PENDING": "ORDER_ACCEPTED"}.get(st, "ORDER_UPDATE")
            app.journal.add(typ, f"broker: {r['side']} {r['sym']} order {r['order_id']} {st}" + (f" · {filled} filled @ {r['avg']}" if filled else "")
                            + (f" · {o.get('rejreason')}" if o.get("rejreason") else ""), mode="LIVE", symbol=r["sym"], trade_id=r.get("trade_id"),
                            order_id=r["order_id"], source="broker")
            n += 1
    if n:
        json.dump(recs, open(REAL_FILE, "w"), indent=1)
        reconcile(app)
    return n


def reconcile(app):
    """Compare what 16VITAWS thinks it holds at the broker with the broker's own position book. Mismatch -> REAL entries paused."""
    if not real_active(app):
        app.recon = {"status": "N/A", "detail": "paper only - nothing at the broker", "rows": [], "at": now().strftime("%H:%M:%S")}
        return app.recon
    if not app.feed or not hasattr(app.feed, "positions"):
        app.recon = {"status": "MISMATCH", "detail": f"{app.broker.title()} position check not supported here - reconcile by hand", "rows": [], "at": now().strftime("%H:%M:%S")}
        return app.recon
    try:
        rows = app.feed.positions()
    except Exception as e:  # noqa
        app.recon = {"status": "MISMATCH", "detail": f"could not read broker positions: {scrub(e, app.cfg)}", "rows": [], "at": now().strftime("%H:%M:%S")}
        return app.recon
    day = now().date().isoformat()
    mine = collections.Counter()
    for r in load_real():
        if str(r.get("time", ""))[:10] == day and r.get("filled"):
            mine[r["sym"]] += int(r["filled"]) * (1 if r["side"] == "BUY" else -1)
    broker = collections.Counter()
    for p in rows:
        sym = str(p.get("tsym", "")).replace("-EQ", "")
        q = int(float(p.get("daybuyqty") or 0)) - int(float(p.get("daysellqty") or 0)) if p.get("daybuyqty") is not None else int(float(p.get("netqty") or 0))
        if sym in mine or sym in app.watch:
            broker[sym] += q
    diff = [{"sym": s, "portal": mine.get(s, 0), "broker": broker.get(s, 0)} for s in sorted(set(mine) | set(broker)) if mine.get(s, 0) != broker.get(s, 0)]
    prev = (getattr(app, "recon", {}) or {}).get("status")
    app.recon = {"status": "MISMATCH" if diff else "OK", "rows": diff, "at": now().strftime("%H:%M:%S"),
                 "detail": ("; ".join(f"{d['sym']}: portal {d['portal']} vs broker {d['broker']}" for d in diff) + " - REAL entries paused") if diff else "positions match the broker"}
    if diff and prev != "MISMATCH":
        app.journal.add("POSITION_MISMATCH", "ACCOUNT RECONCILIATION REQUIRED: " + app.recon["detail"], mode="LIVE", source="broker")
        app.notify("16VITAWS: position mismatch", app.recon["detail"])
    return app.recon


def real_active(app):
    return app.cfg.get("MODE", "PAPER").upper() == "REAL" or any(str(r.get("time", ""))[:10] == now().date().isoformat() for r in load_real())


def resync(app):
    """After every (re)connect: do not trust the local state - refresh and check everything, then allow trading again."""
    steps = []
    app.resync = {"status": "IN PROGRESS", "started": now().strftime("%H:%M:%S"), "steps": steps}
    app.journal.add("RESYNC_STARTED", "RESYNC IN PROGRESS - new automatic entries paused", source="data")
    step = lambda name, ok, detail: steps.append({"name": name, "ok": bool(ok), "detail": detail})
    fs = (app.feed.health() if app.feed else {}).get("state")
    step("Market subscriptions", fs == "LIVE", f"stream {fs}" + (f" · {len(app.tokens)} instruments" if getattr(app, "tokens", None) else ""))
    fresh = sum(1 for q in app.book.snapshot().values() if q.get("recv") and time.time() - q["recv"] < 30)
    step("Latest quotes", fresh > 0, f"{fresh} instruments with a quote in the last 30 s")
    if real_active(app) and app.feed and hasattr(app.feed, "positions"):
        try:
            n = sync_orders(app); step("Open orders + order status", True, f"{n} order update(s) from the broker")
        except Exception as e:  # noqa
            step("Open orders + order status", False, scrub(e, app.cfg))
        try:
            lim = app.feed.limits(); app.funds = lim
            step("Funds / margin", lim is not None, f"cash {lim.get('cash')}" if lim else "not returned")
        except Exception as e:  # noqa
            step("Funds / margin", False, scrub(e, app.cfg))
        r = reconcile(app); step("Reconcile positions with broker", r["status"] == "OK", r["detail"])
    else:
        step("Positions / orders / funds", True, "PAPER: the local paper book is the record (nothing at the broker)")
        reconcile(app)
    try:
        app.refresh_chain(); step("Option chain", True, f"{len(app.chains)} chain(s) re-centred on the latest index price")
    except Exception as e:  # noqa
        step("Option chain", False, scrub(e, app.cfg))
    step("Indicators", True, "recalculated on the next tick (VWAP, day change, chain scores arrive with every tick)")
    step("P&L", True, "recalculated from the latest prices on every update")
    ok = all(s["ok"] for s in steps)
    app.resync.update(status="COMPLETE" if ok else "FAILED", finished=now().strftime("%H:%M:%S"))
    app.journal.add("RESYNC_COMPLETE" if ok else "RESYNC_FAILED", "RESYNC COMPLETE - trading allowed again" if ok else
                    "RESYNC FAILED - TRADING PAUSED: " + "; ".join(f"{s['name']}: {s['detail']}" for s in steps if not s["ok"]), source="data")
    return app.resync


def watchdog(app):
    """Runs on every pass of the main loop: detects lost/stale data (gaps), reconnects, and starts the resync after a reconnect."""
    D = data_state(app)
    prev = getattr(app, "_dstat", None)
    s = D["status"]
    if s != prev:
        app._dstat = s
        bad = s in ("STALE", "LOST", "UNAVAILABLE")
        if bad and app.market == "OPEN":
            app.gaps = (getattr(app, "gaps", []) + [{"from": now().strftime("%H:%M:%S"), "to": None, "status": s, "_t": time.time()}])[-50:]
            app.journal.add("DATA_STALE" if s == "STALE" else "DATA_CONNECTION_LOST", f"{D['label']} - NEW TRADES BLOCKED" +
                            (f" (last tick {D['age_ms']} ms ago)" if D["age_ms"] is not None else ""), source="data")
        elif s == "LIVE" and getattr(app, "gaps", None) and app.gaps[-1]["to"] is None:
            g = app.gaps[-1]; g.update(to=now().strftime("%H:%M:%S"), dur_s=round(time.time() - g["_t"], 1))
            app.journal.add("DATA_RESTORED", f"live data back after {g['dur_s']} s gap - fresh ticks arriving", source="data")
        if s in ("LOST", "UNAVAILABLE", "RESYNC") and (app.resync or {}).get("status") == "COMPLETE":
            app.resync = {**app.resync, "status": "NEEDED"}            # after a disconnect nothing local is trusted until resynced
        if s == "LIVE" and prev in (None, "LOST", "RESYNC", "UNAVAILABLE", "STALE", "MARKET CLOSED") and (app.resync or {}).get("status") != "IN PROGRESS":
            threading.Thread(target=resync, args=(app,), daemon=True).start()
    g = (getattr(app, "gaps", None) or [None])[-1]
    if g and g["to"] is None and not g.get("alerted") and time.time() - g["_t"] > 30 and app.market == "OPEN":
        g["alerted"] = True                                           # one phone alert per gap that lasts > 30 s
        app.notify("16VITAWS: live data stopped", f"{D['label']} since {g['from']}. New automatic trades are blocked until fresh data returns.")
    rs = app.resync or {}
    if rs.get("status") == "FAILED" and time.time() - getattr(app, "_rs_retry", 0) > 60 and s == "LIVE":
        app._rs_retry = time.time(); threading.Thread(target=resync, args=(app,), daemon=True).start()
    return D


# ---- unified trade/event model (single source of truth for chart markers, journal, positions, P&L, strategy stats)
def _iso_ts(x):
    try:
        return dt.datetime.fromisoformat(str(x)).timestamp()
    except (TypeError, ValueError):
        return None


def ledger(app):
    out = []
    snap = app.book.snapshot()
    stock_name = app.trader.strat()["name"]
    for i, t in enumerate(app.paper.s["trades"]):
        r = t.get("rec") or {}
        out.append({"id": r.get("trade_id") or f"S-{i + 1:04d}", "mode": "PAPER", "execution": "SIMULATED", "kind": "STOCK",
                    "strategy": r.get("strategy") or stock_name + (" (before trade records)" if not r else ""), "symbol": t["sym"],
                    "instrument": r.get("instrument") or f"{t['sym']} · NSE equity", "side": "BUY", "qty": t["qty"],
                    "signal_price": r.get("signal_price"), "order_price": r.get("order_price"), "fill_price": t["entry"], "stop": t.get("stop"),
                    "target": t.get("target"), "entry_time": t.get("opened"), "exit_time": t.get("closed"), "exit_price": t["exit"],
                    "exit_signal_price": t.get("exit_signal_price"), "exit_reason": t.get("why"), "exit_type": t.get("exit_type") or "N/A",
                    "gross": t.get("gross"), "charges": t.get("charges"), "net": t["pnl"], "status": "CLOSED", "ltp": None, "unrealized": 0.0,
                    "reasons": r.get("entry_reason"), "contrib": r.get("contrib"), "confidence": r.get("confidence"), "indicators": r.get("indicators"),
                    "checks": r.get("risk_checks"), "fill_model": r.get("fill_model"), "slippage": r.get("slippage"),
                    "timeline": timeline(r.get("t") or {}) if r else None, "order_id": r.get("trade_id"), "chart_sym": t["sym"],
                    "costs": t.get("costs"), "segment": "EQ_DELIVERY", "exit_slip": (t["exit"] - t["exit_signal_price"]) if t.get("exit_signal_price") else None})
    for sym, p in app.paper.s["pos"].items():
        r = p.get("rec") or {}
        ltp = (snap.get(sym) or {}).get("p")
        out.append({"id": r.get("trade_id") or f"S-open-{sym}", "mode": "PAPER", "execution": "SIMULATED", "kind": "STOCK",
                    "strategy": r.get("strategy") or stock_name, "symbol": sym, "instrument": r.get("instrument") or f"{sym} · NSE equity",
                    "side": "BUY", "qty": p["qty"], "signal_price": r.get("signal_price"), "order_price": r.get("order_price"), "fill_price": p["avg"],
                    "stop": p.get("stop"), "target": p.get("target"), "entry_time": p.get("at"), "exit_time": None, "exit_price": None,
                    "status": "OPEN", "ltp": ltp, "unrealized": round(((ltp or p["avg"]) - p["avg"]) * p["qty"], 2),
                    "data_age_ms": round((time.time() - snap[sym]["recv"]) * 1000) if snap.get(sym, {}).get("recv") else None,
                    "reasons": r.get("entry_reason") or [p.get("why")], "contrib": r.get("contrib"), "confidence": r.get("confidence"),
                    "indicators": r.get("indicators"), "checks": r.get("risk_checks"), "fill_model": r.get("fill_model"), "slippage": r.get("slippage"),
                    "timeline": timeline(r.get("t") or {}) if r else None, "order_id": r.get("trade_id"), "chart_sym": sym, "net": None,
                    "costs": {"buy": p.get("buy_costs")} if p.get("buy_costs") else None, "segment": "EQ_DELIVERY"})
    ch = app.chain_tr.s if getattr(app, "chain_tr", None) else {"trades": [], "open": None}
    for i, t in enumerate(ch.get("trades") or []):
        out.append({"id": t.get("trade_id") or f"O-{i + 1:04d}", "mode": "PAPER", "execution": "SIMULATED", "kind": "OPTION",
                    "strategy": t.get("strategy") or ChainTrader.STRATEGY, "symbol": t.get("underlying"), "instrument": t.get("tsym"), "side": "BUY",
                    "qty": t["qty"], "signal_price": t.get("signal_price"), "order_price": t.get("signal_price"), "fill_price": t["entry"],
                    "stop": t.get("stop"), "target": t.get("target"), "entry_time": t.get("entry_time"), "exit_time": t.get("exit_time"),
                    "exit_price": t.get("exit"), "exit_signal_price": t.get("exit_signal_price"), "exit_reason": t.get("why"), "exit_type": t.get("exit_type") or "N/A",
                    "gross": t.get("gross"), "charges": t.get("charges"), "net": t.get("pnl"), "status": "CLOSED", "unrealized": 0.0,
                    "reasons": t.get("reasons"), "contrib": t.get("contrib"), "confidence": t.get("score"), "fill_model": t.get("fill_model"),
                    "slippage": t.get("slippage"), "timeline": timeline(t.get("t") or {}) if t.get("t") else None, "order_id": t.get("trade_id"), "chart_sym": t.get("sym"),
                    "costs": t.get("costs"), "segment": "OPT", "exit_slip": (t["exit"] - t["exit_signal_price"]) if t.get("exit_signal_price") else None})
    o = ch.get("open")
    if o:
        q = snap.get(o["sym"]) or {}
        out.append({"id": o.get("trade_id") or "O-open", "mode": "PAPER", "execution": "SIMULATED", "kind": "OPTION", "strategy": o.get("strategy") or ChainTrader.STRATEGY,
                    "symbol": o.get("underlying"), "instrument": o.get("tsym"), "side": "BUY", "qty": o["qty"], "signal_price": o.get("signal_price"),
                    "order_price": o.get("signal_price"), "fill_price": o["entry"], "stop": o.get("stop"), "target": o.get("target"), "entry_time": o.get("entry_time"),
                    "status": "OPEN", "ltp": q.get("bid") or q.get("p"), "unrealized": round(((q.get("bid") or o["entry"]) - o["entry"]) * o["qty"], 2),
                    "data_age_ms": round((time.time() - q["recv"]) * 1000) if q.get("recv") else None, "reasons": o.get("reasons"), "contrib": o.get("contrib"),
                    "confidence": o.get("score"), "fill_model": o.get("fill_model"), "slippage": o.get("slippage"),
                    "timeline": timeline(o.get("t") or {}) if o.get("t") else None, "order_id": o.get("trade_id"), "chart_sym": o["sym"], "net": None,
                    "segment": "OPT", "costs": None})
    src = {**{(t.get("rec") or {}).get("trade_id"): (t.get("rec") or {}).get("t") for t in app.paper.s["trades"]},
           **{(p.get("rec") or {}).get("trade_id"): (p.get("rec") or {}).get("t") for p in app.paper.s["pos"].values()},
           **{t.get("trade_id"): t.get("t") for t in (ch.get("trades") or []) + ([o] if o else [])}}
    for t in out:
        T = src.get(t["id"]) or {}
        t["entry_ts"] = T.get("fill") or _iso_ts(t.get("entry_time"))                 # actual epoch of the fill (chart marker position)
        t["exit_ts"] = (T.get("exit") or _iso_ts(t.get("exit_time"))) if t["status"] == "CLOSED" else None
        if t.get("stop") is not None and t.get("fill_price"):
            t["risk"] = round((t["fill_price"] - t["stop"]) * t["qty"], 2)
            t["reward"] = round((t["target"] - t["fill_price"]) * t["qty"], 2) if t.get("target") else None
            t["rr"] = f"1 : {t['reward'] / t['risk']:.2f}" if t.get("reward") and t["risk"] > 0 else None
        t["capital"] = round(t["fill_price"] * t["qty"], 2) if t.get("fill_price") else None
        account(app, t)
    return out


OPT_RE = __import__("re").compile(r"^OPT (\w+) (\d{4}-\d{2}-\d{2}) ([\d.]+) (CE|PE)$")


def account(app, t):
    """THE accounting for one trade (single source of truth for every screen): turnover, itemised charges per side,
    gross, total charges, net, slippage cost, ROI, holding time, option details. Open trades: ESTIMATED exit charges."""
    rules, broker = getattr(app, "charges", DEFAULT_CHARGES), getattr(app, "broker", "SHOONYA")
    c = t.get("costs") or {}
    b, sl = c.get("buy"), c.get("sell")
    if t["status"] == "OPEN":
        px = t.get("ltp") or t["fill_price"]
        if not b:
            b = side_costs(rules, broker, t["segment"], "BUY", t["fill_price"], t["qty"], str(t.get("entry_time") or "")[:10] or None)
        sl = side_costs(rules, broker, t["segment"], "SELL", px, t["qty"])
        g = round((px - t["fill_price"]) * t["qty"], 2)
        t.update(gross_unrealized=g, est_exit_charges=sl.get("total"), charges_paid=b.get("total"),
                 est_net=None if b.get("total") is None or sl.get("total") is None else round(g - b["total"] - sl["total"], 2),
                 position_value=round(px * t["qty"], 2), charges_status="ESTIMATED (exit not done yet)")
    else:
        t["charges_status"] = ("CALCULATED from configured rules (paper: SIMULATED)" if t["mode"] == "PAPER" else
                               "CALCULATED - broker charges not available from the API; check the contract note") if b and sl else "DATA UNAVAILABLE (trade recorded before the charge engine)"
    keys = ("brokerage", "stt", "exchange", "sebi", "gst", "stamp", "other")
    t["cost_buy"], t["cost_sell"] = b, sl
    t["components"] = {k: round(((b or {}).get(k) or 0) + ((sl or {}).get(k) or 0), 2) if (b or sl) else None for k in keys}
    t["turnover"] = round(((b or {}).get("turnover") or t["fill_price"] * t["qty"]) + ((sl or {}).get("turnover") or 0), 2)
    ent = (t.get("slippage") or 0) * t["qty"]
    ex = -(t.get("exit_slip") or 0) * t["qty"] if t["status"] == "CLOSED" else 0
    t["slippage_cost"] = round(ent + ex, 2) if (t.get("slippage") is not None or t.get("exit_slip") is not None) else None
    t["roi_pct"] = round(t["net"] / t["capital"] * 100, 2) if t["status"] == "CLOSED" and t.get("net") is not None and t.get("capital") else None
    e, x = _iso_ts(t.get("entry_time")), _iso_ts(t.get("exit_time"))
    t["holding_min"] = round(((x or time.time()) - e) / 60, 1) if e else None
    m = OPT_RE.match(str(t.get("chart_sym") or ""))
    if m:
        t["option"] = {"underlying": m.group(1), "expiry": m.group(2), "strike": float(m.group(3)), "type": m.group(4),
                       "lot": (t.get("qty") or 0) // max(1, int(float(app.cfg.get("OPT_LOTS") or 1))) if getattr(app, "cfg", None) else None}
    return t


def period_report(trades, start, end, capital=None):
    """Profit & charges for closed trades whose EXIT date is in [start, end] (ISO dates), separately per mode - PAPER and LIVE
    are never added together. Open positions appear only as ESTIMATED unrealized."""
    out = {}
    for mode in ("PAPER", "LIVE"):
        cl = [t for t in trades if t["mode"] == mode and t["status"] == "CLOSED" and start <= str(t.get("exit_time") or "")[:10] <= end]
        op = [t for t in trades if t["mode"] == mode and t["status"] == "OPEN"]
        nets = [t["net"] for t in cl if t.get("net") is not None]
        gr = [t.get("gross") or 0 for t in cl]
        comp = {k: round(sum((t.get("components") or {}).get(k) or 0 for t in cl), 2) for k in ("brokerage", "stt", "exchange", "sebi", "gst", "stamp", "other")}
        used = sum(t.get("capital") or 0 for t in cl)
        wins, losses = [x for x in nets if x > 0], [x for x in nets if x < 0]
        out[mode] = {"mode": mode, "from": start, "to": end, "trades": len(cl), "buy_orders": len(cl), "sell_orders": len(cl),
                     "wins": len(wins), "losses": len(losses), "win_rate": round(100 * len(wins) / len(nets), 2) if nets else None,
                     "gross_profit": round(sum(g for g in gr if g > 0), 2), "gross_loss": round(sum(g for g in gr if g < 0), 2), "gross": round(sum(gr), 2),
                     **{k: v for k, v in comp.items()}, "taxes": round(comp["stt"] + comp["gst"] + comp["stamp"], 2),
                     "regulatory": round(comp["exchange"] + comp["sebi"], 2), "charges": round(sum(t.get("charges") or 0 for t in cl), 2),
                     "slippage": round(sum(t.get("slippage_cost") or 0 for t in cl), 2), "net": round(sum(nets), 2),
                     "net_profit": round(sum(wins), 2), "net_loss": round(sum(losses), 2),
                     "roi_pct": round(sum(nets) / used * 100, 2) if used else None, "roi_note": "net P&L / capital used by these trades",
                     "return_on_capital_pct": round(sum(nets) / capital * 100, 3) if capital and mode == "PAPER" else None,
                     "avg_profit": round(sum(wins) / len(wins), 2) if wins else None, "avg_loss": round(sum(losses) / len(losses), 2) if losses else None,
                     "largest_profit": max(wins) if wins else None, "largest_loss": min(losses) if losses else None,
                     "avg_trade": round(sum(nets) / len(nets), 2) if nets else None,
                     "best": max(cl, key=lambda t: t["net"] or 0)["id"] if cl else None, "worst": min(cl, key=lambda t: t["net"] or 0)["id"] if cl else None,
                     "open_positions": len(op), "unrealized_gross_est": round(sum(t.get("gross_unrealized") or 0 for t in op), 2),
                     "unrealized_net_est": round(sum(t.get("est_net") or 0 for t in op), 2),
                     "incomplete": sum(1 for t in cl if not str(t.get("charges_status", "")).startswith("CALCULATED"))}
    return out


def period_bounds(period, frm=None, to=None):
    d = now().date()
    if period == "yesterday":
        y = d - dt.timedelta(days=1); return y.isoformat(), y.isoformat()
    if period == "week":
        return (d - dt.timedelta(days=d.weekday())).isoformat(), d.isoformat()
    if period == "month":
        return d.replace(day=1).isoformat(), d.isoformat()
    if period == "custom" and frm and to:
        return str(frm)[:10], str(to)[:10]
    return d.isoformat(), d.isoformat()


def orders_view(app):
    """Order panel: paper fills (SIMULATED, instant) + real broker orders (status from the broker)."""
    rows = [{"time": f.get("time"), "mode": "PAPER", "execution": "SIMULATED", "side": f["side"], "symbol": f["sym"], "qty": f["qty"],
             "price": f["price"], "status": "COMPLETE (simulated)", "order_id": f.get("trade_id") or "paper", "broker_msg": "paper simulator - no broker"}
            for f in app.paper.s.get("fills", [])[-40:]]
    rows += [{"time": r.get("time"), "mode": "LIVE", "execution": "BROKER", "side": r["side"], "symbol": r["sym"], "qty": r["qty"], "price": r.get("limit"),
              "avg": r.get("avg"), "status": r.get("status") or "SUBMITTED", "order_id": r.get("order_id"), "broker_msg": r.get("broker_msg") or "",
              "broker": r.get("broker"), "order_type": "LIMIT", "product": "CNC (delivery)", "exchange": "NSE", "latency": timeline(r.get("t") or {})}
             for r in load_real()[-40:]]
    return sorted(rows, key=lambda r: str(r.get("time")), reverse=True)


def pnl_today(trades, start_capital):
    day = now().date().isoformat()
    closed = [t for t in trades if t["status"] == "CLOSED" and str(t.get("exit_time") or "")[:10] == day]
    nets = [t["net"] for t in closed if t.get("net") is not None]
    wins, losses = [x for x in nets if x > 0], [x for x in nets if x < 0]
    unreal = round(sum(t.get("unrealized") or 0 for t in trades if t["status"] == "OPEN"), 2)
    realized = round(sum(nets), 2)
    return {"realized": realized, "unrealized": unreal, "total": round(realized + unreal, 2),
            "gross_profit": round(sum(t["gross"] for t in closed if (t.get("gross") or 0) > 0), 2),
            "gross_loss": round(sum(t["gross"] for t in closed if (t.get("gross") or 0) < 0), 2),
            "charges": round(sum(t.get("charges") or 0 for t in closed), 2), "net": realized,
            "return_pct": round((realized + unreal) / start_capital * 100, 2) if start_capital else None,
            "trades": len(closed), "wins": len(wins), "losses": len(losses), "win_rate": round(100 * len(wins) / len(nets), 2) if nets else None,
            "avg_win": round(sum(wins) / len(wins), 2) if wins else None, "avg_loss": round(sum(losses) / len(losses), 2) if losses else None,
            "largest_win": max(wins) if wins else None, "largest_loss": min(losses) if losses else None,
            "best": max(closed, key=lambda t: t["net"])["id"] if closed else None, "worst": min(closed, key=lambda t: t["net"])["id"] if closed else None}


def strategy_perf(trades):
    out = {}
    day = now().date().isoformat()
    for t in trades:
        S = out.setdefault(t["strategy"], {"strategy": t["strategy"], "trades": 0, "wins": 0, "losses": 0, "pnl": 0.0, "open": 0, "today": 0, "_eq": [0.0]})
        if t["status"] == "OPEN":
            S["open"] += 1; continue
        n = t.get("net") or 0
        S["trades"] += 1; S["pnl"] += n; S["wins"] += n > 0; S["losses"] += n < 0
        S["today"] += str(t.get("exit_time") or "")[:10] == day
        S["_eq"].append(S["_eq"][-1] + n)
    for S in out.values():
        eq, peak, dd = S.pop("_eq"), 0.0, 0.0
        for x in eq:
            peak = max(peak, x); dd = max(dd, peak - x)
        S.update(pnl=round(S["pnl"], 2), win_rate=round(100 * S["wins"] / S["trades"], 2) if S["trades"] else None,
                 avg_trade=round(S["pnl"] / S["trades"], 2) if S["trades"] else None, max_drawdown=round(dd, 2))
    return sorted(out.values(), key=lambda S: -S["pnl"])


def signal_history(journal, n=40):
    """One row per signal: what happened to it (EXECUTED / REJECTED / INVALIDATED / WAITING), from the journal."""
    rows = {}
    for e in journal.tail(800):
        sid = e.get("signal_id")
        if not sid:
            continue
        r = rows.setdefault(sid, {"signal_id": sid, "time": e["time"], "symbol": e.get("symbol"), "strategy": e.get("strategy"), "signal": "BUY",
                                  "price": e.get("price"), "result": "SEEN", "reason": "", "trade_id": None})
        typ = e["type"]
        if typ == "SIGNAL_GENERATED":
            r.update(time=e["time"], price=e.get("price"), contrib=e.get("contrib"), why=e.get("why"))
        elif typ in ("SIGNAL_REJECTED", "SIGNAL_INVALIDATED") and r["result"] != "EXECUTED":
            r.update(result="REJECTED" if typ == "SIGNAL_REJECTED" else "INVALIDATED", reason=e["msg"])
        elif typ in ("POSITION_OPENED", "ORDER_FILLED") and e.get("trade_id"):
            r.update(result="EXECUTED", trade_id=e["trade_id"], reason="")
    return list(rows.values())[-n:][::-1]


def daily_report(app, trades=None):
    trades = trades if trades is not None else ledger(app)
    P = pnl_today(trades, app.paper.s.get("start"))
    perf = strategy_perf([t for t in trades if str(t.get("exit_time") or "")[:10] == now().date().isoformat() or t["status"] == "OPEN"])
    curve = getattr(app, "curve", [])
    eqs = [c[1] for c in curve]
    peak, dd = (eqs[0] if eqs else 0), 0.0
    for x in eqs:
        peak = max(peak, x); dd = max(dd, peak - x)
    closed = [t for t in perf if t["trades"]]
    return {"date": now().date().isoformat(), "mode": "PAPER", "starting_equity": eqs[0] if eqs else None, "ending_equity": eqs[-1] if eqs else None,
            "capital_used": round(sum(t.get("capital") or 0 for t in trades if str(t.get("entry_time") or "")[:10] == now().date().isoformat()), 2),
            **P, "max_drawdown": round(dd, 2) if eqs else None, "strategies": perf,
            "most_used": max(perf, key=lambda S: S["trades"] + S["open"])["strategy"] if perf else None,
            "best_strategy": closed[0]["strategy"] if closed else None, "worst_strategy": closed[-1]["strategy"] if closed else None}


_STATE_CACHE = {"at": 0.0, "v": None, "lock": threading.Lock()}


def mask_id(x):
    x = str(x or "")
    return ("X" * max(0, len(x) - 4) + x[-4:]) if x else "not set"


def instrument_of(app, sym):
    if sym in SH_INDEX_TOKENS and app.broker == "SHOONYA":
        return "NSE", SH_INDEX_TOKENS[sym]
    for k, v in list((getattr(app, "tokens", None) or {}).items()):
        if v == sym:
            return tuple(k.split("|")) if "|" in k else ("NSE", k)
    return None


def candles_json(app, sym):
    """1-minute candles for the chart: today's history from Shoonya (TPSeries) + candles built from the SAME live ticks the
    algorithm uses, + every algorithm marker from the unified trade record. Nothing is synthesised."""
    bars = app.book.candles(sym)
    key = f"{sym}|{now().date().isoformat()}"
    h = app.hist.get(key)
    note = None
    if (h is None or (not h["c"] and time.time() - h["at"] > 120)) and app.feed and hasattr(app.feed, "tpseries") and app.sh_auth and app.sh_auth.session:
        ins = instrument_of(app, sym)
        if ins:
            st = dt.datetime.combine(now().date(), dt.time(9, 15), IST).timestamp()
            try:
                app.hist[key] = {"c": app.feed.tpseries(ins[0], ins[1], st, time.time()), "at": time.time()}
            except Exception as e:  # noqa
                app.hist[key] = {"c": [], "at": time.time()}; note = f"history not available: {scrub(e, app.cfg)}"
    hist = (app.hist.get(key) or {}).get("c") or []
    first = bars[0][0] if bars else float("inf")
    candles = [c for c in hist if c[0] < first] + bars
    mk, lines = [], []
    for t in ledger(app):
        if t.get("chart_sym") != sym:
            continue
        mk.append({"ts": t["entry_ts"], "price": t["fill_price"], "type": "AUTO_BUY", "text": f"BUY {t['qty']}", "trade_id": t["id"], "mode": t["mode"]})
        if t["status"] == "CLOSED":
            typ = {"STOP_LOSS": "SL", "TRAILING_STOP": "SL", "TARGET": "TARGET", "MANUAL_EXIT": "MANUAL"}.get(t.get("exit_type"), "EXIT")
            mk.append({"ts": t["exit_ts"], "price": t.get("exit_price"), "type": typ, "text": f"{typ} {t.get('net', 0):+,.0f}", "trade_id": t["id"], "mode": t["mode"]})
        else:
            lines += [{"price": t["fill_price"], "kind": "ENTRY", "trade_id": t["id"]}, {"price": t.get("stop"), "kind": "SL", "trade_id": t["id"]}]
            if t.get("target"):
                lines.append({"price": t["target"], "kind": "TARGET", "trade_id": t["id"]})
    for e in app.journal.tail(800):
        if e.get("symbol") == sym and e["type"] in ("SIGNAL_REJECTED", "SIGNAL_INVALIDATED") and e.get("price"):
            mk.append({"ts": e["ts"] / 1000, "price": e["price"], "type": "REJECTED", "text": "signal not executed", "msg": e["msg"], "mode": e["mode"]})
    return {"sym": sym, "tf": "1m", "candles": candles, "markers": [m for m in mk if m["ts"] and m["price"]], "lines": [l for l in lines if l["price"]],
            "source": (f"Shoonya 1-min history ({len(hist)}) + " if hist else "") + f"live ticks ({len(bars)} candles since this program started)",
            "note": note, "last_tick": ist_ms(app.book.get(sym).get("recv"))}


def state_json(app, max_age=0.2):
    """Everything the screens show, built from ONE snapshot of the central state (cached 200 ms so many screens share one build)."""
    with _STATE_CACHE["lock"]:
        if _STATE_CACHE["v"] is not None and time.time() - _STATE_CACHE["at"] < max_age and _STATE_CACHE.get("app") is app:
            return _STATE_CACHE["v"]
        v = _build_state(app)
        _STATE_CACHE.update(at=time.time(), v=v, app=app)
        return v


def _build_state(app):
    snap = app.book.snapshot()
    nowt = time.time()
    order = ["NIFTY", "BANKNIFTY"] + [s for s in app.watch if s not in ("NIFTY", "BANKNIFTY")]
    quotes = []
    for s in order:
        q = snap.get(s, {})
        t = q.get("t")
        quotes.append({"sym": s, "p": q.get("p"), "pc": q.get("pc"), "o": q.get("o"), "h": q.get("h"), "l": q.get("l"), "vwap": q.get("vwap"),
                       "bid": q.get("bid"), "ask": q.get("ask"), "v": q.get("v"), "oi": q.get("oi"), "src": q.get("src"), "seq": q.get("seq"),
                       "time": dt.datetime.fromtimestamp(t, IST).strftime("%H:%M:%S") if t else None,
                       "exch_ts": t, "recv_ts": q.get("recv"), "recv_time": ist_ms(q.get("recv")),
                       "age": round(nowt - t, 1) if t else None,                                  # seconds since the EXCHANGE time
                       "age_ms": round((nowt - q["recv"]) * 1000) if q.get("recv") else None})    # ms since it ARRIVED here
    pos = [{"sym": s, "qty": p["qty"], "avg": p["avg"], "stop": p["stop"], "target": p.get("target"), "ltp": snap.get(s, {}).get("p"),
            "pnl": ((snap.get(s, {}).get("p") or p["avg"]) - p["avg"]) * p["qty"], "trade_id": (p.get("rec") or {}).get("trade_id"),
            "age_ms": round((nowt - snap[s]["recv"]) * 1000) if snap.get(s, {}).get("recv") else None} for s, p in app.paper.s["pos"].items()]
    D = data_state(app)
    trades = ledger(app)
    P = pnl_today(trades, app.paper.s.get("start"))
    open_t = [t for t in trades if t["status"] == "OPEN"]
    pnl_basis = max([snap.get(t["chart_sym"], {}).get("recv") or 0 for t in open_t] or [0]) or None
    invested = sum(p["qty"] * p["avg"] for p in app.paper.s["pos"].values())
    pos_val = sum(p["qty"] * (snap.get(s, {}).get("p") or p["avg"]) for s, p in app.paper.s["pos"].items())
    opt_prem = sum(t["capital"] or 0 for t in open_t if t["kind"] == "OPTION")
    curve = getattr(app, "curve", [])
    lim = float(app.cfg.get("MAX_DAILY_LOSS") or 2000)
    loss = max(0.0, -P["total"])
    start_eq = curve[0][1] if curve else None
    killed, paused = app.safety.killed(), app.safety.paused()
    if not app.feed and not getattr(app, "tokens", None):
        ast, adet = "ERROR", "no broker connection configured (settings.env)"
    elif killed:
        ast, adet = "PAUSED", "KILL SWITCH ON - no new orders"
    elif paused:
        ast, adet = "PAUSED", "paused by you - open positions are still managed (stops/targets)"
    elif app.market != "OPEN":
        ast, adet = "MARKET CLOSED", f"NSE {app.market}"
    elif D["status"] != "LIVE":
        ast, adet = "WAITING", f"DATA NOT FRESH ENOUGH FOR AUTOMATIC TRADING ({D['label']}) - new entries blocked"
    elif (getattr(app, "resync", {}) or {}).get("status") != "COMPLETE":
        ast, adet = "WAITING", f"resync {(app.resync or {}).get('status') or 'not run yet'} - new entries blocked"
    else:
        ast, adet = "RUNNING", "watching live ticks"
    real_on = app.trader.mode() == "REAL"
    recs = load_real()
    pending = [r for r in recs if r.get("status") not in FINAL and r.get("order_id")]
    rec = getattr(app, "recon", {}) or {}
    cd = getattr(app, "decisions", {}) or {}
    algo = {"status": ast, "detail": adet, "mode": "LIVE" if real_on else "PAPER", "mode_requested": app.cfg.get("MODE", "PAPER").upper(),
            "broker": app.broker.title(), "broker_state": (app.feed.health() if app.feed else {}).get("state"), "market": app.market, "data": D["status"],
            "last_scan": ist_ms(app.trader.last_scan), "last_chain_scan": ist_ms(getattr(app, "last_decide", None)),
            "next_scan": "on the next live tick (event-driven); option chain every 0.5 s",
            "watching": list(app.trader.watching.values()), "engine_blocked": app.trader.signals.get("blocked") or [],
            "regime": app.trader.signals.get("regime"), "strategy": app.trader.strat(), "chain": {u: {k: d.get(k) for k in
                ("decision", "reasons", "warnings", "score", "confidence", "contract", "plan", "bias", "spot", "atm", "expiry", "paused", "min_score")} for u, d in cd.items()},
            "health": [{"name": "Data feed", "status": D["status"], "ok": D["status"] in ("LIVE", "MARKET CLOSED")},
                       {"name": "Strategy engine", "status": "RUNNING" if app.trader.last_scan and nowt - app.trader.last_scan < 120 else "IDLE (no tick in 2 min)", "ok": True},
                       {"name": "Risk engine", "status": "ACTIVE" + (" · KILL" if killed else ""), "ok": True},
                       {"name": "Execution", "status": "LIVE broker" if real_on else "PAPER simulator", "ok": True},
                       {"name": "Broker", "status": str((app.feed.health() if app.feed else {}).get("state")), "ok": (app.feed.health() if app.feed else {}).get("state") == "LIVE"},
                       {"name": "Storage", "status": "OK (journal + paper book on this computer)", "ok": True},
                       {"name": "Last event", "status": (app.journal.ev[-1]["time_ms"] if app.journal.ev else "—"), "ok": True}]}
    status_bar = {"data": {"status": D["status"], "age_ms": D["age_ms"]}, "algorithm": ast,
                  "broker": (app.feed.health() if app.feed else {}).get("state") or "NOT STARTED",
                  "orders": ("PENDING " + str(len(pending))) if pending else ("SYNCED" if real_on or recs else "SYNCED (paper: instant, simulated)"),
                  "positions": {"OK": "SYNCED", "MISMATCH": "MISMATCH"}.get(rec.get("status"), "SYNCED (paper)"),
                  "pnl": {"updated": ist_ms(pnl_basis), "age_ms": round((nowt - pnl_basis) * 1000) if pnl_basis else None},
                  "clock": D["clock"]["status"]}
    return {"time": now().strftime("%H:%M:%S"), "served_at_ms": int(nowt * 1000),
            "feed": app.feed.health() if app.feed else {"state": "DISCONNECTED", "last_error": "no credentials"},
            "mode": app.trader.mode(), "mode_requested": app.cfg.get("MODE", "PAPER").upper(), "market": app.market,
            "killed": killed, "paused": paused, "quotes": quotes, "positions": pos,
            "account": {"equity": app.paper.equity(app.book), "cash": app.paper.s["cash"], "closed": len(app.paper.s["trades"]), "today": app.paper.realized_today()},
            "real_blockers": app.safety.real_blockers(app.paper), "gate": app.paper.record(), "signals": app.trader.signals,
            "events": [{"time": e["time"], "kind": e.get("kind") or e["type"].lower(), "msg": e["msg"]} for e in app.journal.ev[-60:] if e.get("kind")],
            "rejected": app.book.rejected, "options": app.opt.state(), "broker_health": broker_health(app),
            "chain": {"decisions": cd, "paper": app.chain_tr.state() if getattr(app, "chain_tr", None) else None,
                      "config": {k: app.cfg.get(k) or v for k, v in CHAIN_DEFAULTS.items()}},
            "data": D, "status_bar": status_bar, "algo": algo, "resync": getattr(app, "resync", {}), "recon": rec,
            "trades": trades[-1500:], "orders": orders_view(app), "pnl": P,
            "profit": {"today": period_report(trades, *period_bounds("today"), capital=app.paper.s.get("start")), "charges_file": os.path.join(HOME, CHARGES_FILE_NAME),
                       "rules_checked": (getattr(app, "charges", None) or {}).get("checked"), "sources": (getattr(app, "charges", None) or {}).get("sources")},
            "who": {"broker": app.broker.title(), "account": mask_id(app.cfg.get("SHOONYA_UID") if app.broker == "SHOONYA" else app.cfg.get("ANGEL_CLIENT_ID")),
                    "scope": "this computer only - your broker login, positions and P&L never leave this PC"},
            "capital": {"total": app.paper.s.get("start"), "available": app.paper.s["cash"], "invested": round(invested, 2),
                        "margin_used": round(invested, 2), "margin_note": "delivery (cash segment, no leverage): margin used = amount invested",
                        "position_value": round(pos_val, 2), "option_premium": round(opt_prem, 2), "equity": round(app.paper.equity(app.book), 2),
                        "funds_broker": getattr(app, "funds", None)},
            "risk": {"start_equity_today": start_eq, "today": P["total"], "limit": lim, "loss_used_pct": round(loss / lim * 100, 1) if lim else None,
                     "status": "DAILY LOSS LIMIT REACHED" if loss >= lim else ("WARNING" if loss >= lim / 2 else "SAFE"),
                     "exposure": round(pos_val + opt_prem, 2), "open_risk": round(sum(max(0, t.get("risk") or 0) for t in open_t), 2),
                     "positions": len(open_t), "largest": max(open_t, key=lambda t: t.get("capital") or 0)["instrument"] if open_t else None,
                     "risk_per_trade": lim / 2, "max_order_value": float(app.cfg.get("MAX_ORDER_VALUE") or 25000),
                     "max_positions": int(float(app.cfg.get("MAX_OPEN_POSITIONS") or 3)), "orders_today": app.safety.orders_today,
                     "max_orders": int(float(app.cfg.get("MAX_ORDERS_PER_DAY") or 3))},
            "curve": curve[-800:], "perf": strategy_perf(trades), "journal": app.journal.tail(250), "signal_history": signal_history(app.journal),
            "report": getattr(app, "report", None) or daily_report(app, trades),
            "config": {k: cfgv(app.cfg, k) for k in DATA_DEFAULTS}}


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
PORTAL_POSTS = ("/kill", "/resume", "/pause", "/unpause", "/paper/close_all", "/orders/cancel_all")
PORTAL_PATHS = ("/api/state", "/stream", "/api/candles", "/api/report") + PORTAL_POSTS
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
            if not self._local() or self.headers.get("Origin") != PORTAL_ORIGIN or self.path.split("?")[0] not in PORTAL_PATHS:
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
            elif self.path.split("?")[0] == "/stream":
                # event-driven: a new state goes out as soon as a tick arrives (at most every 250 ms for the screen; the
                # trading engine is NOT throttled by this), plus a heartbeat every second so the screen can prove it is current
                self.send_response(200); self.send_header("Content-Type", "text/event-stream"); self.send_header("Cache-Control", "no-cache")
                self._cors(); self.end_headers()
                ver, last = -1, 0.0
                try:
                    while True:
                        ver = app.book.wait_change(ver, 1.0)
                        gap = 0.25 - (time.time() - last)
                        if gap > 0:
                            time.sleep(gap)
                        last = time.time()
                        self.wfile.write(("data: " + json.dumps(state_json(app), default=str) + "\n\n").encode()); self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
                    return
            elif self.path.startswith("/api/report"):
                import urllib.parse as up
                q = {k: v[0] for k, v in up.parse_qs(up.urlparse(self.path).query).items()}
                a, z = period_bounds(q.get("period", "today"), q.get("from"), q.get("to"))
                b = json.dumps(period_report(ledger(app), a, z, capital=app.paper.s.get("start")), default=str).encode()
                self.send_response(200); self.send_header("Content-Type", "application/json"); self._cors(); self.end_headers(); self.wfile.write(b)
            elif self.path.startswith("/api/candles"):
                import urllib.parse as up
                sym = (up.parse_qs(up.urlparse(self.path).query).get("sym") or ["NIFTY"])[0]
                b = json.dumps(candles_json(app, sym), default=str).encode(); self.send_response(200); self.send_header("Content-Type", "application/json")
                self._cors(); self.end_headers(); self.wfile.write(b)
            else:
                self.send_error(404)

        def do_POST(self):
            # local-only + custom header: a web page elsewhere cannot trigger these (CSRF protection)
            if not self._local() or self.headers.get("X-Vision") != "1":
                self.send_error(403); return
            origin = self.headers.get("Origin")
            if origin and origin not in LOCAL_ORIGINS and not (origin == PORTAL_ORIGIN and self.path in PORTAL_POSTS):
                self.send_error(403); return                # the portal can only stop/pause/resume and close PAPER positions - never buy
            pause_file = os.path.join(os.path.dirname(KILL_FILE), "PAUSED")
            if self.path == "/kill":
                open(KILL_FILE, "w").write(now().isoformat()); app.trader.log("kill", "KILL SWITCH ON - no new orders")
            elif self.path == "/pause":
                open(pause_file, "w").write(now().isoformat()); app.journal.add("ALGO_PAUSED", "algorithm PAUSED by you - no new entries; open positions still managed", source="user")
            elif self.path == "/unpause":
                if os.path.exists(pause_file):
                    os.remove(pause_file)
                app.journal.add("ALGO_RESUMED", "algorithm RESUMED by you", source="user")
            elif self.path == "/paper/close_all":
                n = app.trader.close_all_paper() + (app.chain_tr.close_all() if getattr(app, "chain_tr", None) else 0)
                app.journal.add("MANUAL_EXIT", f"you closed all PAPER positions ({n})", source="user")
            elif self.path == "/orders/cancel_all":
                n = 0
                for r in load_real():
                    if r.get("order_id") and r.get("status") not in FINAL and app.feed and hasattr(app.feed, "cancel_order"):
                        try:
                            n += bool(app.feed.cancel_order(r["order_id"]))
                        except Exception as e:  # noqa
                            app.journal.add("BROKER_ERROR", f"cancel {r['order_id']} failed: {scrub(e, app.cfg)}", mode="LIVE", source="broker")
                app.journal.add("ORDERS_CANCEL_REQUESTED", f"cancel requested for {n} pending REAL order(s)", mode="LIVE", source="user")
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
        self.broker = (cfg.get("BROKER") or "SHOONYA").upper()
        self.charges = load_charges()
        self.book, self.safety = Book(), Safety(cfg)
        self.paper = Paper(cfg.get("PAPER_CAPITAL") or 100000, cost_pct=cfgv(cfg, "PAPER_COST_PCT"),
                           costs=lambda side, p, q: side_costs(self.charges, self.broker, "EQ_DELIVERY", side, p, q))
        self.journal = Journal()
        self.resync, self.recon, self.gaps, self.funds, self.report = {}, {}, [], None, None
        self.algo_lat, self.hist = collections.deque(maxlen=600), {}
        self.curve_path = os.path.join(HOME, f"curve_{now().date().isoformat()}.json")
        try:
            self.curve = json.load(open(self.curve_path))
        except Exception:  # noqa
            self.curve = []
        self.market, self.feed = "…", None
        self.broker = (cfg.get("BROKER") or "SHOONYA").upper()
        self.sh_auth = ShoonyaAuth(cfg) if self.broker == "SHOONYA" else None
        self.watch = []
        self.tokens = {}
        self.trader = Trader(cfg, self.book, self.paper, self.safety, {}, None, self.notify, journal=self.journal)
        self.trader.data_check = lambda sym: data_check(self, sym)
        self.trader.real_check = lambda: real_check(self)
        self.opt = OptionsTrader(cfg, self.book, self.safety, self.notify)
        self.opt_loaded = None
        self.ipinfo = None
        self.delayed = None
        self.chain_tr = ChainTrader(cfg, self.book, self.notify, journal=self.journal)
        self.chain_tr.rules, self.chain_tr.broker = self.charges, self.broker
        self.chains, self.decisions, self.nfo, self.nfo_day, self.last_decide = {}, {}, None, None, 0.0
        self._chain_lock = threading.Lock()

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

    def refresh_chain(self):
        """Discover the live option chain around ATM for each configured index (contracts only from Shoonya's NFO list),
        re-centre when the index has moved more than 2 strikes, and stream every contract in the window."""
        if self.broker != "SHOONYA" or not self.feed:
            return
        with self._chain_lock:                                             # main loop and resync may both ask
            return self._refresh_chain()

    def _refresh_chain(self):
        unds = [u.strip().upper() for u in (self.cfg.get("OPT_CHAIN_UNDERLYINGS") or CHAIN_DEFAULTS["OPT_CHAIN_UNDERLYINGS"]).split(",") if u.strip()]
        if self.nfo_day != now().date():
            try:
                self.nfo, self.nfo_day = nfo_index_options(self.req, unds), now().date()
            except Exception as e:  # noqa
                self.chain_tr.log(f"option list download failed ({scrub(e, self.cfg)}) - retry in a minute"); return
        n = int(self.cfg.get("OPT_CHAIN_STRIKES") or CHAIN_DEFAULTS["OPT_CHAIN_STRIKES"])
        mdte = int(self.cfg.get("OPT_MIN_DTE") or CHAIN_DEFAULTS["OPT_MIN_DTE"])
        for u in unds:
            spot = self.book.get(u).get("p")
            ch = self.chains.get(u)
            if not spot:
                continue
            if ch:
                ks = [r["strike"] for r in ch["rows"]]
                near = min(ks, key=lambda k: abs(k - spot))
                if abs(ks.index(near) - ks.index(ch["atm"])) <= 2 and ch["expiry"] >= (now().date() + dt.timedelta(days=mdte)).isoformat():
                    continue
            new = chain_window(self.nfo, u, spot, n, mdte)
            if not new:
                continue
            self.chains[u] = new
            more = {f"NFO|{r[ot]['token']}": chain_sym(u, new["expiry"], r["strike"], ot) for r in new["rows"] for ot in ("CE", "PE")}
            if hasattr(self.feed, "add_tokens"):
                self.feed.add_tokens(more)
            self.chain_tr.log(f"{u} option chain: expiry {new['expiry']}, ATM {new['atm']:g}, {len(more)} contracts streaming")

    def run_chain(self):
        """Every 0.5 s (on fresh ticks): CE/PE decision per index; the best BUY (if any) goes to the paper-only chain trader."""
        if not self.chains or time.time() - self.last_decide < 0.5:
            return
        self.last_decide = time.time()
        regime = (self.trader.signals or {}).get("regime")
        for u, ch in self.chains.items():
            try:
                self.decisions[u] = decide(self.book, ch, self.cfg, regime, self.market if self.market in ("OPEN", "CLOSED", "WEEKEND", "HOLIDAY") else market_state())
            except Exception as e:  # noqa
                self.decisions[u] = {"underlying": u, "decision": "NO_TRADE", "reasons": [f"engine error: {e}"], "paused": True}
        o = self.chain_tr.s.get("open")
        if o:
            return self.chain_tr.on_decision(self.decisions.get(o["underlying"]))
        buys = [d for d in self.decisions.values() if str(d.get("decision", "")).startswith("BUY_")]
        self.chain_tr.on_decision(max(buys, key=lambda d: d["score"]) if buys else None)

    def periodic(self):
        """Every 30 s: equity-curve point (market hours), and the daily report once after the close."""
        if self.market == "OPEN":
            tr = ledger(self)
            P = pnl_today(tr, self.paper.s.get("start"))
            ch = self.chain_tr.s if getattr(self, "chain_tr", None) else {"trades": []}
            eq = self.paper.equity(self.book) + sum(t.get("pnl") or 0 for t in ch.get("trades") or []) + sum(t.get("unrealized") or 0 for t in tr if t["kind"] == "OPTION" and t["status"] == "OPEN")
            self.curve.append([int(time.time()), round(eq, 2), P["realized"], P["unrealized"]])
            try:
                json.dump(self.curve[-1200:], open(self.curve_path, "w"))
            except OSError:
                pass
        if now().time() >= dt.time(15, 31) and now().weekday() < 5 and not getattr(self, "_reported", None) == now().date():
            self._reported = now().date()
            self.report = daily_report(self)
            try:
                os.makedirs(os.path.join(HOME, "reports"), exist_ok=True)
                json.dump(self.report, open(os.path.join(HOME, "reports", f"{self.report['date']}.json"), "w"), indent=1, default=str)
            except OSError:
                pass
            r = self.report
            if r["trades"] or any(t["status"] == "OPEN" for t in ledger(self)):
                self.notify("16VITAWS daily report (paper)", f"{r['trades']} trades · net ₹{r['net']:,.0f} · open P&L ₹{r['unrealized']:,.0f}"
                            + (f" · win rate {r['win_rate']}%" if r["win_rate"] is not None else "") + f" · best strategy: {r['best_strategy'] or '-'}")
            self.journal.add("DAILY_REPORT", f"daily report saved: {r['trades']} trades, net ₹{r['net']:,.2f}")

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
        ver, last_slow, last_orders = -1, 0.0, 0.0
        while True:
            ver = self.book.wait_change(ver, 0.5)                          # wakes on the next tick - no fixed polling delay
            try:
                watchdog(self)
            except Exception as e:  # noqa
                self.journal.add("DATA_ERROR", f"data watchdog error: {e}", source="data")
            if time.time() - last_slow > 30:
                last_slow = time.time(); self.periodic()
            if time.time() - last_orders > 5 and real_active(self):
                last_orders = time.time()
                try:
                    sync_orders(self)
                    if time.time() - getattr(self, "_last_recon", 0) > float(cfgv(self.cfg, "RECONCILE_S")):
                        self._last_recon = time.time(); reconcile(self)
                except Exception as e:  # noqa
                    self.journal.add("BROKER_ERROR", f"order/position sync failed: {scrub(e, self.cfg)}", mode="LIVE", source="broker")
            if time.time() - last_portal > 60:
                self.refresh_portal(); last_portal = time.time()
                self.check_ready()
                try:
                    self.refresh_chain()
                except Exception as e:  # noqa
                    self.chain_tr.log(f"option chain error: {scrub(e, self.cfg)}")
            try:
                self.run_chain()
            except Exception as e:  # noqa
                self.chain_tr.log(f"option decision error: {scrub(e, self.cfg)}")
            for s, q in self.book.snapshot().items():                     # act on every symbol that ticked since last pass
                if q.get("recv") != seen.get(s):
                    seen[s] = q.get("recv")
                    if q.get("recv"):
                        self.algo_lat.append((time.time() - q["recv"]) * 1000)   # tick arrived -> algorithm processed it
                    try:
                        if s.startswith("OPT "):
                            self.opt.on_tick(self.market)
                        else:
                            self.trader.on_tick(s, self.market)
                    except Exception as e:  # noqa
                        self.trader.log("error", f"{s}: {e}")


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
