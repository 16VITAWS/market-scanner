"""
VISION AI engine settings. Plain values; nothing here is a secret.
Secrets (broker keys, Telegram token) come ONLY from environment variables / GitHub Secrets.
"""
import os

ENGINE_VERSION = "2.3.0"

# ---- MODE (one word, never mixed) --------------------------------------------
# RESEARCH               analysis and reports only, nothing traded
# PAPER                  automatic paper trading with simulated funds (default)
# APPROVAL               real orders only after the user taps Approve (needs broker API) - NOT BUILT
# CONTROLLED_AUTOMATION  automatic real orders inside risk limits - NOT BUILT
MODE = os.environ.get("VISION_MODE", "PAPER")
LIVE_MODE = MODE in ("APPROVAL", "CONTROLLED_AUTOMATION")
LIVE_EXECUTION_IMPLEMENTED = False          # hard fact: no broker adapter is wired to a real order path

# ---- paper accounts ------------------------------------------------------------
DEFAULT_ACCOUNTS = [
    # id, name, currency, starting cash, which strategies may trade it
    {"id": "IN-SWING", "name": "India swing (auto)", "currency": "INR", "cash": "200000", "strategies": ["trend_breakout_swing"]},
    {"id": "IN-MANUAL", "name": "India manual practice", "currency": "INR", "cash": "200000", "strategies": []},
    {"id": "US-MANUAL", "name": "US manual practice", "currency": "USD", "cash": "10000", "strategies": []},
    {"id": "US-SWING", "name": "US swing (auto)", "currency": "USD", "cash": "10000", "strategies": ["trend_breakout_swing_us"]},
    {"id": "IN-OPTIONS", "name": "India index options (auto, modeled prices)", "currency": "INR", "cash": "200000", "strategies": ["index_options_regime"]},
]

# ---- risk limits (the risk engine enforces these; strategies cannot override) ---
RISK = {
    "max_risk_per_trade": 0.01,       # 1% of account equity between entry and stop
    "max_daily_loss": 0.02,           # 2% of equity -> no new entries for the day
    "max_drawdown": 0.15,             # 15% from peak -> all strategies paused
    "max_open_positions": 5,
    "max_position_pct": 0.25,         # single position <= 25% of equity
    "max_gross_exposure": 1.0,        # no leverage in paper equities
    "max_sector_pct": 0.40,
    "max_volume_share": 0.05,         # <= 5% of 20-day average volume
    "min_price": 50,
    "min_turnover_cr": 10,            # 20-day average traded value, INR crore
    "max_data_age_days": 4,           # older -> STALE: report only, no new orders
    "max_orders_per_run": 10,
}

US_LIMITS = {"min_price": 10, "min_turnover_cr": 5}      # US: $10 min price, ~$50M/day traded value

# ---- notifications (ntfy.sh: free push to phone/PC; topic is a random name, not a secret password)
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "vision-ai-16vitaws-k7q2m9x4")
NTFY_SERVER = "https://ntfy.sh"
ALERTS = {"index_move_pct": 1.0, "vix_move_pct": 8.0, "stock_move_pct": 4.0}

# ---- strategy defaults ---------------------------------------------------------
SIGNALS = {
    "buy_score": 5, "sell_score": -5, "max_picks": 5,
    "stop_atr": 2.0, "target_atr": 3.0, "trail_after_pct": 0.05, "trail_pct": 0.04,
    "max_hold_days": 20,
}

NEWS_RISK_WORDS = ["sebi", "fraud", "raid", "probe", "resign", "downgrade", "default",
                   "penalty", "ban", "lawsuit", "insolvency", "pledge", "fire", "shutdown",
                   "results", "q1", "q2", "q3", "q4", "earnings"]

# ---- execution model for paper fills ------------------------------------------
FILL = {
    "slippage_pct": 0.001,            # 0.10% against you on every fill (OHLC-only approximation)
    "fill_rule": "next_bar_open",     # decisions on bar t fill at open of bar t+1 (no look-ahead)
}

# ---- data ------------------------------------------------------------------------
UNIVERSE_URL = "https://archives.nseindia.com/content/indices/ind_nifty500list.csv"
HISTORY_YEARS = 3
PATHS = {
    "data": "data", "paper": "data/paper", "runs": "data/runs", "api": "docs/api",
    "raw": "data/raw", "legacy": "data/legacy", "candles": "docs/api/candles",
}

# ---- provider keys: names of the environment variables (values never in code) ----
ENV = {
    "angelone": ["ANGEL_API_KEY", "ANGEL_CLIENT_ID", "ANGEL_PASSWORD", "ANGEL_TOTP_SECRET"],
    "twelvedata": ["TWELVEDATA_API_KEY"],
    "telegram": ["TELEGRAM_TOKEN", "TELEGRAM_CHAT_ID"],
    "groww": ["GROWW_API_KEY", "GROWW_API_SECRET"],
}
