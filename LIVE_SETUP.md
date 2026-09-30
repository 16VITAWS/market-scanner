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

## VISION LIVE (realtime, every tick) - `runner/vision_live.py`

**Default broker: Shoonya (Finvasia), free API.**

1. **Get your API details.** On Shoonya's API key page, note your user id, API client id and secret code. Set the redirect URL to `http://127.0.0.1:8765/shoonya/callback`.
2. **Enter them once.** Put `SHOONYA_UID`, `SHOONYA_CLIENT_ID` and `SHOONYA_SECRET` in `%USERPROFILE%\vision_live\settings.env` (`BROKER=SHOONYA`).
3. **Log in each trading day.** Click **Login to Shoonya** on the live screen and log in on Shoonya's own page with your password and authenticator code. Shoonya returns a one-day token (there's no token renewal in Shoonya's API).
   - Your password is never stored by VISION LIVE.
   - If the redirect doesn't come back to the screen, paste the address from that tab into the box on the screen.
4. **Compatibility is tested.** The token exchange (`GenAcsTok`, sha256(client_id + secret + code)) and order payloads (`PlaceOrder`) are compared in the tests against the official NorenRestApiOAuth SDK.

Angel One remains available with `BROKER=ANGEL`, using the steps below.

The portal itself can only refresh about every 2 minutes. That's a GitHub limit, and its free data is also ~15 minutes delayed. For live trading run **VISION LIVE** on your laptop during market hours:

1. **Get an Angel One account and API app.** Open a free Angel One account, create a SmartAPI app at smartapi.angelone.in and enable TOTP at smartapi.angelone.in/enable-totp. SmartAPI and its live WebSocket feed are free.
2. **Start it.** Download `VISION-LIVE.bat` from the portal's Live Desk and double-click it. The first run installs Python and the official `smartapi-python` SDK, then opens `%USERPROFILE%\vision_live\settings.env` in Notepad. Fill in `ANGEL_API_KEY`, `ANGEL_CLIENT_ID`, `ANGEL_PASSWORD` (login PIN) and `ANGEL_TOTP_SECRET`, then save.
3. **Watch the live screen.** It opens at http://127.0.0.1:8765 and is only reachable from your own computer. Prices update on every exchange trade. Stop-loss and target are checked on every tick. Entries follow the portal's BUY signals, and only when the live price is within 0.5% of the signal's entry.
4. **Choose a mode:**
   - PAPER (default): simulated fills at the live price.
   - ALERT: PAPER plus a phone notification so you place the order yourself.
   - REAL: Angel One LIMIT orders, delivery product. Refused unless:
     - `CONSENT=I ACCEPT REAL MONEY RISK`
     - `STATIC_IP_REGISTERED=yes`. SEBI: from 1 Apr 2026, API orders are accepted only from your registered static IP. Market data doesn't need one.
     - The live-paper record has 30 or more closed trades, profit factor ≥ 1.2 and positive expectancy.
     - The kill switch is off.

   Order limits in REAL: orders per day, value per order, open positions and daily loss.
5. **Stop it.** The kill switch is the red button on the screen; the portal's kill switch also stops it. Resuming requires typing RESUME.

Everything is logged to `%USERPROFILE%\vision_live\audit.jsonl`. Secrets never leave the laptop except to Angel One, and are scrubbed from logs. Status: built on the official SDK and covered by tests (binary tick parsing with the SDK's own parser, validation, stop/target on live ticks, limits, locks). It has **not yet been run against a live Angel One account**.
