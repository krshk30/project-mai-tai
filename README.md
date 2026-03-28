## Project Mai Tai

Production-oriented replacement for the legacy `momentum-stock-trader` platform.

This repository is a parallel rebuild, not a refactor-in-place.

Goals:
- preserve proven strategy behavior from the legacy repo
- replace the runtime shell with a durable OMS, broker abstraction, and restart-safe state
- run beside the legacy system on the same VPS with no interruption
- support Alpaca paper trading first and Charles Schwab live trading later

Initial scope:
- 30s MACD
- 1m MACD
- TOS
- Runner
- no News Bot

Core decisions:
- Python-first stack
- FastAPI control plane
- Postgres as source of truth
- Redis Streams as internal event bus
- native VPS services managed by `systemd`
- `Nginx + basic auth + HTTPS` once the domain is available

See:
- [Architecture](./docs/architecture.md)
- [Implementation Roadmap](./docs/implementation-roadmap.md)
- [VPS Deployment](./docs/vps-deployment.md)
- [Strategy Preservation](./docs/strategy-preservation.md)

Executable scaffold:
- shared Python package under `src/project_mai_tai/`
- FastAPI control plane entrypoint
- worker entrypoints for market data, strategy, OMS, and reconciliation
- initial Alembic migration and Postgres schema
- typed event contracts for Redis stream payloads

Local bootstrap commands once dependencies are installed:
- `alembic upgrade head`
- `python services/control-plane/main.py`
- `python services/market-data-gateway/main.py`
- `python services/strategy-engine/main.py`
- `python services/oms-risk/main.py`
- `python services/reconciler/main.py`
