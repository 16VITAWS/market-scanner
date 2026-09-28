"""
Transaction cost model. Statutory charges are the same at every broker; brokerage differs.
Every number carries a verification status. Figures marked "unverified" must be checked on
the official page before being trusted; the portal shows the status next to the number.
"""
from decimal import Decimal, ROUND_HALF_UP

D = Decimal

# Statutory charges for India (rate of trade value unless noted). Source notes are the
# official pages to check; effective dates are from public circulars as understood on 2026-09-27.
INDIA_STATUTORY = {
    "delivery": {"stt_buy": D("0.001"), "stt_sell": D("0.001"), "stamp_buy": D("0.00015"), "exch": D("0.0000297"), "sebi": D("0.000001"), "dp_sell": D("15.93")},
    "intraday": {"stt_buy": D("0"), "stt_sell": D("0.00025"), "stamp_buy": D("0.00003"), "exch": D("0.0000297"), "sebi": D("0.000001"), "dp_sell": D("0")},
    "futures":  {"stt_buy": D("0"), "stt_sell": D("0.0002"), "stamp_buy": D("0.00002"), "exch": D("0.0000173"), "sebi": D("0.000001"), "dp_sell": D("0")},
    "options":  {"stt_buy": D("0"), "stt_sell": D("0.001"), "stamp_buy": D("0.00003"), "exch": D("0.0003503"), "sebi": D("0.000001"), "dp_sell": D("0")},   # on premium
    "gst": D("0.18"),
    "sources": {"stt": "incometaxindia.gov.in / Finance Act 2024 (F&O STT raised 1 Oct 2024)", "exch": "nseindia.com transaction charges",
                "stamp": "Indian Stamp Act schedule, uniform since Jul 2020", "sebi": "SEBI turnover fee ₹10 per crore", "dp": "typical CDSL DP charge; varies by broker"},
    "verification": "unverified - rates from public circulars as known on 2026-09-27; confirm on the official pages before relying on them",
}

# Broker brokerage. [flat_rupees, pct_of_value] -> charge = min(flat, value*pct) per order; None = not verified.
BROKERS = [
    {"id": "groww", "name": "Groww", "login": "https://groww.in/login", "web": "https://groww.in", "api_docs": "https://groww.in/trade-api/docs",
     "brokerage": {"delivery": [20, 0.0005], "intraday": [20, 0.0005], "futures": [20, 0.0005], "options": [20, None]},
     "data_api_fee_month": 499, "api": {"orders": True, "market_data": True, "paper": False, "auth": "API key + TOTP", "rate_limit": "documented per Groww", "exchanges": ["NSE", "BSE"]},
     "verified": "partial", "checked": "2026-09-25", "source": "groww.in/pricing", "note": "Only broker the user currently uses. Trade API ₹499/month for live data."},
    {"id": "zerodha", "name": "Zerodha Kite", "login": "https://kite.zerodha.com", "web": "https://zerodha.com", "api_docs": "https://kite.trade/docs/connect/v3/",
     "brokerage": {"delivery": [0, 0], "intraday": [20, 0.0003], "futures": [20, 0.0003], "options": [20, None]},
     "data_api_fee_month": 500, "api": {"orders": True, "market_data": True, "paper": False, "auth": "Kite Connect OAuth", "rate_limit": "10 req/s", "exchanges": ["NSE", "BSE", "MCX"]},
     "verified": "unverified", "checked": "2026-09-27", "source": "zerodha.com/charges", "note": "Kite Connect ₹500/month."},
    {"id": "dhan", "name": "Dhan", "login": "https://login.dhan.co", "web": "https://dhan.co", "api_docs": "https://dhanhq.co/docs/",
     "brokerage": {"delivery": [0, 0], "intraday": [20, 0.0003], "futures": [20, 0.0003], "options": [20, None]},
     "data_api_fee_month": 499, "api": {"orders": True, "market_data": True, "paper": False, "auth": "access token", "rate_limit": "documented", "exchanges": ["NSE", "BSE", "MCX"]},
     "verified": "partial", "checked": "2026-09-25", "source": "dhan.co/pricing", "note": "Intraday/futures ₹20 or 0.03% verified earlier; delivery unverified."},
    {"id": "fyers", "name": "Fyers", "login": "https://login.fyers.in", "web": "https://fyers.in", "api_docs": "https://myapi.fyers.in/docsv3",
     "brokerage": {"delivery": [0, 0], "intraday": [20, 0.0003], "futures": [20, 0.0003], "options": [20, None]},
     "data_api_fee_month": 0, "api": {"orders": True, "market_data": True, "paper": False, "auth": "OAuth app id + secret", "rate_limit": "10 req/s", "exchanges": ["NSE", "BSE", "MCX"]},
     "verified": "unverified", "checked": "2026-09-27", "source": "fyers.in/pricing", "note": "Free API with data."},
    {"id": "angelone", "name": "Angel One", "login": "https://trade.angelone.in", "web": "https://www.angelone.in", "api_docs": "https://smartapi.angelbroking.com/docs",
     "brokerage": {"delivery": [20, 0.001], "intraday": [20, 0.0003], "futures": [20, None], "options": [20, None]},
     "data_api_fee_month": 0, "api": {"orders": True, "market_data": True, "paper": False, "auth": "API key + client id + password + TOTP", "rate_limit": "documented per endpoint", "exchanges": ["NSE", "BSE", "NFO", "MCX", "CDS"]},
     "verified": "partial", "checked": "2026-09-25", "source": "angelone.in/pricing", "note": "F&O ₹20 verified earlier. Free SmartAPI."},
    {"id": "upstox", "name": "Upstox", "login": "https://login.upstox.com", "web": "https://upstox.com", "api_docs": "https://upstox.com/developer/api-documentation/",
     "brokerage": {"delivery": [20, 0.001], "intraday": [20, 0.0005], "futures": [20, 0.0005], "options": [20, None]},
     "data_api_fee_month": 0, "api": {"orders": True, "market_data": True, "paper": True, "auth": "OAuth", "rate_limit": "documented", "exchanges": ["NSE", "BSE", "MCX"]},
     "verified": "unverified", "checked": "2026-09-27", "source": "upstox.com/pricing", "note": "Upstox API has a sandbox environment (paper-like)."},
    {"id": "shoonya", "name": "Shoonya (Finvasia)", "login": "https://trade.shoonya.com", "web": "https://shoonya.com", "api_docs": "https://shoonya.com/api-documentation",
     "brokerage": {"delivery": [0, 0], "intraday": [0, 0], "futures": [0, 0], "options": [0, 0]},
     "data_api_fee_month": 0, "api": {"orders": True, "market_data": True, "paper": False, "auth": "user id + password + TOTP", "rate_limit": "documented", "exchanges": ["NSE", "BSE", "MCX"]},
     "verified": "partial", "checked": "2026-09-25", "source": "shoonya.com", "note": "Zero brokerage claim verified earlier; statutory charges still apply."},
    {"id": "aliceblue", "name": "Alice Blue", "login": "https://ant.aliceblueonline.com", "web": "https://aliceblueonline.com", "api_docs": "https://v2api.aliceblueonline.com/",
     "brokerage": {"delivery": [15, 0], "intraday": [15, None], "futures": [15, None], "options": [15, None]},
     "data_api_fee_month": 0, "api": {"orders": True, "market_data": True, "paper": False, "auth": "API key", "rate_limit": "documented", "exchanges": ["NSE", "BSE", "MCX"]},
     "verified": "unverified", "checked": "2026-09-27", "source": "aliceblueonline.com/pricing", "note": ""},
    {"id": "pocketful", "name": "Pocketful", "login": "https://web.pocketful.in", "web": "https://www.pocketful.in", "api_docs": "https://api.pocketful.in/docs",
     "brokerage": {"delivery": [0, 0], "intraday": [20, None], "futures": [20, None], "options": [20, None]},
     "data_api_fee_month": 0, "api": {"orders": True, "market_data": True, "paper": False, "auth": "OAuth", "rate_limit": "documented", "exchanges": ["NSE", "BSE"]},
     "verified": "unverified", "checked": "2026-09-27", "source": "pocketful.in/pricing", "note": ""},
    {"id": "ibkr", "name": "Interactive Brokers (international)", "login": "https://www.interactivebrokers.com/sso/Login", "web": "https://www.interactivebrokers.com", "api_docs": "https://interactivebrokers.github.io/",
     "brokerage": {}, "data_api_fee_month": None, "api": {"orders": True, "market_data": True, "paper": True, "auth": "Client Portal / TWS", "rate_limit": "documented", "exchanges": ["NYSE", "NASDAQ", "LSE", "TSE", "HKEX", "and more"]},
     "verified": "unverified", "checked": "2026-09-27", "source": "interactivebrokers.com/en/pricing", "note": "Needs a separate international account; has an official paper-trading account."},
]


def q(x):
    return D(x).quantize(D("0.01"), rounding=ROUND_HALF_UP)


def india_charges(value, side, segment="delivery", broker_id="groww"):
    """Return dict of each charge (Decimal, rupees) for one order of `value` rupees."""
    value = D(str(value))
    s = INDIA_STATUTORY[segment]
    b = next((x for x in BROKERS if x["id"] == broker_id), BROKERS[0])
    br = b["brokerage"].get(segment)
    if br is None or (br[0] is None):
        brokerage, verified = D("20"), "assumed ₹20 (unverified)"
    else:
        flat, pct = D(str(br[0])), (D(str(br[1])) if br[1] not in (None,) else None)
        brokerage = min(flat, value * pct) if pct is not None else flat
        verified = b["verified"]
    stt = value * (s["stt_buy"] if side == "buy" else s["stt_sell"])
    stamp = value * s["stamp_buy"] if side == "buy" else D("0")
    exch = value * s["exch"]
    sebi = value * s["sebi"]
    dp = s["dp_sell"] if side == "sell" else D("0")
    gst = (brokerage + exch + sebi) * INDIA_STATUTORY["gst"]
    total = brokerage + stt + stamp + exch + sebi + dp + gst
    return {"brokerage": q(brokerage), "stt": q(stt), "stamp": q(stamp), "exchange": q(exch), "sebi": q(sebi), "dp": q(dp), "gst": q(gst),
            "total": q(total), "broker": b["id"], "segment": segment, "verification": verified,
            "statutory_verification": INDIA_STATUTORY["verification"]}


def us_charges(value, side, broker_id="ibkr"):
    """US paper fills: commission-free retail assumption + SEC fee on sells (0.0000278) + FINRA TAF. Marked assumed."""
    value = D(str(value))
    sec = value * D("0.0000278") if side == "sell" else D("0")
    return {"brokerage": D("0.00"), "sec_fee": q(sec), "total": q(sec), "broker": broker_id, "segment": "us_equity",
            "verification": "assumed commission-free; SEC fee rate unverified"}


def charges(value, side, currency="INR", segment="delivery", broker_id="groww"):
    if currency == "INR":
        return india_charges(value, side, segment, broker_id)
    return us_charges(value, side, broker_id)


def estimate_round_trip(value, segment="delivery", broker_id="groww", currency="INR"):
    b = charges(value, "buy", currency, segment, broker_id)
    s = charges(value, "sell", currency, segment, broker_id)
    return {"buy": b, "sell": s, "total": q(b["total"] + s["total"]), "pct_of_value": q(D(100) * (b["total"] + s["total"]) / D(str(value)))}
