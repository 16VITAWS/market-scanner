"""
Machine-learning shadow model (runs beside the rule scanner; NEVER places paper or real orders by itself).

Panel data: every NSE stock x day. Features known at the close of day t:
  rsi, close/SMA50-1, close/SMA200-1, 21d & 63d momentum, 20d vol, volume/20d-avg, 20d-breakout flag,
  ATR%, 63d return minus NIFTY's 63d return.
Label: forward 10-session return > +1% (roughly clears round-trip costs + slippage).
Model: ensemble = average of HistGradientBoosting and L2 logistic regression (scikit-learn).
Validation: walk-forward by calendar month with a 10-session embargo between train and test (no label overlap).
Comparison: precision of the model's top picks vs the rule scanner's BUY signals on the same out-of-sample days.
Promotion rule (reported, not automatic): the model may only influence paper trading after 6+ OOS months where
its top-10 precision beats the rule signals by >= 3 points AND beats the base rate.
"""
import numpy as np
import pandas as pd
from . import indicators as I

FEATURES = ["rsi", "d50", "d200", "m21", "m63", "vol20", "vratio", "brk", "atrp", "rs63"]
HORIZON, THRESH = 10, 0.01


def panel(stocks, index_close, min_bars=260):
    rows = []
    ic = index_close.copy(); ic.index = pd.to_datetime(ic.index.date)
    for sym, df in stocks.items():
        if len(df) < min_bars:
            continue
        d = I.enrich(df)
        d.index = pd.to_datetime(d.index.date)
        c = d["Close"]
        f = pd.DataFrame(index=d.index)
        f["rsi"] = d["rsi"] / 100
        f["d50"] = c / d["sma50"] - 1
        f["d200"] = c / d["sma200"] - 1
        f["m21"] = c / c.shift(21) - 1
        f["m63"] = c / c.shift(63) - 1
        f["vol20"] = c.pct_change().rolling(20).std() * np.sqrt(252)
        f["vratio"] = d["Volume"] / d["vol20"]
        f["brk"] = (c > d["hi20"]).astype(float)
        f["atrp"] = d["atr"] / c
        icr = ic.reindex(f.index, method="ffill")
        f["rs63"] = f["m63"] - (icr / icr.shift(63) - 1)
        f["fwd"] = c.shift(-HORIZON) / c - 1
        f["symbol"] = sym
        rows.append(f)
    if not rows:
        return pd.DataFrame()
    p = pd.concat(rows).replace([np.inf, -np.inf], np.nan).dropna(subset=FEATURES)
    return p


def _models():
    from sklearn.ensemble import HistGradientBoostingClassifier
    from sklearn.linear_model import LogisticRegression
    from sklearn.pipeline import make_pipeline
    from sklearn.preprocessing import StandardScaler
    return [HistGradientBoostingClassifier(max_depth=3, max_iter=150, learning_rate=0.05, l2_regularization=1.0, random_state=7),
            make_pipeline(StandardScaler(), LogisticRegression(C=0.5, max_iter=500))]


def _fit_predict(tr, te):
    X, y = tr[FEATURES].values, (tr["fwd"] > THRESH).astype(int).values
    ps = []
    for m in _models():
        m.fit(X, y); ps.append(m.predict_proba(te[FEATURES].values)[:, 1])
    return np.mean(ps, axis=0)


def run(stocks, index_df, rule_buy_score=5, min_train_days=200):
    try:
        import sklearn  # noqa
    except Exception:
        return {"available": False, "reason": "scikit-learn not installed"}
    p = panel(stocks, index_df["Close"])
    if p.empty:
        return {"available": False, "reason": "not enough stock history"}
    dates = sorted(p.index.unique())
    if len(dates) < min_train_days + 60:
        return {"available": False, "reason": f"needs {min_train_days + 60}+ sessions of panel data"}
    labelled = p.dropna(subset=["fwd"])
    months = sorted({d.to_period("M") for d in dates[min_train_days:]})
    oos = []
    for mth in months:
        test = labelled[labelled.index.to_period("M") == mth]
        if test.empty:
            continue
        cutoff = test.index.min() - pd.tseries.offsets.BDay(HORIZON + 1)    # embargo: no training label overlaps the test month
        train = labelled[labelled.index <= cutoff]
        if len(train) < 20000 and len(train) < len(labelled) * 0.3:
            continue
        if len(train) > 150000:
            train = train.sample(150000, random_state=1)
        te = test.copy(); te["p"] = _fit_predict(train, te)
        oos.append(te)
    base = float((labelled["fwd"] > THRESH).mean())
    res = {"available": True, "features": FEATURES, "horizon_days": HORIZON, "label": f"10-session forward return > {THRESH*100:.0f}%", "base_rate": round(base, 3)}
    if oos:
        o = pd.concat(oos)
        o["hit"] = (o["fwd"] > THRESH).astype(int)
        top = o.groupby(level=0, group_keys=False).apply(lambda g: g.nlargest(10, "p"))
        # rule proxy on the same days: trend + breakout + healthy RSI + relative strength (the scanner's main score parts)
        rule = o[(o["d50"] > 0) & (o["d200"] > 0) & (o["brk"] == 1) & (o["rsi"].between(0.5, 0.7)) & (o["rs63"] > 0)]
        by_month = []
        for mth, g in top.groupby(top.index.to_period("M")):
            rm = rule[rule.index.to_period("M") == mth]
            by_month.append({"month": str(mth), "ml_top10_precision": round(float(g["hit"].mean()), 3), "ml_top10_avg_fwd_pct": round(float(g["fwd"].mean() * 100), 2),
                             "rule_precision": round(float(rm["hit"].mean()), 3) if len(rm) else None, "rule_n": int(len(rm))})
        mlp, rp = float(top["hit"].mean()), (float(rule["hit"].mean()) if len(rule) else None)
        good_months = sum(1 for m in by_month if m["rule_precision"] is not None and m["ml_top10_precision"] >= m["rule_precision"] + 0.03)
        res["oos"] = {"months": len(by_month), "ml_top10_precision": round(mlp, 3), "ml_top10_avg_fwd_pct": round(float(top["fwd"].mean() * 100), 2),
                      "rule_precision": round(rp, 3) if rp is not None else None, "rule_signals": int(len(rule)), "by_month": by_month[-24:],
                      "months_ml_beat_rules_by_3pts": good_months}
        res["promotable"] = bool(len(by_month) >= 6 and rp is not None and mlp >= rp + 0.03 and mlp > base)
        # permutation importance on the most recent OOS months (what the model actually leans on)
        recent = o[o.index >= o.index.max() - pd.Timedelta(days=120)]
        if len(recent) > 500:
            train = labelled[labelled.index <= recent.index.min() - pd.tseries.offsets.BDay(HORIZON + 1)]
            if len(train) > 150000:
                train = train.sample(150000, random_state=1)
            from sklearn.metrics import roc_auc_score
            y = (recent["fwd"] > THRESH).astype(int).values
            basep = _fit_predict(train, recent)
            try:
                base_auc = roc_auc_score(y, basep); imp = []
                for f in FEATURES:
                    sh = recent.copy(); sh[f] = np.random.default_rng(3).permutation(sh[f].values)
                    imp.append({"feature": f, "auc_drop": round(float(base_auc - roc_auc_score(y, _fit_predict(train, sh))), 4)})
                res["importance"] = sorted(imp, key=lambda x: -x["auc_drop"]); res["recent_auc"] = round(float(base_auc), 3)
            except Exception:
                pass
    # today's ranking (fit on all labelled data)
    today = p[p.index == p.index.max()]
    tr = labelled if len(labelled) <= 150000 else labelled.sample(150000, random_state=1)
    today = today.copy(); today["p"] = _fit_predict(tr, today)
    res["today"] = [{"symbol": r.symbol, "p": round(float(r.p), 3), "m63": round(float(r.m63) * 100, 1), "rsi": round(float(r.rsi) * 100, 0),
                     "d50": round(float(r.d50) * 100, 1)} for r in today.nlargest(15, "p").itertuples()]
    res["all_today"] = {r.symbol: round(float(r.p), 3) for r in today.itertuples()}
    res["as_of"] = str(p.index.max().date())
    res["note"] = ("SHADOW MODE: ranks stocks, never trades. " + ("Meets the promotion rule." if res.get("promotable") else "Has NOT met the promotion rule; rule scanner stays in charge."))
    return res
