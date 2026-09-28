"""
Event filters applied before any new entry:
  - F&O ban list (NSE publishes daily; securities in ban cannot take fresh F&O positions) -> block options on them,
    flag the cash stock as "in F&O ban (high open interest)".
  - Results / earnings date within the next 3 sessions -> block new entries (gap risk around results).
  - Ex-dividend within the next 2 sessions -> flag (price drops by the dividend; stops can trigger falsely).
Sources: NSE archives CSV (ban list), Yahoo calendar via yfinance (earnings / ex-dividend; may be missing).
Every lookup failure is reported, never silently treated as "no event".
"""
import io, datetime as dt
import pandas as pd
import requests

UA = {"User-Agent": "Mozilla/5.0 (personal-scanner/2.2)"}
BAN_URL = "https://archives.nseindia.com/content/fo/fo_secban.csv"


def fo_ban():
    try:
        r = requests.get(BAN_URL, headers=UA, timeout=20)
        r.raise_for_status()
        lines = [l.strip() for l in r.text.splitlines() if l.strip()]
        syms = []
        for l in lines[1:]:
            parts = [p.strip() for p in l.split(",") if p.strip()]
            if parts:
                syms.append(parts[-1])
        return {"status": "FETCHED", "date_line": lines[0] if lines else "", "symbols": syms}
    except Exception as e:  # noqa
        return {"status": f"unavailable: {e}", "symbols": []}


def calendar(symbol_yahoo):
    try:
        import yfinance as yf
        c = yf.Ticker(symbol_yahoo).calendar or {}
        out = {}
        e = c.get("Earnings Date")
        if isinstance(e, (list, tuple)) and e:
            out["earnings"] = str(pd.Timestamp(e[0]).date())
        elif e:
            out["earnings"] = str(pd.Timestamp(e).date())
        if c.get("Ex-Dividend Date"):
            out["ex_dividend"] = str(pd.Timestamp(c["Ex-Dividend Date"]).date())
        return out
    except Exception as e:  # noqa
        return {"error": str(e)}


def check(symbols, today, yahoo_map, ban=None):
    """Returns {symbol: {"block": reason|None, "flags": [...], "calendar": {...}}}"""
    ban = ban or {"symbols": []}
    out = {}
    for s in symbols:
        cal = calendar(yahoo_map.get(s, s + ".NS"))
        flags, block = [], None
        if s in ban.get("symbols", []):
            flags.append("in NSE F&O ban list (open interest above limit): no fresh F&O positions")
        if cal.get("earnings"):
            days = (dt.date.fromisoformat(cal["earnings"]) - today).days
            if 0 <= days <= 4:
                block = f"results on {cal['earnings']} ({days} days) - no new entry before results"
        if cal.get("ex_dividend"):
            days = (dt.date.fromisoformat(cal["ex_dividend"]) - today).days
            if 0 <= days <= 2:
                flags.append(f"ex-dividend {cal['ex_dividend']}: price will drop by the dividend")
        if "error" in cal:
            flags.append("event calendar unavailable for this symbol")
        out[s] = {"block": block, "flags": flags, "calendar": cal}
    return out
