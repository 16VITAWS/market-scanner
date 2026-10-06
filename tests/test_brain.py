"""Trading Brain: every layer tested on its own, plus an end-to-end run on synthetic data."""
import os, tempfile, datetime as dt
import numpy as np
import pandas as pd
import pytest
from engine.brain import features as F, regime as RG, strategies as S, calibrate as CA, score as SC, money as MO, montecarlo as MC, daybook as DB, paper as PA
from engine.brain import engine as BE
from engine.ledger import Ledger

DATES = pd.bdate_range("2023-01-02", periods=700, tz="UTC")


def walk(seed, drift=0.0004, vol=0.015, p0=500, n=len(DATES)):
    r = np.random.default_rng(seed)
    c = p0 * np.exp(np.cumsum(r.normal(drift, vol, n)))
    o = c * np.exp(r.normal(0, vol / 3, n))
    h = np.maximum(o, c) * (1 + abs(r.normal(0, vol / 2, n)))
    l = np.minimum(o, c) * (1 - abs(r.normal(0, vol / 2, n)))
    return pd.DataFrame({"Open": o, "High": h, "Low": l, "Close": c, "Volume": r.integers(300000, 3000000, n).astype(float)}, index=DATES[:n])


# ---------------------------------------------------------------- features: no look-ahead
def test_features_use_only_past_bars():
    df, ix = walk(1), walk(2, p0=20000)
    full = F.compute(df, ix["Close"])
    part = F.compute(df.iloc[:500], ix["Close"].iloc[:500])
    a, b = full.iloc[499], part.iloc[-1]
    for k in ("ema20", "atr", "rsi", "don_hi", "z20", "rs63", "bbw_rank", "turnover_cr", "wk_up"):
        assert (pd.isna(a[k]) and pd.isna(b[k])) or abs(a[k] - b[k]) < 1e-9, k


# ---------------------------------------------------------------- regime
def test_regime_states_and_matrix():
    up = walk(3, drift=0.002, vol=0.006, p0=20000)
    st = RG.current(up)
    assert st["state"] in ("STRONG_BULL", "BREAKOUT", "WEAK_BULL")
    down = walk(4, drift=-0.002, vol=0.006, p0=20000)
    assert RG.current(down)["state"] in ("STRONG_BEAR", "BREAKDOWN", "WEAK_BEAR")
    crash = up.copy()
    crash.iloc[-1, crash.columns.get_loc("Close")] = crash["Close"].iloc[-2] * 0.95
    assert RG.current(crash)["state"] == "PANIC"
    assert RG.current(up, data_ok=False)["state"] == "UNSAFE"
    assert RG.MATRIX["STRONG_BEAR"]["allow"] == [] and RG.MATRIX["UNCERTAIN"]["allow"] == []


def test_global_risk_reports_missing_data():
    g = RG.global_risk({"SPX": walk(5, p0=5000)})
    assert any(p.get("status") == "DATA UNAVAILABLE" for p in g["parts"]) and g["score"] is not None


# ---------------------------------------------------------------- trade management
def _frame(bars, atr=2.0):
    f = pd.DataFrame(bars, columns=["open", "high", "low", "close"])
    f["atr"] = atr
    return f


def test_simulate_stop_target_time_and_gap():
    plan = {"stop": 95.0, "t2": 110.0, "max_hold": 3}
    # stop on the second bar
    r = S.simulate(_frame([(100, 100, 100, 100), (100, 101, 99, 100), (99, 99, 94, 95)]), 0, plan, slip=0)
    assert r["why"] == "stop" and abs(r["exit"] - 95) < 1e-9
    # target
    r = S.simulate(_frame([(100, 100, 100, 100), (100, 111, 99, 108)]), 0, plan, slip=0)
    assert r["why"] == "target"
    # time exit at the open after max_hold bars
    r = S.simulate(_frame([(100, 100, 100, 100)] + [(100, 101, 99, 100)] * 3 + [(102, 102, 101, 101)]), 0, plan, slip=0)
    assert r["why"] == "time" and r["exit"] == 102
    # gap below the stop: no entry
    assert S.simulate(_frame([(100, 100, 100, 100), (90, 91, 89, 90)]), 0, plan, slip=0) == {"skipped": "gap below stop"}


def test_breakeven_stop_after_one_R():
    pos = {"entry": 100.0, "stop": 95.0, "risk": 5.0, "t2": 120.0, "bars": 0, "max_hold": 20, "exit_next_open": None}
    S.manage_step(pos, (100, 106, 100, 105.5), 2.0, slip=0)
    assert pos["stop"] >= 100.0          # +1R close -> break-even (never moves down)
    s0 = pos["stop"]
    S.manage_step(pos, (105, 105, 101, 101), 2.0, slip=0)
    assert pos["stop"] >= s0


# ---------------------------------------------------------------- calibration / probability
def _trades(n, win_ret, loss_ret, wins, strategy="BREAKOUT", regime="STRONG_BULL", start="2024-01-01", score=70):
    d0 = dt.date.fromisoformat(start)
    out = []
    for i in range(n):
        d = d0 + dt.timedelta(days=i)
        out.append({"symbol": f"X{i}", "strategy": strategy, "date": str(d), "exit_date": str(d + dt.timedelta(days=5)), "regime": regime,
                    "score": score, "ret": win_ret if i < wins else loss_ret, "R": 1, "bars": 5, "dow": 0, "open": False, "riskf": 0.04})
    return out


def test_shrinkage_and_evidence():
    tr = _trades(200, 0.05, -0.02, 100) + _trades(5, 0.05, -0.02, 5, regime="SIDEWAYS")
    m = CA.Model(tr, cost=0.003)
    big = m.estimate("BREAKOUT", "STRONG_BULL", 70)
    small = m.estimate("BREAKOUT", "SIDEWAYS", 70)
    assert big["evidence"] == "OK" and 0.45 < big["p"] < 0.56
    assert small["p"] < 0.8              # 5/5 wins is pulled strongly toward the strategy's ~50%
    few = CA.Model(_trades(10, 0.05, -0.02, 6), cost=0.003).estimate("BREAKOUT", "STRONG_BULL", 70)
    assert few["evidence"] == "INSUFFICIENT EVIDENCE"


def test_walk_forward_uses_only_past_trades():
    # first 300 trades lose, the later ones win: early predictions must NOT already know about the wins
    tr = _trades(300, 0.05, -0.02, 0) + _trades(300, 0.05, -0.02, 300, start="2024-11-01")
    wf = CA.walk_forward_check(tr, cost=0.003)
    assert wf["available"]
    low = [r for r in wf["reliability"] if r["mean_pred"] < 0.3]
    assert low and low[0]["actual"] > low[0]["mean_pred"]     # model was (correctly) surprised -> no leakage


def test_strategy_status_pauses_clear_losers():
    rep, _ = CA.strategy_report(_trades(80, 0.05, -0.03, 10), cost=0.003)
    assert rep["BREAKOUT"]["status"].startswith(("PAUSED", "REDUCED")) and rep["BREAKOUT"]["weight"] == 0
    rep, _ = CA.strategy_report(_trades(80, 0.05, -0.02, 50), cost=0.003)
    assert rep["BREAKOUT"]["status"] == "ACTIVE" and rep["BREAKOUT"]["weight"] > 0


# ---------------------------------------------------------------- score
def test_score_marks_unavailable_and_normalises():
    f = F.compute(walk(6, drift=0.001), None)
    t = SC.total(SC.components(f.iloc[-1], {}))
    assert t["items"]["options_futures"]["points"] is None and t["available"] < 100 and 0 <= t["score"] <= 100


# ---------------------------------------------------------------- money
def test_costs_match_ledger_cost_function():
    from engine import costs
    from decimal import Decimal
    c = MO.trade_costs(50000)
    assert abs(c["charges"] - float(costs.estimate_round_trip(Decimal("50000"), "delivery")["total"])) < 0.01
    assert c["slippage"] == pytest.approx(100.0)


def test_sizing_risk_based_and_micro_capital():
    q, _ = MO.size(100000, 100000, 500, 490, 0.01)
    assert q == 50                                   # ₹1,000 risk / ₹10 stop = 100, capped by 25% of equity = 50
    q, note = MO.size(1000, 1000, 2500, 2400, 0.01)
    assert q == 0                                    # ₹1,000 cannot buy a ₹2,500 share within risk
    assert MO.tier(1000)["name"] == "MICRO" and MO.tier(1000)["max_open"] == 1


def test_protections_never_increase_size_after_losses():
    m0, b0, _ = MO.protections({"equity": 100, "start_day_equity": 100, "loss_streak": 0})
    m3, _, _ = MO.protections({"equity": 100, "start_day_equity": 100, "loss_streak": 3})
    m5, _, _ = MO.protections({"equity": 100, "start_day_equity": 100, "loss_streak": 5})
    _, b, _ = MO.protections({"equity": 97, "start_day_equity": 100})
    assert m0 == 1.0 and m3 < m0 and m5 < m3 and b and not b0


def test_correlated_same_bet_blocked():
    blk, _, _ = MO.correlation_check("A", "IT", 1.0, 500, [{"symbol": "B", "sector": "Bank", "risk_rupees": 500, "value": 10000}],
                                     lambda a, b: 0.9, 100000)
    assert blk and "same bet" in blk


def test_montecarlo_needs_data_and_reports_ruin():
    assert not MC.run([0.5] * 10)["available"]
    r = MC.run(list(np.random.default_rng(1).normal(0.1, 1.0, 400)))
    assert r["available"] and "prob_ruin_50_pct" in r["horizons"]["250"]


# ---------------------------------------------------------------- day book
def test_daybook_split_adds_up():
    d = tempfile.mkdtemp()
    L = Ledger(os.path.join(d, "l.json")).load([])
    L.create_account("T", "t", "INR", "100000", ["x"])
    o, _ = L.submit("T", "AAA", "buy", 10, bar_date="2026-01-01")
    L.fill(o, 1000, 10, "2026-01-02")
    o, _ = L.submit("T", "BBB", "buy", 5, bar_date="2026-01-01")
    L.fill(o, 2000, 5, "2026-01-02")
    L.mark("T", {"AAA": 1000, "BBB": 2000}, "2026-01-02")
    o, _ = L.submit("T", "AAA", "sell", 10, bar_date="2026-01-02")
    L.fill(o, 1100, 10, "2026-01-03")
    L.mark("T", {"BBB": 1900}, "2026-01-03")
    b = DB.build(L.state, "T")
    s = b["split"]
    assert abs(s["total_since_start"] - (s["booked_net_realized"] + s["open_positions_unrealized"] - s["entry_charges_on_open_positions"])) < 0.05
    assert b["rows"][-1]["trades"] == 1 and b["rows"][-1]["charges_paid_today"] > 0


# ---------------------------------------------------------------- end to end
def _universe(n, drift):
    stocks = {f"S{i:02d}": walk(100 + i, drift=drift + 0.0003 * ((i % 5) - 2) / 2, vol=0.015 + 0.002 * (i % 4)) for i in range(n)}
    return stocks, walk(7, drift=drift, vol=0.008, p0=20000)


def test_end_to_end_cards_have_full_audit_trail():
    stocks, idx = _universe(25, 0.0008)
    d = tempfile.mkdtemp()
    L = Ledger(os.path.join(d, "l.json")).load([])
    out = BE.run(L, stocks, idx, {"NIFTY": idx}, {s: "IT" for s in stocks}, DATES[-1].date())
    assert out["history"]["trades_simulated"] > 100
    for c in out["decisions"]:
        assert c["stages"] and c["decision"] in ("TRADE", "WAIT", "REJECT")
        if c["decision"] == "REJECT":
            assert c["reject_reason"]
        if c["decision"] == "TRADE":
            assert c["qty"] >= 1 and c["ev"]["expected_net"] > 0 and all(s["pass"] for s in c["stages"])
    assert len(out["capital_scenarios"]) == 10 and "split" in out["daybook"]
    assert PA.ACCOUNT["id"] in L.state["accounts"]


def test_falling_market_means_no_new_longs():
    stocks, idx = _universe(20, -0.0015)
    d = tempfile.mkdtemp()
    L = Ledger(os.path.join(d, "l.json")).load([])
    out = BE.run(L, stocks, idx, {"NIFTY": idx}, {}, DATES[-1].date())
    assert out["regime"]["state"] in ("WEAK_BEAR", "STRONG_BEAR", "BREAKDOWN", "PANIC", "UNCERTAIN", "HIGH_VOL")
    if out["regime"]["state"] != "WEAK_BEAR":
        assert out["counts"].get("TRADE", 0) == 0 and out["headline"].startswith("NO TRADE")


def test_stale_data_locks_trading():
    stocks, idx = _universe(15, 0.001)
    d = tempfile.mkdtemp()
    L = Ledger(os.path.join(d, "l.json")).load([])
    out = BE.run(L, stocks, idx, {"NIFTY": idx}, {}, DATES[-1].date(), data_ok=False, trade=False)
    assert out["regime"]["state"] == "UNSAFE" and out["counts"].get("TRADE", 0) == 0
