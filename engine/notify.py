"""Telegram summary of the latest run. Skips silently when the token is absent. Never contains secrets."""
import os, sys, json, requests

def main(site="site"):
    token, chat = os.environ.get("TELEGRAM_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat:
        print("Telegram not configured; skipping."); return
    s = json.load(open(os.path.join(site, "api", "signals.json")))
    p = json.load(open(os.path.join(site, "api", "paper.json")))
    r = s["regime"]
    lines = [f"VISION AI - {r['date']} - {s.get('data_status')}", f"NIFTY {r['close']:,} - regime {r['state']}", ""]
    lines += ["BUY: " + (", ".join(f"{b['symbol']} @{b['entry']} SL {b['stop']} T {b['target']}" for b in s["buys"]) or "none")]
    lines += ["SELL/EXIT: " + (", ".join(b["symbol"] for b in s["sells"]) or "none")]
    for a in p["accounts"]:
        lines.append(f"{a['name']}: {a['equity']} ({a['return_pct']}%), {a['open_positions']} open")
    for aid, acts in (p.get("last_actions") or {}).items():
        lines += [f"{aid}: " + "; ".join(acts[:3])]
    lines.append("PAPER ONLY - not advice.")
    requests.post(f"https://api.telegram.org/bot{token}/sendMessage", json={"chat_id": chat, "text": "\n".join(lines), "disable_web_page_preview": True}, timeout=20)

if __name__ == "__main__":
    main(sys.argv[sys.argv.index("--site") + 1] if "--site" in sys.argv else "site")
