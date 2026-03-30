# Operator Cheat Sheet

Use this when Codex tells you to deploy or restart something and you want the shortest
possible “what do I click, where do I go” answer.

## Default Rule

If Codex says:

- `Deploy Main`
  - go to GitHub Actions
- `Deploy Service`
  - go to GitHub Actions
- `manual VPS restart`
  - SSH to the VPS and run the named script

If a risky live `Deploy Service` run fails preflight:

- do **not** keep retrying casually
- wait for Codex to interpret the failure and tell you whether to stop, retry later, or use the manual VPS path

## GitHub Location

Go here:

1. open the repository on GitHub
2. click `Actions`
3. choose the workflow Codex named

The current workflows you may need are:

- `Validate`
- `Deploy Main`
- `Deploy Service`

## Off-Hours Full Deploy

Use when Codex says:
- run `Deploy Main`

Steps:

1. Open `Actions`.
2. Open `Deploy Main`.
3. Click `Run workflow`.
4. Choose branch `main`.
5. Leave `allow_live_restart` unchecked.
6. Click `Run workflow`.
7. Wait for the job to finish green.

Expected result:

- VPS fast-forwards to `origin/main`
- runtime is refreshed
- all five services restart
- `/health` returns healthy

## Low-Risk Live Deploy

Use when Codex says:
- run `Deploy Service` for `control`
- run `Deploy Service` for `reconciler`

Steps:

1. Open `Actions`.
2. Open `Deploy Service`.
3. Click `Run workflow`.
4. Choose branch `main`.
5. Set `service` to `control` or `reconciler`.
6. Leave `run_migrations` unchecked.
7. Leave `allow_live_restart` unchecked.
8. Click `Run workflow`.
9. Wait for the job to finish green.

Expected result:

- only the selected low-risk service restarts

## Risky Live Deploy

Use when Codex says:
- run `Deploy Service` for `strategy`
- run `Deploy Service` for `oms`
- run `Deploy Service` for `market-data`

Steps:

1. Open `Actions`.
2. Open `Deploy Service`.
3. Click `Run workflow`.
4. Choose branch `main`.
5. Set `service` to `strategy`, `oms`, or `market-data`.
6. Leave `run_migrations` unchecked unless Codex explicitly says otherwise.
7. If Codex says to keep strategy down after `oms` or `market-data`, set `hold_strategy=true`.
8. Set `allow_live_restart=true`.
9. Click `Run workflow`.

What happens next:

- if automated preflight is clean, the deploy continues
- if automated preflight is not clean, the workflow fails without restarting

If preflight fails:

- do not keep rerunning it blindly
- wait for Codex to tell you whether the state is safe later or whether to use manual VPS handling

## When You Need VPS

Only go to VPS when one of these is true:

- Codex explicitly says `manual VPS restart`
- a risky live `Deploy Service` preflight fails and Codex tells you to use the VPS path
- Codex says the situation is red-zone and needs operator-guided handling

## VPS Login

From Windows PowerShell:

```powershell
C:\Windows\System32\OpenSSH\ssh.exe -i C:\Users\kkvkr\.ssh\id_ed25519_codex_vps trader@104.236.43.107
```

After login:

```bash
cd /home/trader/project-mai-tai
git fetch origin
git checkout main
git merge --ff-only origin/main
sudo MAI_TAI_RUN_MIGRATIONS=0 bash ops/bootstrap/08_install_runtime.sh /home/trader/project-mai-tai
```

Only set `MAI_TAI_RUN_MIGRATIONS=1` if Codex explicitly tells you to do that off-hours.

## VPS Restart Commands

If Codex says `manual VPS restart`, use the matching script.

Control:

```bash
bash ops/systemd/restart_control_live.sh
```

Reconciler:

```bash
bash ops/systemd/restart_reconciler_live.sh
```

Strategy:

```bash
bash ops/systemd/restart_strategy_live.sh
```

OMS:

```bash
bash ops/systemd/restart_oms_live.sh
```

OMS and keep strategy stopped:

```bash
bash ops/systemd/restart_oms_live.sh --hold-strategy
```

Market data:

```bash
bash ops/systemd/restart_market_data_live.sh
```

Market data and keep strategy stopped:

```bash
bash ops/systemd/restart_market_data_live.sh --hold-strategy
```

## Quick Decision Table

Use GitHub Actions:

- `Deploy Main`
- `Deploy Service` for `control`
- `Deploy Service` for `reconciler`
- risky `Deploy Service` when Codex tells you to try the automated path first

Use VPS:

- when Codex explicitly says `manual VPS restart`
- when a risky live preflight fails and Codex tells you to use the manual path

## If You Are Unsure

Use this rule:

- if Codex names a GitHub workflow, stay in GitHub
- if Codex names a VPS script, SSH to the VPS
- if a risky live deploy fails preflight, stop and ask Codex what to do next
