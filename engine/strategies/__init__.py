"""
Strategy registry. A strategy is a versioned spec + a pure function:

    signals(df_enriched, context) -> dict(score, action in {BUY, SELL, NONE}, why[list], stop, target)

Strategies never touch the ledger or place orders; they only produce inspectable signals.
Status lifecycle: experimental -> backtested -> paper-approved -> disabled.
"""
from .trend_breakout import TrendBreakoutSwing
from .ma_cross import MACross
from .specs import SPEC_ONLY

REGISTRY = {s.id: s for s in [TrendBreakoutSwing(), MACross(fast=21, slow=50)]}


def get(strategy_id):
    return REGISTRY[strategy_id]


def catalogue():
    """Every strategy card the portal shows: live code strategies plus spec-only ideas."""
    out = [s.spec() for s in REGISTRY.values()]
    out += SPEC_ONLY
    return out
