"""
Your own server-side alert rules (pushed to your phone even when the portal is closed).

Set the GitHub repository VARIABLE  ALERT_RULES  (Settings -> Secrets and variables -> Actions -> Variables), e.g.
    RELIANCE>3000; NIFTY<24000; BANKNIFTY%>2; TCS%<-3; INDIAVIX>18; BRENT%>4
  SYMBOL>price   price at or above        SYMBOL<price   price at or below
  SYMBOL%>x      day change >= +x %       SYMBOL%<x      day change <= x %  (x negative for falls)
Symbols are portal ids (NSE symbols, NIFTY, BANKNIFTY, SPX, BRENT, GOLD, USDINR ...). Checked every 15 minutes in
market hours and at end of day, on delayed quotes. Each rule fires at most once per day.
"""
import re

PAT = re.compile(r"^\s*([A-Z0-9&_\-\.]+)\s*(%?)\s*([<>])\s*(-?[0-9]+(?:\.[0-9]+)?)\s*$")


def parse(text):
    rules, bad = [], []
    for part in re.split(r"[;,\n]+", (text or "").upper()):
        if not part.strip():
            continue
        m = PAT.match(part)
        if not m:
            bad.append(part.strip()); continue
        sym, pct, op, val = m.groups()
        rules.append({"symbol": sym, "kind": "change_pct" if pct else "price", "op": op, "value": float(val), "text": part.strip()})
    return rules, bad


def evaluate(rules, quotes):
    hits = []
    for r in rules:
        q = quotes.get(r["symbol"])
        if not q:
            continue
        x = q.get("chg_pct") if r["kind"] == "change_pct" else q.get("p")
        if x is None:
            continue
        if (r["op"] == ">" and x >= r["value"]) or (r["op"] == "<" and x <= r["value"]):
            hits.append({**r, "observed": x, "quote_time": q.get("t") or q.get("d")})
    return hits
