import os, sys, json, datetime as dt
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
