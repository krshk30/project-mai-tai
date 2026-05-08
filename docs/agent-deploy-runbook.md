## Agent Deploy Runbook

This runbook is the shared operating procedure for multi-agent work on the VPS.

Use this when:

- two agents are working in parallel
- one agent is doing local code work
- one agent is responsible for the production deploy
- the session handoff is the shared source of truth

## Core Rule

For any one change set, only one agent is the deploy owner.

- deploy owner:
  - merges to `main`
  - runs the VPS deploy
  - runs post-deploy validation
  - updates the session handoff with the deployed SHA and live result
- non-deploy agent:
  - edits code locally
  - runs local tests
  - reviews docs and logs
  - may validate read-only on VPS if needed
  - does not restart services or change the VPS checkout

If both agents deploy independently, the session handoff stops being trustworthy.

## Default Production Rule

- `main` is the only deployable branch
- VPS should run `origin/main`
- do not leave the VPS on a feature branch
- do not use ad-hoc `scp` hot patches as the normal path

If an emergency requires a direct VPS patch:

- document it immediately in the session handoff
- describe why normal `main` deploy flow was skipped
- land the same change on GitHub `main` as soon as possible
- return VPS to clean `origin/main`

## Shared Coordination Rule

Before work starts, write this into the current session handoff:

- active owner for Polygon / Schwab / UI / OMS / etc
- deploy owner for the next change
- expected services to restart
- validation owner after deploy

Keep the handoff updated as soon as responsibility changes.

## Pre-Merge Regression Check (mandatory for shared hot files)

Concrete incident this rule prevents — **2026-05-08 commit `d5ac600`** "Finalize Polygon 30s rename on main" silently reverted FOUR previously-shipped fixes (`b6fb7b2`, `4f3c989`, `b24873e`, `6420770`) by deleting 1013 lines across 14 files. The user saw the dashboard CPU saturation come back AND the Path="-" / Close bug return because the rename branch had been based on an older parent and force-applied without diffing against current main. PR #78 had to cherry-pick all four commits back. Multi-hour user-visible regression.

This rule is **mandatory for any commit that touches the shared hot files**:

- `src/project_mai_tai/services/control_plane.py`
- `src/project_mai_tai/services/strategy_engine_app.py`
- `src/project_mai_tai/strategy_core/schwab_native_30s.py`
- `src/project_mai_tai/strategy_core/polygon_30s.py`
- `src/project_mai_tai/strategy_core/bar_builder.py`
- `src/project_mai_tai/oms/service.py`
- `src/project_mai_tai/market_data/gateway.py`

It is also mandatory for **any PR whose net diff against `origin/main` deletes more than 100 lines** in any single file, regardless of which file.

### Required pre-merge step

Before opening or merging the PR, the deploy owner runs:

```powershell
git log --oneline origin/main -- <changed-files>
git diff origin/main...HEAD --stat
```

For each of the **last 10 commits** to each changed file, the deploy owner must inspect:

```powershell
git show <SHA> -- <changed-file>
```

…and confirm that **NONE of those commits' content is being undone** by the current PR. If the current PR removes a function, condition, or line that one of those last 10 commits added, that's a candidate revert and must be either:

- explicitly preserved (re-applied on top of the rebase), or
- explicitly justified in the PR description with the phrase **"Intentionally reverts <SHA>"** and reasoning.

### What goes in the PR description

For PRs that hit the rule (touched a hot file OR deleted >100 lines), the PR description **must include a "Last-N commits review" section**:

```markdown
## Last-10 commits to changed files (per Pre-Merge Regression Check rule)

- <SHA1> "<short title>" — preserved / not relevant
- <SHA2> "<short title>" — preserved / not relevant
- ...

No commits in this list are being reverted by this PR. (If any are, list them
with explicit "Intentionally reverts" justification.)
```

Without this section, the PR should not be merged — admin-merge or otherwise. The check takes ~3 minutes; the consequence of skipping it (multi-hour user-visible regression) is large.

### Why "shared hot files" specifically

`control_plane.py` and `strategy_engine_app.py` accumulate small fixes very fast (cache TTLs, query optimizations, dashboard rendering, heartbeat fields, etc.). A rename or refactor PR that's been pending for a few days easily lands behind 5-10 hot fixes; rebasing without diffing leaves silent reverts. The other listed files have less churn but a regression there directly affects bar-build integrity or order routing — high-stakes. The 100-line threshold is a backstop for files outside this list.

## Normal Flow

### 1. Start Local Work

Deploy owner or non-deploy agent creates or continues a feature branch:

```powershell
git checkout -b codex/<short-name>
```

Or:

```powershell
git checkout codex/<short-name>
git pull --ff-only
```

### 2. Make Local Changes

- keep edits on the feature branch
- run focused local tests
- update docs if behavior or operations changed
- if the change is meaningful, update `docs/session-handoff-global.md` before handing off

### 3. Validate Locally

Run only the relevant tests first, for example:

```powershell
.venv\Scripts\python.exe -m pytest tests\unit\test_polygon_30s_bot.py -q
.venv\Scripts\python.exe -m pytest tests\unit\test_strategy_engine_service.py -q
.venv\Scripts\python.exe -m py_compile src\project_mai_tai\services\strategy_engine_app.py
```

Write into the session handoff:

- what changed
- what tests passed
- what was not tested
- whether this is ready for deploy-owner review

### 4. Push Branch And Open PR

```powershell
git status
git add <files>
git commit -m "fix/<short-description>"
git push -u origin codex/<short-name>
```

Then open or update the PR.

### 5. Wait For Validate

Do not deploy from the branch.

Normal rule:

1. wait for GitHub `Validate` to pass
2. merge into `main`
3. deploy only from `main`

### 6. Merge To Main

After validation passes:

```powershell
git checkout main
git pull --ff-only origin main
```

If merge was done through GitHub UI, local `main` must still be refreshed before any local SHA check.

### 7. Choose Deploy Scope

Use `Deploy Main` only for:

- off-hours full-stack deploys
- broad infrastructure changes
- flat-account restarts where full-stack risk is acceptable

Use `Deploy Service` for:

- `control`
- `reconciler`
- `strategy`
- `oms`
- `market-data`

Preferred rule:

- deploy the smallest service scope that fits the change

## Deploy Decision Table

- UI / API only:
  - deploy `control`
- reconciliation only:
  - deploy `reconciler`
- strategy logic only:
  - deploy `strategy`
- OMS / broker adapter:
  - deploy `oms`
- market-data ingestion / stream plumbing:
  - deploy `market-data`
- broad runtime / dependency / bootstrap change:
  - deploy `main`

## Live Session Safety Rule

During market hours:

- never use full-stack restart casually
- prefer service-scoped deploy
- `strategy`, `oms`, and `market-data` are high-risk
- `oms` and `market-data` must be coordinated with `strategy`

Required choreography:

- for `strategy`:
  - restart `strategy` only
- for `oms`:
  - stop `strategy`
  - restart `oms`
  - start `strategy` again unless intentionally holding it down
- for `market-data`:
  - stop `strategy`
  - restart `market-data`
  - start `strategy` again unless intentionally holding it down

Reference:

- [docs/live-market-restart-runbook.md](C:\Users\kkvkr\OneDrive\Documents\GitHub\project-mai-tai\docs\live-market-restart-runbook.md)
- [docs/deployment-operating-model.md](C:\Users\kkvkr\OneDrive\Documents\GitHub\project-mai-tai\docs\deployment-operating-model.md)

## Recommended Deploy Method

Preferred production deploy path is GitHub Actions, not manual SSH restarts.

### Deploy Main

Use workflow:

- `.github/workflows/deploy-main.yml`

This:

1. requires `main`
2. checks secrets
3. blocks ET market-hour deploy unless explicitly allowed
4. SSHes to the VPS
5. fast-forwards VPS checkout to `origin/main`
6. refreshes runtime
7. restarts all services
8. waits for healthy services and `/health`

### Deploy Service

Use workflow:

- `.github/workflows/deploy-service.yml`

This:

1. requires `main`
2. fast-forwards VPS checkout to `origin/main`
3. refreshes runtime
4. optionally runs migrations
5. restarts only the selected service path

## Manual VPS Verification

After deploy, the deploy owner should verify all three match:

- local `main`
- GitHub `main`
- VPS `main`

### Local SHA

```powershell
git checkout main
git pull --ff-only origin main
git rev-parse HEAD
```

### VPS SHA

```powershell
ssh mai-tai-vps "cd /home/trader/project-mai-tai && git rev-parse HEAD && git status --short && git branch --show-current"
```

Expected:

- branch is `main`
- `git status --short` is empty
- SHA matches local/GitHub `main`

## VPS Post-Deploy Checks

### Service Status

```powershell
ssh mai-tai-vps "sudo systemctl status project-mai-tai-control.service project-mai-tai-market-data.service project-mai-tai-strategy.service project-mai-tai-oms.service project-mai-tai-reconciler.service --no-pager"
```

### Health

```powershell
ssh mai-tai-vps "curl -fsS http://127.0.0.1:8100/health"
```

### Key Logs

```powershell
ssh mai-tai-vps "sudo tail -n 80 /var/log/project-mai-tai/strategy.log"
ssh mai-tai-vps "sudo tail -n 80 /var/log/project-mai-tai/market-data.log"
ssh mai-tai-vps "sudo tail -n 80 /var/log/project-mai-tai/oms.log"
ssh mai-tai-vps "sudo tail -n 80 /var/log/project-mai-tai/control.log"
```

### Dashboard / API

Validate the relevant endpoints:

- `https://project-mai-tai.live/api/overview`
- `https://project-mai-tai.live/api/bots`
- `https://project-mai-tai.live/api/orders`
- `https://project-mai-tai.live/api/positions`
- `https://project-mai-tai.live/api/reconciliation`

Use only the endpoints relevant to the change, plus `/api/overview`.

## What Must Go In Session Handoff

After merge and after deploy, the deploy owner must update `docs/session-handoff-global.md` immediately.

Minimum required fields:

- date/time of deploy
- deploy owner
- branch and PR reference if relevant
- merged SHA on `main`
- VPS SHA after deploy
- services restarted
- whether deploy happened during market hours
- whether account was flat or not
- validations run
- live result
- unresolved risks
- who owns the next follow-up

Recommended format:

### Change

- what changed

### Deploy

- local `main` SHA
- VPS SHA
- workflow used:
  - `Deploy Main` or `Deploy Service`
- service target:
  - `strategy`, `market-data`, etc

### Validation

- local tests
- VPS checks
- dashboard/API checks
- strategy-specific validation

### Result

- accepted / partially accepted / rollback needed

### Next Owner

- who owns the next step

## Multi-Agent Handoff Template

Use this short template inside the session handoff before a deploy:

```text
Deploy owner: <agent/person>
Local code owner: <agent/person>
Active workstream: <Polygon 30s / Schwab 30s / UI / OMS>
Expected service target: <control / strategy / market-data / oms / reconciler / main>
Live-session restart required: <yes/no>
Pre-deploy blockers: <none or list>
Post-deploy validator: <agent/person>
```

And after deploy:

```text
Deployed SHA: <sha>
VPS SHA: <sha>
Workflow: <Deploy Main / Deploy Service>
Service target: <...>
Restart window: <timestamp + timezone>
Validation summary: <short result>
Residual risk: <short result>
Next owner: <agent/person>
```

## Recommended Human Rule

For your setup, the safest working rule is:

1. both agents may code locally
2. both agents may update the session handoff
3. only one agent merges and deploys
4. the other agent stays non-deploying until post-deploy validation is complete

That keeps responsibility clear and makes the session handoff reliable.
