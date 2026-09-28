from .base import Provider, ProviderError, standardise, meta
from .yahoo import Yahoo
from .stubs import AngelOne, TwelveData, NSEWeb, ManualCSV

REGISTRY = {p.id: p for p in [Yahoo, AngelOne, TwelveData, NSEWeb, ManualCSV]}


def get(provider_id):
    return REGISTRY[provider_id]()


def capabilities():
    return [get(k).capabilities() for k in REGISTRY]


def ticker_for(instr, provider_id):
    """Map an instrument row from the symbol master to the provider's ticker."""
    col = {"yahoo": "yahoo", "twelvedata": "twelvedata", "angelone": "angel"}.get(provider_id)
    t = instr.get(col) if col else None
    return t if isinstance(t, str) and t else None
