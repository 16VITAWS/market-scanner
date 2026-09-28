# VISION AI — user manual (plain words)

Open **https://16vitaws.github.io/market-scanner/** on your phone or laptop. Add it to the home screen. Everything updates by itself; tap **Refresh** any time.

## Get alerts on your phone and PC (one-time, 1 minute)
Install the free **ntfy** app (Android / iPhone), tap **+**, type `vision-ai-16vitaws-k7q2m9x4`, Subscribe. On a PC open https://ntfy.sh/vision-ai-16vitaws-k7q2m9x4 and allow notifications. You'll get new buy/exit signals (India + US), options entries/exits, paper fills, stop/target touches and big market moves automatically.

## Install the portal as an app
On the portal tap **⬇ Install app** (Chrome/Edge on PC or Android), or on iPhone Safari: Share → Add to Home Screen.

## The screens (21 in v2.3; new ones described at the end)
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


## New in 2.3 (plain words)
- **Intelligence → Market regime:** says whether NIFTY behaves like an uptrend, a range or a downtrend right now, how sure the model is, and how long such phases usually last. It describes the recent past; the out-of-sample box says whether it has helped predict anything.
- **Intelligence → Global events → Indian sectors:** when oil, the dollar, US stocks or US yields make an unusually big move, you get a push and a table: which Indian sector indices usually move the next day, by how much, and the headlines that may explain it. Statistics, not certainty.
- **Big-Money Flows:** bulk and block deals (who bought/sold big, as disclosed by NSE) and stocks where heavy volume came with high delivery (possible accumulation) — end-of-day only.
- **Mutual Funds:** every direct-growth fund in the main categories, ranked against its own category. Tick 2–4 funds and press Compare. Allocation ideas explain their reason. Check expense ratio and holdings on the AMC site before investing.
- **AI p on the scanner:** the machine-learning model's probability that the stock gains 1%+ in 10 sessions. AGREES / DISAGREES tells you if it supports the rule signal. It never trades by itself.
- **Your own alerts on the phone (Alerts screen):** build a rule line such as `RELIANCE>3000; NIFTY%<-1.5`, copy it, open GitHub variables, create `ALERT_RULES` and paste. Done once; change it any time.
- **Telegram (optional):** add repository secrets `TELEGRAM_TOKEN` and `TELEGRAM_CHAT_ID`; every push is also sent there.
