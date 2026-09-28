"""
Daily NSE scanner.
Runs after market close, scores every stock with fixed rules, checks news,
writes a big-text dashboard (docs/index.html), logs paper trades, and sends
a Telegram message. It never places orders.
"""
import io, os, json, math, datetime as dt
import xml.etree.ElementTree as ET
from urllib.parse import quote

import numpy as np
import pandas as pd
import requests

import config as C
import paper
import dataquality as DQ

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
DOCS = os.path.join(HERE, "docs")
UA = {"User-Agent": "Mozilla/5.0 (compatible; personal-scanner/1.0)"}
REJECTED_DATA = {}   # symbol -> data problems; these are never traded


# ---------------------------------------------------------------- data
def load_universe():
    """Official Nifty 500 list from NSE; falls back to stocks.txt."""
    try:
        r = requests.get("https://archives.nseindia.com/content/indices/ind_nifty500list.csv",
                         headers=UA, timeout=20)
        r.raise_for_status()
        syms = pd.read_csv(io.StringIO(r.text))["Symbol"].dropna().str.strip().tolist()
        if len(syms) > 100:
            return syms, "Nifty 500 (official NSE list)"
    except Exception as e:
        print("Nifty 500 list download failed, using stocks.txt:", e)
    with open(os.path.join(HERE, "stocks.txt")) as f:
        syms = [l.strip() for l in f if l.strip() and not l.startswith("#")]
    return syms, "backup list (stocks.txt)"


def download(symbols):
    import yfinance as yf
    tickers = [s + ".NS" for s in symbols]
    raw = yf.download(tickers, period="2y", interval="1d", auto_adjust=True,
                      group_by="ticker", threads=True, progress=False)
    out = {}
    for s, t in zip(symbols, tickers):
        try:
            df = raw[t] if isinstance(raw.columns, pd.MultiIndex) else raw
            df = DQ.clean(df.dropna(subset=["Close"]))
            if len(df) >= 220:
                bad = DQ.check(df)
                if bad:
                    REJECTED_DATA[s] = bad
                else:
                    out[s] = df
        except KeyError:
            pass
    return out


def download_index():
    import yfinance as yf
    df = yf.download("^NSEI", period="2y", interval="1d", auto_adjust=True, progress=False)
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    return df.dropna(subset=["Close"])


# ---------------------------------------------------------------- indicators
def add_indicators(df):
    d = df.copy()
    c = d["Close"]
    for n in (21, 50, 200):
        d[f"sma{n}"] = c.rolling(n).mean()
    delta = c.diff()
    gain = delta.clip(lower=0).ewm(alpha=1/14, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1/14, adjust=False).mean()
    d["rsi"] = 100 - 100 / (1 + gain / loss.replace(0, np.nan))
    tr = pd.concat([d["High"] - d["Low"], (d["High"] - c.shift()).abs(),
                    (d["Low"] - c.shift()).abs()], axis=1).max(axis=1)
    d["atr"] = tr.ewm(alpha=1/14, adjust=False).mean()
    d["hi20"] = d["High"].rolling(20).max().shift(1)   # previous 20 days, no look-ahead
    d["lo20"] = d["Low"].rolling(20).min().shift(1)
    d["vol20"] = d["Volume"].rolling(20).mean().shift(1)
    d["turnover_cr"] = (c * d["Volume"]).rolling(20).mean() / 1e7
    return d


def market_mood(idx):
    d = add_indicators(idx)
    x = d.iloc[-1]
    vol = d["Close"].pct_change().tail(20).std() * math.sqrt(252) * 100
    if x.Close > x.sma200 and x.sma50 > x.sma200:
        state, text = "BULL", "Market is in an uptrend. Buys allowed."
    elif x.Close < x.sma200 and x.sma50 < x.sma200:
        state, text = "BEAR", "Market is in a downtrend. No new buys — protect your money."
    else:
        state, text = "SIDEWAYS", "Market has no clear direction. Only the strongest buys, smaller size."
    return {"state": state, "text": text, "close": round(float(x.Close), 2),
            "date": str(d.index[-1].date()), "vol": round(float(vol), 1),
            "high_vol": bool(vol > 25),
            "ret3m": float(d["Close"].iloc[-1] / d["Close"].iloc[-64] - 1)}


def score(df, mood):
    d = add_indicators(df)
    x, p = d.iloc[-1], d.iloc[-2]
    s, why = 0, []
    if x.Close > x.sma50 > x.sma200:
        s += 2; why.append("Uptrend (price above 50 and 200 day averages)")
    elif x.Close < x.sma50 < x.sma200:
        s -= 2; why.append("Downtrend (price below 50 and 200 day averages)")
    recent = d.tail(4)
    up = ((recent.sma21 > recent.sma50) & (recent.sma21.shift() <= recent.sma50.shift())).any()
    dn = ((recent.sma21 < recent.sma50) & (recent.sma21.shift() >= recent.sma50.shift())).any()
    if up: s += 1; why.append("21 day average just crossed above 50 day")
    if dn: s -= 1; why.append("21 day average just crossed below 50 day")
    if 50 <= x.rsi <= 70: s += 1; why.append(f"Healthy momentum (RSI {x.rsi:.0f})")
    elif 30 <= x.rsi < 45: s -= 1; why.append(f"Weak momentum (RSI {x.rsi:.0f})")
    elif x.rsi > 75: why.append(f"Overbought (RSI {x.rsi:.0f}) — don't chase")
    vol_ok = x.Volume > 1.5 * x.vol20
    if x.Close > x.hi20 and vol_ok: s += 2; why.append("Broke 20 day high on strong volume")
    if x.Close < x.lo20 and vol_ok: s -= 2; why.append("Broke 20 day low on strong volume")
    rel = d["Close"].iloc[-1] / d["Close"].iloc[-64] - 1 - mood["ret3m"]
    if rel > 0: s += 1; why.append(f"Beating Nifty by {rel*100:.1f}% over 3 months")
    else: s -= 1; why.append(f"Lagging Nifty by {abs(rel)*100:.1f}% over 3 months")
    return s, why, x


def plan(x):
    entry = float(x.Close)
    stop = entry - C.STOP_ATR * float(x.atr)
    target = entry + C.TARGET_ATR * float(x.atr)
    risk = entry - stop
    qty = int(C.CAPITAL * C.RISK_PER_TRADE // risk) if risk > 0 else 0
    qty = min(qty, int(C.CAPITAL // entry))
    if getattr(x, "vol20", None) and x.vol20 > 0:
        qty = min(qty, int(x.vol20 * C.MAX_VOLUME_SHARE))     # liquidity cap
    return {"entry": round(entry, 2), "stop": round(stop, 2), "target": round(target, 2), "qty": qty}


# ---------------------------------------------------------------- news
def headlines(sym, days=3):
    url = ("https://news.google.com/rss/search?q=" + quote(f"{sym} NSE share")
           + "&hl=en-IN&gl=IN&ceid=IN:en")
    try:
        r = requests.get(url, headers=UA, timeout=15)
        root = ET.fromstring(r.content)
    except Exception:
        return [], False
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)
    items, risk = [], False
    for it in root.iter("item"):
        title = it.findtext("title") or ""
        try:
            when = pd.to_datetime(it.findtext("pubDate"), utc=True)
        except Exception:
            continue
        if when < cutoff:
            continue
        items.append({"title": title, "link": it.findtext("link") or ""})
        if any(w in title.lower() for w in C.NEWS_RISK_WORDS):
            risk = True
        if len(items) >= 3:
            break
    return items, risk


# ---------------------------------------------------------------- paper log
LOG = os.path.join(DATA, "paper_log.csv")
LOG_COLS = ["date", "symbol", "entry", "stop", "target", "status", "exit_date", "result_pct"]


def update_paper(data, today_picks, scan_date):
    log = pd.read_csv(LOG, dtype=object).fillna("") if os.path.exists(LOG) else pd.DataFrame(columns=LOG_COLS, dtype=object)
    log = log.astype(object)
    for col in ("entry", "stop", "target"):
        log[col] = pd.to_numeric(log[col], errors="coerce")
    for i, r in log[log.status == "OPEN"].iterrows():
        df = data.get(r.symbol)
        if df is None:
            continue
        after = df[df.index > pd.Timestamp(r.date)]
        for day, bar in after.iterrows():
            if bar.Low <= r.stop:          # count stop first if both hit (conservative)
                log.loc[i, ["status", "exit_date", "result_pct"]] = ["LOSS", str(day.date()), str(round((r.stop / r.entry - 1) * 100, 2))]
                break
            if bar.High >= r.target:
                log.loc[i, ["status", "exit_date", "result_pct"]] = ["WIN", str(day.date()), str(round((r.target / r.entry - 1) * 100, 2))]
                break
    for p in today_picks:
        if not ((log.symbol == p["symbol"]) & (log.status == "OPEN")).any():
            log.loc[len(log)] = [str(scan_date), p["symbol"], p["entry"], p["stop"], p["target"], "OPEN", "", ""]
    log.to_csv(LOG, index=False)
    done = log[log.status.isin(["WIN", "LOSS"])]
    wins = int((done.status == "WIN").sum())
    return {"closed": len(done), "wins": wins, "open": int((log.status == "OPEN").sum()),
            "win_rate": round(wins / len(done) * 100, 1) if len(done) else None,
            "avg_pct": round(pd.to_numeric(done.result_pct).mean(), 2) if len(done) else None}


# ---------------------------------------------------------------- output
def tv(sym):
    return "https://www.tradingview.com/chart/?symbol=" + quote(f"NSE:{sym.replace('&', '_')}")


def esc(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;"))


def card(p, kind):
    reasons = "".join(f"<li>{esc(w)}</li>" for w in p["why"])
    news = "".join(f'<li><a href="{esc(n["link"])}">{esc(n["title"])}</a></li>' for n in p.get("news", []))
    news_block = f'<p class="small">Recent headlines (not verified):</p><ul class="small">{news}</ul>' if news else ""
    if kind == "buy":
        nums = (f'<div class="nums"><div><b>₹{p["entry"]:,}</b><span>Buy near</span></div>'
                f'<div><b>₹{p["stop"]:,}</b><span>Stop-loss</span></div>'
                f'<div><b>₹{p["target"]:,}</b><span>Target</span></div>'
                f'<div><b>{p["qty"]}</b><span>Shares</span></div></div>')
    else:
        nums = f'<div class="nums"><div><b>₹{p["entry"]:,}</b><span>Last price</span></div></div>'
    flag = '<p class="flag">News risk — check headlines before acting</p>' if p.get("news_risk") else ""
    return (f'<article class="card {kind}"><div class="head"><h3>{esc(p["symbol"])}</h3>'
            f'<span class="score">Score {p["score"]:+d}</span></div>{flag}{nums}'
            f'<ul class="why">{reasons}</ul>{news_block}'
            f'<a class="tvbtn" href="{tv(p["symbol"])}">Open chart in TradingView</a></article>')


def write_dashboard(ctx):
    m = ctx["mood"]
    mood_cls = {"BULL": "up", "BEAR": "down"}.get(m["state"], "flat")
    buys = "".join(card(p, "buy") for p in ctx["buys"]) or '<p class="empty">No buys today. Waiting is also a decision.</p>'
    sells = "".join(card(p, "sell") for p in ctx["sells"]) or '<p class="empty">No sell signals today.</p>'
    waits = "".join(f"<li>{esc(p['symbol'])} — {esc(p['reason'])}</li>" for p in ctx["waits"])
    pr = ctx["paper"]
    paper = (f"{pr['wins']} wins out of {pr['closed']} finished picks ({pr['win_rate']}%), average {pr['avg_pct']:+}% per pick. {pr['open']} still open."
             if pr["closed"] else f"No finished picks yet. {pr['open']} open. Results appear here as they finish.")
    pf, st = ctx["pf"], ctx["pstats"]
    value = pf["equity"][-1]["value"] if pf["equity"] else pf["cash"]
    ret = (value / pf["start"] - 1) * 100
    unreal = sum((p.get("last", p["entry"]) - p["entry"]) * p["qty"] for p in pf["positions"])
    realized = (st["net"] if st else 0)
    pos_rows = "".join(
        f'<article class="card {"buy" if p.get("last", p["entry"]) >= p["entry"] else "sell"}"><div class="head"><h3>{esc(p["symbol"])}</h3>'
        f'<span class="score">{p["qty"]} shares · day {p["days"]}</span></div>'
        f'<div class="nums"><div><b>₹{p["entry"]:,}</b><span>Bought</span></div><div><b>₹{p.get("last", p["entry"]):,}</b><span>Now</span></div>'
        f'<div><b>₹{(p.get("last", p["entry"]) - p["entry"]) * p["qty"]:,.0f}</b><span>Profit / loss</span></div>'
        f'<div><b>₹{p["stop"]:,}</b><span>Stop-loss</span></div><div><b>₹{p["target"]:,}</b><span>Target</span></div></div>'
        f'<p class="small">Why: {esc(p["reason"])}</p><a class="tvbtn" href="{tv(p["symbol"])}">Open chart in TradingView</a></article>'
        for p in pf["positions"]) or '<p class="empty">No open positions. Cash is waiting for a signal.</p>'
    acts = "".join(f"<li>{esc(a)}</li>" for a in ctx["actions"]) or "<li>Nothing to do today.</li>"
    pend = "".join(f"<li>{o['side'].upper()} {esc(o['symbol'])} at tomorrow's open — {esc(o['reason'])}</li>" for o in pf["pending"])
    if st:
        card_txt = (f"{st['n']} trades finished · {st['wins']} wins ({st['win_rate']}%) · net ₹{st['net']:,} after all charges · "
                    f"average win ₹{st['avg_win']:,} · average loss ₹{st['avg_loss']:,}" + (f" · profit factor {st['pf']}" if st['pf'] else ""))
    else:
        card_txt = "No trades finished yet. Numbers appear here as trades close."
    portfolio = f"""
<h2>Auto paper portfolio</h2>
<section class="mood {'up' if ret >= 0 else 'down'}"><h1>₹{value:,.0f}</h1><p>{ret:+.2f}% since start · cash ₹{pf['cash']:,.0f}</p>
<small>Realized ₹{realized:,.0f} · Unrealized ₹{unreal:,.0f} (not cash until sold). Started with ₹{pf['start']:,}. Orders fill at the next day's open with 0.1% slippage and full delivery charges.</small></section>
<h2>What it did today</h2><div class="box"><ul class="why">{acts}</ul>{('<p class="small">Waiting for tomorrow:</p><ul class="small">' + pend + '</ul>') if pend else ''}</div>
<h2>Open positions</h2>{pos_rows}
<h2>Report card</h2><div class="box"><p style="margin:0">{card_txt}</p></div>"""
    stale = '<p class="flag">Data is not from today — market holiday or data delay. Nothing new logged.</p>' if ctx["stale"] else ""
    html = f"""<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>My Market — {m['date']}</title>
<style>
:root{{--bg:#F3F5F4;--card:#fff;--ink:#11231F;--muted:#55675F;--up:#0B7A55;--down:#B3261E;--flat:#8A5A00;--line:#D5DDD9}}
@media (prefers-color-scheme:dark){{:root{{--bg:#0E1714;--card:#17231F;--ink:#EAF2EE;--muted:#9DB0A8;--up:#4CC99A;--down:#F28B82;--flat:#E6B85C;--line:#2B3A35}}}}
*{{box-sizing:border-box}} body{{margin:0;background:var(--bg);color:var(--ink);font:20px/1.5 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;
padding:env(safe-area-inset-top) 0 env(safe-area-inset-bottom)}}
main{{max-width:760px;margin:0 auto;padding:20px 16px 60px}}
.mood{{border-radius:20px;padding:24px;color:#fff;margin-bottom:28px}}
.mood.up{{background:var(--up)}} .mood.down{{background:var(--down)}} .mood.flat{{background:var(--flat)}}
.mood h1{{font-size:44px;margin:0;line-height:1.1}} .mood p{{margin:8px 0 0;font-size:22px}}
.mood small{{display:block;margin-top:10px;font-size:16px;opacity:.9}}
h2{{font-size:30px;margin:32px 0 12px}}
.card{{background:var(--card);border:1px solid var(--line);border-left:10px solid;border-radius:16px;padding:18px;margin-bottom:16px}}
.card.buy{{border-left-color:var(--up)}} .card.sell{{border-left-color:var(--down)}}
.head{{display:flex;justify-content:space-between;align-items:baseline;gap:10px}}
.head h3{{font-size:32px;margin:0}} .score{{font-size:18px;color:var(--muted)}}
.nums{{display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:10px;margin:14px 0}}
.nums b{{display:block;font-size:26px}} .nums span{{font-size:16px;color:var(--muted)}}
.why{{margin:8px 0;padding-left:22px}} .why li{{margin-bottom:4px}}
.small{{font-size:16px;color:var(--muted)}} .small a{{color:inherit}}
.flag{{background:#FFF1CC;color:#6B4B00;padding:8px 12px;border-radius:10px;font-weight:600}}
.tvbtn{{display:block;text-align:center;margin-top:12px;padding:16px;border-radius:12px;background:var(--ink);color:var(--bg);font-weight:700;text-decoration:none}}
.empty{{font-size:22px;color:var(--muted)}}
.box{{background:var(--card);border:1px solid var(--line);border-radius:16px;padding:18px}}
footer{{margin-top:32px;font-size:15px;color:var(--muted)}}
</style></head><body><main>
<p class="flag" style="background:#DCEFE8;color:#0B7A55">MODE: {ctx['mode']} · data as of {m['date']} ({m['data_age_days']} days old) · computed {m['computed_at'][11:16]} IST</p>
<section class="mood {mood_cls}"><h1>Market: {m['state']}</h1><p>{m['text']}</p>
<small>Nifty {m['close']:,} on {m['date']} · volatility {m['vol']}%{' (high — sizes halved)' if m['high_vol'] else ''}</small></section>
{stale}
{portfolio}
<h2>Buy ideas (signals)</h2>{buys}
<h2>Sell / exit if you hold</h2>{sells}
<h2>Signal report card</h2><div class="box"><p style="margin:0">{paper}</p></div>
{'<h2>Blocked by rules</h2><ul class="small">' + waits + '</ul>' if waits else ''}
<footer>Scanned {ctx['count']} stocks from the {ctx['source']} after market close. Rules-based signals, not investment advice. Check the chart and news before any order.</footer>
</main></body></html>"""
    os.makedirs(DOCS, exist_ok=True)
    with open(os.path.join(DOCS, "index.html"), "w", encoding="utf-8") as f:
        f.write(html)


def telegram(ctx):
    token, chat = os.environ.get("TELEGRAM_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat:
        print("Telegram not set up; skipping message.")
        return
    m = ctx["mood"]
    lines = [f"Market: {m['state']} — {m['text']}", f"Nifty {m['close']:,} ({m['date']})", ""]
    lines += ["AUTO PAPER TRADER:"] + [f"• {a}" for a in ctx["actions"]] + [""]
    lines.append("BUY:")
    lines += [f"• {p['symbol']} near ₹{p['entry']:,} | SL ₹{p['stop']:,} | T ₹{p['target']:,} | {p['qty']} sh"
              for p in ctx["buys"]] or ["• none today"]
    lines += ["", "SELL / EXIT:"] + ([f"• {p['symbol']}" for p in ctx["sells"]] or ["• none"])
    if os.environ.get("DASHBOARD_URL"):
        lines += ["", os.environ["DASHBOARD_URL"]]
    requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                  json={"chat_id": chat, "text": "\n".join(lines), "disable_web_page_preview": True}, timeout=20)


# ---------------------------------------------------------------- main
def run(data=None, idx=None, source=None, fetch_news=True, assume_fresh=False):
    if data is None:
        syms, source = load_universe()
        data = download(syms)
        idx = download_index()
    mood = market_mood(idx)
    today = dt.datetime.now(IST).date()
    age = DQ.age_days(idx, dt.datetime.now(IST).date())
    stale = (not assume_fresh) and (pd.Timestamp(mood["date"]).date() != today) and age > C.MAX_DATA_AGE_DAYS
    mood["computed_at"] = dt.datetime.now(IST).isoformat(timespec="seconds")
    mood["data_age_days"] = int(age)
    mood["mode"] = C.MODE
    size_mult = 0.5 if mood["high_vol"] else 1.0

    buys, sells, waits = [], [], []
    for sym, df in data.items():
        try:
            d = add_indicators(df)
            last = d.iloc[-1]
            if last.Close < C.MIN_PRICE or last.turnover_cr < C.MIN_TURNOVER_CR:
                continue
            s, why, x = score(df, mood)
        except Exception as e:
            print("skip", sym, e)
            continue
        if s >= C.BUY_SCORE:
            if mood["state"] == "BEAR":
                waits.append({"symbol": sym, "reason": f"score {s:+d}, but market is in a downtrend"})
                continue
            if mood["state"] == "SIDEWAYS" and s < C.BUY_SCORE + 1:
                waits.append({"symbol": sym, "reason": f"score {s:+d}, not strong enough for a sideways market"})
                continue
            p = {"symbol": sym, "score": s, "why": why, **plan(x)}
            p["qty"] = int(p["qty"] * size_mult)
            buys.append(p)
        elif s <= C.SELL_SCORE:
            sells.append({"symbol": sym, "score": s, "why": why, "entry": round(float(x.Close), 2)})

    buys = sorted(buys, key=lambda p: -p["score"])[:C.MAX_PICKS]
    sells = sorted(sells, key=lambda p: p["score"])[:C.MAX_PICKS]
    if fetch_news:
        for p in buys + sells:
            p["news"], p["news_risk"] = headlines(p["symbol"])
        risky = [p for p in buys if p["news_risk"]]
        for p in risky:
            waits.append({"symbol": p["symbol"], "reason": "news risk — check headlines first"})
        buys = [p for p in buys if not p["news_risk"]]

    os.makedirs(DATA, exist_ok=True)
    paper_stats = update_paper(data, [] if stale else buys, mood["date"])
    if stale:
        pf, actions = paper.load(), [f"Market data is {age} days old — STALE, report only, no trades."]
    elif C.MODE == "RESEARCH":
        pf, actions = paper.load(), ["MODE = RESEARCH — signals reported, nothing traded."]
    else:
        pf, actions = paper.run(data, mood, buys, mood["date"])
    ctx = {"mood": mood, "buys": buys, "sells": sells, "waits": waits, "paper": paper_stats,
           "pf": pf, "actions": actions, "pstats": paper.stats(),
           "count": len(data), "source": source, "stale": stale,
           "rejected_data": REJECTED_DATA, "mode": C.MODE}
    with open(os.path.join(DATA, f"signals_{mood['date']}.json"), "w") as f:
        json.dump(ctx, f, indent=1, default=str)
    write_dashboard(ctx)
    telegram(ctx)
    print(f"Done: {mood['state']}, {len(buys)} buys, {len(sells)} sells, {len(data)} stocks scanned.")
    return ctx


if __name__ == "__main__":
    run()
