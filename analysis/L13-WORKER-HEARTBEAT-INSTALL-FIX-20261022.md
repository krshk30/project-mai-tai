# L13 Worker Heartbeat Install Fix

Author checkpoint only. Parent 0577422cb4d1208e0846038768e8a7874e3dafba.
No tests, imports, AST/compile, providers, processes, install or restart executed.
PRICE1 a4d9e72866863edd9eff945fb09476de9cf26cbb is preserved in its separate worktree.

## Finding and Own Assessment

The deployed-unit inventory restarts project-mai-tai-snapshot-worker.service, but
heartbeat_service_for_unit lacked its mapping. Adding only that mapping would
still fail: the worker previously wrote only snapshot-worker-health counters,
not an event in heartbeats. Counter hashes cannot prove a fresh process.

The existing control-plane reader accepts arbitrary named HeartbeatEvent values,
selecting the latest per service; it preserves raw status, timestamp and details.
The existing post-restart gate requires a healthy heartbeat newer than the
systemd process-start time. Its PID/SHA binding remains external systemd/Git
evidence, as the gate explicitly states. These changes do not claim that the
heartbeat itself cryptographically attests the deployed Git SHA.

## Exact Impact

- SnapshotWorkerService.__init__: adds a SEPARATE MarketDataPublisher named
  market-data-snapshot-worker, PID, per-instance UUID, process construction UTC
  time and unique hostname/PID/UUID instance name. No gateway/WS/REST constructor
  added. The snapshot publisher remains market-data-gateway.
- _ensure_reference_data marks reference-ready only after the original method
  succeeds. publish_snapshot_batch_once marks progress only after the unchanged
  original batch publication returns successfully.
- New _publish_worker_heartbeat/_heartbeat_loop: standard starting/healthy/
  degraded/stopping events on the existing heartbeats stream at the configured
  heartbeat cadence, with five-second bounded writes. Healthy requires loaded
  reference data and a successful batch within snapshot interval + heartbeat
  interval (defaults 5 + 15 = 20 seconds), using monotonic age. This is worker
  liveness, NOT native/provider completeness or scanner freshness proof.
- run: emits starting, supervises heartbeat task death alongside polling task
  death, cancels/joins children and emits bounded stopping before Redis close.
  Failed healthy-heartbeat writes are fatal, so systemd's existing Restart=always,
  RestartSec=5 handles recovery. Stopping-write errors cannot mask the original
  failure. The gateway has no dependency on worker heartbeat success.
- ops/systemd/deploy_service.sh heartbeat_service_for_unit maps the worker unit
  to its exact own service name. No deployment command has been executed.
- P4 ChildRedis now recognizes the additional heartbeat stream. Its REST-failure
  control waits for actual snapshot entries rather than treating a heartbeat as
  a successful batch. Original success assertions are retained/strengthened.

No changes to gateway.py, publisher.py, snapshot algorithm or P2 byte-parity
module versus 057. No shared OMS/table/order/control flag writes. Shared Redis
additions are bounded normal heartbeats only; old counters/schema remain intact.

## Controls and Remaining Gates

New test_l13_worker_heartbeat.py has 16 intended controls, ALL UNRUN: own identity,
retained hashes not healthy, stale progress, failed batch, fatal heartbeat write,
returned/cancelled/failed heartbeat task, new boot identity, stopping/close,
no gateway constructor, exact installer mapping, and four existing health-gate
freshness/status cases. Existing G1 P2/P4 whole modules remain required.

Main-only selection: tests/unit/test_l13_snapshot_worker_parity.py,
tests/unit/test_l13_snapshot_worker_lifecycle.py,
tests/unit/test_l13_worker_heartbeat.py, plus G2's current controls on composition.
Expected G1 intended count: original 19 + new 16 = 35; collection unverified.
No result is borrowed from an older head. G2 real-input P3 and P1 remain separate.
Actual systemd automatic restart/PID/health identity receipt remains owed; the
existing P4 manual test supervisor is NOT that receipt. Health stream read
visibility/Redis latency and boot reference duration must be checked by main's
actual gates. Runtime costs are UNMEASURED; no performance qualification claimed.

## Review / Integration

Main owns composition and the combined install runbook. Pick this descendant
after 057; do not edit a running/frozen composed worktree. Unit, entrypoint,
restart order and policies are unchanged. Own health name is now
market-data-snapshot-worker; stream topic heartbeats; heartbeat details contain
runtime_pid/runtime_instance_id/process_started_at and snapshot progress.
