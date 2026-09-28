"""
Writes the JSON files the portal reads (site/api/*.json). Every file carries generated_at,
engine version and a data-status label. Candle files are compact arrays.
"""
import os, json, glob, datetime as dt
from decimal import Decimal
import pandas as pd
from . import config as C, dq, costs, knowledge, calendar as cal
from .providers import capabilities
from .strategies import catalogue

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))


def _enc(o):
    if isinstance(o, Decimal):
        return str(o)
    if isinstance(o, (pd.Timestamp, dt.datetime, dt.date)):
        return o.isoformat()
    if hasattr(o, "item"):
        return o.item()
    return str(o)


def now():
    return dt.datetime.now(IST).isoformat(timespec="seconds")


def write(site, name, obj):
    path = os.path.join(site, "api", name)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    obj = {"generated_at": now(), "engine_version": C.ENGINE_VERSION, "mode": C.MODE, **obj} if isinstance(obj, dict) else obj
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f, default=_enc, separators=(",", ":"), ensure_ascii=False)
    os.replace(tmp, path)
    return path


def candles_json(site, instr, df, meta, interval="1d", max_bars=None, calendar_id=None):
    issues, stats = dq.check(df, calendar_id)
    d = df.tail(max_bars) if max_bars else df
    age = dq.age_days(df)
    obj = {"id": instr["id"], "name": instr.get("name", instr["id"]), "currency": instr.get("currency"), "exchange": instr.get("exchange"),
           "interval": interval, "status": dq.status_label(meta, age), "age_days": age, "meta": meta, "dq": {"issues": issues, **stats},
           "n": len(d), "d": [t.strftime("%Y-%m-%d") if interval in ("1d", "1wk", "1mo") else t.strftime("%Y-%m-%dT%H:%M:%SZ") for t in d.index],
           "o": [round(float(x), 2) for x in d["Open"]], "h": [round(float(x), 2) for x in d["High"]],
           "l": [round(float(x), 2) for x in d["Low"]], "c": [round(float(x), 2) for x in d["Close"]],
           "v": [int(x) for x in d["Volume"].fillna(0)]}
    sub = "candles" if interval == "1d" else f"candles_{interval}"
    return write(site, f"{sub}/{instr['id']}.json", obj)


def quotes_from_frames(frames, master):
    """frames: {id: df}. Returns {id: {p, pc, chg_pct, d, status}}."""
    q = {}
    for k, df in frames.items():
        if df is None or len(df) < 2:
            continue
        c = df["Close"]
        age = dq.age_days(df)
        row = master.loc[k] if k in master.index else {}
        q[k] = {"p": round(float(c.iloc[-1]), 2), "pc": round(float(c.iloc[-2]), 2), "chg_pct": round(float(c.iloc[-1] / c.iloc[-2] - 1) * 100, 2),
                "d": str(df.index[-1].date()), "t": df.index[-1].isoformat(), "age_days": age,
                "status": "STALE" if age > 4 else ("HISTORICAL" if age >= 1 else "DELAYED"),
                "name": row.get("name", k) if hasattr(row, "get") else k, "currency": row.get("currency") if hasattr(row, "get") else None,
                "kind": row.get("kind") if hasattr(row, "get") else None, "sector": row.get("sector") if hasattr(row, "get") else None,
                "hi52": round(float(df["High"].tail(252).max()), 2), "lo52": round(float(df["Low"].tail(252).min()), 2)}
    return q


def paper_json(ledger):
    st = ledger.state
    accounts = []
    for aid, a in st["accounts"].items():
        s = ledger.summary(aid)
        s["positions"] = list(a["positions"].values())
        s["equity_curve"] = a["equity"][-400:]
        s["strategies"] = a["strategies"]
        accounts.append(s)
    return {"accounts": accounts, "orders": st["orders"][-300:], "fills": st["fills"][-300:], "trades": st["trades"][-300:],
            "cash_ledger": st["cash_ledger"][-300:], "audit": st["audit"][-200:], "fill_model": C.FILL, "risk_limits": C.RISK,
            "note": "PAPER ONLY. Fills at next bar open with slippage on OHLC data; intra-bar path unknown. No broker is connected."}


def health_json(site, providers_status, dq_summary, tests, workflow, data_status, extra=None):
    return {"data_status": data_status, "providers": providers_status, "data_quality": dq_summary, "tests": tests, "workflow": workflow,
            "calendars": cal.all_status(), "live_execution": {"implemented": C.LIVE_EXECUTION_IMPLEMENTED, "mode": C.MODE,
                                                              "requirements": ["broker API keys (never in code)", "broker algo approval", "risk review", "kill switch", "explicit per-order confirmation"]},
            "modules": modules_status(providers_status, data_status), **(extra or {})}


def modules_status(prov, data_status):
    yahoo_ok = any(p["id"] == "yahoo" and p.get("last_fetch_ok") for p in prov)
    live = "WORKING" if yahoo_ok else "DEGRADED"
    return [
        {"module": "Command center (regime, scanner, funnel)", "status": live, "source": "Yahoo EOD (unofficial)", "test": "test_bear_regime_blocks_buys_and_pipeline_runs", "acceptance": "regime and signals recomputed from fetched data each run"},
        {"module": "Global markets dashboard", "status": live, "source": "Yahoo EOD/delayed", "test": "quotes_from_frames", "acceptance": "every tile shows date and status"},
        {"module": "Charts (any symbol with candles file)", "status": "WORKING", "source": "candles/*.json", "test": "candles_json dq", "acceptance": "no fabricated candles; gaps flagged"},
        {"module": "AI scanner (rule-based scoring)", "status": live, "source": "pipeline.scan", "test": "test_strategy_signal_ignores_future_bars", "acceptance": "score breakdown per stock"},
        {"module": "Strategy lab (specs, backtests, walk-forward)", "status": "WORKING", "source": "backtest.py", "test": "test_backtest_no_lookahead_and_costs", "acceptance": "reproducible metrics with costs"},
        {"module": "Automatic paper trading (daily)", "status": live if C.MODE == "PAPER" else "PAUSED", "source": "ledger.py + simulator.py", "test": "test_cash_and_positions_reconcile_after_round_trip", "acceptance": "cash and positions reconcile; duplicates blocked"},
        {"module": "Manual paper trading (portal, any time incl. holidays)", "status": "WORKING", "source": "browser ledger (local-first) + quotes.json", "test": "portal self-test", "acceptance": "orders fill on documented rule; export/import"},
        {"module": "Replay / practice on past days", "status": "WORKING", "source": "candles/*.json", "test": "portal", "acceptance": "future bars never shown"},
        {"module": "Risk engine", "status": "WORKING", "source": "risk.py", "test": "test_risk_gate_rejects_oversized_and_missing_stop", "acceptance": "every rejection has a reason"},
        {"module": "News & sentiment (keyword heuristic)", "status": "SIMULATION" if not yahoo_ok else "WORKING (low confidence)", "source": "Google News RSS", "test": "news.classify", "acceptance": "originals and links preserved; method labelled"},
        {"module": "Event-impact graph", "status": "SEED", "source": "knowledge.py seed edges + computed correlations", "test": "correlations", "acceptance": "every edge has basis and uncertainty"},
        {"module": "Options chain", "status": "AWAITING FEED", "source": "NSE website best-effort / manual CSV", "test": "-", "acceptance": "partial chains labelled"},
        {"module": "FII/DII flows", "status": "AWAITING FEED", "source": "NSE website best-effort / manual", "test": "-", "acceptance": "provisional vs final labelled"},
        {"module": "IPO calendar", "status": "MANUAL", "source": "user-entered, unverified", "test": "-", "acceptance": "verification flag shown"},
        {"module": "Broker desk (links, charges, capability matrix)", "status": "WORKING (figures partly unverified)", "source": "costs.py", "test": "test_india_delivery_charges_reasonable", "acceptance": "verification status per figure"},
        {"module": "Alerts (in-app)", "status": "WORKING", "source": "portal rules on api data", "test": "portal", "acceptance": "user-defined price/signal alerts"},
        {"module": "Telegram alerts", "status": "AWAITING TOKEN", "source": "GitHub secret", "test": "-", "acceptance": "message received"},
        {"module": "Angel One data adapter", "status": "AWAITING KEY", "source": "stubs.AngelOne", "test": "test_stub_providers_never_fabricate", "acceptance": "candles match exchange data"},
        {"module": "Twelve Data adapter", "status": "AWAITING KEY", "source": "stubs.TwelveData", "test": "test_stub_providers_never_fabricate", "acceptance": "-"},
        {"module": "Live broker execution", "status": "LOCKED / NOT BUILT", "source": "-", "test": "test_live_locked", "acceptance": "never in this phase"},
    ]


def strategies_json(backtests=None):
    cards = catalogue()
    for c in cards:
        c["backtest"] = (backtests or {}).get(c["id"])
    return {"strategies": cards, "lifecycle": ["experimental", "backtested", "paper-approved", "disabled"],
            "note": "Backtest returns are historical, after modelled costs; they are not expected future profits."}


def brokers_json():
    stat = {k: {kk: str(vv) for kk, vv in v.items()} for k, v in costs.INDIA_STATUTORY.items() if isinstance(v, dict) and k not in ("sources",)}
    return {"brokers": costs.BROKERS, "statutory_india": stat, "statutory_sources": costs.INDIA_STATUTORY["sources"],
            "statutory_verification": costs.INDIA_STATUTORY["verification"], "gst": str(costs.INDIA_STATUTORY["gst"]),
            "examples": {seg: {b["id"]: {k: str(v) for k, v in costs.estimate_round_trip(100000, seg, b["id"]).items() if k == "total"} for b in costs.BROKERS if b["brokerage"]}
                         for seg in ("delivery", "intraday", "futures", "options")}}


def runs_index(site):
    out = []
    for p in sorted(glob.glob(os.path.join(site, "data", "runs", "*.json"))):
        try:
            with open(p) as f:
                j = json.load(f)
            out.append({"date": os.path.basename(p)[:-5], "file": "data/runs/" + os.path.basename(p), "regime": (j.get("regime") or j.get("mood") or {}).get("state"),
                        "buys": len(j.get("buys", [])), "sells": len(j.get("sells", [])), "engine_version": j.get("engine_version", "1.x")})
        except Exception:
            pass
    return out
