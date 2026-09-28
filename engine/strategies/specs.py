"""
Strategy ideas that exist as SPECIFICATIONS ONLY (no code, no backtest). The portal shows them
as cards with status so nothing looks more finished than it is. Four are rebuilt in spirit from
public BullsAI Vault card descriptions; no proprietary code was copied.
"""
SPEC_ONLY = [
    {"id": "vol_trend_atr", "name": "Volatility Trend ATR", "version": "0.1-spec", "status": "experimental", "code": False,
     "universe": "Large caps (e.g. TCS)", "timeframe": "5m / 15m", "segment": "intraday", "min_capital": 10000, "holding": "intraday",
     "hypothesis": "ATR expansion after a quiet period marks the start of an intraday trend; enter with the expansion, exit on ATR contraction or a 1.5xATR trailing stop.",
     "rules": {"entry": "close > VWAP and ATR(14) rises above 1.3x its 20-bar median, in the direction of the 20-bar slope", "exit": "trailing stop 1.5xATR or 15:15 square-off", "sizing": "0.5% risk"},
     "failure_modes": ["needs intraday data (not available yet)", "fake expansions at open", "square-off costs"],
     "blocked_by": "intraday data feed", "inspired_by": "BullsAI Vault card (public description)"},
    {"id": "nifty_short_straddle_sl", "name": "Nifty 50 Short Straddle with Stop-loss", "version": "0.1-spec", "status": "disabled", "code": False,
     "universe": "NIFTY weekly options", "timeframe": "intraday", "segment": "options", "min_capital": 300000, "holding": "intraday",
     "hypothesis": "Selling the ATM straddle collects time decay on quiet days; per-leg stop-loss at 30% of premium limits blow-ups.",
     "rules": {"entry": "09:20 sell ATM CE and PE", "exit": "each leg: stop at +30% premium; both: 15:10 square-off", "sizing": "1 lot per 3 lakh"},
     "failure_modes": ["gap moves and event days: losses far beyond the credit", "requires live option chain and margin model", "SEBI lot-size and margin rules"],
     "blocked_by": "live option chain + margin model + proven paper record", "inspired_by": "BullsAI Vault card (public description)"},
    {"id": "nifty_breakout_precision", "name": "Nifty Breakout Precision", "version": "0.1-spec", "status": "experimental", "code": False,
     "universe": "NIFTY futures", "timeframe": "5m", "segment": "futures", "min_capital": 600000, "holding": "intraday",
     "hypothesis": "A break of the first-hour range on NIFTY futures, in the direction of the daily trend, continues into the afternoon.",
     "rules": {"entry": "break of 09:15-10:15 range with volume; one re-entry after a stop", "exit": "1:1 risk:reward or 15:15", "sizing": "1 lot"},
     "failure_modes": ["range-bound days", "needs intraday futures data"], "blocked_by": "intraday futures data", "inspired_by": "BullsAI Vault card (public description)"},
    {"id": "nifty_channel_surge", "name": "Nifty Channel Surge (futures)", "version": "0.1-spec", "status": "experimental", "code": False,
     "universe": "NIFTY futures", "timeframe": "1d", "segment": "futures", "min_capital": 250000, "holding": "days to weeks",
     "hypothesis": "A close outside a 20-day Donchian channel starts a positional move; ride it with a 2xATR trailing stop.",
     "rules": {"entry": "close > 20-day Donchian upper (long) / < lower (short)", "exit": "2xATR trailing stop", "sizing": "1 lot"},
     "failure_modes": ["whipsaws", "rollover costs"], "blocked_by": "can be backtested on NIFTY spot proxy now; futures basis not modelled",
     "inspired_by": "BullsAI Vault card (public description)"},
    {"id": "nifty_gold_pair", "name": "Nifty ETF vs Gold ETF pair", "version": "0.1-spec", "status": "experimental", "code": False,
     "universe": "NIFTYBEES / GOLDBEES", "timeframe": "1d", "segment": "delivery", "min_capital": 50000, "holding": "weeks",
     "hypothesis": "The 60-day normalised return spread between NIFTYBEES and GOLDBEES mean-reverts; buy the laggard when the spread z-score < -2, exit at 0.",
     "rules": {"entry": "z-score of 60-day return spread < -2", "exit": "z-score crosses 0 or 40 bars", "sizing": "equal rupee legs, long only (no ETF shorting in delivery)"},
     "failure_modes": ["structural regime shifts (gold rally with equity crash)", "spread can widen for months"],
     "blocked_by": "needs GOLDBEES history download (available via Yahoo) - queued for backtest"},
]
