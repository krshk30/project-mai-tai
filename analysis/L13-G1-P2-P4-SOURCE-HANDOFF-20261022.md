# L13 G1 production split and P2/P4 source checkpoint: ALL UNRUN

First incremental commit bf54dd92e0649ef61e2cd403aec7174fef228509 was committed
2026-10-22T14:04:26-04:00, before14:59, direct from exact690. No amend. This
successor completes G1 source/controls for independent review; no worker runtime,
app import, tests, AST, compile, SQL, provider, SSH, install or restart performed.
Main full aa162668 runs separately and does NOT qualify this code.

## G2 API / caller and dependency map

SnapshotWorkerService(settings=None, redis_client=None, *, snapshot_provider=None,
reference_cache=None). Inject ALL fake dependencies to prevent any real client
construction in tests. Production constructs one MassiveSnapshotProvider and
ReferenceDataCache plus one MarketDataPublisher, never a gateway instance or a
MassiveTradeStream. Its exact old snapshot methods are reused unbound, with
their logger/reference_cache/publisher/settings/_timings dependencies initialized.
No in-process gateway subclass or fork of a live client/event loop is involved.

Worker calls run(stop_event=None) -> ensure reference data -> snapshot polling
and periodic reference refresh tasks. Explicit stop_event is for controlled
tests; production binds existing signal handling. Poll task unexpected death
is process-fatal; systemd independently restarts it. REST failures retain the
existing log/retry-after-interval behavior. Reference-refresh errors retain the
existing cache and log, as before. Shutdown cancels/joins tasks/closes Redis.
An in-flight blocking REST thread cannot be forcibly cancelled by asyncio; unit
TimeoutStopSec30 is the external process boundary, not an invented thread kill.

Gateway run no longer calls startup reference build or schedules snapshot/ref
loops. Its stream task/sole websocket/owner union, warning fields, controlhealth,
warmup and04roll logic remain unchanged. Gateway keeps its own REST provider for
historical warmup; worker owns all whole-market/ref builds. A narrow lazy-client
lock now ensures concurrent first access cannot construct multiple SDK clients;
existing retries/timeouts/requests/normalization and websocket code unchanged.

publish_snapshot_batch_once(snapshots)->int retains identical existing method,
as_payloads, MarketDataPublisher encoder, stream maxlen and source_service
market-data-gateway. Snapshot interval still sleep max(1, configured interval)
AFTER fetch/publish. Reference refresh interval unchanged. No added feature flag.

Health hash <prefix>:snapshot-worker-health carries only reference_tickers,
snapshots_without_reference,last_snapshot_symbols. Worker writes after ensure /
batch; gateway copies validated numeric counters into original heartbeat fields.
Reads/writes are bounded1s; failure retains last counters and does not make an
already-published batch disappear from the original log line. These counters
are NOT a freshness proof; control/scanner still read original batch timestamps.
Gateway remains alive on worker absence; snapshot staleness propagates unchanged.

## Unit / deployment lifecycle (main owns combined install runbook)

Final unit: project-mai-tai-snapshot-worker.service. Launch:
.venv/bin/python -m project_mai_tai.services.market_data_snapshot_worker
Restart=always, RestartSec5, StartLimitIntervalSec0, TimeoutStopSec30.
Unit After gateway startup, but NO Requires/BindsTo/PartOf gateway relationship.
Both are PartOf application target only. Target Wants worker ON at boot/install.
Gateway unit's initial bf54 Wants-worker edge is removed in this successor:
restarting gateway alone leaves an existing worker alive, without starting and
then immediately restarting it during first deployment. Target owns auto-start.

Installer/status/enable/restart-all unit inventories include worker. Narrow
deploy_service market-data branch installs worker unit/daemon-reloads, restarts
gateway then worker before optional strategy start; old live preflight/hold flag
and all other service branches remain. No production deployment executed.
Main must preserve existing momentum guard STOP-file handling and guard-based
consumer startup. Exact latch source:
ops/health/momwarn1_repair/optiona/G1/guard_rules.py::require_no_safety_stop;
/var/lib/project-mai-tai/option-a/safety-stop.json. Presence OR unreadable lookup
is STOP; no automatic removal/override. Guard/daily_guard source is unchanged.

## Source controls, ownership and cost

G1 only new named files:
- test_l13_snapshot_worker_parity.py:10 intended controls:9 full serialized-byte
  comparisons (0/2/13000 records x PM/RTH/AH clocks),1 health-failure publication.
  Both envelope UUID/produced_at AND payload completed_at are fixed identically;
  compare the complete data bytes+stream+maxlen, not normalized JSON subsets.
  Legacy path uses retained original690 method with no gateway/client constructor.
- test_l13_snapshot_worker_lifecycle.py:9 intended controls: actual spawn/SIGKILL
  and separate restart while real gateway stream loop publishes mock trades;
  gateway run forbids whole-market tasks; no websocket/gateway constructor;
  one SDK client under8 concurrent accesses; unexpected poll death; REST retry;
  old heartbeat fields/worker loss; retained owner/checkpoints on worker restarts;
  unit/install ordering. Together with P2:19 intended cases, all UNRUN.

P4's tiny test supervisor waits actual5s RestartSec then spawns a NEW mock-native
worker. It uses real separate Python processes and real worker.run, NOT actual
production systemd. Scanner staleness source is unchanged; G2 owns P3 real scanner
consumption/stale controls. No fake claim systemd kill or production lag proven.
P4 expected15-25s bounded spawn/5s wait/cleanup; P2 13000-record cases estimated
seconds, NOT measured. Main owns actual execution/cost/results, no forced PASS.

G2 owns its separate P1/P3 files and imports documented APIs through main. No G2
file touched. P1 >=200/s60s,p99lag<300ms/stall50ms; P2 byte equality; P3 scanner;
P4 lifecycle; P5 tonight ALLON/full on actual composed head; P6 live nextpre-market
30min all remain pending, no standalone-source result borrowed. L1 pulled;
conditional tonight composition L7/L8/L9+L13. Source readiness != install GO.

## Minimal main source-review selection / NOT EXECUTED

python -m pytest -q tests/unit/test_l13_snapshot_worker_parity.py \
  tests/unit/test_l13_snapshot_worker_lifecycle.py

Wider preservation should include whole test_market_data_gateway.py, publisher/
reference-cache/provider, deployment/systemd controls and G2 P1/P3 on composed
head. Existing tests/requirements/assertions are unchanged, no filters/waivers.
Realistic G1 source handoff: this commit; main source/functional review then
qualification timings UNKNOWN. No promise to install without P1-P5 green.
