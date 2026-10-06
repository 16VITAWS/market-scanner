"""
Money layers: cost engine wrapper, expected-value engine, capital-aware position sizing, micro-capital economic
filter, loss protections and portfolio (correlation / sector) control. Pure functions, unit-tested.
Costs come from engine/costs.py - the SAME function the paper ledger charges, so estimates match paper fills.
"""
import math
from decimal import Decimal
from .. import costs

SLIPPAGE = 0.001            # per side, same as the paper simulator
TIERS = [  # (max equity, name, risk per trade, max open positions, min net expected rupees)
    (2000, "MICRO", 0.010, 1, 5),
    (10000, "MICRO", 0.010, 1, 10),
    (25000, "SMALL", 0.010, 2, 25),
    (100000, "STANDARD", 0.0075, 4, 50),
    (500000, "STANDARD", 0.0075, 5, 100),
    (float("inf"), "LARGE", 0.005, 6, 200),
]
LIMITS = {"max_position_pct": 0.25, "max_daily_loss": 0.02, "max_weekly_loss": 0.04, "max_monthly_dd": 0.08,
          "max_portfolio_risk": 0.04, "max_sector_pct": 0.40, "max_corr": 0.80, "max_volume_share": 0.02,
          "min_turnover_cr": 10, "min_net_to_cost": 1.0, "min_rr": 1.5}


def tier(equity):
    for mx, name, rp, mo, mn in TIERS:
        if equity <= mx:
            return {"name": name, "risk_pct": rp, "max_open": mo, "min_net_rupees": mn}


def trade_costs(value, broker="groww"):
    """Round-trip statutory + brokerage charges (delivery) and slippage for a position of `value` rupees."""
    if value <= 0:
        return {"charges": 0.0, "slippage": 0.0, "total": 0.0, "pct": 0.0, "breakdown": {}}
    rt = costs.estimate_round_trip(Decimal(str(round(value, 2))), "delivery", broker)
    ch = float(rt["total"])
    sl = 2 * SLIPPAGE * value
    bd = {k: float(rt["buy"][k]) + float(rt["sell"][k]) for k in ("brokerage", "stt", "stamp", "exchange", "sebi", "dp", "gst")}
    return {"charges": round(ch, 2), "slippage": round(sl, 2), "total": round(ch + sl, 2), "pct": round((ch + sl) / value * 100, 3), "breakdown": bd,
            "verification": rt["buy"].get("verification")}


def size(equity, cash, entry, stop, risk_pct, size_mult=1.0, turnover_cr=None, max_pos_pct=None, avg_vol=None):
    """Risk-based quantity: allowed risk / stop distance, then capped by position %, cash and liquidity. Never sized up after losses."""
    per = entry - stop
    if per <= 0 or entry <= 0:
        return 0, "invalid stop"
    allowed = equity * risk_pct * size_mult
    q = math.floor(allowed / per)
    notes = [f"risk ₹{allowed:,.0f} ({risk_pct * size_mult:.2%} of equity) / ₹{per:,.2f} stop distance = {q}"]
    cap = math.floor(equity * (max_pos_pct or LIMITS["max_position_pct"]) / entry)
    if q > cap:
        q = cap; notes.append(f"capped at {LIMITS['max_position_pct']:.0%} of equity = {cap}")
    cq = math.floor(cash * 0.995 / entry)
    if q > cq:
        q = cq; notes.append(f"cash allows {cq}")
    if avg_vol:
        lq = math.floor(avg_vol * LIMITS["max_volume_share"])
        if q > lq:
            q = lq; notes.append(f"liquidity cap {LIMITS['max_volume_share']:.0%} of average volume = {lq}")
    return max(q, 0), "; ".join(notes)


def protections(account):
    """account: {equity, start_day_equity, start_week_equity, peak_month, loss_streak}. Returns (size multiplier, blocks, notes)."""
    eq = account.get("equity") or 0
    mult, blocks, notes = 1.0, [], []
    d = (eq / account["start_day_equity"] - 1) if account.get("start_day_equity") else 0
    w = (eq / account["start_week_equity"] - 1) if account.get("start_week_equity") else 0
    m = (eq / account["peak_month"] - 1) if account.get("peak_month") else 0
    if d <= -LIMITS["max_daily_loss"]:
        blocks.append(f"daily loss {d:.2%} reached the {LIMITS['max_daily_loss']:.0%} limit - no new trades today")
    if w <= -LIMITS["max_weekly_loss"]:
        blocks.append(f"weekly loss {w:.2%} reached the {LIMITS['max_weekly_loss']:.0%} limit - manual review")
    if m <= -LIMITS["max_monthly_dd"]:
        mult *= 0.5; notes.append(f"monthly drawdown {m:.2%}: half size")
    elif m <= -LIMITS["max_monthly_dd"] / 2:
        mult *= 0.75; notes.append(f"drawdown {m:.2%}: size x0.75")
    s = account.get("loss_streak") or 0
    if s >= 10:
        blocks.append(f"{s} losses in a row - strategy review required before new trades")
    elif s >= 5:
        mult *= 0.5; notes.append(f"{s} losses in a row: half size, checking regime / slippage / data")
    elif s >= 3:
        mult *= 0.75; notes.append(f"{s} losses in a row: size x0.75")
    return mult, blocks, notes


def ev_for_position(est, value, risk_rupees, cost_typical_pct, cost_actual):
    """est: Model.estimate(); cost_typical_pct: the cost fraction the estimate was computed with (as a fraction)."""
    if est.get("p") is None:
        return None
    gross_pct = est["exp_net_pct"] / 100 + cost_typical_pct           # expected price return before costs (after slippage)
    exp_gross = gross_pct * value + cost_actual["slippage"]            # slippage is shown separately below
    net = exp_gross - cost_actual["charges"] - cost_actual["slippage"]
    return {"p_win": est["p"], "expected_gross": round(exp_gross, 2), "charges": cost_actual["charges"], "slippage": cost_actual["slippage"],
            "expected_net": round(net, 2), "net_in_R": round(net / risk_rupees, 3) if risk_rupees else None,
            "cost_share_of_gross": round(cost_actual["total"] / exp_gross, 3) if exp_gross > 0 else None,
            "avg_win_rupees": round((est.get("avg_win_pct") or 0) / 100 * value, 2), "avg_loss_rupees": round((est.get("avg_loss_pct") or 0) / 100 * value, 2)}


def correlation_check(sym, sector, beta, risk_rupees, open_positions, corr_fn, equity):
    """open_positions: [{symbol, sector, risk_rupees, value, beta}]. corr_fn(a,b)->60-day correlation or None.
    All long equity positions share market direction: their beta-weighted risk is summed against max_portfolio_risk."""
    notes, block = [], None
    port = sum((p.get("beta") or 1.0) * (p.get("risk_rupees") or 0) for p in open_positions)
    new = port + (beta or 1.0) * risk_rupees
    if equity and new > LIMITS["max_portfolio_risk"] * equity:
        block = f"portfolio directional risk would be ₹{new:,.0f} > {LIMITS['max_portfolio_risk']:.0%} of equity"
    sec_val = sum(p.get("value", 0) for p in open_positions if sector and p.get("sector") == sector)
    if sector and equity and sec_val > LIMITS["max_sector_pct"] * equity:
        block = block or f"sector {sector} already {sec_val / equity:.0%} of equity"
    for p in open_positions:
        if p["symbol"] == sym:
            continue
        c = corr_fn(sym, p["symbol"]) if corr_fn else None
        if c is not None and c >= LIMITS["max_corr"]:
            block = block or f"{c:.2f} correlated with open position {p['symbol']} - same bet twice"
        elif c is not None and c >= 0.6:
            notes.append(f"correlated {c:.2f} with {p['symbol']}")
    return block, notes, round(new, 2)
