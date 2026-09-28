# Audit & checklist (27 Sep 2026)

## What exists
- Portal (Claude artifact): command center, chart, news, search, vault, paper book, brokers, research, engine files.
  Static page: shows real data entered on 25–26 Sep; cannot fetch live data itself.
- Engine (GitHub 16VITAWS/market-scanner): scanner.py, paper.py, execution.py, dataquality.py, config.py,
  daily-scan.yml. Runs weekdays 4:15 pm; live report at https://16vitaws.github.io/market-scanner/

## Works (tested, 9 automated tests pass)
- Indicators (SMA/RSI/ATR), no look-ahead in breakout levels
- Market-mood regime; buys blocked in BEAR
- Position sizing (1% risk, liquidity cap 5% of avg volume)
- Paper fills at next open + 0.1% slippage + full charges; stop/target/time/regime exits
- Risk gate: hours, stale data, daily loss, size cap, stop validity; duplicate-order block; kill switch
- Data-quality checks: duplicates, gaps, bad OHLC, zero volume -> symbol excluded
- Signals store data date and computation time; MODE shown on every report

## Broken / missing (honest)
- No live intraday data (end-of-day only, Yahoo unofficial)
- No broker connection (Groww API key + SDK + algo approval missing)
- Telegram alerts not configured (needs your bot token)
- Options, F&O, international: analysis only, no data feed
- Portal does not auto-refresh from the engine (GitHub page is the live view)
- Walk-forward / Monte Carlo not yet run on the swing strategy
- Backtest: only 21/50 on Nifty done with costs; swing strategy backtest pending

## Modes (never mixed)
RESEARCH -> PAPER (now) -> APPROVAL (you tap Approve) -> CONTROLLED_AUTOMATION (last, only with approval + kill-switch PIN)

## Next steps in order
1. Commit repaired files to GitHub (execution.py, dataquality.py, config.py, scanner.py, paper.py, tests)
2. Groww API key -> holdings sync + APPROVAL mode
3. Backtest swing strategy 5y + walk-forward
4. Telegram alerts
5. Second strategy (Nifty ETF–Gold ETF pair) in paper
