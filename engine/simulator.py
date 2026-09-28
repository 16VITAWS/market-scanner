"""
Order simulator on OHLC bars. Deterministic. Fill rules (documented, conservative):

  MARKET       fills at next bar's OPEN * (1 +/- (slippage + impact)), impact = 0.1 x range/close x sqrt(qty/volume)
  LIMIT buy    fills if bar LOW <= limit, at min(open, limit)          (no slippage benefit)
  LIMIT sell   fills if bar HIGH >= limit, at max(open, limit)
  STOP buy     triggers if bar HIGH >= stop, fills at max(open, stop)*(1+slip)
  STOP sell    triggers if bar LOW  <= stop, fills at min(open, stop)*(1-slip)   (gap-down fills worse)
  Position stop-loss / target are checked every bar; if both hit in one bar the STOP is assumed first.
  Liquidity: a fill is capped at max_volume_share of the bar's volume; the rest stays PENDING (partial).
  Halts: bar with zero volume => no fill. Orders expire after `expires_after_bars` bars.
Only OHLC is available, so intra-bar path is unknown: this is an approximation and is labelled so.
"""
import math
from decimal import Decimal, ROUND_HALF_UP
from . import config as C

D = Decimal


def px(x):
    return D(str(x)).quantize(D("0.01"), rounding=ROUND_HALF_UP)


def apply_bar(ledger, account, symbol, bar, bar_date, slippage=None, max_vol_share=None, sector=None):
    """Process pending orders and open position exits for `symbol` with one bar. Returns list of action strings."""
    slip = D(str(C.FILL["slippage_pct"] if slippage is None else slippage))
    share = D(str(C.RISK["max_volume_share"] if max_vol_share is None else max_vol_share))
    o_, h_, l_, c_, v_ = (D(str(bar[k])) for k in ("Open", "High", "Low", "Close", "Volume"))
    acts = []
    liq_cap = int(v_ * share) if v_ > 0 else 0
    day_sig = (h_ - l_) / c_ if c_ > 0 else D("0")          # range as a crude daily volatility proxy

    def impact(q):
        """Square-root market-impact model: 0.1 x daily range x sqrt(qty / bar volume). Added to base slippage."""
        if v_ <= 0 or q <= 0:
            return D("0")
        return D("0.1") * day_sig * D(str(math.sqrt(float(q) / float(v_))))

    # 1. pending orders for this symbol decided before this bar
    for o in [o for o in ledger.pending(account) if o["symbol"] == symbol]:
        if o["decided_on_bar"] and str(bar_date) <= o["decided_on_bar"]:
            continue  # never fill on the decision bar (look-ahead guard)
        o["bars_waited"] += 1
        remaining = D(o["qty"]) - D(o["filled_qty"])
        fill_px = None
        if v_ == 0:
            acts.append(f"{symbol}: no volume on {bar_date} (halt / no trade) - order waits")
        elif o["type"] == "MARKET":
            imp = impact(remaining)
            fill_px = o_ * (1 + slip + imp) if o["side"] == "buy" else o_ * (1 - slip - imp)
        elif o["type"] == "LIMIT":
            lim = D(o["limit"])
            if o["side"] == "buy" and l_ <= lim:
                fill_px = min(o_, lim)
            elif o["side"] == "sell" and h_ >= lim:
                fill_px = max(o_, lim)
        elif o["type"] == "STOP":
            sp = D(o["stop_px"])
            if o["side"] == "buy" and h_ >= sp:
                fill_px = max(o_, sp) * (1 + slip)
            elif o["side"] == "sell" and l_ <= sp:
                fill_px = min(o_, sp) * (1 - slip)
        if fill_px is not None:
            qty = remaining if liq_cap == 0 or remaining <= liq_cap else D(liq_cap)
            note = "" if qty == remaining else f"partial: liquidity cap {liq_cap} of bar volume"
            f = ledger.fill(o, px(fill_px), qty, bar_date, note=note)
            if f:
                acts.append(f"{o['side'].upper()} {f['qty']} {symbol} @ {f['price']} ({o['type'].lower()}, fees {f['fees']}) {note}".strip())
        if o["status"] in ("PENDING", "PARTIAL") and o["bars_waited"] >= o.get("expires_after_bars", 3):
            o["status"] = "EXPIRED"; ledger._audit("order_expired", order=o["id"])
            acts.append(f"{symbol}: order {o['id']} expired unfilled")

    # 2. open position management: stop first, then target, then trailing
    a = ledger.account(account)
    pos = a["positions"].get(symbol)
    if pos:
        pos["bars_held"] = int(pos.get("bars_held", 0)) + 1
        pos["last"] = str(c_)
        pos["high_water"] = str(max(D(pos["high_water"]), h_))
        stop = D(pos["stop"]) if pos.get("stop") else None
        target = D(pos["target"]) if pos.get("target") else None
        exit_px, why = None, None
        if stop is not None and l_ <= stop:
            exit_px, why = min(o_, stop) * (1 - slip), "stop-loss hit"
        elif target is not None and h_ >= target:
            exit_px, why = (max(o_, target) if o_ > target else target) * (1 - slip), "target reached"
        if exit_px is not None:
            o, _ = ledger.submit(account, symbol, "sell", pos["qty"], "MARKET", bar_date=None, strategy=pos.get("strategy") or "exit",
                                 reason=why, dedupe_key=f"{account}|{symbol}|exit|{bar_date}", segment=pos.get("segment", "delivery"))
            f = ledger.fill(o, px(exit_px), D(pos["qty"]), bar_date, note=why)
            if f:
                acts.append(f"EXIT {f['qty']} {symbol} @ {f['price']} - {why}")
        else:
            # trailing stop once up trail_after_pct
            trail_after, trail = D(str(C.SIGNALS["trail_after_pct"])), D(str(C.SIGNALS["trail_pct"]))
            if pos.get("strategy") not in (None, "manual") and c_ > D(pos["avg_price"]) * (1 + trail_after):
                new_stop = px(c_ * (1 - trail))
                if stop is None or new_stop > stop:
                    pos["stop"] = str(new_stop)
                    acts.append(f"{symbol}: trailing stop raised to {new_stop}")
    return acts
