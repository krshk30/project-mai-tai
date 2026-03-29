# OMS Package

This package owns execution-side state transitions inside Mai Tai.

Files:

- `service.py`
  - runtime service that consumes trade intents, applies risk checks, selects the adapter, submits/cancels orders, and publishes order events
- `store.py`
  - persistence and state-transition helper for strategies, broker accounts, intents, orders, fills, and derived positions

Responsibility boundary:

- OMS is the only part of Mai Tai allowed to talk to broker adapters
- strategy code can ask for an action by emitting an intent, but it cannot submit orders itself
- durable execution truth is written through this package into Postgres

Key concepts managed here:

- strategy/account seeding
- risk checks
- client-order-id ownership
- broker order and fill persistence
- `virtual_positions`
- `account_positions`
- broker-position sync

Broker-specific transport logic lives next door in:

- `../broker_adapters/README.md`
