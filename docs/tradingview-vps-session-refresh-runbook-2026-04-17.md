# TradingView VPS Session Refresh Runbook

## Purpose

This runbook explains how the current TradingView automation setup works and how
to refresh the VPS TradingView session if TradingView expires the session later.

Current production split:

- `project-mai-tai.live` and `hook.project-mai-tai.live` run on the VPS
- Schwab/webhook execution runs on the VPS
- TradingView alert automation now also runs on the VPS
- The VPS TradingView browser session is bootstrapped from a working local
  Windows TradingView session export

## Current Working State

As of `2026-04-17`, the following is true:

- VPS TradingView login page is rate-limited if we try to sign in directly
- direct VPS login is **not** the preferred method
- local Windows TradingView session export works
- VPS session injection works
- VPS TradingView alert creation and deletion work
- VPS `project-mai-tai-tv-alerts.service` is running in:
  - `provider=playwright`
  - `auto_sync_enabled=true`

## Important Files

Local repo files:

- [scripts/tradingview_export_session.py](/C:/Users/kkvkr/OneDrive/Documents/GitHub/project-mai-tai/scripts/tradingview_export_session.py)
- [scripts/tradingview_probe_session.py](/C:/Users/kkvkr/OneDrive/Documents/GitHub/project-mai-tai/scripts/tradingview_probe_session.py)
- [docs/tradingview-vps-session-refresh-runbook-2026-04-17.md](/C:/Users/kkvkr/OneDrive/Documents/GitHub/project-mai-tai/docs/tradingview-vps-session-refresh-runbook-2026-04-17.md)

Current local export artifact:

- [tmp_tv_alerts/tv-session-export-live.json](/C:/Users/kkvkr/OneDrive/Documents/GitHub/project-mai-tai/tmp_tv_alerts/tv-session-export-live.json)

Current VPS TradingView browser profile:

- `/home/trader/project-mai-tai/tmp_tv_session_probe/user_data_live`

Current VPS TradingView alerts service:

- `project-mai-tai-tv-alerts.service`

## When To Use This Runbook

Use this runbook if:

- TradingView automation stops creating/removing alerts
- `tv-alerts` health shows `auth_required=true`
- the TradingView browser session expires
- alert automation starts failing after a TradingView logout

## Quick Health Checks

### 1. Check Mai Tai dashboard/backend health on VPS

```powershell
ssh mai-tai-vps "curl -s http://127.0.0.1:8100/health"
```

Expected:

- JSON response
- `status: healthy`

### 2. Check TradingView alerts service on VPS

```powershell
ssh mai-tai-vps "curl -s http://127.0.0.1:8110/health"
```

Expected:

- `provider: playwright`
- `auto_sync_enabled: true`
- `last_error: null`
- `auth_required: false`

### 3. Check public dashboard path

```powershell
curl.exe -I https://project-mai-tai.live
```

Expected:

- `401` is normal because the dashboard is basic-auth protected
- `502` is **not** normal

## If `project-mai-tai.live` Is Down

Start the Mai Tai stack:

```powershell
ssh mai-tai-vps "sudo systemctl start project-mai-tai.target"
```

Verify services:

```powershell
ssh mai-tai-vps "sudo systemctl status project-mai-tai-control.service project-mai-tai-market-data.service project-mai-tai-strategy.service project-mai-tai-oms.service project-mai-tai-reconciler.service --no-pager -l"
```

## Session Refresh Flow

### Step 1. Make sure local Windows TradingView session is valid

Open your local Chrome TradingView profile and confirm you are signed in.

### Step 2. Start a Chrome instance with remote debugging on Windows

Example:

```powershell
$chrome = "C:\Program Files\Google\Chrome\Application\chrome.exe"
$userData = "C:\Users\kkvkr\OneDrive\Documents\GitHub\project-mai-tai\tmp_tv_alerts\user_data"
Start-Process -FilePath $chrome -ArgumentList @(
  "--remote-debugging-port=51299",
  "--user-data-dir=$userData",
  "--new-window",
  "https://www.tradingview.com/chart/"
)
```

Verify debug endpoint:

```powershell
curl.exe -s http://127.0.0.1:51299/json/version
```

### Step 3. Export the live TradingView session from Windows

```powershell
.\.venv\Scripts\python.exe scripts\tradingview_export_session.py `
  --cdp-url http://127.0.0.1:51299 `
  --output C:\Users\kkvkr\OneDrive\Documents\GitHub\project-mai-tai\tmp_tv_alerts\tv-session-export-live.json `
  --url https://www.tradingview.com/chart/
```

### Step 4. Copy the exported session file to the VPS

```powershell
scp C:\Users\kkvkr\OneDrive\Documents\GitHub\project-mai-tai\tmp_tv_alerts\tv-session-export-live.json `
  mai-tai-vps:/home/trader/project-mai-tai/tmp_tv_session_export_live.json
```

### Step 5. Probe the session on the VPS

```powershell
ssh mai-tai-vps "/home/trader/project-mai-tai/.venv/bin/python /home/trader/project-mai-tai/scripts/tradingview_probe_session.py --session-file /home/trader/project-mai-tai/tmp_tv_session_export_live.json --user-data-dir /home/trader/project-mai-tai/tmp_tv_session_probe/user_data_live --output /home/trader/project-mai-tai/tmp_tv_session_probe/probe_result_live.json --browser-channel chrome --url https://www.tradingview.com/chart/ --headless && cat /home/trader/project-mai-tai/tmp_tv_session_probe/probe_result_live.json"
```

Expected:

- `logged_in: true`
- `login_redirected: false`
- `rate_limited: false`

### Step 6. Restart the VPS TradingView alerts service

```powershell
ssh mai-tai-vps "sudo systemctl restart project-mai-tai-tv-alerts.service && sleep 10 && curl -s http://127.0.0.1:8110/health"
```

Expected:

- `provider: playwright`
- `auth_required: false`

## What The VPS Service Is Doing

The VPS TradingView alerts service:

- reads watchlist changes from `strategy-state`
- creates alerts when symbols are added
- removes alerts when symbols are removed
- keeps delete protection for active/pending bot state

Current VPS watchlist-managed alerts can be seen from:

```powershell
ssh mai-tai-vps "curl -s http://127.0.0.1:8110/health"
```

## Important Notes

- Do **not** try to log in directly on the VPS unless absolutely necessary
- The preferred path is:
  - local Windows signed-in session
  - export session
  - inject on VPS
- If TradingView expires the VPS session later, repeat the export/import flow
- If anything feels risky or confusing, you can come back and ask me to run the
  sequence again

## What I Can Do For You Later

If you come back later, I can:

- rerun the session export/import flow
- verify the VPS `tv-alerts` health
- check whether the dashboard stack is up
- inspect TradingView add/remove failures
- refresh the VPS session if TradingView expires it
