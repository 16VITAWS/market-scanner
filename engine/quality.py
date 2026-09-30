"""
Quote normalisation, validation and freshness - the single place that decides whether a price is shown and how it is labelled.

Every quote the portal publishes carries:
    sym      symbol (portal id, e.g. RELIANCE, NIFTY)          ex    exchange / calendar (NSE, US, FX, ...)
    p        last price                                           pc    previous session close
    t        MARKET timestamp (time of the bar/trade, ISO, UTC offset included)
    recv     time this engine received it (ISO, UTC)              src   provider id (yahoo, angelone, ...)
    delay_min  the provider's documented delay                    status  provider status at receipt (DELAYED / REALTIME)
    flags    validation flags (empty list = clean)

Freshness (computed from timestamps, never assumed):
    LIVE      realtime provider and market timestamp <= 60 s old while the market is open
    DELAYED   delayed provider, market timestamp within (delay + 10 min) while open
    STALE     market open but the newest market timestamp is older than that window
    CLOSED    market closed: last traded price, labelled with its own time
    OFFLINE   no usable quote at all  -> the UI shows "DATA TEMPORARILY UNAVAILABLE"
Validation rejects impossible records and KEEPS the previous valid quote (never silently overwritten).
"""
import math, datetime as dt

REQUIRED = ("p", "t")
NSE_EQ_MAX_MOVE = 0.205          # NSE equity price bands are at most 20% a day; beyond that the record is suspect
INDEX_LIKE_MAX_MOVE = 0.35       # indices / FX / commodities: anything above 35% in a day is treated as a bad tick
REALTIME_WINDOW_S = 60
DELAY_GRACE_S = 600


def _num(x):
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def parse_ts(s):
    if not s:
        return None
    try:
        t = dt.datetime.fromisoformat(str(s).replace("Z", "+00:00"))
        return t if t.tzinfo else t.replace(tzinfo=dt.timezone.utc)
    except ValueError:
        return None


def normalise(q, sym, ex, src, delay_min, now=None):
    """Attach identity + provenance fields. Never changes prices."""
    now = now or dt.datetime.now(dt.timezone.utc)
    out = dict(q)
    out.update(sym=sym, ex=ex, src=src, delay_min=delay_min, recv=now.isoformat(timespec="seconds"),
               status="REALTIME" if delay_min == 0 else "DELAYED")
    return out


def validate(new, old=None, kind="stock"):
    """Return (accept: bool, flags: list[str]). Hard problems -> reject (keep old). Soft problems -> accept + flag."""
    flags = []
    for k in REQUIRED:
        if new.get(k) in (None, ""):
            return False, [f"missing {k}"]
    for k in ("p", "pc", "day_o", "day_h", "day_l", "day_v"):
        v = new.get(k)
        if v is not None and not _num(v):
            return False, [f"non-numeric {k}"]
    if new["p"] <= 0:
        return False, ["non-positive price"]
    if (new.get("day_v") or 0) < 0:
        return False, ["negative volume"]
    t_new = parse_ts(new["t"])
    if t_new is None:
        return False, ["bad timestamp"]
    if t_new > dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=5):
        return False, ["timestamp in the future"]
    h, l, o = new.get("day_h"), new.get("day_l"), new.get("day_o")
    if h is not None and l is not None:
        tol = 1e-4
        if h < l * (1 - tol) or new["p"] > h * (1 + tol) or new["p"] < l * (1 - tol) or \
                (o is not None and (o > h * (1 + tol) or o < l * (1 - tol))):
            return False, ["OHLC inconsistent"]
    if old and old.get("t"):
        t_old = parse_ts(old["t"])
        if t_old and t_new < t_old:
            return False, ["out-of-order (older than current quote)"]
        if t_old and t_new == t_old and old.get("p") == new["p"]:
            flags.append("duplicate tick")
    pc = new.get("pc")
    if _num(pc) and pc > 0:
        move = abs(new["p"] / pc - 1)
        lim = NSE_EQ_MAX_MOVE if kind == "stock_nse" else INDEX_LIKE_MAX_MOVE if kind == "index" else 0.5
        if move > lim:
            return False, [f"impossible move {move * 100:.1f}% vs previous close (limit {lim * 100:.0f}%)"]
    return True, flags


def accept(quotes, key, new, kind="stock", log=None):
    """Merge `new` into quotes[key] if valid; otherwise keep the old quote and record why. Returns True if accepted."""
    old = quotes.get(key)
    ok, flags = validate(new, old, kind)
    if ok:
        merged = {**(old or {}), **new, "flags": flags}
        quotes[key] = merged
    else:
        if old is not None:
            old.setdefault("flags", [])
            old["last_rejected"] = {"reason": flags[0], "at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
                                    "src": new.get("src")}
        if log is not None:
            log.append({"sym": key, "reason": flags[0], "src": new.get("src"), "t": new.get("t")})
    return ok


def freshness(q, market_open, now=None):
    """Status label computed from the quote's own market timestamp."""
    now = now or dt.datetime.now(dt.timezone.utc)
    if not q or not _num(q.get("p")):
        return "OFFLINE", None
    t = parse_ts(q.get("t"))
    if t is None:
        return "OFFLINE", None
    age = (now - t).total_seconds()
    if not market_open:
        return "CLOSED", age
    delay = q.get("delay_min")
    delay = 15 if delay is None else delay
    if delay == 0 and age <= REALTIME_WINDOW_S:
        return "LIVE", age
    if age <= delay * 60 + DELAY_GRACE_S:
        return "DELAYED", age
    return "STALE", age


def summary(quotes, open_markets, now=None):
    """Counts per freshness state for the data-health page (only quotes whose exchange is known)."""
    out = {"LIVE": 0, "DELAYED": 0, "STALE": 0, "CLOSED": 0, "OFFLINE": 0}
    flagged = 0
    for q in quotes.values():
        st, _ = freshness(q, (q.get("ex") in open_markets), now)
        out[st] += 1
        flagged += bool(q.get("flags"))
    out["flagged"] = flagged
    return out
