"""
Macro indicators (free, official sources; each value carries its own date and source).
  US  : FRED (St. Louis Fed) CSV download, no key  - policy rate, CPI (YoY computed here), unemployment, 10y yield, 10y-2y spread
  India: World Bank API (annual)                   - CPI inflation, real GDP growth
Monthly/annual data move slowly; the "as of" date is shown so stale numbers are obvious.
"""
import io
import pandas as pd
import requests

UA = {"User-Agent": "Mozilla/5.0 (personal-scanner/2.3)"}
FRED = {"FEDFUNDS": ("US Fed funds rate", "%", "level"), "CPIAUCSL": ("US CPI inflation (YoY)", "%", "yoy"), "UNRATE": ("US unemployment", "%", "level"),
        "DGS10": ("US 10y Treasury", "%", "level"), "T10Y2Y": ("US 10y-2y spread", "pp", "level")}
WB = {"FP.CPI.TOTL.ZG": ("India CPI inflation (annual)", "%"), "NY.GDP.MKTP.KD.ZG": ("India real GDP growth (annual)", "%")}


def parse_fred(text, how):
    df = pd.read_csv(io.StringIO(text))
    df.columns = ["date", "v"]
    df["v"] = pd.to_numeric(df["v"], errors="coerce")
    df = df.dropna()
    df["date"] = pd.to_datetime(df["date"])
    s = df.set_index("date")["v"]
    if how == "yoy":
        s = (s / s.shift(12) - 1).dropna() * 100
    return s


def fred(sid, how):
    last = None
    for _ in range(2):
        try:
            r = requests.get(f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={sid}", headers=UA, timeout=45)
            r.raise_for_status()
            return parse_fred(r.text, how)
        except Exception as e:  # noqa
            last = e
    raise last


# ---- fallbacks when FRED is unreachable (it often times out from cloud runners) ----
BLS = {"CPIAUCSL": "CUUR0000SA0", "UNRATE": "LNS14000000"}


def parse_bls(j, how):
    data = j["Results"]["series"][0]["data"]
    rows = {pd.Timestamp(int(x["year"]), int(x["period"][1:]), 1): float(x["value"]) for x in data if x["period"].startswith("M") and x["period"] != "M13"}
    s = pd.Series(rows).sort_index()
    if how == "yoy":
        s = (s / s.shift(12) - 1).dropna() * 100
    return s


def bls(sid, how):
    r = requests.get(f"https://api.bls.gov/publicAPI/v2/timeseries/data/{BLS[sid]}", headers=UA, timeout=30)
    j = r.json()
    if j.get("status") != "REQUEST_SUCCEEDED":
        raise RuntimeError(f"BLS: {j.get('message')}")
    return parse_bls(j, how)


def nyfed_effr():
    r = requests.get("https://markets.newyorkfed.org/api/rates/unsecured/effr/last/500.json", headers=UA, timeout=30)
    rows = r.json()["refRates"]
    return pd.Series({pd.Timestamp(x["effectiveDate"]): float(x["percentRate"]) for x in rows}).sort_index()


def from_frames(sid, frames):
    def ser(k):
        c = frames[k]["Close"].copy(); c.index = pd.to_datetime(c.index.date); return c[~c.index.duplicated(keep="last")]
    if sid == "DGS10" and "US10Y" in frames:
        return ser("US10Y")
    if sid == "T10Y2Y" and "US10Y" in frames and "US2Y" in frames:
        return (ser("US10Y") - ser("US2Y")).dropna()
    raise RuntimeError("no market series")


def worldbank(ind):
    r = requests.get(f"https://api.worldbank.org/v2/country/IND/indicator/{ind}?format=json&per_page=15", headers=UA, timeout=25)
    j = r.json()
    rows = [x for x in (j[1] if isinstance(j, list) and len(j) > 1 and j[1] else []) if x.get("value") is not None]
    return pd.Series({pd.Timestamp(f"{x['date']}-12-31"): float(x["value"]) for x in rows}).sort_index()


def _monthly(s):
    try:
        return s.resample("ME").last()
    except Exception:
        return s.resample("M").last()


def run(frames=None):
    out = []
    for sid, (lab, unit, how) in FRED.items():
        try:
            src = "FRED"
            try:
                s = fred(sid, how)
            except Exception as e0:  # noqa
                if sid in BLS:
                    s, src = bls(sid, how), "BLS (FRED unreachable)"
                elif sid == "FEDFUNDS":
                    s, src = nyfed_effr(), "NY Fed EFFR (FRED unreachable)"
                elif frames:
                    s, src = from_frames(sid, frames), "Yahoo ^TNX / 2YY=F (FRED unreachable)"
                else:
                    raise e0
            before = s[s.index <= s.index[-1] - pd.Timedelta(days=365)]
            prev = before.iloc[-1] if len(before) else None
            out.append({"id": sid, "name": lab, "value": round(float(s.iloc[-1]), 2), "unit": unit, "as_of": str(s.index[-1].date()),
                        "prev_year": round(float(prev), 2) if prev is not None else None,
                        "spark": [round(float(v), 2) for v in _monthly(s).dropna().tail(36)], "source": src, "status": "FETCHED"})
        except Exception as e:  # noqa
            out.append({"id": sid, "name": lab, "status": f"unavailable: {str(e)[:120]}", "source": "FRED"})
    for ind, (lab, unit) in WB.items():
        try:
            s = worldbank(ind)
            out.append({"id": ind, "name": lab, "value": round(float(s.iloc[-1]), 2), "unit": unit, "as_of": str(s.index[-1].year),
                        "prev_year": round(float(s.iloc[-2]), 2) if len(s) > 1 else None, "spark": [round(float(v), 2) for v in s.tail(12)], "source": "World Bank", "status": "FETCHED (annual)"})
        except Exception as e:  # noqa
            out.append({"id": ind, "name": lab, "status": f"unavailable: {str(e)[:120]}", "source": "World Bank"})
    return {"items": out, "note": "Official releases with their own dates. Monthly/annual data: check 'as of'. India monthly CPI/IIP/RBI rates are not on a free API here; see the RBI and MoSPI sites."}
