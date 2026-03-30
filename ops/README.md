# Ops Layout

`ops/` contains deployment and host-operation assets. It does not contain application logic.

Subdirectories:

- `bootstrap/`
  - first-time VPS provisioning scripts
- `env/`
  - env-file template(s) for the runtime
- `nginx/`
  - reverse-proxy configs for the public edge
- `systemd/`
  - service units, status helpers, and restart helpers

Use this directory when you need to answer:

- how to provision a fresh VPS
- where runtime env vars should live
- how traffic reaches the control plane
- how services should be started, stopped, restarted, and inspected
- how GitHub Actions hands off a validated `main` deploy to the VPS

Start here:

- [bootstrap/README.md](./bootstrap/README.md)
- [nginx/README.md](./nginx/README.md)
- [systemd/README.md](./systemd/README.md)

Operational rule of thumb:

- `bootstrap/` is for first-run host setup
- `systemd/` is for day-2 runtime operations
- live-market restart behavior is documented in `../docs/live-market-restart-runbook.md`
- GitHub Actions deploy flow is documented in `../docs/github-actions-deploy.md`
