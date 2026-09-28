"""
Global event -> Indian sector impact map.

1. Sensitivity table (statistics, NOT proven causation):
   For every (global driver, Indian index) pair, an OLS beta of the Indian index's daily return on the driver's move,
   over the last 500 sessions, with t-statistic, R-squared and a stability check (same sign in both halves).
   Timing matters:  drivers that close AFTER the Indian market (US stocks, US yields, VIX, dollar index, Brent, gold)
   are paired with the NEXT Indian session -> usable before the open.  USD/INR trades during Indian hours, so it is
   same-day CO-MOVEMENT (not usable to predict).
2. Shock detector: if a driver's latest move is >= 2 standard deviations, the expected reaction of each linked index is
   beta x move, with a +-1 sd band from the regression residuals. Only links with |t| >= 2.5 and stable sign are used.
3. News linkage: headlines are tagged to drivers by keywords (e.g. "OPEC", "crude" -> Brent) so a shock is shown next
   to the news that may explain it. Keyword tagging is crude and labelled as such.
With ~10 drivers x ~14 indices, some |t| >= 2.5 links can appear by chance (multiple testing); stability filtering reduces this.
"""
import math
import numpy as np
import pandas as pd

DRIVERS = {  # id: (label, timing, transform)
    "SPX": ("S&P 500", "next", "pct"), "NDX": ("Nasdaq 100", "next", "pct"), "VIX": ("US VIX", "next", "pct"),
    "US10Y": ("US 10y yield", "next", "bp"), "DXY": ("Dollar index", "next", "pct"), "BRENT": ("Brent crude", "next", "pct"),
    "GOLD": ("Gold", "next", "pct"), "COPPER": ("Copper", "next", "pct"), "NIKKEI": ("Nikkei", "same", "pct"), "HSI": ("Hang Seng", "same", "pct"),
    "USDINR": ("USD/INR", "same", "pct"),
}
TARGETS = ["NIFTY", "BANKNIFTY", "NIFTYMIDCAP", "NIFTYIT", "NIFTYAUTO", "NIFTYPHARMA", "NIFTYFMCG", "NIFTYMETAL", "NIFTYENERGY",
           "NIFTYREALTY", "NIFTYPSUBANK", "NIFTYINFRA", "NIFTYMEDIA", "FINNIFTY"]
NEWS_MAP = [
    (("crude", "brent", "opec", "oil price", "oil prices", "hormuz", "refinery"), "BRENT"),
    (("fed ", "fomc", "powell", "treasury yield", "us yields", "us inflation", "us cpi", "rate hike", "rate cut"), "US10Y"),
    (("dollar index", "greenback", "dxy"), "DXY"),
    (("rupee", "usd/inr", "usdinr", "inr "), "USDINR"),
    (("gold",), "GOLD"),
    (("wall street", "nasdaq", "s&p 500", "dow jones", "us stocks"), "SPX"),
    (("war", "missile", "attack", "sanction", "tariff", "geopolit", "conflict", "escalat"), "VIX"),
    (("copper", "metal prices", "china stimulus", "iron ore"), "COPPER"),
    (("nikkei", "japan", "boj"), "NIKKEI"),
    (("hang seng", "china stocks", "beijing"), "HSI"),
]


def _series(df):
    s = df["Close"].copy(); s.index = pd.to_datetime(s.index.date)
    return s[~s.index.duplicated(keep="last")]


def _chg(s, how):
    return s.diff() * 100 if how == "bp" else s.pct_change() * 100     # Yahoo ^TNX is the yield in percent -> diff*100 = basis points


def aligned(frames, driver, target):
    """Return (x driver move, y target % return) aligned by timing rule."""
    lab, timing, how = DRIVERS[driver]
    x = _chg(_series(frames[driver]), how).dropna()
    t = _series(frames[target]); y = (t.pct_change() * 100).dropna()
    if timing == "same":
        df = pd.concat([x.rename("x"), y.rename("y")], axis=1, join="inner").dropna()
    else:
        # for each Indian session d, use the driver's last move dated strictly BEFORE d (US day d closes after India day d)
        m = pd.merge_asof(pd.DataFrame({"d": y.index, "y": y.values}).sort_values("d"), pd.DataFrame({"d": x.index, "x": x.values}).sort_values("d"),
                          on="d", direction="backward", allow_exact_matches=False).set_index("d")
        df = m.dropna()
    return df


def ols(df):
    x, y = df["x"].values, df["y"].values
    n = len(x)
    if n < 60 or np.std(x) == 0:
        return None
    xm, ym = x.mean(), y.mean()
    b = ((x - xm) * (y - ym)).sum() / ((x - xm) ** 2).sum()
    a = ym - b * xm
    res = y - a - b * x
    se = math.sqrt((res ** 2).sum() / (n - 2) / ((x - xm) ** 2).sum())
    r2 = 1 - (res ** 2).sum() / ((y - ym) ** 2).sum()
    return {"beta": b, "t": b / se if se else 0.0, "r2": r2, "resid_sd": float(res.std()), "n": n}


def baskets(stocks, sectors, min_members=5):
    """Equal-weight daily-rebalanced sector baskets from the scanned NSE stocks (used when Yahoo lacks history for the
    official sector index). Labelled 'SEC:<sector>'; NOT the official NSE index."""
    groups = {}
    for k, df in stocks.items():
        sec = sectors.get(k)
        if sec and len(df) > 250:
            c = _series(df)
            groups.setdefault(sec, []).append(c.pct_change())
    out = {}
    for sec, rets in groups.items():
        if len(rets) < min_members:
            continue
        r = pd.concat(rets, axis=1).clip(-0.2, 0.2)
        cnt = r.notna().sum(1)
        m = r.mean(1)[cnt >= min_members].fillna(0)
        if len(m) > 250:
            out["SEC:" + sec] = pd.DataFrame({"Close": 1000 * (1 + m).cumprod()})
    return out


def table(frames, window=500, min_bars=250):
    rows = []
    targets = [t for t in TARGETS if t in frames and len(frames[t]) >= min_bars] + sorted(k for k in frames if k.startswith("SEC:"))
    for d in DRIVERS:
        if d not in frames or len(frames[d]) < 120:
            continue
        for t in targets:
            df = aligned(frames, d, t).tail(window)
            o = ols(df)
            if not o:
                continue
            h = len(df) // 2
            o1, o2 = ols(df.iloc[:h]), ols(df.iloc[h:])
            stable = bool(o1 and o2 and np.sign(o1["beta"]) == np.sign(o2["beta"]) == np.sign(o["beta"]))
            rows.append({"driver": d, "driver_label": DRIVERS[d][0], "target": t, "timing": DRIVERS[d][1], "unit": "bp" if DRIVERS[d][2] == "bp" else "%",
                         "beta": round(float(o["beta"]), 3), "t": round(float(o["t"]), 1), "r2": round(float(o["r2"]), 3), "resid_sd_pct": round(o["resid_sd"], 2), "n": o["n"],
                         "stable": stable, "significant": bool(abs(o["t"]) >= 2.5 and stable)})
    return rows


def shocks(frames, rows, z_min=2.0):
    out = []
    for d, (lab, timing, how) in DRIVERS.items():
        if d not in frames or len(frames[d]) < 80:
            continue
        ch = _chg(_series(frames[d]), how).dropna()
        sd = float(ch.tail(250).std())
        last = float(ch.iloc[-1])
        z = last / sd if sd else 0
        if abs(z) < z_min:
            continue
        eff = []
        for r in rows:
            if r["driver"] == d and r["significant"]:
                mv = float(r["beta"] * last)
                eff.append({"target": r["target"], "expected_move_pct": round(mv, 2), "band_pct": [round(mv - r["resid_sd_pct"], 2), round(mv + r["resid_sd_pct"], 2)],
                            "r2": r["r2"], "timing": "next Indian session" if timing == "next" else "same day (co-movement)"})
        eff.sort(key=lambda e: -abs(e["expected_move_pct"]))
        out.append({"driver": d, "label": lab, "date": str(ch.index[-1].date()), "move": round(last, 2), "unit": "bp" if how == "bp" else "%",
                    "z": round(z, 1), "effects": eff[:8],
                    "text": f"{lab} moved {last:+.2f}{'bp' if how == 'bp' else '%'} ({z:+.1f} sd) on {ch.index[-1].date()}" +
                            (f"; historically linked: " + ", ".join(f"{e['target']} {e['expected_move_pct']:+.2f}%" for e in eff[:4]) if eff else "; no stable link to Indian indices")})
    out.sort(key=lambda s: -abs(s["z"]))
    return out


def tag_news(items):
    out = []
    for it in items or []:
        t = " " + (it.get("title") or "").lower() + " "
        drv = sorted({d for words, d in NEWS_MAP if any(w in t for w in words)})
        if drv:
            out.append({"title": it.get("title"), "link": it.get("link"), "published_utc": it.get("published_utc"), "source": it.get("source"),
                        "drivers": drv, "sentiment": it.get("sentiment"), "method": "keyword tag"})
    return out


def run(frames, news_items=None, extra=None):
    frames = {**frames, **(extra or {})}
    rows = table(frames)
    sh = shocks(frames, rows)
    tagged = tag_news(news_items)
    for s in sh:
        s["news"] = [n for n in tagged if s["driver"] in n["drivers"]][:5]
    # strongest stable links per target (the "graph" edges)
    edges = sorted([r for r in rows if r["significant"]], key=lambda r: -r["r2"])
    return {"table": rows, "edges": edges[:60], "shocks": sh, "news_tagged": tagged[:40], "drivers": {k: v[0] for k, v in DRIVERS.items()},
            "targets": sorted({r["target"] for r in rows}),
            "target_note": "Official NSE sector indices are used where the free feed has >= 1 year of history; otherwise 'SEC:<sector>' is an equal-weight basket of the scanned Nifty 500 stocks in that sector (not the official index).", "method": __doc__.strip(), "status": "STATISTICAL (not causal)"}
