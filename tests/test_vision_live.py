"""VISION LIVE runner: tick parsing (official SDK parser, real binary layout), validation, algorithm on live ticks, safety locks.
All brokers/feeds here are test doubles - development only; production uses the real Angel One feed."""
import os, sys, struct, json, time, datetime as dt, importlib
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "runner"))


@pytest.fixture()
def VL(tmp_path, monkeypatch):
    import vision_live as m
    importlib.reload(m)
    for k, v in {"HOME": str(tmp_path), "PAPER_FILE": str(tmp_path / "p.json"), "REAL_FILE": str(tmp_path / "r.json"),
                 "AUDIT": str(tmp_path / "a.jsonl"), "KILL_FILE": str(tmp_path / "KILL")}.items():
        monkeypatch.setattr(m, k, v)
    monkeypatch.setattr(m, "now", lambda: dt.datetime(2026, 10, 1, 10, 30, tzinfo=m.IST))
    return m


def test_parse_real_binary_snapquote_with_official_parser(VL):
    SW = pytest.importorskip("SmartApi.smartWebSocketV2").SmartWebSocketV2
    tok = b"2885".ljust(25, b"\x00")
    pkt = struct.pack("<BB", 3, 1) + tok + struct.pack("<qqq", 7, 1790000000000, 140050)          # mode 3, nse_cm, seq, ts ms, LTP paise
    pkt += struct.pack("<qqq", 10, 139820, 123456) + struct.pack("<dd", 1000.0, 2000.0)             # ltq, avg price, volume, buy/sell qty
    pkt += struct.pack("<qqqq", 139200, 140500, 138800, 139000)                                     # open, high, low, close (paise)
    pkt += b"\x00" * 400
    m = SW._parse_binary_data(SW.__new__(SW), pkt)
    d = VL.parse_tick(m)
    assert str(m["token"]) == "2885" and d["p"] == 1400.50 and d["pc"] == 1390.0 and d["h"] == 1405.0 and d["vwap"] == 1398.2
    assert d["t"] == 1790000000.0 and d["v"] == 123456


def test_book_rejects_bad_ticks_keeps_previous(VL):
    b = VL.Book()
    assert b.update("TCS", {"p": 3400.0, "pc": 3380.0, "h": 3410.0, "l": 3370.0, "t": 100.0})
    assert not b.update("TCS", {"p": 5000.0, "pc": 3380.0, "t": 101.0})                            # beyond 20% band
    assert not b.update("TCS", {"p": 3401.0, "t": 99.0})                                            # out of order
    assert not b.update("TCS", {"p": -1, "t": 102.0})
    assert not b.update("TCS", {"p": 3500.0, "h": 3410.0, "l": 3370.0, "t": 103.0})                 # outside day range
    assert b.get("TCS")["p"] == 3400.0 and len(b.rejected) == 4


def _trader(VL, cfg=None, capital=100000):
    cfg = {"MODE": "PAPER", "MAX_ORDER_VALUE": "25000", "MAX_DAILY_LOSS": "2000", "MAX_ORDERS_PER_DAY": "3", "MAX_OPEN_POSITIONS": "3", **(cfg or {})}
    book, paper, safety = VL.Book(), VL.Paper(capital), VL.Safety(cfg)
    sent = []
    t = VL.Trader(cfg, book, paper, safety, {"TCS": "11536"}, None, lambda a, b: sent.append((a, b)))
    t.set_signals({"buys": [{"symbol": "TCS", "entry": 3400.0, "stop": 3330.0, "target": 3540.0, "score": 5}], "sells": [], "data_status": "OK"})
    return t, book, paper, sent


def test_algorithm_enters_near_entry_and_exits_on_live_stop(VL):
    t, book, paper, _ = _trader(VL)
    book.update("TCS", {"p": 3450.0, "t": 1.0}); t.on_tick("TCS")                                     # 1.5% above entry: never chase
    assert not paper.s["pos"]
    book.update("TCS", {"p": 3405.0, "t": 2.0}); t.on_tick("TCS")
    pos = paper.s["pos"]["TCS"]
    assert pos["qty"] == 7 and pos["stop"] == 3330.0                                                # min(25000//3405=7, 1000//75=13)
    book.update("TCS", {"p": 3329.0, "t": 3.0}); t.on_tick("TCS")                                     # live price through the stop
    assert not paper.s["pos"] and paper.s["trades"][0]["pnl"] < 0 and "stop-loss" in paper.s["trades"][0]["why"]
    book.update("TCS", {"p": 3401.0, "t": 4.0}); t.on_tick("TCS")                                     # no re-entry the same day
    assert not paper.s["pos"]


def test_limits_kill_switch_and_closed_market(VL):
    t, book, paper, _ = _trader(VL, {"MAX_ORDER_VALUE": "1000"})
    book.update("TCS", {"p": 3400.0, "t": 1.0}); t.on_tick("TCS")
    assert not paper.s["pos"]                                                                         # < 1 share within value limit
    t2, book2, paper2, _ = _trader(VL)
    open(VL.KILL_FILE, "w").write("x")
    book2.update("TCS", {"p": 3400.0, "t": 1.0}); t2.on_tick("TCS")
    assert not paper2.s["pos"] and any("kill" in e["msg"] for e in t2.events)
    os.remove(VL.KILL_FILE)
    t3, book3, paper3, _ = _trader(VL)
    book3.update("TCS", {"p": 3400.0, "t": 1.0}); t3.on_tick("TCS", state="CLOSED")
    assert not paper3.s["pos"]


def test_real_mode_stays_locked_until_every_condition_holds(VL):
    t, book, paper, sent = _trader(VL, {"MODE": "REAL", "CONSENT": VL.CONSENT_PHRASE, "STATIC_IP_REGISTERED": "yes"})
    assert t.mode() == "PAPER" and "live-paper track record gate not passed" in t.safety.real_blockers(paper)

    class FakeFeed:                                                                                   # test double, never a real broker
        def __init__(self): self.orders = []
        def place_limit(self, sym, side, qty, limit): self.orders.append({"sym": sym, "side": side, "qty": qty, "limit": limit}); return "OID1"
    t.feed = FakeFeed()
    for i in range(30):                                                                              # a passing live-paper record
        paper.s["trades"].append({"pnl": 100.0 if i % 3 else -50.0, "closed": "2026-09-01T10:00:00"})
    assert paper.record()["passed"] and t.mode() == "REAL"
    book.update("TCS", {"p": 3402.0, "t": time.time()}); t.on_tick("TCS")
    o = t.feed.orders[0]
    assert o["side"] == "BUY" and o["limit"] == 3419.05 and o["sym"] == "TCS"
    t2, *_ = _trader(VL, {"MODE": "REAL", "CONSENT": "yes please", "STATIC_IP_REGISTERED": "yes"})
    assert "CONSENT phrase not set" in t2.safety.real_blockers(t2.paper)


def test_alert_mode_notifies_and_settings_template(VL, tmp_path):
    t, book, paper, sent = _trader(VL, {"MODE": "ALERT"})
    book.update("TCS", {"p": 3400.0, "t": 1.0}); t.on_tick("TCS")
    assert paper.s["pos"] and sent and sent[0][0] == "BUY TCS now"
    cfg = VL.load_settings(str(tmp_path / "s.env"))
    assert cfg["MODE"] == "PAPER" and cfg["STATIC_IP_REGISTERED"] == "no" and cfg["ANGEL_API_KEY"] == ""
    assert VL.totp("GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ", t=59) == "287082"
    assert VL.scrub("error for key ABCD1234xyz", {"ANGEL_API_KEY": "ABCD1234xyz"}) == "error for key ***"



# ---------------- Shoonya (Finvasia): requests compared byte-for-byte with the official NorenRestApiOAuth SDK ----------------
class _Resp:
    def __init__(self, j): self._j, self.content, self.status_code, self.text = j, b"x", 200, json.dumps(j)
    def json(self): return self._j
    def raise_for_status(self): pass


class _Http:                                                   # test double for requests - records what would be sent
    def __init__(self, reply): self.reply, self.sent = reply, []
    def post(self, url, data=None, headers=None, timeout=None):
        self.sent.append({"url": url, "data": data, "headers": headers}); return _Resp(self.reply)


def test_shoonya_token_exchange_matches_official_sdk(VL, monkeypatch, tmp_path):
    reply = {"access_token": "AT1", "USERID": "FA1234", "refresh_token": "RT", "actid": "FA1234", "susertoken": "ST", "stat": "Ok"}
    cfg = {"SHOONYA_CLIENT_ID": "CID_API", "SHOONYA_SECRET": "sEcReT", "SHOONYA_UID": "FA1234"}
    http = _Http(reply)
    a = VL.ShoonyaAuth(cfg, http, path=str(tmp_path / "sess.json"))
    a.exchange("http://127.0.0.1:8765/shoonya/callback?code=AUTHC0DE&state=x")
    mine = http.sent[0]
    sdk_mod = pytest.importorskip("NorenRestApiPy.NorenApi")
    got = {}
    monkeypatch.setattr(sdk_mod.requests, "post", lambda url, data=None, **k: got.update(url=url, data=data) or _Resp(reply))
    sdk_mod.NorenApi("https://api.shoonya.com/NorenWClientAPI/", "wss://api.shoonya.com/NorenWSAPI/").getAccessToken("AUTHC0DE", "sEcReT", "CID_API", "FA1234")
    assert mine["data"] == got["data"]                                                   # same jData, same sha256 checksum
    assert mine["url"].replace("//GenAcsTok", "/GenAcsTok") == got["url"].replace("//GenAcsTok", "/GenAcsTok")
    assert a.session["access_token"] == "AT1" and json.load(open(tmp_path / "sess.json"))["day"] == "2026-10-01"
    assert a.headers()["Authorization"] == "Bearer AT1"
    assert "client_id=CID_API" in a.login_url()


def test_shoonya_order_matches_official_sdk(VL, monkeypatch, tmp_path):
    cfg = {"SHOONYA_CLIENT_ID": "C", "SHOONYA_SECRET": "S", "SHOONYA_UID": "FA1234"}
    http = _Http({"stat": "Ok", "norenordno": "2610010001"})
    a = VL.ShoonyaAuth(cfg, http, path=str(tmp_path / "s.json"))
    a.session = {"access_token": "AT1", "uid": "FA1234", "actid": "FA1234", "day": "2026-10-01"}
    f = VL.ShoonyaFeed(cfg, VL.Book(), {"11536": "TCS"}, a)
    assert f.place_limit("TCS", "BUY", 7, 3419.05) == "2610010001"
    mine = json.loads(http.sent[0]["data"][len("jData="):])
    sdk_mod = pytest.importorskip("NorenRestApiPy.NorenApi")
    got = {}
    monkeypatch.setattr(sdk_mod.requests, "post", lambda url, data=None, headers=None, **k: got.update(url=url, data=data, headers=headers) or _Resp({"stat": "Ok"}))
    api = sdk_mod.NorenApi("https://api.shoonya.com/NorenWClientAPI/", "wss://api.shoonya.com/NorenWSAPI/")
    api.injectOAuthHeader("AT1", "FA1234", "FA1234")
    api.place_order(buy_or_sell="B", product_type="C", exchange="NSE", tradingsymbol="TCS-EQ", quantity=7, discloseqty=0,
                    price_type="LMT", price="3419.05", retention="DAY", remarks="vision_live")
    sdk = json.loads(got["data"][len("jData="):])
    for k in ("uid", "actid", "trantype", "prd", "exch", "tsym", "qty", "dscqty", "prctyp", "prc", "ret", "ordersource"):
        assert mine[k] == sdk[k], k
    assert http.sent[0]["headers"]["Authorization"] == got["headers"]["Authorization"]
    assert http.sent[0]["url"].endswith("/PlaceOrder") and got["url"].endswith("/PlaceOrder")


def test_shoonya_ticks_and_websocket_flow(VL, monkeypatch, tmp_path):
    full = {"t": "tk", "e": "NSE", "tk": "11536", "lp": "3402.50", "pc": "0.52", "o": "3385.00", "h": "3416.10", "l": "3374.85",
            "c": "3385.00", "v": "123456", "ap": "3399.10", "ft": "1790000000", "bp1": "3402.45", "sp1": "3402.55"}
    d = VL.sh_tick(full)
    assert d["p"] == 3402.5 and d["pc"] == 3385.0 and d["vwap"] == 3399.1 and d["bid"] == 3402.45 and d["t"] == 1790000000.0 and d["v"] == 123456
    assert VL.sh_tick({"t": "tf", "tk": "11536", "lp": "3403.00"}) == {"p": 3403.0, "o": None, "h": None, "l": None, "pc": None, "vwap": None, "bid": None, "ask": None}
    assert VL.sh_code_from("https://x/cb?code=ABC123&state=1") == "ABC123" and VL.sh_code_from(" ABC123 ") == "ABC123"

    sent, book = [], VL.Book()
    W = pytest.importorskip("websocket")

    class FakeWS:                                                    # test double for websocket.WebSocketApp
        def __init__(self, url, on_open=None, on_message=None, on_error=None, **k): self.on_open, self.on_message, self.url = on_open, on_message, url
        def send(self, m): sent.append(json.loads(m))
        def close(self): pass
        def run_forever(self, **k):
            assert k.get("ping_payload") == '{"t":"h"}' and "sslopt" not in k        # heartbeat per SDK; TLS verification left on
            self.on_open(self)
            self.on_message(self, json.dumps({"t": "ak", "s": "OK"}))
            self.on_message(self, json.dumps(full))
            self.on_message(self, json.dumps({"t": "tf", "tk": "11536", "lp": "3404.00", "ft": "1790000005"}))
            self.on_message(self, json.dumps({"t": "tf", "tk": "11536", "lp": "9000.00", "ft": "1790000006"}))    # impossible -> rejected
            feed.stop_flag = True
    monkeypatch.setattr(W, "WebSocketApp", FakeWS)
    cfg = {"SHOONYA_UID": "FA1234"}
    a = VL.ShoonyaAuth(cfg, _Http({"stat": "Not_Ok"}), path=str(tmp_path / "s.json"))
    a.session = {"access_token": "AT1", "uid": "FA1234", "actid": "FA1234", "day": "2026-10-01"}
    feed = VL.ShoonyaFeed(cfg, book, {"11536": "TCS", "26000": "NIFTY"}, a)
    monkeypatch.setattr(VL.time, "sleep", lambda s: None)
    feed.run()
    assert sent[0] == {"t": "a", "uid": "FA1234", "actid": "FA1234", "accesstoken": "AT1", "source": "API"}
    assert sent[1] == {"t": "t", "k": "NSE|11536#NSE|26000"}
    q = book.get("TCS")
    assert q["p"] == 3404.0 and q["pc"] == 3385.0 and q["src"] == "shoonya-ws" and book.rejected[-1]["reason"] == "move beyond NSE price band"


def test_shoonya_symbol_master_and_session_reuse(VL, tmp_path):
    import io, zipfile
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("NSE_symbols.txt", "Exchange,Token,LotSize,Symbol,TradingSymbol,Instrument,TickSize,\nNSE,11536,1,TCS,TCS-EQ,EQ,0.05,\nNSE,2885,1,RELIANCE,RELIANCE-EQ,EQ,0.05,\nNSE,999,1,TCS,TCS-BE,EQ,0.05,\n")
    class H:
        def get(self, url, timeout=None):
            r = _Resp(None); r.content = buf.getvalue(); return r
    assert VL.sh_symbol_tokens(H(), ["NIFTY", "TCS", "RELIANCE", "NOPE"]) == {"NIFTY": "26000", "TCS": "11536", "RELIANCE": "2885"}
    p = tmp_path / "sess.json"
    json.dump({"day": "2026-10-01", "access_token": "AT", "uid": "U", "actid": "U"}, open(p, "w"))
    assert VL.ShoonyaAuth({}, None, path=str(p)).session["access_token"] == "AT"             # same day: no new login needed
    json.dump({"day": "2026-09-30", "access_token": "OLD", "uid": "U", "actid": "U"}, open(p, "w"))
    assert VL.ShoonyaAuth({}, None, path=str(p)).session is None                              # new day: log in again


def test_live_paper_record_survives_restart(VL):
    p = VL.Paper(100000)
    p.buy("TCS", 5, 3400.0, 3330.0, 3540.0, "test")
    p.sell("TCS", 3500.0, "target")
    again = VL.Paper(100000)                                              # a new program start reads the same file
    assert len(again.s["trades"]) == 1 and again.s["trades"][0]["sym"] == "TCS" and again.s["cash"] == p.s["cash"]
