"""
Notifications. Primary channel: ntfy.sh (free push to Android/iPhone/PC; install the ntfy app or open
https://ntfy.sh/<topic> in a browser and subscribe). Optional: Telegram if TELEGRAM_* secrets exist.
Nothing secret is ever sent; messages contain paper-trading information only.
Dedupe: data/notified.json remembers what was already sent so re-runs do not spam.
"""
import os, sys, json, hashlib, datetime as dt
import requests
from . import config as C

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))


class Notifier:
    def __init__(self, site_path, dry=False):
        self.path = os.path.join(site_path, "data", "notified.json")
        self.dry = dry
        try:
            self.seen = json.load(open(self.path))
        except Exception:
            self.seen = {}
        self.sent, self.log = 0, []

    def push(self, title, msg, key=None, tags=("chart_with_upwards_trend",), priority=3, click=None):
        key = key or hashlib.sha1((title + msg).encode()).hexdigest()[:16]
        if key in self.seen:
            return False
        self.seen[key] = dt.datetime.now(IST).isoformat(timespec="seconds")
        self.log.append({"time": self.seen[key], "title": title, "msg": msg, "tags": list(tags), "priority": priority})
        if self.dry:
            return True
        ok = False
        try:
            r = requests.post(f"{C.NTFY_SERVER}/{C.NTFY_TOPIC}", data=msg.encode("utf-8"),
                              headers={"Title": title.encode("utf-8").decode("latin-1", "ignore"), "Tags": ",".join(tags), "Priority": str(priority),
                                       **({"Click": click} if click else {})}, timeout=15)
            ok = r.ok
        except Exception as e:  # noqa
            self.log[-1]["error"] = str(e)
        tok, chat = os.environ.get("TELEGRAM_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
        if tok and chat:
            try:
                requests.post(f"https://api.telegram.org/bot{tok}/sendMessage", json={"chat_id": chat, "text": f"{title}\n{msg}", "disable_web_page_preview": True}, timeout=15)
            except Exception:
                pass
        self.sent += 1
        return ok

    def save(self):
        keep = dict(sorted(self.seen.items(), key=lambda kv: kv[1])[-3000:])
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        json.dump(keep, open(self.path, "w"))
        feed = os.path.join(os.path.dirname(os.path.dirname(self.path)), "api", "notifications.json")
        try:
            old = json.load(open(feed)).get("items", [])
        except Exception:
            old = []
        json.dump({"generated_at": dt.datetime.now(IST).isoformat(timespec="seconds"), "topic": C.NTFY_TOPIC, "server": C.NTFY_SERVER,
                   "subscribe_url": f"{C.NTFY_SERVER}/{C.NTFY_TOPIC}", "items": (self.log[::-1] + old)[:200]}, open(feed, "w"))


PORTAL = "https://16vitaws.github.io/market-scanner/"


def eod(n, scan, scan_us, opt_sig, actions):
    r = scan["regime"]
    d = r["date"]
    lines = [f"NIFTY {r['close']:,} · regime {r['state']} · {scan['scanned']} scored",
             "BUY: " + (", ".join(f"{b['symbol']}@{b['entry']}" for b in scan["buys"]) or "none"),
             "SELL/EXIT: " + (", ".join(b["symbol"] for b in scan["sells"]) or "none")]
    if scan_us:
        ru = scan_us["regime"]
        lines.append(f"US: S&P {ru['close']:,} {ru['state']} · buys " + (", ".join(b["symbol"] for b in scan_us["buys"]) or "none"))
    if opt_sig:
        lines.append("Options: " + (opt_sig.get("name") or "no trade") )
    lines.append("Paper only - not advice.")
    n.push(f"VISION AI · EOD {d}", "\n".join(lines), key=f"eod|{d}", tags=("bar_chart",), click=PORTAL)
    for s in scan["buys"]:
        n.push(f"BUY signal {s['symbol']} (paper)", f"Score {s['score']:+d} · near ₹{s['entry']} · SL ₹{s['stop']} · T ₹{s['target']}\n" + "; ".join(s["why"][:3]),
               key=f"buy|{s['symbol']}|{s['bar']}", tags=("green_circle",), priority=4, click=PORTAL + "#scanner")
    for s in scan["sells"]:
        n.push(f"EXIT signal {s['symbol']}", f"Score {s['score']:+d} · ₹{s['entry']}\n" + "; ".join(s["why"][:3]), key=f"sell|{s['symbol']}|{s.get('bar')}", tags=("red_circle",), click=PORTAL + "#scanner")
    for s in (scan_us or {}).get("buys", []):
        n.push(f"US BUY signal {s['symbol']} (paper)", f"Score {s['score']:+d} · near ${s['entry']} · SL ${s['stop']} · T ${s['target']}", key=f"usbuy|{s['symbol']}|{s['bar']}", tags=("us",), priority=4, click=PORTAL + "#scanner")
    if opt_sig and opt_sig.get("action") == "BUY":
        n.push(f"OPTIONS signal: {opt_sig['name']}", f"Debit {opt_sig['debit']} (MODELED) · max loss/unit {opt_sig['max_loss']} · max profit/unit {opt_sig['max_profit']} · breakeven {opt_sig['breakeven']}\n" + "; ".join(opt_sig["why"]),
               key=f"opt|{opt_sig['name']}", tags=("dart",), priority=4, click=PORTAL + "#options")
    for aid, acts in actions.items():
        for a in acts:
            if a.startswith(("BOUGHT", "SOLD", "EXIT", "BUY ", "SELL ", "Queued BUY", "Queued EXIT", "Risk engine REJECTED")):
                n.push(f"Paper {aid}", a, key=f"act|{aid}|{a}", tags=("memo",), click=PORTAL + "#paper")


def main(site="site"):
    """Back-compat entry used by the workflow step; EOD pushes are sent from engine.run now."""
    print("Notifications are sent by engine.run; topic:", C.NTFY_TOPIC)


if __name__ == "__main__":
    main(sys.argv[sys.argv.index("--site") + 1] if "--site" in sys.argv else "site")
