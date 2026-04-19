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
