"""Settings you can change. Everything else runs by itself."""

CAPITAL = 200000          # your trading capital in rupees
RISK_PER_TRADE = 0.01     # risk 1% of capital per trade (loss if stop-loss hits)
MAX_PICKS = 5             # show at most this many buys / sells

BUY_SCORE = 5             # score needed for a BUY
SELL_SCORE = -5           # score needed for a SELL / EXIT
STOP_ATR = 2.0            # stop-loss = entry - 2 x ATR
TARGET_ATR = 3.0          # target    = entry + 3 x ATR

MIN_PRICE = 50            # skip very low-priced stocks
MIN_TURNOVER_CR = 10      # skip stocks trading less than Rs 10 crore a day (hard to buy/sell)

# Headlines containing these words downgrade a BUY to WAIT until you check them
NEWS_RISK_WORDS = ["sebi", "fraud", "raid", "probe", "resign", "downgrade", "default",
                   "penalty", "ban", "lawsuit", "insolvency", "pledge", "fire", "shutdown",
                   "results", "q1", "q2", "q3", "q4", "earnings"]

# ---- MODE (one word, never mixed) ----
# RESEARCH               = analysis and reports only, nothing traded
# PAPER                  = automatic paper trading with simulated funds (default)
# APPROVAL               = real orders only after you tap "Approve and Place Order" (needs broker API)
# CONTROLLED_AUTOMATION  = automatic real orders inside risk limits (needs broker API, algo approval, kill-switch PIN)
MODE = "PAPER"
LIVE_MODE = MODE in ("APPROVAL", "CONTROLLED_AUTOMATION")
AUTO_LIVE = MODE == "CONTROLLED_AUTOMATION"
MAX_ORDERS_PER_DAY = 10
MAX_DAILY_LOSS = 0.02          # 2% of capital, then no more trades that day
MAX_VOLUME_SHARE = 0.05        # never take more than 5% of a stock's average daily volume
MAX_DATA_AGE_DAYS = 4          # data older than this is STALE: report only, no trades
