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


def test_portal_can_only_stop_resume_and_read_state(VL, tmp_path):
    import threading, urllib.request, urllib.error
    from http.server import ThreadingHTTPServer

    class A:   # minimal app double
        pass
    app = A(); app.trader = type("T", (), {"log": lambda *a, **k: None})(); app.sh_auth = None
    srv = ThreadingHTTPServer(("127.0.0.1", 0), VL.make_handler(app)); port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    def req(path, method="GET", origin=None, xv=True):
        h = {"X-Vision": "1"} if xv else {}
        if origin: h["Origin"] = origin
        r = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method=method, headers=h, data=b"" if method == "POST" else None)
        try:
            with urllib.request.urlopen(r) as resp:
                return resp.status, dict(resp.headers)
        except urllib.error.HTTPError as e:
            return e.code, dict(e.headers)
    P = VL.PORTAL_ORIGIN
    st, h = req("/kill", "OPTIONS", P)
    assert st == 204 and h["Access-Control-Allow-Origin"] == P and h["Access-Control-Allow-Private-Network"] == "true"
    assert req("/kill", "OPTIONS", "https://evil.example")[0] == 403
    assert req("/shoonya/code", "OPTIONS", P)[0] == 403
    st, h = req("/kill", "POST", P)
    assert st == 204 and os.path.exists(VL.KILL_FILE) and h["Access-Control-Allow-Origin"] == P
    assert req("/resume", "POST", P)[0] == 204 and not os.path.exists(VL.KILL_FILE)
    assert req("/kill", "POST", "https://evil.example")[0] == 403 and not os.path.exists(VL.KILL_FILE)
    assert req("/shoonya/code", "POST", P)[0] == 403
    assert req("/kill", "POST", P, xv=False)[0] == 403
    srv.shutdown()


# ---------------- NIFTY options on live Shoonya prices ----------------
SIG = {"action": "BUY", "name": "NIFTY 06OCT26 22400/22200 PE DEBIT SPREAD", "underlying": "NIFTY", "kind": "P", "expiry": "2026-10-06",
       "k_long": 22400, "k_short": 22200, "width": 200, "lot": 65, "why": ["Regime BEAR"]}


def _nfo_zip():
    import io, zipfile
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("NFO_symbols.txt", "Exchange,Token,LotSize,Symbol,TradingSymbol,Expiry,Instrument,OptionType,StrikePrice,TickSize,\n"
                   "NFO,43210,65,NIFTY,NIFTY06OCT26P22400,06-OCT-2026,OPTIDX,PE,22400,0.05,\n"
                   "NFO,43209,65,NIFTY,NIFTY06OCT26P22200,06-OCT-2026,OPTIDX,PE,22200,0.05,\n"
                   "NFO,43211,65,NIFTY,NIFTY06OCT26C22400,06-OCT-2026,OPTIDX,CE,22400,0.05,\n"
                   "NFO,53210,65,NIFTY,NIFTY13OCT26P22400,13-OCT-2026,OPTIDX,PE,22400,0.05,\n"
                   "NFO,63210,30,BANKNIFTY,BANKNIFTY06OCT26P22400,06-OCT-2026,OPTIDX,PE,22400,0.05,\n")
    return buf.getvalue()


class _Z:
    def get(self, url, timeout=None):
        r = _Resp(None); r.content = _nfo_zip(); return r


def _opt(VL, tmp_path, cfg=None):
    cfg = {"OPTIONS_MODE": "PAPER", "MAX_OPTION_RISK": "6000", **(cfg or {})}
    book, safety, sent = VL.Book(), VL.Safety(cfg), []
    o = VL.OptionsTrader(cfg, book, safety, lambda a, b: sent.append((a, b)), path=str(tmp_path / "opt.json"))
    legs = VL.sh_option_legs(_Z(), SIG)
    o.set_signal(SIG, legs)
    L = o.leg_syms()
    def px(lb, sb, t):     # long / short leg last prices (bid=ask=last in this test)
        book.update(L["long"], {"p": lb, "bid": lb, "ask": lb, "t": t}); book.update(L["short"], {"p": sb, "bid": sb, "ask": sb, "t": t})
    return o, book, legs, L, px, sent


def test_option_legs_found_in_shoonya_master(VL):
    legs = VL.sh_option_legs(_Z(), SIG)
    assert legs["long"]["token"] == "43210" and legs["long"]["tsym"] == "NIFTY06OCT26P22400"
    assert legs["short"]["token"] == "43209" and legs["long"]["lot"] == 65
    with pytest.raises(RuntimeError):
        VL.sh_option_legs(_Z(), {**SIG, "k_short": 22100})                       # missing contract -> clear error, no guess


def test_options_paper_entry_and_take_profit_on_live_prices(VL, tmp_path):
    o, book, legs, L, px, sent = _opt(VL, tmp_path)
    assert L["long"] == "OPT NIFTY 06OCT26 22400 PE"
    px(130.0, 58.0, 1.0); o.on_tick()
    op = o.s["open"]
    assert op and op["debit"] == 72.0 and op["qty"] == 65 and op["legs"]["long"] == "NIFTY06OCT26P22400"
    px(150.0, 62.0, 2.0); o.on_tick()                                             # 88: not yet +50% of max profit (72+64=136)
    assert o.s["open"]
    px(190.0, 50.0, 3.0); o.on_tick()                                             # 140 >= 136 -> take profit
    t = o.s["trades"][-1]
    assert not o.s["open"] and "take-profit" in t["why"] and t["pnl"] == round((140 - 72) * 65 - 80, 2)
    px(130.0, 58.0, 4.0); o.on_tick()
    assert not o.s["open"]                                                        # once per day per signal


def test_options_stop_signal_change_restart_and_limits(VL, tmp_path):
    o, book, legs, L, px, _ = _opt(VL, tmp_path)
    px(130.0, 58.0, 1.0); o.on_tick()
    px(80.0, 45.0, 2.0); o.on_tick()                                              # 35 <= 36 -> stop
    assert "stop" in o.s["trades"][-1]["why"]
    o2, book2, legs2, L2, px2, _ = _opt(VL, tmp_path / "b")
    px2(130.0, 58.0, 1.0); o2.on_tick()
    again = VL.OptionsTrader(o2.cfg, book2, o2.safety, lambda a, b: None, path=o2.path)   # restart keeps the open spread + its legs
    assert again.s["open"] and again.legs["long"]["tsym"] == "NIFTY06OCT26P22400"
    again.set_signal({"action": "NONE", "name": "no trade"})
    px2(131.0, 58.0, 2.0); again.on_tick()
    assert not again.s["open"] and "no longer active" in again.s["trades"][-1]["why"]
    o3, _, _, _, px3, _ = _opt(VL, tmp_path / "c", {"MAX_OPTION_RISK": "1000"})
    px3(130.0, 58.0, 1.0); o3.on_tick()
    assert not o3.s["open"] and "MAX_OPTION_RISK" in o3.events[-1]["msg"]
    o4, _, _, _, px4, _ = _opt(VL, tmp_path / "d")
    open(VL.KILL_FILE, "w").write("x"); px4(130.0, 58.0, 1.0); o4.on_tick(); os.remove(VL.KILL_FILE)
    assert not o4.s["open"]


def test_options_real_locked_then_orders_both_legs_safely(VL, tmp_path):
    cfg = {"OPTIONS_MODE": "REAL", "CONSENT": VL.CONSENT_PHRASE, "STATIC_IP_REGISTERED": "yes", "OPTIONS_REAL": "yes", "OPTIONS_MIN_TRADES": "3"}
    o, book, legs, L, px, sent = _opt(VL, tmp_path, cfg)
    assert o.mode() == "PAPER" and "options live-paper record not passed yet" in o.real_blockers()

    class FF:
        def __init__(self): self.orders = []
        def place_order(self, exch, tsym, side, qty, limit, prd):
            self.orders.append((exch, tsym, side, qty, limit, prd)); return f"N{len(self.orders)}"
    o.feed = FF()
    o.s["trades"] = [{"pnl": 500.0}, {"pnl": 400.0}, {"pnl": -200.0}]
    assert o.record()["passed"] and o.mode() == "REAL"
    now_t = time.time()
    px(130.0, 58.0, now_t); o.on_tick()
    assert [x[:4] for x in o.feed.orders] == [("NFO", "NIFTY06OCT26P22400", "BUY", 65), ("NFO", "NIFTY06OCT26P22200", "SELL", 65)]
    assert all(x[5] == "M" for x in o.feed.orders) and o.feed.orders[0][4] == 132.6     # buy limit = ask +2% on the 0.05 tick
    px(190.0, 50.0, now_t + 1); o.on_tick()
    assert [x[1:3] for x in o.feed.orders[2:]] == [("NIFTY06OCT26P22200", "BUY"), ("NIFTY06OCT26P22400", "SELL")]   # short bought back first


def test_shoonya_feed_streams_option_legs(VL, monkeypatch, tmp_path):
    W = pytest.importorskip("websocket")
    sent, book = [], VL.Book()

    class FakeWS:
        def __init__(self, url, on_open=None, on_message=None, on_error=None, **k): self.on_open, self.on_message = on_open, on_message
        def send(self, m): sent.append(json.loads(m))
        def close(self): pass
        def run_forever(self, **k):
            self.on_open(self)
            self.on_message(self, json.dumps({"t": "ak", "s": "OK"}))
            self.on_message(self, json.dumps({"t": "tk", "e": "NFO", "tk": "43210", "lp": "131.25", "ft": "1790000000", "bp1": "131.20", "sp1": "131.30"}))
            feed.stop_flag = True
    monkeypatch.setattr(W, "WebSocketApp", FakeWS)
    monkeypatch.setattr(VL.time, "sleep", lambda s: None)
    a = VL.ShoonyaAuth({"SHOONYA_UID": "FA1"}, _Http({"stat": "Not_Ok"}), path=str(tmp_path / "s.json"))
    a.session = {"access_token": "AT", "uid": "FA1", "actid": "FA1", "day": "2026-10-01"}
    feed = VL.ShoonyaFeed({}, book, {"26000": "NIFTY", "NFO|43210": "OPT NIFTY 06OCT26 22400 PE"}, a)
    feed.run()
    assert sent[1] == {"t": "t", "k": "NSE|26000#NFO|43210"}
    q = book.get("OPT NIFTY 06OCT26 22400 PE")
    assert q["p"] == 131.25 and q["bid"] == 131.2 and q["ask"] == 131.3


def test_login_failure_names_the_new_ip(VL, tmp_path):
    class H:
        def post(self, *a, **k): raise OSError("SSL: UNEXPECTED_EOF_WHILE_READING")
        def get(self, url, timeout=None):
            r = _Resp(None); r.text = "27.61.41.198"; return r
    a = VL.ShoonyaAuth({"SHOONYA_CLIENT_ID": "C", "SHOONYA_SECRET": "S", "SHOONYA_UID": "U"}, H(), path=str(tmp_path / "s.json"))
    with pytest.raises(RuntimeError) as e:
        a.exchange("http://127.0.0.1:8765/shoonya/callback?code=ABC")
    assert "27.61.41.198" in str(e.value) and "Primary IP Address" in str(e.value)


def test_invalid_ip_reply_names_the_new_ip(VL, tmp_path):
    class H:
        def post(self, *a, **k): return _Resp({"stat": "Not_Ok", "emsg": "Invalid Input : INVALID_IP"})
        def get(self, url, timeout=None):
            r = _Resp(None); r.text = "27.61.39.198"; return r
    a = VL.ShoonyaAuth({"SHOONYA_CLIENT_ID": "C", "SHOONYA_SECRET": "S", "SHOONYA_UID": "U"}, H(), path=str(tmp_path / "s.json"))
    with pytest.raises(RuntimeError) as e:
        a.exchange("http://127.0.0.1:8765/shoonya/callback?code=ABC")
    assert "27.61.39.198" in str(e.value) and "INVALID_IP" in str(e.value)


def test_browser_abort_is_silent(VL, capsys):
    srv = VL.QuietServer.__new__(VL.QuietServer)
    try:
        raise ConnectionAbortedError(10053, "aborted")
    except ConnectionAbortedError:
        srv.handle_error(None, ("127.0.0.1", 1))
    assert "Traceback" not in capsys.readouterr().err


class _IPHttp:
    def __init__(self, answers): self.answers, self.asked = answers, []
    def get(self, url, timeout=None, **k):
        self.asked.append(url); a = self.answers.get(url, OSError("down"))
        if isinstance(a, Exception): raise a
        r = _Resp(None); r.text = a + "\n"; return r


def test_public_ip_agreement_disagreement_failure_and_private_rejected(VL):
    S = VL.IP_SOURCES
    ok = VL.check_public_ip(_IPHttp({S[0]: "27.61.43.198", S[1]: "27.61.43.198"}), max_age=0)
    assert ok["ip"] == "27.61.43.198" and ok["status"] == "OK"
    bad = VL.check_public_ip(_IPHttp({S[0]: "27.61.43.198", S[1]: "49.205.1.2"}), max_age=0)
    assert bad["ip"] is None and bad["status"] == "UNCERTAIN"                     # never guess
    none = VL.check_public_ip(_IPHttp({}), max_age=0)
    assert none["ip"] is None and none["status"] == "LOOKUP_FAILED"
    priv = VL.check_public_ip(_IPHttp({S[0]: "192.168.1.25", S[1]: "127.0.0.1", S[2]: "10.0.0.4", S[3]: "172.16.0.9"}), max_age=0)
    assert priv["status"] == "LOOKUP_FAILED"                                      # local addresses are never offered for Shoonya
    assert "did not answer" in (VL._IP_CACHE.update(at=0, res=None) or VL.ip_text(_IPHttp({})))


def test_ip_cache_is_not_polled_continuously(VL):
    h = _IPHttp({VL.IP_SOURCES[0]: "27.61.43.198", VL.IP_SOURCES[1]: "27.61.43.198"})
    VL.check_public_ip(h, max_age=0); n = len(h.asked)
    for _ in range(50):
        VL.check_public_ip(h, max_age=120)
    assert len(h.asked) == n


def test_login_errors_are_classified_and_config_errors_say_retry_off(VL, tmp_path):
    assert VL.classify_login_error("Invalid Input : INVALID_IP") == "INVALID_IP"
    assert VL.classify_login_error("Invalid client id") == "CONFIG"
    assert VL.classify_login_error("auth code expired") == "AUTH_CODE"
    class H:
        def post(self, *a, **k): return _Resp({"stat": "Not_Ok", "emsg": "Invalid Input : INVALID_IP"})
        def get(self, url, timeout=None): r = _Resp(None); r.text = "27.61.43.198"; return r
    a = VL.ShoonyaAuth({"SHOONYA_CLIENT_ID": "C", "SHOONYA_SECRET": "S", "SHOONYA_UID": "U"}, H(), path=str(tmp_path / "s.json"))
    for _ in range(3):
        with pytest.raises(RuntimeError) as e:
            a.exchange("code=ABC")
    assert a.status() == "FAILED" and a.fail_reason == "INVALID_IP" and a.fail_count == 3 and "Automatic retry: OFF" in str(e.value)


def test_successful_login_remembers_accepted_ip_and_health_reports_mismatch(VL, tmp_path, monkeypatch):
    monkeypatch.setattr(VL, "SH_IP_FILE", str(tmp_path / "shoonya_ip.json"))
    class H:
        def post(self, *a, **k): return _Resp({"access_token": "T", "USERID": "U"})
        def get(self, url, timeout=None): r = _Resp(None); r.text = "27.61.41.198"; return r
    a = VL.ShoonyaAuth({"SHOONYA_CLIENT_ID": "C", "SHOONYA_SECRET": "S", "SHOONYA_UID": "U"}, H(), path=str(tmp_path / "s.json"))
    a.exchange("code=ABC")
    assert VL.accepted_ip() == "27.61.41.198" and a.status() == "LOGGED IN"
    from types import SimpleNamespace as NS
    a.session = None; a.fail_reason, a.fail_msg = "INVALID_IP", "x"
    app = NS(cfg={"SHOONYA_CLIENT_ID": "C", "SHOONYA_SECRET": "S"}, sh_auth=a, feed=None, book=VL.Book(), ipinfo={"ip": "27.61.43.198", "status": "OK"},
             tokens={"1": "NIFTY"}, broker="SHOONYA", trader=NS(mode=lambda: "PAPER"))
    h = VL.broker_health(app)
    assert h["ip_status"] == "MISMATCH" and h["registered_ip"] == "27.61.41.198" and h["market_data"].startswith("NOT LIVE")
    assert h["real_orders"] == "BLOCKED" and h["paper_engine"] == "RUNNING" and h["token"] == "absent"
    assert h["secret"] == "configured" and "S" not in str(h["client_id"]) + str(h["secret"]).replace("configured", "")


def test_paper_and_alert_orders_never_reach_the_broker(VL, tmp_path):
    calls = []
    class Trap:
        state = "LIVE"
        def place_limit(self, *a, **k): calls.append(a); raise AssertionError("PAPER sent a real order")
        def place_order(self, *a, **k): calls.append(a); raise AssertionError("PAPER sent a real order")
    for mode in ("PAPER", "ALERT", "REAL"):                                        # REAL requested but locked -> must behave as PAPER
        t, book, paper, _ = _trader(VL, {"MODE": mode}); t.feed = Trap()
        book.update("TCS", {"p": 3405.0, "t": 2.0}); t.on_tick("TCS")
        book.update("TCS", {"p": 3329.0, "t": 3.0}); t.on_tick("TCS")
        assert paper.s["trades"], mode
    for mode in ("PAPER", "ALERT"):
        o, book, legs, L, px, _ = _opt(VL, tmp_path / mode, {"OPTIONS_MODE": mode}); o.feed = Trap()
        px(130.0, 58.0, 1.0); o.on_tick(); px(190.0, 50.0, 2.0); o.on_tick()
        assert o.s["trades"], mode
    assert calls == []


def test_diagnose_report_never_prints_secrets(VL, tmp_path, monkeypatch):
    monkeypatch.setattr(VL, "SH_SESSION", str(tmp_path / "none.json")); monkeypatch.setattr(VL, "SH_IP_FILE", str(tmp_path / "ip.json"))
    lines = []
    h = _IPHttp({VL.IP_SOURCES[0]: "27.61.43.198", VL.IP_SOURCES[1]: "27.61.43.198"})
    VL.diagnose({"SHOONYA_UID": "FN0001", "SHOONYA_CLIENT_ID": "FN0001_U", "SHOONYA_SECRET": "TOPSECRETVALUE123", "SHOONYA_REGISTERED_IP": "27.61.41.198"}, h, lines.append)
    txt = "\n".join(lines)
    assert "TOPSECRETVALUE123" not in txt and "27.61.43.198" in txt and "MISMATCH" in txt


def test_delayed_portal_prices_keep_paper_working_and_never_trade_real(VL):
    class H:
        def __init__(self, j): self.j = j
        def get(self, url, timeout=None):
            r = _Resp(self.j); return r
    t, book, paper, _ = _trader(VL)
    f = VL.PortalDelayedFeed(H({"quotes": {"TCS": {"p": 3405.0, "pc": 3390.0, "t": "2026-10-01T04:50:00+00:00"}}}), book, lambda: ["TCS"], lambda: True)
    assert f.pull() == 1 and book.get("TCS")["src"] == "portal-delayed"
    assert f.pull() == 0                                                           # same quote again: nothing new
    t.on_tick("TCS")
    assert paper.s["pos"]["TCS"]["qty"] == 7                                       # PAPER still trades, on delayed prices
    calls = []
    class Trap:
        def place_limit(self, *a, **k): calls.append(a)
        place_order = place_limit
    os.remove(VL.PAPER_FILE)                                                       # fresh paper book for the REAL check
    tr, b2, p2, _ = _trader(VL, {"MODE": "REAL", "CONSENT": VL.CONSENT_PHRASE, "STATIC_IP_REGISTERED": "yes"}); tr.feed = Trap()
    tr.mode = lambda: "REAL"                                                       # even if REAL were unlocked...
    b2.update("TCS", {"p": 3405.0, "t": 5.0, "src": "portal-delayed"}); tr.on_tick("TCS")
    assert calls == [] and not p2.s["pos"]                                         # ...delayed prices never place real orders


def test_ready_for_real_money_alert_fires_once_and_rearms(VL, tmp_path):
    from types import SimpleNamespace as NS
    sent, logs = [], []
    app = VL.App.__new__(VL.App)
    good = {"passed": True, "checks": [{"check": ">= 30 closed live-paper trades", "value": 31, "ok": True}]}
    state = {"rec": good}
    app.paper = NS(record=lambda: state["rec"])
    app.opt = NS(record=lambda: {"passed": False, "closed": 3, "need": 20, "pf": None})
    app.trader = NS(signals={"regime": "BULL"}, log=lambda k, m, **kw: logs.append(m))
    app.notify = lambda t, m: sent.append((t, m))
    app.check_ready(); app.check_ready()
    assert len(sent) == 1 and "ready for real money" in sent[0][0] and "BULL" in sent[0][1] and "not a" in sent[0][1]
    state["rec"] = {"passed": False, "checks": []}
    app.check_ready()
    assert "weakened" in sent[-1][0]
    state["rec"] = good; app.check_ready()
    assert sum("ready for real money" in t for t, _ in sent) == 2
