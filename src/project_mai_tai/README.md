# Package Layout

`project_mai_tai` is the installable application package. This is where the real runtime logic lives.

## Subpackages

- `broker_adapters/`
  - broker-facing implementations and the shared broker protocol
  - see [broker_adapters/README.md](./broker_adapters/README.md)
- `db/`
  - SQLAlchemy models, base metadata, and session construction
- `market_data/`
  - snapshot/trade/quote ingestion, reference caching, payload models, and Redis publishers
- `oms/`
  - intent processing, order/fill persistence, and position/account attribution
- `reconciliation/`
  - periodic drift detection and incident/finding generation
- `services/`
  - service runners and orchestration glue for the runtime processes
- `shadow/`
  - optional legacy-shadow client used by the control plane for comparison views
- `strategy_core/`
  - preserved strategy/scanner logic, indicators, entries, exits, and runner runtime
  - see [strategy_core/README.md](./strategy_core/README.md)

## Cross-Cutting Modules

- `events.py`
  - Redis stream envelope and payload models
- `log.py`
  - logging setup helpers
- `runtime_registry.py`
  - configured strategy and broker-account registration
- `runtime_seed.py`
  - idempotent seeding of strategy/account metadata into Postgres
- `settings.py`
  - env-driven runtime configuration and adapter resolution

## Ownership Boundaries

Use this package layout when deciding where code belongs:

- strategy decisions belong in `strategy_core/` and `services/strategy_engine_app.py`
- broker API calls belong in `broker_adapters/` and can only be triggered by `oms/`
- execution truth belongs in `db/` plus `oms/`
- market-data fetching/subscription logic belongs in `market_data/`
- reconciliation and incident logic belongs in `reconciliation/`
- dashboard rendering, APIs, and operator views belong in `services/control_plane.py`

If a change spans two boundaries, prefer keeping the shared contract in:

- `events.py` for stream payloads
- `settings.py` for configuration
- `runtime_registry.py` for strategy/account registration metadata
