"""
Execution algorithms for paper orders, and an execution-quality report.

TWAP  - equal slices across the session.        Price = mean of slice prices.
VWAP  - slices in proportion to traded volume.   Price = sum(p*v)/sum(v).
POV   - trade a fixed share of market volume;   may take several bars/days for big orders.
Implementation shortfall = (average fill - decision price) / decision price, signed so that positive = cost.

With 5-minute bars the algorithms run slice by slice. With only daily bars they use documented
approximations (TWAP ~ (O+H+L+C)/4, VWAP ~ (H+L+C)/3, POV = pov x daily volume per day) and say so.
"""
import numpy as np
import pandas as pd


def simulate(bars, qty, side, algo="VWAP", pov=0.1, slices=None, spread_bps=5):
    """bars: DataFrame of the execution window (intraday or daily). Returns dict with avg price, filled qty, slices."""
    if bars is None or len(bars) == 0 or qty <= 0:
        return {"filled": 0, "avg": None, "algo": algo, "note": "no bars"}
    half = spread_bps / 1e4 / 2 * (1 if side == "buy" else -1)
    intraday = len(bars) > 1 and (bars.index[1] - bars.index[0]) < pd.Timedelta(hours=6)
    tp = (bars["High"] + bars["Low"] + bars["Close"]) / 3
    fills = []
    if algo == "TWAP":
        n = slices or len(bars)
        idx = np.linspace(0, len(bars) - 1, n).round().astype(int)
        q = np.full(n, qty // n); q[: qty - q.sum()] += 1
        for i, qq in zip(idx, q):
            b = bars.iloc[i]
            px = float(tp.iloc[i]) if intraday else float((b.Open + b.High + b.Low + b.Close) / 4)
            fills.append((px * (1 + half), int(qq)))
    elif algo == "VWAP":
        v = bars["Volume"].replace(0, np.nan).fillna(bars["Volume"].mean() or 1)
        w = (v / v.sum()).values
        q = np.floor(w * qty).astype(int); q[np.argmax(w)] += qty - q.sum()
        for i, qq in enumerate(q):
            if qq > 0:
                fills.append((float(tp.iloc[i]) * (1 + half), int(qq)))
    elif algo == "POV":
        left = qty
        for i in range(len(bars)):
            if left <= 0:
                break
            take = int(min(left, max(1, bars["Volume"].iloc[i] * pov)))
            fills.append((float(tp.iloc[i]) * (1 + half), take)); left -= take
    else:  # OPEN (the default paper rule)
        fills.append((float(bars["Open"].iloc[0]) * (1 + half), qty))
    filled = sum(q for _, q in fills)
    avg = sum(p * q for p, q in fills) / filled if filled else None
    return {"algo": algo, "filled": int(filled), "unfilled": int(qty - filled), "avg": round(avg, 4) if avg else None, "slices": len(fills),
            "basis": "intraday bars" if intraday else "daily-bar approximation"}


def shortfall(avg, decision, side):
    if not avg or not decision:
        return None
    s = (avg - decision) / decision
    return round((s if side == "buy" else -s) * 1e4, 1)      # basis points, positive = cost


def report(fills, frames, decisions):
    """For each engine paper fill, compare the OPEN fill actually used with what TWAP / VWAP / POV would have done on that bar."""
    rows = []
    for f in fills[-100:]:
        sym, bar = f.get("symbol"), f.get("bar")
        df = frames.get(sym)
        if df is None or not bar or f.get("rule", "").startswith("modeled"):
            continue
        day = df[[str(d.date()) == str(bar) for d in df.index]]
        if day.empty:
            continue
        q, side = int(float(f["qty"])), f["side"]
        dec = decisions.get(f.get("order")) or float(df["Close"][df.index < day.index[0]].iloc[-1]) if (df.index < day.index[0]).any() else None
        row = {"symbol": sym, "bar": str(bar), "side": side, "qty": q, "decision_price": round(dec, 2) if dec else None,
               "actual_fill": float(f["price"]), "actual_bps": shortfall(float(f["price"]), dec, side)}
        for algo in ("TWAP", "VWAP", "POV"):
            r = simulate(day, q, side, algo)
            row[algo.lower()] = r["avg"]; row[algo.lower() + "_bps"] = shortfall(r["avg"], dec, side)
        rows.append(row)
    agg = {}
    for k in ("actual_bps", "twap_bps", "vwap_bps", "pov_bps"):
        v = [r[k] for r in rows if r.get(k) is not None]
        agg[k] = round(float(np.mean(v)), 1) if v else None
    return {"rows": rows[-50:], "average_bps": agg,
            "note": "Implementation shortfall in basis points vs the decision price (previous close). Positive = cost. Daily-bar TWAP/VWAP are approximations; intraday bars are used when available."}
