# Architecture

## Purpose

`project-mai-tai` replaces the legacy single-process trading runtime with a service-oriented platform while preserving the proven scanner and bot behavior.

The legacy application is intentionally kept separate during migration.

## Service Topology

Runtime services are implemented in `src/project_mai_tai/` and launched through thin wrappers in `services/`.

### `control-plane`

Primary code:

- `src/project_mai_tai/services/control_plane.py`

Responsibilities:

- operator dashboard and HTML views
- JSON APIs for health, scanner, bot, order, and reconciliation state
- latest Redis stream inspection plus Postgres-backed state views
- optional legacy-shadow comparison views
- scanner blacklist management
- broker-account summary and runtime-status surfaces for operators

Rules:

- does not talk to broker APIs directly
- does not own trading decisions
- write actions must route through persisted state or OMS-safe flows

### `market-data-gateway`

Primary code:

- `src/project_mai_tai/market_data/`
- `src/project_mai_tai/services/market_data_gateway.py`

Responsibilities:

- owns Massive/Polygon snapshot polling
- owns Massive/Polygon live trade and quote subscriptions
- builds reference-data cache
- publishes historical warmup bars for `30s`, `1m`, and `5m`
- manages dynamic symbol subscriptions requested by downstream consumers
- publishes normalized market-data events into Redis Streams

Cadence:

- full-market snapshot polling defaults to `5s`
- live subscribed trades and quotes stream continuously for active symbols

Rules:

- single owner of external market-data connections for the new platform
- no strategy logic
- no broker logic

### `strategy-engine`

Primary code:

- `src/project_mai_tai/services/strategy_engine_app.py`
- `src/project_mai_tai/strategy_core/`

Responsibilities:

- processes snapshot batches, trade ticks, historical warmup, and order events
- computes scanner surfaces including momentum alerts, momentum confirmed, five pillars, and top gainers
- maintains watchlists and publishes desired market-data subscriptions
- runs four bot runtimes:
  - `macd_30s`
  - `macd_1m`
  - `tos`
  - `runner`
- emits trade intents for OMS
- publishes strategy state snapshots for the dashboard
- applies scanner blacklist filtering from Postgres

Cadence:

- scanner surfaces update on each snapshot batch
- alert warmup and squeeze windows are computed from the configured snapshot interval
- bot runtimes update on trade ticks and emit decisions on completed `30s`, `60s`, or `300s` bars depending on strategy

Rules:

- no direct broker calls
- no authoritative execution persistence
- strategy behavior should stay as deterministic as practical

### `oms-risk`

Primary code:

- `src/project_mai_tai/oms/service.py`
- `src/project_mai_tai/oms/store.py`
- `src/project_mai_tai/broker_adapters/`

Responsibilities:

- seeds runtime strategy and broker-account metadata
- validates trade intents and records risk checks
- submits and cancels broker orders through the selected adapter
- persists broker orders, order events, and fills
- maintains:
  - `virtual_positions`
  - `account_positions`
- supports shared-account attribution across strategies
- syncs broker positions back into Postgres on an interval

Supported adapters today:

- `simulated`
- `alpaca_paper`
- `schwab`

Rules:

- only service allowed to talk to broker trading APIs
- Postgres is the execution truth inside Mai Tai
- broker truth must still be reconciled back into that execution model

### `reconciler`

Primary code:

- `src/project_mai_tai/reconciliation/service.py`

Responsibilities:

- compares virtual positions to account positions
- checks average-price drift
- detects stuck orders
- detects stuck intents
- writes reconciliation runs, findings, and incidents
- publishes service health based on reconciliation outcome

Rules:

- reconciliation identifies and records problems
- repair should happen through OMS-safe flows, not ad hoc tracker mutation

## Persistence Model

### Postgres

Postgres is the durable source of truth for:

- broker accounts
- strategies
- trade intents
- broker orders
- broker order events
- fills
- account positions
- virtual positions
- reconciliation runs and findings
- system incidents
- dashboard snapshots
- scanner blacklist entries

Primary model definitions live in:

- `src/project_mai_tai/db/models.py`

### Redis Streams

Redis Streams are the internal event bus, not the final source of truth.

Current stream families include:

- `market-data`
- `snapshot-batches`
- `market-data-subscriptions`
- `strategy-intents`
- `order-events`
- `strategy-state`
- `heartbeats`

Event envelopes and payloads live in:

- `src/project_mai_tai/events.py`

## Shared Account Model

The platform must support both:

- separate paper accounts during migration
- one shared live brokerage account later

To support that, OMS records and derives state by:

- `broker_account_id`
- `strategy_id`
- `intent_id`
- `client_order_id`
- broker-side order/fill identifiers when available

Two position layers are intentionally kept:

- `account_positions`
  - what the broker account actually holds
- `virtual_positions`
  - strategy-attributed position ownership inside that account

## Operator Surface

The dashboard is a first-class runtime requirement.

Current operator surfaces include:

- overall health and service heartbeats
- scanner pipeline and confirmed candidates
- watchlist/runtime state snapshots
- bot-level positions, pending states, and recent decisions
- broker-account summaries
- recent intents, orders, and fills
- virtual and account positions
- reconciliation runs and findings
- incident tracking
- optional shadow comparison against the legacy app

## Security And Deployment Model

Production assumptions in the repo today:

- services bind to localhost where possible
- Nginx is the public edge
- dashboard access is protected with basic auth
- HTTPS terminates at `project-mai-tai.live`
- env files are root-owned under `/etc/project-mai-tai/`
- services are managed by `systemd`

Operational assets live in:

- `ops/bootstrap/`
- `ops/env/`
- `ops/nginx/`
- `ops/systemd/`

## Recovery And Restart Reality

The architecture aims for restart-safe execution truth, but not every runtime concern is fully rehydrated yet.

What survives a restart cleanly:

- broker-side orders and positions
- Postgres-backed intents, orders, fills, virtual positions, and account positions
- dashboard visibility of persisted execution state

What is still more fragile:

- in-memory strategy runtime state
- Redis stream offsets started from new-message positions
- coordinated restarts during active trading

That is why the repo now includes:

- `docs/live-market-restart-runbook.md`
- live-session restart helpers under `ops/systemd/`

Treat those runbooks as part of the current architecture, not as optional side docs.

## Migration Principle

Preserve strategy behavior, replace runtime architecture.

This repo intentionally avoids:

- CSV/JSON as execution truth
- dashboard mutation of bot internals
- direct strategy-to-broker calls
- a single-process trading brain
