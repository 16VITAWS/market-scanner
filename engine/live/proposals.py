"""Turns today's signals into signed live-order proposals. A proposal is inert data until approved (or auto-allowed)."""
import hashlib, json, math, datetime as dt
from .. import config as C, costs

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))


def pid(p):
    core = json.dumps({k: p[k] for k in ("symbol", "side", "qty", "limit", "valid_until", "segment")}, sort_keys=True)
    return hashlib.sha256(core.encode()).hexdigest()[:10].upper()


def next_session(d):
    n = d + dt.timedelta(days=1)
    while n.weekday() > 4:
        n += dt.timedelta(days=1)
    return n


def build(scan, opt_sig, control, bar_date, capital_inr):
    lim = control["limits"]
    valid = next_session(dt.date.fromisoformat(str(bar_date))).isoformat()
    out = []
    for s in scan.get("buys", []):
        limit = round(math.floor(s["entry"] * 1.005 * 20) / 20, 2)           # tick 0.05, 0.5% above last close
        risk_amt = capital_inr * C.RISK["max_risk_per_trade"]
        per = max(limit - s["stop"], 0.01)                                   # risk measured from the limit price actually paid
        qty = int(min(risk_amt // per, lim["max_order_value_inr"] // limit))
        if qty < 1:
            continue
        p = {"symbol": s["symbol"], "exchange": "NSE", "segment": "CASH", "product": "CNC", "side": "BUY", "qty": qty, "order_type": "LIMIT",
             "limit": limit, "stop": s["stop"], "target": s["target"], "valid_until": valid, "strategy": "trend_breakout_swing",
             "reason": "; ".join(s["why"][:3]), "score": s["score"], "est_value": round(qty * limit, 2),
             "est_charges_round_trip": str(costs.estimate_round_trip(qty * limit, "delivery", control["broker"])["total"])}
        p["id"] = pid(p); out.append(p)
    for s in scan.get("sells", []):
        p = {"symbol": s["symbol"], "exchange": "NSE", "segment": "CASH", "product": "CNC", "side": "SELL", "qty": "ALL_HELD", "order_type": "LIMIT",
             "limit": round(math.ceil(s["entry"] * 0.995 * 20) / 20, 2), "valid_until": valid, "strategy": "exit_signal",
             "reason": "EXIT signal if you hold it: " + "; ".join(s["why"][:2]), "score": s["score"], "stop": None, "target": None}
        p["id"] = pid(p); out.append(p)
    if opt_sig and opt_sig.get("action") == "BUY" and "FNO" in lim["segments"]:
        p = {"symbol": opt_sig["name"], "exchange": "NSE", "segment": "FNO", "product": "NRML", "side": "BUY_SPREAD", "qty": opt_sig["lot"],
             "order_type": "LIMIT", "limit": opt_sig["debit"], "valid_until": valid, "strategy": "index_options_regime",
             "legs": [{"action": "BUY", "strike": opt_sig["k_long"], "kind": opt_sig["kind"], "expiry": opt_sig["expiry"]},
                      {"action": "SELL", "strike": opt_sig["k_short"], "kind": opt_sig["kind"], "expiry": opt_sig["expiry"]}],
             "reason": "; ".join(opt_sig["why"]), "max_loss_per_unit": opt_sig["max_loss"], "stop": None, "target": None,
             "pricing": "MODELED debit - real premiums will differ; the runner re-prices from live quotes before placing"}
        p["id"] = pid(p); out.append(p)
    return {"generated_at": dt.datetime.now(IST).isoformat(timespec="seconds"), "for_session": valid, "mode": control["mode"], "items": out,
            "note": "Inert until approved (APPROVAL mode) or auto-allowed (AUTO mode, gated). BUY quantities use 1% risk of the live capital and the per-order value cap."}
