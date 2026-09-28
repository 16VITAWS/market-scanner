# VISION AI — user manual (plain words)

Open **https://16vitaws.github.io/market-scanner/** on your phone or laptop. Add it to the home screen. Everything updates by itself; tap **Refresh** any time.

## The 17 screens
1. **Command Center** — the verdict (BULL / BEAR / SIDEWAYS), today's buy / sell / blocked list with every reason, the automatic paper account, world heat-map, latest automated observation, system status.
2. **Global Markets** — India, US, Europe, Asia, currencies, commodities; market hours; correlation matrix. Each tile shows its date and status.
3. **Charts** — pick any symbol that has a candle file; SMA/EMA/Bollinger, RSI, support/resistance, engine fills and signals drawn on the chart; place a paper order from the chart.
4. **AI Scanner** — every scored stock with the exact rules that made the score; filters.
5. **Strategy Lab** — the strategy catalogue with full rules; describe an idea in words → rules → backtest in the browser with a train/test split.
6. **Paper Trading** — *Manual*: buy/sell any time (holidays too) at the last price with real charges; limit/stop orders. *Automatic*: what the engine did. *Replay*: practise on any past day of any symbol.
7. **Portfolio & Growth** — equity vs NIFTY, drawdown, monthly P&L, rolling win rate, "what to change" diagnosis, and a labelled range of outcomes if the same statistics continued.
8. **Small-Budget Plan** — type your money; see what fits, how many shares, stop, max loss, target, when it sells.
9. **Options** — chain (partial until a feed exists; upload NSE CSV), payoff calculator.
10. **News & Events** — fetched headlines with links, keyword sentiment (low confidence), FII/DII, event → impact map with evidence.
11. **Research & Backtests** — v1 numbers vs independent reproduction, walk-forward, seasonality.
12. **IPOs** — the 7 issues you entered, marked unverified, with official links.
13. **Broker Desk** — login buttons for Groww, Zerodha, Dhan, Fyers, Angel One, Upstox, Shoonya, Alice Blue, Pocketful, IBKR; charge comparison; API capability matrix.
14. **Alerts** — price / signal / RSI / regime / stale-data alerts, browser notifications.
15. **Data Center** — providers, licences, quality report, calendars, export / import / delete your local data.
16. **Engine & Health** — tests, modules audit, run history, engine controls (pause / resume / reset / emergency stop).
17. **Settings & Security** — broker for charges, watchlist, budget; what is and is not stored.

## Daily rhythm
- 09:15–15:30 IST weekdays: quotes refresh every ~15 minutes (delayed); positions are marked.
- 16:20 IST weekdays: full scan, paper orders for tomorrow's open, reports, tests.
- Saturday 09:00: weekend review.
- Telegram message after each evening run once you add the bot token (Engine tab → Secrets).

## Controls
- **Pause automatic paper trading**: GitHub → Settings → Secrets and variables → Actions → Variables → add `VISION_MODE` = `RESEARCH`. Delete it to resume.
- **Emergency stop**: GitHub → Actions → each workflow → "…" → Disable workflow.
- **Reset the automatic account**: delete `data/ledger.json` on the `gh-pages` branch (backups in `data/backups/`).
- **Run now**: GitHub → Actions → "EOD scan + paper trading" → Run workflow.

## What it will never do
Place a real order; show a static number as live; fill a gap with invented candles; promise a return.
