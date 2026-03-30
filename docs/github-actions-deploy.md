# GitHub Actions Deploy

This repo now supports a GitHub Actions path that matches the intended workflow:

1. make changes
2. validate tests and lint
3. push to GitHub
4. deploy `main` to the VPS manually

For the operator decision model, agent-vs-user actions, and risk boundaries, see:

- `docs/deployment-operating-model.md`

Workflow file:

- `.github/workflows/validate.yml`
- `.github/workflows/deploy-main.yml`
- `.github/workflows/deploy-service.yml`
- `.github/workflows/automerge-pr.yml`

Deploy script used on the VPS:

- `ops/systemd/deploy_main.sh`

## Trigger Behavior

Validation runs on:

- pull requests
- pushes to `main`
- pushes to `codex/**`

Auto-merge runs when:

- a `Validate` workflow run for a PR finishes successfully
- or a PR's draft/label state changes and it becomes eligible again

Full-stack deploy runs only when:

- the `Deploy Main` workflow is started manually with `workflow_dispatch`
- the selected ref is `main`

Service-scoped deploy runs only when:

- the `Deploy Service` workflow is started manually with `workflow_dispatch`
- the selected ref is `main`

PR auto-merge is separate from deploy. A PR can merge automatically into `main`, but production still changes only when someone manually runs `Deploy Main` or `Deploy Service`.

## PR Auto-Merge Behavior

PRs into `main` can be auto-merged when all of these are true:

- the PR is open and not draft
- the PR branch comes from this repository
- the latest `validate` check for the PR head SHA passed
- GitHub reports the PR is mergeable
- the PR does not have the `manual-merge` label

If any of those conditions is not true, the auto-merge workflow exits without merging.

For same-repo PRs into `main`, auto-merge is default-on.

Add the `manual-merge` label on any PR you do not want merged automatically.

## Deploy Safety Guard

Manual deploys are blocked during ET market hours unless the workflow run explicitly sets:

- `allow_live_restart = true`

For `Deploy Service`, that live-session block applies to:

- `strategy`
- `oms`
- `market-data`

`control` and `reconciler` are treated as lower-risk service deploys.

This matches the repo's current restart-safety reality:

- off-hours deploys can use full-stack restart flow
- live-session restarts still require operator judgment

## Required GitHub Secrets

Add these repository secrets before enabling production deploys:

- `VPS_HOST`
  - example: `104.236.43.107`
- `VPS_USER`
  - example: `trader`
- `VPS_SSH_KEY`
  - private key that GitHub Actions should use to SSH into the VPS
- `VPS_SSH_KEY_BASE64`
  - optional safer alternative to `VPS_SSH_KEY`
  - if present, the workflow prefers this value and base64-decodes it into the key file

The VPS itself must already be able to `git fetch origin` for this repo.

Recommended practice:

- prefer `VPS_SSH_KEY_BASE64`
- use `VPS_SSH_KEY` only if you are confident the multiline private key was pasted cleanly

Windows command to generate the base64 form of the deploy key:

```powershell
[Convert]::ToBase64String([Text.Encoding]::ASCII.GetBytes((Get-Content C:\Users\kkvkr\.ssh\id_ed25519_codex_vps -Raw)))
```

Paste that single-line output into the GitHub secret `VPS_SSH_KEY_BASE64`.

GitHub repository setting to verify:

- `Settings -> Actions -> General -> Workflow permissions -> Read and write permissions`

The auto-merge workflow needs a writable `GITHUB_TOKEN` to merge PRs. If GitHub Actions is left on read-only workflow permissions, auto-merge can fail with `Resource not accessible by integration`.

## What The Deploy Script Does

`ops/systemd/deploy_main.sh` runs on the VPS and:

1. refuses to run if the checkout is dirty
2. fetches `origin`
3. fast-forwards the checked-out branch to `origin/main`
4. runs `ops/bootstrap/08_install_runtime.sh`
5. restarts the stack with `ops/systemd/restart_all.sh`
6. waits for all five services plus a healthy local `/health` response

This means GitHub Actions deploys are using the same install/restart path we already verified on the server.

`Deploy Main` does not rerun test/lint validation. The intended model is:

1. `Validate` runs automatically on PRs and pushes
2. `main` only receives changes that already passed validation
3. `Deploy Main` updates the VPS to the already-validated `origin/main`

`ops/systemd/deploy_service.sh` is the lower-blast-radius path. It:

1. refuses to run if the checkout is dirty
2. fast-forwards the checked-out branch to `origin/main`
3. refreshes the runtime environment
4. optionally skips migrations unless `run_migrations=true`
5. restarts only the selected service or coordinated service pair
6. prints the current `/health` payload after the restart

Service targets currently supported:

- `control`
- `reconciler`
- `strategy`
- `oms`
- `market-data`

Coordinated behavior:

- `oms` deploy stops `strategy`, restarts `oms`, then starts `strategy` again unless `hold_strategy=true`
- `market-data` deploy stops `strategy`, restarts `market-data`, then starts `strategy` again unless `hold_strategy=true`

## Recommended Operating Model

On the current GitHub plan for a private repository, branch protection is not enforced. Because of that, the safer operating model is:

1. push branches and PRs normally
2. let `validate` run automatically
3. let PRs auto-merge by default after validation, or add `manual-merge` when you do not want that
4. merge to `main`
5. run deploy manually from Actions when you intentionally want production updated

This avoids automatic VPS restarts from any accidental direct push to `main`.

## Operational Note

If a live-session deploy is ever required, prefer:

- manual `workflow_dispatch`
- `allow_live_restart = true`
- explicit operator review of open positions and restart risk first

For the full live-session guidance, see:

- `docs/live-market-restart-runbook.md`
