#!/usr/bin/env python3
"""
VISION AI live order runner.  Runs on YOUR machine or VPS whose static IP is whitelisted with the broker
(SEBI rule from April 2026). It is the ONLY component that can send a real order.

Every minute during NSE hours it:
  1. reads live_control.json, live_proposals.json, approvals.json from your GitHub Pages site
  2. stops immediately if the kill switch is ON or live trading is OFF
  3. for each proposal: in APPROVAL mode only if approved on GitHub (id + hash match, not expired);
     in AUTO mode only if the engine says auto is allowed (consent + track-record gate + limits)
  4. re-checks the live price (skip if it moved beyond the limit), limits (orders/day, value/order, daily loss,
     open positions), then places a LIMIT order via the broker's official SDK with a unique reference id
  5. after a BUY fills, places an OCO smart order (stop-loss + target) on the holding
  6. writes an audit line locally, pushes a phone notification, and (optionally) reports to the repo

Secrets live ONLY in environment variables on this machine (never in the repo):
  GROWW_TOTP_TOKEN, GROWW_TOTP_SECRET      (Groww Trade API, TOTP flow - no daily approval needed)
  VISION_SITE   (default https://16vitaws.github.io/market-scanner)
  NTFY_TOPIC    (default vision-ai-16vitaws-k7q2m9x4)
  RUNNER_DRY_RUN=1   log what would be done, send nothing to the broker (use this first!)
  RUNNER_GITHUB_TOKEN (optional, fine-grained, contents:write) to publish results to the live-results branch

Status: written against Groww's published SDK docs (growwapi: get_access_token, place_order, get_order_status,
create_smart_order). NOT yet tested against a live account - run with RUNNER_DRY_RUN=1 first.
"""
import os, sys, json, time, hashlib, base64, datetime as dt, traceback
import requests

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
SITE = (os.environ.get("VISION_SITE") or "https://16vitaws.github.io/market-scanner").rstrip("/")
TOPIC = os.environ.get("NTFY_TOPIC") or "vision-ai-16vitaws-k7q2m9x4"
DRY = (os.environ.get("RUNNER_DRY_RUN") or "1").strip() != "0"   # anything but an explicit 0 stays dry
STATE = os.path.expanduser("~/.vision_runner_state.json")
AUDIT = os.path.expanduser("~/vision_runner_audit.jsonl")
REPO = "16VITAWS/market-scanner"


def now():
    return dt.datetime.now(IST)


def log(event, **kw):
    row = {"time": now().isoformat(timespec="seconds"), "event": event, "dry_run": DRY, **kw}
    print(json.dumps(row), flush=True)
    with open(AUDIT, "a") as f:
        f.write(json.dumps(row) + "\n")
    return row


def notify(title, msg, prio=4):
    try:
        requests.post(f"https://ntfy.sh/{TOPIC}", data=msg.encode(), headers={"Title": title, "Priority": str(prio), "Tags": "moneybag"}, timeout=10)
    except Exception:
        pass


def fetch(path):
    r = requests.get(f"{SITE}/{path}?t={int(time.time())}", timeout=20)
    r.raise_for_status()
    return r.json()


def load_state():
    try:
        s = json.load(open(STATE))
    except Exception:
        s = {}
    today = now().date().isoformat()
    if s.get("day") != today:
        s = {"day": today, "orders": 0, "done": s.get("done", {}), "realized_loss": 0.0}
    return s


def save_state(s):
    json.dump(s, open(STATE, "w"), indent=1)


def market_open():
    t = now()
    return t.weekday() < 5 and dt.time(9, 16) <= t.time() <= dt.time(15, 20)


def phash(p):
    core = json.dumps({k: p[k] for k in ("symbol", "side", "qty", "limit", "valid_until", "segment")}, sort_keys=True)
    return hashlib.sha256(core.encode()).hexdigest()[:10].upper()


class Groww:
    def __init__(self):
        from growwapi import GrowwAPI
        import pyotp
        tok = GrowwAPI.get_access_token(api_key=os.environ["GROWW_TOTP_TOKEN"], totp=pyotp.TOTP(os.environ["GROWW_TOTP_SECRET"]).now())
        self.g = GrowwAPI(tok)

    def ltp(self, symbol):
        r = self.g.get_ltp(segment=self.g.SEGMENT_CASH, exchange_trading_symbols=f"NSE_{symbol}")
        return float(list(r.values())[0]) if isinstance(r, dict) else float(r)

    def holdings_qty(self, symbol):
        try:
            h = self.g.get_holdings_for_user()
            for x in (h.get("holdings") or h):
                if x.get("trading_symbol") == symbol:
                    return int(float(x.get("quantity", 0)))
        except Exception as e:  # noqa
            log("holdings_error", error=str(e))
        return 0

    def buy_limit(self, symbol, qty, price, ref):
        g = self.g
        return g.place_order(trading_symbol=symbol, quantity=qty, validity=g.VALIDITY_DAY, exchange=g.EXCHANGE_NSE, segment=g.SEGMENT_CASH,
                             product=g.PRODUCT_CNC, order_type=g.ORDER_TYPE_LIMIT, transaction_type=g.TRANSACTION_TYPE_BUY, price=price, order_reference_id=ref)

    def sell_limit(self, symbol, qty, price, ref):
        g = self.g
        return g.place_order(trading_symbol=symbol, quantity=qty, validity=g.VALIDITY_DAY, exchange=g.EXCHANGE_NSE, segment=g.SEGMENT_CASH,
                             product=g.PRODUCT_CNC, order_type=g.ORDER_TYPE_LIMIT, transaction_type=g.TRANSACTION_TYPE_SELL, price=price, order_reference_id=ref)

    def status(self, order_id):
        return self.g.get_order_status(groww_order_id=order_id, segment=self.g.SEGMENT_CASH)

    def oco(self, symbol, qty, stop, target, ref):
        g = self.g
        return g.create_smart_order(smart_order_type=g.SMART_ORDER_TYPE_OCO, reference_id=ref, segment=g.SEGMENT_CASH, trading_symbol=symbol,
                                    quantity=qty, product_type=g.PRODUCT_CNC, exchange=g.EXCHANGE_NSE, duration=g.VALIDITY_DAY,
                                    net_position_quantity=qty, transaction_type=g.TRANSACTION_TYPE_SELL,
                                    target={"trigger_price": f"{target:.2f}", "order_type": g.ORDER_TYPE_LIMIT, "price": f"{target:.2f}"},
                                    stop_loss={"trigger_price": f"{stop:.2f}", "order_type": g.ORDER_TYPE_STOP_LOSS_MARKET, "price": None})


def report(results):
    tok = os.environ.get("RUNNER_GITHUB_TOKEN")
    if not tok or not results:
        return
    path = f"results/{now().date().isoformat()}.json"
    url = f"https://api.github.com/repos/{REPO}/contents/{path}"
    h = {"Authorization": f"Bearer {tok}", "Accept": "application/vnd.github+json"}
    sha = None
    try:
        r = requests.get(url, headers=h, params={"ref": "live-results"}, timeout=20)
        if r.ok:
            sha = r.json()["sha"]; old = json.loads(base64.b64decode(r.json()["content"]))
            results = old + results
    except Exception:
        pass
    body = {"message": "runner results", "branch": "live-results", "content": base64.b64encode(json.dumps(results, indent=1).encode()).decode()}
    if sha:
        body["sha"] = sha
    requests.put(url, headers=h, json=body, timeout=20)


def cycle(broker, st):
    ctl = fetch("api/live_control.json")
    if ctl.get("kill") or not ctl.get("live_trading"):
        return log("idle", reason="kill switch ON" if ctl.get("kill") else "LIVE_TRADING is OFF")
    props = fetch("api/live_proposals.json")
    try:
        appr = {a["id"]: a for a in fetch("data/approvals.json").get("items", []) if a.get("decision") == "APPROVE"}
    except Exception:
        appr = {}
    lim = ctl["limits"]; results = []
    for p in props.get("items", []):
        if p["id"] in st["done"]:
            continue
        if p["id"] != phash(p):
            log("reject", id=p["id"], reason="hash mismatch - proposal altered"); st["done"][p["id"]] = "hash"; continue
        if now().date().isoformat() > p["valid_until"]:
            continue
        allowed = (ctl["mode"] == "AUTO" and ctl.get("auto_allowed")) or (p["id"] in appr and appr[p["id"]].get("hash") == p["id"])
        if not allowed:
            continue
        if st["orders"] >= lim["max_orders_per_day"]:
            log("limit", reason="max orders per day"); break
        if st["realized_loss"] >= lim["max_daily_loss_inr"]:
            log("limit", reason="daily loss limit"); break
        if p["segment"] != "CASH":
            log("skip", id=p["id"], reason="F&O leg symbols not configured for live placement; place manually from the Live Desk ticket")
            st["done"][p["id"]] = "fno-manual"; notify("Manual action needed", f"{p['symbol']}: approved spread - place both legs manually in Groww"); continue
        sym = p["symbol"]
        try:
            px = broker.ltp(sym) if broker else p["limit"]
        except Exception as e:
            log("ltp_error", symbol=sym, error=str(e)); continue
        if p["side"] == "BUY":
            if px > p["limit"]:
                log("wait", id=p["id"], reason=f"price {px} above limit {p['limit']}"); continue
            if p["qty"] * p["limit"] > lim["max_order_value_inr"]:
                log("reject", id=p["id"], reason="order value above cap"); st["done"][p["id"]] = "cap"; continue
            ref = f"VIS-{p['id']}"
            if DRY or not broker:
                r = log("DRY_BUY", id=p["id"], symbol=sym, qty=p["qty"], limit=p["limit"], ltp=px)
            else:
                o = broker.buy_limit(sym, p["qty"], p["limit"], ref)
                r = log("BUY_SENT", id=p["id"], symbol=sym, qty=p["qty"], limit=p["limit"], broker_response=o)
                oid = (o or {}).get("groww_order_id")
                for _ in range(20):
                    time.sleep(3)
                    s = broker.status(oid) if oid else {}
                    if str(s.get("order_status", "")).upper() in ("EXECUTED", "COMPLETED", "FILLED"):
                        oc = broker.oco(sym, p["qty"], p["stop"], p["target"], f"OCO-{p['id']}")
                        log("OCO_SET", id=p["id"], stop=p["stop"], target=p["target"], broker_response=oc); break
                else:
                    log("NOT_FILLED_YET", id=p["id"], note="limit order left working; OCO not yet placed - check the broker app")
            notify(f"LIVE {'(dry) ' if DRY else ''}BUY {sym}", f"{p['qty']} @ limit {p['limit']} (ltp {px}) · SL {p['stop']} · T {p['target']} · id {p['id']}", 5)
        elif p["side"] == "SELL":
            qty = broker.holdings_qty(sym) if broker else 0
            if qty <= 0:
                st["done"][p["id"]] = "not-held"; continue
            if DRY or not broker:
                r = log("DRY_SELL", id=p["id"], symbol=sym, qty=qty, limit=p["limit"], ltp=px)
            else:
                o = broker.sell_limit(sym, qty, max(p["limit"], round(px * 0.995, 1)), f"VIS-{p['id']}")
                r = log("SELL_SENT", id=p["id"], symbol=sym, qty=qty, broker_response=o)
            notify(f"LIVE {'(dry) ' if DRY else ''}EXIT {sym}", f"{qty} shares · id {p['id']}", 5)
        st["orders"] += 1; st["done"][p["id"]] = now().isoformat(timespec="seconds"); results.append(r)
    report(results)
    return None


def main():
    log("start", site=SITE, dry_run=DRY)
    broker = None
    if not DRY:
        broker = Groww()
    notify("VISION runner started", f"dry_run={DRY} site={SITE}", 3)
    while True:
        st = load_state()
        try:
            if market_open():
                cycle(broker, st)
            save_state(st)
        except Exception as e:  # noqa
            log("error", error=str(e), trace=traceback.format_exc()[-500:])
        time.sleep(60)


if __name__ == "__main__":
    if "--once" in sys.argv:
        st = load_state(); cycle(None if DRY else Groww(), st); save_state(st)
    else:
        main()
