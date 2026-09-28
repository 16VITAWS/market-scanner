"""
Index-options paper engine.

WHAT IS REAL AND WHAT IS MODELED
- The underlying (NIFTY / BANKNIFTY close) is real market data from the feed.
- Option PRICES here are MODELED with Black-Scholes (European, no dividends) using India VIX
  (NIFTY) or 20-day realised volatility x 1.15 (others) as the volatility input. They are NOT
  exchange quotes. Every file and screen that uses them is labelled MODELED.
- When a real option-chain feed is connected (Angel One key or NSE chain), fills switch to the
  quoted premium; until then options paper results are an approximation and are labelled so.

STRATEGY (index_options_regime v1.0.0, paper only, defined risk only)
- BEAR regime and NIFTY close < SMA50 and RSI(14) < 45  -> buy a PUT debit spread
  (buy ATM put, sell put `width` points lower), nearest expiry with >= min_dte days left.
- BULL regime and close > previous 20-day high           -> buy a CALL debit spread.
- SIDEWAYS or no trigger                                 -> no trade ("no trade" is a valid answer).
- Size: max loss (net debit x units + charges) <= 3% of account equity (defined, known-in-advance loss);
  stocks keep the 1% rule. Whole lots only; if one lot exceeds the cap, no trade.
- Exit: +50% of max profit, -50% of debit, 2 days before expiry, or regime flips against it.
- Never sells naked options; never more than max_open spreads.
"""
import math, datetime as dt
from decimal import Decimal
import numpy as np
import pandas as pd
from . import indicators as I, costs

D = Decimal
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))

# Lot sizes and expiry rules: as understood on 2026-09-28 after NSE's lot-size revision; VERIFY on nseindia.com.
CONTRACTS = {
    "NIFTY":     {"lot": 65, "step": 50,  "width": 200, "weekly": True,  "expiry_weekday": 1, "vol_from": "INDIAVIX"},   # Tuesday
    "BANKNIFTY": {"lot": 30, "step": 100, "width": 500, "weekly": False, "expiry_weekday": 1, "vol_from": "realised"},   # last Tuesday of month
}
CONTRACT_NOTE = "Lot sizes and expiry weekday as understood on 2026-09-28 (NSE revisions 2025); marked UNVERIFIED - check nseindia.com before relying on them."
PARAMS = {"risk_pct": 0.03, "min_dte": 5, "take_profit": 0.5, "stop_loss": 0.5, "exit_dte": 2, "max_open": 2, "rsi_bear": 45, "r": 0.065}


# ---------------------------------------------------------------- Black-Scholes
def _ncdf(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def _npdf(x):
    return math.exp(-0.5 * x * x) / math.sqrt(2 * math.pi)


def bs(S, K, T, r, sigma, kind):
    """Price and greeks. T in years. kind 'C' or 'P'. Returns dict."""
    if T <= 0 or sigma <= 0:
        intrinsic = max(0.0, S - K) if kind == "C" else max(0.0, K - S)
        return {"price": intrinsic, "delta": (1.0 if S > K else 0.0) if kind == "C" else (-1.0 if S < K else 0.0), "gamma": 0.0, "theta": 0.0, "vega": 0.0}
    d1 = (math.log(S / K) + (r + 0.5 * sigma * sigma) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    if kind == "C":
        price = S * _ncdf(d1) - K * math.exp(-r * T) * _ncdf(d2)
        delta = _ncdf(d1)
        theta = (-S * _npdf(d1) * sigma / (2 * math.sqrt(T)) - r * K * math.exp(-r * T) * _ncdf(d2)) / 365
    else:
        price = K * math.exp(-r * T) * _ncdf(-d2) - S * _ncdf(-d1)
        delta = _ncdf(d1) - 1
        theta = (-S * _npdf(d1) * sigma / (2 * math.sqrt(T)) + r * K * math.exp(-r * T) * _ncdf(-d2)) / 365
    return {"price": max(price, 0.05), "delta": delta, "gamma": _npdf(d1) / (S * sigma * math.sqrt(T)),
            "theta": theta, "vega": S * _npdf(d1) * math.sqrt(T) / 100}


# ---------------------------------------------------------------- expiries
def expiries(underlying, today, n=4, holidays=None):
    """Next n expiry dates. If an expiry falls on a holiday, move to the previous trading day."""
    c = CONTRACTS[underlying]
    holidays = set(holidays or [])
    out, d = [], today
    while len(out) < n and (d - today).days < 200:
        if d.weekday() == c["expiry_weekday"]:
            ok = c["weekly"] or (d + dt.timedelta(days=7)).month != d.month      # monthly = last such weekday
            if ok:
                e = d
                while e.isoformat() in holidays or e.weekday() > 4:
                    e -= dt.timedelta(days=1)
                if e >= today:
                    out.append(e)
        d += dt.timedelta(days=1)
    return out


def vol_input(underlying, frames):
    c = CONTRACTS[underlying]
    if c["vol_from"] == "INDIAVIX" and "INDIAVIX" in frames and len(frames["INDIAVIX"]):
        v = float(frames["INDIAVIX"]["Close"].iloc[-1]) / 100
        return v, f"India VIX {v*100:.2f} ({frames['INDIAVIX'].index[-1].date()})"
    s = frames[underlying]["Close"]
    rv = float(s.pct_change().tail(20).std() * math.sqrt(252))
    v = max(0.10, rv * 1.15)
    return v, f"20-day realised vol {rv*100:.1f}% x 1.15" + (" (raised to the 10% floor)" if rv * 1.15 < 0.10 else "")


def model_chain(underlying, frames, today, holidays=None, strikes_each_side=10):
    S = float(frames[underlying]["Close"].iloc[-1])
    asof = str(frames[underlying].index[-1].date())
    c = CONTRACTS[underlying]
    sigma, src = vol_input(underlying, frames)
    exps = expiries(underlying, today, 3, holidays)
    atm = round(S / c["step"]) * c["step"]
    chains = []
    for e in exps:
        T = max((e - today).days, 0.5) / 365
        rows = []
        for i in range(-strikes_each_side, strikes_each_side + 1):
            K = atm + i * c["step"]
            cc, pp = bs(S, K, T, PARAMS["r"], sigma, "C"), bs(S, K, T, PARAMS["r"], sigma, "P")
            rows.append({"strike": K, "c": {k: round(v, 4 if k in ("delta", "gamma") else 2) for k, v in cc.items()},
                         "p": {k: round(v, 4 if k in ("delta", "gamma") else 2) for k, v in pp.items()}})
        chains.append({"expiry": e.isoformat(), "dte": (e - today).days, "rows": rows})
    return {"underlying": underlying, "spot": round(S, 2), "spot_date": asof, "atm": atm, "sigma": round(sigma, 4), "sigma_source": src,
            "rate": PARAMS["r"], "lot": c["lot"], "step": c["step"], "chains": chains, "status": "MODELED",
            "note": "Black-Scholes prices from the real underlying close and the volatility shown. Not exchange quotes. " + CONTRACT_NOTE}


# ---------------------------------------------------------------- spreads
def spread_value(spread, S, today, sigma):
    """Current modeled value per unit of a debit spread."""
    e = dt.date.fromisoformat(spread["expiry"])
    T = max((e - today).days, 0) / 365
    k = spread["kind"]
    long_ = bs(S, spread["k_long"], T, PARAMS["r"], sigma, k)["price"]
    short = bs(S, spread["k_short"], T, PARAMS["r"], sigma, k)["price"]
    return max(0.0, long_ - short), long_, short


def spread_charges(premium_gross, side):
    """Charges for a 2-leg options trade: 2 orders' brokerage + statutory on total premium turnover."""
    ch = costs.india_charges(premium_gross, side, "options", "groww")
    extra_brokerage = D("20") * D("1.18")          # second leg's brokerage + GST
    return D(ch["total"]) + extra_brokerage, ch


def signal(frames, reg, today, holidays=None, underlying="NIFTY"):
    """Returns a proposed spread dict or a 'no trade' dict with the reason."""
    df = frames.get(underlying)
    if df is None or len(df) < 60:
        return {"action": "NONE", "why": [f"{underlying}: not enough data"]}
    d = I.enrich(df)
    x = d.iloc[-1]
    S = float(x.Close)
    c = CONTRACTS[underlying]
    sigma, src = vol_input(underlying, frames)
    why, kind = [], None
    if reg["state"] == "BEAR" and x.Close < x.sma50 and x.rsi < PARAMS["rsi_bear"]:
        kind = "P"; why = [f"Regime BEAR", f"Close {S:,.0f} < SMA50 {x.sma50:,.0f}", f"RSI {x.rsi:.0f} < {PARAMS['rsi_bear']}"]
    elif reg["state"] == "BULL" and x.Close > x.hi20:
        kind = "C"; why = [f"Regime BULL", f"Close {S:,.0f} broke previous 20-day high {x.hi20:,.0f}"]
    else:
        return {"action": "NONE", "why": [f"Regime {reg['state']}; no options trigger (bear: close<SMA50 and RSI<{PARAMS['rsi_bear']}; bull: 20-day breakout)"],
                "spot": S, "sigma": sigma}
    exps = [e for e in expiries(underlying, today, 6, holidays) if (e - today).days >= PARAMS["min_dte"]]
    if not exps:
        return {"action": "NONE", "why": ["no expiry with enough days left"]}
    e = exps[0]
    atm = round(S / c["step"]) * c["step"]
    k_long = atm
    k_short = atm - c["width"] if kind == "P" else atm + c["width"]
    T = (e - today).days / 365
    long_ = bs(S, k_long, T, PARAMS["r"], sigma, kind)["price"]
    short = bs(S, k_short, T, PARAMS["r"], sigma, kind)["price"]
    debit = long_ - short
    name = f"{underlying} {e.strftime('%d%b%y').upper()} {int(k_long)}/{int(k_short)} {'PE' if kind == 'P' else 'CE'} DEBIT SPREAD"
    return {"action": "BUY", "name": name, "underlying": underlying, "kind": kind, "expiry": e.isoformat(), "dte": (e - today).days,
            "k_long": k_long, "k_short": k_short, "width": c["width"], "debit": round(debit, 2), "long_px": round(long_, 2), "short_px": round(short, 2),
            "max_profit": round(c["width"] - debit, 2), "max_loss": round(debit, 2), "lot": c["lot"], "spot": S, "sigma": sigma, "sigma_source": src,
            "breakeven": round(k_long - debit if kind == "P" else k_long + debit, 2), "why": why, "pricing": "MODELED (Black-Scholes)"}


def run(ledger, account_id, frames, reg, today, bar_date, holidays=None):
    """Daily step for the options paper account: manage exits, then consider one new entry."""
    a = ledger.account(account_id)
    acts = []
    und = "NIFTY"
    if und not in frames:
        return ["No NIFTY data - options engine idle."], None
    S = float(frames[und]["Close"].iloc[-1])
    sigma, _ = vol_input(und, frames)
    # exits
    for sym, pos in list(a["positions"].items()):
        sp = pos.get("spread")
        if not sp:
            continue
        val, _, _ = spread_value(sp, S, today, sigma)
        pos["last"] = str(round(val, 2)); pos["high_water"] = str(max(D(pos["high_water"]), D(str(round(val, 2)))))
        debit = float(pos["avg_price"]); maxp = sp["width"] - debit
        dte = (dt.date.fromisoformat(sp["expiry"]) - today).days
        why = None
        if val - debit >= PARAMS["take_profit"] * maxp:
            why = f"take-profit: value {val:.2f} >= debit + 50% of max profit"
        elif val <= debit * (1 - PARAMS["stop_loss"]):
            why = f"stop: value {val:.2f} <= 50% of debit {debit:.2f}"
        elif dte <= PARAMS["exit_dte"]:
            why = f"time exit: {dte} days to expiry"
        elif (sp["kind"] == "P" and reg["state"] == "BULL") or (sp["kind"] == "C" and reg["state"] == "BEAR"):
            why = f"regime flipped to {reg['state']}"
        if why:
            qty = D(pos["qty"])
            fee, _ = spread_charges(float(qty) * (val + 2 * 0.0), "sell")
            o, _ = ledger.submit(account_id, sym, "sell", qty, "MARKET", bar_date=None, strategy="index_options_regime", reason=why,
                                 dedupe_key=f"{account_id}|{sym}|exit|{bar_date}", segment="options")
            f = ledger.fill(o, D(str(round(val, 2))), qty, bar_date, note=why + " (MODELED price)", fill_rule="modeled_bs_close", fee_override=fee)
            if f:
                acts.append(f"EXIT {sym} x{int(qty)} @ {val:.2f} - {why}")
        else:
            acts.append(f"HOLD {sym}: modeled value {val:.2f} vs debit {debit:.2f}, {dte} days left")
    # entry
    sig = signal(frames, reg, today, holidays, und)
    open_spreads = [p for p in a["positions"].values() if p.get("spread")]
    if sig["action"] == "BUY":
        if len(open_spreads) >= PARAMS["max_open"]:
            acts.append(f"Signal {sig['name']} skipped: {PARAMS['max_open']} spreads already open")
        elif sig["name"] in a["positions"]:
            acts.append(f"Already holding {sig['name']}")
        else:
            eq = ledger.equity(account_id)
            per_lot = D(str(sig["debit"])) * sig["lot"]
            fee1, _ = spread_charges(float(per_lot) + 2 * sig["short_px"] * sig["lot"], "buy")
            risk_budget = eq * D(str(PARAMS["risk_pct"]))
            lots = int(risk_budget // (per_lot + fee1 * 2)) if per_lot > 0 else 0
            if lots < 1:
                acts.append(f"Signal {sig['name']}: one lot risks ₹{float(per_lot + fee1*2):,.0f} > 1% budget ₹{float(risk_budget):,.0f} - NOT traded")
            else:
                qty = lots * sig["lot"]
                gross = (sig["long_px"] + sig["short_px"]) * qty
                fee, ch = spread_charges(gross, "buy")
                o, st = ledger.submit(account_id, sig["name"], "buy", qty, "MARKET", bar_date=None, strategy="index_options_regime",
                                      reason="; ".join(sig["why"]), stop=None, target=None, dedupe_key=f"{account_id}|{sig['name']}|{bar_date}",
                                      segment="options", meta={"spread": sig})
                if st == "ok":
                    f = ledger.fill(o, D(str(sig["debit"])), qty, bar_date, note="MODELED debit at today's close (no option quote feed)",
                                    fill_rule="modeled_bs_close", fee_override=fee)
                    if f:
                        pos = a["positions"][sig["name"]]
                        pos["spread"] = {k: sig[k] for k in ("underlying", "kind", "expiry", "k_long", "k_short", "width", "lot")}
                        pos["max_loss"] = str(round(sig["debit"] * qty + float(fee) * 2, 2))
                        pos["max_profit"] = str(round((sig["width"] - sig["debit"]) * qty - float(fee) * 2, 2))
                        acts.append(f"BOUGHT {lots} lot(s) {sig['name']} @ {sig['debit']:.2f} (max loss ₹{float(pos['max_loss']):,.0f}, max profit ₹{float(pos['max_profit']):,.0f})")
    else:
        acts.append("No new options trade: " + "; ".join(sig["why"]))
    # mark
    prices = {}
    for sym, pos in a["positions"].items():
        if pos.get("spread"):
            prices[sym] = float(pos["last"])
    ledger.mark(account_id, prices, bar_date)
    return acts, sig
