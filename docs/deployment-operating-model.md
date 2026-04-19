# Deployment Operating Model

This document describes the current production deployment design and the
standard operating model for working with Codex on a live trading system.

## Required Branch And Deploy Rule

This is the default rule going forward:

- `main` is the only deployable branch
- the VPS should always run `origin/main`
- feature branches such as `codex/...` are for development and validation only
- do not treat a feature branch as "live" unless there is a true emergency and
  that exception is written down explicitly in the session handoff

Practical meaning:

1. build and test on `codex/...`
2. push branch and open PR
3. wait for GitHub `Validate` to pass
4. merge into `main`
5. update local `main`
6. pull `origin/main` on the VPS
7. restart only the required services
8. verify local `main`, GitHub `main`, and VPS `main` are all on the same SHA
9. record that deployed SHA in the session handoff

As of March 30, 2026, the repo is intentionally split into:

- automatic validation
- automatic PR merge in most cases
- manual production deploy execution

That split is deliberate. The code integration path is automated, but live runtime restarts
still have real risk for `strategy`, `oms`, and `market-data`.

## Standard Release Flow

Use this for normal work unless there is a clearly documented emergency.

### 1. Develop On A Feature Branch

- create or continue a branch such as `codex/...`
- keep local work there until validation is complete
- do not deploy this branch to the VPS as the normal path

### 2. Validate Before Merge

- run the relevant local tests first
- push the branch
- open or update the PR
- wait for GitHub `Validate` to pass on the branch head

### 3. Merge To Main

- merge only after green validation
- if a new commit lands on the branch, validation must go green again before
  merge

### 4. Deploy Only From Main

- switch local checkout back to `main` and pull `origin/main`
- on the VPS, check out `main` and pull `origin/main`
- restart only the services required for the change

### 5. Verify Alignment

Before calling the change live, verify:

- local `main` SHA
- GitHub `main` SHA
- VPS `main` SHA

All three should match.

### 6. Update The Session Log Immediately

After deploy, update the handoff right away with:

- deployed SHA
- branch/PR reference if relevant
- what changed
- what was validated
- any operational exceptions

Do not rely on a later reminder to document it.

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
8. waits for all six services plus healthy `/health`

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
- `tv-alerts`
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
- `tv-alerts`
  - restarts only the TradingView alert sidecar
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
- `control`, `reconciler`, and `tv-alerts` are treated as lower-risk
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
7. merge validated work to `main`
8. deploy the VPS from `main`
9. verify SHA alignment across local, GitHub, and VPS
10. update the session handoff immediately

This means Codex should not leave the system in one of these drifted states
without explicitly calling it out:

- GitHub `main` ahead of VPS `main`
- VPS running a feature branch
- local branch state not reflected in `main`
- live deploy completed but handoff not updated

Today, Codex should treat these as deploy zones:

- Green zone
  - off-hours `Deploy Main`
  - off-hours `Deploy Service`
  - live `control`
  - live `reconciler`
  - live `tv-alerts`
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
3. merge into `main`
4. deploy from `main`
5. verify local/GitHub/VPS SHA alignment
6. update the session handoff immediately

### Low-Risk Live Change

Use this for:
- `control`
- `reconciler`
- `tv-alerts`

Steps:
1. let the agent push the branch and open the PR
2. let `Validate` pass
3. merge into `main`
4. deploy from `main`
5. choose `control`, `reconciler`, or `tv-alerts`
6. leave `allow_live_restart=false`
7. verify SHA alignment and service health
8. update the session handoff immediately

### Higher-Risk Live Change

Use this for:
- `strategy`
- `oms`
- `market-data`

Current safe steps:
1. let the agent push the branch and open the PR
2. let `Validate` pass
3. merge into `main`
4. decide whether the account state is clean enough for a live restart
5. if yes, deploy from `main`
6. choose the risky target
7. use `hold_strategy=true` for `oms` or `market-data` if you want strategy to stay stopped
8. set `allow_live_restart=true` only if you intentionally approve the live restart risk
9. verify the service-specific post-checks in `docs/live-market-restart-runbook.md`
10. verify SHA alignment and update the session handoff immediately

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
- live `Deploy Service` for `tv-alerts`

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

## Release Checklist

Use this every time unless the session handoff explicitly documents an
exception.

1. work on `codex/...`
2. run local validation
3. push branch and update PR
4. wait for green GitHub `Validate`
5. merge into `main`
6. update local `main`
7. update VPS `main`
8. restart only required services
9. verify local/GitHub/VPS SHA match
10. verify live health
11. update the session handoff immediately

## Practical Summary

If you only remember one thing:

- `main` is the release branch
- VPS should stay on `main`
- production deploy execution is still explicit
- low-risk deploys can be mostly agent-owned
- risky live trading deploys still need either preflight automation or human approval
