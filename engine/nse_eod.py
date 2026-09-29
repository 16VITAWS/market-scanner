"""
Official NSE end-of-day files used to FILL IN sessions the free Yahoo feed has not published yet.

  stocks / ETFs : sec_bhavdata_full_DDMMYYYY.csv   (open, high, low, close, total traded quantity)
  indices + VIX : ind_close_all_DDMMYYYY.csv       (open, high, low, close, volume)
Both appear on archives.nseindia.com around 18:00-19:00 IST. A patched bar is only ever ADDED after the last bar the
feed already has (never overwrites), and every patch is listed in the run's data-source note.
"""
import io, datetime as dt
import pandas as pd
from .flows import _get

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
INDEX_NAMES = {"Nifty 50": "NIFTY", "Nifty Bank": "BANKNIFTY", "Nifty Financial Services": "FINNIFTY", "Nifty Midcap 100": "NIFTYMIDCAP",
               "India VIX": "INDIAVIX", "Nifty IT": "NIFTYIT", "Nifty Auto": "NIFTYAUTO", "Nifty Pharma": "NIFTYPHARMA", "Nifty FMCG": "NIFTYFMCG",
               "Nifty Metal": "NIFTYMETAL", "Nifty Energy": "NIFTYENERGY", "Nifty Realty": "NIFTYREALTY", "Nifty PSU Bank": "NIFTYPSUBANK",
               "Nifty Infrastructure": "NIFTYINFRA", "Nifty Media": "NIFTYMEDIA"}


def _num(x):
    try:
        s = str(x).strip().replace(",", "")
        return float(s) if s not in ("", "-", "nan") else None
    except Exception:
        return None


def parse_stocks(text):
    df = pd.read_csv(io.StringIO(text), skipinitialspace=True)
    df.columns = [c.strip().upper() for c in df.columns]
    df["SERIES"] = df["SERIES"].astype(str).str.strip()
    out = {}
    for _, r in df[df["SERIES"].isin(["EQ", "BE", "BZ"])].iterrows():
        o, h, l, c, v = (_num(r.get(k)) for k in ("OPEN_PRICE", "HIGH_PRICE", "LOW_PRICE", "CLOSE_PRICE", "TTL_TRD_QNTY"))
        if None not in (o, h, l, c) and c > 0:
            out[str(r["SYMBOL"]).strip()] = (o, h, l, c, v or 0.0)
    return out


def parse_indices(text):
    df = pd.read_csv(io.StringIO(text))
    df.columns = [c.strip() for c in df.columns]
    out = {}
    for _, r in df.iterrows():
        pid = INDEX_NAMES.get(str(r["Index Name"]).strip())
        if not pid:
            continue
        o, h, l, c = (_num(r.get(k)) for k in ("Open Index Value", "High Index Value", "Low Index Value", "Closing Index Value"))
        v = _num(r.get("Volume")) or 0.0
        if None not in (o, h, l, c) and c > 0:
            out[pid] = (o, h, l, c, v)
    return out


def sessions_to_fill(last_dates, holidays, now=None, max_days=6):
    """Trading days after the OLDEST last bar among NSE frames, up to today (today only after 18:30 IST)."""
    now = now or dt.datetime.now(IST)
    if not last_dates:
        return []
    newest = max(last_dates)
    recent = [x for x in last_dates if x >= newest - dt.timedelta(days=10)]      # ignore long-dead series
    d = min(recent) + dt.timedelta(days=1)
    out = []
    while d <= now.date() and len(out) < max_days:
        if d.weekday() < 5 and str(d) not in holidays and (d < now.date() or now.time() >= dt.time(18, 30)):
            out.append(d)
        d += dt.timedelta(days=1)
    return out


def patch(frames, nse_ids, holidays, fetch=None, now=None):
    """frames: {id: df}; nse_ids: ids of NSE stocks/ETFs/indices eligible for patching. Returns a report dict."""
    fetch = fetch or _get
    lasts = {k: frames[k].index[-1] for k in nse_ids if k in frames and len(frames[k])}
    days = sessions_to_fill([t.date() for t in lasts.values()], set(holidays or []), now)
    report = {"days_checked": [str(d) for d in days], "patched": {}, "sources": {}}
    for d in days:
        rows = {}
        for kind, path, parser in (("stocks", f"/products/content/sec_bhavdata_full_{d.strftime('%d%m%Y')}.csv", parse_stocks),
                                   ("indices", f"/content/indices/ind_close_all_{d.strftime('%d%m%Y')}.csv", parse_indices)):
            try:
                txt, url = fetch(path)
                got = parser(txt)
                rows.update(got)
                report["sources"][f"{d} {kind}"] = f"FETCHED {len(got)}"
            except Exception as e:  # noqa
                report["sources"][f"{d} {kind}"] = f"unavailable: {str(e)[:80]}"
        n = 0
        for k, last in lasts.items():
            if k not in rows or frames[k].index[-1].date() >= d:
                continue
            o, h, l, c, v = rows[k]
            ts = frames[k].index[-1] + pd.Timedelta(days=(d - frames[k].index[-1].date()).days)
            add = pd.DataFrame({"Open": [o], "High": [h], "Low": [l], "Close": [c], "Volume": [v]}, index=pd.DatetimeIndex([ts]))
            for col in frames[k].columns:
                if col not in add.columns:
                    add[col] = None
            frames[k] = pd.concat([frames[k], add[frames[k].columns]])
            n += 1
        if n:
            report["patched"][str(d)] = n
    return report
