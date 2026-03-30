# Project Mai Tai

Production-oriented parallel rebuild of the legacy `momentum-stock-trader` runtime.

This repository is not a UI mock or a scaffold anymore. The current codebase contains:

- a FastAPI control plane
- a market-data gateway for Massive/Polygon snapshots, warmup, trades, and quotes
- a strategy engine for scanner surfaces plus `macd_30s`, `macd_1m`, `tos`, and `runner`
- an OMS/risk service with `simulated`, `alpaca_paper`, and `schwab` broker adapters
- a reconciler that checks OMS truth against broker/account truth
- Postgres-backed execution state and Redis Streams fanout
- VPS deployment assets for `systemd`, Nginx, env management, and restart runbooks

## What This Repo Is Trying To Preserve

The goal is to preserve the legacy strategy behavior while replacing the runtime shell around it.

What stays:

- scanner and bot decision behavior
- operator-first dashboard expectations
- shared-account attribution requirements
- ET-oriented trading assumptions and catalyst windows

What changes:

- no single-process trading runtime
- no CSV/JSON execution truth
- no direct dashboard mutation of in-memory bot state
- no strategy-to-broker shortcuts

## Runtime Topology

Primary runtime code lives in [src/project_mai_tai/README.md](./src/project_mai_tai/README.md).

Service split:

- `control-plane`
  - package: `src/project_mai_tai/services/control_plane.py`
  - wrapper: `services/control-plane/main.py`
  - role: dashboard, admin API, health, scanner, bot, order, position, reconciliation, and shadow views
- `market-data-gateway`
  - package: `src/project_mai_tai/market_data/` and `src/project_mai_tai/services/market_data_gateway.py`
  - wrapper: `services/market-data-gateway/main.py`
  - role: snapshots, quotes, trades, reference data, historical warmup, and subscription fanout
- `strategy-engine`
  - package: `src/project_mai_tai/services/strategy_engine_app.py` and `src/project_mai_tai/strategy_core/`
  - wrapper: `services/strategy-engine/main.py`
  - role: scanner surfaces, watchlists, bot runtimes, and trade intents
- `oms-risk`
  - package: `src/project_mai_tai/oms/` and `src/project_mai_tai/broker_adapters/`
  - wrapper: `services/oms-risk/main.py`
  - role: intent validation, broker submission/cancel, fills, and position/account attribution
- `reconciler`
  - package: `src/project_mai_tai/reconciliation/service.py`
  - wrapper: `services/reconciler/main.py`
  - role: detect quantity drift, average-price drift, stuck orders, and stuck intents

Control plane defaults to `127.0.0.1:8100`. Nginx handles the public edge in production.

## Broker Modes

Current OMS adapters:

- `simulated`
  - default local/dev adapter
- `alpaca_paper`
  - paper-trading mode with the current split-account migration layout
- `schwab`
  - live-trading path with token refresh and shared-account support

Runtime registration and strategy/account seeding live in:

- `src/project_mai_tai/runtime_registry.py`
- `src/project_mai_tai/runtime_seed.py`

## Repo Map

Use these docs to orient quickly:

- [Docs Index](./docs/README.md)
- [Chat Summary 2026-03-29](./docs/chat-summary-2026-03-29.md)
- [Architecture](./docs/architecture.md)
- [Session Handoff 2026-03-29](./docs/session-handoff-2026-03-29.md)
- [Live Market Restart Runbook](./docs/live-market-restart-runbook.md)
- [Active Market Verification Todo](./docs/active-market-verification-todo.md)
- [Schwab Onboarding](./docs/schwab-onboarding.md)
- [VPS Deployment](./docs/vps-deployment.md)
- [Source Layout](./src/README.md)
- [Service Wrappers](./services/README.md)
- [Ops Assets](./ops/README.md)
- [SQL Layout](./sql/README.md)
- [Test Layout](./tests/README.md)

## Local Development

Recommended toolchain:

- Python `3.12`
- `uv`

Typical setup:

1. Create a virtualenv.
2. Install the package in editable mode with dev dependencies.
3. Run migrations.
4. Start whichever services you need.

Windows example:

```powershell
uv venv --python 3.12 .venv
uv pip install --python .venv\Scripts\python.exe -e ".[dev]"
alembic upgrade head
```

macOS/Linux example:

```bash
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -e ".[dev]"
alembic upgrade head
```

You can launch services through the installed console scripts:

- `mai-tai-control`
- `mai-tai-market-data`
- `mai-tai-strategy`
- `mai-tai-oms`
- `mai-tai-reconciler`
- `mai-tai-seed-runtime`

Or through the thin wrapper entrypoints in `services/*/main.py`.

Verification:

- Windows: `.venv\Scripts\python.exe -m pytest -p no:cacheprovider tests`
- macOS/Linux: `.venv/bin/python -m pytest -p no:cacheprovider tests`

## Production/VPS Assets

Operational assets are already in the repo:

- env template: `ops/env/`
- first-run provisioning: `ops/bootstrap/`
- Nginx edge config: `ops/nginx/`
- `systemd` units and restart helpers: `ops/systemd/`

Recommended first-run path is documented in [VPS Deployment](./docs/vps-deployment.md) and [ops/bootstrap/README.md](./ops/bootstrap/README.md).

Important restart rule:

- `ops/systemd/restart_all.sh` is for off-hours or flat-account use
- during an active session, use the coordinated scripts in `ops/systemd/` plus [Live Market Restart Runbook](./docs/live-market-restart-runbook.md)

## Current Documentation Truth

The README and architecture docs are intended to describe the current code, not just the original March 28-29 build milestone.

If a future change materially alters service ownership, broker modes, stream usage, or restart expectations, update:

- this file
- [docs/architecture.md](./docs/architecture.md)
- the closest folder-level README to the changed code
