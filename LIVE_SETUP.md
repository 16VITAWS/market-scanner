# Live trading setup (real money) — plain words

**Two ways to trade live:**

## 1. Without API (works today, no cost)
Portal → **Live Desk → Without API**. Each signal becomes a ticket (quantity from your capital and 1% risk, limit price, stop, target, charges, max loss).
Open Groww, place the order yourself, add a GTT/OCO sell for stop and target, then tap **I placed it**. The portal tracks it and shows **EXIT suggested**
when the stop/target is reached, an exit signal appears, or the market regime turns BEAR. You decide and act every time.

## 2. With API (orders sent for you)
SEBI rule (from April 2026): API orders must come from a **static IP whitelisted with your broker**. GitHub's free servers don't have one,
so a small program — the **VISION runner** — runs on a machine that does.

1. **Groww Trade API**: Groww → Profile → Trade API. Buy the subscription and create a **TOTP** key (token + secret).
2. **Static-IP machine**: a small cloud VPS (e.g. DigitalOcean/AWS Lightsail/Google Cloud, ~$5/month) — or a static-IP internet plan.
3. **Install the runner** (on the VPS, one line):
   `curl -fsSL https://raw.githubusercontent.com/16VITAWS/market-scanner/main/runner/install.sh | bash`
   It prints the machine's public IP.
4. **Whitelist that IP** in Groww → API keys → Primary static IP → Update.
5. **Put your keys on the VPS only**: `nano ~/vision/runner.env` → fill `GROWW_TOTP_TOKEN`, `GROWW_TOTP_SECRET`. Keep `RUNNER_DRY_RUN=1`.
   `sudo systemctl restart vision-runner`. Never put these keys in GitHub or in the portal.
6. **Switch on in GitHub** → Settings → Secrets and variables → Actions → **Variables**:
   - `LIVE_TRADING` = `ON`
   - `LIVE_CAPITAL` = money you allow for live (e.g. `100000`)
   - `LIVE_MAX_ORDER_VALUE` = `25000` · `LIVE_MAX_ORDERS_DAY` = `3` · `LIVE_MAX_DAILY_LOSS` = `2000` · `LIVE_SEGMENTS` = `CASH`
7. **One week dry run**: the runner logs and notifies exactly what it would do and sends nothing. Check the Live Desk → Runner reports.
8. **Go live with approval**: set `RUNNER_DRY_RUN=0` in runner.env, restart. Each evening the engine makes proposals; you approve each one:
   GitHub → Actions → **Approve live order** → Run workflow → paste the ID → APPROVE → type YES. The runner places it (LIMIT, price re-checked)
   during market hours and adds an OCO stop-loss + target after it fills.

### Full auto (optional, gated)
No per-order approval. It switches on **only if all** hold: `LIVE_AUTO=ON`, `LIVE_CONSENT` = `I ACCEPT REAL MONEY RISK`, and the paper record passes
the gate (≥30 closed paper trades, profit factor ≥1.2, max drawdown better than −15%, positive expectancy after costs). Limits still apply.
Option spreads are never auto-placed; you get a two-leg ticket to place manually.

### Kill switch
- Instant: on the VPS `sudo systemctl stop vision-runner`.
- From phone: GitHub variable `LIVE_KILL` = `ON` (the runner sees it at the next engine publish).
- Open orders at the broker are **not** auto-cancelled — cancel them in the Groww app.

### Honest limits
- The runner is written against Groww's published SDK but has **not been tested on a live account**: start with the dry run.
- Signals have **no proven edge yet**; the paper record and the out-of-sample tests on the portal are the evidence to watch.
- Rules change: confirm current requirements with Groww/SEBI before going live. Not investment advice.
