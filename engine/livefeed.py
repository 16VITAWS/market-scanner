"""
Live session loop (runs inside GitHub Actions during market hours).

Every cycle (about every 2 minutes):
  1. quotes  - 5-minute bars for indices / FX / commodities / held symbols and today's running daily bar for every stock in
               the NSE universe (and the US universe while the US market is open). Free feed => ~15 minutes behind the exchange.
               Each quote carries the time of its last bar so the delay is always visible.
  2. paper   - LIVE paper trading: pending paper orders decided on an earlier session fill at today's open, stop-losses and
               targets trigger on today's range so far, option spreads exit when their modeled value hits take-profit / stop.
               Same rules as the end-of-day run (simulator.apply_bar), just applied during the session.
  3. publish - small JSON files (quotes, paper, snapshot, notifications, live status) pushed to the `live-data` branch, which
               the portal reads every minute. The full site is still published at the end of each run.
"""
import os, json, time, shutil, tempfile, subprocess, datetime as dt
from decimal import Decimal as D
import pandas as pd

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
LIVE_FILES = ("quotes.json", "paper.json", "snapshot.json", "notifications.json", "live_status.json", "signals.json")


def quote_from_5m(df, old=None):
    """Quote from 5-minute bars: last price, previous SESSION close (not a stale stored value), today's O/H/L/volume."""
    day = df.index[-1].date()
    today = df[df.index.date == day]
    before = df[df.index.date < day]
    last = float(df["Close"].iloc[-1])
    if len(before):
        prev = float(before["Close"].iloc[-1])
    elif old and old.get("d") and old["d"] < str(day):
        prev = old.get("p")
    else:
        prev = (old or {}).get("pc")
    return {"p": round(last, 2), "pc": round(prev, 2) if prev else None, "chg_pct": round((last / prev - 1) * 100, 2) if prev else None,
            "t": df.index[-1].isoformat(), "d": str(day), "status": "DELAYED", "intraday": True, "age_days": 0,
            "day_o": round(float(today["Open"].iloc[0]), 2), "day_h": round(float(today["High"].max()), 2), "day_l": round(float(today["Low"].min()), 2),
            "day_v": float(today["Volume"].sum())}


def quote_from_daily(df, old=None):
    """Quote from daily bars whose last row is today's running bar."""
    last_row = df.iloc[-1]
    day = df.index[-1].date()
    prev = float(df["Close"].iloc[-2]) if len(df) > 1 else (old or {}).get("pc")
    last = float(last_row["Close"])
    return {"p": round(last, 2), "pc": round(prev, 2) if prev else None, "chg_pct": round((last / prev - 1) * 100, 2) if prev else None,
            "t": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"), "d": str(day), "status": "DELAYED", "intraday": True, "age_days": 0,
            "day_o": round(float(last_row["Open"]), 2), "day_h": round(float(last_row["High"]), 2), "day_l": round(float(last_row["Low"]), 2),
            "day_v": float(last_row["Volume"] or 0)}


def session_bar(q):
    """Today-so-far bar for the simulator."""
    return {"Open": q["day_o"], "High": q["day_h"], "Low": q["day_l"], "Close": q["p"], "Volume": max(q.get("day_v") or 0, 1)}


def paper_live(ledger, quotes, open_markets, simulator, today):
    """Apply today's running bar to every paper account's pending orders and open positions (stocks / ETFs)."""
    acts = {}
    for aid, a in ledger.state["accounts"].items():
        mkt = "US" if a.get("currency") == "USD" else "NSE"
        if mkt not in open_markets:
            continue
        syms = {s for s, p in a["positions"].items() if not p.get("spread")} | {o["symbol"] for o in ledger.pending(aid) if o.get("segment") != "options"}
        for s in sorted(syms):
            q = quotes.get(s)
            if not q or not q.get("intraday") or q.get("d") != str(today) or q.get("day_o") is None:
                continue
            got = simulator.apply_bar(ledger, aid, s, session_bar(q), str(today), intraday=True)
            if got:
                acts.setdefault(aid, []).extend(got)
    return acts


def options_live(ledger, account_id, quotes, options_mod, today, bar_date):
    """Exit option spreads during the session when the MODELED value reaches take-profit or stop."""
    if account_id not in ledger.state["accounts"] or not quotes.get("NIFTY", {}).get("intraday"):
        return []
    a = ledger.account(account_id)
    S = quotes["NIFTY"]["p"]
    vix = quotes.get("INDIAVIX", {}).get("p")
    sigma = (vix / 100) if vix else 0.14
    acts = []
    for sym, pos in list(a["positions"].items()):
        sp = pos.get("spread")
        if not sp:
            continue
        val, _, _ = options_mod.spread_value(sp, S, today, sigma)
        pos["last"] = str(round(val, 2))
        debit = float(pos["avg_price"]); maxp = sp["width"] - debit
        why = None
        if val - debit >= options_mod.PARAMS["take_profit"] * maxp:
            why = f"take-profit (live): modeled value {val:.2f} >= debit + 50% of max profit"
        elif val <= debit * (1 - options_mod.PARAMS["stop_loss"]):
            why = f"stop (live): modeled value {val:.2f} <= 50% of debit {debit:.2f}"
        if why:
            qty = D(pos["qty"])
            fee, _ = options_mod.spread_charges(float(qty) * val, "sell")
            o, st = ledger.submit(account_id, sym, "sell", qty, "MARKET", bar_date=None, strategy="index_options_regime", reason=why,
                                  dedupe_key=f"{account_id}|{sym}|exit|{bar_date}", segment="options")
            if st == "ok":
                f = ledger.fill(o, D(str(round(val, 2))), qty, str(bar_date), note=why + " (MODELED price, live session)", fill_rule="modeled_bs_live", fee_override=fee)
                if f:
                    acts.append(f"EXIT {sym} x{int(qty)} @ {val:.2f} - {why}")
    return acts


def publish(site_path, token, repo, branch="live-data"):
    """Push the small live files to an orphan branch (history never grows)."""
    if not token or not repo:
        return "skipped (no token in this environment)"
    tmp = tempfile.mkdtemp()
    try:
        os.makedirs(os.path.join(tmp, "api"))
        for f in LIVE_FILES:
            src = os.path.join(site_path, "api", f)
            if os.path.exists(src):
                shutil.copy(src, os.path.join(tmp, "api", f))
        run = lambda *c: subprocess.run(c, cwd=tmp, check=True, capture_output=True, text=True)
        run("git", "init", "-q"); run("git", "checkout", "-q", "-b", branch)
        run("git", "-c", "user.name=vision-ai-bot", "-c", "user.email=vision-ai-bot@users.noreply.github.com", "add", "-A")
        run("git", "-c", "user.name=vision-ai-bot", "-c", "user.email=vision-ai-bot@users.noreply.github.com", "commit", "-q", "-m",
            "live " + dt.datetime.now(dt.timezone.utc).strftime("%FT%TZ"))
        run("git", "push", "-q", "-f", f"https://x-access-token:{token}@github.com/{repo}.git", f"HEAD:{branch}")
        return "published"
    except Exception as e:  # noqa
        return f"publish failed: {str(e)[:120]}"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
