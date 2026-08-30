# OMS Functionality

This document describes the OMS functionality currently implemented in Mai Tai. It is an implementation reference, not a future design proposal.

Primary code:

- `src/project_mai_tai/oms/service.py`
- `src/project_mai_tai/oms/store.py`
- `src/project_mai_tai/events.py`
- `src/project_mai_tai/db/models.py`
- `src/project_mai_tai/broker_adapters/`

## Purpose

The OMS is the execution-side state machine for Mai Tai. Strategies do not call broker APIs directly. Strategies emit trade-intent events, and OMS decides whether the intent is allowed, persists the intent/risk/order/fill state, routes the order to the selected broker adapter, publishes order events back to the strategy runtime, and continuously reconciles broker truth into local execution state.

The service name is `oms-risk`.

The OMS owns these responsibilities:

- Seed strategy and broker-account metadata at startup.
- Consume strategy intent events from Redis.
- Validate all intents through OMS-side risk gates.
- Persist trade intents and risk checks.
- Submit broker orders through the broker adapter layer.
- Persist broker orders, broker order events, and fills.
- Maintain virtual strategy positions and broker-account positions.
- Publish order events back to Redis for strategy runtime consumers.
- Periodically sync working broker orders and account positions.
- Manage software hard-stop state and native stop-guard backup orders.
- Cancel or refresh stale working orders.
- Cache Schwab session-ineligible symbols and block repeat open attempts.

The OMS does not own scanner logic, entry signal generation, bar building, indicator calculation, or strategy exit decision rules. Those happen before an intent reaches OMS.

## Runtime Flow

At startup, `OmsRiskService.run()` seeds runtime metadata and starts reading Redis streams.

It reads:

- `strategy-intents`: strategy-to-OMS trade requests.
- `market-data`: quote/trade ticks used by OMS hard-stop and quote-drift protection.

It publishes:

- `order-events`: OMS-to-strategy execution status events.
- `heartbeats`: service health events.

The main loop also runs broker sync on `oms_broker_sync_interval_seconds`, default `5` seconds. If active stop-guard orders exist, sync can run faster according to the stop-guard refresh stage settings.

## Event Contracts

### Trade Intent

Strategies emit `TradeIntentEvent` with `TradeIntentPayload`.

Key payload fields:

- `strategy_code`
- `broker_account_name`
- `symbol`
- `side`: `buy` or `sell`
- `quantity`
- `intent_type`: `open`, `scale`, `close`, or `cancel`
- `reason`
- `metadata`

The metadata field carries strategy-specific routing details such as path, order type, limit price, stop-guard fields, target order IDs for cancels, and extended-hours routing markers.

### Order Event

OMS publishes `OrderEventEvent` with `OrderEventPayload`.

Key payload fields:

- original intent event ID and database intent ID
- database order ID when an order exists
- strategy code and broker account name
- client order ID and broker order ID
- broker fill ID when available
- symbol, side, intent type, status, quantity
- filled quantity and fill price
- reason
- metadata

Supported order-event statuses include:

- `accepted`
- `submitted`
- `partially_filled`
- `filled`
- `cancelled`
- `rejected`

When a broker report status is `accepted`, OMS marks the related intent as `submitted` internally. Other terminal statuses are applied directly.

## Database State

OMS persists execution truth in Postgres.

Core tables:

- `strategies`: strategy identity, execution mode, metadata.
- `broker_accounts`: broker account identity, provider, environment, active flag.
- `trade_intents`: every requested open/scale/close/cancel intent and its current status.
- `risk_checks`: OMS-side pass/reject decisions for each intent.
- `broker_orders`: broker-facing orders keyed by `client_order_id`, with broker order ID when available.
- `broker_order_events`: immutable broker report/event history for orders.
- `fills`: incremental fill records.
- `virtual_positions`: strategy-attributed position ownership within a broker account.
- `account_positions`: broker-account position snapshots.
- `schwab_ineligible_today`: session-day cache for symbols Schwab rejects as broker-ineligible.
- `reconciliation_runs` and `reconciliation_findings`: separate reconciler outputs used to identify mismatches.

Open order statuses are:

- `pending`
- `submitted`
- `accepted`
- `partially_filled`

These open statuses drive duplicate-exit checks, working-order refresh, cancel target lookup, and broker-order sync.

## Broker Adapter Boundary

Only OMS calls broker trading adapters.

The adapter protocol uses:

- `OrderRequest`: normalized order request.
- `ExecutionReport`: normalized broker result/update.
- `BrokerPositionSnapshot`: normalized account position row.

Supported adapter routing includes:

- `simulated`
- `alpaca`
- `schwab`
- `webull`

If multiple active broker providers are configured, OMS builds a routing adapter and routes by broker account. If a single provider is configured, it builds that provider adapter directly. If `oms_adapter` is explicitly `simulated`, it uses the simulated adapter.

## Intent Processing

The normal intent lifecycle is:

1. Strategy emits a `trade_intent`.
2. OMS ensures the strategy row exists.
3. OMS ensures the broker account row exists.
4. OMS creates a `trade_intents` row with status `pending`.
5. OMS evaluates risk.
6. OMS records a `risk_checks` row.
7. If rejected, OMS marks the intent `rejected` and publishes a rejected order event.
8. If accepted, OMS builds an `OrderRequest`.
9. OMS submits the request through the broker adapter.
10. OMS records broker reports into `broker_orders`, `broker_order_events`, and `fills`.
11. OMS updates virtual/account positions for fills.
12. OMS marks the intent status from the broker report.
13. OMS publishes order events.
14. OMS runs a targeted broker-state sync for the affected account.

Client order IDs are built as:

```text
<strategy_code>-<symbol>-<intent_type>-<first_12_hex_chars_of_event_id>
```

Replacement watchdog orders append a short `-r<random>` suffix to the original client order ID base.

## Risk Checks

OMS risk is intentionally narrow and execution-focused. It does not recalculate strategy indicators.

Implemented pre-submit risk checks:

- Protected-symbol hard block: any symbol in `MAI_TAI_PROTECTED_SYMBOLS` is rejected for every intent type with reason `protected_symbol:<SYMBOL>`.
- Quantity validation:
  - `open`, `scale`, and `close` require `quantity > 0`.
  - `cancel` rejects negative quantity.
- Intent type validation: only `open`, `scale`, `close`, and `cancel` are accepted.
- Side validation: only `buy` and `sell` are accepted.

Additional execution-side gates run after the basic risk pass:

- Schwab cached ineligible open block.
- Session symbol block for symbols rejected as not tradable for the rest of the session.
- Duplicate sell-exit detection.
- Strategy virtual-position availability check before selling.
- Broker account-position availability check before selling.
- Pending exit reserved-quantity check before selling.
- Quantity clamp for sells to the minimum of requested quantity, strategy virtual quantity, and available unreserved broker quantity.

## Sell/Exit Safety

For `close` and `scale` sell intents, OMS performs extra protection before submitting.

If the intent is not a native stop-guard management intent, OMS first cancels any active native stop-guard backup order for that strategy/account/symbol. This prevents the broker-side stop order from reserving quantity while a real strategy sell is being routed.

For hard-stop closes with `metadata.stop_guard=true`, OMS also cancels older open non-native exit orders for the same strategy/account/symbol before duplicate-exit and reserved-quantity checks. This allows a hard stop to preempt an older scale or close order that is still working.

OMS then rejects if:

- another non-native open sell exit exists for the same strategy/account/symbol: `duplicate_exit_in_flight`
- the strategy has no virtual quantity to sell: `no strategy position available to sell`
- the broker account has no position after a broker-position refresh: `no broker position available to sell`
- all broker quantity is already reserved by pending exits: `broker quantity already reserved for pending exits`

If some quantity is available but less than requested, OMS reduces the submitted quantity to the available safe amount.

## Cancel Intents

Cancel intents are handled through `_process_cancel_intent()`.

OMS locates a cancellable open order by:

- `target_client_order_id` or `client_order_id` metadata
- `broker_order_id` metadata
- latest open order for the same strategy/account/symbol as fallback

If no target is found, OMS rejects the cancel with `cancel_target_not_found`.

When a target exists, OMS submits a cancel request using the existing target order identity. If the broker rejects the cancel, OMS preserves the original open order status rather than incorrectly terminalizing it.

OMS also creates internal cancel intents for:

- native stop-guard cancellation before a strategy sell
- hard-stop preemption of older pending exits
- working-order watchdog refresh
- stale/invalid open-intent abandonment

Internal cancel intents still get risk-check rows, usually with an internal pass reason.

## Order And Fill Recording

OMS records every broker execution report into durable state.

For each report:

- `broker_orders` is created or updated by client order ID.
- `broker_order_events` appends the report history.
- `fills` records incremental fills only.
- virtual and account positions are updated for fill quantities.
- an order event is built and published to Redis.

Fill deduplication works in two layers:

- If the broker provides a `broker_fill_id`, duplicate fill IDs are ignored.
- If no new filled quantity exists beyond already-recorded fills for that order, no new fill is recorded.

This is important for partial fills because broker updates may report cumulative filled quantity.

## Position Accounting

OMS maintains two position layers.

`account_positions` represent the broker account's total position by symbol. This is broker-account truth as known locally and is refreshed from broker snapshots.

`virtual_positions` represent strategy-attributed ownership within that broker account. This allows multiple strategies or bots to share an account while OMS still knows which strategy owns which quantity.

Buy fills:

- increase quantity
- calculate weighted average price
- set/open `opened_at` for virtual positions
- update source timestamps for account positions

Sell fills:

- reduce quantity
- realize P&L on virtual positions
- reset quantity and average price to zero when fully closed

Broker position sync also clears virtual positions that no longer have broker-account backing. This prevents strategy-owned local positions from surviving after broker truth says the account is flat.

## Broker Sync

OMS periodically runs broker sync.

Position sync:

- Lists active broker accounts.
- Calls `list_account_positions()` for each account.
- Upserts `account_positions`.
- Sets local account positions missing from broker snapshots to zero.
- Clears positive virtual positions without broker-account backing.

Order sync:

- Loads open broker orders.
- Calls `fetch_order_update()` for each order with a broker order ID.
- Records status changes and new fill progress.
- Publishes terminal order events back to strategy runtime.
- Runs working-order refresh logic where appropriate.
- Terminalizes orphaned active intents whose related orders are already terminal.

Terminal intent repair rules:

- If related orders include `filled`, intent becomes `filled`.
- If related orders include `cancelled`, intent becomes `cancelled`.
- If related orders include `rejected`, intent becomes `rejected`.
- If related orders are still `partially_filled`, intent is not terminalized.
- Cancel intents without own order rows can be terminalized from their target order status.

## Working-Order Watchdog

OMS manages stale working orders instead of leaving them indefinitely active.

Default settings:

- `oms_working_order_refresh_seconds = 5`
- `oms_intent_max_age_seconds = 30`
- `oms_quote_drift_cancel_tolerance_cents = 1.0`
- `oms_intent_setup_revalidation_enabled = true`

There are three protection tiers for open limit orders:

1. Quote-driven drift cancel: on quote ticks, if an open limit order's market quote has moved past the limit by more than the configured tolerance, OMS cancels the working order and abandons the intent.
2. Intent max age: during sync, an open intent older than the configured max age is cancelled and abandoned.
3. Setup revalidation: during sync, OMS checks latest `strategy_bar_history` for the strategy/symbol. If the latest bar is no longer a matching signal/path for the original intent, OMS cancels and abandons the intent.

For stale but still-valid working orders, OMS cancel-and-replaces the order:

- It cancels the old working order.
- It builds replacement metadata.
- It fetches the current quote for limit repricing where possible.
- It submits a replacement order for remaining quantity.
- It records the replacement reports against the same intent.

This is watchdog management, not a new strategy signal.

## Software Hard Stop

OMS maintains an in-memory hard-stop registry keyed by:

```text
(strategy_code, broker_account_name, symbol)
```

The registry is armed after buy/open fills when the fill metadata contains:

- `stop_guard_enabled=true`
- `stop_loss_pct`

The stop tracks:

- strategy code
- broker account name
- symbol
- quantity
- weighted entry price
- stop-loss percent
- stop price
- quote max age
- panic limit buffer percent
- whether a close is already in flight

The stop price is calculated from weighted entry price:

```text
stop_price = entry_price * (1 - stop_loss_pct / 100)
```

On quote/trade ticks, OMS evaluates armed stops:

- Uses fresh bid when available.
- Uses fresh last trade when available.
- Requires quote/trade age within the configured max age.
- Triggers when the resolved price is at or below stop price.
- Throttles repeated trigger attempts within 250 ms.

When triggered, OMS emits an internal `close` intent with reason `HARD_STOP` and metadata:

- `stop_guard=true`
- `stop_loss_pct`
- `stop_price`
- `stop_trigger_price`
- `stop_trigger_source`
- `panic_buffer_pct`
- limit-order routing fields based on panic buffer
- extended-hours fields when outside regular market session

The resulting hard-stop order is routed through the same normal OMS flow, including duplicate-exit and reserved-quantity protections.

If a hard-stop close fills, OMS removes the armed stop. If OMS gets no-position style rejections, it also removes the stop because the broker no longer has quantity to protect.

## Native Stop-Guard Backup

During regular market hours, OMS can also maintain a native broker stop backup order for an armed hard stop.

Native stop backup behavior:

- Native stop orders are only armed in regular market session.
- They are internal close intents with reason `HARD_STOP_NATIVE_BACKUP`.
- Metadata includes `native_stop_guard=true`, `order_type=STOP`, `time_in_force=day`, and `stop_price`.
- Before a normal strategy sell, OMS cancels the native stop backup so it does not reserve shares.
- After a buy open fill or after a non-native sell rejection/cancel, OMS can re-arm from the hard-stop registry.

Native stop guards are intentionally excluded from normal duplicate-exit and reserved-quantity checks where needed, because they are protective backup orders rather than strategy exits.

## Stop Rejection Fallback

If a stop-related order is rejected with a reason that looks like a stop rejection, OMS can submit a fallback market close.

The fallback path:

- Refreshes broker position quantity.
- If no broker quantity exists, it does nothing.
- Creates an internal close intent with reason `STOP_REJECTED_FALLBACK`.
- Marks the fallback risk check as pass with reason `stop_rejected_fallback`.
- Submits a market sell for available quantity.
- Records and publishes reports through normal order handling.

Fallback metadata includes:

- original client order ID
- rejection reason
- `stop_reject_fallback=true`
- `order_type=market`

## Stop-Guard Refresh Stages

Stop-guard working orders have faster refresh behavior than normal orders.

Default settings:

- `oms_stop_guard_refresh_stage_1_seconds = 1.0`
- `oms_stop_guard_refresh_stage_2_seconds = 2.0`
- `oms_stop_guard_refresh_stage_3_seconds = 3.0`
- `oms_stop_guard_refresh_stage_1_buffer_pct = 3.0`
- `oms_stop_guard_refresh_stage_2_buffer_pct = 5.0`
- `oms_after_hours_stop_guard_quote_max_age_ms = 1000`
- `oms_after_hours_stop_guard_initial_panic_buffer_pct = 1.0`
- `oms_after_hours_stop_guard_catastrophic_gap_pct = 1.5`
- `oms_after_hours_stop_guard_catastrophic_panic_buffer_pct = 8.0`

When a stop-guard limit order is refreshed, OMS can widen the panic buffer by stage. In extended hours, catastrophic gap handling can widen the panic buffer further if the current price is already far below the stop threshold.

## Schwab Ineligible Cache

OMS has a Schwab-specific ineligible-symbol cache.

When a broker rejection reason contains:

```text
must be placed with a broker
```

OMS records the symbol/account/session-day in `schwab_ineligible_today`.

For future `open` intents on the same Schwab broker account and session day, OMS rejects before sending anything to the broker:

```text
schwab_ineligible_cached
```

Important boundary:

- This cache applies to Schwab-backed `open` intents.
- It does not block `close` intents, because existing positions must remain closable.

OMS also supports a Redis session symbol block when broker rejection says a symbol is not tradable. Open and scale intents for that symbol/account can be blocked for the rest of the session with:

```text
broker_symbol_not_tradable_for_session
```

## Redis Heartbeats

OMS publishes heartbeat events with:

- service name `oms-risk`
- instance hostname
- status such as `starting`, `healthy`, or `stopping`
- details including adapter label and active providers

The control plane uses these heartbeats to surface runtime health.

## Reconciliation Boundary

OMS writes execution state and periodically syncs broker truth. The reconciler is separate.

The reconciler compares:

- virtual positions vs account positions
- stuck orders
- stuck intents
- average-price drift
- other state mismatches

Repairs should be performed through OMS-safe flows, not by directly mutating tracker state.

## Operational Limits

Current limitations and important boundaries:

- OMS does not fully replay strategy intents emitted while OMS is down. The live restart runbook stops strategy before OMS restarts for this reason.
- The software hard-stop registry is in memory. It is rebuilt by future fills and live runtime activity, not as a full durable stop registry replay.
- OMS does not decide whether a strategy should enter or exit. It only enforces execution safety after the strategy emits an intent.
- OMS broker sync is periodic, so broker-state visibility can lag by the configured sync interval.
- Native stop backup orders are regular-market only.
- Extended-hours protection relies on software-triggered marketable limit behavior and watchdog refresh, not native broker stop orders.

## High-Level Data Flow

```text
strategy runtime
  -> Redis strategy-intents
  -> OMS risk and persistence
  -> broker adapter
  -> broker execution reports
  -> OMS order/fill/position persistence
  -> Redis order-events
  -> strategy runtime position/order state
```

## Test Coverage Pointers

Relevant tests include:

- `tests/unit/test_oms_store.py`
- `tests/unit/test_oms_risk_service.py`
- `tests/integration/test_strategy_oms_roundtrip.py`

The tests cover:

- intent persistence and filled-position updates
- basic risk rejection
- protected-symbol hard block
- Schwab ineligible cache behavior
- broker-position sync and virtual-position clearing
- cancel target handling
- rejected cancel preservation
- open order sync and terminal order event publishing
- duplicate partial-fill suppression
- orphaned active intent terminalization
- stale working-order refresh
- quote-drift cancellation
- max-age and setup-invalid abandonment
- hard-stop preemption of pending exits
- native stop-guard cancellation and rearming
- stop-rejection fallback
- shared-account sell quantity safety

