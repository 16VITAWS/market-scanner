"""
Institutional-flow proxies ("big bulls / big bears") from free, official, END-OF-DAY NSE files.

  1. Bulk deals  (a client trading > 0.5% of a company's shares in a day)  - archives.nseindia.com/content/equities/bulk.csv
  2. Block deals (single trades of >= Rs 10 cr in the block window)        - archives.nseindia.com/content/equities/block.csv
  3. Delivery %  (share of traded quantity actually delivered, from the full bhavcopy sec_bhavdata_full_DDMMYYYY.csv)

Accumulation / distribution flag (per stock, per day):
  volume >= 2x its 20-day average AND delivery % at least 10 points above its own recent average (or >= 60% when
  history is short) AND close up >= 1%  -> ACCUMULATION;   same with close down <= -1% -> DISTRIBUTION.
These are PROXIES. Real-time institutional order flow (tick-by-tick, order book) is not available on free feeds,
so nothing here claims to see orders as they happen. Client names come from NSE's public disclosure files.
"""
import io, datetime as dt
import pandas as pd
import requests

UA = {"User-Agent": "Mozilla/5.0 (personal-scanner/2.3)", "Accept": "text/csv,*/*"}
HOSTS = ("https://archives.nseindia.com", "https://nsearchives.nseindia.com")


def _get(path):
    last = None
    for h in HOSTS:
        try:
            r = requests.get(h + path, headers=UA, timeout=25)
            if r.ok and len(r.content) > 50:
                return r.text, h + path
            last = f"HTTP {r.status_code}"
        except Exception as e:  # noqa
            last = str(e)
    raise RuntimeError(last or "unavailable")


def parse_deals(text, kind):
    df = pd.read_csv(io.StringIO(text))
    df.columns = [c.strip() for c in df.columns]
    col = {c.lower(): c for c in df.columns}
    pick = lambda *names: next((col[n] for n in names if n in col), None)
    c_sym, c_cli, c_side, c_qty, c_px, c_date = (pick("symbol"), pick("client name"), pick("buy/sell", "buy / sell"),
                                                pick("quantity traded", "quantity"), pick("trade price / wght. avg. price", "trade price / wght. avg. price", "price"), pick("date"))
    out = []
    for _, r in df.iterrows():
        try:
            qty = float(str(r[c_qty]).replace(",", "")); px = float(str(r[c_px]).replace(",", ""))
        except Exception:
            continue
        side = str(r[c_side]).strip().upper()
        out.append({"kind": kind, "date": str(r[c_date]).strip() if c_date else None, "symbol": str(r[c_sym]).strip().upper(),
                    "client": str(r[c_cli]).strip() if c_cli else "", "side": "BUY" if side.startswith("B") else "SELL",
                    "qty": int(qty), "price": round(px, 2), "value_cr": round(qty * px / 1e7, 2)})
    return out


def deals():
    items, status = [], {}
    for kind in ("bulk", "block"):
        try:
            txt, url = _get(f"/content/equities/{kind}.csv")
            got = parse_deals(txt, kind)
            items += got; status[kind] = {"status": "FETCHED", "rows": len(got), "url": url}
        except Exception as e:  # noqa
            status[kind] = {"status": f"unavailable: {e}"}
    return items, status


def summarise_deals(items, universe=None):
    by = {}
    for d in items:
        s = by.setdefault(d["symbol"], {"symbol": d["symbol"], "buy_value_cr": 0.0, "sell_value_cr": 0.0, "deals": 0, "clients": set(), "kinds": set()})
        s["buy_value_cr" if d["side"] == "BUY" else "sell_value_cr"] += d["value_cr"]
        s["deals"] += 1; s["clients"].add(d["client"]); s["kinds"].add(d["kind"])
    rows = []
    for s in by.values():
        net = s["buy_value_cr"] - s["sell_value_cr"]
        rows.append({"symbol": s["symbol"], "net_value_cr": round(net, 2), "buy_value_cr": round(s["buy_value_cr"], 2), "sell_value_cr": round(s["sell_value_cr"], 2),
                     "deals": s["deals"], "clients": sorted(s["clients"])[:6], "kinds": sorted(s["kinds"]),
                     "in_universe": (s["symbol"] in universe) if universe is not None else None,
                     "read": "net buying by disclosed large clients" if net > 0 else "net selling by disclosed large clients" if net < 0 else "two-way (buy = sell)"})
    rows.sort(key=lambda r: -abs(r["net_value_cr"]))
    return rows


def parse_bhavdata(text):
    df = pd.read_csv(io.StringIO(text), skipinitialspace=True)
    df.columns = [c.strip().upper() for c in df.columns]
    df["SERIES"] = df["SERIES"].astype(str).str.strip()
    df = df[df["SERIES"].isin(["EQ", "BE"])]
    out = {}
    for _, r in df.iterrows():
        try:
            dp = str(r.get("DELIV_PER", "")).strip()
            out[str(r["SYMBOL"]).strip()] = {"deliv_pct": float(dp) if dp not in ("", "-", "nan") else None, "qty": int(float(r["TTL_TRD_QNTY"])),
                                             "close": float(r["CLOSE_PRICE"]), "prev": float(r["PREV_CLOSE"])}
        except Exception:
            continue
    return out


def delivery(day):
    """day: date -> ({symbol: row}, status). Tries the given day only (the bhavcopy exists after ~18:00 IST)."""
    try:
        txt, url = _get(f"/products/content/sec_bhavdata_full_{day.strftime('%d%m%Y')}.csv")
        rows = parse_bhavdata(txt)
        return rows, {"status": "FETCHED", "rows": len(rows), "url": url, "date": str(day)}
    except Exception as e:  # noqa
        return {}, {"status": f"unavailable: {e}", "date": str(day)}


def update_history(hist, day, rows, keep=30, universe=None):
    """hist: {date: {sym: [deliv_pct, qty]}} (stored in site/data). Returns the trimmed dict."""
    if rows:
        hist[str(day)] = {s: [r["deliv_pct"], r["qty"]] for s, r in rows.items() if universe is None or s in universe}
    for d in sorted(hist)[:-keep]:
        hist.pop(d)
    return hist


def accumulation(hist, today_rows, frames=None, min_turnover_cr=5):
    days = sorted(hist)
    prev_days = days[:-1] if days and today_rows and days[-1] in hist else days
    out = []
    for sym, r in today_rows.items():
        if r["deliv_pct"] is None or r["prev"] <= 0:
            continue
        chg = (r["close"] / r["prev"] - 1) * 100
        past = [hist[d][sym] for d in prev_days[-20:] if sym in hist[d] and hist[d][sym][0] is not None]
        if len(past) >= 5:
            avg_q = sum(p[1] for p in past) / len(past); avg_d = sum(p[0] for p in past) / len(past); basis = f"{len(past)}-day delivery history"
        elif frames is not None and sym in frames and len(frames[sym]) > 21:
            v = frames[sym]["Volume"]; avg_q = float(v.iloc[-21:-1].mean()); avg_d = 50.0; basis = "volume from price history; delivery vs 50% default (short history)"
        else:
            continue
        if avg_q <= 0 or r["qty"] * r["close"] / 1e7 < min_turnover_cr:
            continue
        vr = r["qty"] / avg_q
        hi_deliv = r["deliv_pct"] >= max(avg_d + 10, 60 if len(past) < 5 else 0)
        if vr >= 2 and hi_deliv and abs(chg) >= 1:
            out.append({"symbol": sym, "signal": "ACCUMULATION" if chg > 0 else "DISTRIBUTION", "volume_x_avg": round(vr, 1), "delivery_pct": r["deliv_pct"],
                        "delivery_avg_pct": round(avg_d, 1), "close_chg_pct": round(chg, 2), "turnover_cr": round(r["qty"] * r["close"] / 1e7, 1), "basis": basis,
                        "read": ("heavy volume with high delivery on an up day: consistent with large investors buying to hold" if chg > 0 else
                                 "heavy volume with high delivery on a down day: consistent with large holders exiting")})
    out.sort(key=lambda x: -x["volume_x_avg"])
    return out


def run(day, universe, hist, frames=None):
    items, st = deals()
    # the full bhavcopy appears ~18:00 IST; the 16:20 IST run therefore usually gets the PREVIOUS session (labelled)
    rows, dst, used = {}, {}, day
    for back in range(0, 6):
        cand = day - dt.timedelta(days=back)
        if cand.weekday() >= 5:
            continue
        rows, dst = delivery(cand)
        if rows:
            used = cand; break
    hist = update_history(hist, used, rows, universe=set(universe) if universe else None)
    acc = accumulation(hist, {s: r for s, r in rows.items() if not universe or s in universe}, frames)
    return {"as_of": str(day), "delivery_date": str(used), "deals": items[:400], "deal_summary": summarise_deals(items, set(universe) if universe else None)[:80],
            "accumulation": acc[:60], "sources": {**st, "delivery": dst}, "history_days": len(hist),
            "method": "ACCUMULATION = volume >= 2x 20-day average + delivery % >= own average + 10 pts (or >= 60% when history is short) + close up >= 1%; DISTRIBUTION = same with close down >= 1%.",
            "note": "End-of-day proxies from NSE public files. Not real-time order flow. Bulk/block client names are public disclosures."}, hist
