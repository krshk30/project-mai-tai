# Session Handoff - Global

## Use This File First

This is the single global handoff file for active agent context.

If another agent needs current project state, start here first:

- [session-handoff-global.md](C:/Users/kkvkr/OneDrive/Documents/GitHub/project-mai-tai/docs/session-handoff-global.md)

Older dated handoffs have been archived under:

- `docs/archive/session-handoffs/`

## Current Source Snapshot

This global handoff is based on the latest active session consolidation from
`2026-04-17`.

## Deployment Discipline

Standard operating rule going forward:

- `main` is the only deployable branch
- the VPS should stay on `main`
- feature branches such as `codex/...` are for development, validation, and PR
  review only
- after a change is validated, merge to `main`, deploy from `main`, verify SHA
  alignment across local/GitHub/VPS, and update this handoff immediately

Required release checklist:

1. work on `codex/...`
2. run local validation
3. push branch and update PR
4. wait for green GitHub `Validate`
5. merge into `main`
6. update local `main`
7. update VPS `main`
8. restart only the required services
9. verify local/GitHub/VPS all match the same SHA
10. record that SHA and the release summary in this handoff right away

## What Changed

This handoff captures the TradingView automation and webhook work completed on
`2026-04-17`, including:

- Schwab/webhook cutover onto the VPS
- TradingView alert automation build-out and VPS session bootstrap
- cleanup and verification of stale TradingView alerts
- sticky intraday TradingView alert behavior
- current live status and next to-do items

## Webhook / Schwab Status

The VPS webhook path is live and working:

- public webhook host:
  - `https://hook.project-mai-tai.live/webhook`
- Schwab OAuth callback:
  - `https://hook.project-mai-tai.live/auth/callback`
- Schwab auth/token persistence is working on the VPS
- off-hours order construction was corrected to use fresh Schwab quote data
  instead of the old signal-price buffer path

Current operational split:

- scanner / Mai Tai runtime on VPS
- TradingView alert automation on VPS
- webhook execution + Schwab execution on VPS

## TradingView Automation Build-Out

The following pieces were added and verified in this repo:

- TradingView alert sidecar service
  - [tradingview_alerts_app.py](C:/Users/kkvkr/OneDrive/Documents/GitHub/project-mai-tai/src/project_mai_tai/services/tradingview_alerts_app.py)
- Playwright TradingView operator
  - [tradingview_playwright.py](C:/Users/kkvkr/OneDrive/Documents/GitHub/project-mai-tai/src/project_mai_tai/services/tradingview_playwright.py)
- session export / probe scripts
  - [tradingview_export_session.py](C:/Users/kkvkr/OneDrive/Documents/GitHub/project-mai-tai/scripts/tradingview_export_session.py)
  - [tradingview_probe_session.py](C:/Users/kkvkr/OneDrive/Documents/GitHub/project-mai-tai/scripts/tradingview_probe_session.py)
- manual TradingView alert list/delete helper
  - [tradingview_manage_alerts.py](C:/Users/kkvkr/OneDrive/Documents/GitHub/project-mai-tai/scripts/tradingview_manage_alerts.py)
- session refresh runbook
  - [tradingview-vps-session-refresh-runbook-2026-04-17.md](C:/Users/kkvkr/OneDrive/Documents/GitHub/project-mai-tai/docs/tradingview-vps-session-refresh-runbook-2026-04-17.md)
  - [TradingView-VPS-Session-Refresh-Runbook-2026-04-17.docx](C:/Users/kkvkr/OneDrive/Documents/GitHub/project-mai-tai/docs/TradingView-VPS-Session-Refresh-Runbook-2026-04-17.docx)

## Critical VPS TradingView Result

Direct VPS TradingView sign-in was blocked by TradingView rate limiting on the
login endpoint, but the session-bootstrap path now works:

1. export a live TradingView session from local Windows Chrome
2. inject that session into a fresh Linux Chrome profile on the VPS
3. run TradingView automation on the VPS without hitting the VPS login flow

Important result:

- VPS TradingView auth/session bootstrap is viable
- VPS alert create/delete is working
- current active service mode is:
  - `provider=playwright`
  - `auto_sync_enabled=true`

## Alert Cleanup / Verification

Multiple stale TradingView alerts were discovered during bring-up. The initial
cleanup checks were flawed because the TradingView `Log` tab was read instead of
the real `Alerts` tab. That was corrected.

Real stale alert cleanup was later verified against the actual TradingView
Alerts panel.

After final cleanup and later state corrections:

- stale symbols such as `AAPL`, `TSLA`, `NFLX`, `BFRG`, `KIDZ`, and stale
  `MYSE` were removed from the real TradingView account
- current managed alert state was brought back to:
  - `ELAB` only

## Delete-Path Bug Found And Fixed

One important bug was found in the TradingView remove flow:

- the service could treat a symbol as removed based on internal state even when
  the real TradingView alert still existed

This was fixed by tightening the remove path in
[tradingview_playwright.py](C:/Users/kkvkr/OneDrive/Documents/GitHub/project-mai-tai/src/project_mai_tai/services/tradingview_playwright.py):

- after a remove attempt, the operator now re-checks the actual TradingView
  alert list
- it only treats the delete as successful if the alert is truly absent

Regression coverage was added in:

- [test_tradingview_alert_service.py](C:/Users/kkvkr/OneDrive/Documents/GitHub/project-mai-tai/tests/unit/test_tradingview_alert_service.py)

## Sticky Intraday Alert Behavior

The TradingView alert policy changed during this session.

Old behavior:

- scanner confirm -> create alert
- live path drop -> remove alert immediately

New behavior:

- scanner confirm -> create alert
- intraday live path drop -> keep the alert for the current scanner session
- old-session leftovers can still roll off after session change

Reason for the change:

- reduce orphan/mismatch risk
- avoid missing same-session re-entries after a stock re-accelerates
- let the TradingView/Pine side filter poor setups instead of aggressively
  removing the alert immediately

Important note:

- the sticky behavior was deployed
- one manual cleanup pass was needed after deploy to remove the pre-existing
  stale `MYSE` from the sticky set baseline
- the live state now reflects the intended baseline correctly

## Webhook Pending-Entry Close Bug Fixed

A critical after-hours webhook bug was found on `2026-04-17` in the Schwab
execution server:

- TradingView could send a `CLOSE` for a still-pending extended-hours `BUY`
- Schwab order-status lookups were returning `400`
- Schwab cancel attempts were also returning `400`
- the old server logic could still clear the pending entry locally as
  `close_before_fill`

That created an unsafe divergence:

- broker state unknown
- local pending state cleared
- later close alerts rejected as `no position`

The webhook server was patched so that:

- if cancel is not confirmed and order status is still unknown, the server does
  **not** clear the pending entry
- it marks that pending entry with a close-requested state instead
- if that pending buy later fills, the server now immediately submits the close
  instead of silently treating the trade as gone

Regression coverage was added in the webhook-server test suite and the VPS
webhook service was redeployed with the fix.

## Current Live State

At the end of this session, the VPS `tradingview-alerts` health showed:

- `provider = playwright`
- `auto_sync_enabled = true`
- `auth_required = false`
- `last_error = null`
- `managed_symbols = ["ELAB"]`
- `desired_symbols = ["ELAB"]`
- `requested_symbols = ["ELAB"]`

The control plane is up again and `project-mai-tai.live` is reachable behind
basic auth.

Important dashboard interpretation from this session:

- historical fills/data were not lost
- empty live panels earlier in the day were due to empty current runtime state,
  not a database wipe

## ELAB Scanner Read

Key ELAB timeline captured during this session:

- `07:31:05 AM ET`
  - `VOLUME_SPIKE`
  - `SQUEEZE_5MIN`
  - `SQUEEZE_10MIN`
- `07:32 AM ET`
  - news article present, but not qualifying `Path A` news
- `07:36:05 AM ET`
  - scanner confirmation:
    - `confirmation_path = PATH_B_2SQ`

Interpretation:

- ELAB was confirmed because of the Path B squeeze/volume behavior
- news existed, but it was not the reason ELAB was promoted
- Path A news eligibility was false for that event

## Operational Notes

1. Local helper Chrome/session
   - the earlier local helper/browser process used during bring-up was shut down
   - the active TradingView automation path is now the VPS headless service

2. Re-login detection
   - relogin detection logic exists
   - notification delivery is still not configured
   - current practical check is the VPS `tv-alerts` health endpoint

3. Existing control-plane visibility
   - `tradingview-alerts` appears in the service strip and Service Health table
   - there is not yet a dedicated TradingView-specific dashboard tile

## To-Do / Next Items

1. Confirmation / rank timing
   - review whether the current promotion threshold is too slow
   - specifically examine whether waiting for higher rank (for example `70`)
     causes late live-path promotion

2. News relaxation
   - revisit Path A / news strictness
   - consider allowing stronger scanner names through with softer news handling
     when score is already strong enough

3. TradingView bot UI
   - build a dedicated TradingView operations screen showing:
     - managed alerts
     - requested/protected symbols
     - sync plan
     - session/auth state
     - log/activity history

4. Pre-market health check
   - add a `6:00 AM ET` readiness check for TradingView automation / service
     health

5. End-of-day cleanup
   - after-hours reset rule requested:
     - `6:01 PM ET` -> delete all session-created TradingView alerts

6. Momentum alert catch-up logic
   - review and tune the new late catch-up spike path if needed
   - goal: do not miss obvious current-state moves just because the earlier
     internal spike seed was missed

7. Historical scanner overlap analysis
   - historical `five_pillars` / `top_gainers` membership at exact confirmation
     time was not reconstructable from the old persistence model
   - fix deployed:
     - strategy engine now appends `scanner_cycle_history` snapshots to
       `dashboard_snapshots`
     - each row stores reduced per-cycle scanner state:
       - `watchlist`
       - `all_confirmed`
       - `top_confirmed`
       - `five_pillars`
       - `top_gainers`
       - ticker-only helper arrays for overlap checks
     - rows are appended only when scanner state meaningfully changes
     - retention is capped by `MAI_TAI_DASHBOARD_SCANNER_HISTORY_RETENTION`
       (default `5000`)
   - VPS verification after deploy:
     - `scanner_cycle_history` rows are now being written successfully

## EFOI Bug Fix

Issue observed:

- `EFOI` appeared in broad scanners (`top_gainers`, `five_pillars`) but never
  entered the raw momentum-alert sequence
- user highlighted a clear `09:15 - 09:20 AM ET` move with large volume and
  strong price expansion that should still have been capturable

Diagnosis:

- this was not downtime
- this was not a low-volume filter issue
- the real gap was in the momentum alert chain:
  - `VOLUME_SPIKE` must be emitted first
  - only then do `SQUEEZE_5MIN` / `SQUEEZE_10MIN` alerts open up
- if the internal spike seed is missed, later obvious squeezes can be ignored

Fix deployed:

- `momentum_alerts.py` now supports a late catch-up seed path
- if the engine sees an obvious current spike + squeeze combination after the
  earlier seed was missed, it can backfill `VOLUME_SPIKE` and allow squeeze
  alerts in the same cycle
- a regression test was added for this path

Validation:

- `tests/unit/test_strategy_core.py` -> `15 passed`
- strategy service was restarted on VPS
- `project-mai-tai-strategy.service` returned healthy/active after deploy

## If Picking Up Later

The most important current mental model is:

- the VPS TradingView session is now bootstrapped from a valid exported local
  session
- intraday alerts are intentionally sticky
- stale real-alert removal was a real bug and has been fixed
- the scanner now persists historical cycle snapshots for later overlap analysis
- the current expected live baseline is:
  - only real confirmed/session-kept symbols should remain

## Central Feed Retention Policy

Scope:

- this session added a central scanner-to-bot feed-retention layer
- this is not a scanner rewrite
- this sits between:
  - scanner confirmation output
  - bot watchlist / subscription targets
- implementation files:
  - [feed_retention.py](../src/project_mai_tai/strategy_core/feed_retention.py)
  - [strategy_engine_app.py](../src/project_mai_tai/services/strategy_engine_app.py)
  - [settings.py](../src/project_mai_tai/settings.py)

Problem being solved:

- previously the live bot watchlist followed `current_confirmed` directly
- once a name fell out of the scanner-confirmed set, it could disappear from
  the bot feed too quickly
- that caused missed re-spikes / second-leg moves
- but making names sticky for the whole day also kept too much bad chop alive

Central state model implemented:

- `active`
  - feed on
  - entries allowed
- `cooldown`
  - feed on
  - entries blocked
- `resume_probe`
  - feed on
  - entries still blocked
  - waiting for stronger reclaim / expansion proof
- `dropped`
  - feed off
  - entries blocked

Important architectural note:

- this is a central strategy-engine solution for the scanner-fed bar bots
- `runner` still uses its own candidate system
- scanner output still determines initial promotion
- retention now determines how long a symbol stays on the live bot feed

Current first-cut retention rules:

- `active -> cooldown`
  - sustained structure weakness:
    - below `VWAP` and `EMA20`
  - no meaningful activity for the configured duration
  - weak rolling `5m` volume vs active baseline
  - compressed rolling `5m` range
- `cooldown -> resume_probe`
  - reclaim of structure with expansion
  - stronger `5m` volume and range
- `resume_probe -> active`
  - reclaim holds for enough bars
  - expansion still present
- `cooldown -> dropped`
  - prolonged dead tape
  - very weak rolling volume
  - compressed range
- extra after-hours fallback:
  - when `VWAP` is gone and the symbol flattens around `EMA20` on thin tape,
    the policy can still cool/drop it late

Current config knobs added:

- `MAI_TAI_SCANNER_FEED_RETENTION_ENABLED`
- `MAI_TAI_SCANNER_FEED_RETENTION_STRUCTURE_BARS`
- `MAI_TAI_SCANNER_FEED_RETENTION_NO_ACTIVITY_MINUTES`
- `MAI_TAI_SCANNER_FEED_RETENTION_COOLDOWN_VOLUME_RATIO`
- `MAI_TAI_SCANNER_FEED_RETENTION_COOLDOWN_MAX_5M_RANGE_PCT`
- `MAI_TAI_SCANNER_FEED_RETENTION_RESUME_HOLD_BARS`
- `MAI_TAI_SCANNER_FEED_RETENTION_RESUME_MIN_5M_RANGE_PCT`
- `MAI_TAI_SCANNER_FEED_RETENTION_RESUME_MIN_5M_VOLUME_RATIO`
- `MAI_TAI_SCANNER_FEED_RETENTION_RESUME_MIN_5M_VOLUME_ABS`
- `MAI_TAI_SCANNER_FEED_RETENTION_DROP_COOLDOWN_MINUTES`
- `MAI_TAI_SCANNER_FEED_RETENTION_DROP_MAX_5M_RANGE_PCT`
- `MAI_TAI_SCANNER_FEED_RETENTION_DROP_MAX_5M_VOLUME_ABS`

Targeted tests added / updated:

- [test_feed_retention.py](../tests/unit/test_feed_retention.py)
- [test_strategy_engine_service.py](../tests/unit/test_strategy_engine_service.py)

Targeted local validation:

- `tests/unit/test_feed_retention.py` -> `3 passed`
- targeted retention strategy-engine tests -> `2 passed`
- broader nearby strategy-engine slice -> `5 passed`
- compile check on touched files -> passed

## EFOI Retention Result

User-supplied files used:

- `NASDAQ_EFOI, 30S_c2c5b.csv`
- `Multi-Path_Momentum_Scalp_v1.0_NASDAQ_EFOI_2026-04-19_d6a25.csv`

Outcome with the current first-cut central policy:

- allowed trades:
  - `19`
  - net `+$7.88`
- blocked trades:
  - `22`
  - net `-$5.24`

State transitions on the EFOI day:

- `09:00:30` -> `active`
- `13:12:30` -> `cooldown`
- `17:37:30` -> `dropped`

Interpretation:

- the current policy clearly improves the bad midday churn cluster
- it blocks more losing value than winning value
- but it is still conservative on some late-day reactivation cases
- this means:
  - the base architecture is good
  - the next tuning target is smarter `resume` behavior, not removal of the
    central model

## Cross-Symbol Retention Validation

The following user-supplied chart exports were checked with the same central
policy:

- `NASDAQ_COCP, 30S_8850f.csv`
- `NASDAQ_SKYQ, 30S_b4b0b.csv`
- `NASDAQ_ZNTL, 30S_eb4c1.csv`
- `NASDAQ_FUSE, 30S_78353.csv`
- `NASDAQ_MYSE, 30S_4a241.csv`
- `NASDAQ_BDRX, 30S_d082d.csv`
- `NASDAQ_TURB, 30S_fb302.csv`
- `NASDAQ_ELAB, 30S_857b6.csv`

Observed behavior summary:

- `COCP`
  - `active -> cooldown -> dropped`
  - looked reasonable
- `MYSE`
  - `active -> cooldown -> resume_probe -> active -> cooldown -> dropped`
  - strongest proof that multiple same-day cycles work
- `ELAB`
  - `active -> cooldown -> dropped`
  - looked reasonable
- `SKYQ`
  - stayed `active` most of the day
  - cooled/dropped late
- `ZNTL`
  - stayed `active` most of the day
  - cooled/dropped late
- `FUSE`
  - stayed `active` most of the day
  - cooled late
  - still slightly sticky but better than before
- `BDRX`
  - stayed `active` in the captured session
  - no obvious dead-tape window in the file
- `TURB`
  - stayed `active` in the captured session
  - file ended before a real fade/dead window

Cross-symbol conclusion:

- the central retention layer generalizes reasonably well across:
  - clean fades
  - multi-cycle names
  - still-strong names
  - late thin-tape after-hours cases
- the main remaining improvement area is still:
  - better `resume` timing / quality
  - especially for `EFOI`-style late resumptions

Recommended final direction:

- keep this as the central architecture
- keep feed-retention separate from scanner promotion
- allow multiple same-day `cooldown / resume / cooldown / drop` cycles
- do not return to immediate score-drop removal
- next tuning pass should focus on:
  - stronger resume weighting for high-quality `P4`
  - stronger resume weighting for strong `P3`
  - without reopening the midday churn windows the current policy now blocks

Operational next step:

- do not tune resume logic immediately
- run the current central retention policy live for a few trading days first
- review:
  - names that were cooled too early
  - names that should have resumed but did not
  - names where cooldown correctly blocked churn
- only after that short live observation window should the next pass begin on
  smarter `resume` behavior

Important data-capture note:

- live Schwab tick capture is enabled for the Schwab-backed runtime path
- raw tick/quote events are currently archived to file storage, not the SQL
  database
- archive path on VPS:
  - `/var/lib/project-mai-tai/schwab_ticks/YYYY-MM-DD/SYMBOL.jsonl`
- this is sufficient for later replay/simulation
- if long-term queryable analytics are needed later, a future step could copy
  or summarize that archive into the database, but that is not the current
  storage model

## Schwab Mid-Day Restart Warmup Reseed

The Schwab-backed runtimes now reseed recent bar history on service startup.

What changed:

- `macd_30s` and `tos` already persisted completed bars into
  `StrategyBarHistory`
- startup restore previously brought back positions and pending orders, but did
  not reload recent bars into the live Schwab runtimes
- startup now reloads the current session's persisted bars for active
  Schwab-backed symbols and reseeds the runtime bar builders before live ticks
  resume

Practical effect:

- if the service starts at `4:00 AM ET` and stays up, both Schwab bots still
  warm up naturally before trading
- if the service restarts in the middle of the day, `macd_30s` and `tos` no
  longer need to wait through a fresh full bar warmup window
- they come back with enough restored bars to calculate indicators immediately,
  and can resume normal completed-bar evaluation on the next closed bar

Important boundary:

- open positions and pending orders were already restored from DB/broker sync
- this change closes the separate gap where Schwab runtime bar history was not
  being reseeded after restart

Validation:

- focused restart reseed tests now pass in
  [test_strategy_engine_service.py](C:/Users/kkvkr/OneDrive/Documents/GitHub/project-mai-tai/tests/unit/test_strategy_engine_service.py)
- compile checks passed for
  [strategy_engine_app.py](C:/Users/kkvkr/OneDrive/Documents/GitHub/project-mai-tai/src/project_mai_tai/services/strategy_engine_app.py)

## 2026-04-22 Stabilization Handoff

Current operational status:

- live VPS was intentionally left alone during the final Git cleanup pass
- active trading/runtime fixes were already deployed earlier in the session
- later work focused on:
  - control-plane trust/performance
  - strategy/runtime state publication
  - Git branch cleanup and sync

Live/operator-trust state reached during the session:

- `macd_30s` bot page was brought back to a trustworthy state with:
  - `Listening Status`
  - fresh `Decision Tape`
  - `Last Bot Tick`
  - `bar_counts`
- `/bot/30s` and `/api/bots` were optimized and became fast enough for
  real-time use
- `/health` was decoupled from the heavy overview path
- `/api/overview` still has a cold-start cost, but warm refreshes are fast

Key logic/runtime fixes completed:

- session-scoped Decision Tape fallback
- watchlist restore after restart for `macd_30s`
- 30s history hydration / warmup restore path
- generic market-data fallback activation for Schwab-native runtime
- mixed-version VPS drift cleanup during live incident
- `bar_counts` and `last_tick_at` publication
- reduced Schwab reconnect log noise
- reduced 60s bar-builder log spam

Retention/degraded state:

- degraded mode disabled
- feed retention disabled for current live behavior
- empty `Feed States` panel is therefore expected while retention is off

Git / branch status:

- do not merge the large backup PR directly:
  - [PR #10](https://github.com/krshk30/project-mai-tai/pull/10)
  - this remains a backup snapshot only
- new minimal branch created from `main` and validated:
  - `codex/2026-04-22-minimal-stabilization`
- new draft PR for the smaller merge path:
  - [PR #11](https://github.com/krshk30/project-mai-tai/pull/11)

Minimal branch validation completed:

- `ruff check src tests`
- deterministic per-test validation for:
  - [test_time_utils.py](C:/Users/kkvkr/OneDrive/Documents/GitHub/project-mai-tai/tests/unit/test_time_utils.py)
  - [test_control_plane.py](C:/Users/kkvkr/OneDrive/Documents/GitHub/project-mai-tai/tests/unit/test_control_plane.py)
  - [test_strategy_engine_service.py](C:/Users/kkvkr/OneDrive/Documents/GitHub/project-mai-tai/tests/unit/test_strategy_engine_service.py)

Files included in the minimal stabilization branch:

- [events.py](C:/Users/kkvkr/OneDrive/Documents/GitHub/project-mai-tai/src/project_mai_tai/events.py)
- [schwab_streamer.py](C:/Users/kkvkr/OneDrive/Documents/GitHub/project-mai-tai/src/project_mai_tai/market_data/schwab_streamer.py)
- [control_plane.py](C:/Users/kkvkr/OneDrive/Documents/GitHub/project-mai-tai/src/project_mai_tai/services/control_plane.py)
- [strategy_engine_app.py](C:/Users/kkvkr/OneDrive/Documents/GitHub/project-mai-tai/src/project_mai_tai/services/strategy_engine_app.py)
- [settings.py](C:/Users/kkvkr/OneDrive/Documents/GitHub/project-mai-tai/src/project_mai_tai/settings.py)
- selected `strategy_core/*` dependencies required for the stabilized runtime
- matching unit tests

Recommended next step in a new chat:

- continue from [PR #11](https://github.com/krshk30/project-mai-tai/pull/11)
- review the minimal branch instead of the large backup branch
- keep VPS untouched unless a fresh critical live bug appears

## 2026-04-22 Final State After Manual-Stop Runtime Safety Merge

Git / deploy status:

- the final manual-stop runtime safety fix was merged to `main` in:
  - commit `e64f86228b32550e61f7eaae3989368f5a3e5c91`
- local `main`, GitHub `main`, and VPS `HEAD` were verified aligned to that same SHA
- PR status:
  - [PR #12](https://github.com/krshk30/project-mai-tai/pull/12) merged
  - [PR #11](https://github.com/krshk30/project-mai-tai/pull/11) merged earlier
  - [PR #10](https://github.com/krshk30/project-mai-tai/pull/10) remains closed as backup snapshot only

What was proved live:

- the user was correct: `AGPU` really did open a fresh post-stop trade
- it was not just a stale label or old open position
- direct DB evidence showed:
  - final stop around `2026-04-22 18:47:24 UTC`
  - fresh `AGPU` open intent/order around `18:49:34 UTC`
  - path/reason was `ENTRY_P3_SURGE`

Actual root causes found:

- manual stops were not preloaded early enough after strategy restarts
- stopped symbols could be reintroduced into the `macd_30s` watchlist during restore/reseed
- a separate restart bug was also present:
  - `_monitor_schwab_symbol_health()` called `fetch_quotes()` on `SchwabBrokerAdapter`
  - `SchwabBrokerAdapter` did not implement `fetch_quotes`
  - this could restart the strategy service and make stop behavior feel inconsistent

Code merged in PR #12:

- [schwab.py](C:/Users/kkvkr/OneDrive/Documents/GitHub/project-mai-tai/src/project_mai_tai/broker_adapters/schwab.py)
  - added `SchwabBrokerAdapter.fetch_quotes(...)`
- [strategy_engine_app.py](C:/Users/kkvkr/OneDrive/Documents/GitHub/project-mai-tai/src/project_mai_tai/services/strategy_engine_app.py)
  - preload manual stops at startup before post-restart trading resumes
  - filter manual-stopped symbols out of restored watchlists
  - apply manual stops before watchlist restore in `restore_confirmed_runtime_view(...)`
  - guard stale-symbol quote polling so missing `fetch_quotes` no longer crashes the strategy loop
- [test_strategy_engine_service.py](C:/Users/kkvkr/OneDrive/Documents/GitHub/project-mai-tai/tests/unit/test_strategy_engine_service.py)
  - regression tests added for:
    - manual-stop restore safety
    - manual-stop preload before post-restart trading
    - missing-`fetch_quotes` stale-poll safety

Validation completed:

- targeted `pytest` slice for the stop/restart/quote-poll tests passed locally
- `ruff` passed on the changed files
- after VPS update to `origin/main`, live `/api/bots` showed:
  - `macd_30s.watchlist = []`
  - `manual_stop_symbols = ["AGPU", "AKAN", "ELPW", "GP", "TORO", "WBUY"]`
  - `positions = []`

Interpretation of current live bot state:

- paused names are no longer in the live `macd_30s` watchlist
- empty `Feed States` remains expected because feed retention is disabled
- if a stopped symbol appears on screen again, distinguish:
  - real open/pending position visibility
  - versus watchlist/live-symbol rendering bug
- as of the final verification in this session, the backend state was correct

GitHub / workflow note:

- code sync is clean:
  - local `main` == GitHub `main` == VPS `HEAD` at `e64f862`
- GitHub still showed failing `validate` / red `X` workflow notifications around merge time
- this is a CI/workflow cleanliness issue, not a code-sync issue

Local-only changes intentionally left out:

- [active-market-verification-todo.md](C:/Users/kkvkr/OneDrive/Documents/GitHub/project-mai-tai/docs/active-market-verification-todo.md)
- [live-market-restart-runbook.md](C:/Users/kkvkr/OneDrive/Documents/GitHub/project-mai-tai/docs/live-market-restart-runbook.md)
- local `data/history/*.csv`

Recommended starting point for next chat:

- read this handoff file first
- assume the live manual-stop runtime fix is already merged and deployed
- assume local/GitHub/VPS code are synced at `e64f862`
- if anything still looks wrong on screen, debug it as either:
  - UI freshness / rendering
  - or a brand-new live runtime bug

## 2026-04-22 Schwab Native 30s Confirmation Toggle

Scope of this change:

- scanner focus remains the same:
  - live focus is still `macd_30s`
  - live broker path is still Schwab-native
  - other bots should remain disabled in the live env unless explicitly re-enabled later

Config change requested in this session:

- file changed:
  - [trading_config.py](C:/Users/kkvkr/OneDrive/Documents/GitHub/project-mai-tai/src/project_mai_tai/strategy_core/trading_config.py)
- in `make_30s_schwab_native_variant(...)`:
  - `schwab_native_use_confirmation` flipped from `False` to `True`
  - `entry_intrabar_enabled` flipped from `True` to `False`

Intent of the change:

- require confirmation on the Schwab-native `macd_30s` path
- disable intrabar entry handling instead of trying to carve out only selected paths such as `P4` / `P5`

Local validation completed:

- direct config smoke check confirmed:
  - `entry_intrabar_enabled = False`
  - `schwab_native_use_confirmation = True`

Deployment note for this session:

- requested live action is a strategy-service restart only after the updated `main` is pushed and deployed to the VPS

Live deploy follow-up completed:

- commit `ee8cbc621236b815939d0b0dfa0337be0612a805` was pushed to GitHub `main`
- VPS repo was fast-forwarded to the same SHA on `main`
- `project-mai-tai-strategy.service` was restarted on the VPS at:
  - `2026-04-22 19:42:21 UTC`
  - `2026-04-22 03:42:21 PM ET`

Post-restart live verification:

- strategy heartbeat returned after the restart
- `macd_30s` bot API showed:
  - `watchlist = []`
  - `manual_stop_symbols = ["AGPU", "AKAN", "ELPW", "GP", "TORO", "WBUY"]`
  - `position_count = 0`
  - `pending_count = 0`
  - `wiring_status = "live/schwab"`
- strategy log showed the new startup and resumed Schwab stream connectivity

Important operator note about live restart preflight:

- the live deploy preflight still blocks on:
  - raw `open_account_positions` count
  - reconciliation summary totals from the latest run
- but the VPS env explicitly contains an ignored position-mismatch exception list:
  - `MAI_TAI_RECONCILIATION_IGNORED_POSITION_MISMATCHES=paper:macd_30s:CYN,CANF;paper:tos_runner_shared:CYN,CANF`
- current practical meaning:
  - `CYN` and `CANF` are known exception symbols
  - UI/detail views hide those reconciliation findings correctly
  - the deploy preflight script does **not** currently honor that exception list and can over-block risky-service restarts even when the only blockers are those known exception names

Current live interpretation after this restart:

- the requested Schwab-native `30s` config change is deployed
- strategy is running from synced `main`
- control plane / overview can still read as `degraded` because of the exception-driven reconciliation summary, even when the detailed visible findings list is empty

## 2026-04-22 Schwab Native 30s Chop Regime Lock

Scope of this change:

- change is limited to the Schwab-native `macd_30s` entry engine
- goal is to stop `P1` / `P2` in choppy tape, stop `P3` unless momentum is
  truly exceptional, and leave `P4_BURST` / `P5_PULLBACK` as the exception path

Files changed in this session:

- [trading_config.py](C:/Users/kkvkr/OneDrive/Documents/GitHub/project-mai-tai/src/project_mai_tai/strategy_core/trading_config.py)
  - added explicit chop-regime and `P3` extreme-override config knobs
  - enabled `schwab_native_use_chop_regime = True` in
    `make_30s_schwab_native_variant(...)`
- [schwab_native_30s.py](C:/Users/kkvkr/OneDrive/Documents/GitHub/project-mai-tai/src/project_mai_tai/strategy_core/schwab_native_30s.py)
  - added a per-symbol chop lock for the Schwab-native `30s` engine
  - chop lock turns on when at least `2` of these `4` conditions hit:
    - `EMA20` / `VWAP` compression versus ATR
    - `EMA20` flatness
    - `EMA20` / `VWAP` whipsaw crosses
    - no clean side in recent closes
  - `P1_CROSS` and `P2_VWAP` are blocked while the lock is active
  - `P3_SURGE` is blocked while the lock is active unless the extreme-momentum
    override passes
  - `P4_BURST` and `P5_PULLBACK` remain exempt
  - Decision Tape reasons now include the current chop hit count and flags, for
    example:
    - `chop lock active (current 4/4): COMPRESS|EMA20_FLAT|WHIPSAW|NO_CLEAN_SIDE; P1/P2/P3 gated`
- [test_strategy_core.py](C:/Users/kkvkr/OneDrive/Documents/GitHub/project-mai-tai/tests/unit/test_strategy_core.py)
  - added targeted coverage for:
    - `P1` blocked by the chop lock with debug reason text
    - `P3` allowed through the chop lock only when the extreme override passes

Validation completed in this session:

- `compileall` passed for the changed files
- direct bundled-Python strategy-engine harness checks passed for:
  - chop lock blocks `P1` with a `4/4` Decision Tape reason
  - `P3_SURGE` still fires when the extreme override passes during chop lock
  - `P4_BURST` still fires
  - `P5_PULLBACK` still fires
- note:
  - `pytest` is not installed in the current local shell/runtime, so validation
    was done with direct Python harness execution instead of a normal `pytest`
    run

Deployment state:

- local `main` and GitHub `main` now include commit
  `666f7b4c0bd6cf6d52006bc0f3be647d8ddd5b66`
- this change has **not** been deployed to the VPS
- no service restart was performed in this session

## 2026-04-22 Manual Stop Resume Watchlist Resync

New runtime bug found after the earlier manual-stop restart fix:

- on the live `macd_30s` bot, pressing `Resume` on a bot-level manual stop removed
  the symbol from the `Manual Stops` list, but did **not** put it back into the
  bot watchlist immediately
- this made the UI look broken:
  - symbol vanished from `Manual Stops`
  - symbol still did not appear under `Live Symbols`
  - `Tracked Symbols` / watchlist counts could remain at `0`
- important distinction:
  - the earlier restart/restore bug was already fixed
  - this was a separate live-update bug in the manual-stop event path

Root cause:

- [strategy_engine_app.py](C:/Users/kkvkr/OneDrive/Documents/GitHub/project-mai-tai/src/project_mai_tai/services/strategy_engine_app.py)
  handled live `manual_stop_update` resume events by only updating the bot's
  `manual_stop_symbols`
- when feed retention is disabled, `set_manual_stop_symbols(...)` removes a
  stopped name from the live watchlist, but a later `resume` did not rebuild the
  watchlist from `current_confirmed`
- result:
  - stop removed the symbol immediately
  - resume cleared the stop flag
  - but the symbol stayed absent until some later scanner/watchlist rebuild

Fix implemented:

- [strategy_engine_app.py](C:/Users/kkvkr/OneDrive/Documents/GitHub/project-mai-tai/src/project_mai_tai/services/strategy_engine_app.py)
  - added `_resync_bot_watchlists_from_current_confirmed(...)`
  - live bot/global manual-stop updates now immediately rebuild bot watchlists
    from the current confirmed scanner set after the stop/resume change
  - `restore_confirmed_runtime_view(...)` now uses the same helper so the logic
    stays consistent
- [test_strategy_engine_service.py](C:/Users/kkvkr/OneDrive/Documents/GitHub/project-mai-tai/tests/unit/test_strategy_engine_service.py)
  - added a regression test proving:
    - `stop` removes the symbol from the live watchlist
    - `resume` re-adds it immediately

Local validation completed:

- targeted `pytest` slice passed locally in the repo `.venv`:
  - `test_manual_stop_update_removes_symbol_from_live_watchlist_immediately`
  - `test_manual_stop_resume_readds_symbol_to_live_watchlist_immediately`
- direct runtime harness also confirmed:
  - initial watchlist: `['AGPU', 'WBUY']`
  - after stop: `['WBUY']`
  - after resume: `['AGPU', 'WBUY']`

Deployment state:

- code is fixed locally but deployment status must be checked against the latest
  commit / VPS state before assuming the live service has this resume-resync fix

## 2026-04-22 Scanner-To-Bot Handoff Backfill For Manual-Stopped Top Slots

Critical live issue found while investigating `GNLN`:

- `GNLN` was confirmed in the scanner and remained in the confirmed universe,
  but it did not reliably appear in the live `macd_30s` bot
- at times it showed up in the `30s` watchlist and then disappeared again
- this created the exact operator-facing symptom:
  - scanner shows a strong confirmed name
  - `30s` briefly gets it
  - then `30s` loses it even though the symbol is still confirmed

Root cause:

- bot handoff was built from one shared scanner `top_confirmed` list first
- only after that shared list was chosen did each bot apply its own manual-stop
  filter
- this meant manually stopped names could still consume shared top slots even
  though `macd_30s` was not allowed to trade them
- practical example observed live:
  - shared top slots could include `ELPW`, `TORO`, or `WBUY`
  - those names were manually stopped for `macd_30s`
  - `macd_30s` ended up with only `AGPU` / `AKAN`
  - `GNLN` could be the next eligible confirmed name but was still squeezed out
- this also amplified rank churn:
  - the fifth shared slot flipped between names like `GNLN`, `WBUY`, and `GP`
  - when `GNLN` briefly won the slot it appeared in `30s`
  - when it lost the slot, it disappeared again

Fix implemented:

- [strategy_engine_app.py](C:/Users/kkvkr/OneDrive/Documents/GitHub/project-mai-tai/src/project_mai_tai/services/strategy_engine_app.py)
  - bot watchlists now backfill from the ranked confirmed universe **after**
    each bot's own manual-stop filter
  - manually stopped symbols no longer waste live handoff slots for that bot
  - `current_confirmed` / scanner top-confirmed UI remains the shared ranked view
  - but each bot now receives the next eligible confirmed names instead of a
    half-empty watchlist
- [test_strategy_engine_service.py](C:/Users/kkvkr/OneDrive/Documents/GitHub/project-mai-tai/tests/unit/test_strategy_engine_service.py)
  - added regression coverage proving that when paused names occupy shared top
    slots, `macd_30s` backfills with the next ranked eligible symbol

Local validation completed:

- `python -m compileall src/project_mai_tai/services/strategy_engine_app.py tests/unit/test_strategy_engine_service.py`
- targeted `pytest` slice passed locally in the repo `.venv`:
  - `test_manual_stop_update_removes_symbol_from_live_watchlist_immediately`
  - `test_manual_stop_resume_readds_symbol_to_live_watchlist_immediately`
  - `test_bot_watchlist_backfills_next_ranked_symbol_after_manual_stop_filter`

Deployment state:

- local `main`, GitHub `main`, and the VPS checkout were synced to commit
  `f45d98622c46c58f4366f1475fa907e6ca928feb`
- because `systemctl restart` from the `trader` shell required interactive
  authentication, the strategy process was recycled by sending `TERM` to the
  running `mai-tai-strategy` process and letting systemd restart it under
  `Restart=always`
- new live strategy PID / start time after deploy:
  - PID `456872`
  - `2026-04-22 20:43:24 UTC`
  - `2026-04-22 04:43:24 PM ET`
- post-restart heartbeat returned healthy

Post-deploy live note:

- after the restart, the live scanner state no longer contained `GNLN`
  (`strategy-state` latest payload had `all_has_gnln = false`)
- because of that, live verification after the restart could only confirm:
  - new code is deployed and running
  - `macd_30s` is healthy on the new commit
  - direct live validation against `GNLN` was no longer possible in the
    restarted state
- the root-cause fix remains:
  - paused symbols no longer consume per-bot handoff slots
  - when a symbol like `GNLN` is in the ranked confirmed universe, `macd_30s`
    should now backfill it instead of staying half-empty behind paused names

## 2026-04-22 Remove Rank-Score Gating From Scanner-To-Bot Handoff

Behavior change requested and implemented:

- confirmed momentum names should be handed off to the bot immediately
- bot-side logic should decide whether to trade
- scanner rank score should no longer gate bot handoff
- manual stop / resume stays bot-side and global scanner stop still removes a
  symbol from handoff everywhere

What was still wrong before this change:

- the earlier `GNLN` fix only made bot watchlists backfill better after
  bot-specific manual-stop filtering
- handoff was still built from a ranked confirmed list
- that meant a name could be fully confirmed in the momentum scanner but still
  wait behind rank-score filtering before reaching `macd_30s`
- this was not the intended operating model for the current live setup where
  `macd_30s` is the active bot and should police entries itself

Fix implemented:

- [strategy_engine_app.py](C:/Users/kkvkr/OneDrive/Documents/GitHub/project-mai-tai/src/project_mai_tai/services/strategy_engine_app.py)
  - live snapshot processing now hands bot watchlists from `all_confirmed`
    instead of the ranked confirmed handoff list
  - `current_confirmed` remains the visible scanner subset (`all_confirmed[:5]`)
    for dashboard display, but it no longer controls whether a confirmed symbol
    reaches the bot
  - manual-stop resync now rebuilds watchlists from the unranked confirmed set
  - restart/restore seeding now preserves the full confirmed universe for bot
    handoff instead of collapsing back down to the visible top list
- [test_strategy_engine_service.py](C:/Users/kkvkr/OneDrive/Documents/GitHub/project-mai-tai/tests/unit/test_strategy_engine_service.py)
  - updated regression coverage to prove confirmed symbols are handed to bots
    without rank-threshold gating
  - updated manual-stop backfill coverage to prove the next confirmed symbol is
    pulled in after bot-side stop filtering

New canonical model after this change:

- momentum alert fires -> symbol becomes confirmed
- confirmed symbol enters `all_confirmed`
- confirmed symbol is handed to bot watchlists immediately unless blocked by:
  - global scanner manual stop
  - bot-specific manual stop
  - bot-specific exclusions like reclaim exclusions
- trade/no-trade is then decided by the bot strategy itself

Local validation completed:

- `python -m compileall src/project_mai_tai/services/strategy_engine_app.py tests/unit/test_strategy_engine_service.py`
- targeted repo `.venv` pytest slice passed:
  - `test_snapshot_batch_hands_confirmed_symbols_to_bots_without_rank_threshold`
  - `test_bot_watchlist_backfills_next_confirmed_symbol_after_manual_stop_filter`
  - `test_manual_stop_update_removes_symbol_from_live_watchlist_immediately`
  - `test_manual_stop_resume_readds_symbol_to_live_watchlist_immediately`

Deployment state:

- code changed locally on `main`
- deploy / VPS restart status must be checked separately before assuming live
  behavior matches this new handoff model
