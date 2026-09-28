"""
Live execution module — LOCKED by default.

Flow (never SIGNAL -> BROKER directly):
    signal -> risk gate -> execution -> broker adapter -> Groww -> exchange

To go live you need ALL of these; the module refuses otherwise:
  1. LIVE_MODE = True in config.py
  2. GROWW_API_KEY and GROWW_API_SECRET set as environment variables (never in code)
  3. Groww's official Python SDK installed (pip install growwapi — see Groww API docs)
  4. Groww's algo approval / algo ID (SEBI rule) — set GROWW_ALGO_ID
  5. No file named KILL_SWITCH in the data folder
Nothing here invents an order: every live order is confirmed only from the broker's
own order status, and every attempt is written to data/audit_log.csv.
"""
import os, csv, json, uuid, datetime as dt
import config as C

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
AUDIT = os.path.join(DATA, "audit_log.csv")
SENT = os.path.join(DATA, "sent_orders.json")
KILL = os.path.join(DATA, "KILL_SWITCH")
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))

LIVE_MODE = getattr(C, "LIVE_MODE", False)
AUTO_LIVE = getattr(C, "AUTO_LIVE", False)

# Order state machine (persisted in audit log)
STATES = ["DRAFT", "RISK_REJECTED", "AWAITING_USER_APPROVAL", "APPROVED", "SUBMITTING", "OPEN",
          "PARTIALLY_FILLED", "FILLED", "CANCEL_REQUESTED", "CANCELLED", "REJECTED", "EXPIRED",
          "UNKNOWN_RECONCILIATION_REQUIRED", "CLOSED"]
MAX_ORDERS_PER_DAY = getattr(C, "MAX_ORDERS_PER_DAY", 10)
MAX_DAILY_LOSS = getattr(C, "MAX_DAILY_LOSS", 0.02)      # 2% of capital, then stop for the day


class NotReady(Exception):
    """Raised when any live-trading precondition is missing."""


def audit(**row):
    os.makedirs(DATA, exist_ok=True)
    row = {"time": dt.datetime.now(IST).isoformat(timespec="seconds"), **row}
    new = not os.path.exists(AUDIT)
    with open(AUDIT, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["time", "mode", "signal_id", "symbol", "side", "qty", "price", "status", "broker_order_id", "note"])
        if new: w.writeheader()
        w.writerow({k: row.get(k, "") for k in w.fieldnames})


# ---------------------------------------------------------------- risk gate
def risk_gate(order, portfolio_value, todays_pnl, orders_today, data_time):
    """Every check must pass. Returns (ok, reason)."""
    now = dt.datetime.now(IST)
    if os.path.exists(KILL):
        return False, "kill switch is on"
    if now.weekday() > 4 or not (dt.time(9, 20) <= now.time() <= dt.time(15, 15)):
        return False, "outside trading hours"
    if data_time is None or (now - data_time).total_seconds() > 120:
        return False, "market data older than 2 minutes"
    if orders_today >= MAX_ORDERS_PER_DAY:
        return False, "daily order limit reached"
    if todays_pnl <= -MAX_DAILY_LOSS * portfolio_value:
        return False, "daily loss limit hit — no more trades today"
    if order["qty"] <= 0:
        return False, "quantity is zero"
    if order["side"] == "buy" and order["qty"] * order["price"] > portfolio_value * 0.25:
        return False, "single position would exceed 25% of capital"
    if order["side"] == "buy" and order["price"] - order["stop"] <= 0:
        return False, "stop-loss is not below entry"
    return True, "ok"


# ---------------------------------------------------------------- broker adapters
class BrokerAdapter:
    name = "base"
    def connect(self): raise NotImplementedError
    def place_order(self, symbol, side, qty, order_type="MARKET", price=None): raise NotImplementedError
    def order_status(self, broker_order_id): raise NotImplementedError
    def cancel_order(self, broker_order_id): raise NotImplementedError
    def positions(self): raise NotImplementedError
    def funds(self): raise NotImplementedError


class GrowwAdapter(BrokerAdapter):
    """Thin wrapper over Groww's official SDK. Method names follow Groww's docs at the
    time of writing; verify against https://groww.in/trade-api/docs before first live use."""
    name = "groww"

    def __init__(self):
        self.key = os.environ.get("GROWW_API_KEY")
        self.secret = os.environ.get("GROWW_API_SECRET")
        self.algo_id = os.environ.get("GROWW_ALGO_ID")
        self.api = None

    def connect(self):
        if not (self.key and self.secret):
            raise NotReady("GROWW_API_KEY / GROWW_API_SECRET not set")
        if not self.algo_id:
            raise NotReady("GROWW_ALGO_ID missing — SEBI requires broker algo approval before automatic orders")
        try:
            from growwapi import GrowwAPI          # official SDK; pip install growwapi
        except ImportError:
            raise NotReady("Groww SDK not installed (pip install growwapi)")
        token = GrowwAPI.get_access_token(api_key=self.key, secret=self.secret)
        self.api = GrowwAPI(token)
        return True

    def place_order(self, symbol, side, qty, order_type="MARKET", price=None):
        if self.api is None: raise NotReady("not connected")
        return self.api.place_order(trading_symbol=symbol, quantity=qty, validity="DAY",
                                    exchange="NSE", segment="CASH", product="CNC",
                                    order_type=order_type, transaction_type=side.upper(),
                                    price=price, order_reference_id=str(uuid.uuid4())[:20])

    def order_status(self, broker_order_id):
        return self.api.get_order_status(groww_order_id=broker_order_id, segment="CASH")

    def cancel_order(self, broker_order_id):
        return self.api.cancel_order(groww_order_id=broker_order_id, segment="CASH")

    def positions(self):
        return self.api.get_positions_for_user(segment="CASH")

    def funds(self):
        return self.api.get_available_margin_details()


# ---------------------------------------------------------------- execution
def _sent():
    if os.path.exists(SENT):
        with open(SENT) as f: return json.load(f)
    return {}


def execute(signal, portfolio_value, todays_pnl, orders_today, data_time, broker=None):
    """
    signal: {"id","symbol","side","qty","price","stop","target","reason"}
    Returns a dict with status. In PAPER mode it only records the intent.
    """
    sent = _sent()
    if signal["id"] in sent:                                   # idempotency: never send twice
        audit(mode="live" if LIVE_MODE else "paper", signal_id=signal["id"], symbol=signal["symbol"],
              side=signal["side"], qty=signal["qty"], status="DUPLICATE_BLOCKED")
        return {"status": "duplicate", "note": "already sent"}

    ok, reason = risk_gate(signal, portfolio_value, todays_pnl, orders_today, data_time)
    if not ok:
        audit(mode="live" if LIVE_MODE else "paper", signal_id=signal["id"], symbol=signal["symbol"],
              side=signal["side"], qty=signal["qty"], price=signal["price"], status="REJECTED_BY_RISK", note=reason)
        return {"status": "rejected", "note": reason}

    if not LIVE_MODE:
        audit(mode="paper", signal_id=signal["id"], symbol=signal["symbol"], side=signal["side"],
              qty=signal["qty"], price=signal["price"], status="PAPER_ONLY", note="LIVE_MODE is off")
        return {"status": "paper", "note": "live mode locked"}
    if not AUTO_LIVE and not signal.get("user_approved"):
        audit(mode="live", signal_id=signal["id"], symbol=signal["symbol"], side=signal["side"],
              qty=signal["qty"], price=signal["price"], status="AWAITING_USER_APPROVAL",
              note="APPROVAL mode: waiting for you to tap Approve and Place Order")
        return {"status": "awaiting_approval"}

    broker = broker or GrowwAdapter()
    try:
        broker.connect()
        resp = broker.place_order(signal["symbol"], signal["side"], signal["qty"])
        broker_id = (resp or {}).get("groww_order_id")
        status = broker.order_status(broker_id) if broker_id else {}
        final = (status or {}).get("order_status", "UNKNOWN")      # confirmed from broker, not from HTTP 200
        sent[signal["id"]] = {"broker_order_id": broker_id, "status": final, "time": dt.datetime.now(IST).isoformat()}
        with open(SENT, "w") as f: json.dump(sent, f, indent=1)
        audit(mode="live", signal_id=signal["id"], symbol=signal["symbol"], side=signal["side"],
              qty=signal["qty"], price=signal["price"], status=final, broker_order_id=broker_id)
        return {"status": final, "broker_order_id": broker_id}
    except NotReady as e:
        audit(mode="live", signal_id=signal["id"], symbol=signal["symbol"], side=signal["side"],
              qty=signal["qty"], status="NOT_READY", note=str(e))
        return {"status": "not_ready", "note": str(e)}
    except Exception as e:                                     # timeout / network: never resend
        sent[signal["id"]] = {"broker_order_id": None, "status": "UNKNOWN_RECONCILIATION_REQUIRED",
                              "time": dt.datetime.now(IST).isoformat()}
        with open(SENT, "w") as f: json.dump(sent, f, indent=1)
        audit(mode="live", signal_id=signal["id"], symbol=signal["symbol"], side=signal["side"],
              qty=signal["qty"], status="UNKNOWN_RECONCILIATION_REQUIRED", note=str(e)[:200])
        return {"status": "unknown_reconciliation_required", "note": "check broker order book before any retry"}


def readiness():
    """What is still missing before live trading can be switched on."""
    checks = {
        "LIVE_MODE flag in config.py": LIVE_MODE,
        "GROWW_API_KEY set": bool(os.environ.get("GROWW_API_KEY")),
        "GROWW_API_SECRET set": bool(os.environ.get("GROWW_API_SECRET")),
        "GROWW_ALGO_ID (broker algo approval)": bool(os.environ.get("GROWW_ALGO_ID")),
        "Groww SDK installed": _has("growwapi"),
        "Kill switch off": not os.path.exists(KILL),
    }
    return checks, all(checks.values())


def _has(mod):
    try:
        __import__(mod); return True
    except ImportError:
        return False


if __name__ == "__main__":
    checks, ready = readiness()
    for k, v in checks.items():
        print(("OK  " if v else "MISSING  ") + k)
    print("LIVE TRADING READY" if ready else "LIVE TRADING LOCKED")
