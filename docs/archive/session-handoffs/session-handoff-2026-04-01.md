# Session Handoff - 2026-04-01

This file captures the April 1, 2026 premarket stabilization session for
`project-mai-tai`.

It should be read after [session-handoff-2026-03-31.md](C:/Users/kkvkr/OneDrive/Documents/GitHub/project-mai-tai/docs/session-handoff-2026-03-31.md)
because most platform recovery and March 31 parity context still lives there.

This is the single current handoff for all April 1 work. March 31 remains
historical background only; do not split additional April 1 notes into a
second same-day file.

## Main Outcome

The biggest April 1 finding was that the scanner restart path still had two
real gaps:

- stale prior-session `Momentum Confirmed` names could reappear
- momentum alert warmup could restore as "ready" while visible alert history was
  mostly empty after restart

Both were investigated, fixed in code, tested locally, and deployed live.

Late in the session, the TradingView parity deep dive corrected one earlier
assumption: TradingView CSV `Session VWAP` was matching a regular-session
`9:30 AM ET` anchor, not the temporary `4:00 AM ET` premarket anchor we had
switched to during debugging. The final live strategy config was therefore
reverted back to `9:30 AM ET`.

## Fixes Completed Today

### 1. Stale Prior-Session Confirmed Names Were Removed

Root cause:

- control-plane fallback could restore `scanner_confirmed_last_nonempty`
  without ensuring it belonged to the current scanner session
- strategy runtime did not clear confirmed scanner state when the scanner
  session rolled at `4:00 AM ET`

Fixes:

- `src/project_mai_tai/services/control_plane.py`
  - restored confirmed rows are now only shown if `persisted_at` is in the
    current scanner session
- `src/project_mai_tai/services/strategy_engine_app.py`
  - scanner state now rolls cleanly at the `4:00 AM ET` session boundary

Live result:

- stale yesterday names stopped appearing in `Momentum Confirmed`
- live scanner now shows `top_confirmed_source = idle` when there are no
  current-session confirmed names

### 2. Legacy Shadow Divergence Was Comparing The Wrong Things

Root cause:

- legacy confirmed names were being compared against the new system watchlist
  instead of the new confirmed list

Fix:

- `src/project_mai_tai/services/control_plane.py`
  - legacy `confirmed_symbols` are now compared against new `top_confirmed`

Impact:

- this did not cause bad trades
- it did create false drift noise during live comparison and debugging

### 3. Restart Warmup Restored Hidden Alert State But Not Visible Alert Tape

What was happening:

- momentum alert engine history, cooldowns, and spike ticker memory restored
- control plane still showed only the alert(s) fired after restart because
  `recent_alerts` was not restored

Fix:

- `src/project_mai_tai/services/strategy_engine_app.py`
  - persist and restore current-session `recent_alerts`
  - persist and restore `top_gainer_changes`
  - persist and restore `_first_seen_by_ticker`

Important note:

- this alone was not enough because retained Redis snapshot history was still
  too short to rebuild a meaningful warmup window after restart

### 4. Snapshot-Batch Retention Was Too Small For Scanner Warmup Design

This was the bigger design bug behind the alert-history issue.

Observed mismatch:

- snapshot cadence = `5s`
- `squeeze_10min_ready` needs `120` cycles
- config still retained only `12` snapshot batches by default

That meant the strategy service could not reconstruct 5 to 10 minutes of
scanner state after restart even when replay logic was correct.

Fixes:

- `src/project_mai_tai/settings.py`
  - `redis_snapshot_batch_stream_maxlen` raised from `12` to `180`
- `src/project_mai_tai/market_data/publisher.py`
  - default `snapshot_batch_stream_maxlen` raised from `4` to `180`
- `src/project_mai_tai/services/strategy_engine_app.py`
  - startup warmup now attempts to rebuild visible alert tape from retained
    snapshot batches when persisted alert tape is missing

Live verification:

- Redis `mai_tai:snapshot-batches` length started growing beyond the old small
  cap after market-data restart
- scanner began showing fresh alert tape again, including:
  - `ABVE` `VOLUME_SPIKE`
  - `BCG` `SQUEEZE_5MIN`

### 5. Same-Session Restart Now Restores Visible Confirmed Watchlists Immediately

Another restart gap was identified after the alert-history work:

- same-session confirmed candidates were seeded on startup
- but visible `top_confirmed` rows and bot watchlists stayed blank until the
  first fresh snapshot batch revalidated them

Fix:

- `src/project_mai_tai/services/strategy_engine_app.py`
  - seeded same-session `top_confirmed` rows now immediately repopulate
    `current_confirmed`
  - bot watchlists are restored immediately from those visible confirmed names
  - runner candidates are also restored immediately
  - this lets market-data subscriptions re-arm before the first fresh snapshot
    batch

Practical result:

- a same-session strategy-service restart no longer has to wait for the first
  fresh scanner batch to repopulate watchlists for already-confirmed symbols

### 6. Strategy Runtime Now Restores Open Positions And Pending Orders From Database

Another same-session restart gap remained after the scanner fixes:

- confirmed names and watchlists could come back
- but bot-local runtime state for open positions and pending orders could still
  be empty after a strategy-only restart

Why this was happening:

- strategy reads Redis order-events with stream offsets starting at `$`
- old order-events are not replayed on startup
- runtime position and pending-order memory therefore needed an explicit
  durable restore path

Fixes:

- `src/project_mai_tai/services/strategy_engine_app.py`
  - startup now restores runtime state from durable database tables
  - open `virtual_positions` are mapped back into bot runtimes as active
    positions
  - open `broker_orders` plus linked `trade_intents` restore pending opens,
    pending closes, and pending scales
- `src/project_mai_tai/strategy_core/runner.py`
  - runner runtime now supports explicit restore of position and pending state

Practical result:

- a same-session strategy-service restart should now recover:
  - already-confirmed watchlists
  - bot-visible open positions
  - pending open state
  - pending close state
  - pending scale state

This closes the main restart-hardening hole that could have affected active
trading after a strategy-only restart.

### 7. 30s MACD Entry Layer Now Adds Precondition And Anti-Chase Filters

The next change focused specifically on the `macd_30s` entry path because the
bot was still taking too many raw MACD crosses and too many of those trades
were failing.

What was added:

- a pre-trigger quality filter before P1/P2/P3 path selection
- a confirmation-time anti-chase filter before the final buy signal is emitted

Scope:

- applies only to `macd_30s`
- does not change `macd_1m`, `tos`, or `runner`
- does not change existing hard gates, path logic, score logic, or buy payloads

Implementation details:

- `src/project_mai_tai/strategy_core/trading_config.py`
  - added config fields for entry preconditions and anti-chase thresholds
  - added `make_30s_variant()` so the feature is enabled only for the 30s bot
- `src/project_mai_tai/strategy_core/entry.py`
  - remembers recent per-symbol bars inside the entry layer
  - blocks new setups when the prior remembered bar is too extended from VWAP
    or EMA9
  - blocks new setups when recent volume ratio is too weak or too overheated
  - blocks final confirmation when price is too far from VWAP

Thresholds now in code:

- precondition lookback = `3` bars
- max prior-bar VWAP distance = `0.5%`
- max prior-bar EMA9 distance = `0.5%`
- prior-bar volume ratio window = `0.50x` to `1.20x`
- max anti-chase VWAP distance on confirmation = `1.0%`

### 8. 30s Runtime Now Uses Canonical Live Aggregates Plus Audit Overlay

The next 30s parity step addressed the remaining architecture gap that had not
been implemented yet.

What changed:

- `src/project_mai_tai/settings.py`
  - added a dedicated `macd_30s` switch for live aggregate bars
  - added a dedicated `macd_30s` Massive audit overlay switch
- `src/project_mai_tai/market_data/gateway.py`
  - live aggregate publishing now turns on when the 30s aggregate mode is
    enabled, even if the old global aggregate toggle stays off
- `src/project_mai_tai/services/strategy_engine_app.py`
  - `macd_30s` now uses live aggregate bars as its canonical live bar source
  - `macd_30s` now records Massive audit overlay data without replacing the
    local 30s trading inputs
  - provider OHLCV/VWAP diffs are now persisted into strategy bar history
- `src/project_mai_tai/market_data/massive_indicator_provider.py`
  - added a 30s aggregate audit fetch path using Massive aggregate bars

Important behavior:

- `macd_30s` now favors provider aggregate bars over raw trade-built live bars
- the new 30s overlay is audit-only
- unlike `macd_1m`, the 30s overlay does not overwrite local trading inputs yet
- this keeps the first parity pass observational while giving us stored
  provider-vs-local diffs for `open/high/low/close/volume/vwap`

Rollback note:

- this is still a single strategy-service code path change
- rollback is straightforward by reverting the 30s variant wiring and entry
  filter code, then restarting only `project-mai-tai-strategy.service`
- because the thresholds now live in `TradingConfig`, the feature can also be
  disabled cleanly in code/config rather than depending on display-name checks

### 8. Strategy VWAP Anchor Was Wrong For Premarket And Was Corrected

Live validation on `CYCN` exposed a more important issue than threshold tuning:

- the 30s and 1m strategy bots were both showing indicator `vwap` near `1.48`
- the live scanner and market snapshots showed `CYCN` VWAP near the actual
  premarket trading range around `3.6` to `3.7`

Root cause:

- `src/project_mai_tai/strategy_core/config.py`
  - indicator VWAP was still anchored to regular-session open at `9:30 AM ET`
- live scanner and market snapshots were effectively using the current trading
  session starting at `4:00 AM ET`

Impact:

- VWAP-based precondition checks for the 30s bot were falsely blocking strong
  premarket names with nonsense distances like `140%+ from VWAP`
- the same stale VWAP baseline also affected the 1m and TOS indicator views

Fix:

- `src/project_mai_tai/strategy_core/config.py`
  - changed indicator VWAP session anchor from `9:30 AM ET` to `4:00 AM ET`
- `tests/unit/test_strategy_core.py`
  - updated VWAP anchor test to validate reset at `4:00 AM ET`

Practical result:

- strategy-layer VWAP should now align far more closely with the live scanner
  and snapshot VWAP during premarket
- VWAP-based entry filters can now be evaluated on real signal quality instead
  of on a stale regular-session baseline
- local verification passed with:
  - `tests/unit/test_strategy_core.py`
  - `tests/unit/test_decision_layer.py`
  - `tests/unit/test_strategy_engine_service.py`
  - combined result: `47 passed`
- live verification after strategy restart:
  - `CYCN` strategy VWAP moved from the bogus `~1.485` area up to `~3.10`
    on both `macd_30s` and `macd_1m`

Follow-up correction after deeper TradingView parity review:

- the provider's 30s OHLC bars were already very close to TradingView for
  `CYCN` and `RENX`
- the big remaining VWAP mismatch was dominated by session anchoring
- recomputing VWAP from the same provider bars with a `9:30 AM ET` anchor
  matched the TradingView CSV `Session VWAP` very closely
- recomputing from `4:00 AM ET` stayed far away from TradingView because it
  included premarket volume that the TradingView export was not using

Final fix:

- `src/project_mai_tai/strategy_core/config.py`
  - reverted indicator VWAP session anchor back to `9:30 AM ET`
- `tests/unit/test_strategy_core.py`
  - updated the VWAP reset regression to validate the `9:30 AM ET` anchor

Final interpretation:

- the April 1 premarket stale-VWAP bug was real
- but the correct long-term answer for TradingView parity is regular-session
  VWAP at `9:30 AM ET`
- if the system later needs a dedicated premarket VWAP, it should be added as
  a second VWAP series rather than replacing the TradingView-parity anchor

### 9. Dual VWAP Series Added To Resolve TradingView Parity vs Premarket Trading

The next step implemented that dual-series design.

What changed:

- `src/project_mai_tai/strategy_core/indicators.py`
  - now calculates both:
    - `vwap` = regular-session VWAP anchored at `9:30 AM ET`
    - `extended_vwap` = extended-session VWAP anchored at `4:00 AM ET`
- `src/project_mai_tai/strategy_core/trading_config.py`
  - added `entry_vwap_mode`
  - `macd_30s`, `macd_1m`, and `tos` now use `session_aware` mode
- `src/project_mai_tai/strategy_core/entry.py`
  - entry logic now selects VWAP dynamically:
    - before `9:30 AM ET`, bot logic uses `extended_vwap`
    - at and after `9:30 AM ET`, bot logic uses regular-session `vwap`
  - P2 VWAP breakout logic, VWAP preconditions, VWAP score contribution, and
    anti-chase checks all now use the selected trading VWAP instead of the
    display/parity VWAP unconditionally

Why this is the right compromise:

- TradingView parity still needs `vwap` anchored at `9:30 AM ET`
- premarket trading logic still needs a `4:00 AM ET` baseline
- the single-VWAP design could not satisfy both at once

Replay verification:

- session-aware replay on `CYCN`, `RENX`, and `BCG` from `7:00 AM ET` to
  `10:00 AM ET` restored the same premarket trade set as the old
  premarket-anchored run:
  - `7` trades total
  - `4` winners
  - `3` losers
  - net replay P&L `+17.77`
- while indicator parity against TradingView stayed close because the
  externally visible `vwap` field remained regular-session anchored

### 9. Confirmed Universe Was Split Back Out From Bot Top Feed

The next scanner issue was a design mismatch versus legacy:

- `Momentum Confirmed` is supposed to preserve the full confirmed universe for
  the current session
- `Top 5 by score -> Bots` is supposed to be a smaller score-qualified subset
- `5 Pillars` and `Top Gainers` stay independent and should not interfere with
  momentum-confirmed retention or promotion

What was wrong before the fix:

- Mai Tai was blending the full confirmed list and the bot feed too tightly
- low-score confirmed names could disappear entirely instead of remaining
  visible for the day
- when the score gate was relaxed to keep names visible, bots could end up
  listening to names that should have stayed below the `Top 5` threshold

Fixes:

- `src/project_mai_tai/strategy_core/momentum_confirmed.py`
  - added `get_ranked_confirmed(...)` so the scanner can rank the full
    confirmed universe separately from the score-qualified top feed
  - same-session retained names are no longer dropped just because live
    `change_pct` falls below the old static threshold
- `src/project_mai_tai/services/strategy_engine_app.py`
  - `all_confirmed` now keeps the full ranked confirmed universe
  - `top_confirmed` / watchlist now apply the score gate separately for bot
    routing
  - startup restore now recalculates `all_confirmed` and `top_confirmed`
    cleanly instead of reusing a stale saved visible subset
- `src/project_mai_tai/services/control_plane.py`
  - dashboard/API `Momentum Confirmed` view now uses `all_confirmed`
  - legacy shadow divergence now compares legacy confirmed symbols against the
    new full confirmed universe, not only the bot subset
- `src/project_mai_tai/events.py`
  - strategy-state snapshots now publish `all_confirmed`

Live result after deploy:

- `all_confirmed_count` can now exceed `top_confirmed_count`
- low-score names remain visible in the confirmed scanner for the current
  session
- only score-qualified names feed the bot watchlist
- live result after the fix showed:
  - `all_confirmed = CYCN, RENX, BCG, BIAF`
  - `top_confirmed/watchlist = CYCN, RENX`

### 10. Current Score Mismatch Versus Legacy Is Still Real

One more important finding came out of the scanner split work:

- Mai Tai is not currently reusing a known legacy scoring formula
- the legacy shadow connector only imports legacy confirmed symbols and bot
  state
- it does not import or expose legacy score math

Current Mai Tai score behavior:

- implemented in `src/project_mai_tai/strategy_core/momentum_confirmed.py`
- score is relative to the *current confirmed peer set*, not an absolute
  session-wide scale
- weighted components are:
  - volume `20%`
  - low float `20%`
  - RVOL `20%`
  - change% `20%`
  - tight spread `10%`
  - volume/float ratio `10%`

Practical implication:

- Mai Tai score numbers can differ materially from legacy even when the stock
  list is the same
- this is why names like `RENX` or `BCG` can show very different scores
  compared with the legacy dashboard screenshot

Status:

- scanner structure/parity is now much closer to legacy
- score *formula* parity is still unresolved and needs a dedicated follow-up
  if exact legacy score numbers matter

### 11. Scanner Dashboard Now Shows Full Confirmed Universe Again

Another control-plane-only bug was found after the scanner split fix:

- `/api/scanner` correctly exposed `all_confirmed_count > top_confirmed_count`
- but the HTML scanner dashboard was still rendering `top_confirmed` in the
  main `Momentum Confirmed` table

Impact:

- operators could only see the bot-fed subset on the page
- this made it look like low-score confirmed names had disappeared even though
  they were still preserved internally

Fix:

- `src/project_mai_tai/services/control_plane.py`
  - scanner dashboard table now renders `all_confirmed`
  - sidebar confirmed count now reflects `all_confirmed_count`
  - the table subtitle now explicitly says the page shows the full confirmed
    session universe while `TOP5` / bot badges mark the active ranked subset

Live result:

- scanner page now shows `CYCN`, `RENX`, `BCG`, and `BIAF`
- bot watchlists remain correctly limited to `CYCN` and `RENX`

### 12. Scanner Live Badge Now Falls Back To Fresh Feed Activity

The scanner page also had a display flapping problem:

- market-data heartbeat was healthy and reported active symbols
- fresh snapshot batches were still arriving
- but the scanner page could still show no live subscriptions when the
  `market-data-subscriptions` stream detail lagged or was empty

Fix:

- `src/project_mai_tai/services/control_plane.py`
  - scanner page now falls back to market-data heartbeat `active_symbols`
    when the subscription-detail stream is missing or stale
  - live status also treats a very recent snapshot batch as evidence of an
    active live feed for display purposes

Practical result:

- the scanner page no longer drops to a misleading "not live" state just
  because the subscription-detail stream lags
- note: this is a control-plane display workaround; the raw
  `market-data-subscriptions` stream/source mismatch is still worth a follow-up
  - scanner VWAP at the same time was `~3.56`
  - that remaining gap is normal source/aggregation drift, not the old broken
    premarket anchor

### 9. Confirmed Scanner Was Relaxed Toward Legacy Behavior

The next scanner changes were made to move the new system closer to the legacy
confirmed-candidate model:

- same-cycle `SQUEEZE_5MIN` + `SQUEEZE_10MIN` bursts after a `VOLUME_SPIKE`
  can now qualify for Path B confirmation even if they arrive at the same
  timestamp and price
- confirmed names are no longer pruned out of the session universe just because
  they temporarily fade below the live change threshold
- restored current-session `recent_alerts` are now replayed back into the
  confirmed scanner after a strategy restart so names such as `RENX` can be
  reconstructed from the saved alert tape
- single-candidate rank scoring now returns `100.0` instead of `0.0`

Additional note:

- squeeze confirmation now uses the larger of alert volume and current live
  snapshot volume when evaluating volume/float filters during confirmation,
  which helps names like `RENX` whose live volume grows rapidly after the first
  squeeze burst

Files changed:

- `src/project_mai_tai/strategy_core/momentum_confirmed.py`
- `src/project_mai_tai/services/strategy_engine_app.py`
- `tests/unit/test_strategy_core.py`
- `tests/unit/test_strategy_engine_service.py`

Validation:

- `tests/unit/test_strategy_core.py` -> `11 passed`
- `tests/unit/test_strategy_engine_service.py` -> `32 passed`

Live result:

- the structural confirmed-scanner changes are live
- after restart, `RENX` alert history is now present in the restored strategy
  snapshot and replay path
- the remaining limiter for `RENX`/`BCG` becoming active watchlist names is the
  top-ranked visible selection layer, not the old confirmation-path rigidity

## Current Live State After Today’s Fixes

Important live observations from the April 1 premarket window:

- strategy service healthy
- OMS healthy
- reconciler healthy
- no pending intents
- no open `virtual_positions`
- no open `account_positions`
- no active reconciliation findings

Scanner state after the retention fix:

- fresh `BCG` movement visible in `five_pillars` and `top_gainers`
- fresh alert tape visible again
- `squeeze_5min_ready = true`
- `squeeze_10min_ready = false` immediately after restarts until enough new
  retained batches accumulate

One temporary caveat:

- control plane may briefly report `market-data-gateway = stopping` after a
  market-data restart because the cached heartbeat lags behind actual resumed
  traffic

## Tests Run Today

Local targeted test results after today’s changes:

- `tests/unit/test_control_plane.py` -> `8 passed`
- `tests/unit/test_strategy_engine_service.py` -> `31 passed`
- `tests/unit/test_market_data_gateway.py` -> `3 passed`
- `tests/unit/test_decision_layer.py` -> `9 passed`
- `tests/unit/test_strategy_core.py` -> `7 passed`
- `tests/unit/test_strategy_core.py` + `tests/unit/test_decision_layer.py` + `tests/unit/test_strategy_engine_service.py` -> `47 passed`

## Files Changed Today

- `src/project_mai_tai/services/control_plane.py`
- `src/project_mai_tai/services/strategy_engine_app.py`
- `src/project_mai_tai/settings.py`
- `src/project_mai_tai/market_data/publisher.py`
- `src/project_mai_tai/strategy_core/runner.py`
- `src/project_mai_tai/strategy_core/trading_config.py`
- `src/project_mai_tai/strategy_core/entry.py`
- `src/project_mai_tai/strategy_core/config.py`
- `tests/unit/test_control_plane.py`
- `tests/unit/test_decision_layer.py`
- `tests/unit/test_strategy_core.py`
- `tests/unit/test_strategy_engine_service.py`
- `tests/unit/test_market_data_gateway.py`
- `docs/live-broker-comparison-sheet-2026-04-01.md`

## Most Important Practical Conclusion

The April 1 scanner issue was not just "missing UI history." It exposed a real
restart design mismatch:

- warmup readiness depended on a 10-minute window
- retained snapshot history was configured for far less than that

That mismatch is now corrected, and the platform should recover scanner state
far more faithfully after restarts.

## Remaining Trading-Relevant Caveat

The biggest restart-specific gap identified during this session has now been
patched.

The main remaining trading-quality concern is no longer restart continuity.
It is still signal-quality parity on the `30s` path:

- live bar timing
- confirm timing versus external charting
- entry timing versus external broker

## Best Next Steps

1. Continue live validation of `30s` entries versus chart/external broker.
2. Review stale-row handling for `Top Gainers` / `Five Pillars` rows with large
   `data_age_secs`, because some names still show old quotes in low-liquidity
   premarket conditions.
3. Keep an eye on market-data heartbeats after manual service restarts so the
   control-plane status does not stay artificially `degraded`.
4. Focus next on the `30s` buy path and timing parity, which remains the main
   trading-quality question.

## Scanner Promotion Relaxation Applied Live

Later on April 1, 2026 ET, we relaxed the live confirmed-selection layer to
match legacy behavior more closely.

What changed:

- `StrategyEngineState.process_snapshot_batch()` now builds the active
  `top_confirmed` / watchlist from the top confirmed names with `min_score=0`
  instead of applying the static `rank_min_score=50` cutoff.
- This does **not** bypass confirmation itself. It only stops the visible
  candidate list from hiding already-confirmed names just because the intraday
  ranking spread is thin.

Why:

- After the earlier legacy-style scanner fixes, names like `RENX` and `BCG`
  could still be starved out of the live watchlist if the score threshold kept
  them below the visible slice.
- That was inconsistent with the intended operator workflow: confirm more
  symbols, retain them through the session, and let ranking order them rather
  than deleting them from the active universe.

Validation:

- Local test update:
  - `tests/unit/test_strategy_engine_service.py` -> `33 passed`
- Live post-deploy verification on the VPS:
  - `/api/scanner` watchlist = `['CYCN', 'RENX', 'BCG']`
  - `/api/scanner` top_confirmed = `['CYCN', 'RENX', 'BCG']`
  - `subscription_symbols = ['BCG', 'CYCN', 'RENX']`

Practical result:

- The scanner is now promoting multiple confirmed names live instead of
  starving the bots down to only `CYCN`.
- This gives the 30s bot a broader live opportunity set while keeping the
  later entry preconditions and hard gates intact.

## 30s VWAP Threshold Tuning Applied Live

Later on April 1, 2026 ET, the `macd_30s` precondition thresholds were tuned
based on live-log review.

Updated live thresholds in `TradingConfig` / `EntryEngine`:

- prior-bar VWAP precondition widened from `0.50%` to `1.00%`
- confirmation anti-chase widened from `1.00%` to `1.50%`
- separate emergency hard block added at `8.00%` VWAP distance
- EMA9 and volume-ratio preconditions were left unchanged

Why:

- `CYCN` showed that the original VWAP-based guardrails were still too tight
  for live momentum names even after the premarket VWAP anchor bug was fixed
- the goal remained the same: reject obvious chases, but allow stronger valid
  continuation setups through to the later hard gates and scoring layer

Additional cleanup:

- bot/runtime `last_bar_at` timestamps are now normalized to ET in the
  strategy runtime and control-plane API output instead of surfacing raw UTC
  values in some views

Validation:

- `tests/unit/test_decision_layer.py` -> `10 passed`
- `tests/unit/test_strategy_engine_service.py` -> `34 passed`
- `tests/unit/test_control_plane.py` -> `8 passed`

Live verification after deploy:

- control plane `/health` returned `healthy`
- strategy watchlist remained `['CYCN', 'RENX', 'BCG']`
- `macd_30s` / `macd_1m` `last_bar_at` fields in `/api/overview` now render as
  `08:33:00 AM ET` / `08:32:00 AM ET` instead of UTC ISO strings

## 30s Structure And Stoch Exit Refinement

Later on April 1, 2026 ET, we added one small structure-aware entry block and
one small stochastic-exit refinement for the `macd_30s` bot.

What changed:

- `EntryEngine` now keeps a lightweight per-symbol session-high memory and
  blocks `macd_30s` entries that are very near the recent/session high without
  actually breaking out above it.
- The new structure block runs both at trigger time and at confirmation time,
  so a setup can still be rejected if the confirmation bar becomes a late
  near-high stall.
- `IndicatorEngine` now exposes `stoch_d`, `stoch_k_prev2`, and `stoch_d_prev`
  for richer stochastic state.
- `ExitEngine` now suppresses the simple stoch-based exit for `macd_30s` when
  stochastic still looks healthy:
  - `%K` rising for two bars
  - `%K > %D`
  - `%K` slope above a small minimum
  - not rolling over from overbought
- `StrategyBotRuntime._roll_day_if_needed()` now also resets the entry-engine
  state so the new session-high memory does not leak across days.

Why:

- The `RENX` trade review suggested the 30s bot was often entering correctly
  according to indicators, but too late in the move structure.
- We wanted the smallest possible fix: block obvious near-high stalls without a
  true breakout and avoid premature stochastic exits while momentum is still
  improving.

Implementation scope:

- This is intentionally narrow and `macd_30s`-only through
  `TradingConfig.make_30s_variant()`.
- We did **not** add full failed-push counting or a full pullback-structure
  model yet.

Validation:

- `tests/unit/test_decision_layer.py` -> `14 passed`
- `tests/unit/test_strategy_core.py` -> `11 passed`
- `tests/unit/test_strategy_engine_service.py` -> `35 passed`

Notes:

- The new entry structure rule is additive. Existing hard gates, EMA20 gate,
  MACD path logic, one-bar confirmation, anti-chase checks, and score logic all
  still apply.
- Breakout entries still need to pass the existing anti-chase logic; the new
  structure filter does not override that.

## 30s Precondition Loosening Applied Live

Later on April 1, 2026 ET, we replay-tested the `CYCN`, `RENX`, and `BCG`
morning tape from `7:00 AM` to `10:00 AM ET` and confirmed that the existing
`macd_30s` VWAP preconditions were still too aggressive.

Replay takeaway:

- current 30s defaults produced `0` entries across `CYCN`, `RENX`, `BCG`
- the best conservative replay-backed profile was:
  - soft VWAP precondition disabled
  - soft anti-chase VWAP disabled
  - emergency hard VWAP widened to `25%`
  - EMA9 precondition widened from `0.50%` to `1.00%`

What changed in `TradingConfig.make_30s_variant()`:

- `entry_precondition_max_vwap_dist_pct` effectively disabled for `30s`
- `entry_anti_chase_max_vwap_dist_pct` effectively disabled for `30s`
- `entry_hard_block_max_vwap_dist_pct = 0.25`
- `entry_precondition_max_ema9_dist_pct = 0.01`

Why:

- the earlier `1.00%` soft VWAP and `1.50%` anti-chase checks were still
  rejecting strong leaders like `CYCN` and `RENX`
- after removing those soft VWAP chokepoints in replay, `EMA9` became the next
  meaningful filter, and `1.00%` gave a better balance than the original
  `0.50%`

Validation:

- `tests/unit/test_decision_layer.py` -> `14 passed`
- `tests/unit/test_strategy_core.py` -> `11 passed`
- `tests/unit/test_strategy_engine_service.py` -> `35 passed`

Practical intent:

- let the later gates do their job:
  - EMA20 trend gate
  - structure block
  - confirmation
  - score
  - stop / floor / scale exits
- keep only an extreme VWAP sanity brake instead of using VWAP as the main
  throttle

## Strategy Memory Stabilization Applied Live

Later on April 1, 2026 ET, we traced the “strange refresh / missing 30s logs”
symptom to repeated `strategy` service restarts from memory pressure rather
than a dead 30s bot.

What we found:

- `journalctl` showed repeated `SIGKILL` / `oom-kill` events for
  `project-mai-tai-strategy.service`
- the live process had been climbing into roughly `1.5G+` RAM and repeatedly
  restarting
- each restart wiped the in-memory decision tape, which made the bot page look
  like it was refreshing oddly or dropping logs

Root cause:

- `MomentumAlertEngine` was retaining up to `120` full snapshot-history
  entries at the `5s` batch cadence, with nested per-ticker dict payloads
  across the scanned universe
- bot runtimes were also keeping per-symbol bar/indicator/quote caches after
  names rotated off the watchlist
- `StrategyEngineState.latest_snapshots` was holding an unnecessary full-batch
  snapshot map even though only tracked symbols were useful later

Fixes applied:

- `src/project_mai_tai/strategy_core/momentum_alerts.py`
  - compact history entries to `(price, volume)` tuples
  - drop unused `hod` from warmup history retention
  - only retain history for symbols inside the configured scanner price band
  - keep restore compatibility with the older persisted dict-shaped history
- `src/project_mai_tai/services/strategy_engine_app.py`
  - prune runtime bar/indicator/quote state when watchlists shrink
  - clear bot caches on ET day rollover
  - keep `latest_snapshots` only for tracked confirmed/watchlist symbols
- `src/project_mai_tai/strategy_core/bar_builder.py`
  - add bar-builder eviction support
- `src/project_mai_tai/strategy_core/entry.py`
  - add symbol-state pruning support
- `src/project_mai_tai/strategy_core/runner.py`
  - prune runner quote/bar/cooldown state when symbols rotate out

Validation:

- `tests/unit/test_strategy_core.py` -> `12 passed`
- `tests/unit/test_strategy_engine_service.py` -> `36 passed`

Live deploy and result:

- pushed updated strategy files and restarted
  `project-mai-tai-strategy.service` at `01:16:27 PM ET`
- immediate post-restart memory dropped to about `890MB`
- after warmup, memory stabilized around `1.34GB`
- `NRestarts=0` over the next ~2.5 minutes after the deploy
- `/health` remained `healthy`
- `/bot` resumed showing fresh advancing `30s` decision rows at
  `01:17 PM ET`, `01:18 PM ET`, `01:19 PM ET`

Practical conclusion:

- the 30s bot itself was live; the bad dashboard experience was mostly a
  byproduct of strategy-service restart churn
- this live memory patch materially improved service stability and restored a
  normal advancing decision tape

## Late-Session 30s Trading Outcome

Later on April 1, 2026 ET, after the earlier restart-hardening and 30s tuning
passes, Mai Tai recorded additional `macd_30s` executions beyond the older
midday API snapshot that had been used for earlier analysis.

Observed from live bot/trade surfaces:

- earlier clean Mai Tai `30s` closed trades still stood as:
  - `RENX` `02:04:06 PM ET -> 02:06:36 PM ET` `-5.00`
  - `CYCN` `02:05:07 PM ET -> 02:07:11 PM ET` `-3.00`
- later `AGPU` executions appeared in the post-`4:00 PM ET` window:
  - `04:07:02 PM ET` / `04:07:03 PM ET` `AGPU` buy fills
  - `04:22:00 PM ET` additional `AGPU` buy fills
  - `04:22:51 PM ET` `AGPU` `HARD_STOP` close filled
- later `CYCN` executions also appeared:
  - `04:38:01 PM ET` `CYCN` `ENTRY_P2_VWAP_BREAKOUT` buy filled
  - `04:39:30 PM ET` `CYCN` `HARD_STOP` close was attempted but rejected

Important interpretation:

- the older `03:31 PM ET` API snapshot was stale for the later window and did
  not include these later fills
- the `Recent Trades` and `Decision Tape` surfaces later confirmed that
  `macd_30s` remained active into the late-session window
- at least one post-`4:00 PM ET` close was clean (`AGPU`)
- at least one later close path still needs OMS/reconciliation review because
  the `CYCN` hard-stop close was rejected after the buy fill

Practical conclusion:

- the 30s bot was not dead or fully starved for the full afternoon
- but the late-session trades did not yet establish confidence that the new
  filters improved quality
- the next useful follow-up remains:
  - compare late-session Mai Tai `30s` trades against legacy / TradingView
  - inspect why the `CYCN` post-entry hard-stop close rejected
  - review whether the remaining EMA9 and volume-ratio preconditions are still
    suppressing too many valid setups

## Late Follow-Up: OMS Rejected Close Handling Review

We revisited the late-session rejected-close follow-up after the earlier
`CYCN` note and confirmed one real OMS-side failure mode that could produce a
false reject even when the broker still had quantity.

What we found:

- close / scale sell intents were being prechecked against durable
  `account_positions`
- if that table temporarily lagged the broker, OMS could reject with
  `no broker position available to sell` before revalidating broker state
- this was a real workflow risk for late-session or restart-adjacent exits
  because the rejection happened *before* a broker submission attempt

Fix applied:

- `src/project_mai_tai/oms/service.py`
  - before rejecting a close/scale sell for
    `no broker position available to sell`, OMS now refreshes broker positions
    once from the live adapter and resyncs `account_positions` in-session
  - only if the refreshed broker snapshot still shows no quantity does OMS keep
    the reject

Why this matters:

- it hardens hard-stop / protective exits against stale local broker-position
  state
- it reduces one plausible explanation for the late `CYCN` close reject without
    claiming that every reject path is now solved

Validation:

- `tests/unit/test_oms_risk_service.py`
  - stale DB account position with broker quantity still present now exits
    cleanly after refresh
  - true no-position state still rejects with
    `no broker position available to sell`

Current interpretation:

- the broad late-session OMS follow-up is narrower now
- one real stale-state reject path is fixed
- any future late-session close reject is now more likely to be a true broker /
  order-state issue rather than just a stale local `account_positions` row

## Late Follow-Up: Control-Plane Feed-Status Normalization

We also closed the main control-plane/feed-status follow-up that had been left
as a display/workflow issue rather than a trading-core failure.

What we found:

- the scanner/control-plane could still look artificially degraded when
  `market-data-gateway` heartbeat status briefly lagged at `starting` or
  `stopping`
- during those windows, fresh snapshot batches and subscription activity could
  already be flowing again
- operators therefore had to manually infer that the feed was actually live

Fixes applied:

- `src/project_mai_tai/services/control_plane.py`
  - market-data service rows now carry:
    - raw heartbeat `status`
    - normalized `effective_status`
    - a short `status_note`
  - fresh snapshot/subscription activity now upgrades the *display* status to
    healthy/live when heartbeat state is only lagging
  - scanner/API output now includes:
    - `heartbeat_active_symbols`
    - `feed_status`
    - `feed_status_note`
  - recent OMS order rows now also expose `intent_type` and latest known
    `reason`, which makes rejected-close review much easier from the control
    plane

Practical result:

- `/api/overview` and `/scanner/dashboard` are less likely to show a misleading
  degraded feed state during restart transitions
- the scanner page now explains when the raw heartbeat still says
  `stopping`/`starting` but fresh feed activity is present
- OMS rejected-close debugging is easier because the order surfaces now carry
  the latest reason text instead of only a bare status

Validation:

- `tests/unit/test_control_plane.py`
  - fresh market-data activity with a raw `stopping` heartbeat now renders as
    an effective healthy/live feed
  - scanner/dashboard output shows the feed-status note
  - recent orders expose the latest reason field

## Focused Test Run After These Follow-Ups

- `tests/unit/test_oms_risk_service.py` -> `16 passed`
- `tests/unit/test_control_plane.py` -> `9 passed`
- combined targeted run -> `25 passed`

## Updated Remaining Caveat

These two follow-ups are in a better state now:

- the stale local broker-position reject path for close orders is fixed
- the main market-data heartbeat/feed-status display lag is normalized in the
  control plane

What still remains open:

- if a later close is rejected *after* broker submission, the next review
  should focus on the exact broker/order reason rather than the old stale
  `account_positions` path
- raw `market-data-subscriptions` stream/source drift is still worth a later
  cleanup even though the operator-facing status now degrades much less

## Late Follow-Up: 30s TV Confirmation Parity

We dug into April 1 `CYCN` TradingView-versus-Mai-Tai behavior using the TV CSV
export and found that `P1_MACD_CROSS` itself was not the main issue.

What we found:

- Mai Tai matched all three TV-confirmed `P1_MACD_CROSS` entries on April 1
- the bigger mismatch was on some `P2` / `P3` rows where TV confirmed an entry
  after the trigger bar, but Mai Tai re-ran hard gates on the confirmation bar
- that extra confirmation-bar recheck caused misses when:
  - `stochK` rose above `90` after the trigger bar had already passed
  - price moved beyond the `EMA9` 8 percent distance cap after the trigger bar
    had already passed

Fix applied:

- `src/project_mai_tai/strategy_core/entry.py`
  - pending setups now freeze hard-gate eligibility on the trigger bar
  - confirmation bars no longer re-run EMA20 / `stochK` / EMA9 distance hard
    gates for the same pending setup
  - confirmation still honors the narrower cancellation path:
    - outside trading hours / dead zone
    - already in position
    - MACD lost above-signal state
    - price lost the trigger bar close
    - score below threshold

Validation:

- `tests/unit/test_decision_layer.py`
  - added targeted coverage that a valid trigger can still confirm even if the
    next bar moves above the `stochK` cap
  - added targeted coverage that a valid trigger can still confirm even if the
    next bar moves beyond the EMA9 distance cap

Reminder for live validation:

- validate EMA9 trigger-versus-confirm behavior with a real live trade on the
  next trading session before treating the change as fully settled

## Late Follow-Up: Durable Strategy Bar History

We added a durable, storage-only history path for post-trade analysis so we do
not have to rely only on ad hoc CSV exports or replay reconstruction later.

What was missing before:

- durable trade/order/account state already existed in Postgres
- closed-trade CSVs existed on disk
- but Mai Tai did not durably store one row per completed strategy bar with the
  bar data, indicator snapshot, and bar-level decision context

Fix applied:

- `src/project_mai_tai/db/models.py`
  - added `strategy_bar_history`
  - each row stores:
    - strategy code
    - symbol
    - interval
    - completed bar time
    - OHLCV / trade count
    - compact indicator snapshot
    - position state / quantity
    - decision status / reason / path / score
- `sql/migrations/versions/20260401_0003_strategy_bar_history.py`
  - adds the new table plus lookup indexes
- `src/project_mai_tai/services/strategy_engine_app.py`
  - `StrategyBotRuntime` now writes one best-effort upsert per completed bar
  - persistence is write-only and does not affect signal generation or OMS flow
  - if the DB write fails, trading continues and the failure is only logged
- `src/project_mai_tai/settings.py`
  - added `MAI_TAI_STRATEGY_HISTORY_PERSISTENCE_ENABLED` (default `true`)

Design intent:

- memory impact stays flat because this is not a new warm cache
- existing trading logic is unchanged; persistence runs after bar evaluation
- history is durable in Postgres, so we can come back later for TV-vs-Mai-Tai
  gap analysis with internal ground truth instead of only replay guesses

Validation:

- `tests/unit/test_strategy_history_persistence.py`
  - verifies bar + decision snapshots persist correctly
  - verifies repeated writes for the same bar update the same row instead of
    duplicating it

## Late Follow-Up: Expanded Test Trading Window

For the next live test session, we widened the strategy entry window to cover
the full extended-hours test range.

Change applied:

- `src/project_mai_tai/strategy_core/trading_config.py`
  - default strategy trading window now starts at `4:00 AM ET`
  - trading end remains `8:00 PM ET`

Scope:

- this changes the strategy bot entry window only
- scanner/feed behavior was not changed
- the new durable strategy bar history remains active, so tomorrow's premarket
  and after-hours completed bars can be reviewed later from Postgres

Validation:

- `tests/unit/test_strategy_core.py`
  - added coverage that the default strategy window is now `4:00 AM ET` to
    `8:00 PM ET`

## Late Follow-Up: 30s Dedup Lock Bug

We traced the April 2 `TURB` 30-second lockout to a real dedup bug in Mai Tai,
not to TradingView path math.

What we found:

- `macd_30s` correctly evaluates only on completed bars
- but `StrategyBotRuntime._evaluate_completed_bar()` was passing `len(bars)` to
  `EntryEngine.check_entry()`
- the bar builder keeps only the most recent `2000` completed bars in memory
- after a symbol hydrates with more than `2000` bars, `len(bars)` stops
  increasing even though the true completed-bar count keeps advancing
- that made entry dedup think later setups were still the "same bar" and
  produced repeated `dedup (already fired this bar)` blocks on later valid
  `P1` / `P3` rows
- this matched the live April 2 `TURB` pattern exactly:
  - one real `P3_MACD_SURGE` signal at `04:48:30 AM ET`
  - accepted as an extended-hours limit buy
  - then canceled unfilled after about `10` seconds
  - later valid-looking `P1` / `P3` rows were often blocked by stale dedup

Fix applied:

- `src/project_mai_tai/services/strategy_engine_app.py`
  - switched the entry-engine bar index from trimmed `len(bars)` to the bar
    builder's monotonic `get_bar_count()`
  - this preserves same-bar dedup while allowing future completed bars to be
    evaluated normally even after long historical hydration

Validation:

- `tests/unit/test_strategy_engine_service.py`
  - added regression coverage that the runtime now passes a monotonic bar index
    after history trim
  - added regression coverage that a canceled open no longer leaves a
    history-trimmed symbol effectively locked out from the next valid open

## Deferred Follow-Ups: Lower Priority

These are worth keeping on the project list, but they are not the highest
priority compared with live trade validation and core 30s execution quality.

1. Recent Trades vs broker orders reconciliation
   - the control-plane `Recent Trades` panel is fill-level, while the broker
     orders table is order-level, so quantity splits like `33 + 19 + 48 = 100`
     are expected
   - however, partial-fill pricing is not exact today:
     - Alpaca reports cumulative `filled_qty` + cumulative `filled_avg_price`
     - OMS stores an incremental quantity slice using that cumulative average
     - this makes the displayed fill rows useful for activity review, but not a
       true execution-leg ledger
   - concrete example seen on April 2:
     - `TMDE` scale sell order for `75` shares showed fill rows of
       `14 @ 1.74`, `31 @ 1.75`, and `30 @ 1.778`
     - those rows imply a weighted average near `1.7592`
     - the broker order row showed average fill price `1.778`
   - future fix direction:
     - either persist true broker execution legs if available
     - or clearly separate "execution activity rows" from exact
       broker-reconciled fill/accounting rows in the UI

2. EMA9 live validation
   - keep validating the current EMA9 trigger-vs-confirm behavior with live
     trades before treating that rule as permanently settled

3. `P2_VWAP_BREAKOUT` live validation
   - keep checking TV-vs-Mai-Tai parity on fresh live examples
   - recent CSV exports were inconsistent, so live-session comparison is still
     the best source of truth

4. Live 30s bar-close parity vs TV execution parity
   - on April 2 `TMDE`, TV showed a valid `07:00:30 AM ET` `P1_MACD_CROSS`
     that current entry rules would accept if given the same bar values
   - Mai Tai stayed `idle / no entry path matched`, which pointed upstream of
     the entry gates
   - root cause:
     - completed 30-second bars were only evaluated when the next trade tick
       arrived
     - on thinner names, that can delay or skip the exact confirm bar that TV
       used
   - fix direction applied:
     - strategy loop now forces timed bar closes and evaluates due bars even if
       no next trade arrives
   - still-open separate follow-up:
     - execution parity with TV is not the same problem
     - Mai Tai still uses quote-anchored extended-hours limit routing rather
       than literal TradingView order behavior

5. Live 30s bar/feed parity after the timed-close fix
   - on April 2 `SKYQ`, the old live path still missed a strong post-restart
     `P3_MACD_SURGE` even after the timed bar-close bug was fixed
   - direct comparison showed:
     - Massive REST `30s` aggregates were rich and matched the expected bar
       shape around `07:35:30-07:36:30 AM ET`
     - Mai Tai's stored live-built bars for `07:36:00` and `07:36:30` were
       extremely thin, which prevented the path from firing
   - fix direction applied:
     - market-data gateway now publishes live `1s` aggregate bars from Massive
       websocket `A.<symbol>` subscriptions
     - `macd_30s` now builds entry bars from those live aggregate bars instead
       of raw trade ticks
     - raw trade ticks are still used for intrabar price/hard-stop updates
   - production constraint discovered immediately after deployment:
     - the current Massive websocket entitlement rejected `A.<symbol>`
       subscriptions with `1008 policy violation`
     - to keep the live stack healthy, the aggregate-stream path is now guarded
       by `MAI_TAI_MARKET_DATA_LIVE_AGGREGATE_STREAM_ENABLED`
     - default is `false`, so production is back on the stable trade-tick path
       until we either enable a supported aggregate feed or move this parity
       improvement to a REST-based overlay
   - local validation:
     - focused unit coverage now checks `30s` aggregation from live second bars
       and stale-bar rejection
     - replaying `SKYQ` with Massive historical `30s` seed bars plus live `1s`
       aggregates now produces the missed `P3` entry
   - remaining nuance to validate live:
     - the new path now surfaces the trade, but local replay still shows a
       narrower confirm-timing difference versus the TV label timing
     - treat this as much smaller than the original "no signal at all" bug,
       and validate on the next live examples

6. `macd_1m` TAAPI indicator source
   - implemented a new optional TAAPI-backed indicator source for `macd_1m`
     only
   - scope:
     - source `MACD`, `signal`, `histogram`, `EMA9`, `EMA20`, `stoch_k`,
       `stoch_d`, and `VWAP` from TAAPI
     - keep `macd_30s`, `tos`, and `runner` unchanged
     - keep `extended_vwap` local because TAAPI does not provide it directly
   - new settings:
     - `MAI_TAI_STRATEGY_MACD_1M_TAAPI_INDICATOR_SOURCE_ENABLED`
     - `MAI_TAI_TAAPI_SECRET`
   - fallback setting still available:
     - `MAI_TAI_STRATEGY_MACD_1M_MASSIVE_INDICATOR_OVERLAY_ENABLED`
   - behavior:
     - when TAAPI is enabled and both the TAAPI secret and Massive/Polygon key
       are present, `macd_1m` replaces its local `MACD` / `EMA` / `Stoch` /
       `VWAP` inputs with TAAPI values before entry/exit evaluation
     - derived booleans like crosses, rises, histogram growth, and
       `price_cross_above_vwap` are recomputed from the TAAPI-backed values
     - snapshots/history include provider values and local-vs-provider diffs
       for parity review
     - the live path now uses TAAPI's documented Polygon-backed stock mode:
       - endpoint: `https://us-east.taapi.io`
       - `provider=polygon`
       - `providerSecret=<Massive/Polygon key>`
     - the TAAPI HTTP client now sends a standard `User-Agent`; without it,
       the `us-east` endpoint was returning Cloudflare-style `403 / 1010`
       responses even though the same request worked with a normal client
   - current intentional local fallback:
     - `extended_vwap`
     - `price_above_extended_vwap`
     - `price_cross_above_extended_vwap`
   - local validation:
     - focused tests confirm only `macd_1m` receives the TAAPI provider
     - snapshots include TAAPI `stoch` and `VWAP` fields
     - `macd_1m` decision inputs now follow TAAPI values when enabled
   - deployed validation:
     - strategy service restarted cleanly on April 2
     - live `macd_1m` indicator snapshots now show `provider_source=taapi`
       and `provider_status=ready` for scanner names like `COCP` and `SKYQ`

7. `tos` bot entry alignment to Thinkorswim script
   - updated `tos` entry behavior to mirror the provided Thinkorswim script on
     the entry side only
   - entry is now limited to two instant paths:
     - `P1_MACD_CROSS`: MACD cross above + MACD increasing + volume > 5000 +
       VWAP filter pass
     - `P2_VWAP_BREAKOUT`: VWAP cross above + MACD above signal + MACD
       increasing + volume > 5000
   - removed `P3` from `tos` entry logic
   - disabled the dead zone per user direction
   - `tos` still keeps the existing Mai Tai exit framework:
     - tier exits
     - floor protection
     - scale logic

8. `runner` bot one-shot-per-symbol behavior
   - updated `runner` to keep using the existing confirmed top-feed handoff
     without any scanner redesign
   - new behavior:
     - `runner` can now hold multiple symbols at the same time when multiple
       top confirmed names are fed to it
     - once `runner` gets a filled open on a symbol, that symbol is blocked for
       the rest of the ET trading day
     - no same-day re-entry after exit
     - no change to the fixed `10%` trail, EMA exit timing, or scanner feed
   - implementation detail:
     - the one-shot block is tracked inside the runner runtime as
       `entered_today`
     - open positions, pending opens, pending closes, and close-retry blocks
       are now tracked per symbol instead of globally
     - it resets automatically on the next ET trading day
   - local validation:
     - focused runner tests now verify:
       - a symbol cannot be re-entered after its first filled runner trade
       - multiple runner symbols can be open at the same time

9. Eastern-time gating fix across bot runtimes
   - fixed a timezone mismatch that could falsely block entries as
     `outside trading hours (20:00 ET)` while the actual clock was around
     `4:30 PM ET`
   - root cause:
     - strategy bot runtimes were defaulting to a UTC clock for entry/exit
       time checks
     - the block message labeled the hour as ET, which made the false block
       visible on `tos` and would have affected the other strategy bots too
   - fix direction:
     - strategy bot runtimes now default to `now_eastern()` instead of UTC
     - runner runtime now also defaults to `now_eastern()`
   - local validation:
     - added a regression test proving `4:30 PM ET` passes the bot trading
       window instead of being treated like `20:30`

10. Control-plane UI cleanup for overview and bot pages
   - simplified the bot detail pages so the navigation now lives in the top
     banner instead of a noisy left sidebar
   - removed duplicate or low-value bot-side content including:
     - `Legacy Shadow`
     - `Account Model`
     - ranked-feed text clutter
     - pending workflow stacks
     - bot notes
     - account exposure
   - added a cleaner top banner on bot pages with:
     - status
     - account
     - mode
     - provider
     - interval
     - live symbol pills
   - replaced duplicate top metrics with the compact set:
     - daily P&L
     - open
     - closed
     - pending
     - trades today
   - overview page cleanup:
     - removed the `Shadow` navigation link and the legacy shadow fold
     - trimmed the overview section to critical scanner state only
     - removed the text-heavy control-plane notes section
   - later refinement:
     - restored the two-column bot layout with the identity/status rail on the
       left and compact navigation pills on the right
     - kept the primary bot tables focused on:
       - `Open Positions`
       - `Closed Trades`
       - `Failed Actions`
       - `Bot Decisions`
     - `Closed Trades` is now the main realized-trade table with ticker, path,
       quantity, entry, exit, realized P&L, and close reason
     - `Bot Decisions` now renders as a scrollable 50-row table instead of a
       floating log block

11. `macd_30s` trigger-quality / confirmation redesign
   - changed the `30s` entry flow so setup quality is judged on the trigger bar
     and confirmation only validates that the breakout area held
   - implementation changes in `entry.py`:
     - trigger-bar score is stored in pending state
     - confirmation no longer rescoring the next bar
     - confirmation no longer requires price to stay above the trigger close
     - confirmation now requires the confirm-bar close to stay above a stored
       breakout level with a small tolerance
     - setup-quality checks are now run before pending confirmation is created
   - implementation change in `trading_config.py`:
     - added `confirmation_hold_tolerance_pct`
   - testing:
     - updated decision-layer coverage around trigger-score carry-forward and
       pullback confirmation
     - local unit results:
       - `tests/unit/test_decision_layer.py`: `29 passed`
       - `tests/unit/test_strategy_core.py`: `14 passed`
   - same-day stored-bar sanity replay:
     - using April 2 stored `30s` bars with immediate-fill replay logic,
       simulated open count dropped from `38` real filled opens to `21`
     - per-symbol replay opens:
       - `SKYQ 10`
       - `BDRX 3`
       - `COCP 1`
     - `TMDE 4`
     - `TURB 2`
     - `PFSA 1`

12. `macd_30s_probe` / reclaim-path research status as of April 3
   - scope:
     - this work stayed focused on the separate `30s` probe family
     - `macd_1m`, `tos`, and `runner` were intentionally left unchanged for
       the next live session
   - current live/runtime status:
     - `macd_30s_probe` already exists in the runtime registry when
       `MAI_TAI_STRATEGY_MACD_30S_PROBE_ENABLED=1`
     - the new reclaim path has been added inside the entry engine, but it is
       not yet wired as its own separate runtime definition
     - in code today:
       - `entry_logic_mode="pretrigger_probe"` = compression/pressure starter
       - `entry_logic_mode="pretrigger_reclaim"` = pullback/reclaim starter
   - current `pretrigger_probe` logic:
     - hard requirements:
       - compression shelf must be present
       - location must be constructive
       - candle must be constructive
       - pressure must be present
       - histogram must be positive
       - `MACD > Signal`
       - `StochK < 90`
       - starter volume must pass
       - early-momentum gate must pass
       - trend must still be constructive
     - starter sizing:
       - starter quantity = `25%` of base size
       - add-on quantity can scale to full size only after a later confirm
     - failure/add behavior:
       - starter exits quickly on failed hold / no-confirm
       - confirm add is blocked if price is already extended relative to EMA9
   - current `pretrigger_reclaim` logic:
     - looks for:
       - prior pullback from recent high
       - recent touch of EMA9 or selected VWAP
       - reclaim bar above support
       - directional extension control instead of absolute distance
       - near-one-anchor-is-enough location logic
       - positive/recovering momentum, candle quality, and volume
     - this path was built specifically because names like `CYCN` and `RENX`
       did not behave like clean compression shelves
   - local validation summary:
     - probe-only historical/provider replays:
       - `CYCN` (`2026-04-01`) with stricter compression path eventually
         overblocked down to `0` trades
       - `RENX` (`2026-04-01`) with stricter compression path also produced
         `0` trades
     - reclaim-path historical/provider replays:
       - `CYCN` (`2026-04-01`): `2` buys, `0` taken-good, `2` taken-bad
       - `RENX` (`2026-04-01`): `0` buys
     - reclaim-path local TradingView CSV replays for April 2:
       - `SKYQ`: `0` buys
       - `TMDE`: `0` buys
   - main blockers observed repeatedly:
     - compression path:
       - `pretrigger compression not ready`
       - `pretrigger location not ready`
     - reclaim path:
       - `pretrigger reclaim pullback not ready`
       - `pretrigger reclaim location not ready`
       - then candle quality
   - important interpretation:
     - loosening location alone did not unlock `SKYQ` or `TMDE`
     - the reclaim path already uses the directional-extension concept:
       - it does not treat simply being above EMA9/VWAP as invalid
       - it only blocks when price is too extended above the anchor(s)
     - on the current data, the bigger limiter is still the pullback/reclaim
       definition, not the location threshold by itself
   - what still needs follow-up:
     - inspect the exact reclaim near-miss bars on `SKYQ` and `TMDE`
     - tune reclaim pullback window and anchor-touch timing
     - decide whether reclaim deserves its own runtime/bot or should remain an
       internal alternate path first
     - continue live validation tomorrow without restart churn before making
       more structural changes

13. April 3 deploy note
   - files pushed to the VPS:
     - `src/project_mai_tai/strategy_core/entry.py`
     - `src/project_mai_tai/strategy_core/trading_config.py`
     - `docs/session-handoff-2026-04-01.md`
   - remote validation:
     - remote `py_compile` passed for the updated strategy files
     - `project-mai-tai-strategy.service` restarted successfully
     - later service checks showed:
       - `project-mai-tai-strategy.service`: `active`
       - `project-mai-tai-control.service`: `active`
       - `http://127.0.0.1:8000/health`: healthy
   - runtime note:
     - the new reclaim path is deployed in the strategy engine code
     - it is not yet exposed as its own enabled live runtime/bot
     - the currently active live bots remain:
       - `macd_30s`
       - `macd_1m`
       - `tos`
       - `runner`
   - follow-up fix after deploy:
     - the strategy service later showed empty bots/scanner because it was
       crash-looping on the VPS
     - root cause:
       - the VPS copy of `src/project_mai_tai/settings.py` was missing the
         newer `macd_30s_probe` settings fields used by the runtime registry
     - fix applied:
       - pushed `src/project_mai_tai/settings.py` to the VPS
       - re-ran remote `py_compile` for `settings.py`
       - restarted `project-mai-tai-strategy.service`
     - after the fix:
       - `project-mai-tai-strategy.service`: `active`
       - control-plane overview returned normally again
       - scanner/bots were empty because the engine was up but had `0`
         confirmed names / `0` active symbols at that moment, not because of
         another crash
