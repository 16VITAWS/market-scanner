"""
Event-driven bar backtester for a single instrument, plus metrics, walk-forward and seasonality.

* Decisions are taken on the close of bar t using only bars <= t; fills happen at open of t+1 (+slippage).
* Costs from costs.py per fill. Stops/targets checked on each bar (stop first if both hit).
* Returns equity curve, trades, and a metric set. Floats are fine here (research), the paper ledger uses Decimal.
"""
import math
import numpy as np
import pandas as pd
from decimal import Decimal
from . import costs, indicators as I


def _fee(value, side, segment, currency="INR"):
    return float(costs.charges(value, side, currency, segment)["total"])


def run(df, strategy, capital=200000.0, qty=None, qty_mode="fixed", risk_pct=0.01, segment="futures", slippage=0.0005,
        allow_short=False, stop_pct=None, target_pct=None, max_hold=None, ctx_fn=None, currency="INR", warmup=None):
    """
    qty_mode: 'fixed' (qty units) | 'all_in' (cash/price) | 'risk' (risk_pct of equity to strategy stop)
    ctx_fn(d_up_to_t) -> context dict for the strategy (e.g. regime).
    """
    d = df.copy()
    n = len(d)
    warm = warmup or getattr(strategy, "warmup", 200)
    cash, pos, trades, eq, marks, pending = float(capital), None, [], [], [], None
    o, h, l, c = d["Open"].values, d["High"].values, d["Low"].values, d["Close"].values
    dates = d.index
    enriched = I.enrich(d) if "sma200" not in d.columns else d
    for t in range(n):
        # 1. execute pending decision at this bar's open
        if pending and t > 0:
            side = pending["side"]
            if side == "buy" and pos is None:
                px = o[t] * (1 + slippage)
                if qty_mode == "fixed":
                    q = qty or 1
                elif qty_mode == "all_in":
                    q = math.floor(cash * 0.98 / px)
                else:
                    stop = pending.get("stop")
                    q = math.floor(cash * risk_pct / (px - stop)) if stop and px > stop else 0
                    q = min(q, math.floor(cash * 0.98 / px))
                if q > 0:
                    fee = _fee(q * px, "buy", segment, currency)
                    cash -= q * px + fee
                    pos = {"side": 1, "qty": q, "entry": px, "date": dates[t], "fee": fee, "bars": 0,
                           "stop": pending.get("stop") or (px * (1 - stop_pct) if stop_pct else None),
                           "target": pending.get("target") or (px * (1 + target_pct) if target_pct else None), "why": pending.get("why")}
                    marks.append({"t": str(dates[t].date()), "type": "buy", "px": round(px, 2)})
            elif side == "short" and pos is None and allow_short:
                px = o[t] * (1 - slippage); q = qty or 1
                fee = _fee(q * px, "sell", segment, currency)
                cash -= fee
                pos = {"side": -1, "qty": q, "entry": px, "date": dates[t], "fee": fee, "bars": 0, "stop": px * (1 + stop_pct) if stop_pct else None,
                       "target": px * (1 - target_pct) if target_pct else None, "why": pending.get("why")}
                marks.append({"t": str(dates[t].date()), "type": "short", "px": round(px, 2)})
            elif side == "exit" and pos is not None:
                px = o[t] * (1 - slippage) if pos["side"] == 1 else o[t] * (1 + slippage)
                cash, trades, pos = _close(cash, pos, px, dates[t], "signal exit", trades, segment, currency, marks)
            pending = None
        # 2. manage stops/targets on this bar
        if pos is not None:
            pos["bars"] += 1
            ex, why = None, None
            if pos["side"] == 1:
                if pos["stop"] and l[t] <= pos["stop"]:
                    ex, why = min(o[t], pos["stop"]) * (1 - slippage), "stop-loss"
                elif pos["target"] and h[t] >= pos["target"]:
                    ex, why = max(min(o[t], pos["target"]), pos["target"]) * (1 - slippage), "target"
            else:
                if pos["stop"] and h[t] >= pos["stop"]:
                    ex, why = max(o[t], pos["stop"]) * (1 + slippage), "stop-loss"
                elif pos["target"] and l[t] <= pos["target"]:
                    ex, why = pos["target"] * (1 + slippage), "target"
            if ex is None and max_hold and pos["bars"] >= max_hold:
                ex, why = c[t] * (1 - slippage * pos["side"]), "time exit"
            if ex is not None:
                cash, trades, pos = _close(cash, pos, ex, dates[t], why, trades, segment, currency, marks)
        # 3. decide on this bar's close for the next open
        if t >= warm:
            sig = strategy.signal(enriched.iloc[:t + 1], ctx_fn(enriched.iloc[:t + 1]) if ctx_fn else {})
            if sig["action"] == "BUY":
                if pos is None:
                    pending = {"side": "buy", "stop": sig.get("stop"), "target": sig.get("target"), "why": sig.get("why")}
                elif pos["side"] == -1:
                    pending = {"side": "exit"}
            elif sig["action"] == "SELL":
                if pos is not None and pos["side"] == 1:
                    pending = {"side": "exit"}
                elif pos is None and allow_short:
                    pending = {"side": "short", "why": sig.get("why")}
        # 4. mark to market
        if pos is None:
            mv = cash
        elif pos["side"] == 1:
            mv = cash + pos["qty"] * c[t]
        else:
            mv = cash + pos["qty"] * (pos["entry"] - c[t])   # short: margin/collateral not modelled, P&L only
        eq.append(mv)
    if pos is not None:  # close at last close for reporting
        cash, trades, pos = _close(cash, pos, c[-1], dates[-1], "end of test (open position closed at last close)", trades, segment, currency, marks)
        eq[-1] = cash
    equity = pd.Series(eq, index=dates)
    return {"equity": equity, "trades": trades, "marks": marks, "metrics": metrics(equity, trades, capital, df)}


def _close(cash, pos, px, date, why, trades, segment, currency, marks):
    q = pos["qty"]
    if pos["side"] == 1:
        fee = _fee(q * px, "sell", segment, currency)
        pnl = q * (px - pos["entry"]) - fee - pos["fee"]
        cash += q * px - fee
    else:
        fee = _fee(q * px, "buy", segment, currency)
        pnl = q * (pos["entry"] - px) - fee - pos["fee"]
        cash += pnl + pos["fee"]  # return P&L (entry fee already deducted)
    trades.append({"side": "long" if pos["side"] == 1 else "short", "qty": q, "entry_date": str(pos["date"].date()), "entry": round(pos["entry"], 2),
                   "exit_date": str(date.date()), "exit": round(px, 2), "bars": pos["bars"], "pnl": round(pnl, 2),
                   "pnl_pct": round(pnl / (q * pos["entry"]) * 100, 2), "reason": why, "why": pos.get("why")})
    marks.append({"t": str(date.date()), "type": "exit", "px": round(px, 2)})
    return cash, trades, None


def buy_and_hold(df, capital=200000.0, qty=None, segment="futures", slippage=0.0005, currency="INR"):
    o, c = df["Open"].values, df["Close"].values
    px = o[0] * (1 + slippage)
    q = qty or math.floor(capital * 0.98 / px)
    fee_in = _fee(q * px, "buy", segment, currency)
    cash = capital - q * px - fee_in
    eq = pd.Series(cash + q * c, index=df.index)
    out = c[-1] * (1 - slippage)
    fee_out = _fee(q * out, "sell", segment, currency)
    final = cash + q * out - fee_out
    eq.iloc[-1] = final
    tr = [{"side": "long", "qty": q, "entry_date": str(df.index[0].date()), "entry": round(px, 2), "exit_date": str(df.index[-1].date()),
           "exit": round(out, 2), "bars": len(df), "pnl": round(final - capital, 2), "pnl_pct": round((final - capital) / (q * px) * 100, 2), "reason": "hold"}]
    return {"equity": eq, "trades": tr, "marks": [], "metrics": metrics(eq, tr, capital, df)}


def metrics(equity, trades, capital, df=None, periods=252):
    r = equity.pct_change().dropna()
    years = max((equity.index[-1] - equity.index[0]).days / 365.25, 1e-9)
    final = float(equity.iloc[-1])
    total = final / capital - 1
    cagr = (final / capital) ** (1 / years) - 1 if final > 0 else -1.0
    dd = I.drawdown(equity)
    mdd = float(dd.min())
    vol = float(r.std() * math.sqrt(periods)) if len(r) > 1 else 0.0
    sharpe = float(r.mean() / r.std() * math.sqrt(periods)) if len(r) > 1 and r.std() > 0 else 0.0
    downside = r[r < 0].std()
    sortino = float(r.mean() / downside * math.sqrt(periods)) if len(r) > 1 and downside and downside > 0 else 0.0
    calmar = cagr / abs(mdd) if mdd < 0 else None
    pnls = [t["pnl"] for t in trades]
    wins = [p for p in pnls if p > 0]; losses = [p for p in pnls if p <= 0]
    gp, gl = sum(wins), -sum(losses)
    exposure = float((equity.diff().abs() > 0).mean()) if len(equity) > 1 else 0.0
    return {"start": str(equity.index[0].date()), "end": str(equity.index[-1].date()), "years": round(years, 2), "capital": capital,
            "final_equity": round(final, 2), "absolute_return_pct": round(total * 100, 2), "cagr_pct": round(cagr * 100, 2),
            "max_drawdown_pct": round(mdd * 100, 2), "annual_vol_pct": round(vol * 100, 2), "sharpe": round(sharpe, 2),
            "sortino": round(sortino, 2), "calmar": round(calmar, 2) if calmar is not None else None, "trades": len(trades),
            "win_rate_pct": round(len(wins) / len(trades) * 100, 1) if trades else None,
            "profit_factor": round(gp / gl, 2) if gl > 0 else None, "expectancy": round(sum(pnls) / len(trades), 2) if trades else None,
            "avg_win": round(gp / len(wins), 2) if wins else None, "avg_loss": round(-gl / len(losses), 2) if losses else None,
            "turnover_trades_per_year": round(len(trades) / years, 1), "exposure_pct": round(exposure * 100, 1),
            "fees_note": "Indian statutory charges + brokerage per fill; slippage applied; see costs.py verification status"}


def walk_forward(df, strategy_factory, param_grid, train_years=3, test_years=1, capital=200000.0, metric="cagr_pct", **kw):
    """Anchored-window walk-forward: pick best params on train window, evaluate on the next test window."""
    start = df.index[0]; end = df.index[-1]
    folds, t0 = [], start
    while True:
        t1 = t0 + pd.DateOffset(years=train_years); t2 = t1 + pd.DateOffset(years=test_years)
        if t1 >= end:
            break
        train = df[(df.index >= t0) & (df.index < t1)]; test = df[(df.index >= t1) & (df.index < t2)]
        if len(test) < 60 or len(train) < 250:
            break
        best, best_m = None, -1e18
        for p in param_grid:
            res = run(train, strategy_factory(**p), capital=capital, **kw)
            m = res["metrics"][metric] if res["metrics"][metric] is not None else -1e18
            if m > best_m:
                best, best_m = p, m
        oos = run(test, strategy_factory(**best), capital=capital, **kw)
        folds.append({"train": [str(train.index[0].date()), str(train.index[-1].date())], "test": [str(test.index[0].date()), str(test.index[-1].date())],
                      "best_params": best, "in_sample": {metric: best_m}, "out_of_sample": oos["metrics"]})
        t0 = t0 + pd.DateOffset(years=test_years)
    oos_cagrs = [f["out_of_sample"]["cagr_pct"] for f in folds]
    return {"folds": folds, "oos_mean_cagr_pct": round(float(np.mean(oos_cagrs)), 2) if folds else None,
            "oos_positive_folds": sum(1 for x in oos_cagrs if x > 0), "n_folds": len(folds),
            "note": "Parameters chosen on each train window only; test windows never seen during selection."}


def seasonality(df):
    """Monthly and weekday mean returns with t-test p-values, Holm-corrected for multiple testing."""
    from scipy import stats
    r = df["Close"].pct_change().dropna()
    out = {}
    for name, key in [("month", r.index.month), ("weekday", r.index.weekday)]:
        groups = r.groupby(key)
        rows = []
        for k, g in groups:
            t, p = stats.ttest_1samp(g, 0.0) if len(g) > 2 else (float("nan"), float("nan"))
            rows.append({"key": int(k), "n": int(len(g)), "mean_pct": round(float(g.mean() * 100), 3), "p_value": round(float(p), 4),
                         "positive_years_frac": round(float((g.groupby(g.index.year).sum() > 0).mean()), 2)})
        ps = [x["p_value"] for x in rows]
        order = np.argsort(ps); m = len(ps)
        adj = [None] * m; running = 0
        for rank, idx in enumerate(order):
            running = max(running, ps[idx] * (m - rank))
            adj[idx] = min(1.0, running)
        for x, a in zip(rows, adj):
            x["p_holm"] = round(float(a), 4); x["significant_5pct_after_correction"] = bool(a < 0.05)
        out[name] = rows
    out["note"] = "Daily returns grouped by calendar month / weekday. p-values Holm-corrected for 12 (or 5) tests. Nothing here is a trade trigger."
    return out
