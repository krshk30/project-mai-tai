# L13 G1 first production increment: SOURCE ONLY / ALL UNRUN

Base exact690f7d26147c18d718eba2815da058361727ccb3. Separate G1 branch/worktree.
G2 owns P1/P3 files; G1 owns production split and named P2/P4 controls. No runtime
or provider/DB/SSH/install/restart performed. This first increment is NOT qualified.

## Assessment and API for G2 through main

Production gateway run no longer schedules snapshot polling/reference rebuilding
or waits for whole-market reference construction at startup. Separate systemd
snapshot-worker owns that work and Redis snapshot publish, with its own GIL/loop.
No second websocket: worker constructs only ONE MassiveSnapshotProvider REST
client, reference cache and publisher. Gateway keeps its ONE REST provider for
unchanged historical warmup. Subscription owner union/04roll/ticks unchanged.

SnapshotWorkerService(settings=None, redis_client=None, *, snapshot_provider=None,
reference_cache=None) accepts injected fakes, never construct real clients in
tests. await publish_snapshot_batch_once(snapshots) -> int uses the exact old
gateway method and unchanged publisher serialization/source_service/stream.
await run(stop_event=None): injected asyncio.Event avoids process signal binding
in controls; production binds normal signals. Same interval-after-fetch cadence,
same reference refresh loop and same published snapshot batch log retained.

G2 import paths:
- project_mai_tai.market_data.snapshot_worker.SnapshotWorkerService
- project_mai_tai.market_data.gateway.MarketDataGatewayService (stream side)
- project_mai_tai.market_data.publisher.MarketDataPublisher (unchanged encoder)
No fork/shared interpreter assumption: production uses an independent python -m
process from systemd. For P1 inject a separate-process fake provider/Redis sink;
do not put CPU decode into the gateway parent with asyncio.to_thread.

Health counter hash <prefix>:snapshot-worker-health has three original heartbeat
fields; gateway copies bounded numeric counters, never whole-market payloads.
Absent worker counters do not stop trades; scanner still uses batch age as before.
Unit Restart=always/RestartSec=5, no Requires/BindsTo coupling with gateway.
Gateway restart leaves an existing worker alive; worker restart never restarts
gateway. Unit installation integration, safety runbook and G1 P2/P4 controls are
the next increment; no claim systemd source alone proves P4.

Reuse of class METHODS does not instantiate MarketDataGatewayService in worker.
Existing snapshot methods remain callable for byte-parity/legacy tests but are
not scheduled by production gateway. No changes to consumer/scanner/MOM guards.

Pending P1/P2/P3/P4/P5 actual qualification, independent source review and P6
post-install measurements. Tonight composition is L7/L8/L9 plus conditional L13;
L1 explicitly pulled. Main sole executor/queue/LATEST writer.
