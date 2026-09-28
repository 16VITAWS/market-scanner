"""
Market calendars, sessions and live status.
Holiday lists are seeds from public schedules and are marked verified=False until
checked against the exchange circular. A day with no candle is reported as
"no session or data delay" - never silently filled.
"""
import datetime as dt
from zoneinfo import ZoneInfo

CALENDARS = {
    # id: timezone, sessions [(start, end)], weekend days, holidays {date: name}
    "NSE": {"tz": "Asia/Kolkata", "name": "NSE / BSE (India)", "currency": "INR",
            "pre": ("09:00", "09:08"), "sessions": [("09:15", "15:30")], "post": ("15:40", "16:00"),
            "holidays": {"2026-01-26": "Republic Day", "2026-03-03": "Holi", "2026-03-26": "Ram Navami",
                         "2026-03-31": "Mahavir Jayanti", "2026-04-03": "Good Friday", "2026-04-14": "Ambedkar Jayanti",
                         "2026-05-01": "Maharashtra Day", "2026-05-28": "Bakri Id", "2026-06-26": "Muharram",
                         "2026-09-14": "Ganesh Chaturthi", "2026-10-02": "Gandhi Jayanti", "2026-10-20": "Dussehra",
                         "2026-11-09": "Diwali Balipratipada", "2026-11-24": "Guru Nanak Jayanti", "2026-12-25": "Christmas"},
            "verified": False, "holiday_source": "seed list - verify at nseindia.com/resources/exchange-communication-holidays"},
    "US": {"tz": "America/New_York", "name": "NYSE / Nasdaq", "currency": "USD",
           "pre": ("04:00", "09:30"), "sessions": [("09:30", "16:00")], "post": ("16:00", "20:00"),
           "holidays": {"2026-01-01": "New Year", "2026-01-19": "MLK Day", "2026-02-16": "Presidents Day",
                        "2026-04-03": "Good Friday", "2026-05-25": "Memorial Day", "2026-06-19": "Juneteenth",
                        "2026-07-03": "Independence Day (obs)", "2026-09-07": "Labor Day", "2026-11-26": "Thanksgiving",
                        "2026-12-25": "Christmas"},
           "verified": False, "holiday_source": "seed list - verify at nyse.com/markets/hours-calendars"},
    "UK": {"tz": "Europe/London", "name": "London Stock Exchange", "currency": "GBP", "sessions": [("08:00", "16:30")],
           "holidays": {"2026-01-01": "New Year", "2026-04-03": "Good Friday", "2026-04-06": "Easter Monday", "2026-05-04": "Early May",
                        "2026-05-25": "Spring bank holiday", "2026-08-31": "Summer bank holiday", "2026-12-25": "Christmas", "2026-12-28": "Boxing Day (obs)"},
           "verified": False, "holiday_source": "seed list"},
    "EU": {"tz": "Europe/Berlin", "name": "Xetra / Euronext", "currency": "EUR", "sessions": [("09:00", "17:30")],
           "holidays": {"2026-01-01": "New Year", "2026-04-03": "Good Friday", "2026-04-06": "Easter Monday", "2026-05-01": "Labour Day", "2026-12-24": "Christmas Eve", "2026-12-25": "Christmas"},
           "verified": False, "holiday_source": "seed list"},
    "JP": {"tz": "Asia/Tokyo", "name": "Tokyo Stock Exchange", "currency": "JPY", "sessions": [("09:00", "11:30"), ("12:30", "15:30")],
           "holidays": {"2026-01-01": "New Year", "2026-01-02": "Bank holiday", "2026-01-12": "Coming of Age", "2026-02-11": "Foundation Day",
                        "2026-02-23": "Emperor's Birthday", "2026-03-20": "Vernal Equinox", "2026-04-29": "Showa Day", "2026-05-04": "Greenery", "2026-05-05": "Children's",
                        "2026-05-06": "Constitution (obs)", "2026-07-20": "Marine Day", "2026-08-11": "Mountain Day", "2026-09-21": "Respect for Aged", "2026-09-22": "Bridge", "2026-09-23": "Autumnal Equinox",
                        "2026-10-12": "Sports Day", "2026-11-03": "Culture Day", "2026-11-23": "Labour Thanksgiving", "2026-12-31": "Year end"},
           "verified": False, "holiday_source": "seed list"},
    "HK": {"tz": "Asia/Hong_Kong", "name": "Hong Kong Exchange", "currency": "HKD", "sessions": [("09:30", "12:00"), ("13:00", "16:00")], "holidays": {}, "verified": False, "holiday_source": "not loaded"},
    "CN": {"tz": "Asia/Shanghai", "name": "Shanghai Stock Exchange", "currency": "CNY", "sessions": [("09:30", "11:30"), ("13:00", "15:00")], "holidays": {}, "verified": False, "holiday_source": "not loaded"},
    "SG": {"tz": "Asia/Singapore", "name": "Singapore Exchange", "currency": "SGD", "sessions": [("09:00", "17:00")], "holidays": {}, "verified": False, "holiday_source": "not loaded"},
    "AU": {"tz": "Australia/Sydney", "name": "ASX", "currency": "AUD", "sessions": [("10:00", "16:00")], "holidays": {}, "verified": False, "holiday_source": "not loaded"},
    "CA": {"tz": "America/Toronto", "name": "TSX", "currency": "CAD", "sessions": [("09:30", "16:00")], "holidays": {}, "verified": False, "holiday_source": "not loaded"},
    "KR": {"tz": "Asia/Seoul", "name": "Korea Exchange", "currency": "KRW", "sessions": [("09:00", "15:30")], "holidays": {}, "verified": False, "holiday_source": "not loaded"},
    "GIFT": {"tz": "Asia/Kolkata", "name": "NSE IX (GIFT Nifty)", "currency": "USD", "sessions": [("06:30", "15:40"), ("16:35", "02:45")], "holidays": {}, "verified": False, "holiday_source": "not loaded"},
    "FX": {"tz": "UTC", "name": "Global FX (24x5)", "currency": "USD", "sessions": [("00:00", "23:59")], "weekend": [5, 6], "holidays": {}, "verified": True, "holiday_source": "n/a"},
}


def _t(s):
    h, m = s.split(":")
    return dt.time(int(h), int(m))


def status(cal_id, now_utc=None):
    """Return dict: state (OPEN / PRE / POST / CLOSED / HOLIDAY / WEEKEND), local time, next event."""
    c = CALENDARS[cal_id]
    tz = ZoneInfo(c["tz"])
    now = (now_utc or dt.datetime.now(dt.timezone.utc)).astimezone(tz)
    d = now.date()
    weekend = c.get("weekend", [5, 6])
    out = {"calendar": cal_id, "name": c["name"], "local_time": now.strftime("%Y-%m-%d %H:%M"), "tz": c["tz"],
           "holidays_verified": c["verified"], "holiday_source": c["holiday_source"]}
    if now.weekday() in weekend:
        out["state"] = "WEEKEND"
        return out
    hol = c["holidays"].get(d.isoformat())
    if hol:
        out.update(state="HOLIDAY", reason=hol)
        return out
    t = now.time()
    for a, b in c["sessions"]:
        if _t(a) <= t <= _t(b):
            out.update(state="OPEN", session_end=b)
            return out
    if "pre" in c and _t(c["pre"][0]) <= t < _t(c["pre"][1]):
        out.update(state="PRE")
        return out
    if "post" in c and _t(c["post"][0]) <= t <= _t(c["post"][1]):
        out.update(state="POST")
        return out
    out["state"] = "CLOSED"
    return out


def all_status(now_utc=None):
    return {k: status(k, now_utc) for k in CALENDARS}


def is_session_day(cal_id, d):
    c = CALENDARS[cal_id]
    return d.weekday() not in c.get("weekend", [5, 6]) and d.isoformat() not in c["holidays"]


def expected_sessions(cal_id, start, end):
    days, d = [], start
    while d <= end:
        if is_session_day(cal_id, d):
            days.append(d)
        d += dt.timedelta(days=1)
    return days
