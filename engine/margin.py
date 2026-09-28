"""
SPAN-like margin ESTIMATE for option / futures positions (paper and pre-trade checks).

Real NSE margins come from NSE Clearing's SPAN risk-parameter files plus exposure margin, and brokers
may add more; this module does not download those files. It reproduces the idea:
  scan loss = worst portfolio loss over a 16-point grid of price moves (+-1/3, 2/3, 3/3 of the price scan range)
              and volatility moves (+-vol scan range), repriced with Black-Scholes;
  exposure  = 2% of short-option / futures notional (index), per the exchange's usual convention;
  net option value: long premium already paid is not margined.
Price scan range defaults to 3.5 x daily sigma x sqrt(2) (two-day liquidation), floored at 6% for indices.
Always labelled ESTIMATE; the broker's number is the one that counts.
"""
import math, datetime as dt
from . import options as OPT


def estimate(legs, S, sigma, today, index=True):
    """legs: [{kind:'C'|'P'|'F', strike, expiry(date iso), qty (+long/-short units)}]"""
    daily = sigma / math.sqrt(252)
    psr = max(3.5 * daily * math.sqrt(2), 0.06 if index else 0.075)
    vsr = 0.25                                                           # +-25% relative vol shock
    def value(Sx, volx):
        v = 0.0
        for l in legs:
            if l["kind"] == "F":
                v += l["qty"] * Sx; continue
            T = max((dt.date.fromisoformat(l["expiry"]) - today).days, 0) / 365
            v += l["qty"] * OPT.bs(Sx, l["strike"], T, OPT.PARAMS["r"], volx, l["kind"])["price"]
        return v
    base = value(S, sigma)
    scen = []
    for frac in (0, 1 / 3, 2 / 3, 1):
        for sgn in (1, -1):
            for vs in (1 + vsr, 1 - vsr):
                scen.append(base - value(S * (1 + sgn * frac * psr), sigma * vs))
    extreme = [0.35 * (base - value(S * (1 + s * 2 * psr), sigma)) for s in (1, -1)]   # SPAN-style extreme move, 35% weight
    scan = max(0.0, max(scen + extreme))
    short_notional = sum(abs(l["qty"]) * S for l in legs if l["qty"] < 0 or l["kind"] == "F")
    exposure = 0.02 * short_notional if index else 0.035 * short_notional
    long_premium = sum(l["qty"] * OPT.bs(S, l["strike"], max((dt.date.fromisoformat(l["expiry"]) - today).days, 0) / 365, OPT.PARAMS["r"], sigma, l["kind"])["price"]
                       for l in legs if l["kind"] != "F" and l["qty"] > 0)
    only_long = all(l["qty"] > 0 for l in legs)
    total = long_premium if only_long else scan + exposure
    return {"scan_loss": round(scan, 0), "exposure": round(exposure, 0), "estimated_margin": round(total, 0), "price_scan_range_pct": round(psr * 100, 2),
            "vol_scan_range_pct": vsr * 100, "long_premium": round(long_premium, 0), "status": "ESTIMATE",
            "note": "SPAN-like estimate (16 price/vol scenarios + extreme move, plus exposure margin). Your broker's margin is authoritative."}


def spread_margin(spread, qty, S, sigma, today):
    s = spread
    legs = [{"kind": s["kind"], "strike": s["k_long"], "expiry": s["expiry"], "qty": qty},
            {"kind": s["kind"], "strike": s["k_short"], "expiry": s["expiry"], "qty": -qty}]
    est = estimate(legs, S, sigma, today)
    T = max((dt.date.fromisoformat(s["expiry"]) - today).days, 0) / 365
    debit = (OPT.bs(S, s["k_long"], T, OPT.PARAMS["r"], sigma, s["kind"])["price"] - OPT.bs(S, s["k_short"], T, OPT.PARAMS["r"], sigma, s["kind"])["price"]) * qty
    return {"capital_required": round(debit, 0), "max_loss": round(debit, 0), "span_like_upper_bound": est["estimated_margin"], "scan_loss": est["scan_loss"],
            "status": "ESTIMATE", "note": "Debit spread: the most you can lose is the net debit, which is what is paid. Brokers with hedge benefit block about that; "
            "the upper bound is SPAN + 2% exposure on the short leg WITHOUT hedge benefit."}
