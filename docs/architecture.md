# Architecture

## Purpose

`project-mai-tai` replaces the legacy single-process trading runtime with a durable, restart-safe platform while preserving the working strategy logic.

The legacy application remains online during migration.

## Service Topology

### `services/control-plane`

Responsibilities:
- operator dashboard
- admin API
- health and incident views
- reconciliation views
- shadow-vs-legacy comparison views

Rules:
- never mutates in-memory trading state directly
- all write actions go through OMS APIs or command streams

### `services/market-data-gateway`

Responsibilities:
- owns Massive/Polygon REST and WebSocket access
- normalizes trades, quotes, bars, and snapshot refreshes
- publishes market-data events into Redis Streams

Rules:
- single owner of market-data connections for the new platform
- no strategy logic

### `services/strategy-engine`

Responsibilities:
- runs strategy libraries for:
  - 30s MACD
  - 1m MACD
  - TOS
  - Runner
- consumes normalized market-data events
- emits trade intents

Rules:
- no direct broker calls
- no authoritative position storage
- deterministic logic whenever possible

### `services/oms-risk`

Responsibilities:
- validates trade intents
- applies account and strategy risk checks
- submits, cancels, and replaces broker orders
- persists broker orders, events, fills, and derived positions
- exposes strategy-attributed virtual positions inside shared brokerage accounts

Rules:
- only service allowed to talk to broker trading APIs
- source of execution truth is Postgres, reconciled against broker truth

### `services/reconciler`

Responsibilities:
- compares broker account positions/orders to OMS state
- creates incidents and repair recommendations
- verifies recovery after restart

Rules:
- repairs happen through OMS flows, never by manual mutation of tracker state

## Persistence Model

### Postgres

Source of truth for:
- broker accounts
- strategies
- trade intents
- broker orders
- broker order events
- fills
- account positions
- virtual per-strategy positions
- reconciliation findings
- health heartbeats
- operator incidents

### Redis Streams

Used for:
- market-data fanout
- internal commands
- background workflow triggers

Not used as the final source of truth.

## Shared Account Model

The platform must support:
- separate Alpaca paper accounts during migration
- one shared Charles Schwab live account later

To support this, every order and fill must be attributable by:
- `broker_account_id`
- `strategy_id`
- `bot_id`
- `intent_id`
- `client_order_id`

Two levels of position state are required:
- `account_positions`: what the broker account actually holds
- `virtual_positions`: strategy-level attribution inside that account

## Operator Surface

The dashboard is a first-class requirement, not an afterthought.

Minimum operator views:
- overall health
- scanner pipeline
- confirmed candidates
- strategy state
- order timeline
- fills
- virtual positions
- account positions
- reconciliation findings
- shadow divergence vs legacy

## Security Model

Initial production approach:
- services bind to localhost where possible
- Nginx reverse proxy at the edge
- basic auth for the dashboard
- HTTPS once the domain is available
- root-owned env files under `/etc/project-mai-tai/`

## Migration Principle

Preserve strategy behavior, replace runtime architecture.

This repo intentionally avoids:
- CSV/JSON as authoritative state
- direct dashboard mutation of internals
- single-process trading brain
- broker logic embedded inside strategy code
