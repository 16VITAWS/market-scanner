"""
Long-term value scanner (India Nifty 500 + US large caps).

For every stock, from the last (up to) four ANNUAL statements (Yahoo Finance fundamentals, free/unofficial; delayed):
  value     FCF yield (FCF / EV), earnings yield (EBIT / EV), book-to-price
  quality   ROIC (EBIT x (1 - tax) / invested capital), Piotroski F-score (9 checks), accrual ratio (Sloan),
            cash conversion (operating cash flow / net income), operating-margin stability
  growth    revenue CAGR
  safety    net debt / EBIT
  DCF       5-year two-stage discounted free cash flow -> intrinsic value per share in three scenarios (bear / base / bull)
            -> margin of safety and an implied 5-year annual return RANGE (price assumed to move to intrinsic value over 5 years,
            plus intrinsic growth and dividends)

Scores are percentiles inside the same market (India vs US), so a stock is only compared with its own market.
Banks / insurers / NBFCs: ROIC and DCF on free cash flow do not apply -> ranked on P/B, ROE, earnings yield and F-score only.

Rating rules (all must hold):
  BUY    composite >= 75th percentile, base margin of safety >= 20%, F-score >= 6, ROIC >= 12% (or ROE >= 14% for financials),
         free cash flow positive in >= 3 of the last 4 years (non-financials), bear-case return > 0
  WATCH  composite >= 60th percentile and margin of safety >= 0
  HOLD/AVOID otherwise
"conviction" (0-1) = data completeness x quality x stability x valuation cushion. It is a model score, NOT a probability of profit:
fundamentals with point-in-time history are not free, so this scanner cannot be honestly backtested yet (said on the portal).
Position size suggestion: 5% max per stock x conviction x (1 - scenario spread), sector cap 25%, thesis stop = price 25% below entry
or F-score <= 4 or rating falls to AVOID. Not investment advice.
"""
import math, datetime as dt
import numpy as np
import pandas as pd

FIN_SECTORS = ("financial", "bank", "insurance")
PARAMS = {"IN": {"discount": 0.12, "terminal_g": 0.05, "g_cap": 0.18, "rf": 0.068},
          "US": {"discount": 0.09, "terminal_g": 0.025, "g_cap": 0.12, "rf": 0.042}}


def _row(df, *names):
    """First matching statement row as a list of floats (newest first). Missing -> []."""
    if df is None or getattr(df, "empty", True):
        return []
    idx = {str(i).strip().lower(): i for i in df.index}
    for n in names:
        k = idx.get(n.lower())
        if k is not None:
            vals = pd.to_numeric(df.loc[k], errors="coerce")
            if isinstance(vals, pd.DataFrame):
                vals = vals.iloc[0]
            return [None if (v is None or (isinstance(v, float) and math.isnan(v))) else float(v) for v in vals.values]
    return []


def _v(lst, i=0):
    return lst[i] if len(lst) > i and lst[i] is not None else None


def _avg(*xs):
    xs = [x for x in xs if x is not None]
    return sum(xs) / len(xs) if xs else None


def piotroski(ni, ocf, ta, ltd, ca, cl, shares, gp, rev):
    """9 checks, year 0 vs year 1 (newest first lists). Returns (score, checks, n_available)."""
    c = {}
    roa0 = ni[0] / ta[0] if _v(ni) is not None and _v(ta) else None
    roa1 = ni[1] / ta[1] if _v(ni, 1) is not None and _v(ta, 1) else None
    c["positive ROA"] = roa0 > 0 if roa0 is not None else None
    c["positive operating cash flow"] = ocf[0] > 0 if _v(ocf) is not None else None
    c["ROA improved"] = roa0 > roa1 if roa0 is not None and roa1 is not None else None
    c["cash flow > net income"] = ocf[0] > ni[0] if _v(ocf) is not None and _v(ni) is not None else None
    lev0 = ltd[0] / ta[0] if _v(ltd) is not None and _v(ta) else None
    lev1 = ltd[1] / ta[1] if _v(ltd, 1) is not None and _v(ta, 1) else None
    c["leverage fell"] = lev0 <= lev1 if lev0 is not None and lev1 is not None else None
    cr0 = ca[0] / cl[0] if _v(ca) is not None and _v(cl) else None
    cr1 = ca[1] / cl[1] if _v(ca, 1) is not None and _v(cl, 1) else None
    c["current ratio improved"] = cr0 > cr1 if cr0 is not None and cr1 is not None else None
    c["no new shares"] = shares[0] <= shares[1] * 1.005 if _v(shares) and _v(shares, 1) else None
    gm0 = gp[0] / rev[0] if _v(gp) is not None and _v(rev) else None
    gm1 = gp[1] / rev[1] if _v(gp, 1) is not None and _v(rev, 1) else None
    c["gross margin improved"] = gm0 > gm1 if gm0 is not None and gm1 is not None else None
    at0 = rev[0] / ta[0] if _v(rev) and _v(ta) else None
    at1 = rev[1] / ta[1] if _v(rev, 1) and _v(ta, 1) else None
    c["asset turnover improved"] = at0 > at1 if at0 is not None and at1 is not None else None
    avail = [v for v in c.values() if v is not None]
    return (sum(1 for v in avail if v) if len(avail) >= 6 else None), c, len(avail)


def dcf(fcf0, shares, net_debt, g, r, tg, years=5):
    """Two-stage FCF DCF -> equity value per share. g fades linearly to tg over `years`."""
    if not fcf0 or fcf0 <= 0 or not shares or r <= tg:
        return None
    pv, f = 0.0, fcf0
    for t in range(1, years + 1):
        gt = g + (tg - g) * (t - 1) / max(years - 1, 1)
        f *= (1 + gt)
        pv += f / (1 + r) ** t
    tv = f * (1 + tg) / (r - tg) / (1 + r) ** years
    eq = pv + tv - (net_debt or 0)
    return eq / shares if eq > 0 else None


def compute(info, fin, bs, cf, market="IN"):
    """Pure function: fundamentals -> metrics dict (None where data is missing)."""
    P = PARAMS[market]
    price = info.get("currentPrice") or info.get("regularMarketPrice") or info.get("previousClose")
    sector = str(info.get("sector") or "")
    is_fin = any(k in sector.lower() for k in FIN_SECTORS) or any(k in str(info.get("industry") or "").lower() for k in FIN_SECTORS)
    rev = _row(fin, "Total Revenue", "Operating Revenue")
    ebit = _row(fin, "EBIT", "Operating Income")
    ni = _row(fin, "Net Income", "Net Income Common Stockholders")
    gp = _row(fin, "Gross Profit")
    pretax = _row(fin, "Pretax Income")
    tax = _row(fin, "Tax Provision")
    ta = _row(bs, "Total Assets")
    eq = _row(bs, "Stockholders Equity", "Common Stock Equity", "Total Equity Gross Minority Interest")
    debt = _row(bs, "Total Debt")
    ltd = _row(bs, "Long Term Debt", "Long Term Debt And Capital Lease Obligation")
    cash = _row(bs, "Cash And Cash Equivalents", "Cash Cash Equivalents And Short Term Investments")
    ca = _row(bs, "Current Assets")
    cl = _row(bs, "Current Liabilities")
    shares_bs = _row(bs, "Ordinary Shares Number", "Share Issued")
    ocf = _row(cf, "Operating Cash Flow", "Cash Flow From Continuing Operating Activities")
    capex = _row(cf, "Capital Expenditure")
    fcf = _row(cf, "Free Cash Flow")
    if not fcf and ocf:
        fcf = [(o + c) if o is not None and c is not None else None for o, c in zip(ocf, capex + [None] * len(ocf))]
    shares = info.get("sharesOutstanding") or _v(shares_bs)
    mcap = info.get("marketCap") or (price * shares if price and shares else None)
    nd = (_v(debt) or 0) - (_v(cash) or 0)
    ev = info.get("enterpriseValue") or (mcap + nd if mcap else None)
    m = {"price": price, "currency": info.get("currency"), "sector": sector or None, "industry": info.get("industry"), "financial": is_fin,
         "market_cap": mcap, "years": len([x for x in rev if x is not None])}
    trate = (tax[0] / pretax[0]) if _v(tax) is not None and _v(pretax) else 0.25
    trate = min(max(trate, 0.0), 0.35)
    ic0 = (_v(eq) or 0) + (_v(debt) or 0) - (_v(cash) or 0) if _v(eq) is not None else None
    ic1 = (_v(eq, 1) or 0) + (_v(debt, 1) or 0) - (_v(cash, 1) or 0) if _v(eq, 1) is not None else None
    ic = _avg(ic0, ic1)
    m["roic"] = (ebit[0] * (1 - trate) / ic) if _v(ebit) is not None and ic and ic > 0 else None
    m["roe"] = (ni[0] / _avg(_v(eq), _v(eq, 1))) if _v(ni) is not None and _avg(_v(eq), _v(eq, 1)) else None
    fcf_n = _avg(*fcf[:3]) if fcf else None                     # normalised FCF: average of up to 3 years
    m["fcf_yield"] = fcf_n / ev if fcf_n is not None and ev and ev > 0 else None
    m["earnings_yield"] = ebit[0] / ev if _v(ebit) is not None and ev and ev > 0 else None
    m["pe"] = info.get("trailingPE") or (mcap / ni[0] if mcap and _v(ni) and ni[0] > 0 else None)
    m["pb"] = info.get("priceToBook") or (mcap / eq[0] if mcap and _v(eq) and eq[0] > 0 else None)
    m["book_to_price"] = 1 / m["pb"] if m["pb"] and m["pb"] > 0 else None
    m["div_yield"] = (info.get("dividendYield") or 0) / (100 if (info.get("dividendYield") or 0) > 1 else 1)
    n_rev = len([x for x in rev if x])
    if n_rev >= 2 and rev[0] and rev[n_rev - 1] and rev[n_rev - 1] > 0 and rev[0] > 0:
        m["rev_cagr"] = (rev[0] / rev[n_rev - 1]) ** (1 / (n_rev - 1)) - 1
    else:
        m["rev_cagr"] = None
    margins = [e / r for e, r in zip(ebit, rev) if e is not None and r]
    m["op_margin"] = margins[0] if margins else None
    m["margin_stability"] = float(np.std(margins)) if len(margins) >= 3 else None
    m["accruals"] = (ni[0] - ocf[0]) / _avg(_v(ta), _v(ta, 1)) if _v(ni) is not None and _v(ocf) is not None and _avg(_v(ta), _v(ta, 1)) else None
    m["cash_conversion"] = ocf[0] / ni[0] if _v(ocf) is not None and _v(ni) and ni[0] > 0 else None
    m["net_debt_to_ebit"] = nd / ebit[0] if _v(ebit) and ebit[0] > 0 else None
    m["fcf_positive_years"] = sum(1 for x in fcf[:4] if x is not None and x > 0)
    m["fcf_years"] = len([x for x in fcf[:4] if x is not None])
    m["fscore"], m["fscore_checks"], _ = piotroski(ni, ocf, ta, ltd or debt, ca, cl, shares_bs, gp, rev)
    # DCF scenarios (non-financials with positive normalised FCF)
    if not is_fin and fcf_n and fcf_n > 0 and shares and price:
        g_base = min(max(m["rev_cagr"] if m["rev_cagr"] is not None else 0.05, 0.0), P["g_cap"])
        sc = {"bear": (g_base * 0.5, P["discount"] + 0.01, P["terminal_g"] - 0.01),
              "base": (g_base, P["discount"], P["terminal_g"]),
              "bull": (min(g_base * 1.3 + 0.01, P["g_cap"] + 0.03), P["discount"] - 0.01, P["terminal_g"] + 0.005)}
        iv = {k: dcf(fcf_n, shares, nd, *v) for k, v in sc.items()}
        m["dcf"] = {"fcf_normalised": fcf_n, "growth_base": round(g_base, 4), "growth_bear": round(sc["bear"][0], 4), "growth_bull": round(sc["bull"][0], 4),
                    "discount_rate": P["discount"], "terminal_growth": P["terminal_g"], "net_debt": nd, "shares": shares,
                    "iv_bear": iv["bear"], "iv_base": iv["base"], "iv_bull": iv["bull"]}
        if iv["base"]:
            m["margin_of_safety"] = iv["base"] / price - 1

            def ann(ivx, g):
                if not ivx:
                    return None
                tgt = ivx * (1 + min(g, 0.10)) ** 5
                return (tgt / price) ** (1 / 5) - 1 + m["div_yield"]
            m["exp_return"] = {"bear": ann(iv["bear"], sc["bear"][0]), "base": ann(iv["base"], sc["base"][0]), "bull": ann(iv["bull"], sc["bull"][0])}
            m["target_5y"] = iv["base"] * (1 + min(sc["base"][0], 0.10)) ** 5
    return m


def _pct(series):
    return series.rank(pct=True)


def score(rows):
    """rows: list of dicts with 'market' + metrics -> adds percentile scores, composite, rating, conviction, sizing."""
    df = pd.DataFrame(rows)
    if df.empty:
        return rows
    for c in ("fcf_yield", "earnings_yield", "book_to_price", "roic", "roe", "fscore", "accruals", "margin_stability", "rev_cagr", "net_debt_to_ebit", "margin_of_safety"):
        if c not in df:
            df[c] = np.nan
        df[c] = pd.to_numeric(df[c], errors="coerce")
    out = []
    for mkt, g in df.groupby("market"):
        g = g.copy()
        val = pd.concat([_pct(g["fcf_yield"]), _pct(g["earnings_yield"]), _pct(g["book_to_price"])], axis=1).mean(axis=1)
        qual = pd.concat([_pct(g["roic"].fillna(g["roe"])), _pct(g["fscore"]), 1 - _pct(g["accruals"]), 1 - _pct(g["margin_stability"])], axis=1).mean(axis=1)
        grow = _pct(g["rev_cagr"])
        safe = 1 - _pct(g["net_debt_to_ebit"].clip(lower=0))
        g["score_value"], g["score_quality"], g["score_growth"], g["score_safety"] = val, qual, grow, safe
        comp = pd.concat([val * 0.35, qual * 0.35, grow.fillna(0.5) * 0.15, safe.fillna(0.5) * 0.15], axis=1).sum(axis=1)
        g["composite"] = comp.rank(pct=True)
        out.append(g)
    df = pd.concat(out)
    res = []
    for r in df.to_dict("records"):
        r = {k: (None if isinstance(v, float) and math.isnan(v) else v) for k, v in r.items()}
        er = r.get("exp_return") or {}
        mos = r.get("margin_of_safety")
        prof = r.get("roic") if not r.get("financial") else r.get("roe")
        hurdle = 0.14 if r.get("financial") else 0.12
        why, fail = [], []
        comp = r.get("composite") or 0
        checks = [("composite >= 75th pct", comp >= 0.75),
                  ("margin of safety >= 20%", (mos is not None and mos >= 0.20) or (r.get("financial") and (r.get("pb") or 99) < 2.5)),
                  ("F-score >= 6", (r.get("fscore") or 0) >= 6),
                  (f"{'ROE' if r.get('financial') else 'ROIC'} >= {int(hurdle*100)}%", prof is not None and prof >= hurdle),
                  ("FCF positive >= 3 of 4 yrs", r.get("financial") or (r.get("fcf_positive_years") or 0) >= min(3, max(r.get("fcf_years") or 0, 1))),
                  ("bear-case return > 0", r.get("financial") or (er.get("bear") is not None and er["bear"] > 0))]
        for n, ok in checks:
            (why if ok else fail).append(n)
        if all(ok for _, ok in checks):
            rating = "BUY"
        elif comp >= 0.60 and (mos is None or mos >= 0):
            rating = "WATCH"
        elif comp < 0.30 or (mos is not None and mos < -0.30) or (r.get("fscore") is not None and r["fscore"] <= 3):
            rating = "AVOID"
        else:
            rating = "HOLD"
        complete = min(1.0, (r.get("years") or 0) / 4) * (1.0 if r.get("fscore") is not None else 0.7)
        stab = 1 - min(1.0, (r.get("margin_stability") or 0.05) / 0.15)
        cushion = min(1.0, max(0.0, (mos or 0) / 0.5)) if not r.get("financial") else 0.5
        qual = r.get("score_quality") or 0.5
        conv = max(0.0, min(1.0, complete * (0.35 * qual + 0.25 * stab + 0.25 * cushion + 0.15 * comp)))
        spread = None
        if er.get("bull") is not None and er.get("bear") is not None:
            spread = er["bull"] - er["bear"]
        size = 0.0
        if rating == "BUY":
            size = 0.05 * conv * (1 - min(0.8, (spread or 0.2) / 0.5))
        r.update(rating=rating, conviction=round(conv, 3), suggested_weight=round(size, 4), passed=why, failed=fail,
                 stop_rule="exit if price falls 25% below entry, F-score drops to 4 or lower, or rating becomes AVOID",
                 horizon_years=5)
        res.append(r)
    return res


def fetch(yahoo):
    import yfinance as yf
    t = yf.Ticker(yahoo)
    info = t.info or {}
    return info, t.financials, t.balance_sheet, t.cashflow


def refresh(universe, cache, max_fetch=150, today=None, stale_days=7):
    """universe: [(id, yahoo, market, name)]. cache: {id: {..., 'fetched': iso}} -> refresh the oldest `max_fetch`."""
    today = today or dt.date.today()
    order = sorted(universe, key=lambda u: (cache.get(u[0], {}).get("fetched") or "0000"))
    done, errors = 0, {}
    for uid, yh, mkt, name in order:
        if done >= max_fetch:
            break
        f = cache.get(uid, {}).get("fetched")
        if f and (today - dt.date.fromisoformat(f[:10])).days < stale_days:
            continue
        try:
            info, fin, bs, cf = fetch(yh)
            m = compute(info, fin, bs, cf, mkt)
            cache[uid] = {**m, "id": uid, "name": info.get("shortName") or name, "yahoo": yh, "market": mkt, "fetched": today.isoformat()}
        except Exception as e:  # noqa
            errors[uid] = str(e)[:100]
            cache.setdefault(uid, {})["fetched"] = today.isoformat()      # try again next week, not every run
            cache[uid].update(id=uid, market=mkt, yahoo=yh, name=name, error=str(e)[:100])
        done += 1
    return cache, {"fetched_this_run": done, "errors": len(errors), "error_sample": dict(list(errors.items())[:5])}


def build(cache, prices=None):
    """Score everything cached; refresh price-dependent fields with today's prices (margin of safety, returns)."""
    rows = []
    for uid, m in cache.items():
        if m.get("error") or m.get("price") is None:
            continue
        m = dict(m)
        p = (prices or {}).get(uid)
        if p and m.get("price") and m.get("dcf") and m["dcf"].get("iv_base"):
            m["price"] = p
            ivb = m["dcf"]["iv_base"]
            m["margin_of_safety"] = ivb / p - 1
            g = {k: m["dcf"].get(f"growth_{k}", m["dcf"]["growth_base"]) for k in ("bear", "base", "bull")}
            m["exp_return"] = {k: ((m["dcf"][f"iv_{k}"] * (1 + min(g[k], 0.10)) ** 5 / p) ** 0.2 - 1 + (m.get("div_yield") or 0)) if m["dcf"].get(f"iv_{k}") else None
                               for k in ("bear", "base", "bull")}
        elif p:
            m["price"] = p
        rows.append(m)
    scored = score(rows)
    for r in scored:
        for k in list(r):
            if isinstance(r[k], float):
                r[k] = round(r[k], 4)
        if isinstance(r.get("exp_return"), dict):
            r["exp_return"] = {k: (round(v, 4) if v is not None else None) for k, v in r["exp_return"].items()}
        if isinstance(r.get("dcf"), dict):
            r["dcf"] = {k: (round(v, 4) if isinstance(v, float) else v) for k, v in r["dcf"].items()}
    scored.sort(key=lambda r: (-(r.get("rating") == "BUY"), -(r.get("composite") or 0)))
    recs = [{"ticker": r["id"], "market": r["market"], "action": r["rating"], "target_return": (r.get("exp_return") or {}).get("base"),
             "return_range": [(r.get("exp_return") or {}).get("bear"), (r.get("exp_return") or {}).get("bull")], "horizon_years": 5,
             "confidence": r.get("conviction"), "suggested_weight": r.get("suggested_weight")} for r in scored if r.get("rating") in ("BUY", "WATCH")]
    return scored, recs


def sector_capped(buys, cap=0.25, max_names=20):
    """Apply the 25% sector cap and 20-name limit to suggested weights (BUY list, best composite first)."""
    out, sec = [], {}
    for r in buys:
        if len(out) >= max_names:
            break
        s = r.get("sector") or "Other"
        room = cap - sec.get(s, 0)
        w = min(r.get("suggested_weight") or 0, max(room, 0))
        if w <= 0.002:
            continue
        sec[s] = sec.get(s, 0) + w
        out.append({**r, "suggested_weight": round(w, 4)})
    return out


def paper_step(ledger, aid, scored, frames, bar_date, simulator, market, rebalance):
    """Long-term value paper account: process today's bar, exit broken theses, and on rebalance days buy the sector-capped BUY list.
    Orders are MARKET orders filled at the next session's open (or live at the open by the session loop)."""
    from decimal import Decimal as D
    a = ledger.account(aid)
    acts = []
    by_id = {r["id"]: r for r in scored if r.get("market") == market}
    syms = set(a["positions"]) | {o["symbol"] for o in ledger.pending(aid)}
    for s in sorted(syms):
        df = frames.get(s)
        if df is None:
            continue
        ts = [t for t in df.index if str(t.date()) == str(bar_date)]
        if ts:
            acts += simulator.apply_bar(ledger, aid, s, df.loc[ts[0]], bar_date)
    pend = {o["symbol"] for o in ledger.pending(aid)}
    for s, p in list(a["positions"].items()):
        r = by_id.get(s, {})
        why = None
        if r.get("rating") == "AVOID":
            why = "rating fell to AVOID"
        elif r.get("fscore") is not None and r["fscore"] <= 4:
            why = f"F-score fell to {r['fscore']}"
        if why and s not in pend:
            o, st = ledger.submit(aid, s, "sell", p["qty"], "MARKET", bar_date=bar_date, strategy="value_longterm", reason=why,
                                  currency=a["currency"], segment="delivery")
            if st == "ok":
                acts.append(f"Queued EXIT {s} at next open - {why}")
    if rebalance:
        eq = ledger.equity(aid)
        cash = D(a["cash"]) - sum(D(o["qty"]) * D(str(float(frames[o["symbol"]]["Close"].iloc[-1]))) for o in ledger.pending(aid)
                                  if o["side"] == "buy" and o["symbol"] in frames)
        buys = sector_capped([r for r in scored if r.get("market") == market and r.get("rating") == "BUY"])
        for r in buys:
            s = r["id"]
            if s in a["positions"] or s in pend or s not in frames:
                continue
            px = float(frames[s]["Close"].iloc[-1])
            qty = int(float(eq) * r["suggested_weight"] // px)
            if qty < 1 or D(str(qty * px * 1.01)) > cash:
                continue
            o, st = ledger.submit(aid, s, "buy", qty, "MARKET", bar_date=bar_date, strategy="value_longterm",
                                  reason=f"value BUY: conviction {r['conviction']}, base 5y return {((r.get('exp_return') or {}).get('base') or 0)*100:.1f}%/yr",
                                  stop=round(px * 0.75, 2), currency=a["currency"], segment="delivery", sector=r.get("sector"),
                                  expires_bars=5, meta={"weight": r["suggested_weight"], "conviction": r["conviction"]})
            if st == "ok":
                cash -= D(str(qty * px))
                acts.append(f"Queued BUY {qty} {s} at next open (weight {r['suggested_weight']*100:.1f}%, stop -25%)")
    prices = {s: float(frames[s]["Close"].iloc[-1]) for s in a["positions"] if s in frames}
    ledger.mark(aid, prices, bar_date)
    return acts or ["Nothing to do: holding the long-term book."]
