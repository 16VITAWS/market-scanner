"""
Company Intelligence: owner / promoter / management / governance events for listed Indian companies.

Sources (every event keeps a link back to its source):
  1. BSE corporate announcements (official exchange filings, public JSON used by bseindia.com).
     Covers almost every NSE company too, because nearly all are dual-listed.
  2. Google News RSS, only for companies already flagged or held - marked MEDIA REPORT (unverified).

What it does: fetch -> classify (rules) -> severity (rules) -> per-company timeline, clusters, governance score
-> alerts -> coverage report. Nothing is invented: if a source fails, coverage says so and no event is made up.
The classification and scores are RULE-BASED heuristics, labelled as such; they are not legal findings.
"""
import re, time, datetime as dt, hashlib
from urllib.parse import quote
import requests

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
BSE_API = "https://api.bseindia.com/BseIndiaAPI/api/AnnSubCategoryGetData/w"
BSE_PDF = "https://www.bseindia.com/xml-data/corpfiling/AttachLive/"
HDR = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36",
       "Referer": "https://www.bseindia.com/", "Origin": "https://www.bseindia.com", "Accept": "application/json, text/plain, */*"}
KEEP_DAYS = 120
MAX_EVENTS = 3500

CATS = {
    "INSIDER_TRADE": "Insider / promoter share buying or selling",
    "PLEDGE": "Promoter pledge / encumbrance of shares",
    "OWNERSHIP_CHANGE": "Big stake change, open offer or takeover",
    "MANAGEMENT_CHANGE": "Top management or board change",
    "AUDITOR": "Auditor appointment or resignation",
    "COMPENSATION": "Pay, ESOP or remuneration",
    "LEGAL_REGULATORY": "Legal, regulatory, tax, fraud or default matter",
    "GOVERNANCE": "Other governance disclosure",
}

# (pattern on headline+subcategory, category, severity 0-100, plain reason). First match wins; order matters.
_R = r"(?:reg\.?|regulation)\s?"
RULES = [
    (r"\bfraud|forensic|misappropriat|siphon|embezzl", "LEGAL_REGULATORY", 90, "fraud / forensic audit mentioned"),
    (r"quarterly disclosures? by listed entities of defaults", "LEGAL_REGULATORY", 35, "quarterly default disclosure (may be NIL - open the PDF)"),
    (r"corporate insolvency|\bcirp\b|insolvency resolution|resolution professional|liquidat", "LEGAL_REGULATORY", 85, "insolvency proceedings"),
    (r"default(s)? (on|in) (payment|repayment)|non-payment of (interest|principal)|delay in (payment|servicing) of", "LEGAL_REGULATORY", 80, "loan / interest default disclosed"),
    (r"\bsearch\b.{0,40}(income tax|enforcement|gst)|\braid|enforcement directorate|\bcbi\b|\bsfio\b|serious fraud", "LEGAL_REGULATORY", 80, "search / investigation by an agency"),
    (r"resign\w*.{0,80}statutory auditor|statutory auditor.{0,60}resign|resignation of auditor", "AUDITOR", 85, "statutory auditor resigned"),
    (r"casual vacancy.{0,60}auditor|auditor.{0,80}casual vacancy", "AUDITOR", 50, "new auditor to fill a casual vacancy (previous auditor left)"),
    (r"(sebi|securities and exchange board).{0,80}(order|penalt|show cause|adjudicat|settlement)|show[- ]cause|adjudication order|interim order|ministry of corporate affairs|registrar of companies", "LEGAL_REGULATORY", 60, "SEBI / MCA / regulator action"),
    (r"penalt|fine imposed|demand order|tax demand|gst demand|income tax (demand|order)|litigation|legal proceeding|writ petition|arbitration award|court order|\bnclt\b", "LEGAL_REGULATORY", 45, "penalty, tax demand or litigation"),
    (r"resign\w*\b.{0,80}\b(ceo|cfo|managing director|chief executive|chief financial|whole[- ]time director|chairman)\b|\b(ceo|cfo|managing director|chief executive|chief financial)\b.{0,60}(resign|step(s|ped)? down)", "MANAGEMENT_CHANGE", 70, "MD / CEO / CFO / chairman resigned"),
    (r"resign\w*.{0,80}(company secretary|compliance officer)|(company secretary|compliance officer).{0,60}resign", "MANAGEMENT_CHANGE", 30, "company secretary resigned"),
    (r"resign\w*.{0,60}\bdirector|\bdirector.{0,60}resign", "MANAGEMENT_CHANGE", 45, "director resigned"),
    (r"cessation", "MANAGEMENT_CHANGE", 30, "cessation of a director / officer"),
    (r"auditor", "AUDITOR", 15, "auditor appointment / change"),
    (r"reclassification.{0,80}promoter", "OWNERSHIP_CHANGE", 35, "promoter reclassified to public"),
    (r"encumbrance|pledg|invocation|31\s?\(1\)\s?(and|&)\s?31\s?\(2\)", "PLEDGE", 55, "promoter shares pledged / encumbrance changed"),
    (r"open offer|" + _R + r"10\s?\(6\)|takeover bid|change in control|" + _R + r"29\s?\(1\)", "OWNERSHIP_CHANGE", 45, "big stake acquisition / open offer"),
    (_R + r"29\s?\(2\)|substantial acquisition", "OWNERSHIP_CHANGE", 30, "stake change of 2% or more"),
    (_R + r"7\s?\(2\)|prohibition of insider", "INSIDER_TRADE", 35, "insider / promoter trade disclosed"),
    (r"esop|esps|stock option|employee stock|restricted stock|remuneration|sitting fee|commission to director", "COMPENSATION", 15, "ESOP / remuneration"),
    (r"change in (senior )?management|change in directorate|appoint|senior management", "MANAGEMENT_CHANGE", 20, "management / board appointment"),
    (r"clarification sought|movement in price", "GOVERNANCE", 25, "exchange asked about an unusual price move"),
    (r"clarification|deviation|variation|related party", "GOVERNANCE", 20, "clarification / deviation / related-party"),
]
SKIP = re.compile(r"trading window|31 ?\(4\)|reg\.? ?74 ?\(5\)|newspaper publication|analyst|investor meet|earnings call|record date|"
                  r"intimation of board meeting|annual report|book closure|loss of share certificate|duplicate share", re.I)
RELEVANT_CAT = re.compile(r"insider|sast|company update|others|corp", re.I)


def now_ist():
    return dt.datetime.now(IST)


def norm_name(s):
    s = (s or "").lower().replace("&", " and ")
    s = re.sub(r"\b(limited|ltd|the|india|co|company|corporation|corp|inc|plc|pvt|private)\b", " ", s)
    return re.sub(r"[^a-z0-9]", "", s)


def classify(category, subcat, headline, newssub=""):
    """Return (cat, severity, reason) or None for routine filings."""
    text = " ".join(x for x in (subcat or "", headline or "", newssub or "") if x)
    if SKIP.search(text):
        return None
    t = text.lower()
    for pat, cat, sev, why in RULES:
        if re.search(pat, t):
            if cat in ("MANAGEMENT_CHANGE", "AUDITOR") and re.search(r"re-? ?appoint|retires by rotation", t) and sev <= 20:
                sev = 10
            return cat, sev, why
    if re.search(r"insider|sast", (category or "").lower()):
        return "INSIDER_TRADE", 30, "insider / SAST disclosure"
    return None


# ---------------------------------------------------------------- sources
def fetch_bse_day(day, session=None, max_pages=60, pause=0.35):
    """All BSE announcements for one IST date. Returns (rows, status)."""
    s = session or requests.Session()
    ds = day.strftime("%Y%m%d")
    rows, pages, total = [], 0, None
    for p in range(1, max_pages + 1):
        url = f"{BSE_API}?pageno={p}&strCat=-1&strPrevDate={ds}&strScrip=&strSearch=P&strToDate={ds}&strType=C&subcategory=-1"
        try:
            r = s.get(url, headers=HDR, timeout=25)
            r.raise_for_status()
            j = r.json()
        except Exception as e:
            return rows, f"failed on page {p}: {str(e)[:120]}"
        tab = j.get("Table") or []
        pages += 1
        if not tab:
            break
        rows += tab
        total = tab[0].get("TotalPageCnt") or total
        if total and p >= int(total):
            break
        time.sleep(pause)
    return rows, f"ok ({pages} pages, {len(rows)} filings)"


def bse_event(x, universe_idx):
    c = classify(x.get("CATEGORYNAME"), x.get("SUBCATNAME"), x.get("HEADLINE"), x.get("NEWSSUB"))
    if not c:
        return None
    cat, sev, why = c
    name = (x.get("SLONGNAME") or "").strip()
    url = x.get("NSURL") or ""
    slug = ""
    m = re.search(r"stock-share-price/[^/]+/([^/]+)/(\d+)", url)
    if m:
        slug = m.group(1).upper()
    sym, conf = map_symbol(name, slug, universe_idx)
    t = x.get("DissemDT") or x.get("NEWS_DT") or x.get("DT_TM") or ""
    att = (x.get("ATTACHMENTNAME") or "").strip()
    head = re.sub(r"\s+", " ", (x.get("HEADLINE") or x.get("NEWSSUB") or "")).strip()
    eid = "bse-" + str(x.get("NEWSID") or hashlib.sha1((name + t + head).encode()).hexdigest()[:16])
    return {"id": eid, "time": t[:19], "company": name, "symbol": sym, "symbol_match": conf, "bse_code": x.get("SCRIP_CD"),
            "category": cat, "severity": sev, "reason": why, "headline": head[:400],
            "filing_type": f"{(x.get('CATEGORYNAME') or '').strip()} / {(x.get('SUBCATNAME') or '').strip()}".strip(" /"),
            "source": "BSE filing", "verified": "OFFICIAL EXCHANGE FILING",
            "pdf": (BSE_PDF + att) if att else None, "link": url or None}


def map_symbol(name, slug, idx):
    """Map a BSE company to an NSE symbol. Returns (symbol or None, 'exact'|'name'|'none')."""
    by_sym, by_name = idx.get("sym", set()), idx.get("name", {})
    if slug and slug in by_sym:
        return slug, "exact"
    n = norm_name(name)
    if n and n in by_name:
        return by_name[n], "name"
    return None, "none"


def universe_index(rows):
    """rows: iterable of (symbol, company name)."""
    sym, name = set(), {}
    for s, n in rows:
        if not s:
            continue
        sym.add(s.upper())
        if n:
            name.setdefault(norm_name(n), s.upper())
    return {"sym": sym, "name": name}


def load_universe(url, session=None):
    import io, pandas as pd
    try:
        r = (session or requests).get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=20)
        r.raise_for_status()
        df = pd.read_csv(io.StringIO(r.text))
        return list(zip(df["Symbol"].str.strip(), df["Company Name"].str.strip())), "Nifty 500 list"
    except Exception as e:  # noqa
        return [], f"universe list failed: {str(e)[:80]}"


def media_news(companies, fetch_fn, limit_q=20):
    """Google News for flagged / held companies, owner & management keywords only. Marked unverified."""
    out, status = [], {}
    for name, sym in companies[:limit_q]:
        q = f'"{name}" (promoter OR CEO OR MD OR SEBI OR resigns OR pledge OR fraud OR raid OR stake)'
        items, st = fetch_fn(q, days=10, limit=5)
        status[sym or name] = st
        for it in items:
            c = classify("", "", it.get("title", ""))
            if not c:
                continue
            cat, sev, why = c
            out.append({"id": "news-" + hashlib.sha1(it["title"].encode()).hexdigest()[:14], "time": (it.get("published_utc") or "")[:19],
                        "company": name, "symbol": sym, "symbol_match": "search", "category": cat,
                        "severity": max(5, sev - 15), "reason": why + " (media report)", "headline": it["title"][:400],
                        "filing_type": "News report", "source": it.get("source") or "Google News",
                        "verified": "MEDIA REPORT - not verified against a filing", "pdf": None, "link": it.get("link")})
    return out, status


# ---------------------------------------------------------------- analysis
def _age_days(t, now):
    try:
        x = dt.datetime.fromisoformat(t[:19])
        if x.tzinfo is None:
            x = x.replace(tzinfo=IST)
        return max(0.0, (now - x).total_seconds() / 86400)
    except Exception:
        return 999.0


def clusters(evs, now):
    """Patterns inside one company's events (list newest first)."""
    out = []
    recent = [e for e in evs if _age_days(e["time"], now) <= 30]
    mg = [e for e in recent if e["category"] == "MANAGEMENT_CHANGE" and e["severity"] >= 30]
    if len(mg) >= 2:
        out.append({"type": "MANAGEMENT_EXITS", "text": f"{len(mg)} management / board exits in 30 days", "severity": min(90, 40 + 15 * len(mg))})
    aud = [e for e in recent if e["category"] == "AUDITOR" and e["severity"] >= 60]
    if aud and mg:
        out.append({"type": "AUDITOR_AND_MANAGEMENT", "text": "Auditor resignation together with management exits", "severity": 90})
    pl = [e for e in recent if e["category"] == "PLEDGE"]
    if len(pl) >= 2:
        out.append({"type": "REPEATED_PLEDGE", "text": f"{len(pl)} pledge / encumbrance filings in 30 days", "severity": 60})
    ins = [e for e in recent if e["category"] in ("INSIDER_TRADE", "OWNERSHIP_CHANGE")]
    if len(ins) >= 3:
        out.append({"type": "INSIDER_ACTIVITY", "text": f"{len(ins)} insider / stake-change filings in 30 days (direction is inside the PDFs)", "severity": 40})
    lg = [e for e in recent if e["category"] == "LEGAL_REGULATORY"]
    if len(lg) >= 2:
        out.append({"type": "LEGAL_PILEUP", "text": f"{len(lg)} legal / regulatory items in 30 days", "severity": 70})
    return out


def company_profiles(events, now):
    by = {}
    for e in events:
        k = e.get("symbol") or ("BSE:" + str(e.get("bse_code") or e["company"]))
        by.setdefault(k, []).append(e)
    profiles = []
    for k, evs in by.items():
        evs.sort(key=lambda e: e["time"], reverse=True)
        # score: severity decayed by age (half-life 30 days), official filings full weight, media half
        raw, parts, top1 = 0.0, {}, 0.0
        for e in evs:
            w = 0.5 ** (_age_days(e["time"], now) / 30) * (1.0 if e["source"] == "BSE filing" else 0.5)
            v = e["severity"] * w
            raw += v
            top1 = max(top1, v)
            parts[e["category"]] = round(parts.get(e["category"], 0) + v, 1)
        cl = clusters(evs, now)
        raw += sum(c["severity"] for c in cl) * 0.5
        score = round(min(100.0, max(top1 * 0.8, raw / 2)), 1)
        level = "HIGH" if score >= 50 else "WATCH" if score >= 20 else "LOW"
        counts = {}
        for e in evs:
            counts[e["category"]] = counts.get(e["category"], 0) + 1
        top = sorted(evs, key=lambda e: (-e["severity"], e["time"]))[:3]
        profiles.append({"key": k, "symbol": evs[0].get("symbol"), "company": evs[0]["company"], "bse_code": evs[0].get("bse_code"),
                         "score": score, "level": level, "counts": counts, "score_parts": parts, "clusters": cl,
                         "last_event": evs[0]["time"], "events": len(evs),
                         "reasons": [f"{e['reason']} ({e['time'][:10]})" for e in top]})
    profiles.sort(key=lambda p: (-p["score"], p["company"]))
    return profiles


def build(prev, new_events, now, watch=(), coverage=None):
    """Merge new events into the stored history and rebuild profiles / alerts."""
    keep = {e["id"]: e for e in (prev or {}).get("events", []) if _age_days(e.get("time", ""), now) <= KEEP_DAYS}
    fresh = [e for e in new_events if e["id"] not in keep]
    for e in new_events:
        keep[e["id"]] = e
    # the same filing is often posted under two BSE sub-categories: keep one (the most severe)
    best = {}
    for e in keep.values():
        k = (e.get("company"), (e.get("time") or "")[:10], re.sub(r"[^a-z0-9]", "", (e.get("headline") or "").lower())[:80])
        if k not in best or e["severity"] > best[k]["severity"]:
            best[k] = e
    fresh_ids = {e["id"] for e in best.values()}
    fresh = [e for e in fresh if e["id"] in fresh_ids]
    events = sorted(best.values(), key=lambda e: e["time"], reverse=True)[:MAX_EVENTS]
    profiles = company_profiles(events, now)
    watch = {w.upper() for w in watch if w}
    alerts = []
    for e in events:
        if _age_days(e["time"], now) > 3:
            continue
        mine = bool(e.get("symbol") and e["symbol"] in watch)
        if e["severity"] >= 65 or (mine and e["severity"] >= 30):
            alerts.append({**e, "yours": mine})
    stats = {c: sum(1 for e in events if e["category"] == c) for c in CATS}
    first = events[-1]["time"][:10] if events else None
    return {"as_of": now.isoformat(timespec="seconds"), "categories": CATS, "events": events, "profiles": profiles[:600],
            "alerts": alerts[:100], "new_this_run": len(fresh), "stats": stats, "history_from": first,
            "watch": sorted(watch), "coverage": coverage or {}, "method": METHOD, "limits": LIMITS}


METHOD = ("Rule-based: each filing's type and headline are matched against fixed patterns to give a category and a severity "
          "(0-100). A company's score adds its event severities, halving every 30 days, media reports count half, plus "
          "patterns (several exits in 30 days, auditor + management exits, repeated pledges, legal pile-up); total / 2, "
          "or 80% of the single most serious event if that is higher, capped at 100. "
          "HIGH >= 50, WATCH >= 20. This is a reading aid, not a legal or investment judgement.")
LIMITS = [
    "Only BSE filings are read automatically. NSE-only filings and company websites are not read (NSE blocks automated cloud access).",
    "PDF attachments are linked but not read - the number of shares, buy/sell direction, person and price inside an insider-trade PDF are NOT extracted.",
    "Salary / compensation figures come only from annual reports, which are not parsed. Only ESOP allotments and remuneration filings are flagged.",
    "Court cases, police or agency investigations that the company has not disclosed to the exchange are not known.",
    "News items are media reports matched by keywords and can be wrong or about a different company with a similar name.",
    "Company-to-symbol matching uses the BSE short code and the Nifty 500 name list; companies outside Nifty 500 show their BSE name only.",
    "Scores are rule-based heuristics, not fraud detection. A HIGH score means 'read the filings', not 'this company is bad'.",
    "History starts from the first day this feature ran; it keeps the last 120 days.",
]


def run(prev, now=None, universe=None, watch=(), fetch_news=None, session=None, days=None, flagged_news=True):
    """One refresh. prev = previously stored JSON (or {}). Returns new JSON."""
    now = now or now_ist()
    s = session or requests.Session()
    uni_rows, uni_status = universe if universe is not None else ([], "not loaded")
    idx = universe_index(uni_rows)
    if days is None:
        days = 2 if (prev or {}).get("events") else 10
    cov = {"bse": {"name": "BSE corporate announcements (official)", "days": {}, "status": "ok"},
           "universe": uni_status, "news": {"name": "Google News RSS (media, unverified)", "status": "not run"}}
    new = []
    for i in range(days):
        d = (now - dt.timedelta(days=i)).date()
        if d.weekday() >= 5 and i > 0:
            cov["bse"]["days"][d.isoformat()] = "weekend - skipped"
            continue
        rows, st = fetch_bse_day(d, s)
        cov["bse"]["days"][d.isoformat()] = st
        if st.startswith("failed") and not rows:
            cov["bse"]["status"] = "PARTIAL / FAILED - see days"
        for x in rows:
            e = bse_event(x, idx)
            if e:
                new.append(e)
    if not any(v.startswith("ok") for v in cov["bse"]["days"].values()):
        cov["bse"]["status"] = "FAILED - no BSE data this run; older history kept"
    cov["bse"]["last_success"] = now.isoformat(timespec="seconds") if cov["bse"]["status"] == "ok" else (prev or {}).get("coverage", {}).get("bse", {}).get("last_success")
    if fetch_news and flagged_news:
        tmp = build(prev, new, now, watch)
        pick = [(p["company"], p["symbol"]) for p in tmp["profiles"] if p["level"] != "LOW" and p["symbol"]][:12]
        pick += [(w, w) for w in sorted({w.upper() for w in watch}) if w not in {x[1] for x in pick}][:8]
        news, nst = media_news(pick, fetch_fn=fetch_news)
        new += news
        cov["news"] = {"name": "Google News RSS (media, unverified)", "status": f"ok ({len(news)} relevant items, {len(nst)} searches)", "searches": nst}
    cov["mapped_to_nse_symbol"] = f"{sum(1 for e in new if e.get('symbol'))} of {len(new)} new events"
    return build(prev, new, now, watch, cov)
