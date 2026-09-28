"""
Paper ledger: virtual accounts, orders, fills, positions, cash ledger, equity history.

* Money and quantities are Decimal (stored as strings). No binary floating point in the ledger.
* Every state change is appended to `audit`. Saves are atomic (temp file + rename).
* Orders are idempotent via `dedupe_key`; re-running a day never double-books.
* The ledger knows nothing about strategies or risk: it only books what it is told.
"""
import os, json, uuid, datetime as dt
from decimal import Decimal, ROUND_HALF_UP, ROUND_DOWN
from . import costs

D = Decimal
ZERO = D("0")
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
ORDER_STATES = ["PENDING", "PARTIAL", "FILLED", "CANCELLED", "REJECTED", "EXPIRED"]


def now_iso():
    return dt.datetime.now(IST).isoformat(timespec="seconds")


def q2(x):
    return D(x).quantize(D("0.01"), rounding=ROUND_HALF_UP)


def _enc(o):
    if isinstance(o, Decimal):
        return str(o)
    raise TypeError(str(type(o)))


class Ledger:
    def __init__(self, path):
        self.path = path
        self.state = None

    # ---------------------------------------------------------------- persistence
    def load(self, default_accounts=None):
        if os.path.exists(self.path):
            with open(self.path) as f:
                self.state = json.load(f)
        else:
            self.state = {"version": 2, "created": now_iso(), "accounts": {}, "orders": [], "fills": [],
                          "cash_ledger": [], "trades": [], "audit": [], "seq": 0}
            for a in (default_accounts or []):
                self.create_account(a["id"], a["name"], a["currency"], a["cash"], a.get("strategies", []))
        return self

    def save(self):
        tmp = self.path + ".tmp"
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(tmp, "w") as f:
            json.dump(self.state, f, indent=1, default=_enc)
        os.replace(tmp, self.path)

    def _audit(self, kind, **kw):
        self.state["seq"] += 1
        self.state["audit"].append({"seq": self.state["seq"], "time": now_iso(), "kind": kind, **kw})
        self.state["audit"] = self.state["audit"][-5000:]

    # ---------------------------------------------------------------- accounts
    def create_account(self, id, name, currency, cash, strategies=None):
        if id in self.state["accounts"]:
            return self.state["accounts"][id]
        a = {"id": id, "name": name, "currency": currency, "start_cash": str(D(str(cash))), "cash": str(D(str(cash))),
             "created": now_iso(), "strategies": strategies or [], "positions": {}, "equity": [], "peak": str(D(str(cash))),
             "status": "ACTIVE", "realized_pnl": "0", "fees_paid": "0"}
        self.state["accounts"][id] = a
        self._cash(id, "DEPOSIT", D(str(cash)), "opening balance")
        self._audit("account_created", account=id, cash=str(cash), currency=currency)
        return a

    def account(self, id):
        return self.state["accounts"][id]

    def reset_account(self, id, cash=None):
        a = self.account(id)
        cash = D(str(cash)) if cash is not None else D(a["start_cash"])
        self._audit("account_reset", account=id, old_cash=a["cash"], positions=list(a["positions"].keys()))
        a.update(cash=str(cash), start_cash=str(cash), positions={}, equity=[], peak=str(cash), realized_pnl="0", fees_paid="0", status="ACTIVE")
        self.state["orders"] = [o for o in self.state["orders"] if o["account"] != id]
        self._cash(id, "RESET", cash, "account reset")

    def deposit(self, id, amount, note="deposit"):
        a = self.account(id); a["cash"] = str(D(a["cash"]) + D(str(amount)))
        self._cash(id, "DEPOSIT", D(str(amount)), note)

    def withdraw(self, id, amount, note="withdrawal"):
        a = self.account(id)
        if D(a["cash"]) < D(str(amount)):
            raise ValueError("insufficient cash")
        a["cash"] = str(D(a["cash"]) - D(str(amount)))
        self._cash(id, "WITHDRAW", -D(str(amount)), note)

    def _cash(self, account, kind, amount, ref):
        a = self.account(account)
        self.state["cash_ledger"].append({"time": now_iso(), "account": account, "type": kind, "amount": str(q2(amount)),
                                          "balance_after": str(q2(D(a["cash"]))), "ref": ref})

    # ---------------------------------------------------------------- orders
    def submit(self, account, symbol, side, qty, order_type="MARKET", limit=None, stop_px=None, bar_date=None,
               strategy="manual", signal_id=None, reason="", stop=None, target=None, dedupe_key=None, risk=None,
               segment="delivery", currency="INR", sector=None, expires_bars=3, meta=None):
        """Book an order as PENDING. Risk decisions are passed in (the risk engine runs before this)."""
        dedupe_key = dedupe_key or f"{account}|{symbol}|{side}|{bar_date}|{strategy}"
        for o in self.state["orders"]:
            if o.get("dedupe_key") == dedupe_key and o["status"] in ("PENDING", "PARTIAL", "FILLED"):
                self._audit("order_duplicate_blocked", order=o["id"], dedupe_key=dedupe_key)
                return o, "duplicate"
        o = {"id": f"O{self.state['seq'] + 1:07d}-{uuid.uuid4().hex[:6]}", "account": account, "symbol": symbol, "side": side,
             "qty": str(D(str(qty))), "filled_qty": "0", "type": order_type, "limit": str(limit) if limit is not None else None,
             "stop_px": str(stop_px) if stop_px is not None else None, "status": "PENDING", "created_at": now_iso(),
             "decided_on_bar": str(bar_date), "strategy": strategy, "signal_id": signal_id, "reason": reason,
             "stop": str(stop) if stop is not None else None, "target": str(target) if target is not None else None,
             "dedupe_key": dedupe_key, "risk": risk, "segment": segment, "currency": currency, "sector": sector,
             "expires_after_bars": expires_bars, "bars_waited": 0, "fills": [], "meta": meta or {}}
        self.state["orders"].append(o)
        self._audit("order_submitted", order=o["id"], account=account, symbol=symbol, side=side, qty=str(qty), type=order_type, strategy=strategy)
        return o, "ok"

    def reject(self, account, symbol, side, qty, reason, strategy="manual", bar_date=None, risk=None):
        o = {"id": f"R{self.state['seq'] + 1:07d}", "account": account, "symbol": symbol, "side": side, "qty": str(qty), "filled_qty": "0",
             "type": "MARKET", "status": "REJECTED", "created_at": now_iso(), "decided_on_bar": str(bar_date), "strategy": strategy,
             "reason": reason, "risk": risk, "fills": []}
        self.state["orders"].append(o)
        self._audit("order_rejected", order=o["id"], symbol=symbol, reason=reason)
        return o

    def cancel(self, order_id, reason="cancelled"):
        o = self.get_order(order_id)
        if o and o["status"] in ("PENDING", "PARTIAL"):
            o["status"] = "CANCELLED"; o["cancel_reason"] = reason; o["closed_at"] = now_iso()
            self._audit("order_cancelled", order=order_id, reason=reason)
        return o

    def get_order(self, order_id):
        return next((o for o in self.state["orders"] if o["id"] == order_id), None)

    def pending(self, account=None):
        return [o for o in self.state["orders"] if o["status"] in ("PENDING", "PARTIAL") and (account is None or o["account"] == account)]

    # ---------------------------------------------------------------- fills
    def fill(self, order, price, qty, bar_date, note="", fill_rule="next_bar_open", fee_override=None):
        """Apply a (possibly partial) fill. Updates cash, position, fees. Raises on insufficient cash/qty."""
        a = self.account(order["account"])
        price, qty = D(str(price)), D(str(qty))
        remaining = D(order["qty"]) - D(order["filled_qty"])
        qty = min(qty, remaining)
        if qty <= 0:
            return None
        value = price * qty
        if fee_override is not None:
            fee = D(str(fee_override)).quantize(D("0.01"))
            ch = {"total": fee, "method": "override (multi-leg / modeled)"}
        else:
            ch = costs.charges(value, order["side"], a["currency"], order.get("segment", "delivery"))
            fee = D(ch["total"])
        pos = a["positions"].get(order["symbol"])
        if order["side"] == "buy":
            need = value + fee
            if D(a["cash"]) < need:
                # scale down to what cash allows (partial fill), else reject
                afford = ((D(a["cash"]) - fee) / price).to_integral_value(rounding=ROUND_DOWN)
                if afford <= 0:
                    order["status"] = "REJECTED"; order["reason"] = (order.get("reason") or "") + " | insufficient cash at fill"
                    self._audit("fill_rejected_cash", order=order["id"], cash=a["cash"], need=str(q2(need)))
                    return None
                qty = afford; value = price * qty
                ch = costs.charges(value, "buy", a["currency"], order.get("segment", "delivery")); fee = D(ch["total"])
                note += f" | reduced to {qty} for cash"
            a["cash"] = str(D(a["cash"]) - value - fee)
            if pos:
                new_qty = D(pos["qty"]) + qty
                pos["avg_price"] = str(((D(pos["avg_price"]) * D(pos["qty"])) + value) / new_qty)
                pos["qty"] = str(new_qty); pos["fees"] = str(D(pos["fees"]) + fee)
            else:
                a["positions"][order["symbol"]] = pos = {"symbol": order["symbol"], "qty": str(qty), "avg_price": str(price), "fees": str(fee),
                                                          "opened": str(bar_date), "bars_held": 0, "stop": order.get("stop"), "target": order.get("target"),
                                                          "strategy": order.get("strategy"), "reason": order.get("reason"), "sector": order.get("sector"),
                                                          "segment": order.get("segment", "delivery"), "high_water": str(price), "last": str(price)}
            self._cash(order["account"], "BUY", -(value + fee), order["id"])
        else:
            if not pos or D(pos["qty"]) < qty:
                held = D(pos["qty"]) if pos else ZERO
                if held <= 0:
                    order["status"] = "REJECTED"; order["reason"] = (order.get("reason") or "") + " | no position to sell (short selling not enabled)"
                    self._audit("fill_rejected_no_position", order=order["id"])
                    return None
                qty = held; value = price * qty
                ch = costs.charges(value, "sell", a["currency"], order.get("segment", "delivery")); fee = D(ch["total"])
            proceeds = value - fee
            a["cash"] = str(D(a["cash"]) + proceeds)
            cost_basis = D(pos["avg_price"]) * qty
            entry_fee_share = D(pos["fees"]) * qty / D(pos["qty"])
            pnl = proceeds - cost_basis - entry_fee_share
            a["realized_pnl"] = str(D(a["realized_pnl"]) + pnl)
            self.state["trades"].append({"account": order["account"], "symbol": order["symbol"], "qty": str(qty), "entry_date": pos["opened"],
                                         "entry": pos["avg_price"], "exit_date": str(bar_date), "exit": str(price), "pnl": str(q2(pnl)),
                                         "pnl_pct": str(q2(pnl / cost_basis * 100)) if cost_basis else "0", "fees": str(q2(fee + entry_fee_share)),
                                         "bars_held": pos["bars_held"], "reason": note or order.get("reason"), "strategy": pos.get("strategy"), "order": order["id"]})
            self._cash(order["account"], "SELL", proceeds, order["id"])
            left = D(pos["qty"]) - qty
            if left <= 0:
                del a["positions"][order["symbol"]]
            else:
                pos["qty"] = str(left); pos["fees"] = str(D(pos["fees"]) - entry_fee_share)
        a["fees_paid"] = str(D(a["fees_paid"]) + fee)
        f = {"id": f"F{self.state['seq'] + 1:07d}", "order": order["id"], "account": order["account"], "symbol": order["symbol"], "side": order["side"],
             "qty": str(qty), "price": str(price), "value": str(q2(value)), "fees": str(fee), "fee_breakdown": {k: str(v) for k, v in ch.items() if k not in ("verification", "statutory_verification")},
             "bar": str(bar_date), "time": now_iso(), "rule": fill_rule, "note": note}
        self.state["fills"].append(f); order["fills"].append(f["id"])
        order["filled_qty"] = str(D(order["filled_qty"]) + qty)
        order["status"] = "FILLED" if D(order["filled_qty"]) >= D(order["qty"]) else "PARTIAL"
        if order["status"] == "FILLED":
            order["closed_at"] = now_iso()
        self._audit("fill", order=order["id"], fill=f["id"], symbol=order["symbol"], side=order["side"], qty=str(qty), price=str(price), fees=str(fee))
        return f

    # ---------------------------------------------------------------- valuation
    def mark(self, account, prices, bar_date):
        """prices: {symbol: last_price}. Appends an equity point and updates high-water marks."""
        a = self.account(account)
        value = D(a["cash"])
        for s, p in a["positions"].items():
            px = prices.get(s)
            if px is not None:
                p["last"] = str(D(str(px)))
                p["high_water"] = str(max(D(p["high_water"]), D(str(px))))
            value += D(p["last"]) * D(p["qty"])
        point = {"date": str(bar_date), "value": str(q2(value)), "cash": str(q2(D(a["cash"])))}
        if a["equity"] and a["equity"][-1]["date"] == str(bar_date):
            a["equity"][-1] = point
        else:
            a["equity"].append(point)
        a["peak"] = str(max(D(a["peak"]), value))
        return value

    def equity(self, account):
        a = self.account(account)
        return D(a["equity"][-1]["value"]) if a["equity"] else D(a["cash"])

    def drawdown(self, account):
        a = self.account(account)
        e, pk = self.equity(account), D(a["peak"])
        return (e / pk - 1) if pk else ZERO

    def summary(self, account):
        a = self.account(account)
        e = self.equity(account)
        gross = sum(D(p["last"]) * D(p["qty"]) for p in a["positions"].values())
        unreal = sum((D(p["last"]) - D(p["avg_price"])) * D(p["qty"]) for p in a["positions"].values())
        trades = [t for t in self.state["trades"] if t["account"] == account]
        wins = [t for t in trades if D(t["pnl"]) > 0]
        losses = [t for t in trades if D(t["pnl"]) <= 0]
        gp = sum(D(t["pnl"]) for t in wins); gl = -sum(D(t["pnl"]) for t in losses)
        return {"account": account, "name": a["name"], "currency": a["currency"], "status": a["status"], "equity": str(q2(e)), "cash": str(q2(D(a["cash"]))),
                "start_cash": a["start_cash"], "return_pct": str(q2((e / D(a["start_cash"]) - 1) * 100)) if D(a["start_cash"]) else "0",
                "realized_pnl": str(q2(D(a["realized_pnl"]))), "unrealized_pnl": str(q2(unreal)), "fees_paid": str(q2(D(a["fees_paid"]))),
                "gross_exposure": str(q2(gross)), "exposure_pct": str(q2(gross / e * 100)) if e else "0",
                "drawdown_pct": str(q2(self.drawdown(account) * 100)), "peak": a["peak"], "open_positions": len(a["positions"]),
                "pending_orders": len(self.pending(account)), "trades": len(trades), "wins": len(wins), "losses": len(losses),
                "win_rate": str(q2(D(len(wins)) / D(len(trades)) * 100)) if trades else None,
                "profit_factor": str(q2(gp / gl)) if gl > 0 else None,
                "avg_win": str(q2(gp / len(wins))) if wins else None, "avg_loss": str(q2(-gl / len(losses))) if losses else None,
                "expectancy": str(q2(sum(D(t["pnl"]) for t in trades) / len(trades))) if trades else None}
