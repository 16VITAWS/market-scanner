"""
Mutual fund scanner (India).

Sources (free):
  - AMFI NAVAll.txt  (official daily NAV of every scheme; portal.amfiindia.com/spages/NAVAll.txt)
  - mfapi.in         (unofficial mirror of AMFI NAV HISTORY per scheme code; used only for past NAVs)
Universe: DIRECT plan, GROWTH option, open-ended schemes in equity / index / hybrid / selected debt categories.

Per scheme (from daily NAV history): 1y return, 3y & 5y CAGR, 3y annualised volatility, 3y max drawdown,
3y Sharpe (vs 6.5% risk-free), share of rolling 1-year windows that were positive.
Score inside its OWN category = average percentile of 3y CAGR, 3y Sharpe and (smaller) 3y drawdown;
only schemes with >= 3 years of history are ranked.

Allocation ideas are RULES, shown with their reasons:
  - per category: top-scored schemes (past risk-adjusted performance; past performance does not guarantee future results)
  - sector tilt: sectors whose NSE stocks show the strongest 3-month momentum in our scan -> matching sectoral/thematic funds
  - regime: the market regime model suggests how much to lean equity vs hybrid/debt
Not investment advice. Expense ratio (TER), AUM and portfolio holdings are NOT in the AMFI NAV file and are not shown.
"""
import io, math, datetime as dt
from concurrent.futures import ThreadPoolExecutor
import numpy as np
import pandas as pd
import requests

UA = {"User-Agent": "Mozilla/5.0 (personal-scanner/2.3)"}
NAV_URLS = ("https://portal.amfiindia.com/spages/NAVAll.txt", "https://www.amfiindia.com/spages/NAVAll.txt")
RF = 0.065
KEEP = ("Large Cap", "Mid Cap", "Small Cap", "Flexi Cap", "Multi Cap", "Large & Mid Cap", "ELSS", "Focused", "Value", "Contra",
        "Dividend Yield", "Sectoral", "Thematic", "Index Fund", "Aggressive Hybrid", "Balanced Advantage", "Dynamic Asset Allocation",
        "Multi Asset", "Arbitrage", "Liquid", "Gilt", "Short Duration", "Corporate Bond")
SECTOR_WORDS = {
    "Financial Services": ("bank", "financial", "finserv"), "Healthcare": ("pharma", "health"), "Information Technology": ("technology", "digital", " it ", "tech"),
    "Fast Moving Consumer Goods": ("fmcg", "consumption", "consumer"), "Automobile and Auto Components": ("auto", "mobility", "transport"),
    "Oil Gas & Consumable Fuels": ("energy", "oil"), "Power": ("power", "energy"), "Capital Goods": ("infra", "manufactur", "capital goods"),
    "Construction": ("infra",), "Metals & Mining": ("metal", "commodit", "natural resources"), "Realty": ("realty", "housing"),
    "Chemicals": ("chemical",), "Services": ("services",), "Consumer Durables": ("consumption", "consumer"),
}


def parse_navall(text):
    rows, cat, amc, head = [], None, None, ""
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if ";" not in line:
            if "Schemes(" in line or "Schemes (" in line:
                cat = line[line.find("(") + 1:line.rfind(")")] if "(" in line else line
                head = line
            else:
                amc = line
            continue
        f = [x.strip() for x in line.split(";")]
        if not f[0].isdigit():
            continue                                         # header
        if len(f) >= 8:
            code, name, plan, option, nav, date = f[0], f[3], f[4], f[5], f[6], f[7]
        elif len(f) >= 6:
            code, name, nav, date = f[0], f[3], f[4], f[5]
            plan = "Direct Plan" if "direct" in name.lower() else "Regular Plan"
            option = "Growth Option" if "growth" in name.lower() else "IDCW/other"
        else:
            continue
        try:
            navf = float(nav)
        except Exception:
            continue
        rows.append({"code": int(code), "name": name, "plan": plan, "option": option, "nav": navf, "date": date, "category": cat or "", "open_ended": head.lower().startswith("open ended"), "amc": amc or ""})
    return rows


def nav_all():
    last = None
    for u in NAV_URLS:
        try:
            r = requests.get(u, headers=UA, timeout=40)
            if r.ok and "Scheme Code" in r.text[:500]:
                return parse_navall(r.text), {"status": "FETCHED", "url": u}
            last = f"HTTP {r.status_code}"
        except Exception as e:  # noqa
            last = str(e)
    return [], {"status": f"unavailable: {last}"}


def short_cat(c):
    c = c.replace("Equity Scheme - ", "").replace("Hybrid Scheme - ", "").replace("Debt Scheme - ", "").replace("Other Scheme - ", "")
    return c.strip()


def select(rows):
    out = []
    for r in rows:
        if "direct" not in r["plan"].lower() or "growth" not in r["option"].lower():
            continue
        if not r.get("open_ended", True):
            continue
        sc = short_cat(r["category"])
        if not any(k.lower() in sc.lower() for k in KEEP):
            continue
        if "fund of fund" in sc.lower() or ("etf" in sc.lower() and "index" not in sc.lower()):
            continue
        out.append({**r, "cat": sc})
    return out


def history(code):
    try:
        r = requests.get(f"https://api.mfapi.in/mf/{code}", headers=UA, timeout=25)
        j = r.json()
        d = pd.DataFrame(j.get("data", []))
        if d.empty:
            return None
        s = pd.Series(pd.to_numeric(d["nav"], errors="coerce").values, index=pd.to_datetime(d["date"], format="%d-%m-%Y", errors="coerce"))
        s = s[s.index.notna()].dropna()
        s = s[s > 0].sort_index()
        return s[~s.index.duplicated(keep="last")]
    except Exception:
        return None


def metrics(s, asof=None):
    if s is None or len(s) < 30:
        return None
    end = s.index[-1] if asof is None else min(s.index[-1], pd.Timestamp(asof))
    s = s[s.index <= end]

    def at(years):
        t = end - pd.DateOffset(years=years)
        p = s[s.index <= t]
        return float(p.iloc[-1]) if len(p) and (t - p.index[-1]).days <= 10 else None
    last = float(s.iloc[-1])
    out = {"nav": round(last, 4), "nav_date": str(end.date()), "years_of_history": round((end - s.index[0]).days / 365.25, 1)}
    for y, k in ((1, "ret_1y"), (3, "cagr_3y"), (5, "cagr_5y")):
        p = at(y)
        out[k] = round(((last / p) ** (1 / y) - 1) * 100, 2) if p else None
    w = s[s.index >= end - pd.DateOffset(years=3)]
    if len(w) > 200 and out["cagr_3y"] is not None:
        lr = np.log(w).diff().dropna()
        vol = float(lr.std() * math.sqrt(252))
        dd = float((w / w.cummax() - 1).min())
        out["vol_3y"] = round(vol * 100, 2)
        out["max_dd_3y"] = round(dd * 100, 2)
        out["sharpe_3y"] = round((out["cagr_3y"] / 100 - RF) / vol, 2) if vol > 0 else None
        wk = w.resample("W-FRI").last().dropna()
        roll = (wk / wk.shift(52) - 1).dropna()
        out["positive_1y_windows_pct"] = round(float((roll > 0).mean() * 100), 1) if len(roll) else None
    return out


def score(funds):
    df = pd.DataFrame(funds)
    if df.empty:
        return funds
    for c in ("cagr_3y", "sharpe_3y", "max_dd_3y"):
        if c not in df:
            df[c] = np.nan
    ok = df["cagr_3y"].notna() & df["sharpe_3y"].notna() & df["max_dd_3y"].notna()
    df["score"] = np.nan; df["rank_in_category"] = np.nan; df["peers"] = np.nan
    for cat, g in df[ok].groupby("cat"):
        pr = (g["cagr_3y"].rank(pct=True) + g["sharpe_3y"].rank(pct=True) + g["max_dd_3y"].rank(pct=True)) / 3   # max_dd is negative: higher = smaller drawdown
        df.loc[g.index, "score"] = (pr * 100).round(0)
        df.loc[g.index, "rank_in_category"] = pr.rank(ascending=False, method="min")
        df.loc[g.index, "peers"] = len(g)
    return [{k: (None if (isinstance(v, float) and math.isnan(v)) else v) for k, v in r.items()} for r in df.to_dict("records")]


def ideas(funds, sector_strength=None, regime=None, per_cat=3):
    cats = {}
    for f in funds:
        if f.get("score") is not None:
            cats.setdefault(f["cat"], []).append(f)
    top = {c: [{"code": f["code"], "name": f["name"], "score": f["score"], "cagr_3y": f.get("cagr_3y"), "sharpe_3y": f.get("sharpe_3y"),
                "max_dd_3y": f.get("max_dd_3y"), "why": f"rank {int(f['rank_in_category'])}/{int(f['peers'])} in {c} on 3y return, 3y Sharpe and 3y drawdown"}
               for f in sorted(v, key=lambda x: -x["score"])[:per_cat]] for c, v in sorted(cats.items())}
    tilt = []
    if sector_strength:
        strong = sorted(sector_strength.items(), key=lambda kv: -kv[1])[:3]
        weak = [kv for kv in sorted(sector_strength.items(), key=lambda kv: kv[1])[:2] if kv not in strong] if len(sector_strength) >= 6 else []
        themed = [f for f in funds if any(k in f["cat"].lower() for k in ("sectoral", "thematic")) and f.get("score") is not None]
        for sec, mom in strong:
            words = SECTOR_WORDS.get(sec, (sec.lower().split()[0],))
            match = sorted([f for f in themed if any(w in (" " + f["name"].lower() + " ") for w in words)], key=lambda x: -x["score"])[:3]
            tilt.append({"sector": sec, "momentum_3m_pct": round(mom, 1), "direction": "STRONG", "funds": [{"code": f["code"], "name": f["name"], "score": f["score"], "cagr_3y": f.get("cagr_3y")} for f in match],
                         "why": f"{sec} stocks in the NSE scan have the strongest average 3-month momentum ({mom:+.1f}%)"})
        for sec, mom in weak:
            tilt.append({"sector": sec, "momentum_3m_pct": round(mom, 1), "direction": "WEAK", "funds": [],
                         "why": f"{sec} stocks are among the weakest over 3 months ({mom:+.1f}%): review sector funds you hold in this theme"})
    mix = None
    if regime:
        mix = {"BULL": {"equity": "higher end of your plan", "hybrid": "normal", "debt": "lower end", "why": "regime model: uptrend state"},
               "SIDEWAYS": {"equity": "as per plan (SIP continues)", "hybrid": "balanced advantage / multi-asset useful", "debt": "normal", "why": "regime model: range-bound state"},
               "BEAR": {"equity": "SIP continues, avoid large lump sums", "hybrid": "balanced advantage preferred", "debt": "higher end / liquid for dry powder", "why": "regime model: downtrend state"}}.get(regime)
    return {"top_by_category": top, "sector_tilt": tilt, "regime_mix": mix,
            "rules": ["Rank only inside the same category (never compare a small-cap fund with a liquid fund).", "Need >= 3 years of NAV history to be ranked.",
                      "Check the expense ratio and portfolio on the AMC / AMFI site before investing (not in this data).",
                      "Sector/thematic funds are concentrated: size them small.", "Past performance does not guarantee future results. Not investment advice."]}


def run(sector_strength=None, regime=None, max_schemes=900, workers=8):
    rows, st = nav_all()
    sel = select(rows)
    if not sel:
        return {"available": False, "reason": "AMFI NAV file unavailable or empty", "sources": {"amfi": st}}
    # cap per category so one huge category (index funds) cannot starve the others
    by = {}
    for r in sel:
        by.setdefault(r["cat"], []).append(r)
    cap = max(20, max_schemes // max(len(by), 1))
    pick = [r for v in by.values() for r in v[:cap]]
    with ThreadPoolExecutor(workers) as ex:
        hists = list(ex.map(lambda r: history(r["code"]), pick))
    funds, missing = [], 0
    for r, h in zip(pick, hists):
        m = metrics(h)
        if m is None:
            missing += 1
            m = {"nav": r["nav"], "nav_date": r["date"]}
        funds.append({"code": r["code"], "name": r["name"], "amc": r["amc"], "cat": r["cat"], "amfi_nav": r["nav"], "amfi_date": r["date"], **m})
    funds = score(funds)
    return {"available": True, "as_of": max((f.get("nav_date") or "") for f in funds), "schemes_in_amfi_file": len(rows), "direct_growth_selected": len(sel),
            "scanned": len(pick), "history_missing": missing, "categories": sorted(by), "funds": funds, "ideas": ideas(funds, sector_strength, regime),
            "sources": {"amfi": st, "history": "mfapi.in (unofficial AMFI mirror)"}, "method": __doc__.strip(), "status": "DELAYED (daily NAV)"}
