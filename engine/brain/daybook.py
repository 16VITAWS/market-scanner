"""
Day-by-day profit engine for a paper (or real) ledger account. Net realised P&L after every charge is the headline;
gross is shown only next to it. Slippage is already inside paper fill prices; its estimate is shown separately
so it is visible, not subtracted twice.
"""
from collections import defaultdict
from decimal import Decimal as D

SLIP = 0.001
CH_KEYS = ("brokerage", "stt", "stamp", "exchange", "sebi", "dp", "gst")


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


def build(state, account_id, regimes=None):
    a = state["accounts"].get(account_id)
    if not a:
        return {"available": False, "reason": f"no account {account_id}"}
    trades = [t for t in state["trades"] if t["account"] == account_id]
    fills = [f for f in state["fills"] if f["account"] == account_id]
    cash = [c for c in state["cash_ledger"] if c["account"] == account_id and c["type"] in ("DEPOSIT", "WITHDRAW")]
    eqp = a.get("equity") or []
    start_cash = _f(a["start_cash"])
    by_tr, by_fill, by_cash = defaultdict(list), defaultdict(list), defaultdict(float)
    for t in trades:
        by_tr[t["exit_date"]].append(t)
    for f in fills:
        by_fill[f["bar"]].append(f)
    first = eqp[0]["date"] if eqp else None
    for c in cash:
        if c["ref"] == "opening balance":
            continue
        by_cash[c["time"][:10]] += _f(c["amount"])
    rows, prev, peak, cum_net = [], start_cash, start_cash, 0.0
    for p in eqp:
        d = p["date"]
        end = _f(p["value"])
        dep = by_cash.get(d, 0.0)
        ts = by_tr.get(d, [])
        net_real = sum(_f(t["pnl"]) for t in ts)
        fees_closed = sum(_f(t["fees"]) for t in ts)
        fl = by_fill.get(d, [])
        ch = {k: round(sum(_f((f.get("fee_breakdown") or {}).get(k)) for f in fl), 2) for k in CH_KEYS}
        charges_today = round(sum(_f(f["fees"]) for f in fl), 2)
        slip_est = round(sum(_f(f["value"]) for f in fl) * SLIP, 2)
        change = end - prev - dep
        unreal_change = change - net_real
        peak = max(peak, end)
        cum_net += change
        wins = [t for t in ts if _f(t["pnl"]) > 0]
        strat = defaultdict(float)
        for t in ts:
            strat[(t.get("strategy") or "manual").replace("brain:", "")] += _f(t["pnl"])
        rows.append({"date": d, "start": round(prev, 2), "deposits": round(dep, 2), "gross_realized": round(net_real + fees_closed, 2),
                     "charges_paid_today": charges_today, "charges": ch, "slippage_est_in_prices": slip_est,
                     "net_realized": round(net_real, 2), "open_positions_change": round(unreal_change, 2), "total_change": round(change, 2),
                     "end": round(end, 2), "daily_return_pct": round(change / prev * 100, 3) if prev else None,
                     "cum_return_pct": round(cum_net / start_cash * 100, 3) if start_cash else None,
                     "drawdown_pct": round((end / peak - 1) * 100, 3) if peak else None, "trades": len(ts), "wins": len(wins),
                     "losses": len(ts) - len(wins), "best": max((_f(t["pnl"]) for t in ts), default=None),
                     "worst": min((_f(t["pnl"]) for t in ts), default=None), "by_strategy": {k: round(v, 2) for k, v in strat.items()},
                     "regime": (regimes or {}).get(d)})
        prev = end
    unreal_now = sum((_f(x["last"]) - _f(x["avg_price"])) * _f(x["qty"]) for x in a["positions"].values())
    open_fees = sum(_f(x.get("fees")) for x in a["positions"].values())
    real_all = sum(_f(t["pnl"]) for t in trades)
    eq_now = _f(eqp[-1]["value"]) if eqp else start_cash
    today = rows[-1] if rows else None

    def period(prefix_len, key):
        rs = [r for r in rows if r["date"][:prefix_len] == key] if key else []
        return {"net_realized": round(sum(r["net_realized"] for r in rs), 2), "total_change": round(sum(r["total_change"] for r in rs), 2),
                "charges": round(sum(r["charges_paid_today"] for r in rs), 2), "trades": sum(r["trades"] for r in rs), "days": len(rs)}

    last = rows[-1]["date"] if rows else None
    ts_sorted = sorted(trades, key=lambda t: t["exit_date"])
    streak = 0
    for t in reversed(ts_sorted):
        if _f(t["pnl"]) <= 0:
            streak += 1
        else:
            break
    return {"available": True, "account": account_id, "name": a["name"], "start_cash": start_cash, "equity": round(eq_now, 2),
            "split": {"total_since_start": round(eq_now - start_cash, 2), "booked_net_realized": round(real_all, 2),
                      "open_positions_unrealized": round(unreal_now, 2), "entry_charges_on_open_positions": round(open_fees, 2),
                      "today_change": today["total_change"] if today else 0.0, "today_date": last,
                      "explain": "total since start = booked profit from closed trades (after all charges) + open positions valued at the latest close - charges already paid to open them"},
            "charges_total": round(_f(a.get("fees_paid")), 2), "loss_streak": streak,
            "this_month": period(7, last[:7] if last else None), "today": today, "rows": rows[-370:]}
