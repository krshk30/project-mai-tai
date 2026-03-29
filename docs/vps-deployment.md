# VPS Deployment

## Current VPS Baseline

Verified on the live VPS:
- Ubuntu
- 2 vCPU
- 4 GB RAM
- 120 GB disk
- no swap
- `systemd` available
- no Docker
- no Postgres
- no Redis

## Deployment Target

Use native packages and `systemd`.

Reasons:
- lower operational overhead on a small VPS
- simpler process supervision
- fewer moving parts during migration
- better fit than introducing Docker solely for this project

## Parallel Run Layout

Legacy app remains untouched.

New app layout:
- repo path: `/home/trader/project-mai-tai`
- env path: `/etc/project-mai-tai/`
- log path: `/var/log/project-mai-tai/`
- state path: `/var/lib/project-mai-tai/`
- control-plane bind: `127.0.0.1:8100`

## Planned Services

Systemd units:
- `project-mai-tai-control.service`
- `project-mai-tai-market-data.service`
- `project-mai-tai-strategy.service`
- `project-mai-tai-oms.service`
- `project-mai-tai-reconciler.service`

Local infrastructure:
- `postgresql`
- `redis-server`

## Edge Access

Public edge domain:
- `https://project-mai-tai.live`
- `https://www.project-mai-tai.live` redirects to the apex domain

Planned approach:
- Nginx listens on `80/443`
- basic auth enabled for the new dashboard
- HTTPS via Certbot for `project-mai-tai.live` and `www.project-mai-tai.live`
- control plane remains private behind localhost proxying
- bootstrap starts with an HTTP-only Nginx site, then switches to the final
  HTTPS config after certificate issuance

DNS records:
- `A` record for `project-mai-tai.live` -> `104.236.43.107`
- `CNAME` record for `www.project-mai-tai.live` -> `project-mai-tai.live`

Certificate and proxy plan:
- FastAPI control plane listens on `127.0.0.1:8100`
- Nginx terminates TLS and proxies to `127.0.0.1:8100`
- `auth_basic` protects the operator surface
- broker, strategy, Redis, and Postgres remain private

## Coexistence Rules

- no reuse of the legacy app port
- no reuse of legacy JSON/CSV files
- no reuse of legacy service names
- no direct dependency on legacy process lifetime
- shared Massive/Polygon keys are allowed

## Secrets Handling

Initial approach:
- root-owned env files
- `chmod 600`
- loaded by `systemd EnvironmentFile`
- template source lives at `ops/env/project-mai-tai.env.example`

This is the initial production posture for a single VPS.

## Bootstrap Order

Repository scripts live under `ops/bootstrap/`.

Recommended sequence:
1. install system packages
2. prepare directories and permissions
3. create the Nginx basic-auth file
4. enable the HTTP bootstrap site
5. issue the TLS certificate with Certbot using an operator email address
6. switch to the HTTPS Nginx config
7. initialize Postgres for `project-mai-tai` with the real application password
8. edit `/etc/project-mai-tai/project-mai-tai.env` with real runtime secrets
9. create the Python 3.12 venv, install the app, and run Alembic migrations
10. install the `systemd` units and reload `systemd`
11. enable and start `project-mai-tai.target`

## Concrete Service Install

Systemd assets now live under `ops/systemd/`:
- `project-mai-tai.target`
- `project-mai-tai-control.service`
- `project-mai-tai-market-data.service`
- `project-mai-tai-strategy.service`
- `project-mai-tai-oms.service`
- `project-mai-tai-reconciler.service`

Bootstrap/runtime scripts now cover:
- package install
- host directory preparation
- initial env-file placement
- optional non-interactive dashboard auth creation
- automatic `ufw` web-port allowance when the HTTP site is enabled
- Python 3.12 runtime install
- Alembic migration
- `systemd` unit installation
- full-stack enable/start

Useful operator commands:
- `ops/systemd/status.sh`
- `ops/systemd/restart_all.sh`
- `mai-tai-seed-runtime`

Live-market note:
- do not use `ops/systemd/restart_all.sh` during an active session
- use [Live Market Restart Runbook](./live-market-restart-runbook.md) for coordinated restarts of strategy, OMS, or market data

## Runtime Assumptions

The production units assume:
- repo checkout at `/home/trader/project-mai-tai`
- app user `trader`
- venv at `/home/trader/project-mai-tai/.venv`
- env file at `/etc/project-mai-tai/project-mai-tai.env`
- database and Redis on localhost
- FastAPI control plane bound to `127.0.0.1:8100`

## Alpaca Paper Phase

The repo now supports the current paper-account layout directly:
- `macd_30s` uses its own Alpaca paper account
- `macd_1m` uses its own Alpaca paper account
- `tos` and `runner` share one Alpaca paper account

Environment file fields:
- `MAI_TAI_OMS_ADAPTER=alpaca_paper`
- `MAI_TAI_ALPACA_MACD_30S_API_KEY`
- `MAI_TAI_ALPACA_MACD_30S_SECRET_KEY`
- `MAI_TAI_ALPACA_MACD_1M_API_KEY`
- `MAI_TAI_ALPACA_MACD_1M_SECRET_KEY`
- `MAI_TAI_ALPACA_TOS_RUNNER_API_KEY`
- `MAI_TAI_ALPACA_TOS_RUNNER_SECRET_KEY`

Runtime mapping fields:
- `MAI_TAI_STRATEGY_MACD_30S_ACCOUNT_NAME`
- `MAI_TAI_STRATEGY_MACD_1M_ACCOUNT_NAME`
- `MAI_TAI_STRATEGY_TOS_ACCOUNT_NAME`
- `MAI_TAI_STRATEGY_RUNNER_ACCOUNT_NAME`

When OMS starts, it now seeds the configured strategies and broker accounts into
Postgres automatically. That means the dashboard shows the intended runtime
layout even before the first strategy intent is emitted.

## Schwab Live Phase

The repo now also supports Schwab live execution through the OMS adapter.

Recommended live layout:
- all four strategies share one account name such as `live:schwab_shared`
- all four strategies map to the same Schwab account hash
- OMS refreshes bearer tokens from a writable token-store JSON file

Environment file fields:
- `MAI_TAI_OMS_ADAPTER=schwab`
- `MAI_TAI_STRATEGY_MACD_30S_ACCOUNT_NAME=live:schwab_shared`
- `MAI_TAI_STRATEGY_MACD_1M_ACCOUNT_NAME=live:schwab_shared`
- `MAI_TAI_STRATEGY_TOS_ACCOUNT_NAME=live:schwab_shared`
- `MAI_TAI_STRATEGY_RUNNER_ACCOUNT_NAME=live:schwab_shared`
- `MAI_TAI_SCHWAB_ACCOUNT_HASH`
- `MAI_TAI_SCHWAB_CLIENT_ID`
- `MAI_TAI_SCHWAB_CLIENT_SECRET`
- `MAI_TAI_SCHWAB_TOKEN_STORE_PATH=/var/lib/project-mai-tai/schwab_token.json`

Optional overrides:
- `MAI_TAI_SCHWAB_MACD_30S_ACCOUNT_HASH`
- `MAI_TAI_SCHWAB_MACD_1M_ACCOUNT_HASH`
- `MAI_TAI_SCHWAB_TOS_RUNNER_ACCOUNT_HASH`

Operational expectation:
- the token-store file is root-owned and writable by the runtime user
- the file holds the latest `access_token`, `refresh_token`, and `expires_at`
- OMS refreshes and rewrites the token store as tokens rotate

See [Schwab Onboarding](./schwab-onboarding.md) for the recommended first-run flow.
