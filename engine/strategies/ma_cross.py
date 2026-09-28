"""Moving-average crossover (the FFA course strategy). Kept as a research baseline; tested: no edge over holding."""
from .base import Strategy


class MACross(Strategy):
    id = "ma_cross_21_50"
    name = "21 / 50 SMA Crossover"
    version = "1.0.0"
    status = "backtested"
    universe = "NIFTY 50 index (spot as proxy for the future)"
    timeframe = "1d"
    segment = "futures"
    hypothesis = "When the 21-day average crosses above the 50-day, a trend has started; exit when it crosses back."
    rules = {"entry": "SMA21 crosses above SMA50 (long)", "exit": "SMA21 crosses below SMA50", "stop": "none in the course version",
             "sizing": "fixed units (1 lot)"}
    failure_modes = ["late entries and exits in choppy markets", "long flat periods", "gives back most of a trend"]
    min_capital = 200000
    holding = "weeks to months"
    warmup = 55

    def __init__(self, fast=21, slow=50, allow_short=False):
        self.params = {"fast": fast, "slow": slow, "allow_short": allow_short}
        self.id = f"ma_cross_{fast}_{slow}" + ("_ls" if allow_short else "")
        self.name = f"{fast} / {slow} SMA Crossover" + (" long+short" if allow_short else "")

    def signal(self, d, ctx):
        f, s = self.params["fast"], self.params["slow"]
        c = d["Close"]
        if len(c) < s + 2:
            return {"action": "NONE", "score": 0, "why": ["warming up"]}
        fa, sl = c.rolling(f).mean(), c.rolling(s).mean()
        up = fa.iloc[-1] > sl.iloc[-1] and fa.iloc[-2] <= sl.iloc[-2]
        dn = fa.iloc[-1] < sl.iloc[-1] and fa.iloc[-2] >= sl.iloc[-2]
        if up:
            return {"action": "BUY", "score": 1, "why": [f"SMA{f} {fa.iloc[-1]:.1f} crossed above SMA{s} {sl.iloc[-1]:.1f}"], "entry": float(c.iloc[-1]), "stop": None, "target": None}
        if dn:
            return {"action": "SELL", "score": -1, "why": [f"SMA{f} {fa.iloc[-1]:.1f} crossed below SMA{s} {sl.iloc[-1]:.1f}"], "entry": float(c.iloc[-1]), "stop": None, "target": None}
        return {"action": "NONE", "score": 0, "why": []}
