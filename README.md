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
- [GitHub Actions Deploy](./docs/github-actions-deploy.md)
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
- GitHub Actions validation runs automatically, but production deploy is manual via [GitHub Actions Deploy](./docs/github-actions-deploy.md) and the VPS script `ops/systemd/deploy_main.sh`

## GitHub Deploy Workflow

This repo now uses:

- automatic validation
- manual production deploy

That means:

- pushes and PRs run the `Validate` workflow automatically
- merging to `main` does **not** restart the VPS by itself
- production deploy happens only when you manually run the `Deploy Main` workflow in GitHub Actions

### Normal Change Flow

Use this as the standard operating flow:

1. Make changes on a branch such as `codex/...`.
2. Push the branch to GitHub.
3. Open a PR.
4. For same-repo PRs into `main`, GitHub adds the `automerge` label by default when the PR is opened or marked ready for review.
5. Wait for the `validate` job to pass.
6. If the PR still has the `automerge` label and is mergeable, GitHub will merge it automatically after `validate` passes.
7. Remove the `automerge` label if you do **not** want the PR to merge automatically.
8. Manually run the deploy workflow when you actually want the VPS updated.

### What You Have To Do To Deploy

After the change is already merged to `main`:

1. Open GitHub `Actions`.
2. Open the workflow named `Deploy Main`.
3. Click `Run workflow`.
4. Select branch `main`.
5. Leave `allow_live_restart` unchecked for normal off-hours deploys.
6. Click `Run workflow`.
7. Watch the `deploy` job SSH into the VPS, fast-forward the checkout to `origin/main`, run install/migrations, restart services, and check `/health`.

`Deploy Main` does **not** rerun tests. It assumes the `main` commit you are deploying already passed the normal `Validate` workflow before merge.

### Optional PR Auto-Merge

This repo can auto-merge PRs into `main` when all of these are true:

- the PR has the label `automerge`
- the PR targets `main`
- the PR is open and not draft
- the PR branch comes from this repository
- the `validate` job passed for the current PR head commit
- GitHub reports the PR is mergeable

Auto-merge does **not** deploy production. Deploy stays manual.

For same-repo PRs into `main`, GitHub now adds the `automerge` label by default on PR open or when a draft PR is marked ready for review.

Remove the label on any PR you want to keep out of auto-merge.

### When To Use `allow_live_restart`

Leave `allow_live_restart` as `false` unless you intentionally want to permit a deploy during ET market hours.

Use `allow_live_restart=true` only when:

- you understand the live restart risk
- you have reviewed open positions and in-flight work
- you are deliberately choosing a live-session deploy

For live-session restart guidance, use:

- [Live Market Restart Runbook](./docs/live-market-restart-runbook.md)

### First-Time GitHub Setup

The deploy workflow requires these repository secrets:

- `VPS_HOST`
- `VPS_USER`
- `VPS_SSH_KEY_BASE64`

`VPS_SSH_KEY_BASE64` is the preferred form of the deploy key because it avoids multiline copy/paste issues in GitHub secrets.

### What The Deploy Job Actually Does

The GitHub Action does not invent a separate deploy path. It uses the repo's checked-in VPS script:

- `ops/systemd/deploy_main.sh`

That script:

1. refuses to run if the VPS checkout is dirty
2. fetches `origin`
3. fast-forwards the VPS checkout to `origin/main`
4. runs `ops/bootstrap/08_install_runtime.sh`
5. restarts the app stack
6. waits for a healthy local `/health` response

By default this is a full app-stack deploy. It restarts:

- `project-mai-tai-market-data.service`
- `project-mai-tai-strategy.service`
- `project-mai-tai-oms.service`
- `project-mai-tai-reconciler.service`
- `project-mai-tai-control.service`

### Practical Rule

If you only remember one thing, remember this:

- merge to `main` when code is ready
- run deploy manually when production should change

### Agent Vs User Responsibilities

Current recommended split:

- agent responsibilities
  - inspect the repo and make code or doc changes
  - add or update tests when behavior changes
  - run local validation where possible
  - commit and push branch work
  - open or prepare PR-ready changes
  - keep `main` as the source of deployment truth
- user responsibilities
  - review PRs or decide when branch work should land on `main`
  - remove the `automerge` label if you do not want a PR to merge automatically
  - manually trigger the production deploy workflow in GitHub Actions
  - decide whether a live-session deploy is acceptable
  - manage GitHub repository settings, secrets, and access policy

In practical day-to-day use:

1. the agent should do the implementation and validation work
2. the agent should push the branch
3. the user should either allow the default `automerge` label to proceed or remove it and merge manually when satisfied
4. the user should run deploy when production should actually change

This split is intentional for safety on a private trading repo where GitHub cannot fully enforce protected-branch policy on the current plan.

## Current Documentation Truth

The README and architecture docs are intended to describe the current code, not just the original March 28-29 build milestone.

If a future change materially alters service ownership, broker modes, stream usage, or restart expectations, update:

- this file
- [docs/architecture.md](./docs/architecture.md)
- the closest folder-level README to the changed code
