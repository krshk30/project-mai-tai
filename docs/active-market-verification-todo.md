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
- [x] Public site reachable at `https://project-mai-tai.live` behind basic auth.
- [x] `UTC` user-facing timestamps replaced with `ET` across the main dashboard, scanner page, bot pages, and JSON endpoints.
- [x] `/health` is green with all five services reporting healthy after the Redis retention fix.
- [x] Redis stream retention is bounded, and Redis memory returned to a normal baseline after trimming the oversized snapshot stream.
- [x] Massive snapshot access verified live from the VPS.
- [x] All three Alpaca paper accounts verified live from the VPS as `ACTIVE`, with no `trading_blocked` or `account_blocked` flags.
- [x] Alpaca paper broker positions verified directly: all three accounts currently report `0` open positions.
- [x] Strategy/account seed state present: `4` strategies and `3` broker accounts.
- [x] OMS is configured for `alpaca_paper`.

Known caution:
- [ ] The VPS still reports `*** System restart required ***`. Do not reboot before the Monday paper session unless you want a coordinated maintenance restart for both the legacy and Mai Tai stacks.

Tomorrow still requires live-session verification:
- [ ] First non-empty `top_confirmed -> watchlist -> subscription` cycle.
- [ ] First `intent -> accepted -> filled` paper order path.
- [ ] Shared-account attribution check when `tos` or `runner` trades.
- [ ] Live subscribed-symbol WebSocket stability during active order flow.

## Session Header

- Date:
- Operator:
- Market session:
- Legacy app paper trading confirmed disabled:
- New stack commit:

## Pre-Market / Open

- [ ] Confirm [project-mai-tai.live](https://project-mai-tai.live) loads and `/health` is green.
- [ ] Confirm `/api/overview` shows:
  - `oms_adapter = alpaca_paper`
  - `strategies = 4`
  - `broker_accounts = 3`
- [ ] Confirm `/api/shadow` is reachable and legacy comparison is still connected.
- [ ] Confirm no unexpected open `virtual_positions` or `account_positions` before new activity starts.

Notes:

## Scanner And Watchlist Flow

- [ ] Verify fresh snapshot batches continue updating during market hours.
- [ ] Verify at least one symbol reaches `top_confirmed` in the new system.
- [ ] Verify `strategy_runtime.watchlist` becomes non-empty when confirmed names appear.
- [ ] Verify `market_data.active_subscription_symbols` becomes greater than `0` after watchlist population.
- [ ] Verify legacy/new shadow comparison stays reasonable once live movers appear.

Success criteria:
- New system moves from `snapshot -> top_confirmed -> watchlist -> active subscriptions` without manual intervention.

Notes:

## Live Tick And WebSocket Verification

- [ ] Verify subscribed symbols receive live ticks and quotes once active symbols exist.
- [ ] Verify the market-data service does not continue a repeated Massive WebSocket reconnect loop while active symbols are subscribed.
- [ ] If reconnects appear, capture:
  - timestamp
  - active subscribed symbols count
  - whether order flow was impacted

Success criteria:
- No repeated `policy violation` reconnect pattern during an actively subscribed period.

Notes:

## Intent -> Order -> Fill Path

- [ ] Verify the first paper-trade candidate produces a `trade_intent`.
- [ ] Verify OMS creates an `accepted` order event.
- [ ] Verify OMS creates a `filled` order event.
- [ ] Verify `recent_intents`, `recent_orders`, and `recent_fills` all show the same strategy/account/symbol path.
- [ ] Verify the `client_order_id` is present and strategy-specific.

Success criteria:
- First full path completes as `intent -> accepted -> filled` with no manual repair.

Notes:

## Position And Shared-Account Attribution

- [ ] Verify `virtual_positions` updates for the strategy that traded.
- [ ] Verify `account_positions` updates for the broker account that traded.
- [ ] If the trade is in `tos` or `runner`, verify the shared `paper:tos_runner_shared` account still attributes the position to the correct strategy in `virtual_positions`.
- [ ] Verify no unrelated strategy receives a false position or false fill.

Success criteria:
- Shared-account attribution works cleanly for per-strategy virtual positions.

Notes:

## Exit And Lifecycle

- [ ] Verify at least one close or scale event completes when a strategy exits.
- [ ] Verify the close path updates:
  - `recent_orders`
  - `recent_fills`
  - `virtual_positions`
  - `account_positions`
- [ ] Verify the strategy runtime clears pending state after the broker fill.

Success criteria:
- Exit lifecycle is reflected end to end without orphan state.

Notes:

## Reconciliation And Incident Handling

- [ ] Verify `/api/reconciliation` remains clean after the first fill.
- [ ] Verify no unexpected `SystemIncident` opens during normal paper trading.
- [ ] Verify `cutover_confidence` remains high after real order flow begins.
- [ ] If a finding appears, capture the exact finding type and whether it self-clears.

Success criteria:
- Real paper activity does not immediately produce stuck-order, stuck-intent, or quantity-mismatch findings.

Notes:

## Restart Safety

- [ ] During paper trading, perform one controlled restart of the new stack only.
- [ ] Verify open paper positions are preserved after restart.
- [ ] Verify no duplicate orders are created on restart.
- [ ] Verify reconciliation remains clean after restart recovery.

Success criteria:
- Restart with open positions is safe and does not liquidate, duplicate, or orphan state.

Notes:

## End-Of-Day Wrap-Up

- [ ] Compare Alpaca paper account positions against `account_positions`.
- [ ] Compare per-strategy expected positions against `virtual_positions`.
- [ ] Record any shadow divergence that appeared during the day.
- [ ] Record whether the new platform is ready for the next larger paper rollout step.

Final assessment:
- Ready for continued paper rollout:
- Blockers found:
- Follow-up actions:
