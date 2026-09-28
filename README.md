# VISION AI v2.3 — paper-trading, market-intelligence & live-trading workspace

**New in 2.3:** market-regime model (3-state HMM: BULL / SIDEWAYS / BEAR with probabilities, expected duration and an out-of-sample check) ·
global-event → Indian-sector impact map (lag-aware betas of 10 NSE sector indices to US stocks, US yields, VIX, dollar, Brent, gold, copper, Asia;
2-sd shock alerts with expected move and band; headlines keyword-linked to drivers) · next-session probability-range chart · macro panel (FRED, World Bank) ·
**Big-Money Flows** (NSE bulk/block deals, delivery-% accumulation/distribution flags) · **Mutual Fund Scanner** (AMFI NAVs, direct-growth schemes ranked
inside their category on 3y return/Sharpe/drawdown; sector-tilt and regime-based allocation ideas; side-by-side compare) · AI confidence (shadow ML probability)
shown next to every scanner signal · your own server-side alert rules (repo variable `ALERT_RULES`) pushed to the phone · sector indices added.

**New in 2.2:** Intelligence screen (next-session NIFTY forecast with out-of-sample hit rate and calibration, pre-market run at 08:15 IST;
cross-market linkage monitor with rolling correlations, lead-lag and Granger tests; unusual-activity detector; machine-learning shadow model
compared walk-forward against the rule scanner) · execution-quality report (TWAP / VWAP / POV vs decision price) · SPAN-like margin estimates ·
event filters (results blackout, ex-dividend, F&O ban) · **Live Desk**: live trading without API (tickets + "I placed it" tracking) and with API
(Groww, approve-each-order via GitHub, gated auto mode, kill switch; orders placed only by the runner on your static-IP machine — see LIVE_SETUP.md).

**New in 2.1:** automatic index-options paper trading (NIFTY debit spreads, defined risk, Black-Scholes MODELED prices until a quote feed exists) · US large-cap auto paper account (S&P 500 regime) · push notifications to phone/PC via ntfy (signals, fills, options, stop/target touches, big market moves, detected every ~15 min intraday and at EOD) · installable app (PWA) for any phone or PC · VaR/CVaR, two-sided stress tests, Kelly (info) · square-root market-impact slippage. Live real-money execution remains locked and not implemented.

**Live portal:** https://16vitaws.github.io/market-scanner/ (GitHub Pages, branch `gh-pages`)
**Mode:** PAPER only. No broker is connected; live execution is not implemented and is locked by design.

## How it fits together
```
GitHub Actions (free)                       GitHub Pages (gh-pages branch, single commit, rewritten each run)
┌─────────────────────────────┐             ┌──────────────────────────────────────────────┐
│ engine/run.py eod|intraday  │  writes     │ index.html   (portal: reads api/*.json only)  │
│  providers/ yahoo (works)   │ ──────────▶ │ api/ snapshot quotes signals paper health …   │
│             angelone, td    │             │ api/candles/<ID>.json (compact OHLCV + DQ)    │
│             nseweb, manual  │             │ data/ ledger.json runs/ backups/ legacy/ raw/ │
│  dq → indicators → regime   │             └──────────────────────────────────────────────┘
│  strategies → risk → ledger │  reads state from the same branch before each run
│  simulator → analytics      │
│  backtest / walk-forward    │
└─────────────────────────────┘
```
- `main` branch = code + seed data. `gh-pages` = generated site + engine state. History of `gh-pages` never grows (orphan commit each run); every run's signals are kept as files in `data/runs/`, ledger backups in `data/backups/`.
- The portal is one HTML file (`docs/index.html`) copied into the site by each workflow. It never invents numbers: every tile shows the data date and a status label (LIVE / DELAYED / HISTORICAL / STALE / MANUAL / SIMULATED / PAPER).
- User data (manual paper book, real holdings typed by the user, watchlist, alerts, saved strategies) is local-first in the browser with export/import. Nothing is uploaded.

## Engine modules (`engine/`)
| File | Purpose |
|---|---|
| `config.py` | Mode, risk limits, fill model, default paper accounts. No secrets. |
| `instruments.py` | Symbol master: NSE stocks (official Nifty 500 list), Indian/US/EU/Asia indices, FX, commodities, ETFs, sample US stocks; provider tickers, currency, calendar. |
| `calendar.py` | Sessions and holidays per market (seed lists, `verified: false` until checked), live OPEN/CLOSED/HOLIDAY status. |
| `providers/` | `Yahoo` (working, unofficial, delayed), `AngelOne` and `TwelveData` (adapters awaiting keys, never fabricate), `NSEWeb` (best-effort option chain & FII/DII), `ManualCSV`. |
| `dq.py` | Data-quality checks (duplicates, bad OHLC, jumps, gaps vs calendar, staleness). Hard failures exclude a symbol from trading. |
| `indicators.py` | SMA/EMA/RSI/ATR/MACD/ADX/Bollinger/Donchian/VWAP/vol/beta/corr/S-R. Shifted windows: no look-ahead. |
| `strategies/` | Registry; `trend_breakout_swing` (paper-approved, v1.1.0, itemised score), `ma_cross` (baseline), spec-only cards for the other Vault ideas. |
| `risk.py` | Independent gate: kill switch, mode, data freshness, daily loss, drawdown, positions, size, exposure, cash, stop validity, risk/trade, sector. Every rejection has a reason and is stored on the order. |
| `ledger.py` | Decimal paper ledger: accounts, orders (market/limit/stop, partial fills, expiry, dedupe), fills with fee breakdown, positions, cash ledger, trades, equity, audit. Atomic saves. |
| `simulator.py` | Documented fill rules on OHLC bars; stop before target; liquidity cap; zero-volume halts. |
| `costs.py` | Indian statutory charges + broker brokerage table with verification status; US assumption. |
| `pipeline.py` | regime → scan → risk → paper orders → mark-to-market. |
| `backtest.py` | Event-driven backtester, metrics (CAGR, DD, Sharpe, Sortino, Calmar, PF, expectancy…), walk-forward, seasonality with Holm correction. |
| `analytics.py` | Equity/drawdown/monthly stats, rolling win-rate, rule-based diagnosis, bootstrap outcome range (labelled, not a forecast). |
| `options.py` | Black-Scholes pricing/greeks, expiry calendar, modeled chains, defined-risk debit-spread strategy, options paper account. |
| `notify.py` | ntfy.sh push (+ Telegram if configured), dedupe, notification feed. |
| `news.py` | Google News RSS, dedupe, keyword sentiment (low confidence, labelled). |
| `knowledge.py` | Seed event-impact graph (documented mechanisms) + computed correlations/betas. |
| `reports.py` | Pre-market / post-market / weekend automated observations. |
| `api.py`, `run.py`, `notify.py` | Output writers, jobs, Telegram (only if secrets exist). |

## Run locally
```
pip install -r requirements.txt
python -m pytest -q tests                    # 29 tests
python -m engine.run eod --site /tmp/site --offline   # uses the preserved NIFTY CSV, no network
python -m engine.run eod --site /tmp/site             # real run (needs internet to Yahoo)
```

## Secrets (GitHub → Settings → Secrets and variables → Actions)
`TELEGRAM_TOKEN`, `TELEGRAM_CHAT_ID` (alerts); `ANGEL_API_KEY`, `ANGEL_CLIENT_ID`, `ANGEL_PASSWORD`, `ANGEL_TOTP_SECRET` (optional, adapter not yet live-tested); `TWELVEDATA_API_KEY` (optional). Variable `VISION_MODE=RESEARCH` pauses paper trading.

## Not built (honest list)
Live broker execution (locked, requires keys + algo approval + review); intraday strategy execution (quotes refresh only); options paper trading (needs live chain); fundamentals; account-based multi-device sync; verified holiday calendars; corporate-actions feed; Angel One / Twelve Data adapters are untested until keys exist.

Not investment advice. Paper trading only.
