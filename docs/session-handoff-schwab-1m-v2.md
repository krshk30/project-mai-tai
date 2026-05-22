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
