"""Builds api/live_control.json from repository variables (set in GitHub > Settings > Variables) and the paper record."""
import os, hashlib, json, datetime as dt
from decimal import Decimal

CONSENT_PHRASE = "I ACCEPT REAL MONEY RISK"
GATE = {"min_closed_trades": 30, "min_profit_factor": 1.2, "max_drawdown_pct": -15.0, "min_expectancy": 0.0}
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))


def env(name, default=""):
    return (os.environ.get(name) or default).strip()


def gate(paper_accounts):
    acc = next((a for a in paper_accounts if a["account"] == "IN-SWING"), None)
    if not acc:
        return {"passed": False, "checks": [], "reason": "no paper account"}
    ts = (acc.get("analytics") or {}).get("trades") or {}
    es = (acc.get("analytics") or {}).get("equity") or {}
    checks = [
        {"check": f">= {GATE['min_closed_trades']} closed paper trades", "value": ts.get("n", 0), "ok": ts.get("n", 0) >= GATE["min_closed_trades"]},
        {"check": f"profit factor >= {GATE['min_profit_factor']}", "value": ts.get("profit_factor"), "ok": (ts.get("profit_factor") or 0) >= GATE["min_profit_factor"]},
        {"check": f"max drawdown better than {GATE['max_drawdown_pct']}%", "value": es.get("max_drawdown_pct"), "ok": (es.get("max_drawdown_pct") if es.get("max_drawdown_pct") is not None else -999) > GATE["max_drawdown_pct"]},
        {"check": "positive expectancy after costs", "value": ts.get("expectancy"), "ok": (ts.get("expectancy") or -1) > GATE["min_expectancy"]},
    ]
    return {"passed": all(c["ok"] for c in checks), "checks": checks}


def build(paper_accounts):
    live = env("LIVE_TRADING", "OFF").upper() == "ON"
    auto_req = env("LIVE_AUTO", "OFF").upper() == "ON"
    consent = env("LIVE_CONSENT") == CONSENT_PHRASE
    kill = env("LIVE_KILL", "OFF").upper() == "ON"
    g = gate(paper_accounts)
    limits = {"max_orders_per_day": int(env("LIVE_MAX_ORDERS_DAY", "3")), "max_order_value_inr": float(env("LIVE_MAX_ORDER_VALUE", "25000")),
              "max_daily_loss_inr": float(env("LIVE_MAX_DAILY_LOSS", "2000")), "max_open_positions": int(env("LIVE_MAX_POSITIONS", "3")),
              "order_type": "LIMIT (at last price +/- 0.5%); never unprotected market orders", "segments": env("LIVE_SEGMENTS", "CASH").upper().split(",")}
    auto = live and auto_req and consent and g["passed"] and not kill
    why_not_auto = [m for ok, m in [(live, "LIVE_TRADING is not ON"), (auto_req, "LIVE_AUTO is not ON"), (consent, "LIVE_CONSENT phrase not set"),
                                    (g["passed"], "paper track-record gate not passed"), (not kill, "kill switch is ON")] if not ok]
    return {"generated_at": dt.datetime.now(IST).isoformat(timespec="seconds"), "live_trading": live and not kill, "kill": kill,
            "mode": "OFF" if not live or kill else ("AUTO" if auto else "APPROVAL"), "auto_allowed": auto, "auto_blockers": why_not_auto,
            "gate": g, "limits": limits, "broker": env("LIVE_BROKER", "groww"), "consent_phrase_required": CONSENT_PHRASE,
            "static_ip_note": "SEBI (from April 2026): API orders must originate from a static IP whitelisted with the broker. GitHub runners do not have one, so orders are placed only by the VISION runner on your static-IP machine."}
