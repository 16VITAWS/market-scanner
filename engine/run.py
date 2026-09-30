"""
Entry point.  python -m engine.run <job> --site site [--offline]

  eod        after-close: download, scan, paper-trade, backtests refresh, all API files
  intraday   during market hours: refresh quotes/snapshot, mark positions (no decisions)
  weekend    weekend review + full research refresh
  health     tests + provider checks only
  migrate    import v1 state (paper_log, portfolio.json, signals) into v2 ledger/runs

--offline uses the preserved NIFTY CSV and legacy snapshots (used in the sandbox and in tests).
State lives in <site>/data (ledger.json, runs/, backups/); outputs in <site>/api.
"""
import os, sys, io, json, glob, time, shutil, argparse, subprocess, datetime as dt, traceback
from decimal import Decimal
import pandas as pd
import numpy as np
from . import config as C, instruments as INS, calendar as cal, dq, api, pipeline as P, backtest as B, news as NEWS, knowledge as KG, reports as R, analytics as AN, costs
from .ledger import Ledger
from .providers import get as provider, capabilities, ProviderError
from .providers.stubs import ManualCSV, NSEWeb
from .strategies import get as get_strategy, catalogue
from .strategies.ma_cross import MACross
from . import options as OPT, notify as NOTIFY, forecast as FC, contagion as CG, anomaly as AM, ml as ML, execalgo as EX, margin as MG, events as EV
from .live import control as LCTL, proposals as LPROP
from . import nse_eod as NSEEOD, livefeed as LF, simulator, value as VAL
from . import quality as QA, telemetry as TEL
from . import regime_hmm as HMM, impact as IM, flows as FL, mf as MF, macro as MAC, rules as RULES

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def log(*a):
    print(dt.datetime.now(IST).strftime("%H:%M:%S"), *a, flush=True)


# ---------------------------------------------------------------- state helpers
class Site:
    def __init__(self, path):
        self.path = path
        self.data = os.path.join(path, "data")
        for d in ("runs", "backups", "legacy", "raw"):
            os.makedirs(os.path.join(self.data, d), exist_ok=True)
        os.makedirs(os.path.join(path, "api", "candles"), exist_ok=True)
        # seed from repo data on first run
        for d in ("legacy", "raw"):
            src = os.path.join(ROOT, "data", d)
            for f in glob.glob(os.path.join(src, "*")):
                dst = os.path.join(self.data, d, os.path.basename(f))
                if not os.path.exists(dst):
                    shutil.copy(f, dst)
        for f in glob.glob(os.path.join(ROOT, "data", "runs", "*.json")):
            dst = os.path.join(self.data, "runs", os.path.basename(f))
            if not os.path.exists(dst):
                shutil.copy(f, dst)

    def ledger(self):
        L = Ledger(os.path.join(self.data, "ledger.json")).load(C.DEFAULT_ACCOUNTS)
        for a in C.DEFAULT_ACCOUNTS:
            if a["id"] not in L.state["accounts"]:
                L.create_account(a["id"], a["name"], a["currency"], a["cash"], a["strategies"])
        return L

    def backup_ledger(self):
        p = os.path.join(self.data, "ledger.json")
        if os.path.exists(p):
            shutil.copy(p, os.path.join(self.data, "backups", f"ledger-{dt.datetime.now(IST).strftime('%Y%m%d-%H%M')}.json"))
            olds = sorted(glob.glob(os.path.join(self.data, "backups", "ledger-*.json")))
            for f in olds[:-40]:
                os.remove(f)

    def legacy(self, name):
        p = os.path.join(self.data, "legacy", name + ".json")
        return json.load(open(p)) if os.path.exists(p) else None

    def load_json(self, rel, default=None):
        p = os.path.join(self.path, rel)
        return json.load(open(p)) if os.path.exists(p) else default


# ---------------------------------------------------------------- data loading
def load_market(site, offline, universe_limit=None):
    """Returns dict with master, frames (id -> df), meta per id, failures, universe source, provider status."""
    prov_status = []
    master = INS.master()
    frames, metas, failed = {}, {}, []
    if offline:
        df, m = ManualCSV().candles_from_csv(os.path.join(site.data, "raw", "nifty_daily_ohlc_from_portal.csv"))
        latest = site.legacy("latest") or {}
        if latest.get("date") and latest["date"] > str(df.index[-1].date()):
            # append the manually entered 25-Sep close as its own MANUAL bar (never interpolate the gap)
            row = pd.DataFrame({"Open": [latest["prev"]], "High": [max(latest["prev"], latest["close"])], "Low": [min(latest["prev"], latest["close"])],
                                "Close": [latest["close"]], "Volume": [0]}, index=pd.DatetimeIndex([pd.Timestamp(latest["date"], tz="UTC")]))
            df = pd.concat([df, row]); m["note"] += f"; {latest['date']} close added manually (Business Standard / HDFC Sky), gap {df.index[-2].date()}..{latest['date']} NOT filled"
        frames["NIFTY"] = df; metas["NIFTY"] = m
        # synthetic-free offline: no other instruments. Legacy engine run supplies stock signals.
        prov_status.append({**provider("yahoo").capabilities(), "last_fetch_ok": False, "last_error": "offline mode: network to Yahoo not available in this environment"})
        return {"master": master, "frames": frames, "metas": metas, "failed": failed, "universe_source": "offline (NIFTY CSV only)", "providers": prov_status, "sectors": {}}
    y = provider("yahoo")
    nse_df, usrc = INS.load_nifty500(fallback_file=os.path.join(ROOT, "stocks.txt"))
    if universe_limit:
        nse_df = nse_df.head(universe_limit)
    master = INS.master(pd.concat([nse_df, INS.us_equity_rows()], ignore_index=True))
    sectors = dict(zip(nse_df.id, nse_df.sector))
    core = [r for _, r in master.iterrows() if r.source == "core" and isinstance(r.yahoo, str)]
    stocks = [r for _, r in master.iterrows() if r.source != "core" or (r.kind == "stock" and r.currency == "USD")]
    ok = False
    try:
        got, bad = y.candles_many([r.yahoo for r in core], "1d", "5y")
        for r in core:
            if r.yahoo in got:
                frames[r.id] = dq.clean(got[r.yahoo]); metas[r.id] = {"provider": "yahoo", "ticker": r.yahoo, "interval": "1d", "delay_minutes": 15, "licence": y.licence, "fetched_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"), "status": "OK"}
        failed += bad; ok = len(got) > 0
        got, bad = y.candles_many([r.yahoo for r in stocks], "1d", f"{C.HISTORY_YEARS}y")
        for r in stocks:
            if r.yahoo in got:
                frames[r.id] = dq.clean(got[r.yahoo]); metas[r.id] = {"provider": "yahoo", "ticker": r.yahoo, "interval": "1d", "delay_minutes": 15, "licence": y.licence, "fetched_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"), "status": "OK"}
        failed += bad
        err = None
        if TEL.get(site.path).active:
            TEL.get(site.path).active.processed = len(frames); TEL.get(site.path).active.failed = len(failed)
    except Exception as e:  # noqa
        err = f"{type(e).__name__}: {e}"; log("Yahoo download failed:", err)
    prov_status.append({**y.capabilities(), "last_fetch_ok": ok, "last_error": err, "fetched": len(frames), "failed": len(failed)})
    # fill sessions Yahoo has not published yet from NSE's official end-of-day files
    try:
        nse_ids = [k for k in frames if k in master.index and master.loc[k, "currency"] == "INR" and master.loc[k, "calendar"] == "NSE"]
        rep = NSEEOD.patch(frames, nse_ids, list(cal.CALENDARS["NSE"]["holidays"].keys()))
        for d_, n_ in rep["patched"].items():
            log(f"NSE bhavcopy filled {d_} for {n_} symbols")
        prov_status.append({"id": "nse-eod", "name": "NSE official EOD files (bhavcopy + index close)", "last_fetch_ok": bool(rep["patched"]) or None,
                            "last_error": None, "detail": rep, "delay": "after ~18:00 IST", "licence": "NSE public archive"})
    except Exception as e:  # noqa
        log("NSE EOD patch failed:", e)
    for pid in ("angelone", "twelvedata", "nseweb", "manual"):
        prov_status.append({**provider(pid).capabilities(), "last_fetch_ok": None})
    return {"master": master, "frames": frames, "metas": metas, "failed": failed, "universe_source": usrc, "providers": prov_status, "sectors": sectors}


def dq_pass(frames, master):
    """Split frames into clean (tradeable) and rejected, with a summary."""
    clean, rejected, reports = {}, {}, {}
    for k, df in frames.items():
        cal_id = master.loc[k, "calendar"] if k in master.index else None
        issues, stats = dq.check(df, cal_id if cal_id in cal.CALENDARS else None)
        reports[k] = {"issues": issues, **stats}
        if dq.hard_fail(issues):
            rejected[k] = issues
        else:
            clean[k] = df
    return clean, rejected, reports


# ---------------------------------------------------------------- jobs
def repair_ledger(site):
    """One-time, explicit repair: if the repo file data/repair/restore_ledger.txt names a backup that has not been
    restored yet, copy that backup over the ledger (used after a run traded on stale feed data). Logged in data/repairs.json."""
    req = os.path.join(ROOT, "data", "repair", "restore_ledger.txt")
    if not os.path.exists(req):
        return
    name = open(req).read().strip()
    log_path = os.path.join(site.data, "repairs.json")
    done = json.load(open(log_path)) if os.path.exists(log_path) else []
    if any(d.get("restored") == name for d in done):
        return
    src = os.path.join(site.data, "backups", name)
    if not os.path.exists(src):
        log("repair: backup not found", name); return
    cur = os.path.join(site.data, "ledger.json")
    if os.path.exists(cur):
        shutil.copy(cur, os.path.join(site.data, "backups", "ledger-before-repair-" + dt.datetime.now(IST).strftime("%Y%m%d-%H%M") + ".json"))
    shutil.copy(src, cur)
    reason = open(os.path.join(ROOT, "data", "repair", "reason.txt")).read().strip() if os.path.exists(os.path.join(ROOT, "data", "repair", "reason.txt")) else ""
    done.append({"restored": name, "at": api.now(), "reason": reason})
    json.dump(done, open(log_path, "w"), indent=1)
    log("repair: ledger restored from", name)


def sector_momentum(stocks, sectors, days=63):
    acc = {}
    for k, df in stocks.items():
        sec = sectors.get(k)
        if not sec or len(df) <= days:
            continue
        acc.setdefault(sec, []).append(float(df["Close"].iloc[-1] / df["Close"].iloc[-days - 1] - 1) * 100)
    return {s: float(np.median(v)) for s, v in acc.items() if len(v) >= 3}


def job_eod(site, offline=False, universe_limit=None, fetch_news=True, weekend=False):
    log("EOD run start; mode", C.MODE, "offline" if offline else "online")
    repair_ledger(site)
    site.backup_ledger()
    M = load_market(site, offline, universe_limit)
    master, frames, metas = M["master"], M["frames"], M["metas"]
    clean, rejected, dqrep = dq_pass(frames, master)
    idx = clean.get("NIFTY") if "NIFTY" in clean else frames.get("NIFTY")
    if idx is None:
        raise SystemExit("No NIFTY data - aborting run (nothing is written).")
    age = dq.age_days(idx)
    data_status = "OK" if age <= C.RISK["max_data_age_days"] else "STALE"
    reg = P.regime(idx)
    stocks = {k: v for k, v in clean.items() if k in master.index and master.loc[k, "kind"] == "stock" and master.loc[k, "currency"] == "INR"}
    news_fn = (lambda s: NEWS.symbol_news(s)) if (fetch_news and not offline) else None
    if stocks:
        scan = P.scan(stocks, idx, "trend_breakout_swing", news_fn=news_fn, sectors=M["sectors"])
    else:
        # offline: carry the preserved 25-Sep engine run forward as HISTORICAL signals
        leg = site.legacy("engine") or {}
        scan = {"regime": reg, "strategy": get_strategy("trend_breakout_swing").spec(), "buys": leg.get("buys", []),
                "sells": [{"symbol": s["s"], "score": s["score"], "entry": s["px"], "why": s["why"], "action": "SELL", "bar": leg.get("date")} for s in leg.get("sells", [])],
                "blocked": [{"symbol": s, "score": sc, "reason": "NIFTY regime BEAR on 2026-09-25", "why": [], "entry": None} for s, sc in leg.get("blocked", [])],
                "skipped": {}, "table": [], "scanned": leg.get("count", 0), "universe": leg.get("count", 0), "historical": True,
                "note": f"Offline: signals are the preserved run of {leg.get('date')} ({leg.get('run')}), engine v1"}
    scan["data_status"] = data_status
    bar_date = reg["date"]
    # never trade on data OLDER than a session the ledger has already processed (feed hiccups can return a stale last bar)
    done_dates = sorted(os.path.basename(f)[:-5] for f in glob.glob(os.path.join(site.data, "runs", "*.json")) if os.path.basename(f)[:4].isdigit())
    if done_dates and str(bar_date) < done_dates[-1]:
        log(f"latest bar {bar_date} is older than already-processed {done_dates[-1]}: marking BEHIND, no trading")
        data_status = "BEHIND"
        scan["data_status"] = data_status
    # ---- event filters (F&O ban, results / ex-dividend) on candidates only
    ev = {"ban": {"status": "offline", "symbols": []}, "checks": {}}
    if not offline and stocks:
        try:
            ev["ban"] = EV.fo_ban()
            cands = [b["symbol"] for b in scan["buys"]] + [b["symbol"] for b in scan["blocked"]][:10]
            ymap = {k: master.loc[k, "yahoo"] for k in cands if k in master.index}
            ev["checks"] = EV.check(cands, pd.Timestamp(bar_date).date(), ymap, ev["ban"])
            keep = []
            for b in scan["buys"]:
                c = ev["checks"].get(b["symbol"], {})
                b["event_flags"] = c.get("flags", [])
                if c.get("block"):
                    scan["blocked"].append({"symbol": b["symbol"], "score": b["score"], "reason": c["block"], "why": b["why"], "entry": b["entry"]})
                else:
                    keep.append(b)
            scan["buys"] = keep
        except Exception as e:  # noqa
            ev["error"] = str(e)
    scan["events"] = ev
    # ---- US scan (global auto paper trading)
    us_stocks = {k: v for k, v in clean.items() if k in master.index and master.loc[k, "kind"] == "stock" and master.loc[k, "currency"] == "USD"}
    scan_us, us_status = None, None
    if us_stocks and "SPX" in clean:
        spx = clean["SPX"]
        us_status = "OK" if dq.age_days(spx) <= C.RISK["max_data_age_days"] else "STALE"
        scan_us = P.scan(us_stocks, spx, "trend_breakout_swing_us", limits=C.US_LIMITS)
        scan_us["data_status"] = us_status
    # ---- paper trading
    L = site.ledger()
    actions = {}
    if stocks and data_status == "OK" and C.PAPER_ON:
        for aid, a in L.state["accounts"].items():
            if "trend_breakout_swing" in a["strategies"]:
                actions[aid] = P.paper_run(L, aid, stocks, bar_date, scan["buys"], reg, "trend_breakout_swing", data_status, M["sectors"])
    else:
        why = "offline" if not stocks else (f"data {data_status}" if data_status != "OK" else f"mode {C.MODE}")
        for aid, a in L.state["accounts"].items():
            if a["strategies"] and a["strategies"][0] in ("trend_breakout_swing_us", "index_options_regime", "value_longterm"):
                continue
            prices = {s: float(frames[s]["Close"].iloc[-1]) for s in L.account(aid)["positions"] if s in frames}
            L.mark(aid, prices, bar_date)
            actions[aid] = [f"No trading this run ({why}); positions marked to market only."]
    if scan_us and us_status == "OK" and C.PAPER_ON:
        for aid, a in L.state["accounts"].items():
            if "trend_breakout_swing_us" in a["strategies"]:
                actions[aid] = P.paper_run(L, aid, us_stocks, scan_us["regime"]["date"], scan_us["buys"], scan_us["regime"], "trend_breakout_swing_us", us_status)
    elif "US-SWING" in L.state["accounts"]:
        actions["US-SWING"] = ["No US trading this run (" + ("offline / no US data" if not scan_us else f"US data {us_status}") + ")."]
    # ---- index options (modeled prices)
    opt_sig = None
    if data_status == "OK" and C.PAPER_ON and "IN-OPTIONS" in L.state["accounts"]:
        today = pd.Timestamp(bar_date).date()
        hol = list(cal.CALENDARS["NSE"]["holidays"].keys())
        actions["IN-OPTIONS"], opt_sig = OPT.run(L, "IN-OPTIONS", clean if "NIFTY" in clean else frames, reg, today, bar_date, hol)
    L.save()
    # ---- notifications (phone / PC push)
    try:
        n = NOTIFY.Notifier(site.path, dry=offline)
        NOTIFY.eod(n, scan, scan_us, opt_sig, actions)
        n.save()
        log("notifications sent:", n.sent)
    except Exception as e:  # noqa
        log("notify failed:", e)
    # ---- intelligence: forecast, contagion, anomalies, ML shadow
    intel = {}
    try:
        intel["forecast"] = FC.run(clean if "NIFTY" in clean else frames)
    except Exception as e:  # noqa
        intel["forecast"] = {"available": False, "reason": str(e)}
    try:
        intel["contagion"] = CG.run(frames)
    except Exception as e:  # noqa
        intel["contagion"] = {"links": [], "alerts": [], "error": str(e)}
    try:
        intel["anomalies"] = AM.run({k: v for k, v in clean.items() if k in master.index and master.loc[k, "kind"] in ("stock", "index", "commodity", "fx", "etf")}, M["sectors"])
    except Exception as e:  # noqa
        intel["anomalies"] = {"items": [], "error": str(e)}
    try:
        intel["ml"] = ML.run(stocks, clean["NIFTY"]) if stocks and "NIFTY" in clean else {"available": False, "reason": "offline / no stock panel"}
    except Exception as e:  # noqa
        intel["ml"] = {"available": False, "reason": str(e)}
    # ---- v2.3: regime HMM, AI confidence on signals, institutional-flow proxies, mutual funds, macro
    prev_intel = site.load_json("api/intel.json", {})
    try:
        intel["regime_hmm"] = HMM.run(clean["NIFTY"] if "NIFTY" in clean else frames["NIFTY"])
    except Exception as e:  # noqa
        intel["regime_hmm"] = {"available": False, "reason": str(e)}
    ai = (intel.get("ml") or {}).get("all_today") or {}
    base = (intel.get("ml") or {}).get("base_rate")
    for row in scan["buys"] + scan.get("table", []) + scan["blocked"]:
        pv = ai.get(row.get("symbol"))
        if pv is not None:
            row["ai_p"] = pv
            row["ai_view"] = ("AGREES" if base is not None and pv >= base + 0.05 else "DISAGREES" if base is not None and pv <= base - 0.05 else "NEUTRAL")
    if isinstance(intel.get("ml"), dict):
        intel["ml"].pop("all_today", None)
    flows_out = {"available": False, "reason": "offline"}
    if not offline and stocks:
        try:
            dh_path = os.path.join(site.data, "delivery.json")
            dh = json.load(open(dh_path)) if os.path.exists(dh_path) else {}
            flows_out, dh = FL.run(pd.Timestamp(bar_date).date(), list(stocks), dh, stocks)
            json.dump(dh, open(dh_path, "w"))
            flows_out["available"] = True
        except Exception as e:  # noqa
            flows_out = {"available": False, "reason": str(e)}
    api.write(site.path, "inst_flows.json", flows_out)
    sec_mom = sector_momentum(stocks, M["sectors"]) if stocks else {}
    mf_old = site.load_json("api/mf.json", {})
    mf_age = (dt.date.today() - dt.date.fromisoformat(mf_old["generated_at"][:10])).days if mf_old.get("generated_at") else 99
    if not offline and (weekend or mf_age >= 6 or not mf_old.get("available")):
        try:
            mfo = MF.run(sec_mom, (intel["regime_hmm"] or {}).get("current"))
            mfo["sector_momentum_3m"] = {k: round(v, 1) for k, v in sorted(sec_mom.items(), key=lambda kv: -kv[1])}
            api.write(site.path, "mf.json", mfo)
        except Exception as e:  # noqa
            log("mf failed:", e)
            if not mf_old:
                api.write(site.path, "mf.json", {"available": False, "reason": str(e)})
    elif not mf_old:
        api.write(site.path, "mf.json", {"available": False, "reason": "offline - AMFI / mfapi not reachable from this environment"})
    # ---- long-term value scanner + value paper accounts
    try:
        vc_path = os.path.join(site.data, "value_cache.json")
        vcache = json.load(open(vc_path)) if os.path.exists(vc_path) else {}
        vrep = {"fetched_this_run": 0, "errors": 0}
        if not offline:
            univ = [(k, master.loc[k, "yahoo"], "IN" if master.loc[k, "currency"] == "INR" else "US", master.loc[k, "name"])
                    for k in master.index if master.loc[k, "kind"] == "stock" and isinstance(master.loc[k, "yahoo"], str) and master.loc[k, "currency"] in ("INR", "USD")]
            vcache, vrep = VAL.refresh(univ, vcache, max_fetch=400 if weekend else 150)
            json.dump(vcache, open(vc_path, "w"), default=str)
        prices = {k: float(v["Close"].iloc[-1]) for k, v in clean.items()}
        vscored, vrecs = VAL.build(vcache, prices)
        vacts = {}
        if C.PAPER_ON and data_status == "OK":
            mk = L.state.setdefault("value_rebalanced", {})
            for aid, mkt in (("IN-VALUE", "IN"), ("US-VALUE", "US")):
                if aid not in L.state["accounts"] or not [r for r in vscored if r.get("market") == mkt]:
                    continue
                acc_ = L.account(aid)
                ym = str(bar_date)[:7]
                reb = mk.get(aid) != ym
                vacts[aid] = VAL.paper_step(L, aid, vscored, clean, bar_date, simulator, mkt, reb)
                if reb:
                    mk[aid] = ym
                actions[aid] = vacts[aid]
            L.save()
        n_cov = {m_: sum(1 for r in vscored if r.get("market") == m_) for m_ in ("IN", "US")}
        api.write(site.path, "value.json", {"as_of": str(bar_date), "coverage": n_cov, "universe_cached": len(vcache), "refresh": vrep,
                                            "recommendations": vrecs, "stocks": vscored, "actions": vacts,
                                            "method": VAL.__doc__.strip(), "status": "DELAYED fundamentals (annual statements) + today's close",
                                            "honesty": "Point-in-time fundamentals are not available free, so this scanner has NOT been backtested. "
                                                       "Its live record starts with the IN-VALUE / US-VALUE paper accounts. Conviction is a model score, not a probability."})
    except Exception as e:  # noqa
        log("value scanner failed:", e)
        if not site.load_json("api/value.json"):
            api.write(site.path, "value.json", {"recommendations": [], "stocks": [], "error": str(e)})
    if not offline:
        try:
            api.write(site.path, "macro.json", MAC.run(frames))
        except Exception as e:  # noqa
            log("macro failed:", e)
    elif not site.load_json("api/macro.json"):
        api.write(site.path, "macro.json", {"items": [], "note": "offline - FRED / World Bank not reachable from this environment"})
    try:
        n2 = NOTIFY.Notifier(site.path, dry=offline)
        rh, rh_old = intel.get("regime_hmm") or {}, (prev_intel.get("regime_hmm") or {})
        if rh.get("available") and rh_old.get("current") and rh["current"] != rh_old["current"]:
            n2.push(f"Regime change: {rh_old['current']} -> {rh['current']}", f"HMM regime model now {rh['current']} ({rh['probabilities'][rh['current']]:.0%}). Typical duration {rh['expected_duration_days']} days. Descriptive, not a forecast.",
                    key=f"rg|{rh['as_of']}", tags=("compass",), priority=4, click=NOTIFY.PORTAL + "#intel")
        for x in (flows_out.get("accumulation") or [])[:5]:
            n2.push(f"{x['signal']}: {x['symbol']}", f"Volume {x['volume_x_avg']}x avg, delivery {x['delivery_pct']}% (avg {x['delivery_avg_pct']}%), close {x['close_chg_pct']:+.2f}%. End-of-day proxy.",
                    key=f"acc|{x['symbol']}|{bar_date}", tags=("whale",), click=NOTIFY.PORTAL + "#flows")
        for x in (flows_out.get("deal_summary") or [])[:40]:
            if x.get("in_universe") and abs(x["net_value_cr"]) >= 100:
                n2.push(f"Big deal: {x['symbol']} net {x['net_value_cr']:+,.0f} cr", f"{', '.join(x['kinds'])} deals · {x['read']} · {', '.join(x['clients'][:3])}",
                        key=f"deal|{x['symbol']}|{bar_date}", tags=("whale",), click=NOTIFY.PORTAL + "#flows")
        rl, rbad = check_rules(n2, api.quotes_from_frames(frames, master), bar_date) if not offline else RULES.parse(os.environ.get("ALERT_RULES", ""))
        intel["alert_rules"] = {"rules": rl, "invalid": rbad, "note": "From the repository variable ALERT_RULES."}
        for a in intel["contagion"].get("alerts", [])[:5]:
            n2.push(f"Market link change: {a['pair']}", a["text"], key=f"cg|{a['pair']}|{bar_date}", tags=("globe_with_meridians",), click=NOTIFY.PORTAL + "#intel")
        big = [x for x in intel["anomalies"].get("items", []) if abs(x["z"]) >= 4][:8]
        for x in big:
            n2.push(f"Unusual: {x['symbol']} {x['type']}", f"{x['detail']} (z {x['z']})", key=f"an|{x['symbol']}|{x['type']}|{bar_date}", tags=("mag",), click=NOTIFY.PORTAL + "#intel")
        n2.save()
    except Exception as e:  # noqa
        log("intel notify failed:", e)
    # ---- live trading: control file + proposals (inert unless approved / auto-gated; placed only by the runner)
    pj_accounts = api.paper_json(L)["accounts"]
    for acc in pj_accounts:
        trades = [t for t in L.state["trades"] if t["account"] == acc["account"]]
        acc["analytics"] = AN.full(L.account(acc["account"])["equity"], trades, acc["start_cash"])
    ctl = LCTL.build(pj_accounts)
    props = LPROP.build(scan, opt_sig, ctl, bar_date, float(os.environ.get("LIVE_CAPITAL") or 100000))
    live_results = []
    for f in sorted(glob.glob(os.path.join(site.path, "live", "results", "*.json")))[-10:]:
        try:
            live_results += json.load(open(f))
        except Exception:
            pass
    api.write(site.path, "live_control.json", ctl)
    api.write(site.path, "live_proposals.json", props)
    api.write(site.path, "live_results.json", {"items": live_results[-200:], "note": "Reported by the VISION runner on your static-IP machine (if configured)."})
    if ctl["mode"] != "OFF":
        try:
            n3 = NOTIFY.Notifier(site.path, dry=offline)
            for p_ in props["items"]:
                n3.push(f"LIVE proposal {p_['id']}: {p_['side']} {p_['symbol']}", f"qty {p_['qty']} limit {p_['limit']} · valid {p_['valid_until']} · mode {ctl['mode']}\n{p_['reason'][:200]}",
                        key=f"lp|{p_['id']}", tags=("money_with_wings",), priority=5, click=NOTIFY.PORTAL + "#live")
            n3.save()
        except Exception as e:  # noqa
            log("live notify failed:", e)
    # ---- run record (preserved forever)
    run_rec = {"engine_version": C.ENGINE_VERSION, "run_at": api.now(), "data_date": bar_date, "mode": C.MODE, "data_status": data_status,
               "regime": scan["regime"], "buys": scan["buys"], "sells": scan["sells"], "blocked": scan["blocked"], "scanned": scan["scanned"],
               "universe": scan["universe"], "universe_source": M["universe_source"], "strategy": scan["strategy"], "paper_actions": actions,
               "rejected_dq": rejected, "failed_download": M["failed"][:50], "skipped": dict(list(scan.get("skipped", {}).items())[:200])}
    if data_status != "BEHIND":                       # never overwrite the record of a session with a stale re-run
        with open(os.path.join(site.data, "runs", f"{bar_date}.json"), "w") as f:
            f.write(json.dumps(api.clean(json.loads(json.dumps(run_rec, default=api._enc))), indent=1, allow_nan=False))
    # ---- outputs
    write_common(site, M, clean, rejected, dqrep, reg, scan, L, actions, data_status, offline, scan_us=scan_us, opt_sig=opt_sig, intel=intel)
    log("EOD done:", reg["state"], len(scan["buys"]), "buys", len(scan["sells"]), "sells", scan["scanned"], "scanned")
    return run_rec


def write_common(site, M, clean, rejected, dqrep, reg, scan, L, actions, data_status, offline, scan_us=None, opt_sig=None, intel=None):
    master, frames, metas = M["master"], M["frames"], M["metas"]
    # quotes + candles
    q = api.quotes_from_frames(frames, master)
    api.write(site.path, "quotes.json", {"data_status": data_status, "count": len(q), "quotes": q,
              "note": "End-of-day / delayed closes from the free feed. Not live. Status per quote."})
    core_ids = [i for i in master.index if master.loc[i, "source"] == "core"]
    held = set()
    for a in L.state["accounts"].values():
        held |= set(a["positions"].keys())
    sig_ids = {s["symbol"] for s in scan["buys"] + scan["sells"] + scan["blocked"]} | {r["symbol"] for r in scan.get("table", [])[:60]}
    liq = lambda k: -float((frames[k]["Close"] * frames[k]["Volume"]).tail(20).mean())
    inr = [k for k in frames if k not in core_ids and k in master.index and master.loc[k, "currency"] == "INR"]
    usd = [k for k in frames if k not in core_ids and k in master.index and master.loc[k, "currency"] == "USD"]
    top = sorted(inr, key=liq)[:120] + sorted(usd, key=liq)[:60]
    if scan_us:
        sig_ids |= {s["symbol"] for s in scan_us["buys"] + scan_us["sells"] + scan_us["blocked"]} | {r["symbol"] for r in scan_us.get("table", [])[:40]}
    want = [k for k in frames if k in core_ids or k in held or k in sig_ids or k in top]
    written = []
    for k in want:
        instr = master.loc[k].to_dict() if k in master.index else {"id": k}
        api.candles_json(site.path, instr, frames[k], metas.get(k, {"provider": "unknown"}), "1d", max_bars=None if k in core_ids or k == "NIFTY" else 520,
                         calendar_id=instr.get("calendar") if instr.get("calendar") in cal.CALENDARS else None)
        written.append(k)
    api.write(site.path, "candles_index.json", {"symbols": written, "note": "Symbols with a candle file. Others: chart via TradingView link."})
    # snapshot
    legacy_latest = site.legacy("latest") or {}
    snap = {"data_status": data_status, "regime": reg, "nifty": q.get("NIFTY"), "vix": q.get("INDIAVIX"), "usdinr": q.get("USDINR"),
            "indices": {k: q[k] for k in ["NIFTY", "BANKNIFTY", "FINNIFTY", "NIFTYMIDCAP", "SENSEX", "SPX", "NDX", "IXIC", "DJI", "RUT", "FTSE", "DAX", "CAC", "STOXX50", "NIKKEI", "HSI", "SSE", "STI", "ASX200", "TSX", "KOSPI"] if k in q},
            "sectors": {k: q[k] for k in ["NIFTYIT", "NIFTYAUTO", "NIFTYPHARMA", "NIFTYFMCG", "NIFTYMETAL", "NIFTYENERGY", "NIFTYREALTY", "NIFTYPSUBANK", "NIFTYINFRA", "NIFTYMEDIA"] if k in q},
            "fx": {k: q[k] for k in ["USDINR", "EURINR", "EURUSD", "GBPUSD", "USDJPY", "DXY"] if k in q},
            "commodities": {k: q[k] for k in ["BRENT", "WTI", "NATGAS", "GOLD", "SILVER", "COPPER"] if k in q},
            "rates_vol": {k: q[k] for k in ["US10Y", "US2Y", "VIX", "INDIAVIX"] if k in q},
            "legacy_manual_snapshot": {"as_of": "2026-09-25", "vix": legacy_latest.get("vix"), "usdinr": legacy_latest.get("usdinr"), "brent_usd": 98, "brent_peak_note": "$108 on 10 Sep (reported)",
                                       "levels": site.legacy("levels"), "globalmkt": site.legacy("globalmkt"), "status": "MANUAL / HISTORICAL (user-entered 25-26 Sep 2026)"}}
    api.write(site.path, "snapshot.json", snap)
    # signals
    api.write(site.path, "signals.json", {**scan, "rejected_dq_count": len(rejected), "failed_download": M["failed"][:50], "universe_source": M["universe_source"]})
    api.write(site.path, "runs.json", {"runs": api.runs_index(site.path)})
    api.write(site.path, "signals_us.json", {**(scan_us or {"available": False, "note": "No US data this run (offline or feed failure)."})})
    om = {"signal": opt_sig, "params": OPT.PARAMS, "contracts": OPT.CONTRACTS, "contract_note": OPT.CONTRACT_NOTE, "models": {}}
    try:
        today = pd.Timestamp(reg["date"]).date()
        hol = list(cal.CALENDARS["NSE"]["holidays"].keys())
        for u in ("NIFTY", "BANKNIFTY"):
            if u in frames and len(frames[u]) > 30:
                om["models"][u] = OPT.model_chain(u, frames, today, hol)
    except Exception as e:  # noqa
        om["error"] = str(e)
    try:
        today_ = pd.Timestamp(reg["date"]).date()
        acc_o = L.state["accounts"].get("IN-OPTIONS", {})
        S_ = float(frames["NIFTY"]["Close"].iloc[-1]); sig_, _ = OPT.vol_input("NIFTY", frames)
        om["margin"] = [{"symbol": sym, **MG.spread_margin(p_["spread"], float(p_["qty"]), S_, sig_, today_)} for sym, p_ in acc_o.get("positions", {}).items() if p_.get("spread")]
        om["margin_examples"] = {"short_straddle_1_lot_atm": MG.estimate([{"kind": "C", "strike": round(S_ / 50) * 50, "expiry": om["models"]["NIFTY"]["chains"][0]["expiry"], "qty": -65},
                                                                           {"kind": "P", "strike": round(S_ / 50) * 50, "expiry": om["models"]["NIFTY"]["chains"][0]["expiry"], "qty": -65}], S_, sig_, today_)} if "NIFTY" in om["models"] else {}
    except Exception as e:  # noqa
        om["margin_error"] = str(e)
    api.write(site.path, "options_model.json", om)
    intel = intel or {}
    try:
        intel["execution"] = EX.report(L.state["fills"], frames, {})
    except Exception as e:  # noqa
        intel["execution"] = {"rows": [], "error": str(e)}
    old_fc = site.load_json("api/intel.json", {}).get("forecast_history", [])
    fc = intel.get("forecast") or {}
    if fc.get("available"):
        old_fc = [h for h in old_fc if h.get("for_session_after") != fc["for_session_after"]] + [{k: fc.get(k) for k in ("for_session_after", "p_up", "lean", "last_close")}]
        nifty_c = frames["NIFTY"]["Close"] if "NIFTY" in frames else None
        if nifty_c is not None:
            idx_d = [str(d.date()) for d in nifty_c.index]
            for h in old_fc:
                if h.get("actual") is None and h["for_session_after"] in idx_d:
                    i = idx_d.index(h["for_session_after"])
                    if i + 1 < len(idx_d):
                        h["actual_pct"] = round(float(nifty_c.iloc[i + 1] / nifty_c.iloc[i] - 1) * 100, 2); h["actual"] = "UP" if h["actual_pct"] > 0 else "DOWN"
                        h["correct"] = (h["lean"] == h["actual"]) if h["lean"] in ("UP", "DOWN") else None
    intel["forecast_history"] = old_fc[-120:]
    intel["events"] = scan.get("events")
    write_feeds(site, offline)
    try:
        news_items = (site.load_json("api/news.json", {}).get("live") or {}).get("items", [])
        stk = {k: v for k, v in clean.items() if k in master.index and master.loc[k, "kind"] == "stock" and master.loc[k, "currency"] == "INR"}
        bk = IM.baskets(stk, M.get("sectors") or {}) if stk else {}
        if bk:
            json.dump({k: {"d": [str(d.date()) for d in v.index], "c": [round(float(x), 3) for x in v["Close"]]} for k, v in bk.items()},
                      open(os.path.join(site.data, "sector_baskets.json"), "w"))
        intel["impact"] = IM.run(frames, news_items, bk)
        if not offline:
            n4 = NOTIFY.Notifier(site.path)
            for sh in intel["impact"]["shocks"][:4]:
                n4.push(f"Global shock: {sh['label']} {sh['move']:+.2f}{sh['unit']}", sh["text"], key=f"shock|{sh['driver']}|{sh['date']}", tags=("ocean",), priority=4, click=NOTIFY.PORTAL + "#intel")
            n4.save()
    except Exception as e:  # noqa
        intel["impact"] = {"table": [], "shocks": [], "error": str(e)}
    api.write(site.path, "intel.json", intel)
    # paper + analytics
    pj = api.paper_json(L)
    pj["last_actions"] = actions
    bench = frames["NIFTY"]["Close"] if "NIFTY" in frames else None
    bench = bench.tz_localize(None) if bench is not None and bench.index.tz is not None else bench
    for acc in pj["accounts"]:
        trades = [t for t in L.state["trades"] if t["account"] == acc["account"]]
        acc["analytics"] = AN.full(L.account(acc["account"])["equity"], trades, acc["start_cash"], bench if acc["currency"] == "INR" else None)
        try:
            acc["risk"] = AN.portfolio_risk(L.account(acc["account"])["equity"], L.account(acc["account"])["positions"], frames, float(acc["equity"]),
                                            "NIFTY" if acc["currency"] == "INR" else "SPX")
        except Exception as e:  # noqa
            acc["risk"] = {"error": str(e)}
    api.write(site.path, "paper.json", pj)
    # health
    tests = run_tests()
    dq_summary = {"symbols_ok": len(clean), "symbols_rejected": len(rejected), "symbols_failed_download": len(M["failed"]), "rejected": rejected,
                  "nifty": dqrep.get("NIFTY"), "checked_at": api.now(),
                  "nifty_history_note": "Portal copy of the course CSV (1,970 rows, 14 Sep 2018 - 11 Sep 2026, no Volume column). 12-24 Sep 2026 missing and NOT filled; 25 Sep close entered manually."}
    workflow = {"eod": "weekdays 16:20 IST (GitHub Actions cron 10:50 UTC)", "intraday": "every 15 min 09:15-15:30 IST weekdays (quotes + marks only)", "weekend": "Saturday 09:00 IST",
                "runner": "GitHub Actions (free)", "offline": offline}
    api.write(site.path, "health.json", api.health_json(site.path, M["providers"], dq_summary, tests, workflow, data_status,
              {"holiday_lists_verified": {k: v["verified"] for k, v in cal.CALENDARS.items()}}))
    api.write(site.path, "calendar.json", {"markets": cal.all_status(), "holidays": {k: v["holidays"] for k, v in cal.CALENDARS.items() if v["holidays"]},
              "verified": {k: v["verified"] for k, v in cal.CALENDARS.items()}, "sources": {k: v["holiday_source"] for k, v in cal.CALENDARS.items()}})
    # research: strategies, backtests, correlations, graph
    bts = backtests(site, frames.get("NIFTY"), frames)
    api.write(site.path, "strategies.json", api.strategies_json({("ma_cross_21_50" if k == "ma_cross_21_50" else k): v.get("summary") for k, v in bts.items() if isinstance(v, dict)}))
    api.write(site.path, "backtests.json", {"results": bts, "reported_v1": {"buy_and_hold": 406, "ma_21_50_long": 342, "ma_21_50_long_short": 267, "ma_9_21_long_short": -101,
              "note": "Figures reported in the v1 Research tab, computed elsewhere from the same CSV; reproduced independently below with documented costs."}})
    closes = pd.DataFrame({k: frames[k]["Close"] for k in ["NIFTY", "BANKNIFTY", "SPX", "NDX", "NIKKEI", "HSI", "FTSE", "DAX", "BRENT", "GOLD", "USDINR", "US10Y", "INDIAVIX", "VIX", "GOLDBEES", "NIFTYBEES"] if k in frames})
    corr = KG.correlations(closes) if len(closes.columns) > 1 else {"windows": {}, "note": "needs at least two series (offline)"}
    api.write(site.path, "correlations.json", {**corr, "betas_vs_nifty": KG.betas(closes) if "NIFTY" in closes else {}})
    api.write(site.path, "graph.json", {"edges": KG.seed_graph(), "event_types": KG.EVENT_TYPES, "note": "Documented mechanisms + statistical edges. Not causal proof."})
    # news, flows, options, ipos: written above (write_feeds) before the impact map
    api.write(site.path, "brokers.json", api.brokers_json())
    api.write(site.path, "instruments.json", {"count": len(master), "instruments": [r.to_dict() for _, r in master.iterrows()]})
    # reports
    rq = {**q}
    rep = {"post_market": R.post_market(reg, scan, actions.get("IN-SWING", ["-"]), rq, dq_summary, data_status),
           "pre_market": R.pre_market(rq, cal.all_status(), reg), "legacy_weekend_scan": {**(site.legacy("scan") or {}), "kind": "HUMAN-AUTHORED, 26 Sep 2026 (historical)"}}
    if "NIFTY" in frames:
        wk = frames["NIFTY"]["Close"].resample("W-FRI").last().pct_change().dropna()
        ww = {k: float(frames[k]["Close"].resample("W-FRI").last().pct_change().dropna().iloc[-1] * 100) for k in ["SPX", "NDX", "NIKKEI", "BRENT", "GOLD"] if k in frames and len(frames[k]) > 10}
        rep["weekend"] = R.weekend(reg, rq, [L.summary(a) for a in L.state["accounts"]], scan, {"nifty_week_pct": round(float(wk.iloc[-1] * 100), 2) if len(wk) else None, "world_week_pct": ww})
    api.write(site.path, "reports.json", rep)
    api.write(site.path, "meta.json", {"data_status": data_status, "data_date": reg["date"], "regime": reg["state"], "offline": offline,
              "portal_note": "All numbers come from api/*.json written by the engine; the portal never invents data."})


def write_feeds(site, offline):
    legacy_news = site.legacy("news") or []
    out = {"legacy": {"as_of": "2026-09-25", "items": legacy_news, "headline_stat_pct": 33, "status": "MANUAL / HISTORICAL",
                      "note": "Seven headlines entered by hand on 25 Sep 2026; 33% = positive/(positive+negative), a descriptive count, not a model."}}
    if not offline:
        try:
            out["live"] = NEWS.market_news(); out["live"]["status"] = "FETCHED (Google News RSS, keyword sentiment)"
        except Exception as e:  # noqa
            out["live"] = {"status": f"failed: {e}", "items": []}
    else:
        out["live"] = {"status": "offline - not fetched", "items": []}
    api.write(site.path, "news.json", out)
    flows = {"legacy": {**(site.legacy("flows") or {}), "status": "MANUAL (NSE provisional via news reports)", "unit": "INR crore"}, "live": None}
    chain = {"legacy": {**(site.legacy("chain") or {}), "status": "PARTIAL - 5 strikes from a screenshot; NOT the full chain", "complete": False}, "live": None}
    if not offline:
        n = NSEWeb()
        try:
            flows["live"] = n.fii_dii(); flows["live"]["status"] = "FETCHED (NSE provisional)"
        except ProviderError as e:
            flows["live"] = {"status": str(e)}
        try:
            chain["live"] = n.option_chain("NIFTY"); chain["live"]["status"] = "FETCHED (NSE delayed)"
        except ProviderError as e:
            chain["live"] = {"status": str(e)}
    api.write(site.path, "flows.json", flows)
    api.write(site.path, "options.json", chain)
    ipos = site.legacy("ipos") or []
    api.write(site.path, "ipos.json", {"items": [{"name": r[0], "dates": r[1], "price_band": r[2], "size": r[3], "status_text": r[4], "verification": "unverified (HDFC Sky report, 25 Sep 2026)"} for r in ipos],
              "source": "HDFC Sky report via user, 25 Sep 2026", "note": "No GMP or subscription figures are shown because none were verified."})


def backtests(site, nifty, frames):
    """Independent reproduction of the v1 research + walk-forward. Cached per data date."""
    if nifty is None:
        return {}
    cache = os.path.join(site.data, f"backtests-{nifty.index[-1].date()}.json")
    if os.path.exists(cache):
        return json.load(open(cache))
    df = nifty.tz_localize(None) if nifty.index.tz is not None else nifty
    out = {}
    def pack(res, name, desc, params):
        eq = res["equity"]
        step = max(1, len(eq) // 400)
        return {"name": name, "description": desc, "params": params, "summary": res["metrics"],
                "equity": {"d": [str(d.date()) for d in eq.index[::step]], "v": [round(float(v), 0) for v in eq.values[::step]]},
                "trades": res["trades"][-60:], "marks": res["marks"][-120:]}
    bh = B.buy_and_hold(df, qty=75, segment="futures")
    out["buy_and_hold_75u"] = pack(bh, "Buy & hold 1 lot (75 units) NIFTY", "Hold from first bar to last; futures-style charges; spot as proxy", {"units": 75})
    out["ma_cross_21_50"] = pack(B.run(df, MACross(21, 50), qty=75, segment="futures", warmup=52), "21/50 SMA long only, 1 lot", "Course strategy; next-open fills, 0.05% slippage, F&O charges", {"fast": 21, "slow": 50, "units": 75})
    out["ma_cross_21_50_ls"] = pack(B.run(df, MACross(21, 50, True), qty=75, segment="futures", allow_short=True, warmup=52), "21/50 SMA long + short, 1 lot", "Shorts on the death cross", {"fast": 21, "slow": 50, "short": True})
    out["ma_cross_9_21_ls"] = pack(B.run(df, MACross(9, 21, True), qty=75, segment="futures", allow_short=True, warmup=23), "9/21 SMA long + short, 1 lot", "Pine-default lengths, SMA version", {"fast": 9, "slow": 21, "short": True})
    out["ma_cross_21_50_risk"] = pack(B.run(df, MACross(21, 50), qty_mode="all_in", segment="delivery", warmup=52), "21/50 SMA long only, cash account (NIFTYBEES-like, delivery charges)", "All-in cash sizing, no leverage", {"fast": 21, "slow": 50, "sizing": "all_in"})
    grid = [{"fast": f, "slow": s} for f in (9, 13, 21) for s in (34, 50, 100) if f < s]
    wf = B.walk_forward(df, lambda fast, slow: MACross(fast, slow), grid, train_years=3, test_years=1, qty=75, segment="futures", warmup=102)
    out["walk_forward_ma"] = {"name": "Walk-forward: MA crossover lengths", "description": "3-year train / 1-year test, anchored; best CAGR in-sample, evaluated out-of-sample", "summary": {k: v for k, v in wf.items() if k != "folds"}, "folds": wf["folds"]}
    out["seasonality"] = {"name": "Seasonality (monthly / weekday)", "summary": B.seasonality(df)}
    out["_note"] = "Independent reproduction. Differences from v1 figures come from cost model, slippage and next-open fills. No figure here is a forecast."
    with open(cache, "w") as f:
        f.write(json.dumps(api.clean(json.loads(json.dumps(out, default=api._enc))), allow_nan=False))
    return out


def run_tests():
    try:
        r = subprocess.run([sys.executable, "-m", "pytest", "-q", os.path.join(ROOT, "tests")], capture_output=True, text=True, timeout=600)
        tail = r.stdout.strip().splitlines()[-1] if r.stdout.strip() else r.stderr[-300:]
        return {"passed": r.returncode == 0, "summary": tail, "ran_at": api.now()}
    except Exception as e:  # noqa
        return {"passed": False, "summary": f"could not run: {e}", "ran_at": api.now()}


def job_intraday(site, offline=False, minutes=None, every=None):
    """LIVE session loop: quotes for everything, live paper fills (stocks + options), alerts, and a small publish every cycle.
    Runs one pass when no market is open. See engine/livefeed.py for the rules."""
    if offline:
        log("intraday: offline, nothing to do"); return
    minutes = float(os.environ.get("LIVE_MINUTES") or (minutes if minutes is not None else 24))
    every = float(os.environ.get("LIVE_EVERY_SECONDS") or (every if every is not None else 120))
    t_end = time.time() + minutes * 60
    token, repo = os.environ.get("LIVE_PUSH_TOKEN"), os.environ.get("GITHUB_REPOSITORY")
    L = site.ledger()
    nse = INS.load_nifty500(fallback_file=os.path.join(ROOT, "stocks.txt"))[0]
    master = INS.master(pd.concat([nse, INS.us_equity_rows()], ignore_index=True))
    y = provider("yahoo")
    tel = TEL.get(site.path)
    ex_of = lambda i: master.loc[i, "calendar"] if i in master.index else "NSE"
    kind_of = lambda i: ("stock_nse" if master.loc[i, "currency"] == "INR" else "stock") if i in master.index and master.loc[i, "kind"] in ("stock", "etf") else "index"
    angel = provider("angelone")
    angel_on = angel.configured()
    if angel_on:
        try:
            t_ = time.time(); angel.login(); tel.call("angelone", True, time.time() - t_)
            wanted = {i: master.loc[i, "angel"] for i in master.index if master.loc[i, "calendar"] == "NSE" and isinstance(master.loc[i, "angel"], str)}
            angel.load_tokens(wanted)
            tel.provider_status("angelone", tokens_mapped=len(angel.tokens or {}), status="CONNECTED")
        except Exception as e:  # noqa
            angel_on = False
            tel.call("angelone", False, error=e); tel.provider_status("angelone", status="LOGIN FAILED - using delayed feed")
            log("Angel One unavailable, continuing on the delayed feed:", TEL.scrub(e))
    else:
        tel.provider_status("angelone", status="AWAITING SECRETS", configured=False)
    cycles = 0
    while True:
        cycles += 1
        t0 = time.time()
        st = cal.all_status()
        open_mk = {k for k in ("NSE", "US") if st.get(k, {}).get("state") == "OPEN"}
        q = site.load_json("api/quotes.json", {"quotes": {}})
        q.setdefault("quotes", {})
        held = set()
        for acc in L.state["accounts"].values():
            held |= {s_ for s_, p_ in acc["positions"].items() if not p_.get("spread")}
        for o in L.state["orders"]:
            if o["status"] in ("PENDING", "PARTIAL") and o.get("segment") != "options":
                held.add(o["symbol"])
        core = [i for i in master.index if master.loc[i, "source"] == "core" and isinstance(master.loc[i, "yahoo"], str)]
        tick5 = {i: master.loc[i, "yahoo"] for i in core}
        for h_ in held:
            tick5[h_] = master.loc[h_, "yahoo"] if h_ in master.index else h_ + ".NS"
        rejected, n_ok = [], 0
        t_ = time.time()
        got, bad = y.candles_many(list(tick5.values()), "5m", "5d")
        tel.call("yahoo", len(got) > 0, time.time() - t_, error=f"{len(bad)} of {len(tick5)} tickers returned nothing" if bad else None)
        for i, t in tick5.items():
            df = got.get(t)
            if df is None or len(df) < 2:
                continue
            old = q["quotes"].get(i, {})
            n_ok += QA.accept(q["quotes"], i, QA.normalise(LF.quote_from_5m(df, old), i, ex_of(i), "yahoo", 15), kind_of(i), rejected)
        if angel_on and "NSE" in open_mk:
            try:
                t_ = time.time()
                aq, aerr = angel.quotes([i for i in (angel.tokens or {}) if i in tick5 or i in held])
                tel.call("angelone", bool(aq), time.time() - t_, error="; ".join(aerr[:3]) if aerr else None, rate_limited="rate limited" in aerr)
                for i, a_ in aq.items():
                    n_ok += QA.accept(q["quotes"], i, QA.normalise(a_, i, "NSE", "angelone", 0), kind_of(i), rejected)
            except Exception as e:  # noqa
                tel.call("angelone", False, error=e)
        # every stock in the open market(s): today's running daily bar
        univ = []
        if "NSE" in open_mk:
            univ += [i for i in master.index if master.loc[i, "kind"] == "stock" and master.loc[i, "currency"] == "INR" and i not in tick5]
        if "US" in open_mk:
            univ += [i for i in master.index if master.loc[i, "kind"] == "stock" and master.loc[i, "currency"] == "USD" and i not in tick5]
        n_univ = 0
        if univ:
            td = {i: master.loc[i, "yahoo"] for i in univ if isinstance(master.loc[i, "yahoo"], str)}
            t_ = time.time()
            gd, bad_d = y.candles_many(list(td.values()), "1d", "5d")
            tel.call("yahoo", len(gd) > 0, time.time() - t_, error=f"{len(bad_d)} of {len(td)} tickers returned nothing" if bad_d else None)
            from zoneinfo import ZoneInfo
            today_mk = {"INR": dt.datetime.now(IST).date(), "USD": dt.datetime.now(ZoneInfo("America/New_York")).date()}
            for i, t in td.items():
                df = gd.get(t)
                if df is None or len(df) < 2:
                    continue
                if df.index[-1].date() != today_mk[master.loc[i, "currency"]]:
                    continue                                  # no bar for today yet
                old = q["quotes"].get(i, {})
                if QA.accept(q["quotes"], i, QA.normalise(LF.quote_from_daily(df, old), i, ex_of(i), "yahoo", 15), kind_of(i), rejected):
                    n_univ += 1; n_ok += 1
        now = api.now()
        q["note"] = ("Session refresh every ~2 min. Yahoo (free) quotes are ~15 min behind the exchange and labelled DELAYED; "
                     + ("Angel One realtime quotes (src=angelone) override them for NSE symbols." if angel_on else
                        "no realtime feed is configured (add the ANGEL_* secrets for realtime NSE quotes)."))
        api.write(site.path, "quotes.json", {k: v for k, v in q.items() if k not in ("generated_at", "engine_version", "mode")})
        # ---- live paper trading
        today = dt.datetime.now(IST).date()
        acts = LF.paper_live(L, q["quotes"], open_mk, simulator, today)
        if "NSE" in open_mk:
            oa = LF.options_live(L, "IN-OPTIONS", q["quotes"], OPT, today, str(today))
            if oa:
                acts.setdefault("IN-OPTIONS", []).extend(oa)
        for aid in L.state["accounts"]:
            acc = L.account(aid)
            if ("US" if acc["currency"] == "USD" else "NSE") not in open_mk:
                continue                                        # only mark accounts whose market is trading now
            prices = {s_: q["quotes"][s_]["p"] for s_, p_ in acc["positions"].items() if s_ in q["quotes"] and not p_.get("spread") and q["quotes"][s_].get("d") == str(today)}
            prices.update({s_: float(p_["last"]) for s_, p_ in acc["positions"].items() if p_.get("spread")})
            L.mark(aid, prices, str(today))
        L.save()
        pj = api.paper_json(L)
        prev_pj = site.load_json("api/paper.json", {})
        pj["last_actions"] = prev_pj.get("last_actions", {})
        prev_acc = {x.get("account"): x for x in prev_pj.get("accounts", [])}
        for acc_ in pj["accounts"]:                       # keep the evening analytics / risk blocks; values above are live
            for k_ in ("analytics", "risk"):
                if k_ in prev_acc.get(acc_["account"], {}):
                    acc_[k_] = prev_acc[acc_["account"]][k_]
        for k_ in prev_pj:
            if k_ not in pj and k_ not in ("generated_at", "engine_version", "mode"):
                pj[k_] = prev_pj[k_]
        live_log = (prev_pj.get("live_actions") or [])
        for aid, al in acts.items():
            for a_ in al:
                live_log.append({"time": now, "account": aid, "action": a_})
        pj["live_actions"] = live_log[-200:]
        pj["live_updated_at"] = now
        api.write(site.path, "paper.json", pj)
        snap = site.load_json("api/snapshot.json", {})
        for k in ("generated_at", "engine_version", "mode"):
            snap.pop(k, None)
        for grp in ("indices", "fx", "commodities", "rates_vol", "sectors"):
            for k in list(snap.get(grp, {}) or {}):
                if k in q["quotes"]:
                    snap[grp][k] = q["quotes"][k]
        snap["nifty"] = q["quotes"].get("NIFTY", snap.get("nifty")); snap["vix"] = q["quotes"].get("INDIAVIX", snap.get("vix")); snap["usdinr"] = q["quotes"].get("USDINR", snap.get("usdinr"))
        api.write(site.path, "snapshot.json", snap)
        api.write(site.path, "calendar.json", {**{k: v for k, v in site.load_json("api/calendar.json", {}).items() if k not in ("generated_at", "engine_version", "mode")}, "markets": st})
        # ---- alerts
        try:
            n = NOTIFY.Notifier(site.path)
            day = today.isoformat()
            for aid, al in acts.items():
                for a_ in al:
                    if a_.startswith(("BUY", "SELL", "EXIT")):
                        n.push(f"Paper {aid} (live)", a_, key=f"live|{aid}|{a_}", tags=("memo",), priority=4, click=NOTIFY.PORTAL + "#paper")
            for k, lim in (("NIFTY", C.ALERTS["index_move_pct"]), ("BANKNIFTY", C.ALERTS["index_move_pct"]), ("SPX", C.ALERTS["index_move_pct"]),
                           ("INDIAVIX", C.ALERTS["vix_move_pct"]), ("BRENT", 3.0), ("USDINR", 0.5), ("GOLD", 2.0)):
                x = q["quotes"].get(k)
                if x and x.get("intraday") and x.get("chg_pct") is not None and abs(x["chg_pct"]) >= lim and x.get("d") == day:
                    band = int(abs(x["chg_pct"]) // lim)
                    n.push(f"{x.get('name', k)} {x['chg_pct']:+.2f}% today", f"{k} {x['p']:,} (prev {x['pc']:,}) · delayed ~15 min",
                           key=f"move|{k}|{day}|{band}|{'up' if x['chg_pct'] > 0 else 'dn'}", tags=("warning",), priority=4, click=NOTIFY.PORTAL + "#markets")
            check_rules(n, q["quotes"], day)
            n.save()
        except Exception as e:  # noqa
            log("live alerts failed:", e)
        open_cals = {k for k, v in st.items() if v.get("state") == "OPEN"}
        status = {"validation": {"accepted": n_ok, "rejected": len(rejected), "rejected_sample": rejected[:10]},
                  "freshness": QA.summary(q["quotes"], open_cals), "realtime_provider": "angelone" if angel_on else None,
                  "cycle": cycles, "updated_at": now, "open_markets": sorted(open_mk), "quotes_5m": len(got), "stocks_live": n_univ,
                  "paper_actions_this_cycle": sum(len(v) for v in acts.values()), "every_seconds": every,
                  "note": "Free data is ~15 minutes behind the exchange. Real-time needs a broker data feed (see Live Desk setup)."}
        api.write(site.path, "live_status.json", status)
        job_ = tel.active
        if job_:
            job_.processed += n_ok; job_.failed += len(rejected) + len(bad)
        tel.save()
        pub = LF.publish(site.path, token, repo)
        log(f"live cycle {cycles}: markets {sorted(open_mk) or 'closed'} · 5m {len(got)} · stocks {n_univ} · actions {status['paper_actions_this_cycle']} · {pub}")
        if not open_mk or time.time() + every > t_end:
            break
        if LF.eod_waiting(repo, token):
            log("end-of-day run is waiting - ending the live session early so it can publish")
            break
        time.sleep(max(5, every - (time.time() - t0)))
    log(f"live session done after {cycles} cycle(s)")


def job_premarket(site, offline=False):
    """08:15 IST: US/Asia closes are in -> next-day forecast with complete overnight data + pre-market report + push."""
    M = load_market(site, offline, universe_limit=0) if offline else None
    if not offline:
        master = INS.master()
        y = provider("yahoo")
        core = [r for _, r in master.iterrows() if r.source == "core" and isinstance(r.yahoo, str)]
        got, bad = y.candles_many([r.yahoo for r in core], "1d", "5y")
        frames = {r.id: dq.clean(got[r.yahoo]) for r in core if r.yahoo in got}
        try:
            NSEEOD.patch(frames, [k for k in frames if k in master.index and master.loc[k, "currency"] == "INR" and master.loc[k, "calendar"] == "NSE"],
                         list(cal.CALENDARS["NSE"]["holidays"].keys()))
        except Exception as e:  # noqa
            log("NSE EOD patch failed:", e)
    else:
        frames = M["frames"]
    if "NIFTY" not in frames:
        log("premarket: no NIFTY"); return
    fc = FC.run(frames)
    intel = site.load_json("api/intel.json", {})
    for k in ("generated_at", "engine_version", "mode"):
        intel.pop(k, None)
    fc["us_close_included"] = bool("SPX" in frames and frames["SPX"].index[-1].date() >= frames["NIFTY"].index[-1].date())
    fc["made_at"] = "pre-market"
    intel["forecast"] = fc
    try:
        news_items = (site.load_json("api/news.json", {}).get("live") or {}).get("items", [])
        bk = {}
        try:
            for k, v in json.load(open(os.path.join(site.data, "sector_baskets.json"))).items():
                bk[k] = pd.DataFrame({"Close": v["c"]}, index=pd.to_datetime(v["d"]))
        except Exception:
            pass
        intel["impact"] = IM.run(frames, news_items, bk)
        intel["impact"]["made_at"] = "pre-market"
        if not offline:
            n0 = NOTIFY.Notifier(site.path)
            for sh in intel["impact"]["shocks"][:4]:
                n0.push(f"Overnight shock: {sh['label']} {sh['move']:+.2f}{sh['unit']}", sh["text"], key=f"shock|{sh['driver']}|{sh['date']}", tags=("ocean",), priority=4, click=NOTIFY.PORTAL + "#intel")
            n0.save()
    except Exception as e:  # noqa
        log("premarket impact failed:", e)
    hist = intel.get("forecast_history", [])
    if fc.get("available"):
        hist = [h for h in hist if h.get("for_session_after") != fc["for_session_after"]] + [{k: fc.get(k) for k in ("for_session_after", "p_up", "lean", "last_close")}]
    intel["forecast_history"] = hist[-120:]
    api.write(site.path, "intel.json", intel)
    q = api.quotes_from_frames(frames, INS.master())
    reg = site.load_json("api/signals.json", {}).get("regime") or P.regime(frames["NIFTY"])
    rep = site.load_json("api/reports.json", {})
    for k in ("generated_at", "engine_version", "mode"):
        rep.pop(k, None)
    rep["pre_market"] = R.pre_market(q, cal.all_status(), reg)
    if fc.get("available"):
        rep["pre_market"]["lines"].insert(1, f"Next-session model: P(up) {fc['p_up']:.0%} ({fc['lean']}); 1-sd range {fc['range_1sd'][0]:,.0f}-{fc['range_1sd'][1]:,.0f}; out-of-sample hit rate {fc['oos']['hit_rate']} vs baseline {fc['oos']['baseline_hit']}.")
    api.write(site.path, "reports.json", rep)
    if fc.get("available") and not offline:
        n = NOTIFY.Notifier(site.path)
        n.push(f"Pre-market: NIFTY lean {fc['lean']} ({fc['p_up']:.0%} up)", "\n".join(rep["pre_market"]["lines"][:4]) + ("\nModel has NO proven edge - context only." if not fc["oos"]["has_skill"] else ""),
               key=f"pm|{now_ist_date()}", tags=("sunrise",), click=NOTIFY.PORTAL + "#intel")
        n.save()
    log("premarket done", fc.get("lean"), fc.get("p_up"))


def check_rules(n, quotes, today):
    rules, bad = RULES.parse(os.environ.get("ALERT_RULES", ""))
    for h in RULES.evaluate(rules, quotes):
        n.push(f"Your alert: {h['text']}", f"{h['symbol']} {'change' if h['kind'] == 'change_pct' else 'price'} now {h['observed']} (rule {h['text']}) · delayed quote {h.get('quote_time') or ''}",
               key=f"rule|{h['text']}|{today}", tags=("bell",), priority=5, click=NOTIFY.PORTAL + "#alerts")
    return rules, bad


def now_ist_date():
    return dt.datetime.now(IST).date().isoformat()


def job_migrate(site):
    """Import v1 state so history is not lost."""
    L = site.ledger()
    p = os.path.join(ROOT, "data", "paper_log.csv")
    if os.path.exists(p):
        shutil.copy(p, os.path.join(site.data, "legacy", "v1_paper_log.csv"))
    L.state.setdefault("migrations", []).append({"at": api.now(), "from": "v1 (market-scanner main, portfolio.json absent; paper_log.csv empty)",
                                                 "note": "v1 auto paper account had cash 2,00,000 and 0 positions on 2026-09-25; IN-SWING starts from the same state."})
    L.save()
    log("migrated")


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("job", choices=["eod", "intraday", "weekend", "health", "migrate", "premarket"])
    ap.add_argument("--site", default="site")
    ap.add_argument("--offline", action="store_true")
    ap.add_argument("--limit", type=int, default=None, help="limit universe size (testing)")
    ap.add_argument("--no-news", action="store_true")
    a = ap.parse_args(argv)
    site = Site(a.site)
    tel = TEL.get(site.path)
    with tel.job(a.job, provider="yahoo" + ("+angelone" if a.job == "intraday" and provider("angelone").configured() else "")):
        _dispatch(a, site)


def _dispatch(a, site):
    if a.job in ("eod", "weekend"):
        job_eod(site, a.offline, a.limit, fetch_news=not a.no_news, weekend=a.job == "weekend")
    elif a.job == "premarket":
        job_premarket(site, a.offline)
    elif a.job == "intraday":
        job_intraday(site, a.offline)
    elif a.job == "health":
        api.write(site.path, "health.json", api.health_json(site.path, capabilities(), {}, run_tests(), {}, "UNKNOWN"))
    elif a.job == "migrate":
        job_migrate(site)


if __name__ == "__main__":
    main()
