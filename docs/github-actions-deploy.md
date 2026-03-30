# GitHub Actions Deploy

This repo now supports a GitHub Actions path that matches the intended workflow:

1. make changes
2. validate tests and lint
3. push to GitHub
4. deploy `main` to the VPS manually

Workflow file:

- `.github/workflows/validate-and-deploy.yml`
- `.github/workflows/automerge-pr.yml`
- `.github/workflows/default-automerge-label.yml`

Deploy script used on the VPS:

- `ops/systemd/deploy_main.sh`

## Trigger Behavior

Validation runs on:

- pull requests
- pushes to `main`
- pushes to `codex/**`
- manual workflow dispatch

Auto-merge runs when:

- a `Validate And Deploy` workflow run for a PR finishes successfully
- or a PR is labeled `automerge`

Deploy runs only when:

- the workflow is started manually with `workflow_dispatch`
- the selected ref is `main`
- validation in that same run already passed

PR auto-merge is separate from deploy. A PR can merge automatically into `main`, but production still changes only when someone manually runs deploy.

## PR Auto-Merge Behavior

PRs into `main` can be auto-merged when all of these are true:

- the PR has the `automerge` label
- the PR is open and not draft
- the PR branch comes from this repository
- the latest `validate` check for the PR head SHA passed
- GitHub reports the PR is mergeable

If any of those conditions is not true, the auto-merge workflow exits without merging.

For same-repo PRs into `main`, the `default-automerge-label.yml` workflow now adds the `automerge` label automatically when the PR is opened, reopened, or marked ready for review.

Remove the label on any PR you do not want merged automatically.

## Deploy Safety Guard

Manual deploys are blocked during ET market hours unless the workflow run explicitly sets:

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

## What The Deploy Script Does

`ops/systemd/deploy_main.sh` runs on the VPS and:

1. refuses to run if the checkout is dirty
2. fetches `origin`
3. fast-forwards the checked-out branch to `origin/main`
4. runs `ops/bootstrap/08_install_runtime.sh`
5. restarts the stack with `ops/systemd/restart_all.sh`
6. waits for all five services plus a healthy local `/health` response

This means GitHub Actions deploys are using the same install/restart path we already verified on the server.

## Recommended Operating Model

On the current GitHub plan for a private repository, branch protection is not enforced. Because of that, the safer operating model is:

1. push branches and PRs normally
2. let `validate` run automatically
3. let the default `automerge` label stay on PRs you want merged automatically after validation, or remove it when you do not
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
