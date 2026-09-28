"""
Market knowledge graph (seed) + statistical relationships computed from data.

Each edge: source -> target, relation, direction (+ / -), basis (documented | statistical | hypothesis),
evidence text, source note, date. Documented edges come from public, widely-reported economics
(e.g. oil is an input cost for airlines). Statistical edges are recomputed from price data each run
with a lookback window and are labelled with their correlation and sample size. Nothing here claims
causation.
"""
import numpy as np
import pandas as pd

SEED_EDGES = [
    # subject, relation, object, direction, basis, evidence
    ("BRENT", "input_cost_for", "sector:Oil marketing (IOC, BPCL, HPCL)", "-", "documented", "Crude is the main input; retail prices adjust with a lag"),
    ("BRENT", "input_cost_for", "sector:Aviation (INDIGO)", "-", "documented", "ATF is ~40% of airline costs"),
    ("BRENT", "input_cost_for", "sector:Paints (ASIANPAINT, BERGEPAINT)", "-", "documented", "Crude derivatives are ~50% of raw material"),
    ("BRENT", "revenue_driver_for", "sector:Oil upstream (ONGC, OIL)", "+", "documented", "Realisations track crude"),
    ("BRENT", "pressure_on", "USDINR", "+", "documented", "India imports ~85% of crude; higher oil widens the current account and weakens INR"),
    ("BRENT", "pressure_on", "INDIA_CPI", "+", "documented", "Fuel weight in CPI and transport pass-through"),
    ("USDINR", "revenue_driver_for", "sector:IT services (TCS, INFY, HCLTECH, WIPRO)", "+", "documented", "Dollar revenue, rupee costs"),
    ("USDINR", "revenue_driver_for", "sector:Pharma exporters (SUNPHARMA, DRREDDY, CIPLA)", "+", "documented", "US generics revenue in USD"),
    ("USDINR", "input_cost_for", "sector:Oil marketing", "-", "documented", "Crude bought in USD"),
    ("USDINR", "input_cost_for", "sector:Airlines", "-", "documented", "Fuel and leases in USD"),
    ("US10Y", "valuation_pressure_on", "NIFTY", "-", "documented", "Higher US yields pull FPI flows out of emerging markets"),
    ("US10Y", "valuation_pressure_on", "sector:IT services", "-", "statistical", "Long-duration growth stocks; correlation varies"),
    ("RBI_REPO", "margin_driver_for", "sector:Banks (HDFCBANK, ICICIBANK, SBIN)", "+/-", "documented", "Rate cuts compress NIM short-term, lift credit growth"),
    ("RBI_REPO", "cost_driver_for", "sector:NBFC (BAJFINANCE, SHRIRAMFIN, CHOLAHLDNG)", "-", "documented", "Borrowing costs move with policy rates"),
    ("RBI_REPO", "demand_driver_for", "sector:Real estate & housing finance", "-", "documented", "Mortgage affordability"),
    ("RBI_REPO", "demand_driver_for", "sector:Autos (MARUTI, M&M, TMPV)", "-", "documented", "Vehicle loans"),
    ("GOLD", "hedge_against", "NIFTY", "-", "statistical", "Rolling correlation is usually near zero; turns negative in stress"),
    ("VIX", "stress_indicator_for", "SPX", "-", "documented", "Implied volatility rises when the S&P falls"),
    ("INDIAVIX", "stress_indicator_for", "NIFTY", "-", "documented", "Implied volatility of NIFTY options"),
    ("SPX", "leads", "GIFTNIFTY", "+", "statistical", "Overnight US moves show in GIFT Nifty before the NSE open"),
    ("NDX", "leads", "sector:IT services", "+", "statistical", "US tech spending sentiment"),
    ("FII_FLOWS", "flow_pressure_on", "NIFTY", "+", "statistical", "Net FII selling coincides with weak days; not predictive on its own"),
    ("COPPER", "demand_signal_for", "sector:Metals (HINDALCO, TATASTEEL, JSWSTEEL)", "+", "documented", "Global industrial demand proxy"),
    ("NATGAS", "input_cost_for", "sector:Fertilisers & city gas", "-", "documented", "Feedstock"),
    ("TARIFFS_US", "demand_risk_for", "sector:Textiles, gems & jewellery, chemicals exporters", "-", "documented", "Export exposure to the US"),
    ("WAR_MIDDLE_EAST", "supply_risk_for", "BRENT", "+", "documented", "Gulf supply and shipping routes"),
    ("NIFTY", "constituent_of", "index:NIFTY 50 members", "+", "documented", "Index moves with weighted members"),
    ("NIFTYBEES", "tracks", "NIFTY", "+", "documented", "ETF tracks the index"),
    ("GOLDBEES", "tracks", "GOLD", "+", "documented", "ETF tracks domestic gold (INR)"),
]

EVENT_TYPES = {
    "rate_decision": {"watch": ["RBI_REPO", "US10Y", "sector:Banks", "sector:NBFC", "sector:Real estate"], "horizon": "1-10 sessions"},
    "inflation_release": {"watch": ["INDIA_CPI", "RBI_REPO", "sector:FMCG"], "horizon": "1-5 sessions"},
    "oil_shock": {"watch": ["BRENT", "sector:Oil marketing", "sector:Aviation", "sector:Paints", "USDINR"], "horizon": "1-20 sessions"},
    "geopolitics": {"watch": ["BRENT", "GOLD", "VIX", "INDIAVIX", "USDINR"], "horizon": "1-20 sessions"},
    "tariffs": {"watch": ["TARIFFS_US", "sector:Textiles", "sector:Chemicals", "USDINR"], "horizon": "5-60 sessions"},
    "earnings": {"watch": ["company"], "horizon": "1-5 sessions"},
    "currency_move": {"watch": ["USDINR", "sector:IT services", "sector:Pharma exporters", "sector:Oil marketing"], "horizon": "1-10 sessions"},
}


def seed_graph():
    return [{"from": a, "relation": r, "to": b, "direction": d, "basis": basis, "evidence": ev, "uncertainty": "medium" if basis == "documented" else "high",
             "source": "economic mechanism, widely reported; verify per event", "as_of": "2026-09-27"} for a, r, b, d, basis, ev in SEED_EDGES]


def correlations(closes, windows=(20, 60, 120)):
    """closes: DataFrame of aligned close prices (columns = ids). Returns matrices for each window and rolling 60d for key pairs."""
    rets = closes.pct_change()
    out = {"windows": {}, "note": "Pearson correlation of daily returns. Correlations change over time and do not imply causation."}
    for w in windows:
        r = rets.tail(w).dropna(how="all")
        m = r.corr(min_periods=max(10, w // 2))
        out["windows"][str(w)] = {"ids": list(m.columns), "matrix": [[None if np.isnan(v) else round(float(v), 2) for v in row] for row in m.values],
                                  "n": int(len(r))}
    return out


def betas(closes, bench="NIFTY", n=120):
    rets = closes.pct_change().tail(n)
    if bench not in rets:
        return {}
    b = rets[bench]
    out = {}
    for c in rets.columns:
        if c == bench:
            continue
        pair = pd.concat([rets[c], b], axis=1).dropna()
        if len(pair) < 30:
            continue
        cov = np.cov(pair.iloc[:, 0], pair.iloc[:, 1])
        out[c] = {"beta": round(float(cov[0, 1] / cov[1, 1]), 2), "corr": round(float(pair.corr().iloc[0, 1]), 2), "n": int(len(pair))}
    return out


def impact_candidates(event_type, graph):
    """Which nodes to look at for an event type, with the documented reasons. No direction claims for the price."""
    watch = EVENT_TYPES.get(event_type, {}).get("watch", [])
    edges = [e for e in graph if any(w.lower() in (e["from"] + e["to"]).lower() for w in watch)]
    return {"event_type": event_type, "horizon": EVENT_TYPES.get(event_type, {}).get("horizon"), "watch": watch, "edges": edges,
            "caveat": "Candidates only. Actual moves must be measured after the event; alternative explanations always exist."}
