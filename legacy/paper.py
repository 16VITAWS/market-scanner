"""
Automatic paper trading. Runs every evening after the scan.
- Orders decided today are executed at TOMORROW's opening price (no cheating with today's close)
- Slippage 0.1% against you, full delivery charges, stop-loss, target, time exit, market-mood exit
- Everything stored in data/portfolio.json and data/trades.csv
"""
import os, json, datetime as dt
import pandas as pd
import config as C

HERE = os.path.dirname(os.path.abspath(__file__))
PF = os.path.join(HERE, "data", "portfolio.json")
TRADES = os.path.join(HERE, "data", "trades.csv")
SLIP = 0.001            # 0.1% worse than the open, both ways
MAX_POS = 5
MAX_DAYS = 20           # exit after 20 trading days if neither stop nor target hit


def charges(value, side):
    brk = min(20, value * 0.0005)
    stt = value * 0.001                                  # delivery, both sides
    stamp = value * 0.00015 if side == "buy" else 0
    exch, sebi = value * 0.0000297, value * 0.000001
    dp = 15.93 if side == "sell" else 0                  # typical DP charge on a delivery sell
    return round(brk + stt + stamp + exch + sebi + dp + 0.18 * (brk + exch + sebi), 2)


def load():
    if os.path.exists(PF):
        with open(PF) as f:
            return json.load(f)
    return {"cash": C.CAPITAL, "start": C.CAPITAL, "positions": [], "pending": [], "equity": [], "log": []}


def save(pf):
    os.makedirs(os.path.dirname(PF), exist_ok=True)
    with open(PF, "w") as f:
        json.dump(pf, f, indent=1)


def record(row):
    df = pd.DataFrame([row])
    df.to_csv(TRADES, mode="a", header=not os.path.exists(TRADES), index=False)


def bar_on(df, date):
    """The bar for `date` if the data has it, else None."""
    d = pd.Timestamp(date)
    return df.loc[d] if d in df.index else None


def run(data, mood, picks, scan_date):
    """data: {symbol: OHLCV DataFrame}; mood: market mood dict; picks: today's BUY list."""
    pf = load()
    today = pd.Timestamp(scan_date)
    actions = []

    # 1. execute yesterday's pending orders at today's open
    still = []
    for o in pf["pending"]:
        df = data.get(o["symbol"])
        bar = bar_on(df, today) if df is not None else None
        if bar is None:
            still.append(o) if (today - pd.Timestamp(o["date"])).days < 4 else actions.append(f"Cancelled {o['symbol']}: no data")
            continue
        if o["side"] == "buy":
            px = round(float(bar.Open) * (1 + SLIP), 2)
            qty = min(o["qty"], int(pf["cash"] * 0.95 // px))
            if "vol20" in bar and bar.get("vol20"):
                qty = min(qty, int(bar["vol20"] * getattr(C, "MAX_VOLUME_SHARE", 0.05)))
            if qty <= 0 or len(pf["positions"]) >= MAX_POS:
                actions.append(f"Skipped {o['symbol']}: no cash or too many positions"); continue
            cost = charges(qty * px, "buy")
            pf["cash"] -= qty * px + cost
            pf["positions"].append({"symbol": o["symbol"], "qty": qty, "entry": px, "entry_date": str(today.date()),
                                    "stop": o["stop"], "target": o["target"], "cost": cost, "days": 0, "reason": o["reason"]})
            actions.append(f"BOUGHT {qty} {o['symbol']} at ₹{px:,} (stop ₹{o['stop']:,}, target ₹{o['target']:,})")
        else:
            pos = next((p for p in pf["positions"] if p["symbol"] == o["symbol"]), None)
            if pos:
                px = round(float(bar.Open) * (1 - SLIP), 2)
                close_position(pf, pos, px, today, o["reason"], actions)
    pf["pending"] = still

    # 2. manage open positions with today's bar
    for pos in list(pf["positions"]):
        df = data.get(pos["symbol"]); bar = bar_on(df, today) if df is not None else None
        if bar is None:
            continue
        pos["days"] += 1
        pos["last"] = round(float(bar.Close), 2)
        if bar.Low <= pos["stop"]:
            px = round(min(float(bar.Open), pos["stop"]) * (1 - SLIP), 2)   # gap-down fills worse
            close_position(pf, pos, px, today, "stop-loss hit", actions)
        elif bar.High >= pos["target"]:
            px = round(max(float(bar.Open), pos["target"]) * (1 - SLIP), 2) if bar.Open > pos["target"] else round(pos["target"] * (1 - SLIP), 2)
            close_position(pf, pos, px, today, "target reached", actions)
        elif pos["days"] >= MAX_DAYS:
            pf["pending"].append({"symbol": pos["symbol"], "side": "sell", "reason": "time exit (20 days)", "date": str(today.date())})
        elif mood["state"] == "BEAR":
            pf["pending"].append({"symbol": pos["symbol"], "side": "sell", "reason": "market turned bearish", "date": str(today.date())})
        elif pos["last"] > pos["entry"] * 1.05:                             # trail the stop once 5% up
            pos["stop"] = max(pos["stop"], round(pos["last"] * 0.96, 2))

    # 3. queue today's buys for tomorrow's open
    held = {p["symbol"] for p in pf["positions"]} | {o["symbol"] for o in pf["pending"]}
    room = MAX_POS - len(pf["positions"]) - sum(1 for o in pf["pending"] if o["side"] == "buy")
    for p in picks:
        if room <= 0: break
        if p["symbol"] in held: continue
        pf["pending"].append({"symbol": p["symbol"], "side": "buy", "qty": p["qty"], "stop": p["stop"],
                              "target": p["target"], "reason": "; ".join(p["why"][:2]), "date": str(today.date())})
        actions.append(f"Order queued: buy {p['qty']} {p['symbol']} at tomorrow's open")
        room -= 1

    # 4. mark to market
    value = pf["cash"] + sum(p["qty"] * p.get("last", p["entry"]) for p in pf["positions"])
    pf["equity"].append({"date": str(today.date()), "value": round(value, 2)})
    pf["log"] = ([{"date": str(today.date()), "actions": actions}] + pf["log"])[:30]
    save(pf)
    return pf, actions


def close_position(pf, pos, px, today, reason, actions):
    value = pos["qty"] * px
    cost = charges(value, "sell")
    pnl = round(value - cost - pos["qty"] * pos["entry"] - pos["cost"], 2)
    pf["cash"] += value - cost
    pf["positions"].remove(pos)
    record({"symbol": pos["symbol"], "qty": pos["qty"], "entry_date": pos["entry_date"], "entry": pos["entry"],
            "exit_date": str(today.date()), "exit": px, "reason": reason, "pnl": pnl,
            "pnl_pct": round(pnl / (pos["qty"] * pos["entry"]) * 100, 2)})
    actions.append(f"SOLD {pos['qty']} {pos['symbol']} at ₹{px:,} — {reason} — {'profit' if pnl >= 0 else 'loss'} ₹{abs(pnl):,}")


def stats():
    if not os.path.exists(TRADES):
        return None
    t = pd.read_csv(TRADES)
    w = t[t.pnl > 0]; l = t[t.pnl <= 0]
    return {"n": len(t), "wins": len(w), "win_rate": round(len(w) / len(t) * 100, 1) if len(t) else 0,
            "net": round(float(t.pnl.sum()), 2), "avg_win": round(float(w.pnl.mean()), 2) if len(w) else 0,
            "avg_loss": round(float(l.pnl.mean()), 2) if len(l) else 0,
            "pf": round(float(w.pnl.sum() / -l.pnl.sum()), 2) if len(l) and l.pnl.sum() < 0 else None}
