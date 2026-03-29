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
- [Session Handoff 2026-03-29](./docs/session-handoff-2026-03-29.md)
- [Architecture](./docs/architecture.md)
- [Active Market Verification Todo](./docs/active-market-verification-todo.md)
- [Live Market Restart Runbook](./docs/live-market-restart-runbook.md)
- [Implementation Roadmap](./docs/implementation-roadmap.md)
- [Schwab Onboarding](./docs/schwab-onboarding.md)
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

## Local Dev

Recommended toolchain:
- `uv`
- `Python 3.12`

Fresh setup:
- `uv python install 3.12`
- `uv venv --python 3.12 .venv`
- `uv pip install --python .venv/bin/python -e ".[dev]"`

Test run:
- `.venv/bin/python -m pytest`

## VPS Runtime Assets

The repo now includes concrete VPS deployment assets for the parallel stack:
- production env template in `ops/env/`
- first-run bootstrap scripts in `ops/bootstrap/`
- `systemd` units and helpers in `ops/systemd/`

The intended production path is:
1. bootstrap packages, directories, auth, and database
2. edit `/etc/project-mai-tai/project-mai-tai.env`
3. run `ops/bootstrap/08_install_runtime.sh`
4. run `ops/bootstrap/09_install_systemd_units.sh`
5. run `ops/bootstrap/10_enable_services.sh`
6. run `mai-tai-seed-runtime` if you want an explicit metadata seed outside OMS startup

## Paper Trading Config

The new runtime now supports the current Alpaca paper-account layout directly:
- `macd_30s` -> `MAI_TAI_STRATEGY_MACD_30S_ACCOUNT_NAME`
- `macd_1m` -> `MAI_TAI_STRATEGY_MACD_1M_ACCOUNT_NAME`
- `tos` + `runner` share `MAI_TAI_STRATEGY_TOS_ACCOUNT_NAME` / `MAI_TAI_STRATEGY_RUNNER_ACCOUNT_NAME`

To enable real paper execution instead of the simulated OMS adapter:
- set `MAI_TAI_OMS_ADAPTER=alpaca_paper`
- fill the three Alpaca paper credential pairs in `/etc/project-mai-tai/project-mai-tai.env`
- restart `project-mai-tai-oms.service`

The OMS seeds the configured strategies and broker accounts on startup, so the
dashboard no longer has to wait for the first intent before it shows runtime metadata.

## Schwab Live Config

Schwab live execution is now supported through the OMS broker abstraction.

Recommended live setup:
- set `MAI_TAI_OMS_ADAPTER=schwab`
- point all four `MAI_TAI_STRATEGY_*_ACCOUNT_NAME` values at the same shared live account name
- set `MAI_TAI_SCHWAB_ACCOUNT_HASH`
- set `MAI_TAI_SCHWAB_CLIENT_ID` and `MAI_TAI_SCHWAB_CLIENT_SECRET`
- set `MAI_TAI_SCHWAB_TOKEN_STORE_PATH` to a writable root-owned JSON file

The onboarding and token-store details live in [Schwab Onboarding](./docs/schwab-onboarding.md).
