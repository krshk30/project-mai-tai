# VPS Deployment

## Current VPS Baseline

Verified on the live VPS:
- Ubuntu
- 2 vCPU
- 4 GB RAM
- 120 GB disk
- no swap
- `systemd` available
- no Docker
- no Postgres
- no Redis

## Deployment Target

Use native packages and `systemd`.

Reasons:
- lower operational overhead on a small VPS
- simpler process supervision
- fewer moving parts during migration
- better fit than introducing Docker solely for this project

## Parallel Run Layout

Legacy app remains untouched.

New app layout:
- repo path: `/home/trader/project-mai-tai`
- env path: `/etc/project-mai-tai/`
- log path: `/var/log/project-mai-tai/`
- state path: `/var/lib/project-mai-tai/`
- control-plane bind: `127.0.0.1:8100`

## Planned Services

Systemd units:
- `project-mai-tai-control.service`
- `project-mai-tai-market-data.service`
- `project-mai-tai-strategy.service`
- `project-mai-tai-oms.service`
- `project-mai-tai-reconciler.service`

Local infrastructure:
- `postgresql`
- `redis-server`

## Edge Access

Public edge domain:
- `https://project-mai-tai.live`
- `https://www.project-mai-tai.live` redirects to the apex domain

Planned approach:
- Nginx listens on `80/443`
- basic auth enabled for the new dashboard
- HTTPS via Certbot for `project-mai-tai.live` and `www.project-mai-tai.live`
- control plane remains private behind localhost proxying
- bootstrap starts with an HTTP-only Nginx site, then switches to the final
  HTTPS config after certificate issuance

DNS records:
- `A` record for `project-mai-tai.live` -> `104.236.43.107`
- `CNAME` record for `www.project-mai-tai.live` -> `project-mai-tai.live`

Certificate and proxy plan:
- FastAPI control plane listens on `127.0.0.1:8100`
- Nginx terminates TLS and proxies to `127.0.0.1:8100`
- `auth_basic` protects the operator surface
- broker, strategy, Redis, and Postgres remain private

## Coexistence Rules

- no reuse of the legacy app port
- no reuse of legacy JSON/CSV files
- no reuse of legacy service names
- no direct dependency on legacy process lifetime
- shared Massive/Polygon keys are allowed

## Secrets Handling

Initial approach:
- root-owned env files
- `chmod 600`
- loaded by `systemd EnvironmentFile`

This is the initial production posture for a single VPS.

## Bootstrap Order

Repository scripts live under `ops/bootstrap/`.

Recommended sequence:
1. install system packages
2. prepare directories and permissions
3. create the Nginx basic-auth file
4. enable the HTTP bootstrap site
5. issue the TLS certificate with Certbot using an operator email address
6. switch to the HTTPS Nginx config
7. initialize Postgres for `project-mai-tai` with the real application password
