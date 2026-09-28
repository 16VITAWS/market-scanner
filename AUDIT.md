# VISION AI — Audit of the existing portal and engine (27 Sep 2026)

## What was inspected
- **Portal v1**: Claude artifact "VISION AI" (273 KB single HTML, 15 tabs, all data embedded in one JS object `D0`, user data in `localStorage`). Saved unchanged as `legacy/portal_v1_artifact.html`.
- **Engine v1 (GitHub `16VITAWS/market-scanner`, main)**: `scanner.py`, `paper.py`, `config.py`, `stocks.txt`, `requirements.txt`, `.github/workflows/daily-scan.yml`, `docs/index.html` (big-text dashboard), `data/signals_2026-09-25.json`, empty `data/paper_log.csv`. One successful run (26 Sep 2026, 494 stocks).
- **Engine v1 "repaired" files** embedded in the portal's Engine tab but **never committed**: newer `scanner.py`, `config.py` (MODE, limits), `paper.py`, plus `execution.py`, `dataquality.py`, `test_engine.py`, `AUDIT.md`. Preserved in `legacy/`.

## What worked (v1)
| Item | Verdict | Notes |
|---|---|---|
| NIFTY history 2018-09-14 → 2026-09-11 (1,970 rows) | Real | Portal copy has O/H/L/C only — **no Volume column** (the course CSV reportedly has one; not uploaded). Now `data/raw/nifty_daily_ohlc_from_portal.csv` + SHA-256. |
| 25 Sep 2026 close 23,140.50 | Manual | Kept as a separate MANUAL bar; 12–24 Sep gap **not** filled. |
| Daily scan on GitHub Actions | Working | Yahoo (unofficial) EOD, 2-year history, rule score, regime filter, next-open paper fills. |
| Chart / replay (NIFTY daily only) | Working | Home-grown SVG; no zoom; single instrument. |
| Manual paper book (prices typed) | Working | Local only, no export. |
| Broker table | Partly verified | Groww/Dhan/Angel/Shoonya figures partly checked; others `null`. |
| 21/50 backtests | Unverified | Numbers typed in; calculation not in the repo. |

## What was simulated or misleading (v1)
- "AI ANALYSIS" run button: a scripted animation over static numbers (regime from embedded candles; funnel from the 25 Sep run). Not a live scan.
- Market snapshot, FII/DII, VIX, USD/INR, global closes, IPOs, news, weekend scan: **all typed in on 25–26 Sep** and shown without a stale label once days passed.
- Option chain: 5 strikes from a screenshot presented in a full-chain table.
- Dropdowns with one option (Replay: NIFTY/daily; options: NIFTY/29-Sep; broker: Groww).
- Modules tab marked "WORKING" for features whose UI existed but whose data was static.
- "Outlook · analyst scenarios" 25/45/30 shown next to gauges as if computed.
- No auto-refresh: the artifact could not fetch anything (static page).

## What was broken / missing
- Repo main lacked `execution.py`, `dataquality.py`, tests; `AUDIT.md` step 1 ("commit repaired files") never happened.
- Backtest reproduction: **9/21 long+short reported −101% could not be reproduced** (SMA version gives +177%); buy-and-hold "+406%" with 1 lot on ₹2 lakh hides a >100% drawdown (wiped out in March 2020).
- No intraday data, no options feed, no FII/DII feed, no news pipeline, no global instruments, no correlations, no risk engine independent of the strategy, no Decimal ledger, no order types, no walk-forward, no data-quality reporting to the user, no export/import of user data, no market calendar, no per-symbol charts.
- Telegram token never configured.

## Data provenance now preserved
`data/raw/` (NIFTY CSV + checksum), `data/legacy/` (latest, news, levels, flows, globalmkt, scan, engine run, chain, ipos), `data/runs/signals_2026-09-25.json`, `legacy/` (all v1 code and both v1 HTML pages).

## What v2 changes
See `README.md` (architecture) and `MANUAL.md` (how to use). In one line: the engine now writes `api/*.json` on GitHub Pages and the portal only reads it; nothing static is shown as live.
