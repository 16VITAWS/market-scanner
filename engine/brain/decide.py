"""
The decision pipeline. For every candidate setup it runs, in order:
DATA CHECK -> MARKET REGIME -> STRATEGY PERMISSION -> STRATEGY HEALTH -> EVIDENCE -> TRADE SCORE -> PROBABILITY ->
POSITION SIZE / CAPITAL -> COSTS -> EXPECTED NET VALUE -> REWARD/RISK -> LIQUIDITY -> EVENTS -> PORTFOLIO -> LOSS LIMITS
-> RANKING -> TRADE / WAIT / REJECT.
The first failing stage gives the rejection reason; every stage result is kept on the card.
"""
import math
import datetime as dt
import numpy as np
import pandas as pd
from . import features as F, regime as RG, strategies as S, calibrate as CA, score as SC, money as MO, montecarlo as MC

CAPITALS = [1000, 2000, 5000, 6000, 10000, 25000, 50000, 100000, 500000, 1000000]
TYPICAL_VALUE = 50000


def _num(x, nd=2):
    return None if x is None or (isinstance(x, float) and (math.isnan(x) or math.isinf(x))) else round(float(x), nd)


def build_context(stocks, idx, vix=None, frames=None, sectors=None):
    feats = {}
    ic = idx["Close"].astype(float)
    for s, df in stocks.items():
        if df is None or len(df) < 60:
            continue
        try:
            feats[s] = F.compute(df, ic)
        except Exception:
            continue
    closes = pd.DataFrame({s: f["close"] for s, f in feats.items()})
    br = RG.breadth_frame(closes) if len(closes.columns) >= 20 else None
    reg_series = RG.series(idx, br, vix)
    sec_rs = {}
    if sectors:
        tmp = {}
        for s, f in feats.items():
            v = f["rs63"].iloc[-1]
            if sectors.get(s) and v == v:
                tmp.setdefault(sectors[s], []).append(float(v))
        sec_rs = {k: float(np.median(v)) for k, v in tmp.items() if len(v) >= 3}
    return {"feats": feats, "breadth": br, "regimes": reg_series, "sector_rs": sec_rs, "global": RG.global_risk(frames or {})}


def calibrate(ctx, as_of):
    regs = ctx["regimes"]
    panic = set(regs[regs == "PANIC"].index)
    trades = CA.collect(ctx["feats"], regs, SC.technical, panic_dates=panic)
    cost = MO.trade_costs(TYPICAL_VALUE)["charges"] / TYPICAL_VALUE          # charges only; slippage is inside the simulated prices
    model = CA.Model(trades, cost, before=as_of)
    rep, base = CA.strategy_report(trades, cost)
    wf = CA.walk_forward_check(trades, cost)
    act = {k for k, v in rep.items() if v["status"].startswith("ACTIVE") or v["status"].startswith("REDUCED (recent")}
    netR = [(t["ret"] - cost) / t["riskf"] for t in trades if not t.get("open") and t["strategy"] in act and t.get("riskf")]
    mc = MC.run(netR) if netR else {"available": False, "reason": "no ACTIVE strategy with enough trades"}
    # adaptive score threshold: lowest bucket whose own closed trades have positive net expectancy (n >= 50)
    thr, btab = 65.0, []
    for lo, hi in CA.BUCKETS:
        r = [t["ret"] for t in trades if not t.get("open") and t["strategy"] != S.BENCHMARK and lo <= t["score"] < hi]
        st = CA.stats(r, cost)
        btab.append({"bucket": f"{lo}-{min(hi, 100)}", **{k: st.get(k) for k in ("n", "win_rate", "expectancy_pct", "profit_factor")}})
    good = [b for b in btab if (b["n"] or 0) >= 50 and (b["expectancy_pct"] or -1) > 0]
    if good:
        thr = max(40.0, float(good[0]["bucket"].split("-")[0]))
    span = (min(t["date"] for t in trades), max(t["date"] for t in trades)) if trades else (None, None)
    return {"trades": trades, "model": model, "cost_frac": cost, "report": rep, "benchmark": base, "walk_forward": wf, "montecarlo": mc,
            "score_threshold": thr, "score_buckets": btab, "span": span}


def _corr_fn(feats):
    cache = {}

    def f(a, b):
        k = tuple(sorted((a, b)))
        if k in cache:
            return cache[k]
        fa, fb = feats.get(a), feats.get(b)
        if fa is None or fb is None:
            cache[k] = None
            return None
        ra, rb = fa["close"].pct_change().tail(60), fb["close"].pct_change().tail(60)
        j = pd.concat([ra, rb], axis=1).dropna()
        cache[k] = float(j.corr().iloc[0, 1]) if len(j) >= 40 else None
        return cache[k]
    return f


def evaluate(ctx, cal, account, regime_now, data_ok=True, event_flags=None, sectors=None, betas=None):
    """account: {equity, cash, open_positions[list], slots_used, protections inputs...}. Returns ranked decision cards."""
    feats, model, cost_frac = ctx["feats"], cal["model"], cal["cost_frac"]
    state = regime_now["state"]
    mx = RG.MATRIX[state]
    br50 = (regime_now.get("signals") or {}).get("breadth50")
    rep = cal["report"]
    eq, cash = float(account["equity"]), float(account["cash"])
    tier = MO.tier(eq)
    p_mult, p_blocks, p_notes = MO.protections(account)
    corr = _corr_fn(feats)
    cards = []
    for sym, f in feats.items():
        row = f.iloc[-1]
        if f.index[-1] != ctx["regimes"].index[-1]:
            continue                                         # no bar for the latest session
        sig = S.signals(f.tail(260))
        hits = [k for k, v in sig.items() if k != S.BENCHMARK and bool(v.iloc[-1])]
        for strat in hits:
            plan = S.setup(strat, row)
            if not plan:
                continue
            stages = []

            def stage(name, ok, detail):
                stages.append({"stage": name, "pass": bool(ok), "detail": detail})
                return ok
            card = {"symbol": sym, "strategy": strat, "strategy_text": S.STRATEGIES[strat], "regime": state, "date": str(f.index[-1].date()),
                    "plan": plan, "sector": (sectors or {}).get(sym)}
            hist = model.estimate(strat, state, SC.technical(row))
            comp = SC.components(row, {"breadth50": br50, "sector_rs": ctx["sector_rs"].get((sectors or {}).get(sym)),
                                       "event_flags": (event_flags or {}).get(sym), "history": hist})
            sc = SC.total(comp)
            card["score"] = sc
            card["probability"] = hist
            st_info = rep.get(strat, {})
            weight = st_info.get("weight", 0.0)
            ok = (stage("DATA CHECK", data_ok, "fresh end-of-day data" if data_ok else "data stale - safety lock")
                  and stage("MARKET REGIME", strat in mx["allow"], f"{state}: {mx['note']}")
                  and stage("STRATEGY HEALTH", weight > 0, f"{strat} status {st_info.get('status', 'unknown')}, weight {weight}")
                  and stage("EVIDENCE", hist.get("evidence") == "OK", f"{hist.get('n_strategy', 0)} past trades of this strategy ({hist.get('n_regime', 0)} in {state})")
                  and stage("TRADE SCORE", sc["score"] >= cal["score_threshold"], f"{sc['score']} ({SC.band(sc['score'])}) vs adaptive threshold {cal['score_threshold']}"))
            size_mult = mx["size"] * p_mult * (min(weight, 1.0) if weight else 0)
            qty, size_note = MO.size(eq, cash, plan["entry_ref"], plan["stop"], tier["risk_pct"], size_mult,
                                     max_pos_pct=MO.LIMITS["max_position_pct"], avg_vol=(row["turnover_cr"] * 1e7 / row["close"]) if pd.notna(row["turnover_cr"]) else None)
            value = qty * plan["entry_ref"]
            risk_rs = qty * plan["risk_per_share"]
            costs = MO.trade_costs(value) if qty else MO.trade_costs(plan["entry_ref"])
            ev = MO.ev_for_position(hist, value if qty else plan["entry_ref"], risk_rs if qty else plan["risk_per_share"], cost_frac, costs)
            card.update(qty=qty, value=round(value, 2), risk_rupees=round(risk_rs, 2), risk_pct_equity=round(risk_rs / eq * 100, 3) if eq else None,
                        sizing=size_note, costs=costs, ev=ev, capital_required=round(value + costs["charges"], 2))
            min_net = max(tier["min_net_rupees"], MO.LIMITS["min_net_to_cost"] * costs["total"])
            min_R = 0.05 * mx["edge"]
            ok = ok and (stage("POSITION SIZE / CAPITAL", qty >= 1, size_note if qty >= 1 else
                               f"cannot buy even 1 share within the risk budget (₹{plan['entry_ref']:,.2f} per share, ₹{plan['risk_per_share']:,.2f} risk per share)")
                         and stage("EXPECTED NET VALUE", ev is not None and ev["expected_net"] > 0 and (ev["net_in_R"] or 0) >= min_R,
                                   f"expected net ₹{ev['expected_net'] if ev else 'n/a'} = {ev['net_in_R'] if ev else 'n/a'}R (need >= {min_R:.2f}R)")
                         and stage("COST CHECK", ev["expected_net"] >= min_net,
                                   f"net ₹{ev['expected_net']:.0f} vs required ₹{min_net:.0f} (max of tier minimum and {MO.LIMITS['min_net_to_cost']}x total costs ₹{costs['total']:.0f})")
                         and stage("REWARD / RISK", plan["rr_t2"] >= MO.LIMITS["min_rr"], f"target 2 = {plan['rr_t2']}R, target 1 = {plan['rr_t1']}R")
                         and stage("LIQUIDITY", (row["turnover_cr"] if pd.notna(row["turnover_cr"]) else 0) >= MO.LIMITS["min_turnover_cr"], f"₹{(row['turnover_cr'] if pd.notna(row['turnover_cr']) else 0):.0f} cr average daily value"))
            if ok:
                fl = (event_flags or {}).get(sym) or []
                ok = stage("NEWS / EVENTS", not fl, "; ".join(fl) if fl else "no blocking event found (results / ex-date / F&O ban / governance)")
            if ok:
                blk, cn, port = MO.correlation_check(sym, card["sector"], (betas or {}).get(sym, 1.0), risk_rs, account.get("open_positions", []), corr, eq)
                ok = stage("PORTFOLIO", blk is None, blk or ("ok" + (": " + "; ".join(cn) if cn else "")))
            if ok:
                ok = stage("LOSS LIMITS", not p_blocks, "; ".join(p_blocks) if p_blocks else ("ok" + ("; " + "; ".join(p_notes) if p_notes else "")))
            card["stages"] = stages
            card["decision"] = "CANDIDATE" if ok else "REJECT"
            card["reject_reason"] = None if ok else next(s for s in stages if not s["pass"])["stage"] + ": " + next(s for s in stages if not s["pass"])["detail"]
            card["rank_key"] = (ev["net_in_R"] or 0) * max(weight, 0.01) if ev else -9
            cards.append(card)
    # ranking & slots
    cands = sorted([c for c in cards if c["decision"] == "CANDIDATE"], key=lambda c: -c["rank_key"])
    held = {p["symbol"] for p in account.get("open_positions", [])}
    slots = max(0, tier["max_open"] - len(held) - account.get("pending", 0))
    taken, n = set(), 0
    for c in cands:
        if c["symbol"] in held:
            c["decision"], c["reject_reason"] = "WAIT", "already holding this stock"
        elif c["symbol"] in taken:
            c["decision"], c["reject_reason"] = "WAIT", "another strategy already selected this stock"
        elif n < slots:
            c["decision"] = "TRADE"; taken.add(c["symbol"]); n += 1
        else:
            c["decision"], c["reject_reason"] = "WAIT", f"passed every check but ranked below better opportunities (only {slots} free slot(s) for a {tier['name']} account)"
    order = {"TRADE": 0, "WAIT": 1, "REJECT": 2}
    cards.sort(key=lambda c: (order[c["decision"]], -c["rank_key"]))
    for i, c in enumerate(cards, 1):
        c["rank"] = i
    return cards, {"tier": tier, "slots": slots, "protection_mult": p_mult, "protection_blocks": p_blocks, "protection_notes": p_notes}


def capital_scenarios(cards, cal, regime_now):
    """For each capital level: can the best setups that passed the non-capital checks be traded economically?"""
    mx = RG.MATRIX[regime_now["state"]]
    pool = [c for c in cards if len(c.get("stages", [])) >= 6]          # passed data, regime, health, evidence and score checks
    out = []
    for cap in CAPITALS:
        t = MO.tier(cap)
        best, viable = None, 0
        for c in pool:
            p = c["plan"]
            w = cal["report"].get(c["strategy"], {}).get("weight", 0)
            q, _ = MO.size(cap, cap, p["entry_ref"], p["stop"], t["risk_pct"], mx["size"] * min(w, 1.0))
            if q < 1:
                continue
            v = q * p["entry_ref"]
            co = MO.trade_costs(v)
            ev = MO.ev_for_position(c["probability"], v, q * p["risk_per_share"], cal["cost_frac"], co)
            need = max(t["min_net_rupees"], MO.LIMITS["min_net_to_cost"] * co["total"])
            if ev and ev["expected_net"] >= need and (ev["net_in_R"] or 0) >= 0.05 * mx["edge"]:
                viable += 1
                if best is None or ev["expected_net"] > best["expected_net"]:
                    best = {"symbol": c["symbol"], "strategy": c["strategy"], "qty": q, "value": round(v, 2), "expected_net": ev["expected_net"],
                            "costs": co["total"], "cost_share_pct": round(co["total"] / max(ev["expected_gross"], 1e-9) * 100, 1)}
        msg = (f"{viable} economically viable setup(s)" if viable else
               ("NO TRADE — CURRENT CAPITAL IS INSUFFICIENT FOR A HIGH-QUALITY SETUP" if pool else "NO TRADE — no setup passed the quality checks today"))
        out.append({"capital": cap, "tier": t["name"], "risk_per_trade_pct": t["risk_pct"] * 100, "max_open": t["max_open"], "viable": viable, "best": best, "message": msg})
    return out


def card_text(c):
    """The VISION AI TRADE DECISION block in plain text."""
    p, ev, pr = c["plan"], c.get("ev") or {}, c.get("probability") or {}
    lines = ["VISION AI TRADE DECISION",
             f"Instrument: {c['symbol']} (NSE cash, delivery)", "Direction: LONG", f"Strategy: {c['strategy']}", f"Market Regime: {c['regime']}",
             f"Entry: next open (reference {p['entry_ref']})", f"Stop: {p['stop']}", f"Target 1: {p['t1']}", f"Target 2: {p['t2']}",
             f"Risk/Reward: {p['rr_t1']}R / {p['rr_t2']}R", f"Model Score: {c['score']['score']} ({SC.band(c['score']['score'])})",
             f"Probability Estimate: {pr.get('p')} ({pr.get('method', '')})", f"Capital Required: ₹{c.get('capital_required')}",
             f"Position Size: {c.get('qty')} shares", f"Expected Gross Profit: ₹{ev.get('expected_gross')}", f"Estimated Charges: ₹{ev.get('charges')}",
             f"Estimated Slippage: ₹{ev.get('slippage')}", f"Expected Net Profit: ₹{ev.get('expected_net')}", f"Maximum Loss: ₹{c.get('risk_rupees')} (+ costs, gaps can be worse)",
             f"Final Decision: {c['decision']}" + (f" - {c['reject_reason']}" if c.get("reject_reason") else "")]
    return "\n".join(lines)
