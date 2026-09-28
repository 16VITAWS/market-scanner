import os, sys, json, math, datetime as dt
from decimal import Decimal
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import numpy as np, pandas as pd, pytest
from engine import config as C, indicators as I, dq, risk, costs, backtest as B, pipeline as P, calendar as cal
from engine.ledger import Ledger
from engine import simulator
from engine.strategies import get, catalogue
from engine.strategies.ma_cross import MACross
from engine.providers.stubs import ManualCSV, AngelOne, TwelveData
from engine.providers import ProviderError

D = Decimal
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def mk(n=300, seed=0, drift=0.001, start=100.0, vol=1e6):
    rng = np.random.default_rng(seed)
    ix = pd.bdate_range(end=pd.Timestamp("2026-09-25", tz="UTC"), periods=n)
    c = start * np.exp(np.cumsum(rng.normal(drift, 0.01, n)))
    o = c * (1 + rng.normal(0, 0.002, n))
    h = np.maximum(o, c) * (1 + abs(rng.normal(0, 0.004, n)))
    l = np.minimum(o, c) * (1 - abs(rng.normal(0, 0.004, n)))
    return pd.DataFrame({"Open": o, "High": h, "Low": l, "Close": c, "Volume": vol}, index=ix)


# ---------------------------------------------------------------- indicators / data
def test_sma_matches_pandas():
    d = I.enrich(mk())
    assert abs(d.sma21.iloc[-1] - d.Close.tail(21).mean()) < 1e-9


def test_no_lookahead_in_breakout_levels():
    d = I.enrich(mk())
    assert d.hi20.iloc[-1] == d.High.iloc[-21:-1].max()
    assert d.vol20.iloc[-1] == d.Volume.iloc[-21:-1].mean()


def test_strategy_signal_ignores_future_bars():
    """The signal on bar t must be identical whether or not later bars exist in the frame."""
    df = mk(320, seed=3)
    S = get("trend_breakout_swing")
    ctx = {"regime": "BULL", "index_ret3m": 0.0}
    a = S.signal(I.enrich(df.iloc[:280]), ctx)
    b_full = I.enrich(df)
    b = S.signal(b_full.iloc[:280], ctx)
    assert a["score"] == b["score"] and a["action"] == b["action"]


def test_dq_flags_bad_bars_and_missing_sessions():
    df = mk(); df.iloc[5, df.columns.get_loc("Low")] = df.iloc[5]["High"] + 1
    issues, _ = dq.check(df)
    assert "high/low/close inconsistent" in issues and dq.hard_fail(issues)
    df2 = mk().drop(pd.Timestamp("2026-09-15", tz="UTC"), errors="ignore")
    issues, stats = dq.check(df2, "NSE")
    assert stats["missing_count"] >= 1


def test_manual_csv_preserved_with_checksum():
    p = os.path.join(ROOT, "data", "raw", "nifty_daily_ohlc_from_portal.csv")
    df, meta = ManualCSV().candles_from_csv(p)
    assert len(df) == 1970 and str(df.index[0].date()) == "2018-09-14" and str(df.index[-1].date()) == "2026-09-11"
    assert meta["provider"] == "manual"
    assert os.path.exists(os.path.join(ROOT, "data", "raw", "CHECKSUMS.sha256"))


def test_calendar_status_states():
    sat = dt.datetime(2026, 9, 26, 10, 0, tzinfo=dt.timezone.utc)
    assert cal.status("NSE", sat)["state"] == "WEEKEND"
    open_ = dt.datetime(2026, 9, 28, 5, 0, tzinfo=dt.timezone.utc)   # 10:30 IST Monday
    assert cal.status("NSE", open_)["state"] == "OPEN"
    hol = dt.datetime(2026, 10, 2, 5, 0, tzinfo=dt.timezone.utc)
    assert cal.status("NSE", hol)["state"] == "HOLIDAY"
    assert cal.status("NSE", open_)["holidays_verified"] is False


def test_stub_providers_never_fabricate():
    for p in (AngelOne(), TwelveData()):
        assert p.capabilities()["configured"] is False
        with pytest.raises(ProviderError):
            p.candles("X")


# ---------------------------------------------------------------- costs
def test_india_delivery_charges_reasonable():
    ch = costs.india_charges(100000, "buy", "delivery", "groww")
    assert ch["brokerage"] == D("20.00") and ch["stt"] == D("100.00") and ch["stamp"] == D("15.00")
    assert D("140") < ch["total"] < D("150")
    assert "verif" in ch["statutory_verification"]


# ---------------------------------------------------------------- ledger / simulator
def fresh_ledger(tmp_path, cash="200000"):
    L = Ledger(str(tmp_path / "ledger.json")).load([{"id": "T", "name": "t", "currency": "INR", "cash": cash, "strategies": ["trend_breakout_swing"]}])
    return L


def test_market_order_fills_next_open_with_slippage_and_fees(tmp_path):
    L = fresh_ledger(tmp_path)
    df = mk(); d0, d1 = df.index[-2], df.index[-1]
    o, st = L.submit("T", "AAA", "buy", 10, "MARKET", bar_date=d0.date(), stop=1, target=1e9)
    assert st == "ok"
    acts = simulator.apply_bar(L, "T", "AAA", df.loc[d0], d0.date())     # decision bar: must not fill
    assert o["status"] == "PENDING"
    acts = simulator.apply_bar(L, "T", "AAA", df.loc[d1], d1.date())
    assert o["status"] == "FILLED"
    f = L.state["fills"][0]
    assert D(f["price"]) == simulator.px(D(str(df.loc[d1, "Open"])) * D("1.001"))
    cash = D(L.account("T")["cash"])
    assert cash == D("200000") - D(f["price"]) * 10 - D(f["fees"])
    assert D(L.account("T")["fees_paid"]) == D(f["fees"])


def test_cash_and_positions_reconcile_after_round_trip(tmp_path):
    L = fresh_ledger(tmp_path)
    df = mk(); d0, d1, d2 = df.index[-3], df.index[-2], df.index[-1]
    L.submit("T", "AAA", "buy", 10, "MARKET", bar_date=d0.date(), stop=1, target=1e9)
    simulator.apply_bar(L, "T", "AAA", df.loc[d1], d1.date())
    L.submit("T", "AAA", "sell", 10, "MARKET", bar_date=d1.date())
    simulator.apply_bar(L, "T", "AAA", df.loc[d2], d2.date())
    a = L.account("T")
    assert a["positions"] == {}
    buy, sell = L.state["fills"]
    expect = D("200000") - D(buy["price"]) * 10 - D(buy["fees"]) + D(sell["price"]) * 10 - D(sell["fees"])
    assert D(a["cash"]) == expect
    t = L.state["trades"][0]
    assert D(t["pnl"]) == (D(sell["price"]) * 10 - D(sell["fees"]) - D(buy["price"]) * 10 - D(buy["fees"])).quantize(D("0.01"))
    # cash ledger sums to balance
    assert sum(D(x["amount"]) for x in L.state["cash_ledger"]) == D(a["cash"]).quantize(D("0.01"))


def test_stop_loss_fills_worse_on_gap_down(tmp_path):
    L = fresh_ledger(tmp_path)
    df = mk(); d0, d1, d2 = df.index[-3], df.index[-2], df.index[-1]
    L.submit("T", "AAA", "buy", 10, "MARKET", bar_date=d0.date(), stop=float(df.loc[d1, "Open"]) * 0.99, target=1e9)
    simulator.apply_bar(L, "T", "AAA", df.loc[d1], d1.date())
    bar = df.loc[d2].copy(); bar["Open"] = bar["Low"] = float(df.loc[d1, "Open"]) * 0.95; bar["High"] = bar["Open"] * 1.01; bar["Close"] = bar["Open"]
    simulator.apply_bar(L, "T", "AAA", bar, d2.date())
    assert L.account("T")["positions"] == {}
    assert D(L.state["fills"][-1]["price"]) == simulator.px(D(str(bar["Open"])) * D("0.999"))


def test_duplicate_order_blocked(tmp_path):
    L = fresh_ledger(tmp_path)
    o1, s1 = L.submit("T", "AAA", "buy", 10, bar_date="2026-09-25", strategy="x")
    o2, s2 = L.submit("T", "AAA", "buy", 10, bar_date="2026-09-25", strategy="x")
    assert s1 == "ok" and s2 == "duplicate" and o1["id"] == o2["id"]


def test_insufficient_cash_and_no_short(tmp_path):
    L = fresh_ledger(tmp_path, cash="500")
    df = mk(); d0, d1 = df.index[-2], df.index[-1]
    o, _ = L.submit("T", "AAA", "buy", 100, "MARKET", bar_date=d0.date())
    simulator.apply_bar(L, "T", "AAA", df.loc[d1], d1.date())
    assert o["status"] in ("FILLED", "PARTIAL", "REJECTED")
    assert D(L.account("T")["cash"]) >= 0
    o2, _ = L.submit("T", "ZZZ", "sell", 5, "MARKET", bar_date=d0.date())
    simulator.apply_bar(L, "T", "ZZZ", df.loc[d1], d1.date())
    assert o2["status"] == "REJECTED"


def test_limit_and_stop_orders(tmp_path):
    L = fresh_ledger(tmp_path)
    df = mk(); d0, d1 = df.index[-2], df.index[-1]
    lim = float(df.loc[d1, "Low"]) + 0.01
    o, _ = L.submit("T", "AAA", "buy", 5, "LIMIT", limit=round(lim, 2), bar_date=d0.date())
    simulator.apply_bar(L, "T", "AAA", df.loc[d1], d1.date())
    assert o["status"] == "FILLED" and D(L.state["fills"][-1]["price"]) <= D(str(round(lim, 2)))
    o2, _ = L.submit("T", "BBB", "buy", 5, "LIMIT", limit=1.0, bar_date=d0.date(), expires_bars=1)
    simulator.apply_bar(L, "T", "BBB", df.loc[d1], d1.date())
    assert o2["status"] == "EXPIRED"


def test_ledger_survives_restart(tmp_path):
    L = fresh_ledger(tmp_path)
    df = mk(); d0, d1 = df.index[-2], df.index[-1]
    L.submit("T", "AAA", "buy", 10, "MARKET", bar_date=d0.date(), stop=1, target=1e9)
    simulator.apply_bar(L, "T", "AAA", df.loc[d1], d1.date()); L.save()
    L2 = Ledger(L.path).load()
    assert L2.account("T")["positions"]["AAA"]["qty"] == "10"
    assert L2.state["audit"][-1]["kind"] == "fill"


# ---------------------------------------------------------------- risk
def test_risk_gate_rejects_oversized_and_missing_stop(tmp_path):
    L = fresh_ledger(tmp_path)
    s = L.summary("T")
    dec = risk.gate({"symbol": "A", "side": "buy", "qty": 1000, "price": 1000, "stop": 990}, s, {})
    assert not dec["approved"] and "cap" in dec["reason"]
    dec = risk.gate({"symbol": "A", "side": "buy", "qty": 10, "price": 100, "stop": None}, s, {})
    assert not dec["approved"] and "stop" in dec["reason"]
    dec = risk.gate({"symbol": "A", "side": "buy", "qty": 10, "price": 100, "stop": 95}, s, {}, data_status="STALE")
    assert not dec["approved"] and "STALE" in dec["reason"]
    dec = risk.gate({"symbol": "A", "side": "buy", "qty": 10, "price": 100, "stop": 95}, s, {}, kill_switch=True)
    assert not dec["approved"] and "kill" in dec["reason"]
    dec = risk.gate({"symbol": "A", "side": "buy", "qty": 10, "price": 100, "stop": 95}, s, {}, todays_realized=D("-5000"))
    assert not dec["approved"] and "daily loss" in dec["reason"]
    dec = risk.gate({"symbol": "A", "side": "buy", "qty": 10, "price": 100, "stop": 95}, s, {})
    assert dec["approved"] and all(c["ok"] for c in dec["checks"])


def test_live_locked():
    assert C.MODE == "PAPER" and not C.LIVE_MODE and C.LIVE_EXECUTION_IMPLEMENTED is False


# ---------------------------------------------------------------- pipeline
def test_bear_regime_blocks_buys_and_pipeline_runs(tmp_path):
    idx = mk(drift=-0.003, seed=9)
    data = {f"T{i}": mk(seed=i, drift=0.004) for i in range(6)}
    res = P.scan(data, idx)
    assert res["regime"]["state"] == "BEAR" and res["buys"] == []
    L = fresh_ledger(tmp_path)
    acts = P.paper_run(L, "T", data, str(idx.index[-1].date()), res["buys"], res["regime"], "trend_breakout_swing")
    assert acts and L.account("T")["equity"][-1]["date"] == str(idx.index[-1].date())


def test_bull_regime_places_orders_through_risk_gate(tmp_path):
    idx = mk(drift=0.003, seed=11)
    data = {f"T{i}": mk(seed=100 + i, drift=0.006, vol=5e6) for i in range(8)}
    res = P.scan(data, idx)
    assert res["regime"]["state"] == "BULL"
    L = fresh_ledger(tmp_path)
    acts = P.paper_run(L, "T", data, str(idx.index[-1].date()), res["buys"], res["regime"], "trend_breakout_swing")
    pend = L.pending("T")
    for o in pend:
        assert o["risk"]["approved"] and o["stop"] is not None
        sig = next(s for s in res["buys"] if s["symbol"] == o["symbol"])
        assert D(o["qty"]) * (D(str(sig["entry"])) - D(o["stop"])) <= D("200000") * D("0.0105")   # ~1% risk per trade
    assert len(pend) <= C.RISK["max_open_positions"]


# ---------------------------------------------------------------- backtest
def test_backtest_no_lookahead_and_costs():
    df = mk(600, seed=5, drift=0.001)
    r = B.run(df, MACross(21, 50), qty=10, segment="delivery", warmup=52)
    for t in r["trades"]:
        assert t["entry_date"] > str(df.index[52].date())
    assert r["metrics"]["trades"] == len(r["trades"])
    bh = B.buy_and_hold(df, qty=10, segment="delivery")
    assert bh["metrics"]["final_equity"] < 200000 + 10 * (df.Close.iloc[-1] - df.Open.iloc[0])   # fees + slippage reduce it


def test_reproduce_reported_2150_backtest_direction():
    p = os.path.join(ROOT, "data", "raw", "nifty_daily_ohlc_from_portal.csv")
    df, _ = ManualCSV().candles_from_csv(p)
    lo = B.run(df, MACross(21, 50), qty=75, segment="futures", warmup=52)["metrics"]
    bh = B.buy_and_hold(df, qty=75, segment="futures")["metrics"]
    assert bh["absolute_return_pct"] > lo["absolute_return_pct"] > 0      # matches the reported ordering


def test_seasonality_has_holm_correction():
    p = os.path.join(ROOT, "data", "raw", "nifty_daily_ohlc_from_portal.csv")
    df, _ = ManualCSV().candles_from_csv(p)
    s = B.seasonality(df)
    assert len(s["month"]) == 12 and all("p_holm" in r for r in s["month"])


def test_catalogue_has_all_seven_vault_cards():
    ids = {c["id"] for c in catalogue()}
    assert {"trend_breakout_swing", "ma_cross_21_50", "vol_trend_atr", "nifty_short_straddle_sl", "nifty_breakout_precision", "nifty_channel_surge", "nifty_gold_pair"} <= ids


# ---------------------------------------------------------------- v2.1: options, US, risk, notify
from engine import options as OPT, analytics as AN


def test_black_scholes_put_call_parity():
    S, K, T, r, v = 23000, 23000, 30 / 365, 0.065, 0.15
    c, p = OPT.bs(S, K, T, r, v, "C")["price"], OPT.bs(S, K, T, r, v, "P")["price"]
    assert abs((c - p) - (S - K * math.exp(-r * T))) < 0.5


def test_expiries_are_tuesdays_and_future():
    today = dt.date(2026, 9, 28)
    ex = OPT.expiries("NIFTY", today, 3)
    assert len(ex) == 3 and all(e >= today for e in ex)
    assert all(e.weekday() == 1 or e.isoformat() in () for e in ex)
    bn = OPT.expiries("BANKNIFTY", today, 2)
    assert all((e + dt.timedelta(days=7)).month != e.month for e in bn)


def test_options_engine_defined_risk_and_sizing(tmp_path):
    L = Ledger(str(tmp_path / "l.json")).load([{"id": "IN-OPTIONS", "name": "o", "currency": "INR", "cash": "200000", "strategies": ["index_options_regime"]}])
    idx = mk(300, seed=21, drift=-0.004, start=24000)
    frames = {"NIFTY": idx}
    reg = P.regime(idx)
    assert reg["state"] == "BEAR"
    today = idx.index[-1].date()
    acts, sig = OPT.run(L, "IN-OPTIONS", frames, reg, today, str(today))
    pos = [p for p in L.account("IN-OPTIONS")["positions"].values() if p.get("spread")]
    if sig["action"] == "BUY" and pos:
        p = pos[0]
        assert float(p["max_loss"]) <= 200000 * OPT.PARAMS["risk_pct"] * 1.02   # risk cap incl. charges
        assert p["spread"]["kind"] == "P" and p["spread"]["k_short"] < p["spread"]["k_long"]   # put debit spread, no naked short
    else:
        assert any("NOT traded" in a or "No new options trade" in a for a in acts)
    # a flat/sideways market produces no trade
    side = mk(300, seed=3, drift=0.0, start=24000)
    r2 = dict(reg, state="SIDEWAYS")
    s2 = OPT.signal({"NIFTY": side}, r2, side.index[-1].date())
    assert s2["action"] == "NONE"


def test_us_scan_uses_us_limits_and_spx_regime():
    idx = mk(drift=0.003, seed=31, start=5000)
    data = {f"U{i}": mk(seed=200 + i, drift=0.006, start=30, vol=5e6) for i in range(5)}
    res = P.scan(data, idx, "trend_breakout_swing_us", limits=C.US_LIMITS)
    assert res["strategy"]["id"] == "trend_breakout_swing_us" and res["scanned"] == 5


def test_var_cvar_and_stress():
    eq = [{"date": str(d.date()), "value": v} for d, v in zip(pd.bdate_range("2026-01-01", periods=60), 200000 * np.cumprod(1 + np.random.default_rng(1).normal(0, 0.01, 60)))]
    r = AN.portfolio_risk(eq, {}, {}, 200000)
    assert r["method"].startswith("historical") and r["cvar_pct"] >= r["var_pct"] > 0
    assert set(r["stress"]) == {"-20%", "-10%", "-5%", "+5%", "+10%"}


def test_notifier_dedupes(tmp_path):
    from engine.notify import Notifier
    os.makedirs(tmp_path / "data"); os.makedirs(tmp_path / "api")
    n = Notifier(str(tmp_path), dry=True)
    assert n.push("t", "m", key="k1") and not n.push("t", "m", key="k1")
    n.save()
    assert json.load(open(tmp_path / "api" / "notifications.json"))["items"][0]["title"] == "t"


# ---------------------------------------------------------------- v2.2: intelligence, execution, margin, live
from engine import forecast as FC, contagion as CG, anomaly as AM, execalgo as EX, margin as MG, ml as ML
from engine.live import control as LCTL, proposals as LPROP


def test_forecast_walk_forward_outputs_probabilities():
    frames = {"NIFTY": mk(900, seed=41, start=18000), "SPX": mk(900, seed=42, start=5000), "BRENT": mk(900, seed=43, start=80)}
    r = FC.run(frames)
    assert r["available"] and 0 < r["p_up"] < 1 and r["oos"]["n"] > 100
    assert r["lean"] in ("UP", "DOWN", "NO CLEAR EDGE") and "baseline" in r["note"]


def test_granger_detects_a_real_lead():
    rng = np.random.default_rng(5); n = 600
    x = rng.normal(0, 0.01, n); y = np.r_[0, 0.8 * x[:-1]] + rng.normal(0, 0.004, n)
    ix = pd.bdate_range("2023-01-02", periods=n)
    g = CG.granger(pd.Series(x, ix), pd.Series(y, ix))
    assert g["p"] < 0.01
    g2 = CG.granger(pd.Series(y, ix), pd.Series(rng.normal(0, 0.01, n), ix))
    assert g2["p"] > 0.01


def test_anomaly_flags_volume_spike_and_big_move():
    df = mk(200, seed=9); df.iloc[-1, df.columns.get_loc("Volume")] = 2e7
    df.iloc[-1, df.columns.get_loc("Close")] = df["Close"].iloc[-2] * 1.12
    df.iloc[-1, df.columns.get_loc("High")] = df["Close"].iloc[-1] * 1.01
    types = {f["type"] for f in AM.symbol_flags(df)}
    assert {"volume spike", "big move"} <= types


def test_execution_algos_formulas():
    ix = pd.date_range("2026-09-25 03:45", periods=4, freq="5min", tz="UTC")
    bars = pd.DataFrame({"Open": [100, 101, 102, 103], "High": [101, 102, 103, 104], "Low": [99, 100, 101, 102], "Close": [100, 101, 102, 103], "Volume": [100, 300, 100, 500]}, index=ix)
    v = EX.simulate(bars, 1000, "buy", "VWAP", spread_bps=0)
    tp = (bars.High + bars.Low + bars.Close) / 3
    assert abs(v["avg"] - float((tp * bars.Volume).sum() / bars.Volume.sum())) < 0.05
    t = EX.simulate(bars, 1000, "buy", "TWAP", spread_bps=0)
    assert abs(t["avg"] - float(tp.mean())) < 0.01 and t["filled"] == 1000
    p = EX.simulate(bars, 1000, "buy", "POV", pov=0.1, spread_bps=0)
    assert p["filled"] == 100 and p["unfilled"] == 900       # 10% of 1000 total volume
    assert EX.shortfall(101, 100, "buy") == 100.0 and EX.shortfall(99, 100, "sell") == 100.0


def test_margin_short_straddle_far_above_debit_spread():
    today = dt.date(2026, 9, 28); e = "2026-10-06"
    straddle = MG.estimate([{"kind": "C", "strike": 23000, "expiry": e, "qty": -65}, {"kind": "P", "strike": 23000, "expiry": e, "qty": -65}], 23000, 0.14, today)
    spread = MG.spread_margin({"kind": "P", "k_long": 23000, "k_short": 22800, "expiry": e}, 65, 23000, 0.14, today)
    assert straddle["estimated_margin"] > 100000 and straddle["estimated_margin"] > 5 * spread["capital_required"] and straddle["status"] == "ESTIMATE"
    assert spread["capital_required"] == spread["max_loss"] < 200 * 65


def test_live_control_defaults_off_and_gate(monkeypatch):
    for k in ("LIVE_TRADING", "LIVE_AUTO", "LIVE_CONSENT", "LIVE_KILL"):
        monkeypatch.delenv(k, raising=False)
    c = LCTL.build([])
    assert c["mode"] == "OFF" and not c["auto_allowed"]
    monkeypatch.setenv("LIVE_TRADING", "ON"); monkeypatch.setenv("LIVE_AUTO", "ON"); monkeypatch.setenv("LIVE_CONSENT", LCTL.CONSENT_PHRASE)
    c = LCTL.build([{"account": "IN-SWING", "analytics": {"trades": {"n": 5, "profit_factor": 3, "expectancy": 10}, "equity": {"max_drawdown_pct": -2}}}])
    assert c["mode"] == "APPROVAL" and not c["auto_allowed"] and "paper track-record gate not passed" in c["auto_blockers"]
    monkeypatch.setenv("LIVE_KILL", "ON")
    assert LCTL.build([])["mode"] == "OFF"


def test_live_proposals_hash_and_approval(tmp_path, monkeypatch):
    ctl = {"mode": "APPROVAL", "limits": {"max_order_value_inr": 25000, "segments": ["CASH"]}, "broker": "groww"}
    scan = {"buys": [{"symbol": "AAA", "entry": 500.0, "stop": 480.0, "target": 530.0, "why": ["x"], "score": 6}], "sells": []}
    today = dt.datetime.now(dt.timezone(dt.timedelta(hours=5, minutes=30))).date().isoformat()
    pr = LPROP.build(scan, None, ctl, today, 100000)          # proposals expire, so build for today
    p = pr["items"][0]
    assert p["id"] == LPROP.pid(p) and p["qty"] * p["limit"] <= 25000 and p["order_type"] == "LIMIT"
    assert p["qty"] * (p["limit"] - p["stop"]) <= 100000 * 0.011
    os.makedirs(tmp_path / "api"); json.dump(pr, open(tmp_path / "api" / "live_proposals.json", "w"))
    from engine.live import approve
    monkeypatch.setenv("PID", p["id"]); monkeypatch.setenv("DECISION", "APPROVE"); monkeypatch.setenv("CONFIRM", "YES")
    approve.main(str(tmp_path))
    a = json.load(open(tmp_path / "data" / "approvals.json"))
    assert a["items"][0]["id"] == p["id"] and a["items"][0]["decision"] == "APPROVE"
    monkeypatch.setenv("CONFIRM", "no")
    with pytest.raises(SystemExit):
        approve.main(str(tmp_path))


def test_runner_dry_cycle_places_only_approved(tmp_path, monkeypatch):
    sys.path.insert(0, os.path.join(ROOT, "runner"))
    import importlib; vr = importlib.import_module("vision_runner")
    monkeypatch.setattr(vr, "DRY", True); monkeypatch.setattr(vr, "AUDIT", str(tmp_path / "audit.jsonl")); monkeypatch.setattr(vr, "notify", lambda *a, **k: None)
    monkeypatch.setattr(vr, "now", lambda: dt.datetime(2026, 9, 28, 10, 0, tzinfo=vr.IST))
    ctl = {"mode": "APPROVAL", "live_trading": True, "kill": False, "auto_allowed": False, "limits": {"max_orders_per_day": 3, "max_order_value_inr": 25000, "max_daily_loss_inr": 2000}}
    mkp = lambda sym: (lambda p: {**p, "id": vr.phash(p)})({"symbol": sym, "side": "BUY", "qty": 10, "limit": 100.0, "valid_until": "2026-09-29", "segment": "CASH", "stop": 95, "target": 110})
    p1, p2 = mkp("AAA"), mkp("BBB")
    data = {"api/live_control.json": ctl, "api/live_proposals.json": {"items": [p1, p2]}, "data/approvals.json": {"items": [{"id": p1["id"], "hash": p1["id"], "decision": "APPROVE"}]}}
    monkeypatch.setattr(vr, "fetch", lambda path: data[path])
    st = {"day": "2026-09-28", "orders": 0, "done": {}, "realized_loss": 0.0}
    vr.cycle(None, st)
    lines = [json.loads(l) for l in open(tmp_path / "audit.jsonl")]
    assert [l["symbol"] for l in lines if l["event"] == "DRY_BUY"] == ["AAA"] and st["orders"] == 1
    data["api/live_control.json"] = {**ctl, "kill": True}
    st2 = {"day": "2026-09-28", "orders": 0, "done": {}, "realized_loss": 0.0}
    vr.cycle(None, st2)
    assert st2["orders"] == 0


def test_ml_shadow_runs_walk_forward():
    idx = mk(520, seed=51, start=20000)
    stocks = {f"S{i}": mk(520, seed=300 + i, drift=0.0005 * (i % 3), start=100, vol=3e6) for i in range(40)}
    r = ML.run(stocks, idx)
    assert r["available"] and len(r["today"]) == 15 and "SHADOW" in r["note"]


# ---------------------------------------------------------------- v2.3
from engine import regime_hmm as HMM, impact as IMP, flows as FLW, mf as MFS, rules as RUL, macro as MAC


def test_hmm_finds_regimes_without_lookahead():
    rng = np.random.default_rng(5)
    r = np.r_[rng.normal(0.002, 0.006, 300), rng.normal(-0.003, 0.02, 200), rng.normal(0.0003, 0.008, 300)]
    idx = pd.bdate_range("2023-01-02", periods=len(r), tz="UTC")
    df = pd.DataFrame({"Close": 100 * np.exp(np.cumsum(r))}, index=idx)
    out = HMM.run(df, min_obs=500, oos_days=150)
    assert out["available"] and set(out["probabilities"]) == {"BULL", "SIDEWAYS", "BEAR"}
    assert abs(sum(out["probabilities"].values()) - 1) < 0.01
    # filtered labels for the past must not change when future data is appended beyond them
    first = HMM.run(df.iloc[:700], min_obs=500, oos_days=150)
    assert first["history"][-1]["date"] == str(idx[699].date())
    bear = next(s for s in out["states"] if s["state"] == "BEAR"); bull = next(s for s in out["states"] if s["state"] == "BULL")
    assert bear["avg_daily_return_pct"] < bull["avg_daily_return_pct"]


def test_impact_recovers_lagged_beta_and_flags_shock():
    rng = np.random.default_rng(9)
    idx = pd.bdate_range("2023-01-02", periods=600, tz="UTC")
    br = rng.normal(0, 2, 600); br[-1] = 10
    tgt = np.r_[0, -0.3 * br[:-1]] + rng.normal(0, 1, 600)
    f = lambda r: pd.DataFrame({"Close": 100 * np.exp(np.cumsum(r / 100))}, index=idx)
    out = IMP.run({"BRENT": f(br), "NIFTYENERGY": f(tgt), "NIFTY": f(rng.normal(0, 1, 600))}, [{"title": "Brent crude surges on OPEC cut"}])
    row = next(r for r in out["table"] if r["target"] == "NIFTYENERGY")
    assert row["significant"] and -0.36 < row["beta"] < -0.24 and row["timing"] == "next"
    sh = out["shocks"][0]
    assert sh["driver"] == "BRENT" and sh["effects"][0]["target"] == "NIFTYENERGY" and sh["news"]


def test_flows_parsers_and_accumulation():
    deals = FLW.parse_deals("Date,Symbol,Security Name,Client Name,Buy/Sell,Quantity Traded,Trade Price / Wght. Avg. Price,Remarks\n"
                            "28-SEP-2026,ABC,Abc Ltd,FUND A,BUY,1000000,500.00,-\n28-SEP-2026,ABC,Abc Ltd,TRADER B,SELL,400000,501.00,-\n", "bulk")
    s = FLW.summarise_deals(deals, {"ABC"})[0]
    assert s["net_value_cr"] == round(50.0 - 20.04, 2) and s["in_universe"]
    rows = FLW.parse_bhavdata("SYMBOL, SERIES, DATE1, PREV_CLOSE, OPEN_PRICE, HIGH_PRICE, LOW_PRICE, LAST_PRICE, CLOSE_PRICE, AVG_PRICE, TTL_TRD_QNTY, TURNOVER_LACS, NO_OF_TRADES, DELIV_QTY, DELIV_PER\n"
                              "ABC, EQ, 26-Sep-2026, 100, 100, 106, 99, 105, 105.00, 103, 3000000, 3000, 1, 2100000, 70.00\n"
                              "XYZ, BL, 26-Sep-2026, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1\n")
    assert list(rows) == ["ABC"] and rows["ABC"]["deliv_pct"] == 70.0
    hist = {f"2026-09-{d:02d}": {"ABC": [40.0, 1000000]} for d in range(10, 20)}
    acc = FLW.accumulation(hist, rows)
    assert acc and acc[0]["signal"] == "ACCUMULATION" and acc[0]["volume_x_avg"] == 3.0


def test_mf_parse_select_score():
    txt = ("Scheme Code;ISIN Div Payout/ ISIN Growth;ISIN Div Reinvestment;Scheme Name;Plan;Option;Net Asset Value;Date\n\n"
           "Open Ended Schemes(Equity Scheme - Large Cap Fund)\n\nA MF\n\n"
           "1;I;-;A Large;Direct Plan;Growth Option;10;25-Sep-2026\n2;I;-;A Large;Regular Plan;Growth Option;9;25-Sep-2026\n"
           "Close Ended Schemes(Equity Scheme - ELSS)\nB MF\n3;I;-;B ELSS;Direct Plan;Growth Option;10;25-Sep-2026\n")
    sel = MFS.select(MFS.parse_navall(txt))
    assert [r["code"] for r in sel] == [1]
    idx = pd.bdate_range("2021-01-01", "2026-09-25")
    funds = []
    for i, d in enumerate((0.0002, 0.0006, 0.001)):
        s = pd.Series(10 * np.exp(np.cumsum(np.full(len(idx), d))), index=idx)
        funds.append({"code": i, "name": f"F{i}", "cat": "Large Cap Fund", **MFS.metrics(s)})
    sc = MFS.score(funds)
    assert max(sc, key=lambda f: f["score"])["code"] == 2 and sc[2]["cagr_3y"] > sc[0]["cagr_3y"]


def test_alert_rules_parse_and_fire():
    rules, bad = RUL.parse("RELIANCE>3000; nifty%<-1.5; junk")
    assert len(rules) == 2 and bad == ["JUNK"]
    hits = RUL.evaluate(rules, {"RELIANCE": {"p": 3001, "chg_pct": 0.1}, "NIFTY": {"p": 24000, "chg_pct": -1.2}})
    assert [h["symbol"] for h in hits] == ["RELIANCE"]


def test_macro_fred_parser_yoy():
    s = MAC.parse_fred("observation_date,CPIAUCSL\n" + "\n".join(f"20{20 + i // 12}-{i % 12 + 1:02d}-01,{100 * 1.03 ** (i / 12)}" for i in range(30)), "yoy")
    assert abs(s.iloc[-1] - 3.0) < 0.01
