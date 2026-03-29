# Runtime Services Package

This package contains the in-package runtime runners and orchestration glue for the five long-running services.

Files:

- `control_plane.py`
  - FastAPI app, dashboard rendering, JSON APIs, health computation, and operator views
- `market_data_gateway.py`
  - runtime wrapper that launches the market-data package as a service
- `strategy_engine.py`
  - runtime wrapper for the strategy engine service
- `strategy_engine_app.py`
  - the actual strategy runtime implementation, including scanner views, watchlists, bot runtimes, subscriptions, and order-event handling
- `oms_risk.py`
  - runtime wrapper for OMS
- `reconciler.py`
  - runtime wrapper for reconciliation
- `runtime.py`
  - shared signal-handling helpers for clean service shutdown

Important separation:

- files in this package wire long-running service behavior together
- reusable business logic should usually live in `market_data/`, `strategy_core/`, `oms/`, or `reconciliation/`

If you are deciding whether code belongs here, a useful rule is:

- lifecycle, stream-loop, and service-heartbeat code belongs here
- pure strategy, persistence, or broker-translation logic usually belongs elsewhere
