# VISION AI v2 — paper-trading & market-intelligence workspace

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
| `news.py` | Google News RSS, dedupe, keyword sentiment (low confidence, labelled). |
| `knowledge.py` | Seed event-impact graph (documented mechanisms) + computed correlations/betas. |
| `reports.py` | Pre-market / post-market / weekend automated observations. |
| `api.py`, `run.py`, `notify.py` | Output writers, jobs, Telegram (only if secrets exist). |

## Run locally
```
pip install -r requirements.txt
python -m pytest -q tests                    # 23 tests
python -m engine.run eod --site /tmp/site --offline   # uses the preserved NIFTY CSV, no network
python -m engine.run eod --site /tmp/site             # real run (needs internet to Yahoo)
```

## Secrets (GitHub → Settings → Secrets and variables → Actions)
`TELEGRAM_TOKEN`, `TELEGRAM_CHAT_ID` (alerts); `ANGEL_API_KEY`, `ANGEL_CLIENT_ID`, `ANGEL_PASSWORD`, `ANGEL_TOTP_SECRET` (optional, adapter not yet live-tested); `TWELVEDATA_API_KEY` (optional). Variable `VISION_MODE=RESEARCH` pauses paper trading.

## Not built (honest list)
Live broker execution (locked, requires keys + algo approval + review); intraday strategy execution (quotes refresh only); options paper trading (needs live chain); fundamentals; account-based multi-device sync; verified holiday calendars; corporate-actions feed; Angel One / Twelve Data adapters are untested until keys exist.

Not investment advice. Paper trading only.
