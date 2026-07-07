# ops/health — fleet health & readiness (version-controlled ops scripts)

These are the **independent** health/readiness scripts for the fleet. They are deliberately
NOT services: stdlib + `psql`/`redis-cli` subprocess only, no app import — so a frozen
service or a hung DB can't take a monitor down the same way. They run from cron and alert
via ntfy (topic `mai-tai-preopen-28806a5a97b7`).

**These files are the SOURCE OF TRUTH.** They historically lived un-versioned in
`/home/trader/`; committed here (PR-E follow-on / F3) so they are reviewable + CI-visible +
diffable. On deploy they are available in the tree at
`/home/trader/project-mai-tai/ops/health/`; crons point at these paths. Edit here, `git pull`,
never hand-edit the deployed copy.

## The three monitors (defense in depth)
1. **Pre-open readiness** (`preopen_readiness_check.py` + `preopen_readiness_cron.sh` +
   `preopen_alert.sh`) — daily ~09:12 ET all-fleet GO/AMBER/RED before the open.
2. **OMS-liveness watchdog** (`oms_liveness_check.py` + `oms_liveness_watch_cron.sh`) — every
   minute 07:00–18:15 ET; RED if the `oms-risk` heartbeat is >180s stale (process ALIVE check).
3. **Fleet FUNCTION-health** (`fleet_health_check.py` + `fleet_health_cron.sh`) — F3; validates
   FUNCTION, not process: "is it doing its job" against GROUND TRUTH (DB/fills/independent
   capture), never a component's self-report. The self-report is the thing that lies.

## fleet_health_check.py — the F3 framework
A check registry: each check verdicts GREEN/AMBER/RED against ground truth; `main()` prints one
`VERDICT:` line per check + an aggregate and exits worst (0/1/2) → the cron routes to ntfy.
**Design constraint:** a check is RED *only* when genuinely broken (never on normal quiet), or
it gets ignored. Checks are added incrementally.

- **#1 strategy-bar-freshness** (live) — polygon_30s must keep persisting 30s bars. RED only
  when the bars are stale AND the independent Polygon capture (`market_capture_trades`) is
  SIMULTANEOUSLY live → a frozen loop (the "reports healthy while dead" class). A quiet
  market / feed outage → GREEN (staleness not attributable to the strategy).
- **#2 oms-order-lifecycle** (planned) — intents-in-but-no-orders/fills-out for >N min (the
  07-01 zombie signature); keyed on relative progress, never absolute counts (no-quiet-alarm).
- **#3 stops-armed** (planned) — every OMS-OWNED open position (`oms_managed_positions` +
  F2's `oms_armed_stops`) has an armed stop. OMS-owned only (scoping invariant — a manual
  position must never trip "unprotected").

## Deploy (F3 = adding a cron, no service restart)
Crontab (trader), dual-UTC for DST (the ET guard inside runs the body only in-window):
```
*/5 13-21 * * 1-5 /home/trader/project-mai-tai/ops/health/fleet_health_cron.sh
```
`fleet_health_cron.sh --selftest` sends a RED push to verify phone delivery (no DB/Redis).
Rollback: remove the crontab line. No live-service impact.
