"""
Strategy registry. A strategy is a versioned spec + a pure function:

    signals(df_enriched, context) -> dict(score, action in {BUY, SELL, NONE}, why[list], stop, target)

Strategies never touch the ledger or place orders; they only produce inspectable signals.
Status lifecycle: experimental -> backtested -> paper-approved -> disabled.
"""
from .trend_breakout import TrendBreakoutSwing, TrendBreakoutSwingUS
from .ma_cross import MACross
from .specs import SPEC_ONLY

REGISTRY = {s.id: s for s in [TrendBreakoutSwing(), TrendBreakoutSwingUS(), MACross(fast=21, slow=50)]}


def get(strategy_id):
    return REGISTRY[strategy_id]


def catalogue():
    """Every strategy card the portal shows: live code strategies plus spec-only ideas."""
    out = [s.spec() for s in REGISTRY.values()]
    out += [OPTIONS_SPEC]
    out += SPEC_ONLY
    return out


OPTIONS_SPEC = {"id": "index_options_regime", "name": "NIFTY Options: Regime Debit Spreads", "version": "1.0.0", "status": "experimental", "code": True,
    "universe": "NIFTY weekly options (modeled prices until a quote feed exists)", "timeframe": "1d", "segment": "options", "min_capital": 50000,
    "holding": "2-10 sessions", "hypothesis": "In a confirmed downtrend with weak momentum, a defined-risk put spread captures continuation with capped loss; mirror for confirmed breakouts in uptrends.",
    "rules": {"entry": "BEAR + close<SMA50 + RSI<45 -> buy ATM put / sell put 200 pts lower; BULL + 20-day breakout -> call spread", "exit": "+50% of max profit, -50% of debit, 2 days to expiry, or regime flip",
              "sizing": "max loss (known in advance) <= 3% of account equity, whole lots only; max 2 open spreads (max 6% total at risk)", "never": "no naked short options"},
    "failure_modes": ["prices are Black-Scholes MODELED, not quotes: real fills differ (spreads, skew)", "gap moves through the short strike cap the gain", "IV crush after events", "lot-size / expiry rules must be verified"],
    "params": {"width": 200, "risk_pct": 0.03, "take_profit": 0.5, "stop_loss": 0.5}}
