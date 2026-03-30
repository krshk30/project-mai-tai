# Deployment Operating Model

This document describes the current production deployment design and the recommended
risk-based operating model for working with Codex on a live trading system.

As of March 30, 2026, the repo is intentionally split into:

- automatic validation
- automatic PR merge in most cases
- manual production deploy execution

That split is deliberate. The code integration path is automated, but live runtime restarts
still have real risk for `strategy`, `oms`, and `market-data`.

## What Happens Automatically Today

GitHub currently automates these steps:

1. The agent pushes a branch such as `codex/...`.
2. GitHub runs `Validate`.
3. If the PR targets `main`, is mergeable, is not draft, and does not have the label
   `manual-merge`, GitHub auto-merges it into `main`.

GitHub does not originate code pushes by itself. A branch still has to be pushed by the
agent or by you before any GitHub automation starts.

GitHub does **not** automatically deploy production after merge.

That means:

- code can land on `main` automatically
- production only changes when someone explicitly runs `Deploy Main` or `Deploy Service`

## Current Deployment Path

The current deploy chain is:

1. GitHub workflow starts manually.
2. GitHub Actions SSHes to the VPS.
3. The VPS checkout fast-forwards to `origin/main`.
4. The runtime environment is refreshed.
5. The selected restart path runs.
6. Health checks are evaluated.

There are two deploy workflows:

- `Deploy Main`
  - full-stack deploy
  - uses `ops/systemd/deploy_main.sh`
- `Deploy Service`
  - lower-blast-radius deploy
  - uses `ops/systemd/deploy_service.sh`

## What Each Workflow Actually Does

### Validate

Workflow:
- `.github/workflows/validate.yml`

Runs automatically on:
- pull requests
- pushes to `main`
- pushes to `codex/**`

Runs:
- unit tests
- integration and replay tests
- Ruff lint

Purpose:
- authoritative clean-room validation tied to the exact commit SHA

### Auto Merge PR

Workflow:
- `.github/workflows/automerge-pr.yml`

Default behavior:
- same-repo PRs into `main` auto-merge after `Validate` passes

Opt-out:
- add the `manual-merge` label

Important note:
- auto-merge changes Git history only
- auto-merge does **not** deploy the VPS

### Deploy Main

Workflow:
- `.github/workflows/deploy-main.yml`

Trigger:
- manual `workflow_dispatch`

Behavior:
1. requires `main`
2. checks secrets
3. blocks ET market-hour full-stack deploy unless `allow_live_restart=true`
4. SSHes to the VPS
5. fast-forwards the checkout to `origin/main`
6. runs `ops/bootstrap/08_install_runtime.sh`
7. runs `ops/systemd/restart_all.sh`
8. waits for all five services plus healthy `/health`

Default use:
- off-hours production deploy
- post-merge full-stack deploy when the account is flat

### Deploy Service

Workflow:
- `.github/workflows/deploy-service.yml`

Trigger:
- manual `workflow_dispatch`

Targets:
- `control`
- `reconciler`
- `strategy`
- `oms`
- `market-data`

Behavior:
1. requires `main`
2. checks secrets
3. fast-forwards the checkout to `origin/main`
4. refreshes the runtime environment
5. optionally runs migrations if `run_migrations=true`
6. restarts only the selected service path

Special service behavior:
- `control`
  - restarts only control plane
- `reconciler`
  - restarts only reconciler
- `strategy`
  - restarts only strategy
- `oms`
  - stops strategy
  - restarts OMS
  - restarts strategy unless `hold_strategy=true`
- `market-data`
  - stops strategy
  - restarts market data
  - restarts strategy unless `hold_strategy=true`

Market-hour guard:
- `control` and `reconciler` are treated as lower-risk
- `strategy`, `oms`, and `market-data` are blocked during ET market hours unless
  `allow_live_restart=true`

## What Is Still Manual

Production deploy execution is still manual by design.

The manual parts today are:

- choosing whether production should change at all
- choosing full-stack vs service-scoped deploy
- choosing whether a risky live-session restart is acceptable
- choosing whether migrations are safe during a given window

Why this is still manual:

- `strategy` does not fully rehydrate runtime bot state after restart
- `oms` does not safely buffer/replay intents while down
- `market-data` isolated restart can lose subscription continuity if not coordinated

These are runtime resilience gaps, not GitHub workflow gaps.

## What The Agent Does Today

Current default Codex responsibilities:

1. inspect the repo and implement the change
2. add or update tests when behavior changes
3. run local validation where possible
4. push a branch to GitHub
5. open a PR
6. monitor validation results
7. explain the correct deploy path for the risk level

Today, Codex should treat these as deploy zones:

- Green zone
  - off-hours `Deploy Main`
  - off-hours `Deploy Service`
  - live `control`
  - live `reconciler`
- Yellow zone
  - live `strategy`
  - live `oms`
  - live `market-data`
  - only when state looks clean
- Red zone
  - open positions with unclear runtime state
  - pending fills or cancels
  - reconciliation already critical
  - schema migrations during live trading
  - any restart where the operator does not understand the current account state

## Your Actions Today

### Normal Off-Hours Change

Use this when:
- market is closed
- no urgent live-session constraint exists

Steps:
1. let the agent push the branch and open the PR
2. let `Validate` pass
3. allow auto-merge, or add `manual-merge` and merge yourself
4. run `Deploy Main`
5. verify the workflow finishes green

### Low-Risk Live Change

Use this for:
- `control`
- `reconciler`

Steps:
1. let the agent push the branch and open the PR
2. let `Validate` pass
3. merge the PR or let it auto-merge
4. run `Deploy Service`
5. choose `control` or `reconciler`
6. leave `allow_live_restart=false`
7. verify the workflow finishes green

### Higher-Risk Live Change

Use this for:
- `strategy`
- `oms`
- `market-data`

Current safe steps:
1. let the agent push the branch and open the PR
2. let `Validate` pass
3. merge the PR or let it auto-merge
4. decide whether the account state is clean enough for a live restart
5. if yes, run `Deploy Service`
6. choose the risky target
7. use `hold_strategy=true` for `oms` or `market-data` if you want strategy to stay stopped
8. set `allow_live_restart=true` only if you intentionally approve the live restart risk
9. verify the service-specific post-checks in `docs/live-market-restart-runbook.md`

### Red-Zone Case

Use this when:
- positions are open and state is unclear
- fills/cancels are still moving
- reconciliation is already showing serious drift

Steps:
1. do not run `Deploy Main`
2. do not run risky `Deploy Service`
3. stop and review `/api/orders`, `/api/positions`, and `/api/reconciliation`
4. if needed, keep `strategy` stopped and leave `oms` running
5. make a conscious human decision before any further restart

## Recommended Future Operating Model

This is the model I recommend for reducing manual work while keeping live risk honest.

### Green Zone

Codex acts without asking:

- off-hours `Deploy Main`
- off-hours `Deploy Service`
- live `Deploy Service` for `control`
- live `Deploy Service` for `reconciler`

### Yellow Zone

Codex runs automated preflight first:

- live `strategy`
- live `oms`
- live `market-data`

If preflight is clean:
- Codex can proceed without asking

If preflight is not clean:
- Codex stops and asks you

### Red Zone

Human approval remains required until runtime restart safety improves.

## Best Improvements From Here

If you want less manual production work, the most valuable next steps are:

1. Add automated preflight checks for risky live deploys.
   Suggested checks:
   - pending/submitted/accepted intents
   - recent fills/cancels still settling
   - reconciliation critical findings
   - open account positions
   - stale heartbeat or service degradation

2. Pin deploys to a specific commit SHA instead of always deploying the moving tip of `main`.

3. Add rollback workflow by previously deployed SHA.

4. Add service-specific post-deploy verification instead of relying mostly on generic `/health`.

5. Improve runtime resilience:
   - strategy rehydration
   - OMS intent buffering/replay
   - market-data subscription reseeding

These runtime improvements are what would truly allow more agent-owned live deploys.

## Practical Summary

If you only remember one thing:

- code integration is mostly automated now
- production deploy execution is still explicit
- low-risk deploys can be mostly agent-owned
- risky live trading deploys still need either preflight automation or human approval
