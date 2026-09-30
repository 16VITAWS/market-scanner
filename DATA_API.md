# VISION AI data API (v2.8)

The portal has no server of its own. The engine runs on GitHub Actions and publishes **read-only JSON files**. Those files are the API: any browser, script or app can read them over HTTPS. There are no credentials and nothing to write, so there's no authentication. Provider keys never appear in any file.

| Base | Updated | Contents |
|---|---|---|
| `https://16vitaws.github.io/market-scanner/api/` | End-of-day runs (16:20 and 18:45 IST), pre-market, weekend | Everything below |
| `https://raw.githubusercontent.com/16VITAWS/market-scanner/live-data/api/` | Every ~2 min while NSE or US is open | `quotes`, `paper`, `snapshot`, `notifications`, `live_status`, `signals`, `pipeline` |

All timestamps are ISO-8601 with an explicit offset: UTC for machine fields, IST for `generated_at`. The UI converts to IST (`Asia/Kolkata`).

## Quote record (`quotes.json → quotes.<SYMBOL>`)

```json
{"sym": "RELIANCE", "ex": "NSE", "p": 1400.5, "pc": 1390.0, "chg_pct": 0.76,
 "t": "2026-09-30T05:45:02+00:00", "recv": "2026-09-30T05:45:40+00:00",
 "src": "angelone", "delay_min": 0, "status": "REALTIME",
 "day_o": 1392, "day_h": 1405, "day_l": 1388, "day_v": 12345, "vwap": 1398.2, "bid": 1400.4, "ask": 1400.6,
 "intraday": true, "flags": []}
```

- `t` is the market timestamp (bar/trade time) and `recv` is when the engine received the quote. `src` and `delay_min` identify the provider: `yahoo` is 15 min delayed and `angelone` is realtime.
- **Freshness is computed, not stored.** The rules:
  - LIVE: `delay_min` is 0 and `t` is 60 s old or less while the market is open.
  - DELAYED: `t` is within `delay_min` + 10 min.
  - STALE: `t` is older than that window while the market is open.
  - CLOSED: the market is closed.
  - OFFLINE: there's no valid `p` or `t`.

  The engine (`engine/quality.py`) and the portal (`fresh()`) use identical rules.
- **Validation.** Every incoming record is checked for missing or non-numeric fields, a non-positive price, negative volume, inconsistent OHLC, timestamps in the future or out of order, and impossible moves (more than 20.5% vs previous close for NSE equities, more than 35% for indices).
  - A rejected record is never written. The previous valid quote stays, with `last_rejected: {reason, at, src}`.
  - Soft issues are kept as `flags`, for example `duplicate tick`.

## Files

| File | Contains |
|---|---|
| `meta.json` | engine version, build time, `data_status` (OK / BEHIND / STALE), last bar date |
| `quotes.json` | quote records above |
| `snapshot.json` | indices, FX, commodities, rates, sectors (quote records) |
| `calendar.json` | `markets.<ID>.state` (OPEN / CLOSED / PRE / POST + reason) for NSE, US, UK, EU, JP, … and holiday lists (seed, marked unverified) |
| `candles/<ID>.json` | daily OHLCV `{t (UTC), o, h, l, c, v}` plus `meta` (provider, delay, fetched_at) |
| `candles_index.json` | which symbols and intervals have candle files |
| `live_status.json` | session-refresh cycle, open markets, validation (accepted / rejected + sample), freshness counts, realtime provider |
| `pipeline.json` | job log (job, started, finished, seconds, status, processed, failed, provider, error), `last_ok` per job, provider health (requests, errors, latency, last OK / error, rate-limit) |
| `news.json` | headlines with publisher, link, time, matched symbols |
| `value.json` | long-term value scores, `recommendations[]` (model output) |
| `signals.json`, `signals_us.json` | rule-based signals (model output) with the bar they were computed on |
| `paper.json` | paper accounts, positions, orders, fills |
| `health.json` | tests, data-quality summary, provider capabilities |
| `mf.json`, `flows.json`, `macro.json`, `options.json` | mutual-fund NAVs (daily, with NAV date), FII/DII flows, macro releases, option chain (each with its own as-of date) |

## Errors and limits

- A file that doesn't exist yet returns HTTP 404. Consumers must treat missing data as "unavailable", never as zero.
- `raw.githubusercontent.com` caches responses for about 5 min unless a cache-busting query is added (`?t=<now>`). Poll at most once a minute; the portal does exactly that and pauses while the tab is hidden.
- Provider limits the engine respects:
  - Yahoo: batched downloads, one per cycle per group.
  - Angel One: at most 1 request per second, 50 symbols per request, and it stops for the cycle on HTTP 429.

## Streaming

There's no WebSocket; a static site can't hold one open. During market hours "streaming" means the `live-data` files are replaced every ~2 minutes and the portal re-reads them every 60 s without a page reload. Tick-level streaming needs an always-on machine (the optional VPS runner) with a broker WebSocket.
