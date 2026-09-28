import sys, os, datetime as dt
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import numpy as np, pandas as pd
import scanner, paper, execution as E, dataquality as DQ, config as C

def mk(n=300, seed=0, drift=0.001):
    rng = np.random.default_rng(seed)
    ix = pd.bdate_range(end=pd.Timestamp("2026-09-25"), periods=n)
    c = 100 * np.exp(np.cumsum(rng.normal(drift, 0.01, n)))
    return pd.DataFrame({"Open": c, "High": c * 1.01, "Low": c * 0.99, "Close": c, "Volume": 1e6}, index=ix)

def test_sma_matches_pandas():
    d = scanner.add_indicators(mk())
    assert abs(d.sma21.iloc[-1] - d.Close.tail(21).mean()) < 1e-9

def test_no_lookahead_in_breakout_levels():
    d = scanner.add_indicators(mk())
    # hi20 on the last bar must not include the last bar's own high
    assert d.hi20.iloc[-1] == d.High.iloc[-21:-1].max()

def test_bear_market_blocks_buys():
    idx = mk(drift=-0.003); data = {f"T{i}": mk(seed=i, drift=0.004) for i in range(5)}
    ctx = scanner.run(data, idx, "test", fetch_news=False, assume_fresh=True)
    assert ctx["mood"]["state"] == "BEAR" and ctx["buys"] == []

def test_position_size_risks_about_one_percent():
    d = scanner.add_indicators(mk()); x = d.iloc[-1]
    p = scanner.plan(x)
    assert p["qty"] * (p["entry"] - p["stop"]) <= C.CAPITAL * C.RISK_PER_TRADE + p["entry"]

def test_risk_gate_rejects_stale_data():
    ok, why = E.risk_gate({"qty": 1, "price": 100, "stop": 95, "side": "buy"}, 200000, 0, 0, None)
    assert not ok and "data" in why or "hours" in why

def test_live_locked_by_default():
    assert C.MODE != "LIVE_ENABLED" and not E.LIVE_MODE

def test_duplicate_order_blocked(tmp_path, monkeypatch):
    monkeypatch.setattr(E, "SENT", str(tmp_path / "sent.json")); monkeypatch.setattr(E, "AUDIT", str(tmp_path / "a.csv"))
    import json; json.dump({"sig-1": {}}, open(E.SENT, "w"))
    r = E.execute({"id": "sig-1", "symbol": "X", "side": "buy", "qty": 1, "price": 100, "stop": 95, "target": 110}, 200000, 0, 0, None)
    assert r["status"] == "duplicate"

def test_data_quality_flags_bad_bars():
    df = mk(); df.iloc[5, df.columns.get_loc("Low")] = df.iloc[5]["High"] + 1
    assert "high/low/close inconsistent" in DQ.check(df)

def test_paper_fill_is_next_open_with_slippage(tmp_path, monkeypatch):
    monkeypatch.setattr(paper, "PF", str(tmp_path / "pf.json")); monkeypatch.setattr(paper, "TRADES", str(tmp_path / "t.csv"))
    df = mk(); data = {"AAA": df}
    mood = {"state": "BULL"}
    pick = {"symbol": "AAA", "qty": 10, "stop": 1, "target": 1e9, "why": ["x"]}
    d0, d1 = df.index[-2], df.index[-1]
    paper.run({"AAA": df[df.index <= d0]}, mood, [pick], d0)      # evening: queue order
    pf, acts = paper.run(data, mood, [], d1)                     # next day: fill at open
    assert pf["positions"][0]["entry"] == round(float(df.loc[d1, "Open"]) * 1.001, 2)
    assert any("BOUGHT" in a for a in acts)
