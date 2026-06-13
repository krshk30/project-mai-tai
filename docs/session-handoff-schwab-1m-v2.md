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

- **2026-06-13 23:14 UTC — ✅ Track 1 DEPLOYED DORMANT (attended, weekend, after-close).** PR #296
  admin-squash-merged (main `d6baf67`); VPS pulled `4540645`→`d6baf67` (ff-only, the 4 prior commits
  were docs-only); **v2-only restart** at 23:14:29 (OMS/strategy/control untouched — verified their
  start timestamps unchanged). **Dormancy verified:** deployed `atr_flip_enabled=False` (code default,
  NO env override — running config is OFF); **zero "ATR Flip" intents, zero `[V2-ATR-PROBE]`** (ATR
  probe unset; only MACD probe is `*`); ATR code ran clean on warmup replay (CAST 1509 / VSME 4120
  bars) with **no errors/tracebacks post-restart**; Paths 1/2 unchanged (`[V2-MACD-PROBE]` + C2
  `[V2-PENDING-CROSS-SET]` computing normally — not ATR); streamer reconnected (`[V2-WS-LOGIN-OK]` +
  SUBS CAST,VSME 23:14:37-38); token mtime advancing 22:44→23:14 (control-service refresher, untouched);
  NRestarts=0; CYN protected-env intact. (A stale 2026-06-10 `[V2-LOOP-DEGRADED]`/heartbeat traceback
  surfaced in an undated grep — confirmed STALE by date-filtering; nothing post-restart.)
  - **⚠️ CARRY TO MONDAY PRE-FLIGHT — v2 holding 2 UNMANAGED paper positions (the no-exits gap):**
    `paper:schwab_1m_v2` **CAST 10 (filled Fri 06-12 23:25 UTC) + VSME 10 (filled Fri 20:53 UTC)** —
    Paths-1/2 entries with no managed exit (Track 2 / TOP open item) sitting open over the weekend.
    Deliberately NOT flattened (operator call): they're the live no-exits-gap evidence for the Track-2
    priority, and flattening is out-of-scope state mutation. **Monday: confirm NO double-open on
    CAST/VSME after the restart (re-entry gate held — `pos_qty=10` re-read confirmed at deploy), and
    decide flatten timing as part of Track-2 work.** Account-flat deploy-gate was waived for these
    (static sim holdings, restart provably doesn't touch them; gate's intent = no live in-flight
    orders, satisfied).
  - **Track 1 — ATR-Flip (P3-B) entry path: BUILT, PR #296 (MERGED `d6baf67`; deployed dormant — see
    above).** Third v2 entry path "ATR Flip" alongside Paths 1/2: variant B (intrabar
    touch of the resting ATR trail) + liquidity floor (vol>5000) as the ONLY filter; variant A
    default-off for live A/B. **Default flag OFF → ships dormant**, qty 10. Design (operator-
    approved) `docs/schwab-1m-v2-atr-flip-entry-design.md`. Key call: **incremental ATR state on
    `SymbolState`, reset at the 04:00-ET session anchor** (the 300-bar deque can't reach the anchor
    mid-session, but `on_bar` sees every warmup+live bar → matches the validated session-sliced
    backtest). Indicator = `analysis/atr_flip.py::compute_atr_trail` ported verbatim. ATR fields
    write-disjoint from Paths 1/2; precedence MACD>VWAP>ATR; dormant=warm (computed every bar,
    emits nothing until flag on). `reference_price` = the touched trail level. 5 tests pass incl.
    the LOAD-BEARING `test_atr_indicator_parity_vs_oracle` (incremental == frozen verbatim oracle,
    bar-for-bar). Settings: `…atr_flip_enabled/variant/quantity/vol_floor/period/factor/probe`.
  - **Track 2 — v2↔OMS exits Step A SCOPING: shipped, PR #297 (`codex/v2-exit-scoping`, HELD).**
    Read-only `docs/v2-exit-wiring-scoping.md` — maps the momentum-bot exit chain (order-events→
    PositionTracker→ExitEngine→close/scale-intent, verified file:lines) and the v2 gap inventory
    (no ExitEngine/PositionTracker/fill-binding/exit-config/eval-loop; not in the engine bot
    registry). Three candidate wiring approaches laid out NEUTRALLY (no recommendation). **Step B
    (choose the wiring) is the operator's next decision** — §6 open questions. Do NOT design/build
    the integration yet.
  - **Track 3 — tick capture activation: runbook CONFIRMED READY, no code, no flip.** All artifacts
    present (migration `20260611_0007_market_ticks.py`, `scripts/prune_market_ticks.py`,
    `scripts/replay_exit_from_ticks.py`, db models, flag). Activation runbook = `docs/v2-tick-
    capture-design.md` §125-134. Monday market-hours attended flip only.
  - **Deploy plan:** weekend after-close attended, all flags OFF (verify dormant). Monday (attended,
    paper): flip tick-capture → (v2 exits if Track 2 lands) → ATR-Flip; watch live triggers. NO
    credentials, paper throughout. **v2 still runs NO managed exits** (Track 2) — an ATR-Flip paper
    position has nothing to close it until Track 2 lands; that's the top dependency.

- **2026-05-26** — REST warmup window widened + data-flow watchdog shipped
  (PR #225, VPS `0650a99`, deployed 12:06 UTC). Fixes v2 going dark after the
  long weekend: the fixed `now-24h` warmup window returned empty Schwab
  pricehistory across the Sat+Sun+Memorial-Mon gap → no warmup → silent
  `bars_processed=0` under a misleading `healthy` heartbeat (the bar loop never
  died — it was data-starved). Now: warmup reaches the last session via a
  7-day lookback (`strategy_schwab_1m_v2_warmup_lookback_days`); the heartbeat
  carries `data_flow` / `market_session` / `secs_since_last_bar` /
  `secs_since_last_quote` / `quotes_live` / `rest_empty_streak_max` and goes
  `degraded` on a real RTH stall (quote-liveness gates RTH-vs-offhours so
  pre-market REST-dryness reads as expected, not a fault); US market holidays
  classify as `closed`. Post-deploy verify: warmed 3/3 in ~18s
  (1035/532/3565-bar feeds), `bars_processed` climbing, `data_flow=flowing`.
  First v2 unit tests added (`tests/unit/test_schwab_1m_v2_bot.py`, 14).
  Deployed MANUALLY (GitHub Actions down — see global handoff). Streamer is the
  separate after-close pre-market fix; Sat Day-1 test = NO-GO (flapping). Full
  per-change detail in `docs/session-handoff-global.md`.

- **2026-05-22 EOD** — Live with full MACD Momentum v1.32 entry strategy.
  Service active, watchlist 10-15 symbols, 569 rows persisted per hour at
  47 bars/symbol (compared to 35/sym/hr on schwab_1m WebSocket), persist-lag
  p50=85s p95=162s, zero errors in last hour. Strategy hasn't fired a live
  intent yet — small-cap watchlist is in a quiet mid-morning window where
  even schwab_1m only fired 1 P1_CROSS-equivalent signal all day. Tomorrow
  pre-market 07-09 ET is the real validation window. See per-deploy log
  below.

- **2026-05-22 morning** — Scaffolding PR opened. Bot service boots in idle
  state. REST poll client + bar/quote handlers wired but no strategy
  decision body yet (placeholder returns `None`). Bot URL `/bot/1m-schwab-v2`
  registered in dashboard. Enable flag default `false`.

## Files owned by this bot (the entire surface)

| Path | Purpose |
|---|---|
| `src/project_mai_tai/market_data/schwab_v2_rest_client.py` | Dedicated Schwab Price History + Quotes REST poller (cold-start warmup + reconnect gap-fill) |
| `src/project_mai_tai/market_data/schwab_v2_streamer.py` | Dedicated Schwab CHART_EQUITY WebSocket streamer (live 1m bars when streamer flag enabled) |
| `src/project_mai_tai/strategy_core/schwab_1m_v2.py` | Bar storage, inline indicators, strategy placeholder, intent emitter |
| `src/project_mai_tai/services/schwab_1m_v2_bot.py` | Sixth service entrypoint; subscribes to scanner state, drives REST + streamer |
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

### 2026-05-22 — Day 1: scaffolding → handoff/UI → strategy → live validation

Eight PRs shipped, all admin-merged. Listed in chronological order with
the operational lesson where applicable.

| # | PR | Title | What it shipped |
|---|---|---|---|
| 1 | #207 | scaffolding | 3 new source files (rest_client / strategy_module / engine_service) + systemd unit + dashboard route + deploy target + runtime_registry registration + dedicated session doc. Strategy body placeholder. |
| 2 | #208 | handoff + bar persistence + dashboard wiring | New `IsolatedBotStateEvent`, new Redis stream `mai_tai:strategy-state-isolated`, control_plane `_load_stream_state` merges both streams. v2 now publishes its own `StrategyBotStatePayload` every 5s without overwriting strategy-engine's snapshot. Bar persistence to `strategy_bar_history`. |
| 3 | #209 | Live Symbols panel + ET timezone | `_build_bot_views` had a fall-back to `bot_watchlist` only when `watched_by` was empty; isolated bots' codes never appear in `watched_by`, so v2's Live Symbols rendered empty. Added unconditional final pass appending bot_watchlist. Also added local `_format_eastern` helper so `last_tick_at` matches the existing strategy-engine's "YYYY-MM-DD HH:MM:SS AM/PM ET" format. |
| 4 | #210 | scanner cold-start seed | xread with `last_id="$"` only sees events after the call — after restart, v2 sat with empty watchlist until strategy-engine published its next snapshot (which fires only on bar/intent events, minutes apart in pre-market). Added xrevrange(count=1) seed on startup. |
| 5 | #211 | REST `startDate/endDate` | **Critical Schwab REST gotcha**: `?periodType=day&period=1` returns the **last fully-closed trading session**, NOT "today so far." During RTH it returns yesterday. Verified with direct API call: period=1 latest candle = 2026-05-21 23:59 UTC; explicit startDate/endDate = 2026-05-22 13:46 UTC. Fix: explicit `startDate=(now - 24h)` and `endDate=now`. |
| 6 | #212 | MACD Momentum v1.32 entry strategy | Replaced placeholder with full v1.32 design doc entry logic. Two paths: "MACD Cross" + "VWAP Breakout". Seven filter gates (trend/macdStrength/stochNotChase/greenBar/relVol/volAbs/timeAllowed) each with toggle-off semantics. Per-symbol state machine (entry side only — exits remain OMS's job). `_position_poll_loop` queries `virtual_positions` + in-flight `trade_intents` every 5s. Cooldown counter armed on True→False position transitions. Inputs hardcoded as `SchwabV2Config` dataclass (operator can edit the constants in-place to tune). |
| 7 | #213 | REST batch fetch for instant warmup | After deploy of #212, validation showed bot needed 35 bars per symbol to bootstrap MACD but only had ~22 after restart (~25 min cold-start gap). Schwab REST already returns 500+ candles per call; we were throwing away 499. Changed `_fetch_latest_bar` → `_fetch_recent_closed_bars(since_ts_ms)` returning all closed candles newer than the since-cursor. First poll per symbol returns the full 24h window. Added `MAX_BAR_AGE_SECONDS_FOR_EMIT=180` freshness guard so historical warmup bars update the indicator memo but cannot fire intents. |
| 8 | #214 | skip DB persist for warmup bars | Warmup feed of 500-1000 bars per symbol was sequentially awaiting DB persist, blocking the bar loop ~9s per symbol (>80s across 10 symbols). Added `PERSIST_BAR_AGE_LIMIT_SECONDS=300` so only bars within 5 min of wall clock get DB-written. Strategy still ingests every bar for indicators. |

### Operational fixes on the VPS the same day

- Cleared `MAI_TAI_LEGACY_API_BASE_URL` in env (commented out). The legacy
  `momentum-stock-trader` HTTP service isn't running on this VPS, so the
  control-plane was emitting `/scanner/confirmed`, `/bot`, `/bot1m`,
  `/tosbot`, `/runnerbot` → Connection-refused errors every refresh,
  driving overall status to `degraded`. Disabling the URL cleared the
  errors array to `[]` and removed those particular degraded sources.
- Killed a rogue `mai-tai-schwab-1m-v2` python process (PID 1536187) left
  over from a `--help` test. Our service entrypoint ignores `argparse` and
  unconditionally runs `asyncio.run(main())`, so the test became a permanent
  background instance, polluting the isolated stream with stale state and
  causing the dashboard to show empty watchlist intermittently. Lesson:
  if you ever need to test the entrypoint locally, set the enable flag to
  `false` first so the service idles instead of competing with the
  systemd-managed one.
- Reconciler is still `degraded` because `paper:schwab_1m` shows 8000 CYN
  at broker but virtual_position=0 — this is the operator-frozen position
  per PR #116 protected-symbols hard-block. Decision: leave as-is (operator
  confirmed 2026-05-22). The dashboard `status: degraded` overall is
  driven solely by this finding now.

### Validation snapshot 2026-05-22 16:02 UTC (12:02 ET)

| Metric | `schwab_1m` baseline | `schwab_1m_v2` |
|---|---|---|
| Bars persisted (last 60 min) | 284 | **569** |
| Symbols covered | 8 | 12 |
| Avg bars/symbol/hr | 35.5 | **47.4** |
| Latest bar age | 207s | 87s |
| Persist-lag p50 / p95 / max | n/a | 85s / 162s / 197s |
| Errors / warnings last hour | n/a | **0 new** (the 1 historical 429 was pre-fix) |
| Intents fired today (whole bot family) | 16 (all REJECTED at OMS) | 0 |

Existing schwab_1m fired only 1 P1_CROSS-equivalent signal all day; v2's
two entry paths require a MACD cross (which schwab_1m's various P1-P5
paths each detect slightly differently). On a quiet small-cap mid-morning,
zero v2 fires is consistent with broader market quietness, not a bug.

### What's still open (carry into tomorrow)

- **Live signal validation**: needs a real MACD cross. Best window:
  tomorrow pre-market 07-09 ET on the small-cap universe (per memory, that's
  when 55-65% of schwab_1m fills land).
- **OMS reject path**: every existing bot's intents today were rejected at
  OMS layer with no row in `risk_checks` (table was empty for the same
  window). When v2 finally fires, it'll likely hit the same reject reason
  — pre-existing OMS issue affecting all bots, not v2-specific. Operator
  noted earlier they're "not ready" for live trading; deferred.
- **Strategy spec slot still open**: SchwabV2Config in `schwab_1m_v2.py`
  hardcodes all v1.32 defaults. Operator can tune any constant in that
  single file (trade_size=read from env; everything else in the dataclass).
  No new env vars added beyond the original enable + quantity ones.
- **Decision tape sparseness**: v2's strategy_bar_history rows have empty
  `decision_status` (we only persist OHLCV, not decision metadata). The
  control_plane decision-tape filter requires `decision_status != ''` so
  the tape on `/bot/1m-schwab-v2` will be empty until either (a) we add
  decision-row persistence on every evaluation, or (b) we start emitting
  intents and OMS writes them. Not blocking — by-design for now.

### 2026-05-23 — Day 2 (PR #216): dedicated CHART_EQUITY WebSocket streamer (dormant)

**Why**: Day 1's REST-poll design has a structural persist-lag floor of
~85s p50 / 162s p95 vs TOS evaluating at T+0. Lag chain = (a) bar close,
(b) Schwab API aggregation delay 5-15s, (c) round-robin queue (15s/symbol
× 12 symbols = up to 180s between polls of the same symbol),
(d) the 60s `_fetch_latest_bar` finality wait. Net: signals fire ~90s
after TOS would have. To match TOS we need a push feed, not a pull feed.

**What shipped** (default-OFF, code lands dormant):

- `src/project_mai_tai/market_data/schwab_v2_streamer.py` (new) —
  dedicated WS client for `CHART_EQUITY` only. Reads `/trader/v1/userPreference`
  for streamer creds, opens `wss://…`, sends ADMIN LOGIN, then SUBS/ADD/
  UNSUBS on watchlist changes. Bar extract from fields 0/2/3/4/5/6/7.
  Exponential-backoff reconnect (1s → 30s). Per-symbol `last_bar_ts_ms`
  dedupe inside the streamer so Schwab same-bucket re-emits don't
  double-feed. NO imports from `market_data/schwab_streamer.py`.
- `services/schwab_1m_v2_bot.py` — boots `SchwabV2Streamer` alongside
  `SchwabV2RestClient`. Both call the same `_handle_bar`; idempotency
  is handled by the strategy's same-timestamp update semantics +
  `_persist_bar` UPSERT. Heartbeat details now include `streamer_enabled`
  and `streamer_connected`.
- `settings.py` — three new env vars (defaults shown):
  - `MAI_TAI_STRATEGY_SCHWAB_1M_V2_STREAMER_ENABLED=false`
  - `MAI_TAI_STRATEGY_SCHWAB_1M_V2_STREAMER_RECONNECT_BASE_SECS=1.0`
  - `MAI_TAI_STRATEGY_SCHWAB_1M_V2_STREAMER_RECONNECT_MAX_SECS=30.0`

**REST stays as-is** — keeps running concurrently for (a) cold-start
warmup of 35-bar MACD window, (b) reconnect gap-fill. Both feeds are
idempotent, so we don't need a tighter coupling between them. Slight
duplicate bandwidth (REST keeps polling at 15s/symbol even when streamer
is healthy) — well under the 120 RPM Schwab limit; optimize later if it
matters.

**Why CHART_EQUITY only**: bar-close based v1.32 strategy doesn't need
LEVELONE quotes or TIMESALE trades for entry signals. Smaller protocol
surface = lower risk. If intrabar entry refinement ever becomes needed,
add a second service (LEVELONE) — don't expand this one.

**OAuth single-session collision risk** — the streamer reuses the same
OAuth token that `schwab_streamer.py` (strategy-engine process) already
holds a WS session on. Schwab typically allows one concurrent streamer
session per OAuth user. If v2's connect kicks the existing session off,
production schwab_1m / macd_30s bot WS feeds go dark.

Mitigations:
- **Ship dormant**. The streamer enable flag defaults to `false` so this
  PR has zero runtime impact when merged + deployed.
- **Evening test only**. First connect attempt MUST be after 16:00 ET
  close. On the existing schwab_1m log, watch for new
  `Schwab streamer connection loop failed` warnings starting within
  seconds of `[V2-WS-LOGIN-OK]` — that's the collision signature.
- **One-line rollback**: set
  `MAI_TAI_STRATEGY_SCHWAB_1M_V2_STREAMER_ENABLED=false` in
  `/etc/project-mai-tai/project-mai-tai.env` and
  `systemctl restart project-mai-tai-schwab-1m-v2.service`. REST-only
  path is identical to today, no regression to other bots.

**Activation runbook** (when ready to test):

1. After 16:00 ET, on the VPS:
   ```bash
   sudo sed -i 's/^# MAI_TAI_STRATEGY_SCHWAB_1M_V2_STREAMER_ENABLED.*/MAI_TAI_STRATEGY_SCHWAB_1M_V2_STREAMER_ENABLED=true/' \
     /etc/project-mai-tai/project-mai-tai.env
   # (or just append the line if the comment isn't there)
   sudo systemctl restart project-mai-tai-schwab-1m-v2.service
   ```
2. Tail both logs in parallel:
   ```bash
   sudo tail -F /var/log/project-mai-tai/schwab-1m-v2.log
   sudo tail -F /var/log/project-mai-tai/strategy.log | grep -i schwab
   ```
3. Within ~15s expect:
   - `[V2-WS-LOGIN-OK] schwab_v2 streamer connected (symbols_desired=N)`
   - `[V2-WS-SUB] cmd=SUBS count=N sample=…`
   - Existing strategy.log shows NO new "Schwab streamer connection loop
     failed" entries beyond pre-test baseline.
4. Within 90s expect at least one CHART_EQUITY bar arrival (any
   `_handle_bar` log line for a watchlist symbol), then watch persist-lag
   in DB:
   ```sql
   SELECT bar_time, EXTRACT(EPOCH FROM (created_at - bar_time)) AS lag
   FROM strategy_bar_history
   WHERE strategy_code='schwab_1m_v2' AND created_at > NOW() - INTERVAL '5 min'
   ORDER BY created_at DESC LIMIT 20;
   ```
   Target: lag drops to <5s on bars fed by the streamer (vs ~85s p50
   under REST-only).

**If collision is confirmed**: flip flag off + restart v2. Plan Day 3 as
"second Schwab developer-app credential" — a separate OAuth identity for
v2's streamer with its own token store path. v2's REST stays on the
shared token (read-only file access doesn't conflict).

### 2026-05-23 — Day 2 (PR #217 + PR #218): MACD probe + W1 warmup-settling bars

**PR #217 — Diagnostic-only `[V2-MACD-PROBE]` per-bar log.**
Gated by `MAI_TAI_STRATEGY_SCHWAB_1M_V2_MACD_PROBE_SYMBOLS` (CSV or `*`;
empty = off). Dumps every input to cross detection per evaluated bar.
Operator validated against TOS for CPSH 2026-05-22 15:46–16:00 ET:
bot's `macd` / `sig` match TOS to 4 decimals at steady state (`n_bars=300`),
including the 16:00 ET closing bar at 6-decimal precision (0.009498 /
0.004169). Closes match exactly.

**PR #218 — W1 fix: raise `min_bars` from 35 to 135.**
Same probe revealed the EMA seed-bias zone empirically: bot MACD at
`n_bars=35` is 0.085 (CPSH on 2026-05-22 warmup batch), decaying through
`n_bars=60` (0.022) → `n_bars=80` (-0.032) → `n_bars=100` (-0.01,
steady-state range). The unreliable zone is `n_bars=35–100`. By
`n_bars≈135` the bias decays below TOS display precision.

Fix: new field `SchwabV2Config.macd_warmup_settling_bars=100`, added to
the bootstrap `min_bars` formula. `_evaluate_completed_bar` now requires
`n_bars ≥ 135` (= `macd_slow + macd_signal + 100`) before computing
indicators or touching the `prev_*` memo. The first ~100 warmup bars
still feed into the deque but don't trigger evaluation, so the
cross-detection memo handed off to live evaluation reflects converged
EMAs rather than seed-biased noise.

Trade-off (intentional): the strategy now needs ~135 minutes of bar
history before any cross can fire. The REST cold-start warmup batch
(~500 bars per symbol per PR #213) covers this comfortably. The deque
`maxlen=300` is unchanged — leaves 165 bars of post-warmup convergence
headroom.

Code review status: this is the W1 finding from the code review doc.
C1 (the stateful-EMA rewrite) is **deferred** with this fix in place —
W1 walls off the entire unreliable region from any code path that
matters, so the rewrite is no longer load-bearing. The review doc has
been updated with a "Validation status" header at the top reflecting
C1-deferred / W1-confirmed-keep.

### 2026-05-23 — Day 2 (PR #219): C3 + W2 + W3 streamer/REST seam bundle

**Why**: the code review (`schwab_1m_v2_code_review.md`) flagged three
issues at the REST/streamer seam that the PR #216 "both feeds are
idempotent" claim doesn't actually cover. Idempotency holds for bar
**storage** (UPSERT) and bar **state** (strategy's same-bucket update)
but NOT for cross **detection** or intent **emission** — those are
first-delivery side effects, so which feed wins the cross is a race
when both are live. Three coupled fixes below; they ship together
because the gating (C3) depends on the warmup signal (W2) and the
streamer dedupe behavior (W3).

**C3 — single signal source when streamer connected**
- `services/schwab_1m_v2_bot.py` — REST and streamer now use distinct
  callbacks (`_handle_bar_from_rest` / `_handle_bar_from_streamer`).
- `_should_skip_rest_strategy_feed`: when `streamer.connected` is True
  AND streamer has delivered a bar with `ts_ms >= bar.timestamp_ms` for
  this symbol, REST suppresses the strategy feed entirely. REST still
  runs its poll loop and advances its internal cursor — only the
  forward to `_handle_bar` is gated.
- Two heartbeat counters added: `rest_bars_gated_total` (suppressed —
  streamer already had it) and `rest_bars_gap_fill_total` (forwarded
  while streamer was connected — genuine gap-fills).

**W2 — explicit "REST warmup before streamer subscribes" ordering**
- New per-symbol set `_rest_warmup_done`. A symbol is added the first
  time REST forwards a bar within `REST_WARMUP_FRESH_THRESHOLD_SECS=300`
  of wall clock (i.e. the warmup batch has caught up to live).
- `_apply_strategy_state_event` passes the watchlist set to REST
  immediately but only the warmed subset to the streamer.
- When REST marks a new symbol warmed, the streamer's desired set is
  extended via `_extend_streamer_subscriptions_to_warmed`.
- When a symbol leaves the watchlist, its warmup status is cleared.
  If it re-joins, REST has to re-warm it before streamer is told.
- `[V2-REST-WARMED]` INFO log per symbol on warmup completion, with
  warmed/watchlist counts.

**W3 — streamer dedupe drops equal-timestamp re-emits**
- `market_data/schwab_v2_streamer.py` — changed
  `bar.timestamp_ms < prev` to `<= prev`. CHART_EQUITY's contract is
  that each emit is a final snapshot for the closed minute; same-bucket
  re-emits would touch `state.bars[-1]` under the strategy's
  update-in-place path without re-running cross detection, which is
  noise. With `<=`, the streamer emits each bucket exactly once.

**Operational impact while streamer flag is OFF (current state)**:
- `streamer.connected` is False permanently → `_should_skip_rest_strategy_feed`
  always returns False → REST runs identically to today.
- `_rest_warmup_done` still tracks warmup completion but is only used
  to gate streamer subscriptions — streamer.run() is idle.
- W3 dedupe change has no effect (streamer isn't connected, doesn't
  process messages).
- Heartbeat counters publish but stay at zero.
- **Zero behavior change today. Activation requires the evening test
  per PR #216's runbook.**

**Operational impact when streamer flag is ON (future evening test)**:
- On bot startup, streamer.run() connects but `set_desired_symbols`
  starts with empty intersection (no symbols warmed yet). Streamer
  connects, logs in, no SUBS sent.
- REST batch fetches per-symbol histories. As each batch's tail crosses
  the 300s freshness threshold, the symbol joins `_rest_warmup_done`
  and streamer SUBS that symbol.
- From that point: streamer pushes 1m bars at minute close (T+0-1s);
  REST keeps polling but is gated out of the strategy feed for the
  same buckets. Persist-lag p50 should drop from ~85s to <2s for any
  symbol where streamer is delivering.
- On streamer disconnect: REST resumes feeding strategy (no gating
  while disconnected). Reconnect triggers re-SUBS for the warmed set;
  no extra gap-fill code needed because REST has been continuously
  filling during the disconnect window.

**What's still NOT covered by this bundle**:
- C2 (age-guard-consumes-cross at the warmup→live seam). Will be the
  next PR, against the settled seam this bundle creates.

### 2026-05-23 — Day 2 (PR #220): C2 pending-cross carryforward

**Why**: with W1, C3, W2, W3 in place, the REST/streamer seam is stable
and the indicator memo updates only on n_bars≥135 bars. But there's
still a subtle bug at the warmup→live boundary: when a NATIVE cross is
detected on a stale bar (which is then suppressed by the
`MAX_BAR_AGE_SECONDS_FOR_EMIT=180s` freshness guard), the memo update
still happens, which means the next bar's cross detection compares
against post-cross state. **A real cross at the warmup tail is silently
eaten** — the bot has no record that it ever happened.

**Fix**: decouple memo update from emit eligibility.

- New `SymbolState` fields: `pending_path_macd`, `pending_path_vwap`,
  `pending_cross_bar_ts_ms`.
- New config: `SchwabV2Config.pending_cross_max_gap_secs=180` (operator-
  tunable; default matches the existing freshness window).
- In `_evaluate_completed_bar`:
  1. Compute native cross detection (unchanged).
  2. If the bar is STALE AND a native cross is detected, stash it as
     `pending_*` (with `cross_bar_ts_ms = cur.timestamp_ms`).
  3. Update the memo (unconditional — indicator continuity).
  4. If the bar is STALE, return None (existing behavior preserved).
  5. If the bar is FRESH: check pending. If `(cur.timestamp_ms -
     pending_cross_bar_ts_ms) > pending_cross_max_gap_secs`, expire +
     discard. Otherwise validate that the cross condition still holds
     on the CURRENT bar (`macd_above_signal=True` + filters pass) and,
     if so, promote pending into the effective `path_macd` /
     `path_vwap` for the state-machine gates.
  6. Always clear `pending_*` after a fresh-bar evaluation, whether
     consumed, expired, or invalidated by reversed momentum. The
     cross's window of opportunity is one fresh bar.

**Why the on-fresh-bar validation**: a pending cross-up was detected
when the memo at that time said macd≤signal and the cross-bar said
macd>signal. If the next fresh bar shows `macd<signal` (reversal
during the gap), firing on the stale cross would buy into reversing
momentum. The `macd_above_signal` check on the consuming bar prevents
this — the cross-up is consumed only if the cross is still "alive."

**Expiry policy** (180s default): chosen to match
`MAX_BAR_AGE_SECONDS_FOR_EMIT`. Operator can tune
`MAI_TAI_STRATEGY_*` env… actually no, this is a `SchwabV2Config`
dataclass field — edit in the strategy file to change.
- Cross at T, fresh bar at T+60 → gap 60s → consume.
- Cross at T, fresh bar at T+120 → gap 120s → consume.
- Cross at T, fresh bar at T+180 → gap 180s exactly → consume.
- Cross at T, fresh bar at T+240 → gap 240s > 180 → expire.
- AKTX-sized 11-min gap (T+660) → expire. Correct — a cross from 11
  minutes ago is not a tradeable signal anymore.

**Observability**: three new INFO log markers.

- `[V2-PENDING-CROSS-SET]` — once per pending stash (de-duped: only logs
  on the FIRST stale bar with a cross; subsequent stale bars don't
  spam because they don't have new native crosses, only the same path
  staying active).
- `[V2-PENDING-CROSS-CONSUMED]` — when a fresh bar promotes the
  pending into an effective path.
- `[V2-PENDING-CROSS-EXPIRED]` — when the gap exceeds the cap.

**What this does NOT cover**: the AKTX/GITS/WHLR-style "no candle on
no-trade minute" gaps are **missing bars**, not stale bars. The bar
that arrives after such a gap is fresh, so the C2 bug doesn't directly
trigger there. C2 fires on the warmup→live seam (every bot restart)
and on any genuine "stale bar contains a cross" path (delayed REST
delivery, network hiccup). The warmup batch on each restart IS the
canonical reproduction — exercised once per process start.

**Verification post-deploy**: grep the three markers in the post-restart
log. Expect:
- `[V2-PENDING-CROSS-SET]` lines: appear during warmup-batch processing
  when stale bars trigger native crosses (likely several per symbol).
- `[V2-PENDING-CROSS-EXPIRED]` lines: appear when the warmup batch
  has a long stretch of "pending held, no fresh bar" then a fresh bar
  arrives more than 180s after the last stale cross.
- `[V2-PENDING-CROSS-CONSUMED]` lines: appear when the warmup tail
  resolves into the live feed and the cross is still valid.

Counts will vary by session — the existence of all three states under
real data is the validation, not specific counts.

### 2026-05-22 EOD — Post-#220 regression spot-check (clean)

Quick "did the merges break anything" sweep run at 20:52 UTC, ten
minutes after the PR #220 restart. Four random watchlist symbols
sampled for bar-build cadence + persist-lag, all three review markers
checked, heartbeat W2 progression confirmed, error grep clean.

| Symbol | bars/10min | latest_age_s | lag p50 |
|---|---|---|---|
| BIYA | 8 | 138s | 90.5s |
| CPSH | 2 | 318s | 84.7s |
| GOVX | 8 | 138s | 91.8s |
| RYOJ | 9 | 78s | 82.3s |

Persist-lag p50 in the 82–92s band across all four — matches the
pre-PR baseline of ~85s. CPSH's sparser 2-bars/10min is its known
no-trade-gap behavior on a thin penny stock, not a regression.

C2 marker counts since the 20:42 restart:
- `[V2-PENDING-CROSS-SET]`: 19
- `[V2-PENDING-CROSS-EXPIRED]`: 10
- `[V2-PENDING-CROSS-CONSUMED]`: 0

The +1 EXPIRED over the immediate post-restart count is a live-data
hit — confirms C2 actively expiring stale pending crosses outside the
warmup batch too.

W2 verified: heartbeat shows `warmed_size=10, watchlist_size=10`
stable from 20:49 onward. Got from 0/10 at boot to 10/10 in ~7 min
(matches REST round-robin × warmup batch time).

Zero errors / tracebacks / exceptions since the #220 restart.

### 2026-05-23 — Streamer Activation Test Plan (Saturday plumbing → weekday-after-close live → separate credential)

This is the day-by-day plan for activating the CHART_EQUITY streamer
(PR #216, dormant) against the settled REST/streamer seam
(PRs #218–#220).

Read **Core concepts** first — it prevents the most common confusions.

#### Core concepts (read first)

**1. Architecture: streamer is primary, REST is demoted (not removed).**

- Once activated, the CHART_EQUITY streamer is the PRIMARY live feed.
  It pushes each closed 1-minute bar at minute close (~T+0–1s).
- REST stays running but is demoted to TWO jobs only:
  - **warmup** — the ~500-bar cold-start batch that seeds indicators.
  - **gap-fill / fallback** — feeds the strategy only when the
    streamer is disconnected.
- When the streamer is connected and healthy, C3 gating (PR #219)
  suppresses REST from the strategy feed. REST still polls; it just
  stops forwarding bars to the strategy.

**2. The streamer runs ALL sessions once it is on.**

- Once the flag is on and the streamer is connected, it runs
  continuously: pre-market, regular hours, post-market.
- You activate it ONCE. You do not flip it on/off around market hours.
- A clean activation is left ON and carries into every following
  session on its own.

**3. Why activation happens AFTER MARKET CLOSE — the key point.**

- The v2 streamer opens a WebSocket session on the SHARED Schwab OAuth
  token — the same token the production `schwab_1m` / `macd_30s` bots
  use.
- Schwab allows ONE streamer session per OAuth token.
- The first time the flag is flipped, the v2 streamer's connect MAY
  kick the production session offline (the "collision").
- Therefore the first flag-flip is done AFTER market close, when
  production bots are not trading and a collision costs nothing.
- "After 6 PM" is NOT the streamer's operating schedule. It is the
  one-time safe window to flip the flag and observe whether it
  collides. After a clean activation, the streamer runs 24/7 normally.

**4. The real fix that removes all of this timing fuss.**

- Day 3 below: give v2's streamer its OWN Schwab developer-app
  credential (separate OAuth identity, separate token store path).
- With a separate token there is NO shared session and NO collision
  possible. The streamer can then be activated any time.
- Until Day 3 is done, the after-close activation dance is the
  interim safe path.

---

#### DAY 1 — Saturday (market closed all day): PLUMBING TEST

**Goal**: confirm the streamer connects, logs in, subscribes, and does
not crash or collide. This validates the MECHANICS only.

**What this test CAN prove**: connection works, OAuth works, the code
path runs without crashing, the flag flips cleanly, rollback works.

**What this test CANNOT prove**: that bars actually flow, or that
persist-lag drops. The market is closed Saturday, so CHART_EQUITY has
no new bars to push. Expect FEW or ZERO `_handle_bar` lines. That is
NOT a failure — it is expected with the market closed.

**A. Record the starting state (so revert is exact).**

1. SSH to the VPS.
2. Check the flag's current state:
   ```bash
   sudo grep -E '^#?\s*MAI_TAI_STRATEGY_SCHWAB_1M_V2_STREAMER_ENABLED' \
     /etc/project-mai-tai/project-mai-tai.env
   ```
3. Write down its EXACT current form (commented out OR `=false`).
   This is what you revert to.

**B. Activate.**

4. Set the flag to true:
   ```bash
   sudo sed -i 's/^#\?\s*MAI_TAI_STRATEGY_SCHWAB_1M_V2_STREAMER_ENABLED=.*/MAI_TAI_STRATEGY_SCHWAB_1M_V2_STREAMER_ENABLED=true/' \
     /etc/project-mai-tai/project-mai-tai.env
   ```
   (or edit the line directly to `=true`)
5. Restart ONLY the v2 service:
   ```bash
   sudo systemctl restart project-mai-tai-schwab-1m-v2.service
   ```

**C. Watch — two logs in parallel.**

6. v2 log:
   ```bash
   sudo tail -F /var/log/project-mai-tai/schwab-1m-v2.log
   ```
7. Production log (collision check):
   ```bash
   sudo tail -F /var/log/project-mai-tai/strategy.log | grep -i schwab
   ```

**D. Expected within ~15–30 seconds.**

- `[V2-WS-INIT] schwab_v2 streamer enabled, REST polling continues
  for cold-start warmup + reconnect gap-fill` → flag flip picked up.
- `[V2-WS-LOGIN-OK] schwab_v2 streamer connected (symbols_desired=N)`
  → connection + OAuth OK. `N` may be 0 at first because W2 makes
  streamer wait for REST warmup.
- `[V2-WS-SUB] cmd=SUBS count=N sample=...` → subscription sent. May
  lag the LOGIN by a minute or two on cold start because the streamer
  only subscribes to REST-warmed symbols. Normal.
- Production log: NO new "Schwab streamer connection loop failed"
  lines right after `[V2-WS-LOGIN-OK]`. If those appear → that is
  the collision.
- `_handle_bar` lines: probably few or none (market closed).
  Expected. Not a failure.

**E. Revert — DO NOT leave the flag on after Day 1.**

8. Set the flag back to its recorded starting state. Two cases:
   ```bash
   # Case A — original was commented out:
   sudo sed -i 's/^MAI_TAI_STRATEGY_SCHWAB_1M_V2_STREAMER_ENABLED=true/# MAI_TAI_STRATEGY_SCHWAB_1M_V2_STREAMER_ENABLED=false/' \
     /etc/project-mai-tai/project-mai-tai.env

   # Case B — original was explicit =false:
   sudo sed -i 's/^MAI_TAI_STRATEGY_SCHWAB_1M_V2_STREAMER_ENABLED=true/MAI_TAI_STRATEGY_SCHWAB_1M_V2_STREAMER_ENABLED=false/' \
     /etc/project-mai-tai/project-mai-tai.env
   ```
9. Restart v2 again to apply the revert:
   ```bash
   sudo systemctl restart project-mai-tai-schwab-1m-v2.service
   ```
10. Confirm:
    ```bash
    sudo systemctl status project-mai-tai-schwab-1m-v2.service | head -5
    sudo tail -n 30 /var/log/project-mai-tai/schwab-1m-v2.log | grep -E 'idle|enabled'
    ```
    Expected: service active + a log line like
    `schwab_v2_streamer idle: enabled=False token_path='...'`.

**Day 1 outcomes**:

- **Clean** (login OK, no collision, no crash) → proceed to Day 2.
- **Collision** (production log shows connection-loop failures) →
  revert immediately. Do NOT proceed to Day 2. Prioritize Day 3
  (separate credential) — the streamer cannot be safely activated on
  the shared token.
- **Crash / errors in v2 log** → revert, capture the error, fix
  before retrying.

---

#### DAY 2 — A WEEKDAY (Mon–Fri), AFTER MARKET CLOSE (after ~16:00 ET / ~20:00 UTC): LIVE ACTIVATION

**Goal**: activate the streamer for real and confirm it delivers the
lag fix. This is the test Day 1 cannot do, because it needs live bars.

**This is NOT a weekend test.** Saturday/Sunday have no post-close
extended-hours activity, so CHART_EQUITY won't push anything fresh
even after a "close" that never happened. Day 2 MUST be a weekday
trading day, run after the close.

**Pre-req**: Day 1 came back clean.

**Why after close, again**: the flag-flip is the collision-risk
moment. Doing it after close means production bots are not trading,
so a collision (if any) is consequence-free. Once activated clean,
the streamer is LEFT ON and runs into the next pre-market and RTH on
its own.

**Steps**: same A–D as Day 1 (record state, flip flag, restart, watch
both logs).

**Difference from Day 1 — what to expect, because the market traded
today**:

- `_handle_bar` lines for watchlist symbols within ~90s of activation
  (recent post-close / extended-hours activity is enough).
- Persist-lag in the DB should drop. Check:
  ```sql
  SELECT bar_time,
         EXTRACT(EPOCH FROM (created_at - bar_time)) AS lag
  FROM strategy_bar_history
  WHERE strategy_code='schwab_1m_v2'
    AND created_at > NOW() - INTERVAL '5 min'
  ORDER BY created_at DESC LIMIT 20;
  ```
  Target: lag < 5s on streamer-fed bars (vs ~85s p50 under REST-only).
- Heartbeat counter `rest_bars_gated_total` should start climbing —
  that's C3 working: REST stepping aside while the streamer feeds.
- **NOTE**: `rest_bars_gap_fill_total` will show a non-zero baseline
  that is NOT real gap-fills — it counts benign warmup subscription
  lag. Do not misread a climbing gap-fill counter as the streamer
  dropping bars.

**Decision point**:

- **Clean + lag dropping + no collision** → LEAVE THE FLAG ON.
  **Do not revert.** The streamer is now the live feed and carries
  forward into all following sessions. Monitor the next pre-market.
- **Collision or instability** → revert (Day 1 steps 8–10), flag
  off, fall back to REST-only (identical to pre-PR-#216 behavior, no
  regression). Prioritize Day 3.

---

#### DAY 3 — Separate OAuth credential (the proper fix)

**Goal**: remove the collision risk entirely so the streamer no
longer depends on after-close activation windows.

**What it involves**:

- Register a separate Schwab developer-app credential — a distinct
  OAuth identity for v2's streamer, with its own token store path
  (e.g. `MAI_TAI_SCHWAB_V2_TOKEN_STORE_PATH=/var/lib/macd-webhook-server/data/schwab_v2_tokens.json`).
- v2's REST client stays on the SHARED token (read-only file access
  doesn't conflict; the shared token is unrelated to streamer session
  uniqueness).
- v2's streamer uses the new dedicated token. No shared session, no
  collision possible.
- Implementation note: v2 streamer reads its token via
  `settings.schwab_token_store_path` today. The Day 3 change adds a
  v2-streamer-specific token store path and wires
  `SchwabV2Streamer._read_access_token` to read from it instead.

**After Day 3**:

- The streamer can be activated any time — no after-close window
  needed.
- The Day 1 / Day 2 timing constraints no longer apply to future
  restarts or re-activations.

**Sequencing note**: if Day 1 shows a collision, Day 3 becomes the
immediate priority and Day 2 is skipped until Day 3 is done.

---

#### Open items to watch during/after activation

1. **C2 CONSUMED path** — `[V2-PENDING-CROSS-CONSUMED]` has fired 0
   times so far. The code is review-verified but its runtime path
   isn't yet exercised on real data. Grep for this marker over the
   next several restarts until it appears at least once.
2. **Silent WS stall** — if the streamer's socket goes silent without
   formally disconnecting, `streamer.connected` stays True, C3 keeps
   gating REST out, and there's a ~20–40s gap until the ping-timeout
   fires. A staleness watchdog would close this; not built yet.
3. **`rest_bars_gap_fill_total` baseline** — misnamed; counts benign
   warmup subscription lag as "gap-fill." Expect a non-zero baseline
   even when the streamer is delivering normally. Not a bug.

---

#### Quick reference — the one rule that prevents confusion

- The streamer RUNS during market hours. That is its job.
- The streamer is ACTIVATED (first flag-flip) after market close —
  ONCE — because the flag-flip is the collision-risk moment.
- A clean activation is LEFT ON and runs every session afterward.
- Activate on / observe / leave on (if clean) — or revert (if not).
- The after-close timing exists ONLY because of the shared OAuth
  token. Day 3 removes it.

---

### 2026-05-23 — Day 1 plumbing test executed; Day 2 NO-GO pending subscribe/evaluate decoupling

#### Day 1 result

Executed against VPS HEAD `fadb467` (== `origin/main`). Pre-test env did
NOT contain `MAI_TAI_STRATEGY_SCHWAB_1M_V2_STREAMER_ENABLED` at all
(neither commented nor `=false`); appended `=true`, restarted v2,
reverted by deleting the line. Final env SHA256 matches pre-test
byte-for-byte; backup retained at
`/etc/project-mai-tai/project-mai-tai.env.bak-day1-2026-05-23`.

Observed pattern over ~2 min, ~14 cycles each ~3-4 s apart:

```
[V2-WS-LOGIN-OK] schwab_v2 streamer connected (symbols_desired=0)
[V2-WS-DISCONNECT] schwab_v2 streamer failure #1: received 1000 (OK); then sent 1000 (OK)
```

No `[V2-WS-SUB]` ever fired. No production-side `Schwab streamer
connection loop failed` lines — collision criterion did NOT trigger.

#### Streamer code-read (`market_data/schwab_v2_streamer.py`, `fadb467`)

1. **Empty-sub send is gated.** `_apply_subscription_delta` (lines
   292-301) computes `to_add = desired - requested`. With both empty,
   it returns without calling `_send_subscription`. Belt-and-suspenders
   guard at `_send_subscription` line 306: `if not symbols or
   self._creds is None: return`. Nothing leaves the wire when
   `_desired_symbols` is empty.
2. **No heartbeat defect.** No app-level keepalive code anywhere in
   the file. Protocol-level pings are configured at connect: line 152
   `ping_interval=20, ping_timeout=20`. Disconnect at 1.3-2.5 s after
   LOGIN-OK is far too fast to be heartbeat-related (would be ~40 s).
3. **Server-initiated close, confirmed.** Log signature `received 1000
   (OK); then sent 1000 (OK)` is the `websockets` library's
   `ConnectionClosed` format for a remote-initiated close. Code path:
   `_receive_loop` line 227-228 re-raises `ConnectionClosed`; `run()`
   line 172-178 catches and logs `[V2-WS-DISCONNECT]`; `finally` line
   184-187 acks via `ws.close()`. No streamer code path closes
   pre-emptively.
4. **Streamer-level verdict: BENIGN.** Schwab closes idle,
   subscription-less sessions. The streamer reconnects with
   exponential backoff capped at
   `strategy_schwab_1m_v2_streamer_reconnect_max_secs`. The streamer
   itself ships correct.

#### Bot service code-read (`services/schwab_1m_v2_bot.py`, `fadb467`) — gating analysis

1. **When `streamer.set_desired_symbols(...)` is called.** Two
   callsites only, both gated on `_rest_warmup_done`:
   - Line 484 (`_apply_strategy_state_event`, on scanner-state arrival):
     `self.streamer.set_desired_symbols(selected & self._rest_warmup_done)`.
     On cold start `_rest_warmup_done` is `set()`, so even with a
     non-empty watchlist the streamer receives `set()`.
   - Line 587 (`_extend_streamer_subscriptions_to_warmed`):
     `warmed = self._watchlist & self._rest_warmup_done; self.streamer.set_desired_symbols(warmed)`.
     Called from `_handle_bar_from_rest` line 536 each time REST
     delivers a bar with `bar_age_secs <= 300 s` for a not-yet-warmed
     symbol.
2. **Subscription IS gated on warmup.** `REST_WARMUP_FRESH_THRESHOLD_SECS
   = 300.0` (line 79). A symbol is added to `_rest_warmup_done` only
   when REST delivers a bar within 300 s of wall clock (line 531-535).
   This is the W2 design comment at line 121-126: *"streamer doesn't
   subscribe to a symbol until REST has fed its history, so streamer
   can't drop live bars onto an empty deque ahead of the historical
   context."* The W1 min_bars gate inside the strategy (`SchwabV2Strategy`)
   already prevents premature evaluation; W2 is over-cautious and
   conflates subscription with evaluation.
3. **Subscribe/evaluate decoupling is NOT in place.** The current
   wiring is "subscribe late, evaluate late." The correct split per
   intent is "subscribe early (immediately on scanner-state), evaluate
   late (min_bars guard in strategy)." Line 484 is the offending
   intersection; removing the `& self._rest_warmup_done` mask is the
   minimal fix.
4. **Weekday cold-start implication.** On a weekday, REST round-robin
   (5 s per symbol, `bar_poll_interval_seconds=5`) will mark the first
   symbol warmed within ~5-10 s. Per validation snapshot 2026-05-22,
   full warmup of 10 symbols took ~7 min. So a weekday cold start sees
   the same LOGIN-OK → DISCONNECT loop for the first ~5-15 s until at
   least one symbol crosses the 300 s freshness threshold and the
   streamer's `_sync_event` fires SUBS on the next reconnect attempt.
   Once any symbol is subscribed Schwab holds the session and `SUBS`
   incrementally expands as more symbols warm. The Saturday loop is
   the same pathology stretched indefinitely because REST never
   produces a bar within 300 s of wall clock.

#### Day 2 verdict: **NO-GO**

Subscribe/evaluate decoupling is not in place at line 484. Day 2 on a
weekday after close would reproduce the same reconnect loop for the
first ~5-15 s of activation. That window is short, but:

- It generates `[V2-WS-DISCONNECT]` noise that's indistinguishable
  from a real collision when scanning logs post-test, undermining the
  Day 2 success criterion (clean activation → leave flag on).
- The reconnect-backoff schedule (base 0.5 s, doubling, capped) means
  successive failures during the warmup window stretch the time to
  first SUBS, not shorten it.
- Reconnects mid-session (e.g. transient network blip) clear
  `_requested_symbols` (streamer line 167) and re-derive desired from
  the bot's current `set_desired_symbols` value; if a mid-session
  reconnect lands during a watchlist transition where every symbol's
  warmup state was just cleared (line 476:
  `self._rest_warmup_done &= selected`), the same loop recurs
  mid-day. Low probability but real.

#### Required fix before Day 2 (do not implement yet — review needed)

Minimal change in `services/schwab_1m_v2_bot.py`:

```diff
-        if self.streamer is not None:
-            # Streamer only subscribes to symbols REST has confirmed
-            # warmed. Newly-added symbols will be added to the streamer
-            # subscription set incrementally as REST batches complete
-            # (see `_handle_bar_from_rest`).
-            self.streamer.set_desired_symbols(selected & self._rest_warmup_done)
+        if self.streamer is not None:
+            # Streamer subscribes to the full watchlist as soon as
+            # scanner-state arrives. REST warmup still drives indicator
+            # bootstrap and the W1 min_bars gate inside SchwabV2Strategy
+            # prevents premature evaluation. The earlier W2 design
+            # ("don't subscribe until warmed") conflated subscription
+            # with evaluation and produced an idle-session reconnect
+            # loop on cold start.
+            self.streamer.set_desired_symbols(selected)
```

Same simplification applies to `_extend_streamer_subscriptions_to_warmed`
(line 580-587), which becomes redundant — its only caller is
`_handle_bar_from_rest` line 536, which can drop the call. The
`_rest_warmup_done` set itself is still useful elsewhere (REST
freshness reporting, gap-fill counter heuristics) so keep it; just
stop using it as a subscription gate.

Validation before Day 2 retry:
- Unit test: cold-start `_apply_strategy_state_event` with a non-empty
  watchlist and empty `_rest_warmup_done` → assert
  `streamer.set_desired_symbols` called with the full watchlist.
- Unit test: pre-existing W1 min_bars gate still rejects evaluation
  when streamer bars arrive before REST warmup.
- Live: a Saturday re-run of Day 1 with the fix should show a single
  LOGIN-OK followed by SUBS (with N matching the most recent
  scanner-state snapshot), then a stable held session. Saturday won't
  produce bars, but the no-disconnect outcome alone proves the
  decoupling works.

#### Day 1 retry — explicit pass condition

After PR #224 lands on the VPS, re-run Day 1 to confirm the loop is
gone. Pass condition:

- `[V2-WS-LOGIN-OK]` followed by `[V2-WS-SUB]` within ~1 second
- session held — no `[V2-WS-DISCONNECT]` cycles in the post-restart
  window
- production `strategy.log` shows no `Schwab streamer connection loop
  failed` activity in the same window

Mandatory pre-flight: confirm `XLEN mai_tai:strategy-state` is
non-zero AND the most recent snapshot contains a non-empty `watchlist`
/ `all_confirmed`. An empty scanner-state at boot reproduces the
reconnect loop legitimately (no symbols → no SUBS → idle session
closed by Schwab); that is a **false alarm**, not a regression.
Verify scanner-state before declaring failure.

If scanner-state is empty: re-run after the next strategy-engine
snapshot publishes (state-publish loop fires every 5 s by default
while the engine is healthy), or run during pre-market when the
scanner is actively producing snapshots.

#### Tuesday 2026-05-26 — Day 2 live activation plan

After market close (≥ 16:00 ET / 20:00 UTC), with PR #224 live:

- Activate per the existing Day 2 runbook (env flag flip + restart).
- Watch criteria:
  - persist-lag p50 drops from ~85 s (REST-only baseline) toward
    < 5 s (streamer push at minute close)
  - `_handle_bar` lines flow within ~90 s of activation for
    post-close extended-hours active symbols
  - production `strategy.log` shows no collision markers
- Clean activation → leave flag ON; do not revert.
- Note in the doc whether `[V2-PENDING-CROSS-CONSUMED]` fires for the
  first time during the Tuesday window (open item below).

#### Carry-forward open items (after this work stream)

These persist past Day 2 and aren't addressed by PRs #223 / #224:

1. **C2 CONSUMED path** — `[V2-PENDING-CROSS-CONSUMED]` has fired 0
   times across all restarts so far. The code is review-verified but
   has not been exercised on live data yet. Grep for this marker
   after Tuesday Day 2 and on each subsequent restart until at least
   one real fire is observed.
2. **OAuth separate-credential (Day 3)** — the real fix that removes
   the shared-token collision constraint entirely. Until Day 3 is
   done, every streamer activation carries a non-zero collision risk
   on the shared OAuth session. Implementation sketch in the
   Streamer Activation Test Plan section above; not in this PR set.
3. **Silent WS stall watchdog** — if the streamer's socket goes
   silent without formally disconnecting, `streamer.connected` stays
   True and C3 keeps gating REST out until the ping-timeout fires
   (~20–40 s). A staleness watchdog would close this gap; not built
   yet.
4. **`rest_bars_gap_fill_total` baseline noise** — counter is
   misnamed and currently counts benign cold-start REST bars as
   "gap-fill" because the streamer's first bar lags REST by one
   minute. Expect a non-zero baseline even when the streamer is
   delivering normally. Cosmetic; not a bug.

#### Rolling forward

- PRs in flight: **#223 (this doc)** + **#224 (subscribe-early code
  fix + buffer-and-replay + safety drop, 8 new tests)**.
- Merge order: **#223 first** (doc only, zero code), **#224 second**
  (code; depends on the doc's findings being landed).
- Day 1 retry on Saturday or any market-closed day **after** #224
  ships to the VPS.
- Day 2 live activation on Tuesday 2026-05-26 after close.
- Day 1 mechanics (OAuth, websocket, code path, flag flip + revert
  byte-identity) proved out on 2026-05-23 — no Day 3 escalation
  needed yet, but Day 3 remains the durable fix per the carry-forward
  list above.

#### 2026-05-26 update — status correction (supersedes "Rolling forward" above)

- **Both PRs shipped.** #223 (this doc) merged via the 2026-05-26 batched handoff
  PR; **#224 merged `9aa4cbb` + deployed to VPS 13:05 UTC with the streamer flag
  OFF** (rebased onto #225; 23 unioned tests pass; real-CI-gated — its own tests
  green, the 22 CI failures are the pre-existing baseline in untouched files).
- **Cause framing confirmed:** this doc's Day-1 analysis correctly attributed the
  flap to the **empty-subscription idle-close**, NOT an OAuth collision. The "OAuth
  collision signature" phrasing in some interim handoff/memory notes was the
  misattribution. #224 implements the subscribe-early fix that resolves it.
- **Collision risk remains OPEN, not closed.** Saturday was market-closed (couldn't
  exercise a real collision), and on 2026-05-26 the *production* CHART_EQUITY
  streamer was found flapping ~every 20s during RTH (see the top item in
  `session-handoff-global.md`) — which both confounds any v2 collision check and is
  a higher-priority production issue.
- **Day-1 retry and Day-2 activation are DEFERRED** (not merely rescheduled) until
  the production streamer is stable. The v2 streamer flag stays OFF. Day-3
  (separate Schwab dev-app credential) remains the durable fix once we get there.
