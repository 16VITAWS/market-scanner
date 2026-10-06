"""
16VITAWS Trading Brain - a multi-layer, cost-aware decision engine for NSE cash equities (daily bars).

Layers (each module is independent and unit-tested):
  features   -> per-instrument features known at the close of day t (no future bars)
  regime     -> market regime (14 states) from NIFTY trend/vol, breadth, India VIX; strategy permission matrix;
                global risk score
  strategies -> ensemble of independent setups (trend-momentum, breakout, pullback, mean reversion,
                relative strength, volatility squeeze) + a simple benchmark; ONE shared trade-management rule set
  calibrate  -> event study over history: every setup simulated with the same fills/costs as the paper ledger;
                walk-forward probability + calibration check (Brier); per strategy x regime statistics;
                adaptive strategy weights / PAUSED-REVIEW status
  score      -> 0-100 trade-quality score, itemised, with unavailable components shown as unavailable
  ev         -> probability, expected gross / costs / slippage / net value for the ACTUAL position size
  sizing     -> risk-based position size, capital tiers, micro-capital economic filter, loss protections
  portfolio  -> correlated / sector exposure control
  decide     -> the pipeline that produces TRADE / WAIT / REJECT decision cards and the opportunity ranking
  montecarlo -> drawdown / ruin probabilities from the measured trade distribution
  daybook    -> day-by-day net P&L after costs with strategy attribution
Nothing here places a real order. The paper account IN-BRAIN executes TRADE decisions through the ledger.
"""
