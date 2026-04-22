# Systemd

Unit files and deployment notes for running `project-mai-tai` beside the legacy platform.

Nginx remains a separate edge service and proxies `project-mai-tai.live` to the
control plane on `127.0.0.1:8100`.

Included assets:
- `project-mai-tai.target` to start the whole stack
- one service unit per runtime component
- `install_units.sh` to copy units into `/etc/systemd/system/`
- `status.sh` to inspect all services
- `restart_all.sh` to restart the application stack without touching legacy
- `deploy_main.sh` to fast-forward the VPS checkout to `main`, reinstall, restart, and verify health
- `deploy_service.sh` to fast-forward the VPS checkout to `main`, refresh the runtime, and restart only one service or coordinated service pair
- `restart_control_live.sh` for a live-session control-plane restart
- `restart_reconciler_live.sh` for a live-session reconciler restart
- `restart_strategy_live.sh` for a live-session strategy restart with preflight prompts
- `restart_oms_live.sh` for coordinated OMS maintenance during a live session
- `restart_market_data_live.sh` for coordinated market-data maintenance during a live session

Operator note:
- `restart_all.sh` is intended for off-hours or flat-account use
- `deploy_main.sh` is also intended for off-hours by default and blocks ET market-hour deploys unless explicitly overridden
- `deploy_service.sh` is the manual path for service-scoped deploys and uses lower-risk choreography for `control`, `reconciler`, `tv-alerts`, `strategy`, `oms`, and `market-data`
- during an active trading session, use `docs/live-market-restart-runbook.md` instead of a full-stack restart
- the live-session scripts in this directory follow that runbook and stop for operator confirmation where automation would be unsafe
- invoke the helper scripts with `bash ops/systemd/<script>.sh` if the executable bit is not present on your checkout

Expected runtime layout:
- repo: `/home/trader/project-mai-tai`
- env file: `/etc/project-mai-tai/project-mai-tai.env`
- venv: `/home/trader/project-mai-tai/.venv`
- logs: `/var/log/project-mai-tai/*.log`
