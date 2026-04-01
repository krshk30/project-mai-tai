# Bootstrap

First-time VPS setup scripts for Postgres, Redis, directories, and service prerequisites.

Recommended first-run order on the VPS:

1. `01_install_packages.sh`
2. `02_prepare_host.sh`
3. `03_create_dashboard_auth.sh <username> [password]`
4. `04_enable_http_site.sh`
5. `05_issue_certificate.sh <email>`
6. `06_enable_https_site.sh`
7. `07_bootstrap_database.sh <db_password>`
8. `08_install_runtime.sh`
9. `09_install_systemd_units.sh`
10. `10_enable_services.sh`

Notes:
- Run these only on the new `project-mai-tai` VPS target, not inside the
  legacy repo.
- DNS for `project-mai-tai.live` and `www.project-mai-tai.live` should already
  resolve to the VPS before `05_issue_certificate.sh`.
- Keep Cloudflare records as `DNS only` during initial certificate issuance.
- If `ufw` is active, `04_enable_http_site.sh` now opens `80/443` via the
  `Nginx Full` profile automatically.
- `02_prepare_host.sh` also writes a Redis drop-in under
  `/etc/redis/redis.conf.d/99-project-mai-tai.conf` so Redis stays a bounded
  cache/event bus instead of loading oversized persisted snapshots into memory.
- After `07_bootstrap_database.sh`, store the same database password in the
  root-owned env file under `/etc/project-mai-tai/`.
- Edit `/etc/project-mai-tai/project-mai-tai.env` before `08_install_runtime.sh`
  so the runtime installs and migrations use real credentials.
- For Alpaca paper mode, set `MAI_TAI_OMS_ADAPTER=alpaca_paper` and fill the
  three paper credential pairs for `30s`, `1m`, and shared `tos/runner`.
- `08_install_runtime.sh` preserves `MAI_TAI_DATABASE_URL` into the Alembic run
  so migrations work with the root-owned env file.
- `10_enable_services.sh` enables the concrete service units, then starts the
  `project-mai-tai.target` stack.
