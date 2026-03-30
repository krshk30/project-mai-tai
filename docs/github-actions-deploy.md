# GitHub Actions Deploy

This repo now supports a GitHub Actions path that matches the intended workflow:

1. make changes
2. validate tests and lint
3. push to GitHub
4. deploy `main` to the VPS

Workflow file:

- `.github/workflows/validate-and-deploy.yml`

Deploy script used on the VPS:

- `ops/systemd/deploy_main.sh`

## Trigger Behavior

Validation runs on:

- pull requests
- pushes to `main`
- pushes to `codex/**`
- manual workflow dispatch

Deploy runs only when:

- the ref is `main`
- validation already passed
- the event is `push` or `workflow_dispatch`

## Safety Guard

Automatic deploys are blocked during ET market hours unless a manual workflow run explicitly sets:

- `allow_live_restart = true`

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

The VPS itself must already be able to `git fetch origin` for this repo.

## What The Deploy Script Does

`ops/systemd/deploy_main.sh` runs on the VPS and:

1. refuses to run if the checkout is dirty
2. fetches `origin`
3. fast-forwards the checked-out branch to `origin/main`
4. runs `ops/bootstrap/08_install_runtime.sh`
5. restarts the stack with `ops/systemd/restart_all.sh`
6. waits for all five services plus a healthy local `/health` response

This means GitHub Actions deploys are using the same install/restart path we already verified on the server.

## Operational Note

If a live-session deploy is ever required, prefer:

- manual `workflow_dispatch`
- `allow_live_restart = true`
- explicit operator review of open positions and restart risk first

For the full live-session guidance, see:

- `docs/live-market-restart-runbook.md`
