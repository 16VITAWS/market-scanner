"""Records an APPROVE / REJECT for a live proposal (run only by the 'Approve live order' GitHub workflow)."""
import os, sys, json, datetime as dt
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))


def main(site):
    pid = os.environ.get("PID", "").strip().upper()
    decision = os.environ.get("DECISION", "").strip().upper()
    if os.environ.get("CONFIRM", "").strip().upper() != "YES":
        sys.exit("Not confirmed: type YES in the confirm box.")
    if decision not in ("APPROVE", "REJECT"):
        sys.exit("decision must be APPROVE or REJECT")
    props = json.load(open(os.path.join(site, "api", "live_proposals.json")))
    p = next((x for x in props.get("items", []) if x["id"] == pid), None)
    if not p:
        sys.exit(f"Proposal {pid} not found in today's proposals.")
    if dt.datetime.now(IST).date().isoformat() > p["valid_until"]:
        sys.exit(f"Proposal {pid} expired on {p['valid_until']}.")
    path = os.path.join(site, "data", "approvals.json")
    try:
        a = json.load(open(path))
    except Exception:
        a = {"items": []}
    a["items"] = [x for x in a["items"] if x["id"] != pid] + [{"id": pid, "hash": pid, "decision": decision, "symbol": p["symbol"], "side": p["side"],
                                                                "qty": p["qty"], "limit": p["limit"], "by": os.environ.get("ACTOR", "?"),
                                                                "at": dt.datetime.now(IST).isoformat(timespec="seconds"), "valid_until": p["valid_until"]}]
    a["items"] = a["items"][-200:]
    os.makedirs(os.path.dirname(path), exist_ok=True)
    json.dump(a, open(path, "w"), indent=1)
    print(f"{decision}D {pid}: {p['side']} {p['qty']} {p['symbol']} limit {p['limit']}")


if __name__ == "__main__":
    main(sys.argv[sys.argv.index("--site") + 1] if "--site" in sys.argv else "site")
