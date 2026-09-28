"""
Trend + Breakout Swing (the running paper strategy). Port of the v1 scanner scoring, unchanged
in logic so historical runs stay comparable; every point of the score is itemised.
"""
from .base import Strategy
from .. import config as C


class TrendBreakoutSwing(Strategy):
    id = "trend_breakout_swing"
    name = "Trend + Breakout Swing"
    version = "1.1.0"
    status = "paper-approved"
    universe = "NIFTY 500 (NSE), price >= 50, 20-day turnover >= 10 cr"
    timeframe = "1d"
    hypothesis = ("Stocks in an uptrend that break their 20-day high on above-average volume, while outperforming NIFTY, "
                  "tend to continue for a few weeks. Only trade when the index itself is in an uptrend.")
    rules = {
        "score": ["+2 price > 50-day SMA > 200-day SMA (uptrend); -2 if the reverse",
                  "+1 21-day SMA crossed above 50-day within 4 bars; -1 if crossed below",
                  "+1 RSI(14) between 50 and 70; -1 if between 30 and 45",
                  "+2 close above previous 20-day high with volume > 1.5x 20-day average; -2 for a 20-day low",
                  "+1 3-month return beats NIFTY; -1 if it lags"],
        "entry": "score >= +5 AND index regime BULL (or SIDEWAYS with score >= +6); no news-risk words in last 3 days",
        "exit": "score <= -5 (SELL), stop 2xATR below entry, target 3xATR above, trailing stop 4% once up 5%, time exit 20 bars, regime turns BEAR",
        "sizing": "1% of equity at risk between entry and stop; <= 25% of equity; <= 5% of 20-day volume; halve size when index 20-day vol > 25%",
    }
    params = {"buy_score": C.SIGNALS["buy_score"], "sell_score": C.SIGNALS["sell_score"], "stop_atr": C.SIGNALS["stop_atr"],
              "target_atr": C.SIGNALS["target_atr"], "max_hold": C.SIGNALS["max_hold_days"], "vol_mult": 1.5, "rs_lookback": 63}
    failure_modes = ["whipsaw in sideways markets (false breakouts)", "gap-downs through the stop (fill worse than stop)",
                     "regime filter is slow: late entries after long rallies", "news shocks; only headline words are screened"]

    def signal(self, d, ctx):
        """d: enriched frame up to the decision bar. ctx: {'regime': BULL/BEAR/SIDEWAYS, 'index_ret3m': float}"""
        if len(d) < 205:
            return {"action": "NONE", "score": 0, "why": ["insufficient history"], "contrib": []}
        x, p = d.iloc[-1], d.iloc[-2]
        s, why, contrib = 0, [], []

        def add(pts, text):
            nonlocal s
            s += pts; why.append(text); contrib.append({"points": pts, "rule": text})

        if x.Close > x.sma50 > x.sma200:
            add(2, f"Uptrend: close {x.Close:.2f} > SMA50 {x.sma50:.2f} > SMA200 {x.sma200:.2f}")
        elif x.Close < x.sma50 < x.sma200:
            add(-2, f"Downtrend: close {x.Close:.2f} < SMA50 {x.sma50:.2f} < SMA200 {x.sma200:.2f}")
        recent = d.tail(4)
        up = ((recent.sma21 > recent.sma50) & (recent.sma21.shift() <= recent.sma50.shift())).any()
        dn = ((recent.sma21 < recent.sma50) & (recent.sma21.shift() >= recent.sma50.shift())).any()
        if up: add(1, "21-day SMA crossed above 50-day within the last 4 bars")
        if dn: add(-1, "21-day SMA crossed below 50-day within the last 4 bars")
        if 50 <= x.rsi <= 70: add(1, f"Healthy momentum: RSI {x.rsi:.0f}")
        elif 30 <= x.rsi < 45: add(-1, f"Weak momentum: RSI {x.rsi:.0f}")
        elif x.rsi > 75: why.append(f"Overbought: RSI {x.rsi:.0f} (no points; do not chase)")
        vol_ok = x.Volume > self.params["vol_mult"] * x.vol20 if x.vol20 and x.vol20 > 0 else False
        if x.Close > x.hi20 and vol_ok: add(2, f"Broke previous 20-day high {x.hi20:.2f} on volume {x.Volume/x.vol20:.1f}x average")
        if x.Close < x.lo20 and vol_ok: add(-2, f"Broke previous 20-day low {x.lo20:.2f} on volume {x.Volume/x.vol20:.1f}x average")
        n = self.params["rs_lookback"]
        rel = d["Close"].iloc[-1] / d["Close"].iloc[-n - 1] - 1 - ctx.get("index_ret3m", 0)
        if rel > 0: add(1, f"Beating NIFTY by {rel*100:.1f}% over 3 months")
        else: add(-1, f"Lagging NIFTY by {abs(rel)*100:.1f}% over 3 months")

        action = "NONE"
        if s >= self.params["buy_score"]:
            action = "BUY"
        elif s <= self.params["sell_score"]:
            action = "SELL"
        entry = float(x.Close); atr = float(x.atr)
        return {"action": action, "score": s, "why": why, "contrib": contrib, "entry": round(entry, 2),
                "stop": round(entry - self.params["stop_atr"] * atr, 2), "target": round(entry + self.params["target_atr"] * atr, 2),
                "atr": round(atr, 2), "rsi": round(float(x.rsi), 1), "sma50": round(float(x.sma50), 2), "sma200": round(float(x.sma200), 2),
                "vol_ratio": round(float(x.Volume / x.vol20), 2) if x.vol20 else None, "turnover_cr": round(float(x.turnover_cr), 1) if x.turnover_cr == x.turnover_cr else None}

    def regime_block(self, action, score, regime):
        """Why a BUY is held back by the index regime. Returns reason or None."""
        if action != "BUY":
            return None
        if regime == "BEAR":
            return f"score {score:+d}, but NIFTY regime is BEAR (close < SMA200 and SMA50 < SMA200)"
        if regime == "SIDEWAYS" and score < self.params["buy_score"] + 1:
            return f"score {score:+d}, not strong enough for a SIDEWAYS regime (needs {self.params['buy_score'] + 1:+d})"
        return None
