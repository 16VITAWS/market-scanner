"""
Central risk engine. Independent of strategies; may reject any order and must give a reason.
Returns a decision dict that is stored on the order so every rejection is auditable.
"""
from decimal import Decimal
from . import config as C

D = Decimal


def decision(ok, reason, checks):
    return {"approved": ok, "reason": reason, "checks": checks}


def size_position(equity, entry, stop, risk_pct, max_pos_pct, vol20=None, max_vol_share=None, lot=1):
    """Risk-based quantity: risk_pct of equity between entry and stop, capped by position and liquidity limits."""
    equity, entry, stop = D(str(equity)), D(str(entry)), D(str(stop))
    risk_per_unit = entry - stop
    if risk_per_unit <= 0:
        return 0, "stop is not below entry"
    qty = int((equity * D(str(risk_pct))) / risk_per_unit)
    cap_qty = int((equity * D(str(max_pos_pct))) / entry)
    notes = []
    if qty > cap_qty:
        qty = cap_qty; notes.append(f"capped at {int(max_pos_pct*100)}% of equity")
    if vol20 and max_vol_share:
        liq = int(D(str(vol20)) * D(str(max_vol_share)))
        if qty > liq:
            qty = liq; notes.append(f"capped at {int(max_vol_share*100)}% of 20-day volume")
    if lot and lot > 1:
        qty = (qty // lot) * lot
    return max(qty, 0), "; ".join(notes) or "ok"


def gate(order, account_summary, positions, limits=None, data_status="OK", kill_switch=False, todays_realized=D("0"),
         orders_today=0, sector_exposure=None, correlated_exposure=None):
    """
    order: dict(symbol, side, qty, price, stop, sector, value)
    account_summary: from Ledger.summary()
    Every check is recorded; the first failure gives the reason.
    """
    L = {**C.RISK, **(limits or {})}
    checks = []
    equity = D(account_summary["equity"])
    value = D(str(order["qty"])) * D(str(order["price"]))

    def chk(name, ok, detail):
        checks.append({"check": name, "ok": bool(ok), "detail": detail})
        return ok

    if not chk("kill_switch", not kill_switch, "kill switch is ON" if kill_switch else "off"):
        return decision(False, "kill switch is on", checks)
    if not chk("mode", C.MODE in ("PAPER", "RESEARCH"), f"paper engine mode {C.MODE}"):
        return decision(False, "live modes are not implemented; paper only", checks)
    if not chk("data_fresh", data_status == "OK", f"data status {data_status}"):
        return decision(False, f"market data is {data_status}; no new orders", checks)
    if not chk("qty_positive", D(str(order["qty"])) > 0, f"qty {order['qty']}"):
        return decision(False, "quantity is zero", checks)
    if not chk("orders_per_run", orders_today < L["max_orders_per_run"], f"{orders_today}/{L['max_orders_per_run']}"):
        return decision(False, "order limit for this run reached", checks)
    if not chk("account_active", account_summary.get("status") == "ACTIVE", account_summary.get("status")):
        return decision(False, f"account is {account_summary.get('status')}", checks)

    if order["side"] == "buy":
        if not chk("daily_loss", todays_realized > -D(str(L["max_daily_loss"])) * equity, f"today {todays_realized} vs limit {-D(str(L['max_daily_loss'])) * equity:.0f}"):
            return decision(False, "daily loss limit hit; no new entries today", checks)
        dd = D(account_summary["drawdown_pct"]) / 100
        if not chk("max_drawdown", dd > -D(str(L["max_drawdown"])), f"drawdown {dd*100:.1f}%"):
            return decision(False, "account drawdown limit reached; entries paused", checks)
        if not chk("max_positions", len(positions) < L["max_open_positions"], f"{len(positions)}/{L['max_open_positions']} open"):
            return decision(False, "maximum open positions reached", checks)
        if not chk("not_already_held", order["symbol"] not in positions, "already held" if order["symbol"] in positions else "new"):
            return decision(False, "already holding this symbol", checks)
        if not chk("position_size", value <= equity * D(str(L["max_position_pct"])), f"{value:.0f} vs cap {equity * D(str(L['max_position_pct'])):.0f}"):
            return decision(False, "position would exceed the single-position cap", checks)
        gross = D(account_summary["gross_exposure"]) + value
        if not chk("gross_exposure", gross <= equity * D(str(L["max_gross_exposure"])), f"{gross:.0f} vs {equity:.0f}"):
            return decision(False, "gross exposure limit (no leverage in paper equities)", checks)
        if not chk("cash", value <= D(account_summary["cash"]), f"need {value:.0f}, cash {D(account_summary['cash']):.0f}"):
            return decision(False, "insufficient cash", checks)
        stop = order.get("stop")
        if not chk("stop_below_entry", stop is not None and D(str(stop)) < D(str(order["price"])), f"stop {stop}"):
            return decision(False, "stop-loss missing or not below entry", checks)
        risk_amt = (D(str(order["price"])) - D(str(stop))) * D(str(order["qty"]))
        if not chk("risk_per_trade", risk_amt <= equity * D(str(L["max_risk_per_trade"])) * D("1.05"), f"risk {risk_amt:.0f} vs {equity * D(str(L['max_risk_per_trade'])):.0f}"):
            return decision(False, "risk per trade exceeds limit", checks)
        if sector_exposure is not None and order.get("sector"):
            sec = sector_exposure.get(order["sector"], D("0")) + value
            if not chk("sector_exposure", sec <= equity * D(str(L["max_sector_pct"])), f"{order['sector']} {sec:.0f}"):
                return decision(False, f"sector exposure limit for {order['sector']}", checks)
        if correlated_exposure is not None:
            if not chk("correlated_exposure", correlated_exposure <= D("0.6") * equity, f"corr-cluster {correlated_exposure:.0f}"):
                return decision(False, "too much exposure to highly correlated names", checks)
    else:
        if not chk("has_position", order["symbol"] in positions, "position exists" if order["symbol"] in positions else "none"):
            return decision(False, "no position to exit (short selling is disabled)", checks)
    return decision(True, "approved", checks)
