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
