"""
Automated reports: pre-market, post-market, weekend. Generated from data by rules;
labelled AUTOMATED OBSERVATION. No language model, no forecasts, no advice.
"""
import datetime as dt

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))


def _chg(q, k):
    x = q.get(k)
    return f"{x['name']} {x['p']:,} ({x['chg_pct']:+.2f}%, {x['d']})" if x else f"{k}: no data"


def _line(q, keys):
    return "; ".join(_chg(q, k) for k in keys if k in q)


def post_market(regime, scan, paper_actions, quotes, dq_summary, data_status):
    now = dt.datetime.now(IST)
    lines = [f"AUTOMATED POST-MARKET OBSERVATION - {now.strftime('%a %d %b %Y %H:%M')} IST. Data status: {data_status}.",
             f"NIFTY {regime['close']:,} on {regime['date']} - regime {regime['state']}: {regime['text']}",
             f"RSI {regime['rsi']}, 20-day realised vol {regime['vol20_annualised_pct']}% {'(high - sizes halved)' if regime['high_vol'] else ''}",
             f"Support {', '.join(f'{s:,}' for s in regime['support'])}; resistance {', '.join(f'{r:,}' for r in regime['resistance'])} (swing-point clusters, last 120 sessions).",
             f"Scanner: {scan['scanned']} stocks scored, {len(scan['buys'])} buy signals, {len(scan['sells'])} sell/exit signals, {len(scan['blocked'])} strong names blocked by rules.",
             "Paper account: " + " | ".join(paper_actions[:6]),
             "Cross-market: " + _line(quotes, ["SPX", "NDX", "NIKKEI", "BRENT", "GOLD", "USDINR", "US10Y", "INDIAVIX"]),
             f"Data quality: {dq_summary.get('symbols_ok', 0)} symbols clean, {dq_summary.get('symbols_rejected', 0)} rejected, {dq_summary.get('symbols_failed_download', 0)} failed to download.",
             "Invalidation: this observation is stale once a newer bar exists; nothing here is a forecast or advice."]
    return {"type": "post_market", "as_of": now.isoformat(timespec="seconds"), "data_date": regime["date"], "lines": lines, "kind": "AUTOMATED OBSERVATION"}


def pre_market(quotes, calendars, regime):
    now = dt.datetime.now(IST)
    lines = [f"AUTOMATED PRE-MARKET OBSERVATION - {now.strftime('%a %d %b %Y %H:%M')} IST.",
             "Overnight (last available closes): " + _line(quotes, ["SPX", "NDX", "DJI", "NIKKEI", "HSI", "BRENT", "GOLD", "USDINR", "US10Y", "VIX"]),
             f"Carry-over regime: {regime['state']} (from {regime['date']}). Levels: support {regime['support'][-1:] if regime['support'] else '-'}, resistance {regime['resistance'][:1] if regime['resistance'] else '-'}.",
             "Market status: " + "; ".join(f"{k} {v['state']}" for k, v in calendars.items() if k in ("NSE", "US", "UK", "JP", "HK")),
             "Reminder: quotes are end-of-day or delayed; GIFT Nifty is not available from the free feed."]
    return {"type": "pre_market", "as_of": now.isoformat(timespec="seconds"), "lines": lines, "kind": "AUTOMATED OBSERVATION"}


def weekend(regime, quotes, paper_summaries, scan, week_stats):
    now = dt.datetime.now(IST)
    lines = [f"AUTOMATED WEEKEND REVIEW - {now.strftime('%a %d %b %Y')}.",
             f"NIFTY week: {week_stats.get('nifty_week_pct', 'n/a')}% ; regime {regime['state']} on {regime['date']}.",
             "World week: " + "; ".join(f"{k} {v:+.2f}%" for k, v in week_stats.get("world_week_pct", {}).items()),
             "Paper accounts: " + " | ".join(f"{s['name']}: equity {s['equity']} ({s['return_pct']}%), {s['open_positions']} open, drawdown {s['drawdown_pct']}%" for s in paper_summaries),
             ("Strongest names by score: " + ", ".join("%s (%+d)" % (r["symbol"], r["score"]) for r in scan["table"][:8])) if scan.get("table") else "No scan table.",
             "Next week: check the calendar tab for holidays and events; the engine trades only when data is fresh.",
             "Nothing here is a forecast; scenario weights are not produced by this engine."]
    return {"type": "weekend", "as_of": now.isoformat(timespec="seconds"), "lines": lines, "kind": "AUTOMATED OBSERVATION"}
