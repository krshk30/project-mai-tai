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
11. `11_install_unattended_upgrade_policy.sh`

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
- `11_install_unattended_upgrade_policy.sh` prevents unattended replacement of
  Postgres/OpenSSL server and client-library packages, reports package changes
  and failures to the existing ntfy channel, and installs explicit DST-safe
  timers. Downloads run at 01:30 ET and installs at 02:30 ET. Both timers set
  `Persistent=false`, so a missed overnight run is not caught up during the
  04:00-20:00 ET operating window. The download-only timer is moved too: it
  does not install packages, but its default 12-hour random delay was observed
  scheduling network/disk work during the market session.
  `Unattended-Upgrade::Mail` is deliberately not set: the host has neither a
  mail transport nor a `mailx` provider. Instead, a service drop-in snapshots
  installed package versions before and after the vendor unattended-upgrade
  command. A changed package set or failed command is sent to the already
  monitored ntfy topic; a no-change run is recorded locally and stays quiet.
  Notification delivery uses bounded retries and fails the systemd unit if the
  page cannot be delivered, rather than recording a false success.
