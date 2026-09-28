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
import os, sys, io, json, glob, shutil, argparse, subprocess, datetime as dt, traceback
from decimal import Decimal
import pandas as pd
import numpy as np
from . import config as C, instruments as INS, calendar as cal, dq, api, pipeline as P, backtest as B, news as NEWS, knowledge as KG, reports as R, analytics as AN, costs
from .ledger import Ledger
from .providers import get as provider, capabilities, ProviderError
from .providers.stubs import ManualCSV, NSEWeb
from .strategies import get as get_strategy, catalogue
from .strategies.ma_cross import MACross

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
    master = INS.master(nse_df)
    sectors = dict(zip(nse_df.id, nse_df.sector))
    core = [r for _, r in master.iterrows() if r.source == "core" and isinstance(r.yahoo, str)]
    stocks = [r for _, r in master.iterrows() if r.source != "core"]
    ok = False
    try:
        got, bad = y.candles_many([r.yahoo for r in core], "1d", "2y")
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
    except Exception as e:  # noqa
        err = f"{type(e).__name__}: {e}"; log("Yahoo download failed:", err)
    prov_status.append({**y.capabilities(), "last_fetch_ok": ok, "last_error": err, "fetched": len(frames), "failed": len(failed)})
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
def job_eod(site, offline=False, universe_limit=None, fetch_news=True):
    log("EOD run start; mode", C.MODE, "offline" if offline else "online")
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
    # ---- paper trading
    L = site.ledger()
    actions = {}
    if stocks and data_status == "OK" and C.MODE == "PAPER":
        for aid, a in L.state["accounts"].items():
            if "trend_breakout_swing" in a["strategies"]:
                actions[aid] = P.paper_run(L, aid, stocks, bar_date, scan["buys"], reg, "trend_breakout_swing", data_status, M["sectors"])
    else:
        why = "offline" if not stocks else ("data STALE" if data_status != "OK" else f"mode {C.MODE}")
        for aid in L.state["accounts"]:
            prices = {s: float(frames[s]["Close"].iloc[-1]) for s in L.account(aid)["positions"] if s in frames}
            L.mark(aid, prices, bar_date)
            actions[aid] = [f"No trading this run ({why}); positions marked to market only."]
    L.save()
    # ---- run record (preserved forever)
    run_rec = {"engine_version": C.ENGINE_VERSION, "run_at": api.now(), "data_date": bar_date, "mode": C.MODE, "data_status": data_status,
               "regime": scan["regime"], "buys": scan["buys"], "sells": scan["sells"], "blocked": scan["blocked"], "scanned": scan["scanned"],
               "universe": scan["universe"], "universe_source": M["universe_source"], "strategy": scan["strategy"], "paper_actions": actions,
               "rejected_dq": rejected, "failed_download": M["failed"][:50], "skipped": dict(list(scan.get("skipped", {}).items())[:200])}
    with open(os.path.join(site.data, "runs", f"{bar_date}.json"), "w") as f:
        json.dump(run_rec, f, indent=1, default=api._enc)
    # ---- outputs
    write_common(site, M, clean, rejected, dqrep, reg, scan, L, actions, data_status, offline)
    log("EOD done:", reg["state"], len(scan["buys"]), "buys", len(scan["sells"]), "sells", scan["scanned"], "scanned")
    return run_rec


def write_common(site, M, clean, rejected, dqrep, reg, scan, L, actions, data_status, offline):
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
    top = sorted([k for k in frames if k not in core_ids], key=lambda k: -float((frames[k]["Close"] * frames[k]["Volume"]).tail(20).mean()))[:120]
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
            "fx": {k: q[k] for k in ["USDINR", "EURINR", "EURUSD", "GBPUSD", "USDJPY", "DXY"] if k in q},
            "commodities": {k: q[k] for k in ["BRENT", "WTI", "NATGAS", "GOLD", "SILVER", "COPPER"] if k in q},
            "rates_vol": {k: q[k] for k in ["US10Y", "US2Y", "VIX", "INDIAVIX"] if k in q},
            "legacy_manual_snapshot": {"as_of": "2026-09-25", "vix": legacy_latest.get("vix"), "usdinr": legacy_latest.get("usdinr"), "brent_usd": 98, "brent_peak_note": "$108 on 10 Sep (reported)",
                                       "levels": site.legacy("levels"), "globalmkt": site.legacy("globalmkt"), "status": "MANUAL / HISTORICAL (user-entered 25-26 Sep 2026)"}}
    api.write(site.path, "snapshot.json", snap)
    # signals
    api.write(site.path, "signals.json", {**scan, "rejected_dq_count": len(rejected), "failed_download": M["failed"][:50], "universe_source": M["universe_source"]})
    api.write(site.path, "runs.json", {"runs": api.runs_index(site.path)})
    # paper + analytics
    pj = api.paper_json(L)
    pj["last_actions"] = actions
    bench = frames["NIFTY"]["Close"] if "NIFTY" in frames else None
    bench = bench.tz_localize(None) if bench is not None and bench.index.tz is not None else bench
    for acc in pj["accounts"]:
        trades = [t for t in L.state["trades"] if t["account"] == acc["account"]]
        acc["analytics"] = AN.full(L.account(acc["account"])["equity"], trades, acc["start_cash"], bench if acc["currency"] == "INR" else None)
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
    # news, flows, options, ipos (live attempts with honest fallbacks)
    write_feeds(site, offline)
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
        json.dump(out, f, default=api._enc)
    return out


def run_tests():
    try:
        r = subprocess.run([sys.executable, "-m", "pytest", "-q", os.path.join(ROOT, "tests")], capture_output=True, text=True, timeout=600)
        tail = r.stdout.strip().splitlines()[-1] if r.stdout.strip() else r.stderr[-300:]
        return {"passed": r.returncode == 0, "summary": tail, "ran_at": api.now()}
    except Exception as e:  # noqa
        return {"passed": False, "summary": f"could not run: {e}", "ran_at": api.now()}


def job_intraday(site, offline=False):
    """Refresh quotes for core instruments + held/watched symbols with 5-minute bars; mark positions. No decisions."""
    if offline:
        log("intraday: offline, nothing to do"); return
    L = site.ledger()
    master = INS.master()
    y = provider("yahoo")
    held = set()
    for a in L.state["accounts"].values():
        held |= set(a["positions"].keys())
    ids = [i for i in master.index if master.loc[i, "source"] == "core" and isinstance(master.loc[i, "yahoo"], str)] + sorted(held)
    tick = {i: (master.loc[i, "yahoo"] if i in master.index else i + ".NS") for i in ids}
    got, bad = y.candles_many(list(tick.values()), "5m", "5d")
    q = site.load_json("api/quotes.json", {"quotes": {}})
    now = api.now()
    for i, t in tick.items():
        df = got.get(t)
        if df is None or len(df) < 2:
            continue
        day = df.index[-1].date()
        today = df[df.index.date == day]
        before = df[df.index.date < day]
        prev = q["quotes"].get(i, {}).get("pc") or (float(before["Close"].iloc[-1]) if len(before) else None)
        last = float(df["Close"].iloc[-1])
        q["quotes"][i] = {**q["quotes"].get(i, {}), "p": round(last, 2), "pc": round(prev, 2) if prev else None, "chg_pct": round((last / prev - 1) * 100, 2) if prev else None,
                          "t": df.index[-1].isoformat(), "d": str(day), "status": "DELAYED", "intraday": True,
                          "day_o": round(float(today["Open"].iloc[0]), 2), "day_h": round(float(today["High"].max()), 2), "day_l": round(float(today["Low"].min()), 2)}
        instr = master.loc[i].to_dict() if i in master.index else {"id": i, "name": i}
        api.candles_json(site.path, instr, df, {"provider": "yahoo", "ticker": t, "interval": "5m", "delay_minutes": 15, "licence": y.licence, "fetched_at_utc": now}, "5m")
    q["generated_at"] = now; q["note"] = "Intraday refresh: 5-minute bars, ~15 min delayed, from the free feed."
    api.write(site.path, "quotes.json", {k: v for k, v in q.items() if k not in ("generated_at", "engine_version", "mode")})
    for aid in L.state["accounts"]:
        prices = {s: q["quotes"][s]["p"] for s in L.account(aid)["positions"] if s in q["quotes"]}
        L.mark(aid, prices, dt.datetime.now(IST).date())
    L.save()
    pj = api.paper_json(L); pj["last_actions"] = site.load_json("api/paper.json", {}).get("last_actions", {})
    api.write(site.path, "paper.json", pj)
    snap = site.load_json("api/snapshot.json", {})
    for grp in ("indices", "fx", "commodities", "rates_vol"):
        for k in snap.get(grp, {}):
            if k in q["quotes"]:
                snap[grp][k] = q["quotes"][k]
    snap["nifty"] = q["quotes"].get("NIFTY", snap.get("nifty")); snap["vix"] = q["quotes"].get("INDIAVIX", snap.get("vix"))
    api.write(site.path, "snapshot.json", {k: v for k, v in snap.items() if k not in ("generated_at", "engine_version", "mode")})
    api.write(site.path, "calendar.json", {**{k: v for k, v in site.load_json("api/calendar.json", {}).items() if k not in ("generated_at", "engine_version", "mode")}, "markets": cal.all_status()})
    log("intraday refresh:", len(got), "series,", len(bad), "failed")


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
    ap.add_argument("job", choices=["eod", "intraday", "weekend", "health", "migrate"])
    ap.add_argument("--site", default="site")
    ap.add_argument("--offline", action="store_true")
    ap.add_argument("--limit", type=int, default=None, help="limit universe size (testing)")
    ap.add_argument("--no-news", action="store_true")
    a = ap.parse_args(argv)
    site = Site(a.site)
    if a.job in ("eod", "weekend"):
        job_eod(site, a.offline, a.limit, fetch_news=not a.no_news)
    elif a.job == "intraday":
        job_intraday(site, a.offline)
    elif a.job == "health":
        api.write(site.path, "health.json", api.health_json(site.path, capabilities(), {}, run_tests(), {}, "UNKNOWN"))
    elif a.job == "migrate":
        job_migrate(site)


if __name__ == "__main__":
    main()
