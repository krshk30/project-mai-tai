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

Expected runtime layout:
- repo: `/home/trader/project-mai-tai`
- env file: `/etc/project-mai-tai/project-mai-tai.env`
- venv: `/home/trader/project-mai-tai/.venv`
- logs: `/var/log/project-mai-tai/*.log`
