# Session Handoff - Global

## 2026-04-27 Trade Coach Review Center (Control-Plane Phase)

Current state:

- local `main` now includes a dedicated aggregated trade-coach review surface
- this phase is control-plane only:
  - no strategy-engine changes
  - no OMS changes
  - no trade-coach prompt/schema changes
- purpose of this phase:
  - make it easier to review coach output across trades without opening raw JSON
  - give an operator-facing place to filter by bot, verdict, focus, and symbol

What was added:

- new aggregated coach API endpoint:
  - `/api/coach-reviews`
- new aggregated coach HTML page:
  - `/coach/reviews`
- new control-plane navigation link:
  - `Trade Coach`
- new review-center filters:
  - `strategy_code`
  - `verdict`
  - `coaching_focus`
  - `symbol`
- aggregated review-center summary counts:
  - visible reviews
  - `good`
  - `mixed`
  - `bad`
  - `manual_review`
  - `should_skip`

Implementation notes:

- the new page reuses the existing persisted `recent_trade_coach_reviews` feed
- no new DB tables or migrations were needed
- review rows are enriched with bot display context from the existing bot views:
  - `display_name`
  - `account_display_name`
- per-bot pages still keep their local `Trade Coach Reviews` table
- the new review center is the cross-trade / cross-bot scan surface

Validation completed:

- passed:
  - `.venv\Scripts\python.exe -m pytest tests/unit/test_control_plane.py -q`
  - `.venv\Scripts\python.exe -m py_compile src/project_mai_tai/services/control_plane.py`
- focused control-plane suite result for this phase:
  - `28 passed`

### 2026-04-27 Trade Coach Operator Workflow (Queue + Drilldown)

Current state:

- local `main` now extends the review center with an operator workflow layer
- still control-plane only:
  - no strategy-engine changes
  - no OMS changes
  - no coach prompt/schema changes
- goal of this phase:
  - make the coach actionable after scan-level review
  - let an operator decide which trade to inspect next

What was added:

- aggregated coach API now also returns:
  - `review_queue`
- new single-review API endpoint:
  - `/api/coach-review?cycle_key=...`
- new single-review HTML page:
  - `/coach/review?cycle_key=...`
- new review-center features:
  - `Priority Review Queue`
  - `Open review` links from aggregated review rows
  - full single-trade drilldown page

Priority queue rules:

- queue score increases when:
  - coach verdict is `bad`
  - coach verdict is `mixed`
  - `should_review_manually = true`
  - `should_have_traded = false`
  - quality scores are weak
  - rule violations exist
  - trade closed red
- queue labels:
  - `high`
  - `medium`
  - `low`

Review detail page includes:

- trade facts:
  - path
  - entry/exit times
  - entry/exit prices
  - P&L and P&L %
  - exit summary
  - cycle key
- coach breakdown:
  - verdict
  - action
  - focus
  - confidence
  - priority reasons
  - key reasons
  - rule hits
  - rule violations
  - next-time notes
  - quality scores

Validation completed:

- passed:
  - `.venv\Scripts\python.exe -m pytest tests/unit/test_control_plane.py -q`
  - `.venv\Scripts\python.exe -m py_compile src/project_mai_tai/services/control_plane.py tests/unit/test_control_plane.py`
- focused control-plane suite result for this phase:
  - `28 passed`

### 2026-04-27 Trade Coach Pattern Memory (Review Context Phase)

Current state:

- local `main` now adds a first pattern-memory layer on top of the review drilldown
- still control-plane only:
  - no strategy-engine changes
  - no OMS changes
  - no trade-coach prompt/schema changes
- goal of this phase:
  - start connecting reviews to prior similar reviewed trades
  - move the coach closer to “we have seen this kind of setup before”

What was added:

- single-review API now also returns:
  - `same_path_summary`
  - `same_symbol_summary`
  - `recent_same_path_reviews`
  - `recent_same_symbol_reviews`
- single-review drilldown page now includes:
  - `Pattern Memory`
  - same-path count, verdict mix, and average P&L %
  - same-symbol count, verdict mix, and average P&L %
  - recent same-path review links
  - recent same-symbol review links

Intent of this phase:

- this is the first UI layer that starts to answer:
  - “how have similar reviewed path setups behaved lately?”
  - “how has this symbol behaved lately under reviewed trades?”
- it is still descriptive, not predictive
- it does not block live trading or alter order flow

Validation completed:

- passed:
  - `.venv\Scripts\python.exe -m pytest tests/unit/test_control_plane.py -q`
  - `.venv\Scripts\python.exe -m py_compile src/project_mai_tai/services/control_plane.py tests/unit/test_control_plane.py`
- focused control-plane suite result for this phase:
  - `28 passed`

## 2026-04-24 Trade Coach Foundation (Merged To Main, Deployed Disabled)

Merged PR:

- `#52`
- [Add trade coach foundation service](https://github.com/krshk30/project-mai-tai/pull/52)
- merged into `main` as `93fa397` on `2026-04-27`

Important state:

- this work is now merged to `main`
- deployed to the VPS from `main` on `2026-04-26`
- local and GitHub `main` now include the follow-up handoff update commit
  `8ccfa59`
- VPS trade coach code deployment is on `1ec069d`
- production remains disabled by default
- VPS trade coach secret is configured outside the repo
- VPS trade coach flags remain disabled:
  - `MAI_TAI_TRADE_COACH_ENABLED=false`
  - `MAI_TAI_TRADE_COACH_SHADOW_ENABLED=false`
  - `MAI_TAI_TRADE_COACH_PROMOTE_ENABLED=false`
- repo now includes a dedicated `project-mai-tai-trade-coach.service`
  for manual advisory-only runs
- that service now forces `MAI_TAI_TRADE_COACH_ENABLED=true` only for its own
  process start while leaving the shared VPS env file disabled by default
- current scope is the first trade-coach foundation pass for the two 30-second
  bots only:
  - `macd_30s`
  - `webull_30s`

What was added:

- detailed implementation checklist document:
  - [trade-coach-implementation-plan.md](C:/Users/kkvkr/OneDrive/Documents/GitHub/project-mai-tai/docs/trade-coach-implementation-plan.md)
- live test runbook for first VPS validation:
  - [trade-coach-live-test-runbook.md](C:/Users/kkvkr/OneDrive/Documents/GitHub/project-mai-tai/docs/trade-coach-live-test-runbook.md)
- shared completed-trade reconstruction module:
  - [trade_episodes.py](C:/Users/kkvkr/OneDrive/Documents/GitHub/project-mai-tai/src/project_mai_tai/trade_episodes.py)
- control-plane completed-position rendering now reuses that shared
  fill-first/filled-order-fallback cycle reconstruction instead of carrying a
  separate inline copy
- trade coach package scaffold:
  - [models.py](C:/Users/kkvkr/OneDrive/Documents/GitHub/project-mai-tai/src/project_mai_tai/ai_trade_coach/models.py)
  - [repository.py](C:/Users/kkvkr/OneDrive/Documents/GitHub/project-mai-tai/src/project_mai_tai/ai_trade_coach/repository.py)
  - [service.py](C:/Users/kkvkr/OneDrive/Documents/GitHub/project-mai-tai/src/project_mai_tai/ai_trade_coach/service.py)
- new AI review persistence model and migration:
  - `ai_trade_reviews`
  - [20260424_0004_ai_trade_reviews.py](C:/Users/kkvkr/OneDrive/Documents/GitHub/project-mai-tai/sql/migrations/versions/20260424_0004_ai_trade_reviews.py)
- trade coach service wiring:
  - [trade_coach_app.py](C:/Users/kkvkr/OneDrive/Documents/GitHub/project-mai-tai/src/project_mai_tai/services/trade_coach_app.py)
  - [trade_coach.py](C:/Users/kkvkr/OneDrive/Documents/GitHub/project-mai-tai/src/project_mai_tai/services/trade_coach.py)
  - [services/trade-coach/main.py](C:/Users/kkvkr/OneDrive/Documents/GitHub/project-mai-tai/services/trade-coach/main.py)
  - new console script:
    - `mai-tai-trade-coach`
- settings added under the existing AI config pattern:
  - `trade_coach_*`
- control-plane data load now includes recent persisted trade coach reviews and
  per-bot review slices in `/api/bots`
- trade coach review selection now sorts globally across both configured
  strategy/account pairs before applying the review limit
- trade coach Responses client now explicitly forces the
  `submit_trade_review` function path and keeps strict structured parsing
- trade coach client now also normalizes common off-schema model outputs before
  final validation:
  - `0-10` score responses are converted to `0.0-1.0`
  - free-text verdict/action/timing labels are mapped onto the allowed enums

Intentional design choices from this pass:

- do **not** rebuild flat-to-flat trade pairing separately inside the AI coach
- keep trade-coach review cycles keyed by:
  - `strategy_code`
  - `broker_account_name`
  - `symbol`
  - flat-to-flat cycle key
- keep the first version post-trade only
- do **not** place any AI network call inline inside:
  - `strategy_engine_app.py`
  - `oms/service.py`
- use the OpenAI Responses API path in the coach client instead of the older
  Chat Completions style used by the earlier catalyst helper

Validation completed:

- passed:
  - `.venv\Scripts\python.exe -m pytest tests/unit/test_trade_episodes.py tests/unit/test_trade_coach_service.py tests/unit/test_trade_coach_repository.py tests/unit/test_control_plane.py -q`
  - `.venv\Scripts\python.exe -m py_compile src/project_mai_tai/trade_episodes.py src/project_mai_tai/ai_trade_coach/models.py src/project_mai_tai/ai_trade_coach/repository.py src/project_mai_tai/ai_trade_coach/service.py src/project_mai_tai/services/trade_coach_app.py src/project_mai_tai/services/trade_coach.py src/project_mai_tai/services/control_plane.py src/project_mai_tai/db/models.py`
  - `.venv\Scripts\python.exe -m project_mai_tai.services.trade_coach`
  - `.venv\Scripts\python.exe -m pytest tests/unit/test_trade_coach_service.py tests/unit/test_trade_episodes.py tests/unit/test_trade_coach_repository.py -q`

Latest validation snapshot:

- targeted trade-coach/control-plane suite passed locally:
  - `32 passed`
- disabled-mode smoke pass:
  - trade coach process exited cleanly with default `trade_coach_enabled = false`
  - no API request path was exercised yet because the service remains disabled
- `2026-04-26` synthetic API smoke pass:
  - real OpenAI Responses API call succeeded through the trade coach client
  - strict function-call parsing path returned a valid structured review payload
  - test used a synthetic completed `macd_30s` episode only; no live or VPS state
    was modified
- `2026-04-26` historical trade verification for `2026-04-24`:
  - read-only VPS Postgres reconstruction confirmed real closed `macd_30s`
    trades existed for `2026-04-24`
  - distinct reconstructed `macd_30s` completed cycles: `18`
  - distinct reconstructed `webull_30s` completed cycles: `0`
  - example `macd_30s` closed names from that day included:
    - `IMA`
    - `KITT`
    - `BMNU`
    - `PZG`
    - `SKLZ`
    - `ENVB`
    - `IONZ`
    - `SST`
- `2026-04-26` one-off historical AI reviews completed successfully for real
  `macd_30s` closed trades from `2026-04-24`:
  - `BMNU`
    - verdict: `good`
    - action: `enter`
    - timing: `on_time`
    - confidence: `0.85`
    - setup_quality: `0.90`
  - `SKLZ`
    - verdict: `good`
    - action: `exit`
    - timing: `on_time`
    - confidence: `0.80`
    - setup_quality: `0.90`
  - `IMA`
    - verdict: `mixed`
    - action: `exit`
    - timing: `on_time`
    - confidence: `0.40`
    - setup_quality: `0.60`
  - these were one-off local AI reviews using read-only VPS historical episode
    extraction
  - they were **not** persisted into VPS `ai_trade_reviews` because the branch
    is not merged/deployed and the local shell still lacks a direct Postgres
    runtime for the normal service path
- local dry-run blocker on `2026-04-26`:
  - no local Postgres listener on `localhost:5432`
  - because of that, a true DB-backed closed-trade review pass could not run from
    this shell yet
- local dev secret state:
  - local development environment now has `MAI_TAI_TRADE_COACH_API_KEY`
    configured outside the repo
  - do **not** commit secrets into `.env`, repo files, or handoff notes
  - VPS / production now also has the trade coach API key configured outside
    the repo
- merge/deploy status:
  - merged to GitHub `main`
  - local `main` was fast-forwarded and then updated to `fcc62b4`
  - VPS deploy completed successfully from `main`
  - VPS migration `20260424_0004` for `ai_trade_reviews` ran successfully
  - VPS health check passed at `http://127.0.0.1:8100/health`
  - deploy also exposed and fixed two legacy env-file quoting issues in
    `/etc/project-mai-tai/project-mai-tai.env`:
    - `MAI_TAI_TRADINGVIEW_ALERTS_CONDITION_TEXT`
    - `MAI_TAI_RECONCILIATION_IGNORED_POSITION_MISMATCHES`

Known non-blocking note from local verification:

- `tests/unit/test_oms_risk_service.py` still showed pre-existing routing/runtime
  expectation failures unrelated to the trade-coach files touched here and was
  not used as a blocker for this foundation pass

What is still not done:

- no dedicated trade coach dashboard UI yet
- no live shadow advice path yet
- no OMS advisory gate yet

## 2026-04-24 Manual Stop Session Cleanup

Morning follow-up found stale bot manual stops still leaking into the current
session even after the broader live-symbol/session cleanup work. The live
smoking gun on the VPS was:

- latest `bot_manual_stop_symbols` snapshot was created on `2026-04-24
  06:53 AM ET`
- payload still contained yesterday's `macd_30s` stop list
- snapshot had **no** `scanner_session_start_utc` marker

Why it leaked:

- manual-stop restore logic was still falling back to `created_at >= session
  start` when the session marker was missing
- that meant a markerless row written after `4:00 AM ET` could be treated as a
  valid current-session stop list even if its contents were stale
- control-plane manual stop writes were also willing to merge from the latest
  snapshot without first proving it belonged to the current scanner session

Fix applied:

- manual-stop snapshots are now treated more strictly than generic scanner
  snapshots
- both control plane and strategy-engine now require a valid
  `scanner_session_start_utc` marker before trusting persisted bot/global
  manual-stop snapshots
- manual-stop write paths no longer merge with stale or markerless snapshots
- strategy startup now purges stale/markerless manual-stop snapshots before
  preloading live runtime state

Expected result:

- stale manual stops from yesterday should no longer reappear on `Schwab 30 Sec
  Bot` or `Webull 30 Sec Bot`
- tomorrow morning the old stop list should auto-clear instead of being revived
  by a fresh timestamp

## 2026-04-24 Schwab Stream Prewarm Load Mitigation

After the manual-stop cleanup, the Schwab bot still briefly flashed `DATA HALT`
in the morning. Live investigation showed:

- active Schwab 30-second watchlist was only about `5` symbols
- but the strategy heartbeat was still carrying about `43` Schwab stream
  subscriptions
- those extra subscriptions were coming from the raw-alert `schwab_prewarm`
  path, which was:
  - restored from old `recent_alerts` on restart
  - allowed to accumulate across the session without aging out

Likely effect:

- the real live names could get caught in short Schwab stream stalls even though
  only a handful were actually on the bot watchlist

Mitigation applied:

- do **not** repopulate Schwab prewarm from restored/rebuilt historical
  `recent_alerts`
- only real-time raw alerts can add fresh Schwab prewarm symbols
- Schwab prewarm symbols now expire automatically after `10` minutes unless they
  are refreshed by a new alert
- Schwab prewarm list is capped more conservatively at `12` symbols instead of
  `40`

Intent:

- keep the early warmup behavior for genuinely fresh raw alerts
- stop the Schwab stream from carrying dozens of stale prewarm-only symbols that
  are no longer relevant to the live 30-second bot

Follow-up after deploy:

- stream load dropped from about `43` Schwab subscriptions down to the actual
  live set (`4`)
- this removed the prewarm overload, but the live Schwab stream still exposed a
  second blocker:
  - `TimeoutError: timed out during opening handshake`
  - TLS connectivity to Schwab still succeeded from the VPS, so the remaining
    failure point is the websocket opening handshake itself

Additional mitigation:

- increased Schwab websocket `open_timeout` from the library default to `30`
  seconds in both the live connection loop and the probe path
- intent is to tolerate slow Schwab websocket opens instead of treating them as
  immediate stream failure

Further live finding:

- direct isolated streamer probe succeeded on the VPS and delivered live trades
  and quotes
- an isolated long-running streamer also connected and received data, but Schwab
  then closed the socket with `1000 OK`
- our client was treating that normal close like a real failure, which could
  poison health and cascade into later stale/data-halt behavior during the
  reconnect cycle

Streamer reconnect fix:

- treat `websockets.exceptions.ConnectionClosedOK` as a normal Schwab socket
  rotation, not as a hard failure
- clear `last_error` for that path
- reconnect quickly (`0.5s`) instead of waiting the full normal reconnect delay

## 2026-04-24 Schwab OAuth Callback Recovery

Morning live checks found the remaining `Schwab 30 Sec Bot` red state was not a
cleanup bug. The live blocker was:

- Schwab refresh-token auth on the VPS was failing with
  `refresh_token_authentication_error` / `unsupported_token_type`
- the public callback host `https://hook.project-mai-tai.live/auth/callback`
  was also broken because nginx still proxied `/auth/*` to the obsolete
  `tv-alerts` sidecar on port `3000`
- that sidecar no longer ships in current `main`, so the callback host returned
  `502` and prevented a clean re-consent flow

Recovery change:

- [control_plane.py](C:/Users/kkvkr/OneDrive/Documents/GitHub/project-mai-tai/src/project_mai_tai/services/control_plane.py)
  now exposes:
  - `/auth/schwab/start`
  - `/auth/callback`
- the control plane can now:
  - redirect into the Schwab authorize URL
  - exchange the returned authorization code for fresh tokens
  - persist the refreshed token store directly to the configured VPS token path

Operational fix:

- nginx `/auth/*` on `hook.project-mai-tai.live` should point to the live control
  plane instead of the dead `tv-alerts` sidecar
- after browser consent completes, restart `project-mai-tai-strategy.service`
  and verify the Schwab bot leaves `DATA HALT`

## Current Live Focus - 2026-04-23

This handoff is now superseded by the current 30-second live-trading work from
`2026-04-23`.

Current operating model:

- only the 30-second bot family is actively in focus
- the existing Schwab-backed bot is now labeled:
  - `Schwab 30 Sec Bot`
- a second 30-second bot has been scaffolded locally:
  - `Webull 30 Sec Bot`

Important current implementation state:

- `Schwab 30 Sec Bot`
  - broker provider: `schwab`
  - market data: live Schwab native tick/quote path
  - trading window: existing Schwab 30-second window
- `Webull 30 Sec Bot`
  - broker provider: `webull`
  - market data: Polygon/Massive tick and historical path
  - trading window: `4:00 AM -> 6:00 PM ET`
  - strategy logic: same 30-second entry/indicator stack as the Schwab bot
  - current broker execution status:
    - scaffolded only
    - listens, warms up, evaluates, handoff works
    - OMS routes orders to a Webull adapter stub
    - orders intentionally reject cleanly until official Webull OpenAPI
      credentials are available

Why this was done:

- user wants to compare a second 30-second bot using Polygon data and Webull
  execution
- official Webull App Key / Secret approval is still pending
- the safe interim state is:
  - bot runs
  - UI/control-plane visibility works
  - intents and OMS flow can be validated
  - broker execution rejects safely instead of silently failing

Local code changes prepared in this session:

- new broker adapter scaffold:
  - [webull.py](C:/Users/kkvkr/OneDrive/Documents/GitHub/project-mai-tai/src/project_mai_tai/broker_adapters/webull.py)
- runtime registration + naming updates:
  - [runtime_registry.py](C:/Users/kkvkr/OneDrive/Documents/GitHub/project-mai-tai/src/project_mai_tai/runtime_registry.py)
- settings for Webull provider / account / enable flag:
  - [settings.py](C:/Users/kkvkr/OneDrive/Documents/GitHub/project-mai-tai/src/project_mai_tai/settings.py)
- strategy-engine runtime wiring for `webull_30s`:
  - [strategy_engine_app.py](C:/Users/kkvkr/OneDrive/Documents/GitHub/project-mai-tai/src/project_mai_tai/services/strategy_engine_app.py)
- control-plane page and metadata:
  - [control_plane.py](C:/Users/kkvkr/OneDrive/Documents/GitHub/project-mai-tai/src/project_mai_tai/services/control_plane.py)
- 30-second Webull config variant:
  - [trading_config.py](C:/Users/kkvkr/OneDrive/Documents/GitHub/project-mai-tai/src/project_mai_tai/strategy_core/trading_config.py)
- focused unit coverage:
  - [test_webull_30s_bot.py](C:/Users/kkvkr/OneDrive/Documents/GitHub/project-mai-tai/tests/unit/test_webull_30s_bot.py)

Validation completed locally before deploy:

- UTF-8 compile pass on touched files
- targeted unit tests passed for:
  - runtime registration
  - strategy-engine routing
  - OMS provider construction
  - control-plane metadata / renamed Schwab bot / Webull bot shell
- restart-state protection added before final deploy:
  - when an older persisted handoff snapshot does not contain `webull_30s`
    yet, restore now seeds the new bot from current confirmed names instead of
    leaving it empty until a future confirmation cycle

Release state after deploy:

- PR `#34` merged into `main`
- follow-up restore seeding patch applied locally and prepared for deploy
- local / GitHub / VPS baseline commit for the initial Webull scaffold:
  - `ba4a733323b4da29e6dda41b2933d863df7f5f1d`
- VPS env updated with:
  - `MAI_TAI_STRATEGY_WEBULL_30S_ENABLED=true`
- control-plane routes confirmed live:
  - `/bot/30s`
  - `/bot/30s-webull`
- `/api/bots` confirms both bot identities:
  - `macd_30s -> Schwab 30 Sec Bot`
  - `webull_30s -> Webull 30 Sec Bot`

Operational expectation until Webull keys arrive:

- the Webull bot should warm up, listen, receive handoff, and evaluate on
  Polygon/Massive data
- OMS recognizes the `webull` provider
- order attempts reject explicitly and safely until:
  - `MAI_TAI_WEBULL_APP_KEY`
  - `MAI_TAI_WEBULL_APP_SECRET`
  - `MAI_TAI_WEBULL_ACCOUNT_ID`
  are configured and real order submission is implemented

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
- local `main`, GitHub `main`, and the VPS checkout were updated to commit
  `d4b90c644a35ed7112d01973895aa53a95ffeffb`
- VPS repo was fast-forwarded on `main`
- `project-mai-tai-strategy.service` was restarted by sending `TERM` to the
  running process and letting systemd restart it under `Restart=always`
- new live strategy start time:
  - `2026-04-22 21:06:13 UTC`
  - `2026-04-22 05:06:13 PM ET`
- direct post-restart `/api/bots` verification for `macd_30s` showed:
  - `watchlist = ["AGPU", "AKAN", "GNLN"]`
  - `watchlist_count = 3`
  - `manual_stop_symbols = ["ELPW", "GP", "TORO", "WBUY"]`
  - `position_count = 0`
  - `pending_count = 0`
- this confirms the live `30s` bot is carrying `GNLN` after the unranked
  handoff deploy

Post-deploy caveat:

- the control-plane `/health` endpoint remained `degraded`, but that was still
  driven by the existing reconciler findings
- its `strategy-engine` row also continued to show a stale `stopping` snapshot
  from `2026-04-22 05:06:07 PM ET` even though:
  - systemd showed the strategy service active/running on the new PID
  - `/api/bots` was serving fresh post-restart runtime state
- treat that as a separate health/status freshness issue unless the strategy API
  itself stops updating

## 2026-04-22 Disable Non-30s Defaults And Clarify Scanner-vs-Handoff UI

Requested cleanup for the next code pass:

- keep only the Schwab-backed `macd_30s` path enabled by default
- stop showing score/rank as if it gates bot handoff
- keep score visible in the momentum-confirmed scanner for operator context
- make control-plane/scanner surfaces show ranked scanner names separately from
  symbols actually handed to bots
- do not deploy or restart anything yet from this change set

Root cause found during the sweep:

- the repo still had a split-brain setup:
  - `settings.py` still defaulted `macd_1m`, `tos`, `runner`, and
    `macd_30s_reclaim` to enabled
  - `runtime_registry.py` was even worse: it unconditionally appended
    `macd_1m`, `tos`, and `runner` registrations regardless of settings
- control-plane wording still implied ranked `top_confirmed` names were the bot
  feed even after the earlier unranked handoff change
- scanner rows also mislabeled bot-fed names as `TOP5` because `is_top5` was
  derived from `watched_by` instead of true ranked-scanner membership

Fix implemented locally on branch `codex/disable-non30s-and-clarify-handoff`:

- [settings.py](C:/Users/kkvkr/OneDrive/Documents/GitHub/project-mai-tai/src/project_mai_tai/settings.py)
  - defaulted these to disabled:
    - `strategy_macd_30s_reclaim_enabled = False`
    - `strategy_macd_1m_enabled = False`
    - `strategy_tos_enabled = False`
    - `strategy_runner_enabled = False`
- [runtime_registry.py](C:/Users/kkvkr/OneDrive/Documents/GitHub/project-mai-tai/src/project_mai_tai/runtime_registry.py)
  - made `macd_1m`, `tos`, and `runner` registrations conditional on their
    respective settings instead of always present
- [strategy_engine_app.py](C:/Users/kkvkr/OneDrive/Documents/GitHub/project-mai-tai/src/project_mai_tai/services/strategy_engine_app.py)
  - preserved the current operating model:
    - bot handoff still comes from full `all_confirmed`
  - restored the scanner-visible `top_confirmed` slice back to a ranked view
    using `get_ranked_confirmed(min_score=0)` so score remains visible only as
    scanner context
  - restart/restore seeding now rebuilds visible scanner rows from that ranked
    view while preserving full `all_confirmed` for bot handoff
- [control_plane.py](C:/Users/kkvkr/OneDrive/Documents/GitHub/project-mai-tai/src/project_mai_tai/services/control_plane.py)
  - added a separate `bot_handoff` view/count in the scanner payload
  - fixed `is_top5` to mean actual ranked-scanner membership
  - added `is_handed_to_bot` for explicit bot-feed badges
  - updated dashboard copy so:
    - ranked scanner view is clearly informational
    - handed-to-bot symbols are shown separately
  - bot navigation now follows enabled/registered bots instead of hardcoded
    links to disabled runtimes
- tests:
  - [test_runtime_registry.py](C:/Users/kkvkr/OneDrive/Documents/GitHub/project-mai-tai/tests/unit/test_runtime_registry.py)
    adds direct coverage for default-vs-enabled registrations
  - [test_control_plane.py](C:/Users/kkvkr/OneDrive/Documents/GitHub/project-mai-tai/tests/unit/test_control_plane.py)
    updated for the new UI/API shape and for explicit opt-in when older bot
    pages are under test
  - [test_strategy_engine_service.py](C:/Users/kkvkr/OneDrive/Documents/GitHub/project-mai-tai/tests/unit/test_strategy_engine_service.py)
    updated scanner/handoff expectations to the current model:
    - ranked scanner view stays visible
    - all confirmed names can still hand to enabled bots
    - non-30s bots only exist in tests when explicitly enabled

Current canonical behavior after this local change:

- default local/runtime registration should expose only `macd_30s`
- momentum-confirmed score/rank remains visible in scanner views only
- score no longer gates whether a confirmed symbol reaches the bot
- control plane should show:
  - ranked scanner names
  - handed-to-bot names
  as separate concepts

Local validation completed:

- `python -m compileall` passed for:
  - `src/project_mai_tai/runtime_registry.py`
  - `src/project_mai_tai/settings.py`
  - `src/project_mai_tai/services/strategy_engine_app.py`
  - `src/project_mai_tai/services/control_plane.py`
  - updated unit tests
- repo `.venv` pytest passed:
  - `tests/unit/test_runtime_registry.py`
  - full `tests/unit/test_control_plane.py`
  - targeted broader `tests/unit/test_strategy_engine_service.py` slice:
    - `snapshot_batch`
    - `restore_confirmed_runtime_view`
    - `seeded_confirmed_candidates`
    - `preload_manual_stop_state`
- one note on test scope:
  - the full `tests/unit/test_strategy_engine_service.py` file still timed out
    in this local environment even with a long timeout, so validation for this
    pass used the broader scanner/handoff slice instead of claiming a full-file
    green run

Deployment state for this section:

- no VPS deploy
- no restart
- no GitHub merge yet
- work remains local on branch `codex/disable-non30s-and-clarify-handoff`

## 2026-04-22 Tighten P3 Surge Entry Gates Instead Of Disabling P3

Requested follow-up:

- do not disable `P3_SURGE`
- instead tighten the live Schwab 30s entry gate so late/overextended P3
  entries are blocked more aggressively

Change implemented locally on branch `codex/disable-non30s-and-clarify-handoff`:

- [trading_config.py](C:/Users/kkvkr/OneDrive/Documents/GitHub/project-mai-tai/src/project_mai_tai/strategy_core/trading_config.py)
  - added `p3_entry_stoch_k_cap: float | None = None` to `TradingConfig`
  - updated `make_30s_schwab_native_variant()` to set:
    - `p3_allow_momentum_override = False`
    - `p3_entry_stoch_k_cap = 85.0`
- [schwab_native_30s.py](C:/Users/kkvkr/OneDrive/Documents/GitHub/project-mai-tai/src/project_mai_tai/strategy_core/schwab_native_30s.py)
  - after path evaluation and before confirmation handling, `P3_SURGE` now
    blocks immediately when `stoch_k >= p3_entry_stoch_k_cap`
  - the decision tape reason is explicit:
    - `P3 entry stoch_k cap (<value> >= 85.0)`

Targeted regression coverage added:

- [test_strategy_core.py](C:/Users/kkvkr/OneDrive/Documents/GitHub/project-mai-tai/tests/unit/test_strategy_core.py)
  - `P3` blocked when the old momentum-override style setup would otherwise
    have fired (`stoch_k >= 90`)
  - `P3` blocked when `stoch_k >= 85` at entry
  - `P3` still fires when `stoch_k < 85` and the common gates pass

Local validation completed:

- `python -m compileall src/project_mai_tai/strategy_core/trading_config.py src/project_mai_tai/strategy_core/schwab_native_30s.py tests/unit/test_strategy_core.py`
- repo `.venv` pytest slice passed:
  - `test_schwab_native_entry_engine_blocks_p3_when_momentum_override_would_have_fired`
  - `test_schwab_native_entry_engine_blocks_p3_when_entry_stoch_k_hits_cap`
  - `test_schwab_native_entry_engine_allows_p3_when_entry_stoch_k_is_below_cap`
  - `test_schwab_native_entry_engine_can_fire_p3_with_high_vwap_override`

Deployment state for this section:

- no VPS deploy
- no restart
- change is only on the branch / PR until explicitly merged and deployed

## 2026-04-22 PR #13 Merged, Deployed, And Live Env Recovered

This section records the actual merge/deploy that followed the local-only notes
above.

GitHub merge:

- PR [#13](https://github.com/krshk30/project-mai-tai/pull/13) was merged into
  `main`
- merged `main` commit:
  - `5b0e77f15e03b8b3e3e716bc313ab43c2edbb59b`
- merged scope:
  - default runtime is `macd_30s` only unless non-30s bots are explicitly
    enabled by env
  - scanner score/rank remains visible in momentum-confirmed UI only
  - bot handoff remains unranked from full confirmed scanner state
  - `P3_SURGE` is tightened via:
    - `p3_allow_momentum_override = False`
    - `p3_entry_stoch_k_cap = 85.0`

Initial VPS deploy:

- VPS repo:
  - `/home/trader/project-mai-tai`
- the repo had an untracked `tmp_tv_session_probe/` directory, so the normal
  deploy helper refused a clean deploy
- deployment was completed manually from synced GitHub `main`:
  - `git checkout main`
  - `git merge --ff-only refs/remotes/origin/main`
  - `sudo MAI_TAI_RUN_MIGRATIONS=0 bash ops/bootstrap/08_install_runtime.sh /home/trader/project-mai-tai`
  - `sudo systemctl restart project-mai-tai-strategy.service`
- first successful post-merge strategy restart:
  - `2026-04-22 22:00:29 UTC`

Critical incident during follow-up env cleanup:

- the live env file `/etc/project-mai-tai/project-mai-tai.env` was accidentally
  truncated while trying to force only the 30-second bot on the VPS
- after that truncation, a restart at:
  - `2026-04-22 22:01:55 UTC`
  brought the strategy up with no bots:
  - `strategy bot config | macd_30s=False reclaim=False macd_1m=False tos=False runner=False qty=10 bots=[]`
- this was not a code regression in PR `#13`; it was a bad live env state

Recovery:

- the env file was reconstructed from the still-running service environment,
  using the OMS process as the recovery source:
  - `/proc/274832/environ`
- the live strategy enable flags were then forced to the intended production
  state:
  - `MAI_TAI_STRATEGY_MACD_30S_ENABLED=true`
  - `MAI_TAI_STRATEGY_MACD_30S_RECLAIM_ENABLED=false`
  - `MAI_TAI_STRATEGY_MACD_30S_RETEST_ENABLED=false`
  - `MAI_TAI_STRATEGY_MACD_30S_PROBE_ENABLED=false`
  - `MAI_TAI_STRATEGY_MACD_1M_ENABLED=false`
  - `MAI_TAI_STRATEGY_TOS_ENABLED=false`
  - `MAI_TAI_STRATEGY_RUNNER_ENABLED=false`
- the corrected env was reinstalled and both services were restarted

Final live restart after recovery:

- strategy:
  - `2026-04-22 22:05:13 UTC`
- control plane:
  - `2026-04-22 22:05:14 UTC`

Verified live state after recovery:

- strategy log shows the intended production config:
  - `strategy bot config | macd_30s=True reclaim=False macd_1m=False tos=False runner=False qty=10 bots=['macd_30s']`
- control plane is listening on:
  - `127.0.0.1:8100`
  not `127.0.0.1:8000`
- live `GET /api/bots` on `127.0.0.1:8100` shows only `macd_30s`
- live `/health` on `127.0.0.1:8100` shows:
  - `strategy-engine = healthy`
  - `control-plane = degraded` only because the reconciler still reports
    `cutover_confidence=30`, `total_findings=2`, `critical_findings=2`
- per current operating assumptions, that reconciler degradation is tolerated
  for now because the known mismatch exceptions remain:
  - `CYN`
  - `CANF`

Current intended production model after this recovery:

- only the Schwab-connected `macd_30s` bot should be live
- scanner confirmation should hand off directly to the 30-second bot without
  score/rank gating
- scanner score remains visible only as informational context in the momentum
  confirmed view
- manual bot stop and global scanner stop remain the runtime/operator controls
  for suppressing names

## 2026-04-22 Public HTTP/HTTPS Outage Root Cause And Fix

Issue observed after the recovery above:

- the Mai Tai public site looked down even though the control plane process was
  healthy

What was actually happening:

- `project-mai-tai-control.service` was running normally
- the control plane was listening on:
  - `127.0.0.1:8100`
- public HTTPS returned:
  - `502 Bad Gateway`

Root cause:

- nginx active site file:
  - `/etc/nginx/sites-enabled/project-mai-tai.live.conf`
  was still proxying to:
  - `http://127.0.0.1:8000`
- but the live control plane was bound to:
  - `http://127.0.0.1:8100`
- there was already a correct `sites-available` version pointing to `8100`,
  but the enabled copy was stale

Fix applied on VPS:

- replaced the active enabled site config with the current `sites-available`
  config so nginx now proxies to:
  - `http://127.0.0.1:8100`
- validated nginx config with:
  - `nginx -t`
- reloaded nginx

Follow-up cleanup:

- backup files under `/etc/nginx/sites-enabled/` were causing duplicate server
  name warnings during reload
- those `project-mai-tai.live.conf.bak-*` files were moved out of
  `sites-enabled` into:
  - `/etc/nginx/sites-backup/`
- nginx config was retested and reloaded cleanly

Verification:

- local control plane health still responds on:
  - `127.0.0.1:8100`
- public HTTPS now returns:
  - `401 Unauthorized`
  which is the expected Basic Auth challenge
- this confirms the public reverse proxy is back and the outage was nginx
  routing drift, not an application crash

## 2026-04-22 Remove Remaining Bot Watchlist Cap From Scanner Handoff

Final clarification requested by user:

- once a symbol is confirmed by the momentum scanner, it must be handed to the
  bot immediately
- scanner score/rank should remain visible only as informational context
- scanner ranking must not later push a confirmed name back out of bot
  eligibility
- bot runtime rules, not scanner ranking, decide whether a handed-off symbol
  actually trades

Root cause of the remaining gap:

- the rank gate had already been removed from handoff earlier
- however, `_watchlist_for_bot()` in
  [strategy_engine_app.py](C:/Users/kkvkr/OneDrive/Documents/GitHub/project-mai-tai/src/project_mai_tai/services/strategy_engine_app.py)
  still hard-capped each bot watchlist to `5` symbols
- that meant:
  - confirmed symbols beyond the first five handed-off names were still blocked
    from new bot entry evaluation
  - existing positions / pending symbols could still be managed, but fresh
    symbols outside the capped watchlist could not enter

Change implemented:

- removed the remaining `5`-symbol truncation from `_watchlist_for_bot()`
- current live/expected model is now:
  - squeeze alert
  - momentum scanner confirmation
  - immediate handoff to bot watchlist
  - bot decides whether to trade
- manual stops and global scanner stops still filter symbols before bot entry,
  by design

Related runtime visibility cleanup:

- strategy heartbeat `watchlist_size` now reports the actual retained bot
  watchlist size instead of the ranked scanner `top_confirmed` size
- this avoids misleading health counts now that bot handoff is no longer a
  `top 5` concept

Validation completed locally:

- `python -m compileall src/project_mai_tai/services/strategy_engine_app.py tests/unit/test_strategy_engine_service.py`
- repo `.venv` pytest slice passed for:
  - handoff without rank threshold
  - manual-stop backfill behavior
  - new regression proving confirmed symbols are no longer truncated at `5`
  - manual-stop remove/resume runtime resync coverage

New canonical handoff rule after this change:

- scanner confirmation is the handoff gate
- scanner score/rank is informational only
- bot watchlist cap no longer blocks confirmed names from reaching the bot
- trade decisions are owned by the bot runtime after handoff

Deployment for this section:

- committed on `main` as:
  - `b4b5b441df584fdcae7258fe79eeb5e5b5f9a83a`
- GitHub `main` updated
- VPS repo fast-forwarded to the same SHA
- live strategy service restarted at:
  - `2026-04-22 23:48:44 UTC`

Live verification after restart:

- strategy log shows the intended bot config:
  - `macd_30s=True reclaim=False macd_1m=False tos=False runner=False`
- live `GET /api/bots` on `127.0.0.1:8100` remained healthy after restart
- current live bot state at verification time showed:
  - `watchlist=["GNLN"]`
  - `watchlist_count=1`
- one note:
  - `/health` still briefly showed a stale strategy-engine heartbeat snapshot
    from the restart window (`status=stopping`, `watchlist_size=5`)
  - the bot API was already healthy on the new process, so treat that as
    heartbeat freshness lag rather than a failed deploy

## 2026-04-22 Morning Validation Automation Added

User requested a proactive tomorrow-morning readiness check because the live
environment behaved inconsistently earlier in the day.

Automation created:

- thread heartbeat automation:
  - `4AM Mai Tai Check`
- cadence:
  - daily at approximately `4:10 AM` America/New_York
- purpose:
  - validate the overnight reset state
  - confirm pages are blank/cleared for the new session as expected
  - confirm control plane and strategy services are healthy
  - confirm public HTTP/HTTPS is reachable
  - confirm only the Schwab-backed `macd_30s` bot is active
  - confirm scanner-to-bot handoff is behaving as designed
  - report anything stale, broken, or inconsistent back into this thread

Operational intent:

- this automation is meant to catch the exact class of issues seen today:
  - stale morning UI/runtime state
  - broken public HTTP routing
  - bot enablement drift
  - scanner handoff drift

## 2026-04-23 Morning Readiness Fixes From 4 AM Automation

The first morning validation heartbeat found two real blockers:

- market-data gateway was crash-looping before it could stream live data
- scanner/bot state still showed prior-session symbols after the 4 AM reset

Root cause:

- the market-data gateway had started passing an aggregate-bar callback named
  `on_agg` into the trade stream provider
- `MassiveTradeStream.start()` and the `TradeStreamProvider` protocol had not
  been updated for that callback, so the market-data service crashed with:
  - `TypeError: MassiveTradeStream.start() got an unexpected keyword argument 'on_agg'`
- the scanner session reset still depended on `process_snapshot_batch()`
  receiving a fresh market-data snapshot
- because market-data was crash-looping, no fresh snapshot arrived after 4 AM,
  so stale prior-day scanner/watchlist state could remain visible
- persisted `scanner_confirmed_last_nonempty` snapshots also did not include a
  scanner-session marker, so old snapshots were too easy to trust during
  restart/restore

Fix implemented:

- [protocols.py](C:/Users/kkvkr/OneDrive/Documents/GitHub/project-mai-tai/src/project_mai_tai/market_data/protocols.py)
  - `TradeStreamProvider.start()` now accepts optional `on_agg`
- [massive_provider.py](C:/Users/kkvkr/OneDrive/Documents/GitHub/project-mai-tai/src/project_mai_tai/market_data/massive_provider.py)
  - `MassiveTradeStream.start()` now accepts optional `on_agg`
  - Massive aggregate channels (`A.SYMBOL`) are subscribed/unsubscribed when an
    aggregate callback is active
  - Massive aggregate messages are normalized into `LiveBarRecord`
- [strategy_engine_app.py](C:/Users/kkvkr/OneDrive/Documents/GitHub/project-mai-tai/src/project_mai_tai/services/strategy_engine_app.py)
  - scanner/runtime session rollover now runs from the heartbeat loop, so the
    4 AM reset no longer depends on a fresh scanner snapshot
  - scanner rollover clears confirmed scanner state, current/all confirmed
    rows, retained watchlist, momentum-alert engine state, top-gainer tracker
    state, recent alerts, feed-retention state, manual stops, bot watchlists,
    and recent decision rows for the new session
  - persisted non-empty scanner snapshots now include
    `scanner_session_start_utc`
  - persisted momentum-alert warmup snapshots now include
    `scanner_session_start_utc`
  - restart seeding now skips unmarked, invalid, or prior-session confirmed
    scanner and momentum-alert snapshots
- [control_plane.py](C:/Users/kkvkr/OneDrive/Documents/GitHub/project-mai-tai/src/project_mai_tai/services/control_plane.py)
  - scanner UI fallback data now requires a matching scanner-session marker
    before it can render a last-nonempty confirmed snapshot

Regression coverage added:

- [test_market_data_gateway.py](C:/Users/kkvkr/OneDrive/Documents/GitHub/project-mai-tai/tests/unit/test_market_data_gateway.py)
  - verifies the Massive stream accepts and normalizes aggregate callbacks
- [test_strategy_engine_service.py](C:/Users/kkvkr/OneDrive/Documents/GitHub/project-mai-tai/tests/unit/test_strategy_engine_service.py)
  - verifies the scanner session can roll cleanly without any new snapshot batch
  - verifies unmarked old scanner snapshots do not reseed stale symbols
  - verifies unmarked old momentum-alert snapshots do not replay stale alerts

Operational prevention:

- keep the `4AM Mai Tai Check` heartbeat active
- future provider callback/signature changes must include contract coverage
- scanner reset must stay heartbeat-driven, not market-data-snapshot-driven
- old scanner restore data must remain tied to a concrete scanner session before
  it is trusted

Deployment and verification:

- PR [#14](https://github.com/krshk30/project-mai-tai/pull/14) merged the
  Massive aggregate callback and heartbeat-driven scanner reset fix
- PR [#15](https://github.com/krshk30/project-mai-tai/pull/15) merged the
  follow-up stale scanner restore hardening
- final deployed `main` SHA:
  - `e6eaee2e04499dce17c89910c15ee56826958da0`
- VPS checkout was fast-forwarded to that SHA
- one-time cleanup removed bad persisted scanner dashboard snapshots that were
  written while the stale restore path was still active:
  - `scanner_confirmed_last_nonempty`
  - `scanner_alert_engine_state`
  - `scanner_cycle_history`
- restarted targeted services only:
  - `project-mai-tai-market-data.service`
  - `project-mai-tai-strategy.service`
  - `project-mai-tai-control.service`
- final live verification:
  - public HTTPS returns `401`, expected Basic Auth challenge
  - market-data gateway healthy with no `on_agg` / unexpected-keyword crash
  - strategy engine healthy with `bot_count=1`
  - only Schwab-backed `macd_30s` appears in `/api/bots`
  - `/api/scanner` is clean for the new session:
    - `status=idle`
    - `cycle_count=0`
    - `watchlist_count=0`
    - `all_confirmed_count=0`
    - `bot_handoff_count=0`
- overall `/health` remains `degraded` only because the known reconciler
  findings bucket is still reporting two findings; strategy, market-data, OMS,
  and control-plane functionality are healthy

## 2026-04-23 Schwab Raw-Alert Prewarm Patch

Decision:

- temporary safe warm-up path for the Schwab-native 30-second bot
- do not use Polygon/Massive historical 30-second bars for the Schwab-native
  trading bot
- start Schwab streaming earlier for raw momentum-alert symbols, before they
  become confirmed scanner handoff symbols
- prewarm symbols must not trade early; they only build Schwab-derived 30-second
  bars

Implemented behavior:

- when the momentum alert engine emits a raw alert, the ticker is added to
  `schwab_prewarm_symbols`
- `schwab_stream_symbols()` now includes:
  - active Schwab bot symbols
  - open-position symbols
  - raw-alert prewarm symbols
- manual stops still win:
  - global/manual-stopped names are removed from the prewarm list and Schwab
    stream subscription set
- the `macd_30s` runtime keeps prewarm symbols separate from the live watchlist
- prewarm-only Schwab trade ticks build 30-second bars and persist bar history
  with decision status `prewarm` / reason `Schwab prewarm only`
- prewarm-only symbols do not evaluate completed-bar entries or intrabar entries
- if live aggregate bars are enabled, prewarm-only Schwab trade ticks still build
  bars from the Schwab tick stream instead of returning early
- scanner/session rollover clears the prewarm list for the new day
- strategy-state events and control-plane runtime snapshots now expose:
  - per-bot `prewarm_symbols`
  - state-level `schwab_prewarm_symbols`

Operational meaning:

- flow is now:
  - squeeze/momentum raw alert appears
  - strategy subscribes Schwab stream for that ticker immediately
  - Schwab ticks start building 30-second bars
  - confirmed scanner handoff later promotes the ticker into the `macd_30s`
    watchlist
  - only after watchlist promotion can the bot evaluate entries/trade
- this should improve same-morning warm-up without mixing data providers
- it is still a temporary bridge; the more solid future solution is a true
  Schwab-native historical 30-second warm-up source if Schwab exposes one or if
  we build durable session-wide Schwab tick/bar capture

Regression coverage added:

- raw momentum alert adds a Schwab prewarm symbol without adding it to the bot
  watchlist
- prewarm-only Schwab trade ticks build bars while skipping all entry checks
- global manual stop removes a symbol from Schwab prewarm and stream targets
- existing Schwab stream subscription tests were adjusted for the intended
  one-active-bot posture where disabled bots do not exist in runtime state
- manual-stop preload now compares persisted stop snapshots against the
  service/runtime clock instead of the real wall clock, keeping restart safety
  tests and injected-clock service runs consistent

Validation:

- passed:
  - `python -m py_compile src/project_mai_tai/events.py src/project_mai_tai/services/control_plane.py src/project_mai_tai/services/strategy_engine_app.py`
  - `python -m ruff check src/project_mai_tai/events.py src/project_mai_tai/services/control_plane.py src/project_mai_tai/services/strategy_engine_app.py tests/unit/test_runner_strategy.py tests/unit/test_strategy_core.py tests/unit/test_strategy_engine_service.py`
  - `python -m pytest tests/unit/test_strategy_engine_service.py -k "prewarm or schwab_stream or schwab_native or manual_stop or scanner_session"`
  - `python -m pytest tests/unit/test_strategy_core.py tests/unit/test_runner_strategy.py`
- attempted full `python -m pytest tests/unit`, but it exceeded the 5-minute
  local desktop timeout; do not treat that as a pass

## 2026-04-23 Schwab Prewarm Deploy Follow-Up

Live deploy note:

- PR #16 initially expanded Schwab stream targets to include raw-alert prewarm
  symbols as intended
- on VPS restart, the strategy process stayed active but kept reporting
  `starting`
- cause found in runtime loop, not Schwab auth:
  - prewarm increased Schwab stream targets to 18 symbols
  - `_drain_schwab_stream_queues()` drained quote/trade queues with
    `while not queue.empty()`
  - in a busy premarket stream, the queue can keep refilling faster than the
    loop can finish, starving heartbeat/scanner/runtime work
- hotfix:
  - bound each Schwab stream drain pass with `_schwab_stream_drain_max_events`
  - default cap is 1000 events per loop pass
  - remaining queued ticks are processed on the next loop pass, allowing
    heartbeat, scanner batches, state snapshots, and subscription sync to run
- additional live-load guard:
  - Schwab quote ticks are now ignored for prewarm-only symbols
  - prewarm still processes Schwab trade ticks, which are what build 30-second
    OHLCV bars
  - quotes are kept once a symbol is in an active watchlist/open-position path
    because routing still needs bid/ask there
  - generic market-data fallback now excludes prewarm-only Schwab symbols; the
    fallback can still cover active/watchlist/open-position Schwab symbols, but
    raw-alert prewarm must stay Schwab-native and must not trigger generic
    historical hydration/replay
- regression coverage added:
  - Schwab queue drain processes only the configured max events and leaves the
    remainder queued for the next pass
  - Schwab quote enqueue skips prewarm-only symbols but keeps quotes after
    watchlist promotion
  - generic fallback receives active Schwab symbols only, not prewarm-only
    symbols

Final deployment state:

- PR #16 merged raw-alert Schwab prewarm
- PR #17 merged bounded Schwab stream queue draining
- PR #18 merged quote-drop behavior for prewarm-only symbols
- PR #19 merged generic-fallback exclusion for prewarm-only symbols
- final runtime code deployed to VPS:
  - `b1b4efd9bc2770de8ec471ec2b5a1f4076edd9eb`
- VPS runtime refreshed with migrations disabled and strategy service restarted
- final live verification:
  - `project-mai-tai-strategy.service` active
  - strategy heartbeat healthy
  - only `macd_30s` bot active
  - Schwab stream symbols were populated from raw-alert prewarm/current active
    symbols
  - market-data fallback active symbol count returned to `0`, confirming
    prewarm-only symbols are no longer being routed through generic fallback
  - overall `/health` still degraded only because the known reconciler findings
    bucket reports two critical findings

## 2026-04-23 Critical Prewarm Loop Stall Fix

Live symptom:

- Decision Tape stopped advancing around `2026-04-23 07:16:30 AM ET`
- strategy service process stayed systemd-active, but strategy heartbeat dropped
  out of `/health`
- strategy log stopped immediately after the `07:17 AM ET` raw momentum-alert
  burst

## 2026-04-23 Live Readiness Heartbeat Follow-Up

Heartbeat check at `2026-04-23 09:33 AM ET` found the Schwab-backed
`macd_30s` bot healthy and listening, with no Schwab stale symbols and no
generic fallback active.

Operational cleanup performed on the VPS:

- disabled and stopped stale `project-mai-tai-tv-alerts.service`
  - current `main` no longer ships the `mai-tai-tv-alerts` executable
  - systemd was crash-looping with `status=203/EXEC`
  - this was an obsolete service-unit/runtime mismatch, not a Schwab bot issue
- manually stopped `YCBD` for `macd_30s` after a rapid scale sequence created a
  temporary reconciler mismatch
  - broker/virtual reconciliation cleared after the fills settled
  - `YCBD` was removed from the live `macd_30s` watchlist

Current post-cleanup state:

- `project-mai-tai-strategy.service`, control, market-data, OMS, and reconciler
  are active
- only Schwab-backed `macd_30s` is active in `/api/bots`
- bot `data_health` is healthy
- strategy heartbeat reports no stale Schwab symbols
- public HTTPS still returns the expected Basic Auth `401`
- `/health` remains degraded only from reconciler history/open incidents, not
  from strategy/Schwab data health

Root cause:

- raw-alert Schwab prewarm correctly subscribed many symbols before confirmation
- prewarm-only completed 30-second bars were also being persisted to
  `strategy_bar_history`
- during a live alert burst, that created per-bar database writes for symbols
  that were not yet tradable/watchlisted, pinning the strategy loop enough to
  starve heartbeat, scanner handoff, state snapshots, and fresh decisions

Fix:

- prewarm-only bars still build from Schwab trade ticks in memory
- prewarm-only bars do not calculate indicators; a later confirmed handoff uses
  the warmed bar builder and calculates indicators on the active/tradable path
- prewarm-only bars no longer write `StrategyBarHistory` rows or Decision Tape
  rows
- active/watchlist/open-position bars still persist normally after confirmation
- Schwab stream queue drain cap reduced from `1000` to `100` events per loop
  pass so heartbeat/scanner/control-plane work keeps getting time under bursts

Regression coverage added:

- prewarm-only Schwab trade ticks build bars without entry checks, indicator
  calculation, or `_persist_bar_history`

Follow-up live finding:

- after the first fix, the process survived the 7:30 AM ET alert burst but
  stalled again after the 7:31 AM ET ELAB burst
- second root cause was the remaining prewarm-only indicator calculation:
  `builder.get_bars_as_dicts()` plus full indicator recalculation on every
  completed prewarm-only 30-second bar across roughly 40 Schwab stream symbols
- prewarm is now strictly bar accumulation only until a symbol becomes active

## 2026-04-23 Decision Tape Live-Symbol Cleanup

Observed after the prewarm fixes:

- `/api/bots` and the Decision Tape could still show old/runtime diagnostic
  decision rows for Schwab stream/prewarm symbols that were not in the live bot
  watchlist
- the left rail correctly showed live symbols such as `AUUD` and `ELAB`, but the
  table was noisy because it displayed every recent runtime decision row

Fix:

- bot runtime summaries now expose Decision Tape rows only for live symbols:
  watchlist, open positions, and pending order symbols
- control-plane `/api/bots` applies the same live-symbol filter, including when
  it falls back to persisted bar-history decisions
- user-facing `idle / no entry path matched` is normalized to:
  - status: `evaluated`
  - reason: `entry evaluated; no setup matched this bar`
- meaning: the symbol had enough warm-up to calculate indicators and was checked
  on that completed bar; no configured entry path fired on that bar

Regression coverage added:

- runtime summary filters prewarm/non-live decision rows out of the displayed
  Decision Tape
- control-plane `/api/bots` filters Decision Tape rows to the live watchlist and
  normalizes the no-entry wording

## 2026-04-23 Schwab Data Halt Circuit Breaker

Implementation branch:

- `codex/schwab-data-halt-circuit-breaker`

Critical safety change:

- Schwab-backed bot symbols now enter a `critical` data halt when the Schwab
  stream is stale/disconnected
- halted Schwab symbols block new entries inside the 30-second runtime
- stale Schwab symbols are surfaced through bot `data_health`
- control-plane bot pages show red `DATA HALT` / `Schwab Data Halt` state when
  the halt is active
- strategy heartbeats become `degraded` while Schwab stale symbols exist and
  include `schwab_stale_symbols`

Hotfix after first deploy:

- `HeartbeatPayload.status` only allows `starting`, `healthy`, `degraded`, or
  `stopping`
- the first circuit-breaker deploy incorrectly emitted heartbeat status
  `critical`, causing the strategy service to restart when Schwab symbols became
  stale
- heartbeat status now uses `degraded` for Schwab data halt, while bot
  `data_health.status` remains `critical` for the red bot UI

Second safety tuning:

- the first monitor pass used the old 3-second per-symbol stale threshold for
  all active watchlist symbols
- that was too aggressive for normal sparse Schwab quotes and caused repeated
  ELAB halt/recover/resubscribe loops while the stream was connected
- the data-halt circuit now halts immediately when the Schwab stream client is
  disconnected, but connected per-symbol quietness must exceed at least 30
  seconds before it blocks/closes

Emergency close behavior:

- if a halted Schwab symbol has an open position, the strategy service attempts
  a close intent with reason `SCHWAB_DATA_STALE_EMERGENCY_CLOSE`
- emergency close routing uses Schwab quote polling/bid data only
- if Schwab quotes are unavailable, entries remain halted and the UI stays red;
  the bot records that emergency close is waiting for a sellable quote

Fallback policy change:

- generic market-data / Polygon fallback no longer targets Schwab-native bot
  strategy codes, even when the Schwab stream is disconnected or stale
- this keeps 30-second bot decisions/trading strictly on the Schwab-native data
  path; fallback can still be diagnostic/subscription noise, not a trading input

Regression coverage added:

- stale Schwab open position creates a `critical` halt and emergency close
  intent
- stale Schwab watchlist symbol without an open position halts entries and
  clears on live Schwab stream recovery
- missing Schwab quote-poll support does not restart the strategy service; it
  leaves the halt visible/critical instead
- generic market data never routes stale/disconnected symbols into the Schwab
  native bot
- control plane exposes the red data-halt state in API/page rendering

Validation:

- passed:
  - `.venv\Scripts\python.exe -m py_compile src/project_mai_tai/events.py src/project_mai_tai/services/strategy_engine_app.py src/project_mai_tai/services/control_plane.py tests/unit/test_strategy_engine_service.py tests/unit/test_control_plane.py`
  - `.venv\Scripts\python.exe -m pytest tests/unit/test_strategy_engine_service.py::test_service_uses_fallback_quotes_for_stale_schwab_open_positions tests/unit/test_strategy_engine_service.py::test_service_skips_stale_quote_poll_when_adapter_lacks_fetch_quotes tests/unit/test_strategy_engine_service.py::test_service_halts_stale_schwab_watchlist_symbol_without_open_position tests/unit/test_strategy_engine_service.py::test_generic_market_data_never_targets_schwab_native_bot_when_stream_is_stale tests/unit/test_control_plane.py::test_control_plane_marks_schwab_data_halt_red_on_bot_page -q`
  - `.venv\Scripts\python.exe -m pytest tests/unit/test_control_plane.py::test_control_plane_surfaces_probe_and_reclaim_bot_pages_when_enabled tests/unit/test_control_plane.py::test_bot_page_renders_simple_trade_summary_table -q`
- attempted broader:
  - `.venv\Scripts\python.exe -m pytest tests/unit/test_strategy_engine_service.py tests/unit/test_control_plane.py -q`
  - this hung until the local desktop timeout and did not produce a useful
    failure; do not count it as a pass

## 2026-04-23 AUUD Data-Halt Ghost State Follow-Up

Live heartbeat finding:

- AUUD entered a Schwab data halt and the emergency close was submitted at
  09:39:35 AM ET
- Schwab eventually filled the close at 09:43:44 AM ET for 10 shares at 9.44
- AUUD was manually stopped, removed from the live watchlist, and had no open
  bot position afterward
- the bot `data_health` panel still showed AUUD as halted because the runtime
  only cleared data-halt flags for still-active symbols that recovered; symbols
  removed from the active/open set could leave a stale red UI state behind

Fix branch:

- `codex/clear-stale-data-halt-on-symbol-removal`

Code change:

- `StrategyEngineService._monitor_schwab_symbol_health` now clears Schwab
  runtime data-halt flags for symbols that are no longer active or open
- this keeps manual-stopped/closed symbols from leaving ghost `DATA HALT`
  labels after the safety close has completed

Regression coverage added:

- stale Schwab watchlist symbol enters data halt
- symbol is then removed from the active watchlist while another Schwab symbol
  remains active
- subsequent Schwab health monitor pass clears the old halted symbol and returns
  bot `data_health` to healthy

## 2026-04-23 Live Decision Tape Placeholder Follow-Up

Live UI issue:

- AUUD could appear in the bot live-symbol list with fresh Schwab activity while
  the Decision Tape showed only other symbols
- this was not a handoff-cap bug; the control plane only rendered persisted
  decision rows, so a live symbol with fresh ticks but no recent completed
  evaluable 30-second bar could disappear from the table entirely
- this created the impression that the bot was not listening even when the
  symbol was active in the watchlist

Fix:

- the control plane now injects a placeholder Decision Tape row for live bot
  symbols that have no current decision event
- placeholder rows show `pending` with an explicit reason such as:
  - `live in bot; waiting for next completed 30s trade bar to evaluate`
  - `live in bot; receiving Schwab ticks, waiting for first completed 30s trade bar`
- this makes live/watchlist state and Decision Tape state line up for symbols
  like AUUD without changing trading behavior

Regression coverage added:

- control plane still filters the Decision Tape to live symbols only
- a live watchlist symbol with fresh ticks and bar history but no recent
  decision row now appears in `/api/bots` with the placeholder pending reason

Validation:

- passed:
  - `.venv\Scripts\python.exe -m pytest -p no:cacheprovider tests/unit/test_control_plane.py::test_control_plane_decision_tape_shows_only_live_symbols tests/unit/test_control_plane.py::test_control_plane_decision_tape_includes_live_symbol_waiting_for_evaluation -q`
  - `.venv\Scripts\python.exe -c "from pathlib import Path; import ast; ast.parse(Path(r'src/project_mai_tai/services/control_plane.py').read_text(encoding='utf-8')); ast.parse(Path(r'tests/unit/test_control_plane.py').read_text(encoding='utf-8')); print('syntax ok')"`

## 2026-04-23 SKLZ Schwab Data-Halt Root Cause

Live finding:

- the red `DATA HALT` panel on `SKLZ` was a real runtime halt, not a control-plane
  freshness/rendering bug
- live strategy logs showed repeated `SKLZ` stale/recover cycles where Schwab
  stream activity went quiet long enough to trigger the stale-symbol monitor and
  then recovered a few seconds later
- the critical bug was that a symbol could leave the active Schwab set and later
  re-enter while still carrying old `last_trade_at` / `last_quote_at` timestamps
- when that happened, the next reactivation inherited stale age from the
  symbol's previous active period and could trip `DATA HALT` almost immediately
  after handoff/re-confirm instead of receiving a fresh grace window

Code fix:

- `StrategyEngineService._clear_inactive_schwab_runtime_data_halts` now prunes
  inactive per-symbol Schwab freshness trackers as soon as a symbol leaves the
  active set
- cleared inactive state now includes:
  - `_schwab_symbol_last_stream_trade_at`
  - `_schwab_symbol_last_stream_quote_at`
  - `_schwab_symbol_last_resubscribe_at`
  - `_schwab_symbol_last_quote_poll_at`
  - inactive entries in `_schwab_stale_symbols`
- the no-active-symbols branch now uses the same cleanup path, so a symbol that
  fully leaves the bot cannot carry stale freshness timestamps into a future
  reactivation

Why this matters:

- without this cleanup, names like `SKLZ` could be re-confirmed or resumed into
  the 30s Schwab bot and inherit an old freshness timestamp from a prior active
  period
- that made the runtime treat the symbol as already 30s+ stale even though it had
  just re-entered the bot, which is the root-cause bug behind the near-immediate
  red-halt behavior

Regression coverage added:

- stale symbol leaves the active set and the bot health returns cleanly
- manually stopped / removed symbol drops old Schwab freshness timestamps
- the same symbol can then be resumed/reactivated without inheriting an immediate
  stale halt

Validation:

- passed:
  - `.venv\Scripts\python.exe -m pytest -p no:cacheprovider tests/unit/test_strategy_engine_service.py::test_service_clears_data_halt_when_stale_symbol_leaves_active_set tests/unit/test_strategy_engine_service.py::test_service_reactivated_symbol_gets_fresh_schwab_stale_grace_window tests/unit/test_strategy_engine_service.py::test_service_does_not_halt_quiet_schwab_symbol_inside_grace_window -q`
  - `.venv\Scripts\python.exe -m py_compile src/project_mai_tai/services/strategy_engine_app.py tests/unit/test_strategy_engine_service.py`

## 2026-04-23 FTFT Repeating Schwab Stale/Re-subscribe Flap

Live finding:

- after the noisy intraday heartbeat was reduced, the control plane still showed
  intermittent red `DATA HALT` states for `FTFT`; this was not caused by the
  automation change
- live strategy logs showed a repeating pattern where `FTFT` would go stale,
  trigger forced Schwab resubscribe, then recover a few seconds later
- the stale monitor was using an aggressive default of only `3.0` seconds for
  per-symbol Schwab stream freshness
- that threshold was too tight for quiet but still-valid Schwab symbols and
  produced transient halts on names like `FTFT` even when the broader stream
  was healthy

Code fix:

- raised the default `schwab_stream_symbol_stale_after_seconds` from `3.0` to
  `8.0` in `Settings`
- kept the halt behavior itself unchanged:
  - a stale symbol still enters `DATA HALT`
  - entries are still blocked for halted symbols
  - open positions still retain emergency-close protection

Why this matters:

- the runtime was correctly auto-recovering these symbols after forced
  resubscribe, but the `3.0` second threshold created unnecessary red flaps and
  temporary entry blocks on otherwise recoverable symbols
- moving to `8.0` seconds preserves safety while tolerating short quiet gaps in
  Schwab updates, which better matches what was observed live on `FTFT`

Regression coverage added:

- default Schwab settings now tolerate a brief `5` second quiet period without
  flagging a symbol as stale

Expected live behavior after deploy:

- brief FTFT-style quiet gaps under `8` seconds should no longer trigger
  transient `DATA HALT`
- if a symbol truly stops updating for longer than that window, the existing
  halt and forced-resubscribe logic still engages

## 2026-04-23 Scanner-To-Bot One-Way Handoff Ownership

Root cause:

- the runtime was still re-syncing bot watchlists from the scanner confirmed
  list on every scanner cycle
- that meant scanner state still controlled bot membership after handoff
- a global scanner `Stop` correctly removed the symbol everywhere, but a later
  `Resume` only re-added it if the scanner still owned it in current confirmed
- that is why names like `SST` could come back in momentum/scanner while never
  being restored into the 30s bot

Code fix:

- added durable bot-owned handoff state in `StrategyEngineState`
  - `bot_handoff_symbols_by_strategy`
  - `bot_handoff_history_by_strategy`
- newly confirmed symbols are now added into the bot-owned handoff set
- bot watchlists now resync from that bot-owned handoff set, not from the
  scanner confirmed list
- global scanner `Stop` now removes the symbol from active bot handoff state
  while preserving session history
- global scanner `Resume` now restores the symbol back into the bot handoff set
  if it had already been handed off earlier in the same session
- 4:00 AM scanner-session reset now clears the bot-owned handoff state for the
  new day

Restart persistence:

- persisted scanner snapshots now save:
  - `bot_handoff_symbols_by_strategy`
  - `bot_handoff_history_by_strategy`
- restart restore now prefers that persisted bot-owned handoff state so the bot
  does not lose ownership midday just because scanner confirmed visibility
  changed
- cycle-history fallback also restores bot handoff ownership if needed

Behavior after this fix:

- scanner can still:
  - detect alerts
  - confirm symbols
  - show rankings / score / momentum views
  - globally stop a symbol everywhere
- but scanner no longer removes a previously handed-off symbol from the bot just
  because scanner confirmed membership changes later
- after handoff, the bot owns the symbol until:
  - global scanner stop
  - bot/manual stop rules
  - daily 4:00 AM reset

Regression coverage added:

- global stop then resume restores a previously handed-off symbol into the bot
- persisted bot handoff state restores correctly into bot watchlists
- scanner-cycle snapshot persistence now includes bot handoff ownership
- adjacent restart/manual-stop regressions still pass

Validation:

- passed:
  - `.venv\Scripts\python.exe -m pytest tests/unit/test_strategy_engine_service.py -k "global_stop_resume_restores_previously_handed_off_symbol_to_bot_watchlist or restore_confirmed_runtime_view_prefers_persisted_bot_handoff_state or publish_strategy_state_persists_scanner_cycle_history_snapshot or seeded_confirmed_candidates_restore_watchlist_from_all_confirmed_when_top_confirmed_empty or manual_stop_resume_readds_symbol_to_live_watchlist_immediately or snapshot_batch_keeps_faded_confirmed_symbols_in_bot_watchlists_for_session_continuity"`
  - `.venv\Scripts\python.exe -m pytest tests/unit/test_strategy_engine_service.py -k "service_preloads_manual_stops_before_post_restart_trading or seeded_confirmed_candidates_are_revalidated_into_fresh_top_confirmed or global_manual_stop_removes_schwab_prewarm_symbol"`
  - AST parse check for:
    - `src/project_mai_tai/services/strategy_engine_app.py`
    - `tests/unit/test_strategy_engine_service.py`

Known validation note:

- a full `tests/unit/test_strategy_engine_service.py` run exceeded the local
  command timeout window here, so validation was done with the targeted restart,
  stop/resume, and snapshot persistence slices above

## 2026-04-23 OMS Working-Order Watchdog Refresh

Root cause:

- OMS was syncing broker order status, but it was not actively managing working
  orders after submission
- if a buy, close, or scale order stayed open while price moved away, Mai Tai
  could leave that order hanging for many minutes
- the strategy runtime then kept the symbol in pending state waiting for that
  old order to resolve, which is why names like `SKLZ` could sit with stale
  sell limits instead of chasing the market

Code fix:

- added an OMS working-order refresh watchdog in
  `src/project_mai_tai/oms/service.py`
- every broker sync pass now checks open working orders and, once a working
  order has had no progress for `5` seconds, OMS:
  - fetches the latest broker status
  - keeps any partial-fill progress already reported
  - cancels the stale working order internally
  - submits a replacement order for the remaining quantity
- limit orders are repriced from fresh live broker quotes before resubmission
  - buys refresh from the ask
  - sells refresh from the bid
- market orders are also watched every `5` seconds and can be resubmitted if
  they somehow remain working
- internal watchdog cancels are persisted in OMS order history, but they are not
  published back to the strategy runtime as terminal `cancelled` events
  - this avoids falsely clearing bot pending-open / pending-close / pending-scale
    state during an in-flight cancel-and-replace cycle

Settings change:

- `oms_broker_sync_interval_seconds`: `15` -> `5`
- new setting: `oms_working_order_refresh_seconds = 5`

Additional correctness fix:

- OMS order rows now persist the original request `order_type` and
  `time_in_force` instead of silently defaulting every stored order to `market`
  / `day`
- that keeps later broker sync and watchdog replacement logic aligned with the
  real order semantics

Regression coverage added:

- stale working limit buy order is cancelled and replaced with a fresh ask-based
  price
- stale partially-filled sell order is cancelled and replaced only for the
  remaining quantity using a fresh bid-based price
- internal watchdog cancel is intentionally hidden from runtime order-event
  publication so strategy pending state stays intact
- adjacent OMS sync tests for cancel / partial-fill / terminal event publishing
  still pass

Validation:

- passed:
  - `.venv\Scripts\python.exe -m pytest tests/unit/test_oms_risk_service.py -k "refreshes_stale_working_limit_buy_order or refreshes_remaining_quantity_for_stale_sell_order or syncs_open_order_status_from_broker or sync_publishes_terminal_order_event_for_strategy_runtime or sync_skips_duplicate_partial_without_new_fill_progress"`
  - compile check for:
    - `src/project_mai_tai/oms/service.py`
    - `src/project_mai_tai/settings.py`
    - `tests/unit/test_oms_risk_service.py`

## 2026-04-23 Schwab Disconnect Debounce + Safer DATA HALT Copy

Root cause:

- a brief Schwab websocket disconnect was being treated as an immediate
  stale-symbol halt for every active Schwab-backed symbol
- the runtime already had a 30-second minimum stale grace window for
  per-symbol quiet periods, but `_monitor_schwab_symbol_health()` bypassed that
  grace entirely whenever the streamer reported `connected = false`
- the bot page copy then always said open positions were being routed for
  emergency close, even when the bot had zero open positions

Code fix:

- added a streamer disconnect grace timer in
  `src/project_mai_tai/services/strategy_engine_app.py`
- short Schwab reconnect blips now wait through the same data-halt grace window
  before escalating active symbols into runtime `DATA HALT`
- persistent disconnects still escalate into symbol halts and still preserve the
  emergency-close behavior for real open positions
- updated the bot listening-status detail and `Schwab Data Halt` panel copy in
  `src/project_mai_tai/services/control_plane.py`
  - if the bot has no open positions, the page now says there are no open
    positions exposed to the emergency-close path
  - if the bot does have open positions, the page still warns that those names
    are eligible for emergency close using Schwab quotes

Regression coverage added:

- brief Schwab stream disconnect stays inside the data-halt grace window and
  does not mark active symbols stale immediately
- persistent Schwab stream disconnect still halts symbols after the grace window
- control-plane bot page and `/bot` listening-status copy now reflect the
  no-open-position case correctly

Validation:

- passed:
  - `.venv\Scripts\python.exe -m pytest tests/unit/test_strategy_engine_service.py -k "brief_schwab_stream_disconnect or persistent_schwab_stream_disconnect or stale_schwab_watchlist_symbol_without_open_position or default_stale_threshold_tolerates_brief_quiet_gap"`
  - `.venv\Scripts\python.exe -m pytest tests/unit/test_control_plane.py -k "schwab_data_halt_red_on_bot_page"`
  - `.venv\Scripts\python.exe -m py_compile src/project_mai_tai/services/strategy_engine_app.py src/project_mai_tai/services/control_plane.py tests/unit/test_strategy_engine_service.py tests/unit/test_control_plane.py`

## 2026-04-24 Overnight Validation Gaps + Session Restore Guard

Root causes found during the 6:00 AM ET live-readiness check:

- the validator caught the Schwab OAuth refresh-token failure, but the prompt did
  not explicitly force inspection of bot listening status plus stale live symbols
  / feed-state carryover on both 30-second bots
- the new `Webull 30 Sec Bot` reused several hard-coded Schwab UI labels on the
  bot page and in placeholder decision rows, which made Polygon-backed waiting /
  halt states look like Schwab wiring errors
- scanner cycle-history restore could repopulate watchlist / bot-handoff symbols
  from a prior snapshot even when the new session had not yet produced a real
  current-session handoff; that made both Schwab and Webull appear to wake up
  with yesterday-style live symbols / feed states already attached

Code fix:

- updated `src/project_mai_tai/services/control_plane.py`
  - bot listening-status detail now uses the runtime provider name
  - `Schwab Data Health` / `Schwab Data Halt` page labels are now provider-aware
    and render as `Polygon ...` on the Webull 30-second bot
  - placeholder Decision Tape rows for Webull now say `Polygon market data` /
    `Polygon ticks` instead of `Schwab ...`
- updated `src/project_mai_tai/services/strategy_engine_app.py`
  - added a persisted `session_handoff_active` marker
  - scanner cycle-history watchlist fallback now restores only after a real
    current-session handoff has been recorded
  - overnight / fresh-session snapshots without that marker no longer repopulate
    stale live symbols or feed states into `macd_30s` or `webull_30s`

Operational note:

- the 6:00 AM ET check also confirmed a separate live Schwab auth issue on the
  VPS: refresh token exchange was failing with
  `refresh_token_authentication_error` / `unsupported_token_type`
- that OAuth problem is independent from the session-restore/UI fix above and
  still requires Schwab reauthorization on the VPS

Regression coverage added:

- Webull Decision Tape placeholders use Polygon wording
- Webull bot page halt cards and listening detail use Polygon wording
- scanner cycle-history restore skips watchlist-only snapshots that do not carry
  the new `session_handoff_active` marker
- scanner cycle-history restore still works when a real current-session handoff
  snapshot includes the marker

Validation:

- passed:
  - `.venv\Scripts\python.exe -m pytest tests/unit/test_control_plane.py -k "decision_tape or webull_bot_page_uses_polygon_data_halt_wording"`
  - `.venv\Scripts\python.exe -m pytest tests/unit/test_scanner_cycle_history_restore.py`
  - `.venv\Scripts\python.exe -m py_compile src/project_mai_tai/services/control_plane.py src/project_mai_tai/services/strategy_engine_app.py tests/unit/test_control_plane.py tests/unit/test_scanner_cycle_history_restore.py`

## 2026-04-24 Morning Follow-Up: Schwab Hidden Prewarm Load + Auth-Failure Visibility

Live investigation:

- the user-reported `AUUD` / `CAST` morning symbols were not literally stale
  carryover from 2026-04-23; they were freshly confirmed on 2026-04-24:
  - `CAST` confirmed at `04:06:56 AM ET`
  - `AUUD` confirmed at `06:18:23 AM ET`
- however, the live Schwab strategy heartbeat showed a larger hidden stream load:
  - visible bot watchlist size: `2`
  - hidden `schwab_stream_symbols`: `32`
- root cause: Schwab prewarm symbols were session-long and only capped by count;
  they did not age out intraday, so momentum alerts could accumulate a large
  hidden Schwab stream subscription set even after symbols never handed off
- the separate Schwab halt problem was confirmed as an OAuth/auth issue, not a
  symbol-count issue:
  - `strategy.log` showed repeated Schwab streamer connection failures while
    refreshing the token / fetching streamer credentials
  - prior manual token probe already confirmed
    `refresh_token_authentication_error` / `unsupported_token_type`
  - because the stream failed before login, the 2-symbol visible watchlist was
    not the cause of the halt

Code fix:

- added `schwab_prewarm_symbol_ttl_seconds` in `src/project_mai_tai/settings.py`
  with a default of `900` seconds (`15` minutes)
- updated `src/project_mai_tai/services/strategy_engine_app.py`
  - Schwab prewarm symbols now track `added_at`
  - prewarm symbols are pruned when they age past the TTL or once they become
    real active bot symbols
  - bot/runtime prewarm sets are kept in sync after pruning so expired prewarm
    names really leave the hidden Schwab stream target set
  - restore now re-seeds both `macd_30s` and `webull_30s` from current
    confirmed fallback symbols when an older snapshot explicitly contains an
    empty Webull handoff map; that prevents restart from clearing Webull while
    Schwab still receives the same current-session confirmed names
  - heartbeat details now publish:
    - `schwab_prewarm_symbols`
    - `schwab_stream_connected`
    - `schwab_stream_failures`
    - `schwab_stream_last_error`
  - Schwab data-halt reasons now distinguish auth failure from ordinary stale
    stream disconnects
  - forced resubscribe attempts are skipped when the Schwab stream client is
    explicitly disconnected, avoiding misleading fake resubscribe noise
- updated `src/project_mai_tai/broker_adapters/schwab.py`
  - HTTP error bodies now decode safely even when gzip-compressed
  - Schwab OAuth errors now preserve both `error` and `error_description`
    instead of collapsing to the shorter token
- updated `src/project_mai_tai/market_data/schwab_streamer.py`
  - streamer client now tracks `last_error` so auth failures can be surfaced in
    health/state output
- updated `src/project_mai_tai/services/control_plane.py`
  - listening-status detail now shows the exact data-halt reason when all halted
    symbols share one cause, so Schwab auth failures render clearly on the bot
    page instead of looking like a generic stale-feed issue

Operator meaning:

- if Schwab tokens are invalid, the live fix is still to reauthorize Schwab on
  the VPS; this patch does not bypass broker auth
- what this patch does is:
  - remove unnecessary hidden Schwab prewarm load
  - make the morning halt reason honest and actionable
  - prevent the UI from implying the bot is just randomly stale when the real
    problem is Schwab OAuth

Regression coverage added:

- expired Schwab prewarm symbols are pruned from the stream target set
- restart restore seeds Webull from current confirmed symbols even if an older
  snapshot stores `webull_30s: []`
- Schwab auth failures surface the OAuth-specific halt reason and do not trigger
  fake resubscribe attempts
- control-plane halt cards still render correctly for both Schwab and Webull

Validation:

- passed:
  - `.venv\Scripts\python.exe -m py_compile src/project_mai_tai/settings.py src/project_mai_tai/broker_adapters/schwab.py src/project_mai_tai/market_data/schwab_streamer.py src/project_mai_tai/services/strategy_engine_app.py src/project_mai_tai/services/control_plane.py tests/unit/test_control_plane.py tests/unit/test_schwab_prewarm_and_auth.py tests/unit/test_bot_handoff_restore_seed.py`
  - `.venv\Scripts\python.exe -m pytest tests/unit/test_schwab_prewarm_and_auth.py tests/unit/test_control_plane.py -k "schwab_data_halt_red_on_bot_page or webull_bot_page_uses_polygon_data_halt_wording or prewarm or auth_failure"`
  - `.venv\Scripts\python.exe -m pytest tests/unit/test_bot_handoff_restore_seed.py`

## 2026-04-24 Morning Follow-Up: Manual Stop Session Scope + Honest Schwab Auth Halt

Context:

- the user again reported `AUUD` / `CAST` showing on the 30-second bot in the
  morning and assumed they were stale leftovers from the prior day
- live VPS verification showed those names were actually current-session
  confirmations, not literal prior-day carryover:
  - `CAST` confirmed at `04:06:56 AM ET`
  - `AUUD` confirmed at `06:18:23 AM ET`
  - `IQST` later joined and both bots should have carried all three
- the actual cross-bot mismatch was different:
  - `Schwab 30 Sec Bot` had `AUUD`, `CAST`, `IQST`
  - `Webull 30 Sec Bot` initially had only `IQST`
  - `/api/bots` showed `webull_30s.manual_stop_symbols = ["AUUD", "CAST"]`
- control-plane access logs confirmed those exact bot-level Webull manual-stop
  actions existed:
  - `/bot/symbol/stop?strategy_code=webull_30s&symbol=AUUD`
  - `/bot/symbol/stop?strategy_code=webull_30s&symbol=CAST`

Root cause:

- per-bot and global manual-stop snapshots were only session-filtered by
  `created_at >= current_scanner_session_start_utc()`
- that timestamp-only rule is fragile during messy morning recovery because an
  old payload can be rewritten in the new session and then incorrectly survive
  restart/preload as if it belongs to the current trading day
- separately, the Schwab halt issue was confirmed again as broker auth failure,
  not symbol-count pressure:
  - live `strategy.log` repeated
    `refresh_token_authentication_error` / `unsupported_token_type`
  - the Schwab stream therefore never authenticated cleanly, so the red halt
    state was real but its displayed reason was still too generic

Code fix:

- updated `src/project_mai_tai/services/control_plane.py`
  - bot/global manual-stop snapshots now persist
    `scanner_session_start_utc`
  - snapshot restore/load now prefers exact session-marker match; it only falls
    back to `created_at` for older legacy snapshots that do not yet carry a
    marker
- updated `src/project_mai_tai/services/strategy_engine_app.py`
  - manual-stop preload now uses the same exact session-marker check, so stale
    per-bot stop payloads no longer leak into a new morning session just because
    they were rewritten after 4 AM
  - Schwab halt monitoring now derives a specific auth-failure reason from the
    streamer client error state
  - stale-symbol halts now use that auth-specific reason when appropriate
  - forced Schwab resubscribe attempts are skipped when the root problem is
    failed OAuth refresh, preventing noisy retry loops against dead credentials
  - heartbeat details now include Schwab stream connectivity plus the last
    stream error for easier morning diagnosis
- updated `src/project_mai_tai/market_data/schwab_streamer.py`
  - streamer now tracks `last_error`, clearing it on successful connect and
    recording the latest connection/auth failure

Live remediation applied immediately:

- resumed `AUUD` and `CAST` on `Webull 30 Sec Bot` so the bot immediately
  rejoined the current-session handoff without waiting for another deploy
- after resume, live `/api/bots` showed:
  - `macd_30s.watchlist = ["AUUD", "CAST", "IQST"]`
  - `webull_30s.watchlist = ["AUUD", "CAST", "IQST"]`
  - `webull_30s.manual_stop_symbols = []`

Operator meaning:

- the morning “leftover” symptom was a mix of two things:
  - current-day confirmed symbols that were legitimately present
  - stale bot-manual-stop state that incorrectly kept Webull from receiving the
    same current-day handoff after restart
- the Schwab red halt is still a real blocker until Schwab OAuth is reauthorized
  on the VPS; this patch makes that cause explicit instead of pretending the
  issue is generic stale ticks
- current evidence does **not** support “too many symbols caused the halt”; the
  live blocker is Schwab token/auth failure

Regression coverage added:

- `tests/unit/test_manual_stop_session_scope.py`
  - wrong-session `bot_manual_stop_symbols` markers are ignored by strategy
    preload
  - Schwab auth failure surfaces the OAuth-specific halt reason and skips
    forced resubscribe
- `tests/unit/test_control_plane.py`
  - persisted manual-stop snapshots now include `scanner_session_start_utc`
  - control-plane ignores manual-stop snapshots whose explicit session marker
    does not match the current scanner day

Validation:

- passed:
  - `.venv\Scripts\python.exe -m pytest tests/unit/test_control_plane.py -k "manual_stop_symbols or wrong_session_marker"`
  - `.venv\Scripts\python.exe -m pytest tests/unit/test_manual_stop_session_scope.py`
  - `.venv\Scripts\python.exe -m py_compile src/project_mai_tai/market_data/schwab_streamer.py src/project_mai_tai/services/strategy_engine_app.py src/project_mai_tai/services/control_plane.py tests/unit/test_control_plane.py tests/unit/test_manual_stop_session_scope.py`

## 2026-04-24 Alert Observability + Full-Day Alert CSV Export

Context:

- user flagged a real scanner observability gap while debugging `NTIP`
- live investigation had already proven:
  - `NTIP` was visible to Mai Tai in `top_gainers` by `08:32:09 AM ET`
  - `NTIP` was visible in `five_pillars` by `08:33:10 AM ET`
  - the first alert still did not fire until `08:37:20 AM ET`
  - the live alert carried `catchup_seed=True`, proving the alert engine
    backfilled a missed earlier seed instead of catching the move on time
- user asked for two things:
  - durable code-side observability so the next missed symbol is explainable
  - scanner alert export to CSV for the full current-day alert ledger, not just
    the visible table rows

Root cause / product gap:

- the alert engine did not persist any structured “candidate seen but blocked”
  diagnostics
- once an alert failed to fire on time, Mai Tai could only prove that the
  symbol existed in scanner universes, not which alert predicate blocked it on
  each cycle
- the scanner page only exposed `recent_alerts`, which is a short in-memory
  tape, so the operator could not export the full day’s alert history from the
  UI

Code fix:

- updated `src/project_mai_tai/strategy_core/momentum_alerts.py`
  - added `recent_rejections` tracking for near-candidate symbols that were
    seen by the alert engine but did not fire
  - each rejection now captures:
    - ticker / time / price / volume
    - blocking reasons
    - 5m / 10m squeeze metrics
    - 5m volume vs expected volume
    - whether the volume gate was open
  - rejection diagnostics persist through alert-engine snapshot export/restore
  - reset now clears the rejection ledger at the start of a new scanner session
- updated `src/project_mai_tai/services/strategy_engine_app.py`
  - added a `today_alerts` ledger that records the full current-session alert
    stream separately from the short `recent_alerts` UI tape
  - `today_alerts` persists in the `scanner_alert_engine_state` dashboard
    snapshot and restores across same-session restarts
  - new scanner sessions clear `today_alerts` automatically
- updated `src/project_mai_tai/services/control_plane.py`
  - loads the current-session `scanner_alert_engine_state` snapshot from the DB
  - scanner dashboard now exposes:
    - `today_alerts_count`
    - `alert_diagnostics`
    - `alert_diagnostics_count`
  - added `/scanner/alerts/export.csv`
    - exports the full current-day alert ledger, not just visible rows
  - scanner dashboard “Momentum Alerts” panel now includes an `Export Today CSV`
    button
  - added a new “Recent Alert Rejections” table so blocked candidates are
    visible directly in the scanner UI

Operator meaning:

- the scanner can now prove more than “this symbol was present but did not
  alert”
- for the next `NTIP`-type miss, Mai Tai will retain the recent blocking
  reasons instead of forcing a purely inferential postmortem
- alert CSV export is now suitable for same-day review in Excel because it
  includes the whole current-session alert ledger

Regression coverage added:

- `tests/unit/test_strategy_core.py`
  - near-threshold candidates now record recent rejection reasons
- `tests/unit/test_control_plane.py`
  - scanner dashboard renders the full-day alert export affordance
  - scanner alerts API exposes today-count + diagnostics
  - `/scanner/alerts/export.csv` returns the full persisted current-day alert
    ledger

Validation:

- passed:
  - `.venv\Scripts\python.exe -m pytest tests/unit/test_strategy_core.py -k "alert_engine_records_recent_rejection_reasons_for_near_candidates or alert_engine_backfills_missed_spike_when_late_squeeze_is_obvious or alert_engine_history_is_compact_and_backwards_compatible"`
  - `.venv\Scripts\python.exe -m pytest tests/unit/test_control_plane.py -k "control_plane_overview_and_dashboard_render or decision_tape_uses_polygon_wording_for_webull_bot"`

## 2026-04-24 Schwab Quiet-Symbol False Data Halt

Context:

- user reported another `DATA HALT` on the Schwab 30s bot around `11:06 AM ET`
- UI showed halted symbols `APLZ` and `PBM`
- live VPS checks showed:
  - top-level strategy heartbeat stayed `healthy`
  - `schwab_stream_connected=true`
  - other Schwab symbols continued receiving updates
  - no open positions existed during the halt
- strategy log showed the exact sequence:
  - `11:05:15 AM ET` `APLZ` went stale and recovered
  - `11:06:05 AM ET` `APLZ` + `PBM` were marked stale again
  - `11:06:17 AM ET` both recovered after forced resubscribe

Root cause:

- this was not a full Schwab auth outage or websocket-wide disconnect
- it was a symbol-specific false positive in the stale-health logic
- Mai Tai treated a flat watchlist symbol as hard-stale after about `30s`
  without a fresh Schwab trade/quote update
- for quieter names like `APLZ` / `PBM`, a `30-40s` silent window can happen
  naturally even while the broader Schwab stream is healthy
- because no-position symbols used the same halt threshold as open positions,
  the bot page went red for normal quiet tape

Code fix:

- updated `src/project_mai_tai/settings.py`
  - added `schwab_stream_symbol_stale_after_seconds_without_position`
  - defaulted to `90.0`
- updated `src/project_mai_tai/services/strategy_engine_app.py`
  - `_schwab_data_halt_stale_after_seconds()` is now position-aware
  - open positions still use the stricter existing protection
  - flat watchlist symbols now require the longer no-position stale window
    before entering runtime `DATA HALT`
- updated `tests/unit/test_strategy_engine_service.py`
  - existing stale-watchlist test now pins the no-position threshold low when
    it wants to prove a halt
  - added regression coverage that a flat Schwab watchlist symbol with a
    `~40s` quiet gap no longer trips `DATA HALT` under the new defaults

Operator meaning:

- true protection is preserved for live open positions
- quiet Schwab names that are merely not printing for `30-40s` should no
  longer flash the whole Schwab 30s bot red
- if a flat symbol really goes dark for longer than the extended window, the
  halt still happens

Validation:

- passed:
  - `.venv\Scripts\python.exe -m pytest tests/unit/test_strategy_engine_service.py -k "stale_schwab_watchlist_symbol_without_open_position or gives_flat_schwab_watchlist_symbol_extended_stale_window or uses_fallback_quotes_for_stale_schwab_open_positions"`
  - `.venv\Scripts\python.exe -m py_compile src/project_mai_tai/settings.py src/project_mai_tai/services/strategy_engine_app.py tests/unit/test_strategy_engine_service.py`

Follow-up hotfix:

- first VPS deploy exposed one missed helper call site:
  - _schwab_stream_disconnect_has_exceeded_grace() still called the
    position-aware stale helper without the new keyword argument
  - result: the strategy service restarted once on deploy with a TypeError,
    then systemd brought it back
- hotfix updated the disconnect-grace helper signature and caller so the
  position-aware stale window is applied consistently for both:
  - symbol-specific stale checks
  - stream-disconnect grace checks

## 2026-04-24 Webull 30s Aggregate-Bar Wiring Fix

Context:

- live Webull 30 Sec Bot under-traded badly versus both the market and an
  on-demand Polygon replay
- live bot had only 2 order attempts today while a replay on the same
  watchlist produced 37 simulated trades
- code review showed the Webull runtime was not actually wired like the replay:
  - webull_30s hardcoded use_live_aggregate_bars=False
  - webull_30s hardcoded live_aggregate_fallback_enabled=False
  - market-data gateway only enabled Massive live aggregate streaming for the
    global flag or the Schwab 30s aggregate flag

Root cause:

- the Webull bot was running on the generic Polygon tick path, but not on the
  Polygon live aggregate-bar path that best matches the replayed 30s engine
- that meant the live Webull runtime and the replay were not actually exercising
  the same bar-ingestion path

Code fix:

- updated src/project_mai_tai/settings.py
  - added Webull-specific live aggregate settings:
    - strategy_webull_30s_live_aggregate_bars_enabled
    - strategy_webull_30s_live_aggregate_fallback_enabled
    - strategy_webull_30s_live_aggregate_stale_after_seconds
  - defaulted Webull aggregate bars/fallback to enabled with a 3s stale window
- updated src/project_mai_tai/market_data/gateway.py
  - Massive live aggregate subscription is now enabled when Webull 30s aggregate
    bars are enabled, not just for the old global/Schwab path
- updated src/project_mai_tai/services/strategy_engine_app.py
  - webull_30s now uses live aggregate bars and aggregate-to-tick fallback
    through its own settings instead of being hardcoded off
- updated 	ests/unit/test_webull_30s_bot.py
  - added regression coverage that Webull 30s defaults to live aggregate bars
    with fallback
  - added regression coverage that the market-data gateway enables the Massive
    aggregate stream when only Webull 30s requires it

Operator meaning:

- live Webull 30s now consumes the Polygon live bar path the replay was using,
  while still falling back to trade ticks if live aggregates stall
- this closes the biggest runtime wiring gap between Polygon replay trades
  and live Webull does almost nothing

Validation:

- local:
  - passed:
    - `.venv\Scripts\python.exe -m pytest tests/unit/test_webull_30s_bot.py -q`
    - `.venv\Scripts\python.exe -m py_compile src/project_mai_tai/settings.py src/project_mai_tai/market_data/gateway.py src/project_mai_tai/services/strategy_engine_app.py tests/unit/test_webull_30s_bot.py`
  - note:
    - two older aggregate-focused tests in `tests/unit/test_strategy_engine_service.py`
      were already red in the worktree and were not introduced by this patch
- VPS deploy:
  - PR `#45` merged to `main`
  - VPS pulled `main` and restarted:
    - `project-mai-tai-market-data.service`
    - `project-mai-tai-strategy.service`
  - post-deploy verification:
    - `/health` returned `healthy`
    - `market-data-gateway` healthy with `active_symbols=17`
    - `strategy-engine` healthy with `watchlist_size=17`, `bot_count=2`,
      `schwab_stream_connected=true`, and no stale Schwab symbols
    - both `Schwab 30 Sec Bot` and `Webull 30 Sec Bot` came back on the same
      17-symbol live watchlist with healthy `data_health`

API note:

- the shared multi-bot JSON endpoint remains `GET /api/bots`
- per-bot JSON endpoints are:
  - `GET /bot` for `macd_30s`
  - `GET /botwebull` for `webull_30s`
- there is no separate `/api/botwebull` route in the current control plane

## 2026-04-24 Webull Last Bot Tick Snapshot Fix

Context:

- Webull 30 Sec Bot page showed:
  - `Listening`
  - fresh `Last Market Data`
  - fresh `Last Decision`
  - but empty `Last Bot Tick`
- live VPS payload confirmed the exact gap:
  - `macd_30s.last_tick_at` contained many symbol timestamps
  - `webull_30s.last_tick_at` was `{}` in `/api/bots`

Root cause:

- control-plane renders `Last Bot Tick` from the bot snapshot field `last_tick_at`
- Schwab updates that field visibly because the Schwab queue drain republishes
  strategy-state snapshots whenever stream events are seen
- the generic market-data path used by Webull only republished strategy-state
  snapshots when:
  - intents were generated, or
  - completed bars were flushed later
- result:
  - Webull runtime could be actively handling Polygon trade/live-bar events and
    updating in-memory `_last_tick_at`
  - but control-plane never saw those timestamps if no new intents happened

Code fix:

- updated `src/project_mai_tai/services/strategy_engine_app.py`
  - added a throttled helper that republishes `strategy-state` snapshots for
    generic bot activity at most every 5 seconds
  - wired generic `trade_tick` and `live_bar` handling to use that helper when
    non-Schwab bots are targeted but no intents are generated

Operator meaning:

- Webull `Last Bot Tick` now reflects real Polygon bot activity instead of
  staying blank until an intent happens
- this is a control-plane visibility fix, not a strategy-behavior change

Validation:

- passed:
  - `.venv\Scripts\python.exe -m pytest tests/unit/test_strategy_engine_service.py -k "live_bar_publishes_strategy_snapshot_for_generic_bot_activity_without_intents" -q`
  - `.venv\Scripts\python.exe -m py_compile src/project_mai_tai/services/strategy_engine_app.py tests/unit/test_strategy_engine_service.py`
- note:
  - an older fallback-routing test in `tests/unit/test_strategy_engine_service.py`
    remains out of sync with current generic-market-data selection logic and
    was not used as a blocker for this targeted visibility fix

## 2026-04-24 Webull Last Bot Tick Forced-Bar-Close Fix

Context:

- After the snapshot publish fix above, the raw live `strategy-state` payload
  still showed:
  - `webull_30s.recent_decisions` updating with fresh current-session bar times
  - but `webull_30s.last_tick_at` remained empty
- `Schwab 30 Sec Bot` did not show the same problem because its runtime also
  receives direct Schwab tick timestamps continuously.

Root cause:

- Webull/Polygon was producing many of its current 30-second decisions through
  the runtime `flush_completed_bars()` path
- that path closes due bars on schedule and evaluates them, but it did not
  stamp `last_tick_at` for the symbol before persisting the strategy snapshot
- result:
  - fresh decisions and bar counts were visible
  - `Last Bot Tick` still rendered as blank because the backing snapshot field
    stayed `{}` for `webull_30s`

Code fix:

- updated `src/project_mai_tai/services/strategy_engine_app.py`
  - in `StrategyBotRuntime.flush_completed_bars()`, each symbol whose bar is
    force-closed now records `last_tick_at` with the current normalized runtime
    clock before `_evaluate_completed_bar(...)`
- added `tests/unit/test_webull_last_bot_tick.py`
  - dedicated regression test proving a Webull symbol evaluated through
    `flush_completed_bars()` now appears in `bot.summary()["last_tick_at"]`

Operator meaning:

- `Last Bot Tick` for Webull no longer stays blank just because the bot is
  evaluating through timed 30-second bar closes instead of direct intent-
  generating ticks
- this is a visibility/state correctness fix only; it does not change any
  entry or exit logic

## 2026-04-24 Webull Tick-Built Parity Revert

Context:

- live trading review showed `Schwab 30 Sec Bot` remained active while
  `Webull 30 Sec Bot` stayed unusually quiet after the early morning
- code review confirmed an important runtime asymmetry:
  - `macd_30s` defaults to `strategy_macd_30s_live_aggregate_bars_enabled = false`
  - `webull_30s` had been changed to default
    `strategy_webull_30s_live_aggregate_bars_enabled = true`
  - the 30-second entry config still has `entry_intrabar_enabled = false`
- result:
  - Schwab built and aged its 30-second structure directly from tick flow
  - Webull skipped `on_trade()` for most live ticks whenever aggregate bars were
    healthy, relying instead on the aggregate-bar path plus fallback

Why this mattered:

- the intended comparison was “same 30-second strategy stack, different broker
  and data source”
- the aggregate-first Webull default violated that expectation by changing the
  bar-building path itself, not just the source of ticks
- that made Webull behave less like “Polygon tick-built 30s” and more like
  “Massive aggregate bars with occasional tick fallback”

Code fix:

- updated `src/project_mai_tai/settings.py`
  - reverted the default for
    `strategy_webull_30s_live_aggregate_bars_enabled` back to `false`
- updated `tests/unit/test_webull_30s_bot.py`
  - Webull now asserts tick-built 30-second parity by default
  - the aggregate-stream gateway test now explicitly enables the Webull
    aggregate setting when it wants to prove that optional path

Operator meaning:

- Webull now matches Schwab much more closely in bar construction:
  - Polygon trade ticks build the 30-second series directly by default
  - live aggregate bars remain available as an explicit opt-in path later if
    needed
- if Webull still under-trades after this parity revert, the next root-cause
  layer is more likely real strategy/data behavior rather than a hidden bar-path
  mismatch

## 2026-04-24 - Bot page live-symbol UI cap fix

Context:

- both `Schwab 30 Sec Bot` and `Webull 30 Sec Bot` could be tracking more live
  symbols than the sidebar actually showed
- operator saw only 10 symbols in `Live Symbols` even when the runtime watchlist
  count was 18

Root cause:

- the shared control-plane bot-page renderer was slicing the watchlist before it
  built the sidebar live-symbol list:
  - `for symbol in bot["watchlist"][:10]:`
- this was a UI-only cap in `src/project_mai_tai/services/control_plane.py`,
  affecting both 30-second bot pages equally

Fix:

- removed the hard `[:10]` slice so the sidebar now renders the full live
  watchlist for each bot
- added a regression test in `tests/unit/test_control_plane.py` that seeds a
  12-symbol watchlist and verifies all symbols render on `/bot/30s`

Operator meaning:

- `Live Symbols` on both bot pages should now reflect the actual current bot
  watchlist instead of silently truncating at 10
- this does not change bot behavior or handoff logic; it only fixes the control
  plane view so operators can trust the displayed live list

## 2026-04-24 - 30s completed-bar wait escalation and watchdog

Context:

- operators flagged the Decision Tape placeholder
  `live in bot; waiting for next completed 30s trade bar to evaluate`
  as too vague for live trading
- the old placeholder did not distinguish:
  - a normal between-bar wait on an actively ticking symbol
  - a dangerous case where a live symbol had gone too long without producing a
    completed 30-second trade bar

Fix:

- updated `src/project_mai_tai/services/control_plane.py`
  - normal waiting now shows elapsed time since the last live tick, e.g.
    `waiting for next completed 30s trade bar to evaluate (18s since last Schwab tick)`
  - if the wait stretches past 45 seconds, the reason now escalates to a
    clearer warning
  - if the wait stretches past 90 seconds, the placeholder escalates to
    `critical` with:
    `no completed 30s trade bar for ... after the last live ... tick - verify tape/bar flow now`
- added targeted coverage in `tests/unit/test_control_plane.py` for:
  - the normal elapsed-time placeholder
  - the stalled/critical completed-bar wait path

Operator meaning:

- a plain `pending` completed-bar wait is now easier to read and less scary
- a long wait is now explicitly visible as a possible bar-flow problem instead
  of looking like a harmless placeholder
- this is a control-plane observability fix; it does not change trading logic,
  entry rules, or how bars are built

## 2026-04-24 - Suppress after-hours flat Schwab stale halts

Context:

- the Schwab 30 Sec Bot could still flip into a red `DATA HALT` after the
  strategy trading window had already ended, even with no open positions
- this created scary false alerts such as:
  - stale/disconnected symbols at `6:20 PM ET`
  - recent decision rows already saying `outside trading hours`
  - no emergency-close exposure because the bot was flat
- operator also observed that this could be mistaken for a missed-bar or
  in-session failure when it was actually an after-hours quiet-tape condition

Root cause:

- `_monitor_schwab_symbol_health()` enforced Schwab stale/data-halt escalation
  for active watchlist symbols regardless of whether their owning runtime was
  still inside its configured trading hours
- for `macd_30s`, that meant flat symbols could still become `critical` after
  `6:00 PM ET` purely from quiet/noisy after-hours tape behavior

Fix:

- updated `src/project_mai_tai/services/strategy_engine_app.py`
  - added `_schwab_symbol_should_enforce_data_halt(...)`
  - flat symbols now only enforce Schwab stale/data-halt escalation while at
    least one owning Schwab runtime is still inside its configured trading
    window
  - open positions still always enforce stale protection, regardless of clock
- added focused test coverage in `tests/unit/test_schwab_after_hours_stale_halt.py` for:
  - in-session flat-symbol stale halt still occurs
  - after-hours flat symbols do not escalate into `DATA HALT`

Operator meaning:

- after the Schwab 30s trading window ends, quiet flat symbols should no longer
  poison the whole bot page red just because their tape stops printing
- real protection remains in place for open positions and in-session stale
  failures

## 2026-04-27 - Pending next fix items from live scanner review

Context:

- operator reviewed `YAAS`, which moved from sub-$1 to above `$1` very quickly
- live trace showed:
  - visible in `five_pillars` and `top_gainers` by about `07:01 AM ET`
  - `VOLUME_SPIKE` at `07:06:49 AM ET`
  - `SQUEEZE_5MIN` at `07:07:03 AM ET`
  - no confirm until `07:12:08 AM ET`, when a second squeeze arrived
- operator also called out that `Decision Tape` remains noisy for manual
  validation when current confirmed count is zero but historical blocked rows
  still dominate the table

Pending fix decisions:

- lower `MomentumConfirmedConfig.extreme_mover_min_day_change_pct`
  - current behavior: `PATH_C_EXTREME_MOVER` requires `>= 50%` day change for a
    single-squeeze confirm
  - requested next change: reduce this threshold from `50.0` to `30.0`
  - reason: a name like `YAAS` already had enough operator-visible momentum by
    `07:07 AM ET`, but current policy forced it to wait for `PATH_B_2SQ`
- tighten `Decision Tape` default filtering
  - target behavior: show current actionable confirmed/live symbols by default
  - avoid mixing raw historical blocked rows and non-actionable past symbols
    into the primary operator validation view

Operator meaning:

- this is not a data outage or handoff bug; it is a policy/UI follow-up item
- next agent should treat both items as active queued fixes, not as open
  questions

## 2026-04-27 - Trade coach live-session follow-up

Live result:

- operator confirmed a real closed `macd_30s` trade in `USEG`
- cycle details on control plane:
  - entry: `2026-04-27 08:01:30 AM ET`
  - exit: `2026-04-27 08:01:43 AM ET`
  - path: `P4_BURST`
  - result: stopped out / losing close

What initially went wrong:

- `recent_trade_coach_reviews` stayed empty even though the trade was fully
  closed
- root cause was operational, not pairing logic:
  - no `project-mai-tai-trade-coach-smoke` unit was running
  - first manual smoke attempt failed because `trader` could not read
    `/etc/project-mai-tai/project-mai-tai.env`
  - that meant the coach started without the API key and exited immediately

What was confirmed:

- rerunning the coach with env sourced under `sudo` successfully backfilled the
  closed cycle
- `/api/bots` then showed the persisted review under `macd_30s`:
  - symbol: `USEG`
  - verdict: `good`
  - action: `exit`
  - confidence: `0.9`
  - summary:
    `Good execution on a valid setup conforming to P4_BURST path. Exited on hard stop timely to manage risk.`

Follow-up change prepared:

- repo now includes a dedicated manual-start
  `ops/systemd/project-mai-tai-trade-coach.service`
- service behavior:
  - reads the normal VPS env file as root via systemd
  - forces `MAI_TAI_TRADE_COACH_ENABLED=true` only for the service process
  - leaves shared VPS env flags disabled by default outside that unit
  - uses a longer request timeout and shorter poll interval for live-session use

Operator meaning:

- future closed trades today should be reviewed automatically once that service
  is installed on the VPS and started
- stopping that service returns trade coach to fully disabled behavior without
  changing the shared env defaults

Live-session service fix and result:

- initial dedicated service start still exited immediately with repeated:
  - `trade coach disabled; exiting`
- root cause:
  - shared VPS env file still contained `MAI_TAI_TRADE_COACH_ENABLED=false`
  - for this unit, the shared env file value still beat the inline
    `Environment=MAI_TAI_TRADE_COACH_ENABLED=true` attempt
- fix:
  - updated
    `ops/systemd/project-mai-tai-trade-coach.service`
  - service now forces:
    `MAI_TAI_TRADE_COACH_ENABLED=true`
    directly in `ExecStart`
  - restart policy was also tightened from `Restart=always` to
    `Restart=on-failure`
- VPS deployment / verification:
  - local / GitHub / VPS `main` advanced to `1ec069d`
  - service now stays running normally on VPS:
    - `project-mai-tai-trade-coach.service`
  - service log showed:
    - `trade coach starting for macd_30s, webull_30s`
    - `trade coach reviewed 1 completed trade cycles`
- live result after the fix:
  - `/api/bots` now shows two persisted `macd_30s` coach reviews for `USEG`
  - the newly auto-reviewed cycle was:
    - entry: `2026-04-27 08:08:32 AM ET`
    - exit: `2026-04-27 08:08:41 AM ET`
    - verdict: `good`
    - action: `exit`
    - confidence: `0.9`
    - summary:
      `Good trade on P5_PULLBACK setup entered and exited on time with hard stop loss management. Setup was good quality with favorable indicators; execution was timely and within rules.`

## 2026-04-27 - Trade coach bot-page visibility

Context:

- operators could verify trade coach output in `/api/bots`
- but there was still no simple bot-page section showing recent reviews beside
  completed positions and order history

UI follow-up:

- updated
  `src/project_mai_tai/services/control_plane.py`
- bot detail pages now render a dedicated `Trade Coach Reviews` table using the
  already-persisted `recent_trade_coach_reviews` slice for that bot
- current columns:
  - review time
  - ticker
  - verdict
  - action
  - confidence
  - concise coach summary

Important scope note:

- this is a visibility-only control-plane improvement
- no change was made to:
  - trade pairing
  - coach prompting
  - strategy behavior
  - OMS behavior
- the page is simply surfacing the reviews that were already being generated

Validation:

- passed:
  - `.venv\\Scripts\\python.exe -m pytest tests\\unit\\test_control_plane.py -k "bot_page_renders_simple_trade_summary_table or reports_schwab_live_wiring or webull_30s_page_uses_polygon_data_halt_labels" -q`
  - `.venv\\Scripts\\python.exe -m py_compile src/project_mai_tai/services/control_plane.py tests/unit/test_control_plane.py`

## 2026-04-27 - Trade coach payload tightening

Context:

- initial trade coach output proved the live pipeline worked
- but reviews on the bot page were still mostly a short summary line, which made
  them feel repetitive and too praise-heavy
- the persisted AI payload already contained richer critique fields, but the
  control plane was not surfacing most of them

Changes:

- updated `src/project_mai_tai/ai_trade_coach/repository.py`
  - persisted review payloads now also include a compact `trade_snapshot`
  - snapshot fields include:
    - `path`
    - `entry_time`
    - `exit_time`
    - `entry_price`
    - `exit_price`
    - `quantity`
    - `pnl`
    - `pnl_pct`
    - `exit_summary`
- updated `src/project_mai_tai/ai_trade_coach/service.py`
  - tightened the model instruction to:
    - separate outcome from quality
    - avoid generic praise
    - use `mixed` more honestly when evidence is mixed
    - cite concrete path/timing/scale/stop/bar facts in reasons and advice
- updated `src/project_mai_tai/services/trade_coach_app.py`
  - expanded the rulebook with an explicit review rubric for:
    - `good`
    - `mixed`
    - `bad`
    - `skip`
- updated `src/project_mai_tai/services/control_plane.py`
  - `/api/bots` and bot pages now surface richer coach fields:
    - `execution_timing`
    - `setup_quality`
    - `should_have_traded`
    - `key_reasons`
    - `rule_hits`
    - `rule_violations`
    - `next_time`
    - `trade_snapshot` facts when available
  - bot-page `Trade Coach Reviews` table now shows:
    - trade facts
    - verdict + action + confidence
    - should-have-traded flag
    - why / violations / next-time notes

Important scope note:

- no schema migration was required because the richer facts live inside the
  existing JSON `payload`
- older reviews may not have the new `trade_snapshot` block, but new reviews
  will

Validation:

- passed:
  - `.venv\\Scripts\\python.exe -m pytest tests\\unit\\test_trade_coach_service.py tests\\unit\\test_trade_coach_repository.py tests\\unit\\test_control_plane.py -q`
  - `.venv\\Scripts\\python.exe -m py_compile src/project_mai_tai/ai_trade_coach/repository.py src/project_mai_tai/ai_trade_coach/service.py src/project_mai_tai/services/trade_coach_app.py src/project_mai_tai/services/control_plane.py tests/unit/test_trade_coach_repository.py tests/unit/test_control_plane.py`

## 2026-04-27 - Trade coach review versioning and refresh

Context:

- after the richer payload launch, older reviews still rendered with missing
  trade facts because they were created before `trade_snapshot` and the expanded
  rubric fields existed
- leaving the system mixed between old and new review shapes would make the bot
  page inconsistent and block meaningful comparison of newer review quality

Changes:

- updated `src/project_mai_tai/ai_trade_coach/models.py`
  - trade coach config now carries a review contract version:
    - `review_schema_version = "trade_coach_v2"`
  - review payload now requires additional structured critique fields:
    - `coaching_focus`
    - `execution_quality`
    - `outcome_quality`
    - `should_review_manually`
- updated `src/project_mai_tai/ai_trade_coach/service.py`
  - tightened the review schema and model instruction around:
    - single primary coaching focus
    - separate setup / execution / outcome scoring
    - manual-review flag for ambiguous cases
  - normalization now supports these new fields
- updated `src/project_mai_tai/ai_trade_coach/repository.py`
  - persisted payloads now include:
    - `schema_version`
  - `save_review(...)` now upserts by:
    - `review_type`
    - `cycle_key`
    instead of always inserting a brand-new row
  - review selection now refreshes older incomplete reviews automatically when:
    - schema version is old or missing
    - `trade_snapshot` is missing
    - required richer fields are missing
- updated `src/project_mai_tai/services/control_plane.py`
  - bot pages and `/api/bots` now surface the new critique fields:
    - `coaching_focus`
    - `execution_quality`
    - `outcome_quality`
    - `should_review_manually`

Operator meaning:

- restarting the trade coach service on the VPS now allows older same-day
  reviewed cycles to be refreshed in place with the newer richer contract
- this avoids needing a schema migration or duplicate review rows

Validation:

- passed:
  - `.venv\\Scripts\\python.exe -m pytest tests\\unit\\test_trade_coach_service.py tests\\unit\\test_trade_coach_repository.py tests\\unit\\test_control_plane.py -q`
    - `33 passed`
  - `.venv\\Scripts\\python.exe -m py_compile src/project_mai_tai/ai_trade_coach/models.py src/project_mai_tai/ai_trade_coach/repository.py src/project_mai_tai/ai_trade_coach/service.py src/project_mai_tai/services/control_plane.py`

## 2026-04-27 - Shared historical warmup ordering fix for Schwab/Webull 30s

Context:

- operator flagged that `Webull 30 Sec Bot` again produced only a few early
  order attempts while `Schwab 30 Sec Bot` continued trading actively
- live VPS comparison showed this was not just "different data"
- the two runtimes were carrying materially different internal bar state on the
  same current symbols:
  - very different cumulative bar counts
  - very different VWAP values
  - very different `active_reference_5m_volume`
  - different lifecycle states on the same names

Root cause:

- both 30-second bots seed historical warmup bars from the shared
  `MassiveSnapshotProvider`
- `fetch_historical_bars()` was trusting provider order and returning bars as
  received
- `StrategyBotRuntime.seed_bars()` was also trusting incoming order and seeding
  the builder directly
- if historical bars arrive newest-first, the last seeded bar becomes stale /
  old, and the 30s bar builder can then manufacture long stretches of flat
  synthetic gap bars before the next live trade
- that poisons VWAP / short-volume / chop / lifecycle state, especially on the
  Polygon-driven `webull_30s` runtime, but it can also distort the first
  bootstrap period on the Schwab bot because Schwab uses the same historical
  warmup source before live ticks take over

Fix applied:

- updated
  `src/project_mai_tai/market_data/massive_provider.py`
  so historical warmup bars are explicitly sorted chronologically by timestamp
  before returning
- updated
  `src/project_mai_tai/services/strategy_engine_app.py`
  so `StrategyBotRuntime.seed_bars()` also sorts bars defensively before
  hydrating the bar builder
- added focused tests in
  `tests/unit/test_historical_bar_seed_order.py`
  covering:
  - chronological sorting in the Massive historical provider
  - defensive chronological sorting inside runtime seeding even when bars are
    supplied out of order

Why this matters:

- this is a shared bootstrap-path fix, not just a Webull-only patch
- expected impact:
  - Webull 30s should stop carrying polluted / stale-seeded bar history
  - early-session Schwab warmup should also be cleaner because the shared
    Polygon/Massive historical seed is no longer allowed to land out of order

Validation:

- passed:
  - `.venv\\Scripts\\python.exe -m pytest tests\\unit\\test_historical_bar_seed_order.py tests\\unit\\test_market_data_gateway.py tests\\unit\\test_webull_30s_bot.py`
  - `.venv\\Scripts\\python.exe -m py_compile src/project_mai_tai/market_data/massive_provider.py src/project_mai_tai/services/strategy_engine_app.py tests/unit/test_historical_bar_seed_order.py`

## 2026-04-27 - Expose listening_status in shared /api/bots payload

Context:

- operator saw a transient red `DATA HALT` bot-page screenshot during a restart
  window, then later healthy bot pages
- follow-up verification showed `/bot` and `/botwebull` already carried correct
  `listening_status`, but `/api/bots` did not expose the same top-card status
  block
- that made shared payload checks look thinner than the live per-bot pages and
  increased confusion during validation

Fix applied:

- updated `src/project_mai_tai/services/control_plane.py` so `/api/bots`
  attaches `listening_status` using the same `_build_bot_listening_status(...)`
  helper as `/bot` and `/botwebull`
- updated `tests/unit/test_control_plane.py` to assert that the Webull bot in
  `/api/bots` now includes the same `DATA HALT` listening status already proven
  on `/botwebull`

Why this matters:

- multi-bot monitors and direct payload checks now see the same top-card
  listening state as the rendered per-bot pages
- this reduces false suspicion that one API says "healthy" while the bot page
  says "halted" or vice versa

