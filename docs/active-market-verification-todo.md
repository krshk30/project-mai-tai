# Active Market Verification Todo

Use this tracker during the next active U.S. equities session.

Context:

- On Saturday, March 28, 2026, the new stack was healthy and configured for `alpaca_paper`.
- The dashboard at `https://project-mai-tai.live` showed `4` strategies and `3` broker accounts.
- The system was quiet, which matched the non-trading-day timing.
- Snapshot polling was working.
- Live WebSocket behavior should still be re-checked during an active subscribed-symbol session.

## Preflight Status For Monday, March 30, 2026

Completed on Sunday, March 29, 2026:

- Public site reachable at `https://project-mai-tai.live` behind basic auth.
- `UTC` user-facing timestamps replaced with `ET` across the main dashboard, scanner page, bot pages, and JSON endpoints.
- `/health` is green with all five services reporting healthy after the Redis retention fix.
- Redis stream retention is bounded, and Redis memory returned to a normal baseline after trimming the oversized snapshot stream.
- Massive snapshot access verified live from the VPS.
- All three Alpaca paper accounts verified live from the VPS as `ACTIVE`, with no `trading_blocked` or `account_blocked` flags.
- Alpaca paper broker positions verified directly: all three accounts currently report `0` open positions.
- Strategy/account seed state present: `4` strategies and `3` broker accounts.
- OMS is configured for `alpaca_paper`.

Known caution:

- The VPS still reports `*** System restart required ***`. Do not reboot before the Monday paper session unless you want a coordinated maintenance restart for both the legacy and Mai Tai stacks.

Tomorrow still requires live-session verification:

- First non-empty `top_confirmed -> watchlist -> subscription` cycle.
- First `intent -> accepted -> filled` paper order path.
- Shared-account attribution check when `tos` or `runner` trades.
- Live subscribed-symbol WebSocket stability during active order flow.

## Standing Validation Expectations From User

These should be treated as ongoing validation requirements, not one-off questions from the previous agent.

- Continue verifying Mai Tai UI parity against legacy when the user calls out missing surfaces or columns.
- Treat scanner and bot pages as primary operator tools, not secondary admin pages.
- Keep user-facing time displays in ET on any new surface.
- Make health/status labels compact but explainable if questioned.
- When validating news-driven behavior, use current-session logic starting from previous market close (`4:00 PM ET`) rather than generic “last 24h” assumptions.
- When validating runner/news behavior, remember that news is meant to relax earlier entry only for strict `PATH_A_NEWS`, not act as a stand-alone trade trigger.

## Legacy Parity Checks To Re-Use

If the user asks whether Mai Tai matches the legacy workflow, re-check these classes of surfaces directly:

- scanner dashboard structure
- momentum confirmed table columns
- 5 Pillars panel
- Top Gainers panel
- Momentum Alerts panel
- Top Gainer Changes panel
- `30s` bot page
- `1m` bot page
- `tos` bot page
- `runner` bot page

Notes:

- The user specifically notices missing columns, alerts, and trade-log style panels.
- The expectation is not pixel-perfect copying, but workflow parity and operator usefulness.

## Session Header

- Date:
- Operator:
- Market session:
- Legacy app paper trading confirmed disabled:
- New stack commit:

## Pre-Market / Open

- Confirm [project-mai-tai.live](https://project-mai-tai.live) loads and `/health` is green.
- Confirm `/api/overview` shows:
  - `oms_adapter = alpaca_paper`
  - `strategies = 4`
  - `broker_accounts = 3`
- Confirm `/api/shadow` is reachable and legacy comparison is still connected.
- Confirm no unexpected open `virtual_positions` or `account_positions` before new activity starts.

Notes:

## Scanner And Watchlist Flow

- Verify fresh snapshot batches continue updating during market hours.
- Verify at least one symbol reaches `top_confirmed` in the new system.
- Verify `strategy_runtime.watchlist` becomes non-empty when confirmed names appear.
- Verify `market_data.active_subscription_symbols` becomes greater than `0` after watchlist population.
- Verify legacy/new shadow comparison stays reasonable once live movers appear.
- Verify the scanner page still feels complete during live flow:
  - momentum confirmed
  - 5 pillars
  - top gainers
  - momentum alerts
  - top gainer changes
- If `Momentum Confirmed` is populated, verify catalyst cells show:
  - confidence
  - article count
  - freshness
  - reason
  - `PATH A ready` only when appropriate

Success criteria:

- New system moves from `snapshot -> top_confirmed -> watchlist -> active subscriptions` without manual intervention.

Notes:

## Live Tick And WebSocket Verification

- Verify subscribed symbols receive live ticks and quotes once active symbols exist.
- Verify the market-data service does not continue a repeated Massive WebSocket reconnect loop while active symbols are subscribed.
- If reconnects appear, capture:
  - timestamp
  - active subscribed symbols count
  - whether order flow was impacted

Success criteria:

- No repeated `policy violation` reconnect pattern during an actively subscribed period.

Notes:

## Intent -> Order -> Fill Path

- Verify the first paper-trade candidate produces a `trade_intent`.
- Verify OMS creates an `accepted` order event.
- Verify OMS creates a `filled` order event.
- Verify `recent_intents`, `recent_orders`, and `recent_fills` all show the same strategy/account/symbol path.
- Verify the `client_order_id` is present and strategy-specific.

Success criteria:

- First full path completes as `intent -> accepted -> filled` with no manual repair.

Notes:

## Position And Shared-Account Attribution

- Verify `virtual_positions` updates for the strategy that traded.
- Verify `account_positions` updates for the broker account that traded.
- If the trade is in `tos` or `runner`, verify the shared `paper:tos_runner_shared` account still attributes the position to the correct strategy in `virtual_positions`.
- Verify no unrelated strategy receives a false position or false fill.

Success criteria:

- Shared-account attribution works cleanly for per-strategy virtual positions.

Notes:

## Exit And Lifecycle

- Verify at least one close or scale event completes when a strategy exits.
- Verify the close path updates:
  - `recent_orders`
  - `recent_fills`
  - `virtual_positions`
  - `account_positions`
- Verify the strategy runtime clears pending state after the broker fill.

Success criteria:

- Exit lifecycle is reflected end to end without orphan state.

Notes:

## Reconciliation And Incident Handling

- Verify `/api/reconciliation` remains clean after the first fill.
- Verify no unexpected `SystemIncident` opens during normal paper trading.
- Verify `cutover_confidence` remains high after real order flow begins.
- If a finding appears, capture the exact finding type and whether it self-clears.

Success criteria:

- Real paper activity does not immediately produce stuck-order, stuck-intent, or quantity-mismatch findings.

Notes:

## Restart Safety

- During paper trading, perform one controlled restart of the new stack only.
- Verify open paper positions are preserved after restart.
- Verify no duplicate orders are created on restart.
- Verify reconciliation remains clean after restart recovery.

Success criteria:

- Restart with open positions is safe and does not liquidate, duplicate, or orphan state.

Notes:

## End-Of-Day Wrap-Up

- Compare Alpaca paper account positions against `account_positions`.
- Compare per-strategy expected positions against `virtual_positions`.
- Record any shadow divergence that appeared during the day.
- Record whether the new platform is ready for the next larger paper rollout step.

Final assessment:

- Ready for continued paper rollout:
- Blockers found:
- Follow-up actions:
  - Lower `MomentumConfirmedConfig.extreme_mover_min_day_change_pct` from `50.0` to `30.0`.
    Context: `YAAS` had `VOLUME_SPIKE + SQUEEZE_5MIN` by about `07:07 AM ET`, but it did not confirm until a second squeeze at `07:12 AM ET` because current `PATH_C_EXTREME_MOVER` requires `>= 50%` day change for single-squeeze confirmation.
  - Revisit the confirm-stage `volume/float` threshold in `MomentumConfirmedScanner._check_common_filters(...)`.
    Context: current logic already uses cumulative intraday snapshot/day volume, not just the initial spike bar, but the hard `>= 20%` float-turnover gate appears too strict for early-session mid-float momentum names like `SNBR`. Evaluate lowering it to around `10%` or using a float-tiered threshold instead of a flat `20%`.
  - Update the 30s bot `Decision Tape` default view so it shows current actionable confirmed/live symbols only.
    Context: operator validation gets noisy when historical blocked rows and non-actionable symbols remain visible after current confirmed count is zero.
  - Revisit Schwab extended-hours protection design.
    Context: Schwab does not support true stop orders in extended hours, so we need a deliberate design review around software-triggered aggressive marketable-limit exits, optional pre-staged profit-taking limit orders, and dead-man/process-down protection. Do not rely on a resting sell limit below market as a fake hard-stop substitute for long positions.

## Learning Log For Next Agent

Use this section to append new observations, not to replace prior context.

- The user values operational confidence and parity much more than abstract architecture discussion during live validation.
- Quiet/off-hours correctness should still be explained clearly in the UI.
- If a functionality question comes up, the preferred pattern is:
  1. verify against legacy if relevant
  2. verify current Mai Tai runtime behavior
  3. record the conclusion here or in the session handoff
