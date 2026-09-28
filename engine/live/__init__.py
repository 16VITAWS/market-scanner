"""
Live trading (real money). Three paths, strictly separated from paper:

1. ASSISTED (no API)   - the portal turns signals into order tickets; the user places them in the broker app and
                         logs the fill ("I placed it"). Nothing is sent to any broker by this software.
2. API + APPROVAL       - the engine writes order PROPOSALS; the user approves each one by running the
                         "Approve live order" workflow on GitHub (authenticated); the RUNNER - which must run on a
                         machine whose static IP is whitelisted with the broker (SEBI rule from April 2026) - places
                         only approved, unexpired, hash-matching proposals through the broker's official API.
3. API + AUTO           - same runner, no per-order approval. OFF by default. Enabled only when ALL are true:
                         repo variable LIVE_AUTO=ON, repo variable LIVE_CONSENT equals the exact consent phrase,
                         the paper track-record gate passes, and hard limits (orders/day, value/order, daily loss) hold.
LIVE_KILL=ON (repo variable) stops everything at the next runner poll.
"""
