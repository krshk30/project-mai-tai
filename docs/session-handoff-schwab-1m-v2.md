# Session Handoff — schwab_1m_v2 (isolated bot)

## Why this doc exists

`schwab_1m_v2` is a deliberately-isolated parallel 1-minute bot built to
escape the regression chain that's been hitting `schwab_1m` and `macd_30s`.
Nothing in this doc references `schwab_streamer.py`, `schwab_native_30s.py`,
or `strategy_engine_app.py`. Issues that require touching those files
belong in `docs/session-handoff-global.md`, not here.

If a fix to this bot starts pulling in those existing files, stop and
reconsider — that's how regressions cross-contaminate.

## Status

- **2026-05-22** — Scaffolding PR opened. Bot service boots in idle state.
  REST poll client + bar/quote handlers wired but no strategy decision body
  yet (placeholder returns `None` for every bar). Bot URL `/bot/1m-schwab-v2`
  registered in dashboard. Enable flag `MAI_TAI_STRATEGY_SCHWAB_1M_V2_ENABLED`
  defaults to `false` — set to `true` on the VPS env file when you're ready
  to start polling.

## Files owned by this bot (the entire surface)

| Path | Purpose |
|---|---|
| `src/project_mai_tai/market_data/schwab_v2_rest_client.py` | Dedicated Schwab Price History + Quotes REST poller |
| `src/project_mai_tai/strategy_core/schwab_1m_v2.py` | Bar storage, inline indicators, strategy placeholder, intent emitter |
| `src/project_mai_tai/services/schwab_1m_v2_bot.py` | Sixth service entrypoint; subscribes to scanner state, drives REST client |
| `ops/systemd/project-mai-tai-schwab-1m-v2.service` | systemd unit |
| `docs/session-handoff-schwab-1m-v2.md` | this doc |

**Strictly off-limits when patching this bot** (touching them defeats the
isolation purpose):
`market_data/schwab_streamer.py`, `strategy_core/schwab_native_30s.py`,
`strategy_core/bar_builder.py`, `strategy_core/indicators.py`,
`strategy_core/entry.py`, `strategy_core/exit.py`,
`strategy_core/polygon_30s.py`, `services/strategy_engine_app.py`,
`services/strategy_engine.py`.

## Architecture summary

```
Schwab REST API (Price History + Quotes)
        ↑ HTTP GET (Bearer <existing schwab_token>)
        |
SchwabV2RestClient
  ├─ bar loop: round-robin watchlist, one symbol per bar_poll_interval_seconds
  └─ quote loop: batched, all watchlist, every quote_poll_interval_seconds
        ↓ on_chart_bar / on_quote callbacks
        |
SchwabV2BotService
  ├─ subscribes mai_tai:strategy-state (scanner snapshot)
  ├─ feeds confirmed symbols → rest_client.set_desired_symbols
  ├─ heartbeats every service_heartbeat_interval_seconds
  └─ on bar/quote → SchwabV2Strategy.on_bar/on_quote → maybe emit
        ↓ via SchwabV2IntentEmitter
        |
mai_tai:strategy-intents Redis stream
        ↓ (existing path)
OMS (paper:schwab_1m_v2 account) → DB persist → broker flow
```

## What's reused from the existing platform (intentionally)

- Schwab OAuth access token (read from `settings.schwab_token_store_path`).
  We do NOT refresh it ourselves — existing services handle the refresh
  cycle; we piggyback on whatever they write.
- Postgres database, `strategies` and `trade_intents` tables.
- Redis Streams (`mai_tai:strategy-state` for input, `mai_tai:strategy-intents`
  for output, `mai_tai:heartbeats` for health).
- OMS service (`oms-risk`) consumes intents via the existing stream.
- Control-plane dashboard auto-renders `/bot/1m-schwab-v2` once the DB row
  exists (created by `mai-tai-seed-runtime`).

## What's NEW (the isolation guarantee)

- Dedicated bar storage, indicator math, strategy decision, intent emission.
- Dedicated REST client (no shared `SchwabBrokerAdapter`).
- Dedicated service process / systemd unit / log file at
  `/var/log/project-mai-tai/schwab-1m-v2.log`.
- Dedicated DB row (`schwab_1m_v2`) and broker account (`paper:schwab_1m_v2`)
  created by the runtime seed.

## How to enable

1. On VPS env (`/etc/project-mai-tai/project-mai-tai.env`), add:
   ```
   MAI_TAI_STRATEGY_SCHWAB_1M_V2_ENABLED=true
   ```
2. Optional tuning (defaults in `settings.py`):
   ```
   MAI_TAI_STRATEGY_SCHWAB_1M_V2_BAR_POLL_INTERVAL_SECONDS=15
   MAI_TAI_STRATEGY_SCHWAB_1M_V2_QUOTE_POLL_INTERVAL_SECONDS=5
   MAI_TAI_STRATEGY_SCHWAB_1M_V2_MAX_WATCHLIST_SIZE=25
   MAI_TAI_STRATEGY_SCHWAB_1M_V2_DEFAULT_QUANTITY=100
   ```
3. Run the seed (creates the `strategies` row and broker_account row):
   ```
   /home/trader/project-mai-tai/.venv/bin/mai-tai-seed-runtime
   ```
4. Install the new systemd unit (if not already installed):
   ```
   sudo bash /home/trader/project-mai-tai/ops/systemd/install_units.sh
   sudo systemctl enable project-mai-tai-schwab-1m-v2.service
   sudo systemctl start project-mai-tai-schwab-1m-v2.service
   ```
5. Verify:
   - `sudo systemctl status project-mai-tai-schwab-1m-v2.service` → active
   - `sudo tail -f /var/log/project-mai-tai/schwab-1m-v2.log`
   - Dashboard nav shows "Schwab 1m v2"
   - `/bot/1m-schwab-v2` renders (will be empty until the strategy body is
     implemented and the bot starts emitting intents)

## Bar-build invariants (local to this bot)

Restated here so this bot's invariants can evolve independently from the
existing `schwab_native_30s.py` ones.

1. **REST candles are treated as final once their bucket is older than 60s**
   (`_fetch_latest_bar` filter). Avoids treating an in-flight candle as a
   completed bar.
2. **Idempotent bar dispatch**: `_last_bar_timestamp_ms` is checked per
   symbol before invoking `on_chart_bar`. Re-polling the same minute does
   not double-feed the strategy.
3. **Quote stream is informational only**: quotes update `state.last_quote`
   and feed `evaluate_intrabar`. They do NOT mutate `state.bars`.
4. **No tick-level bar building**. This bot does not consume LEVELONE
   trade ticks. If the strategy needs sub-minute granularity, the design
   choice is: poll quotes more aggressively, or upgrade to WebSocket later.

## Strategy spec slot (pending operator)

`SchwabV2Strategy._evaluate_completed_bar` and `_evaluate_intrabar` both
return `None` today. When the spec arrives:
- Implement the entry rule in `_evaluate_completed_bar` using
  `state.bars` (deque of `OHLCVBar`, newest at right) and `V2Indicators`.
  Return a `TradeIntentDraft` to emit; return `None` to skip.
- Implement intrabar exit/scale logic in `_evaluate_intrabar`.
- Add per-symbol risk filters in `SchwabV2Strategy.watchlist_state` or
  inside the evaluate methods. Don't add a new shared file.

## Open follow-ups

- **Trade tick stream**: not built. If the strategy needs LEVELONE ticks,
  add a tick poll OR a dedicated WebSocket — but inside this bot's own
  module tree, not by importing `schwab_streamer.py`.
- **Symbol filtering**: bot currently takes the union of `all_confirmed +
  top_confirmed + watchlist` from `mai_tai:strategy-state` (capped at
  `max_watchlist_size`). Bot-specific filters (price band, ADV, float)
  belong inside `SchwabV2Strategy`, not in the engine.
- **Tick archive**: not currently written. The existing bot writes ticks to
  `/var/lib/project-mai-tai/schwab_ticks/`; if v2 needs an archive,
  introduce `/var/lib/project-mai-tai/schwab_v2_ticks/` and write inside
  `SchwabV2RestClient` (don't share `schwab_tick_archive.py`).
- **OMS persistence of intents**: relies on OMS picking up intents off the
  Redis stream and inserting `trade_intents` rows. If OMS misses one, v2
  has no fallback persist. Add inside `SchwabV2IntentEmitter` if needed.

## Per-deploy log

### 2026-05-22 — Scaffolding PR

- **PR**: TBD (link will be added at merge time)
- **What shipped**: 3 new source files + 1 systemd unit + 1 dashboard route
  + 1 deploy target + settings flags + runtime registration. Strategy body
  is a placeholder.
- **VPS deploy**: pending operator decision. Service is safe to deploy in
  idle state (`MAI_TAI_STRATEGY_SCHWAB_1M_V2_ENABLED=false`), but no value
  without strategy body. Recommend deploying only after the strategy spec
  has been added.
