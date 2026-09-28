"""
Orchestration: regime -> scan -> risk -> paper orders -> mark-to-market. Pure functions over
in-memory data plus one Ledger; all I/O happens in run.py / api.py.
"""
import math, datetime as dt
from decimal import Decimal
import pandas as pd
from . import config as C, indicators as I, dq, risk, simulator
from .strategies import get as get_strategy

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
D = Decimal


def regime(idx):
    d = I.enrich(idx)
    x = d.iloc[-1]
    vol = float(d["Close"].pct_change().tail(20).std() * math.sqrt(252) * 100)
    if x.Close > x.sma200 and x.sma50 > x.sma200:
        state, text = "BULL", "Index above its 200-day average and 50-day above 200-day: uptrend. New buys allowed."
    elif x.Close < x.sma200 and x.sma50 < x.sma200:
        state, text = "BEAR", "Index below its 200-day average and 50-day below 200-day: downtrend. No new buys; protect capital."
    else:
        state, text = "SIDEWAYS", "Averages disagree: no clear direction. Only the strongest buys, half size."
    sup, res = I.support_resistance(d)
    return {"state": state, "text": text, "close": round(float(x.Close), 2), "date": str(d.index[-1].date()),
            "sma50": round(float(x.sma50), 2), "sma200": round(float(x.sma200), 2), "rsi": round(float(x.rsi), 1),
            "vol20_annualised_pct": round(vol, 1), "high_vol": bool(vol > 25), "ret3m": float(d["Close"].iloc[-1] / d["Close"].iloc[-64] - 1),
            "support": sup, "resistance": res, "rule": "BULL: close>SMA200 and SMA50>SMA200; BEAR: both below; else SIDEWAYS",
            "computed_at": dt.datetime.now(IST).isoformat(timespec="seconds")}


def scan(data, idx, strategy_id="trend_breakout_swing", news_fn=None, sectors=None, limits=None):
    """data: {symbol: OHLCV DataFrame (clean)}. Returns the full signal table plus buy/sell/blocked lists."""
    S = get_strategy(strategy_id)
    reg = regime(idx)
    ctx = {"regime": reg["state"], "index_ret3m": reg["ret3m"]}
    Lm = {**C.RISK, **(limits or {})}
    rows, buys, sells, blocked, skipped = [], [], [], [], {}
    for sym, df in data.items():
        try:
            d = I.enrich(df)
            last = d.iloc[-1]
            if last.Close < Lm["min_price"]:
                skipped[sym] = f"price {last.Close:.2f} below minimum {Lm['min_price']}"; continue
            if not (last.turnover_cr == last.turnover_cr) or last.turnover_cr < Lm["min_turnover_cr"]:
                skipped[sym] = f"20-day turnover {0 if last.turnover_cr != last.turnover_cr else last.turnover_cr:.1f} (x1e7) below {Lm['min_turnover_cr']}"; continue
            sig = S.signal(d, ctx)
        except Exception as e:  # noqa
            skipped[sym] = f"error: {e}"; continue
        sig.update(symbol=sym, bar=str(d.index[-1].date()), strategy=strategy_id, version=S.version,
                   sector=(sectors or {}).get(sym), vol20=float(last.vol20) if last.vol20 == last.vol20 else None,
                   signal_id=f"{strategy_id}|{sym}|{d.index[-1].date()}")
        rows.append(sig)
        if sig["action"] == "BUY":
            why_block = S.regime_block("BUY", sig["score"], reg["state"])
            if why_block:
                blocked.append({"symbol": sym, "score": sig["score"], "reason": why_block, "why": sig["why"], "entry": sig["entry"]})
            else:
                buys.append(sig)
        elif sig["action"] == "SELL":
            sells.append(sig)
    buys = sorted(buys, key=lambda s: -s["score"])[:C.SIGNALS["max_picks"]]
    sells = sorted(sells, key=lambda s: s["score"])[:C.SIGNALS["max_picks"]]
    if news_fn:
        for s in buys + sells:
            items, risky, st = news_fn(s["symbol"])
            s["news"], s["news_risk"], s["news_status"] = items, risky, st
        risky = [s for s in buys if s.get("news_risk")]
        for s in risky:
            blocked.append({"symbol": s["symbol"], "score": s["score"], "reason": "news-risk word in a headline from the last 3 days; check before acting", "why": s["why"], "entry": s["entry"]})
        buys = [s for s in buys if not s.get("news_risk")]
    strong = sorted([r for r in rows if r["action"] != "NONE" or abs(r["score"]) >= 3], key=lambda r: -abs(r["score"]))
    return {"regime": reg, "strategy": S.spec(), "buys": buys, "sells": sells, "blocked": blocked, "skipped": skipped,
            "table": strong[:200], "scanned": len(rows), "universe": len(data)}


def paper_run(ledger, account_id, data, bar_date, buys, reg, strategy_id, data_status="OK", sectors=None, lots=None):
    """One daily step for one account. Returns list of action strings. Never touches a broker."""
    a = ledger.account(account_id)
    acts, S = [], get_strategy(strategy_id)
    # 1. bring pending orders and open positions up to date with today's bar
    syms = set(a["positions"].keys()) | {o["symbol"] for o in ledger.pending(account_id)}
    for sym in sorted(syms):
        df = data.get(sym)
        if df is None:
            acts.append(f"{sym}: no data today - untouched"); continue
        ts = [t for t in df.index if str(t.date()) == str(bar_date)]
        if not ts:
            acts.append(f"{sym}: no bar for {bar_date}"); continue
        acts += simulator.apply_bar(ledger, account_id, sym, df.loc[ts[0]], bar_date)
    # 2. time / regime exits decided on today's close -> next open
    for sym, pos in list(a["positions"].items()):
        why = None
        if pos.get("strategy") == strategy_id and int(pos.get("bars_held", 0)) >= C.SIGNALS["max_hold_days"]:
            why = f"time exit ({C.SIGNALS['max_hold_days']} bars)"
        elif pos.get("strategy") == strategy_id and reg["state"] == "BEAR":
            why = "index regime turned BEAR"
        if why and not any(o["symbol"] == sym and o["side"] == "sell" for o in ledger.pending(account_id)):
            o, st = ledger.submit(account_id, sym, "sell", pos["qty"], "MARKET", bar_date=bar_date, strategy=strategy_id, reason=why,
                                  segment=pos.get("segment", "delivery"), currency=a["currency"])
            if st == "ok":
                acts.append(f"Queued EXIT {sym} at next open - {why}")
    # 3. new entries through the risk gate
    summ = ledger.summary(account_id)
    equity = D(summ["equity"])
    todays = sum(D(t["pnl"]) for t in ledger.state["trades"] if t["account"] == account_id and t["exit_date"] == str(bar_date))
    orders_today = 0
    sector_exp = {}
    for p in a["positions"].values():
        if p.get("sector"):
            sector_exp[p["sector"]] = sector_exp.get(p["sector"], D("0")) + D(p["last"]) * D(p["qty"])
    size_mult = D("0.5") if reg.get("high_vol") else D("1")
    for s in buys:
        if strategy_id not in a["strategies"]:
            break
        qty, note = risk.size_position(equity * size_mult, s["entry"], s["stop"], C.RISK["max_risk_per_trade"], C.RISK["max_position_pct"],
                                       s.get("vol20"), C.RISK["max_volume_share"], (lots or {}).get(s["symbol"], 1))
        order = {"symbol": s["symbol"], "side": "buy", "qty": qty, "price": s["entry"], "stop": s["stop"], "sector": s.get("sector")}
        dec = risk.gate(order, summ, a["positions"], data_status=data_status, todays_realized=todays, orders_today=orders_today, sector_exposure=sector_exp)
        if not dec["approved"]:
            ledger.reject(account_id, s["symbol"], "buy", qty, dec["reason"], strategy_id, bar_date, risk=dec)
            acts.append(f"Risk engine REJECTED {s['symbol']}: {dec['reason']}"); continue
        o, st = ledger.submit(account_id, s["symbol"], "buy", qty, "MARKET", bar_date=bar_date, strategy=strategy_id, signal_id=s["signal_id"],
                              reason="; ".join(s["why"][:2]), stop=s["stop"], target=s["target"], risk=dec, sector=s.get("sector"), currency=a["currency"],
                              meta={"score": s["score"], "sizing_note": note, "size_mult": str(size_mult)})
        if st == "ok":
            orders_today += 1
            sector_exp[s.get("sector")] = sector_exp.get(s.get("sector"), D("0")) + D(str(qty)) * D(str(s["entry"]))
            summ = ledger.summary(account_id)
            acts.append(f"Queued BUY {qty} {s['symbol']} at next open (stop {s['stop']}, target {s['target']}; {note})")
        else:
            acts.append(f"{s['symbol']}: duplicate order blocked")
    # 4. mark to market
    prices = {}
    for sym in a["positions"]:
        df = data.get(sym)
        if df is not None:
            prices[sym] = float(df["Close"].iloc[-1])
    ledger.mark(account_id, prices, bar_date)
    if not acts:
        acts.append("Nothing to do: no fills, no exits, no new entries.")
    return acts


def mark_only(ledger, account_id, prices, bar_date):
    """Intraday: refresh position marks with latest quotes (no trading decisions)."""
    ledger.mark(account_id, prices, bar_date)
