"""
News ingestion: Google News RSS (free, public; headlines + links only, originals preserved).
Dedupe by normalised title. Sentiment is a KEYWORD HEURISTIC and is labelled as such - it is
not a trained model and carries low confidence.
"""
import re, hashlib, datetime as dt
import xml.etree.ElementTree as ET
from urllib.parse import quote
import requests
import pandas as pd
from . import config as C

UA = {"User-Agent": "Mozilla/5.0 (compatible; personal-scanner/2.0)"}
POS = ["record high", "surge", "jump", "rally", "beats", "upgrade", "profit rises", "strong", "buyback", "order win", "inflow", "ceasefire", "rate cut", "eases"]
NEG = ["fall", "slump", "plunge", "crash", "downgrade", "loss", "probe", "fraud", "raid", "penalty", "default", "war", "escalat", "outflow", "rate hike", "tariff", "ban", "resign"]
EVENT = ["results", "earnings", "q1", "q2", "q3", "q4", "rbi", "fed", "fomc", "budget", "cpi", "inflation", "gdp", "ipo", "listing", "dividend", "split", "bonus"]


def fetch(query, days=3, limit=8):
    url = "https://news.google.com/rss/search?q=" + quote(query) + "&hl=en-IN&gl=IN&ceid=IN:en"
    try:
        r = requests.get(url, headers=UA, timeout=15)
        root = ET.fromstring(r.content)
    except Exception as e:
        return [], f"fetch failed: {e}"
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)
    items = []
    for it in root.iter("item"):
        title = it.findtext("title") or ""
        try:
            when = pd.to_datetime(it.findtext("pubDate"), utc=True)
        except Exception:
            continue
        if when < cutoff:
            continue
        src = it.find("source")
        items.append({"title": title, "link": it.findtext("link") or "", "published_utc": when.isoformat(),
                      "source": (src.text if src is not None else "") or title.rsplit(" - ", 1)[-1], "query": query})
        if len(items) >= limit:
            break
    return items, "ok"


def classify(title):
    t = title.lower()
    tags, score = [], 0
    if any(w in t for w in POS): tags.append("POSITIVE"); score += 1
    if any(w in t for w in NEG): tags.append("NEGATIVE"); score -= 1
    if any(w in t for w in EVENT): tags.append("EVENT")
    risk = any(w in t for w in C.NEWS_RISK_WORDS)
    return {"tags": tags or ["NEUTRAL"], "sentiment": "POSITIVE" if score > 0 else "NEGATIVE" if score < 0 else "NEUTRAL",
            "news_risk": risk, "method": "keyword heuristic", "confidence": "low"}


def dedupe(items):
    seen, out = set(), []
    for it in items:
        key = hashlib.sha1(re.sub(r"[^a-z0-9]", "", it["title"].lower().rsplit(" - ", 1)[0])[:80].encode()).hexdigest()
        if key in seen:
            continue
        seen.add(key); it["id"] = key[:12]; it.update(classify(it["title"])); out.append(it)
    return out


def market_news(queries=("NSE Nifty market", "Sensex today", "RBI", "FII DII", "crude oil price", "US Fed rates", "rupee dollar")):
    items, status = [], {}
    for qn in queries:
        got, st = fetch(qn, days=2, limit=6)
        items += got; status[qn] = st
    items = dedupe(items)
    pos = sum(1 for i in items if i["sentiment"] == "POSITIVE"); neg = sum(1 for i in items if i["sentiment"] == "NEGATIVE")
    return {"items": sorted(items, key=lambda x: x["published_utc"], reverse=True), "fetch_status": status,
            "headline_stat": {"positive": pos, "negative": neg, "neutral": len(items) - pos - neg,
                              "positive_share_pct": round(pos / (pos + neg) * 100, 1) if pos + neg else None,
                              "note": "Share of positive among positive+negative keyword-tagged headlines. Descriptive only, not predictive."}}


def symbol_news(symbol, days=3):
    got, st = fetch(f"{symbol} NSE share", days=days, limit=4)
    got = dedupe(got)
    return got, any(i["news_risk"] for i in got), st
