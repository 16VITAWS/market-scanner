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
    r = requests.get(f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={sid}", headers=UA, timeout=25)
    r.raise_for_status()
    return parse_fred(r.text, how)


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


def run():
    out = []
    for sid, (lab, unit, how) in FRED.items():
        try:
            s = fred(sid, how)
            before = s[s.index <= s.index[-1] - pd.Timedelta(days=365)]
            prev = before.iloc[-1] if len(before) else None
            out.append({"id": sid, "name": lab, "value": round(float(s.iloc[-1]), 2), "unit": unit, "as_of": str(s.index[-1].date()),
                        "prev_year": round(float(prev), 2) if prev is not None else None,
                        "spark": [round(float(v), 2) for v in _monthly(s).dropna().tail(36)], "source": "FRED", "status": "FETCHED"})
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
