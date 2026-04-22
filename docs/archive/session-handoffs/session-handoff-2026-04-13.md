# Session Handoff - 2026-04-13

## What Changed

The 30s research path has now pivoted away from trying to keep loosening reclaim.

External research was validated with both:

- Perplexity API
- OpenAI Platform API

That research agreed with the replay evidence:

- reclaim loosening keeps adding bad trades
- location relaxation is not solving the core issue
- the next idea should be a more selective setup shape, not a wider reclaim gate

## New Research Bot

A new research-only 30s bot path has been added:

- `macd_30s_retest`

It is wired through:

- settings
- runtime registry
- strategy engine service
- 30s compare script

Default status:

- disabled by default
- intended for replay and research first

## Retest Logic Shape

Current design is intentionally conservative:

1. require recent breakout history
2. require a clean retest of the prior breakout level
3. do not buy the retest bar immediately
4. arm the setup
5. only buy on the next-bar break of the retest high

This is meant to support the user goal:

- fewer trades
- cleaner entries
- win rate over trade count

## Validation Done

Passed targeted checks:

- retest entry-engine unit tests
- retest runtime/service wiring tests
- py_compile on touched strategy/runtime files

## Important Current Truth

This is an implementation slice, not a replay verdict yet.

What is still needed next:

1. replay `macd_30s_retest` against the same combined universe
2. compare against frozen `macd_30s_reclaim`
3. tune retest only if win quality improves without opening the floodgates

## Recommended Next Step

Run replay comparisons for:

- `macd_30s`
- `macd_30s_reclaim`
- `macd_30s_retest`

Use the same combined universe standard:

- original April 1-2 recovered set
- April 8 top gainers

Judge the retest path by:

- resolved good rate
- bad trade count
- per-symbol cleanliness

Do not optimize for total trade count.

## Current Retest Research Baseline

After the first replay plus external research, the retest path moved to a slightly wider but still conservative setup-recognition baseline:

- `pretrigger_retest_breakout_window_bars = 6`
- `pretrigger_retest_min_breakout_pct = 0.0025`
- `pretrigger_retest_max_pullback_from_breakout_pct = 0.04`
- `pretrigger_retest_level_tolerance_pct = 0.005`

The important choice here was to keep candle rules unchanged for now. A softer candle version produced a little more activity, but it also added more bad trades, which does not fit the user goal of a small number of cleaner entries.

## Latest Combined-Set Benchmark

The latest apples-to-apples research comparison now disables ticker-loss pause by default inside [compare_30s_research_family.py](/C:/Users/kkvkr/OneDrive/Documents/GitHub/project-mai-tai/scripts/compare_30s_research_family.py) so replay results measure entry quality instead of risk throttling.

Output:

- [compare_30s_research_family_nopause.json](/C:/Users/kkvkr/OneDrive/Documents/GitHub/project-mai-tai/tmp_replay/compare_30s_research_family_nopause.json)

Current result:

- `macd_30s_reclaim`: `52 good / 65 bad`
- `macd_30s_retest`: `1 good / 5 bad`

Interpretation:

- reclaim stays frozen as the working benchmark
- retest is still the right development direction for the user goal, but it is not ready
- the next retest fix should target breakout-context recognition, not more broad loosening of touch, hold, or candle rules

Most important retest blockers in the no-pause comparison:

- `pretrigger retest breakout not ready`
- `pretrigger retest touch not ready`
- `pretrigger retest hold not ready`
- `pretrigger retest candle not ready`

Recommended next implementation step:

- redesign how retest recognizes a valid breakout impulse before the retest
- likely around breakout range expansion and breakout-bar quality, not a wider retest zone by itself

## Follow-Up Retest Result

That breakout-context pass has now been implemented in [entry.py](/C:/Users/kkvkr/OneDrive/Documents/GitHub/project-mai-tai/src/project_mai_tai/strategy_core/entry.py):

- breakout bar must be bullish
- breakout can qualify by strong high-break instead of close-only
- breakout candidate is selected by strength, not first-match order

Latest replay:

- [compare_30s_retest_nopause.json](/C:/Users/kkvkr/OneDrive/Documents/GitHub/project-mai-tai/tmp_replay/compare_30s_retest_nopause.json)

Latest retest combined-set result:

- `1 good / 4 bad / 1 open`

Conclusion from this pass:

- slight cleanliness improvement
- not a big enough step
- the next retest target should be hold/support modeling, not more breakout loosening

## Reclaim Cleanup Update

Reclaim is now the main path again. The first cleanup pass targeted reclaim losers that were more extended above EMA9 and VWAP than the winners.

That change is now promoted into the reclaim research baseline in [trading_config.py](/C:/Users/kkvkr/OneDrive/Documents/GitHub/project-mai-tai/src/project_mai_tai/strategy_core/trading_config.py):

- `pretrigger_reclaim_max_extension_above_ema9_pct = 0.04`
- `pretrigger_reclaim_max_extension_above_vwap_pct = 0.04`

Combined-universe no-pause result for that stricter reclaim profile:

- before: `52 good / 65 bad`
- after: `50 good / 61 bad`

Interpretation:

- reclaim remains the working benchmark
- this is a real quality improvement, not a trade-count expansion
- next reclaim work should keep targeting bad-trade reduction, one evidence-based filter at a time

## Scanner Catalyst Source Handling Update

Path A itself was not the main failure on `BTBD`. The real issue was the catalyst-source path:

- Alpaca had no company-specific article in the first decision window
- Mai Tai cached that empty answer too long
- when Alpaca caught up later, Path A could stay blind longer than it should

The catalyst engine now handles that more safely:

- empty/no-article news results retry quickly
- real article results still keep the longer cache
- the scanner now carries explicit miss diagnostics instead of a vague blank news state

The new scanner-facing statuses are:

- `no_articles`
- `generic_only`
- `non_qualifying_articles`
- `fetch_failed`
- `provider_disabled`

This means we can now distinguish:

- provider had no article yet
- provider only returned roundup coverage
- provider had articles but none matched Path A
- provider request failed
- provider credentials were unavailable

## Reclaim Focus Universe

The five biggest reclaim-quality destroyers are now excluded from the active reclaim tuning universe, but their replay data is still kept for future analysis:

- `JEM`
- `CYCN`
- `BFRG`
- `UCAR`
- `BBGI`

That leaves the active reclaim focus universe at:

- `12` symbol-days
- `12` unique stocks

With the current reclaim baseline on that focus universe:

- `32 good`
- `22 bad`
- `10 open`
- resolved good rate `0.5926`

## Latest Reclaim Tightening

On the focus universe, the next EMA/VWAP pass showed:

- tighter VWAP alone: no meaningful change
- forcing the reclaim floor exactly at EMA9: no meaningful change
- tighter EMA9 extension cap: small but real improvement

So the reclaim baseline is now tightened again:

- `pretrigger_reclaim_max_extension_above_ema9_pct = 0.02`
- `pretrigger_reclaim_max_extension_above_vwap_pct = 0.04`

Focus-universe result for that tighter EMA9 version:

- `26 good`
- `14 bad`
- `9 open`
- resolved good rate `0.6500`

Interpretation:

- fewer total trades
- materially fewer bad trades

## Reclaim Live Paper Path

`macd_30s_reclaim` is now enabled by default as a real paper bot, and the runtime now supports a reclaim-only live exclusion list through:

- `MAI_TAI_STRATEGY_MACD_30S_RECLAIM_EXCLUDED_SYMBOLS`

Current default exclusions match the active reclaim replay focus set:

- `JEM`
- `CYCN`
- `BFRG`
- `UCAR`
- `BBGI`

Important behavior:

- the momentum scanner still drives the watchlist
- other bots still receive the normal scanner watchlist
- only reclaim filters out those excluded names before paper-trading decisions

This keeps the live paper reclaim path aligned with the replay-tuned focus universe while preserving the broader scanner state in the control plane.
- much better quality
- consistent with the user’s live observation that overextended EMA entries are more dangerous

There is also a stricter research-only option at `1.5%` above EMA9 (`21 good / 10 bad`), but `2%` is the current preferred middle ground.

## Paper Trading Activation

`macd_30s_reclaim` is now promoted from research-only status to an active paper-trading bot in the normal project lineup.

Project shape:

- reclaim stays a separate `30s Reclaim` bot
- it uses the current reclaim research baseline from [trading_config.py](/C:/Users/kkvkr/OneDrive/Documents/GitHub/project-mai-tai/src/project_mai_tai/strategy_core/trading_config.py)
- it is enabled by default in [settings.py](/C:/Users/kkvkr/OneDrive/Documents/GitHub/project-mai-tai/src/project_mai_tai/settings.py)
- it keeps the paper account name `paper:macd_30s_reclaim`

Operational intent:

- run reclaim as an actual paper-trading bot
- watch live paper results on the control plane
- keep replay and paper review in parallel
- continue tuning only if a change improves paper quality or explains paper mistakes clearly

## Reclaim Runtime Reconciliation Hardening

During live paper trading, reclaim exposed a real state-propagation weakness:

- Alpaca/broker truth could go flat
- Postgres/OMS could reconcile back to flat
- but the strategy runtime or bot page could still temporarily show a stale open position or `ACCOUNT-ONLY` mismatch

This was not treated as a cosmetic problem. The strategy runtime now periodically re-syncs its in-memory position and pending-order state from database truth:

- source of truth for open positions: `VirtualPosition`
- source of truth for pending opens/closes/scales: open `BrokerOrder` rows linked to strategy accounts
- reconcile cadence: about every `5` seconds inside the strategy loop

What the new runtime reconcile does:

- restore missing runtime positions from open virtual positions
- clear stale runtime positions when no virtual backing remains
- replace stale runtime pending-open / pending-close / pending-scale sets with DB truth

Files changed for this hardening:

- [strategy_engine_app.py](/C:/Users/kkvkr/OneDrive/Documents/GitHub/project-mai-tai/src/project_mai_tai/services/strategy_engine_app.py)
- [test_strategy_engine_service.py](/C:/Users/kkvkr/OneDrive/Documents/GitHub/project-mai-tai/tests/unit/test_strategy_engine_service.py)

Focused validation passed locally:

- restore runtime positions and pending state from DB
- restore missing runtime positions when DB still has an open virtual position
- clear stale runtime positions when DB no longer has virtual backing

Live verification after deploy:

- Alpaca paper positions: flat
- Alpaca open orders: none
- latest reconciliation findings: `0`
- open account positions: `0`
- open virtual positions: `0`
- stale reclaim `RECT` position cleared from the live bot payload after restart

Important operator takeaway:

- this class of drift should now self-heal from DB truth instead of waiting for a manual service restart
- after-hours we still did one clean restart to flush stale runtime residue and confirm the fixed code came up clean online

## Core 30s Disabled For Reclaim-Only Paper Test

To isolate `macd_30s_reclaim` from the regular 30-second bot during live paper testing, the project now supports a dedicated core enable flag:

- [settings.py](/C:/Users/kkvkr/OneDrive/Documents/GitHub/project-mai-tai/src/project_mai_tai/settings.py): `strategy_macd_30s_enabled`
- [runtime_registry.py](/C:/Users/kkvkr/OneDrive/Documents/GitHub/project-mai-tai/src/project_mai_tai/runtime_registry.py): only registers `macd_30s` when that flag is enabled
- [strategy_engine_app.py](/C:/Users/kkvkr/OneDrive/Documents/GitHub/project-mai-tai/src/project_mai_tai/services/strategy_engine_app.py): only builds the core 30-second runtime when that flag is enabled
- [.env.example](/C:/Users/kkvkr/OneDrive/Documents/GitHub/project-mai-tai/.env.example): documents `MAI_TAI_STRATEGY_MACD_30S_ENABLED=true`

Focused validation added:

- [test_strategy_engine_service.py](/C:/Users/kkvkr/OneDrive/Documents/GitHub/project-mai-tai/tests/unit/test_strategy_engine_service.py)
  - verifies `macd_30s` can be disabled while `macd_30s_reclaim` stays enabled

Online paper-trading isolation:

- VPS repo `.env` now sets `MAI_TAI_STRATEGY_MACD_30S_ENABLED=false`
- strategy service restarted after that change
- fresh live strategy logs now replay 30-second history into `macd_30s_reclaim` only

Operational meaning:

- reclaim remains the active 30-second paper-trading bot
- the regular 30-second core bot is no longer supposed to generate new paper-trade activity in the live strategy runtime

## Reclaim Confirm-Add Tightening

After reviewing the April 14 live reclaim session, one repeat pattern stood out:

- too many reclaim starters were valid enough to open
- but some `R1_BREAK_CONFIRM` adds were happening before the starter had really proven itself
- that made some weak reclaim attempts grow into larger giveback losses

To tighten that without broadening or rewriting reclaim entry logic, reclaim now has a confirm-add guard:

- [trading_config.py](/C:/Users/kkvkr/OneDrive/Documents/GitHub/project-mai-tai/src/project_mai_tai/strategy_core/trading_config.py): `pretrigger_reclaim_confirm_add_min_peak_profit_pct`
- [entry.py](/C:/Users/kkvkr/OneDrive/Documents/GitHub/project-mai-tai/src/project_mai_tai/strategy_core/entry.py): reclaim `R1_BREAK_CONFIRM` add now requires the starter to have reached a minimum unrealized peak profit first

Current reclaim baseline:

- `pretrigger_reclaim_confirm_add_min_peak_profit_pct = 1.0`

Meaning:

- reclaim can still open the starter normally
- but it will not add the larger confirm size until the starter has shown at least `+1%` peak profit

Why this was chosen:

- it is narrower than adding another broad entry filter
- it specifically targets premature add/giveback behavior
- it preserves the reclaim starter while asking the trade to prove itself before size increases

Focused local validation passed:

- reclaim add still works once the starter has reached the configured peak threshold
- reclaim add is blocked while the starter is still below that threshold
- reclaim research baseline test still passes

Deployment:

- deployed after hours to the VPS
- restarted only `project-mai-tai-strategy.service`
- post-deploy health check returned healthy with the stack flat

## Reclaim Close-Reason Preservation

The next live-day reporting gap was attribution:

- reclaim close fills were landing in the runtime as generic `OMS_FILL`
- that hid the real strategy behavior in `closed_today` and in the reclaim day report
- it made it harder to separate `STOCHK_TIER1`, `HARD_STOP`, `FLOOR_BREACH`, and other real exit patterns

The runtime now preserves the actual close reason from the OMS order event:

- [strategy_engine_app.py](/C:/Users/kkvkr/OneDrive/Documents/GitHub/project-mai-tai/src/project_mai_tai/services/strategy_engine_app.py)
- [test_strategy_engine_service.py](/C:/Users/kkvkr/OneDrive/Documents/GitHub/project-mai-tai/tests/unit/test_strategy_engine_service.py)

Meaning:

- a filled reclaim close now stores the original strategy reason when it is available
- `OMS_FILL` is only used as a fallback when no strategy reason is present

Why this matters:

- tomorrow's reclaim report should reflect real exit behavior instead of generic broker-fill text
- that gives us cleaner evidence for the next reclaim tuning decisions

Focused validation passed locally:

- runtime still clears the position on a final close fill even when the fill quantity differs
- reclaim now preserves a real close reason like `STOCHK_TIER1` through the filled-close path
- `py_compile` passed on the touched files

## Reclaim Later-Pullback Scenario Findings

After the reporting cleanup, I extended the live-day reclaim what-if tooling to test smarter later-pullback scenarios on the April 14 paper session:

- [reclaim_live_day_whatif.py](/C:/Users/kkvkr/OneDrive/Documents/GitHub/project-mai-tai/scripts/reclaim_live_day_whatif.py)
- [reclaim_live_day_report.py](/C:/Users/kkvkr/OneDrive/Documents/GitHub/project-mai-tai/scripts/reclaim_live_day_report.py)
- [reclaim_live_day_report_2026-04-14.md](/C:/Users/kkvkr/OneDrive/Documents/GitHub/project-mai-tai/tmp_replay/reclaim_live_day_report_2026-04-14.md)

The strongest evidence-backed findings tonight were:

- `ticker_pause` remains the clearest overblock
  - `155` bars
  - `+1%` follow-through rate `0.684`
  - `stop-first` rate `0.348`
- `below_vwap_and_ema9` is the most interesting later-pullback blocker
  - `64` bars
  - `+1%` follow-through rate `0.766`
  - `+2%` follow-through rate `0.625`
  - `stop-first` rate `0.328`
- `below_ema9` also hides some later reclaim opportunity, but it is smaller and noisier
  - `23` bars
  - `+1%` follow-through rate `0.783`
  - `stop-first` rate `0.478`

Important conclusion:

- the opportunity is real, but my more disciplined dual-anchor recovery simulations were still too strict and captured `0` bars
- so there is *not* yet a safe new location deploy from tonight
- the next reclaim design work should focus on a new reclaim-specific location split for later pullbacks, centered on the `below_vwap_and_ema9` cluster, instead of broad location loosening

## Reclaim Early-Profit Floor Tightening

The next reclaim issue after confirm-add was giveback:

- several live reclaim losers had already reached at least `+1%` unrealized first
- the old floor logic still allowed too much of that early profit to leak away

To tighten that without changing the full exit engine, reclaim now uses slightly stronger early profit locks:

- [trading_config.py](/C:/Users/kkvkr/OneDrive/Documents/GitHub/project-mai-tai/src/project_mai_tai/strategy_core/trading_config.py)
- [position_tracker.py](/C:/Users/kkvkr/OneDrive/Documents/GitHub/project-mai-tai/src/project_mai_tai/strategy_core/position_tracker.py)

Current reclaim baseline floor settings:

- after `+1%` peak profit: lock `+0.25%`
- after `+2%` peak profit: lock `+0.75%`
- `+3%` and `4%+` behavior remains unchanged from the prior baseline

Why this was chosen:

- it is a narrow giveback reduction
- it specifically targets the “got green, still finished red” reclaim cluster
- it avoids a broad rewrite of reclaim exits during the live paper cycle

Focused validation passed locally:

- reclaim baseline test still passes
- reclaim-specific floor test confirms a trade that peaks above `+1%` now trips the floor sooner
- `py_compile` passed on the touched files

Deployment:

- deployed after hours to the VPS
- restarted only `project-mai-tai-strategy.service`
- post-deploy health check returned healthy with the stack flat
