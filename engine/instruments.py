"""
Symbol master. One record per instrument, with the ticker each provider uses,
currency, exchange, asset class, trading calendar and what the engine may do with it.

    can: A = analyse/chart, T = track in watchlists, P = paper trade, L = live (never here)
"""
import io, os, csv
import pandas as pd

INDIA_HOLIDAY_NOTE = "NSE calendar (see calendar.py)"

# ---- core static universe (always present, even offline) ------------------------
CORE = [
    # id, name, kind, exchange, currency, yahoo, twelvedata, angel, calendar, can, sector, lot
    ("NIFTY",      "NIFTY 50",             "index",  "NSE",   "INR", "^NSEI",     None,       "NIFTY",      "NSE",  "AT",  "Index", 25),
    ("BANKNIFTY",  "NIFTY Bank",           "index",  "NSE",   "INR", "^NSEBANK",  None,       "BANKNIFTY",  "NSE",  "AT",  "Index", 15),
    ("FINNIFTY",   "NIFTY Financial Svcs", "index",  "NSE",   "INR", "NIFTY_FIN_SERVICE.NS", None, "FINNIFTY", "NSE", "AT", "Index", 25),
    ("NIFTYMIDCAP","NIFTY Midcap 100",     "index",  "NSE",   "INR", "^CNXMIDCAP",None,       None,         "NSE",  "AT",  "Index", None),
    ("NIFTYIT", "NIFTY IT", "index", "NSE", "INR", "^CNXIT", None, None, "NSE", "AT", "Sector index", None),
    ("NIFTYAUTO", "NIFTY Auto", "index", "NSE", "INR", "^CNXAUTO", None, None, "NSE", "AT", "Sector index", None),
    ("NIFTYPHARMA", "NIFTY Pharma", "index", "NSE", "INR", "^CNXPHARMA", None, None, "NSE", "AT", "Sector index", None),
    ("NIFTYFMCG", "NIFTY FMCG", "index", "NSE", "INR", "^CNXFMCG", None, None, "NSE", "AT", "Sector index", None),
    ("NIFTYMETAL", "NIFTY Metal", "index", "NSE", "INR", "^CNXMETAL", None, None, "NSE", "AT", "Sector index", None),
    ("NIFTYENERGY", "NIFTY Energy", "index", "NSE", "INR", "^CNXENERGY", None, None, "NSE", "AT", "Sector index", None),
    ("NIFTYREALTY", "NIFTY Realty", "index", "NSE", "INR", "^CNXREALTY", None, None, "NSE", "AT", "Sector index", None),
    ("NIFTYPSUBANK", "NIFTY PSU Bank", "index", "NSE", "INR", "^CNXPSUBANK", None, None, "NSE", "AT", "Sector index", None),
    ("NIFTYINFRA", "NIFTY Infrastructure", "index", "NSE", "INR", "^CNXINFRA", None, None, "NSE", "AT", "Sector index", None),
    ("NIFTYMEDIA", "NIFTY Media", "index", "NSE", "INR", "^CNXMEDIA", None, None, "NSE", "AT", "Sector index", None),
    ("INDIAVIX",   "India VIX",            "index",  "NSE",   "INR", "^INDIAVIX", None,       "INDIAVIX",   "NSE",  "AT",  "Volatility", None),
    ("SENSEX",     "S&P BSE Sensex",       "index",  "BSE",   "INR", "^BSESN",    None,       None,         "NSE",  "AT",  "Index", None),
    ("NIFTYBEES",  "Nippon Nifty 50 ETF",  "etf",    "NSE",   "INR", "NIFTYBEES.NS","NIFTYBEES:NSE", "NIFTYBEES-EQ", "NSE", "ATP", "ETF", 1),
    ("GOLDBEES",   "Nippon Gold ETF",      "etf",    "NSE",   "INR", "GOLDBEES.NS","GOLDBEES:NSE",  "GOLDBEES-EQ",  "NSE", "ATP", "ETF", 1),
    ("BANKBEES",   "Nippon Bank ETF",      "etf",    "NSE",   "INR", "BANKBEES.NS", None,     None,         "NSE",  "ATP", "ETF", 1),
    ("USDINR",     "US Dollar / Rupee",    "fx",     "FX",    "INR", "INR=X",     "USD/INR",  None,         "FX",   "AT",  "Currency", None),
    ("EURINR",     "Euro / Rupee",         "fx",     "FX",    "INR", "EURINR=X",  "EUR/INR",  None,         "FX",   "AT",  "Currency", None),
    ("GBPUSD",     "Pound / Dollar",       "fx",     "FX",    "USD", "GBPUSD=X",  "GBP/USD",  None,         "FX",   "AT",  "Currency", None),
    ("EURUSD",     "Euro / Dollar",        "fx",     "FX",    "USD", "EURUSD=X",  "EUR/USD",  None,         "FX",   "AT",  "Currency", None),
    ("USDJPY",     "Dollar / Yen",         "fx",     "FX",    "JPY", "JPY=X",     "USD/JPY",  None,         "FX",   "AT",  "Currency", None),
    ("DXY",        "US Dollar Index",      "index",  "ICE",   "USD", "DX-Y.NYB",  None,       None,         "US",   "AT",  "Currency", None),
    ("BRENT",      "Brent crude (front)",  "commodity","ICE", "USD", "BZ=F",      None,       None,         "US",   "AT",  "Energy", None),
    ("WTI",        "WTI crude (front)",    "commodity","NYMEX","USD","CL=F",      None,       None,         "US",   "AT",  "Energy", None),
    ("NATGAS",     "Natural gas (front)",  "commodity","NYMEX","USD","NG=F",      None,       None,         "US",   "AT",  "Energy", None),
    ("GOLD",       "Gold (front)",         "commodity","COMEX","USD","GC=F",      None,       None,         "US",   "AT",  "Metals", None),
    ("SILVER",     "Silver (front)",       "commodity","COMEX","USD","SI=F",      None,       None,         "US",   "AT",  "Metals", None),
    ("COPPER",     "Copper (front)",       "commodity","COMEX","USD","HG=F",      None,       None,         "US",   "AT",  "Metals", None),
    ("SPX",        "S&P 500",              "index",  "NYSE",  "USD", "^GSPC",     None,       None,         "US",   "AT",  "Index", None),
    ("NDX",        "Nasdaq 100",           "index",  "NASDAQ","USD", "^NDX",      None,       None,         "US",   "AT",  "Index", None),
    ("IXIC",       "Nasdaq Composite",     "index",  "NASDAQ","USD", "^IXIC",     None,       None,         "US",   "AT",  "Index", None),
    ("DJI",        "Dow Jones Industrial", "index",  "NYSE",  "USD", "^DJI",      None,       None,         "US",   "AT",  "Index", None),
    ("RUT",        "Russell 2000",         "index",  "NYSE",  "USD", "^RUT",      None,       None,         "US",   "AT",  "Index", None),
    ("VIX",        "CBOE VIX",             "index",  "CBOE",  "USD", "^VIX",      None,       None,         "US",   "AT",  "Volatility", None),
    ("US10Y",      "US 10-year yield",     "yield",  "CBOE",  "USD", "^TNX",      None,       None,         "US",   "AT",  "Rates", None),
    ("US2Y",       "US 2-year yield",      "yield",  "CBOE",  "USD", "2YY=F",     None,       None,         "US",   "AT",  "Rates", None),
    ("SPY",        "SPDR S&P 500 ETF",     "etf",    "NYSE",  "USD", "SPY",       "SPY",      None,         "US",   "ATP", "ETF", 1),
    ("QQQ",        "Invesco Nasdaq 100 ETF","etf",   "NASDAQ","USD", "QQQ",       "QQQ",      None,         "US",   "ATP", "ETF", 1),
    ("FTSE",       "FTSE 100",             "index",  "LSE",   "GBP", "^FTSE",     None,       None,         "UK",   "AT",  "Index", None),
    ("DAX",        "DAX 40",               "index",  "XETRA", "EUR", "^GDAXI",    None,       None,         "EU",   "AT",  "Index", None),
    ("CAC",        "CAC 40",               "index",  "EURONEXT","EUR","^FCHI",    None,       None,         "EU",   "AT",  "Index", None),
    ("STOXX50",    "Euro Stoxx 50",        "index",  "EUREX", "EUR", "^STOXX50E", None,       None,         "EU",   "AT",  "Index", None),
    ("NIKKEI",     "Nikkei 225",           "index",  "TSE",   "JPY", "^N225",     None,       None,         "JP",   "AT",  "Index", None),
    ("HSI",        "Hang Seng",            "index",  "HKEX",  "HKD", "^HSI",      None,       None,         "HK",   "AT",  "Index", None),
    ("SSE",        "Shanghai Composite",   "index",  "SSE",   "CNY", "000001.SS", None,       None,         "CN",   "AT",  "Index", None),
    ("STI",        "Straits Times",        "index",  "SGX",   "SGD", "^STI",      None,       None,         "SG",   "AT",  "Index", None),
    ("ASX200",     "S&P/ASX 200",          "index",  "ASX",   "AUD", "^AXJO",     None,       None,         "AU",   "AT",  "Index", None),
    ("TSX",        "S&P/TSX Composite",    "index",  "TSX",   "CAD", "^GSPTSE",   None,       None,         "CA",   "AT",  "Index", None),
    ("KOSPI",      "KOSPI",                "index",  "KRX",   "KRW", "^KS11",     None,       None,         "KR",   "AT",  "Index", None),
    ("GIFTNIFTY",  "GIFT Nifty (SGX Nifty successor)", "index", "NSE IX", "USD", None, None, None,          "GIFT", "T",   "Index", None),
    # a few US mega caps for the US paper account (extend via watchlist)
    ("AAPL",  "Apple",       "stock", "NASDAQ", "USD", "AAPL",  "AAPL",  None, "US", "ATP", "Technology", 1),
    ("MSFT",  "Microsoft",   "stock", "NASDAQ", "USD", "MSFT",  "MSFT",  None, "US", "ATP", "Technology", 1),
    ("NVDA",  "Nvidia",      "stock", "NASDAQ", "USD", "NVDA",  "NVDA",  None, "US", "ATP", "Technology", 1),
    ("AMZN",  "Amazon",      "stock", "NASDAQ", "USD", "AMZN",  "AMZN",  None, "US", "ATP", "Consumer", 1),
    ("GOOGL", "Alphabet",    "stock", "NASDAQ", "USD", "GOOGL", "GOOGL", None, "US", "ATP", "Communication", 1),
    ("TSLA",  "Tesla",       "stock", "NASDAQ", "USD", "TSLA",  "TSLA",  None, "US", "ATP", "Consumer", 1),
    ("META",  "Meta",        "stock", "NASDAQ", "USD", "META",  "META",  None, "US", "ATP", "Communication", 1),
    ("JPM",   "JPMorgan",    "stock", "NYSE",   "USD", "JPM",   "JPM",   None, "US", "ATP", "Financials", 1),
    ("XOM",   "ExxonMobil",  "stock", "NYSE",   "USD", "XOM",   "XOM",   None, "US", "ATP", "Energy", 1),
]
# US large-cap universe for the US auto paper account (S&P 100-style list; membership not point-in-time - survivorship bias noted).
US_UNIVERSE = ["AAPL","MSFT","NVDA","AMZN","GOOGL","META","TSLA","BRK-B","JPM","V","MA","UNH","XOM","JNJ","PG","HD","COST","ABBV","MRK","AVGO",
    "PEP","KO","LLY","WMT","BAC","CVX","ADBE","CRM","NFLX","AMD","ORCL","CSCO","TMO","ACN","MCD","ABT","DHR","WFC","LIN","TXN","INTC","QCOM",
    "PM","NEE","UNP","IBM","AMGN","HON","LOW","CAT","GS","MS","SPGI","BLK","INTU","ISRG","AMAT","NOW","GE","RTX","BKNG","DE","PLD","SBUX",
    "MDT","GILD","ADI","LMT","SYK","MMC","C","CB","TJX","VRTX","MO","SO","DUK","ZTS","PGR","BA","UPS","PYPL","COP","T","VZ","CMCSA","DIS",
    "NKE","MU","PANW","UBER","ABNB","SCHW","AXP","ELV","CI","REGN","BMY","USB","F","GM"]

COLS = ["id", "name", "kind", "exchange", "currency", "yahoo", "twelvedata", "angel", "calendar", "can", "sector", "lot"]

# Existing 25-Sep-2026 engine run traded these NSE stocks; keep sectors for the ones we know.
SECTOR_HINTS = {
    "RELIANCE": "Energy", "ONGC": "Energy", "COALINDIA": "Energy", "NTPC": "Utilities", "POWERGRID": "Utilities",
    "HDFCBANK": "Financials", "ICICIBANK": "Financials", "SBIN": "Financials", "AXISBANK": "Financials", "KOTAKBANK": "Financials",
    "INDUSINDBK": "Financials", "BAJFINANCE": "Financials", "BAJAJFINSV": "Financials", "SBILIFE": "Financials", "HDFCLIFE": "Financials",
    "SHRIRAMFIN": "Financials", "CHOLAHLDNG": "Financials",
    "INFY": "IT", "TCS": "IT", "HCLTECH": "IT", "WIPRO": "IT", "TECHM": "IT",
    "BHARTIARTL": "Telecom", "ITC": "FMCG", "HINDUNILVR": "FMCG", "NESTLEIND": "FMCG", "BRITANNIA": "FMCG", "TATACONSUM": "FMCG",
    "LT": "Infrastructure", "ULTRACEMCO": "Cement", "GRASIM": "Cement", "ADANIPORTS": "Infrastructure", "ADANIENT": "Conglomerate",
    "TATASTEEL": "Metals", "JSWSTEEL": "Metals", "HINDALCO": "Metals",
    "MARUTI": "Auto", "M&M": "Auto", "BAJAJ-AUTO": "Auto", "EICHERMOT": "Auto", "HEROMOTOCO": "Auto", "TMPV": "Auto", "TATAMOTORS": "Auto",
    "SUNPHARMA": "Pharma", "DRREDDY": "Pharma", "CIPLA": "Pharma", "DIVISLAB": "Pharma", "APOLLOHOSP": "Healthcare", "ZYDUSLIFE": "Pharma",
    "TITAN": "Consumer", "ASIANPAINT": "Consumer", "TRENT": "Retail",
    "BEL": "Defence", "COCHINSHIP": "Defence", "ENGINERSIN": "Engineering", "CASTROLIND": "Energy", "AEGISLOG": "Logistics",
}


def us_equity_rows(symbols=None):
    rows = []
    for s in (symbols or US_UNIVERSE):
        rows.append({"id": s, "name": s, "kind": "stock", "exchange": "US", "currency": "USD", "yahoo": s, "twelvedata": s,
                     "angel": None, "calendar": "US", "can": "ATP", "sector": "Unknown", "lot": 1, "source": "us_universe"})
    return pd.DataFrame(rows, columns=COLS + ["source"])


def _core_df():
    df = pd.DataFrame(CORE, columns=COLS)
    df["source"] = "core"
    return df


def nse_equity_rows(symbols, sectors=None):
    rows = []
    for s in symbols:
        rows.append({"id": s, "name": s, "kind": "stock", "exchange": "NSE", "currency": "INR",
                     "yahoo": s + ".NS", "twelvedata": f"{s}:NSE", "angel": f"{s}-EQ", "calendar": "NSE",
                     "can": "ATP", "sector": (sectors or {}).get(s, SECTOR_HINTS.get(s, "Unknown")), "lot": 1,
                     "source": "nse_nifty500"})
    return pd.DataFrame(rows, columns=COLS + ["source"])


def load_nifty500(session=None, fallback_file="stocks.txt", url=None):
    """Official Nifty 500 constituents from NSE; falls back to the repo stock list. Returns (df, source)."""
    import requests
    from . import config as C
    url = url or C.UNIVERSE_URL
    try:
        r = (session or requests).get(url, headers={"User-Agent": "Mozilla/5.0 (personal-scanner)"}, timeout=20)
        r.raise_for_status()
        raw = pd.read_csv(io.StringIO(r.text))
        syms = raw["Symbol"].dropna().str.strip().tolist()
        sectors = dict(zip(raw["Symbol"].str.strip(), raw.get("Industry", pd.Series(dtype=str)).fillna("Unknown")))
        if len(syms) > 100:
            return nse_equity_rows(syms, sectors), "Nifty 500 (official NSE list)"
    except Exception as e:  # noqa
        print("Nifty 500 list download failed:", e)
    with open(fallback_file) as f:
        syms = [l.strip() for l in f if l.strip() and not l.startswith("#")]
    return nse_equity_rows(syms), "backup list (stocks.txt)"


def master(nse_df=None):
    df = _core_df()
    if nse_df is not None:
        df = pd.concat([df, nse_df[~nse_df.id.isin(df.id)]], ignore_index=True)
    return df.set_index("id", drop=False)


def lookup(m, text):
    """Case-insensitive search by id or name."""
    t = text.strip().upper()
    if t in m.index:
        return m.loc[t]
    hit = m[m.name.str.upper().str.contains(t, regex=False)]
    return hit.iloc[0] if len(hit) else None
