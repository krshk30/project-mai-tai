# L13 GWSTALL1 G2 source checkpoint, 2026-10-22

Owner: G2 Maxwell. Base690f7d26147c18d718eba2815da058361727ccb3.
Only tools/tests changed. All new controls UNRUN. L1 is PULLED tonight;
old raw timing failures and UNKNOWN attribution are not superseded.

## G1 binding needed through main

`L13_G2_ADAPTER=tools.l13_g2_controls:ProductionWorkerAdapter` imports the
G2 adapter bound to G1 bf54dd92e0649ef61e2cd403aec7174fef228509's actual API.
No missing-binding fallback or pretend native worker exists in these tools.
The adapter instance must implement:

* `open_worker(settings, snapshot_provider, reference_cache, redis_client)`:
  asynchronous context manager, using the actual G1 production snapshot worker
  and lifecycle, accepting the supplied picklable doubles in its child process.
  Enter returns after startup; exit MUST stop and join the owned child, boundedly,
  surface child errors, and leave no owned process/thread running. The doubles
  never create REST clients, websocket connections, Redis servers, or a database.
* `publish_recorded_batch(snapshots, reference_data)`: awaitable returning the
  exact encoded snapshot-batch envelope published by the actual new worker for
  that input, not a local duplicate serializer. Same injection isolation applies.

The adapter spawns the actual SnapshotWorkerService, passes an injected event
to run(), and never duplicates the polling algorithm. The P3 one-batch child
calls its actual publish_snapshot_batch_once(). Spawn startup and bounded
cleanup controls are authored, UNRUN. Child configures the actual production
entrypoint logger; parent capture/handlers/levels and GC are not reduced.
G1's frozen increment was cherry-picked as6d29278ac2065d313a98231b87efef2cd99803f4;
no G1 production file was manually edited. Main must review the binding before
execution. Any later G1 API change needs explicit static revalidation.
P1 source binding prepared, NOT runtime-qualified; P3 additionally INPUT BLOCKED.

## P1 measurement and scope

240 independent producer deadlines per second, 60 seconds, 14,400 unique SIP
timestamps. Timestamp = wall anchor plus the independent monotonic deadline,
NOT the delayed actor arrival time. Feed frames traverse the real Massive
`_process_messages` / `_handle_messages`, gateway enqueue/publication loop,
real publisher/event model, and controlled Redis XADD sink. Sink reads actual
envelope produced_at minus SIP. This is a controlled offline path, not a socket,
Redis-server, provider latency, MOM/ATR/ORB protection, or installation claim.

Snapshot child receives 13,000 records per fetch. It repeatedly JSON-decodes
the controlled SDK-shaped document and invokes production normalization until
at least350ms thread CPU has elapsed. No sleep simulates decode. Production
worker must perform actual reference enrichment and snapshot publication.
Bounded128-entry child receipts record fetch CPU, PID/thread, intervals and
publication size/hash. Parent drains at20ms cadence, without whole-market
decoding on the trade loop. Only complete intervals inside the measured window
qualify; >=8 fetches and publishes, every fetch overlapping actual trades.
Same-process execution, missing CPU, empty/missing snapshots, missing receipts,
loss, duplicate identities, pending queues, task errors, or leaked producer fail.
Thread creation, decode, actual logging/capture and GC remain unmodified.

Strict gates: >=60s, >=200 received trades/s, exactly14,400 sent/received,
produced_at-minus-SIP nearest-rank p99<300ms, loop stall<50ms. The1ms timer uses
monotonic elapsed minus its requested sleep. Nothing filters large samples.
Both all-trade p99 AND snapshot-cycle p99 must satisfy300ms. Cycle intervals
span actual fetch start through actual publication; include a trade if either
its scheduled SIP deadline or actual publication overlaps, so delayed frames
cannot disappear from the cycle denominator. >=8 complete cycles required.
All lag/stall samples, SIP/publication timestamps and production module hashes
are retained. Complete raw metrics export after worker and producer cleanup, also on failed
assertion. Existing files cannot be overwritten. No qualification from unit tests.
P4/G1 must independently prove the real gateway startup does not also run the
old snapshot loop and that supervisor crash/restart behavior is correct.

## P3 recorded input gap and actual consumer

Required immutable packet manifest:

```json
{"kind":"recorded_snapshot_batches","file":"snapshot-batches.jsonl",
 "sha256":"FULL_SHA256","provenance":"capture command, host, source head, stream, time range",
 "prior_consumer_state":{"file":"prior.json","sha256":"FULL_SHA256"},
 "expected_consumer_results":{"file":"expected.json","sha256":"FULL_SHA256"}}
```

Each JSONL row must be an actual full SnapshotBatchEvent with original
produced_at, snapshots, reference_data and completed_at. Need increasing
timestamps and sufficient actual inputs/prior warm-up to witness a confirmed
name plus SQUEEZE_5MIN and SQUEEZE_10MIN; no synthetic favorable references or
backdated historical rows. Packet limit128MiB/300batches. No such packet has
been verified in the delivered L11/L12 case/log/bar exports. Exact minimal
request: retained `mai_tai:snapshot-batches` sequence with its references and
original timestamps covering a known confirmed+5/10-minute alert interval;
capture command, source head, coverage and SHA256. If prior history is necessary,
include the original warm-up history/preceding batches, not invented seed state.
Prior-state file (<=16MiB) must have kind=recorded_scanner_prior_state,
provenance/asof_utc no later than first batch; actual alert_engine export_state,
confirmed_tracking, confirmed_candidates, reference_data, recent_alerts,
today_alerts, first_seen_by_ticker, global_manual_stop_symbols,
seeded_confirmed_pending_revalidation, pending_recent_alert_replay. These are
mandatory operands, never defaulted empty. Real alert restore/confirmed seed
methods are used; pending confirmation tracking is copied from the actual
capture, not reconstructed as favorable. Prior-session identity is restored
so the real scanner roll still executes if the batch crosses04:00.
Expected file (<=16MiB) must have kind=recorded_scanner_expected_results,
provenance and batches list, each original source_event_id + confirmed,
top_confirmed, alerts(today tape), emitted_alerts(this batch). Missing or wrong
coverage/hash/identity/asof is a material blocker. Both old/new must match the
recorded expected outputs, not merely each other. Real alert method is wrapped
only to capture its exactly-once result. Positive5/10-minute witnesses must be
emitted during replay, not borrowed from prior tape; final warm-up fully_ready.
Capture-time scanner configuration/catalyst context must be reviewed against
this controlled offline context before a P3 claim; news-dependent paths are NOT
proved by an empty catalyst default. Any missing state/context remains UNKNOWN.

Legacy publisher is independently pinned by SHA256 to690. Candidate envelope
must come from actual G1 worker. Only consumer replay time is set to each actual
capture produced_at; new worker envelope creation time is not historical proof.
Both payload snapshot/reference lists must match before real service dispatch.
The real StrategyEngineService `_handle_stream_message` validates/converts,
rolls the scanner session and calls the actual StrategyEngineState scanners.
Alert/confirmation logic is NOT stubbed. Only external DB/manual-stop/blacklist
reads, subscription publication and dashboard persistence are controlled empty
I/O, identically on both sides. This is batching/scanner equivalence under that
explicit controlled context, not historical broker/admission replay. Strategy
bots/news/persistence disabled to prevent providers/DB; scanner engines enabled.
Historical scanner time is bound to each original capture in Eastern time,
including legacy helpers without an explicit now parameter. These test-only
bindings are restored afterward; P1 clocks/GC are never patched. Service logging
setup is delegated to the existing logger instead of resetting parent capture;
no handler/level/filter is removed. Settings construction ignores host env/files
so operator secrets and switches cannot leak into the controlled worker.
Per-batch confirmed/top-confirmed, alerts/recent alerts, warm-up and subscription
targets compared exactly; positive confirmed and5/10-minute witnesses required.

## Proposed main commands, only after binding/input review and serial claim

```sh
PYTHONPATH=src:. /Users/velkris/Projects/project-mai-tai/.venv/bin/python -m pytest -q tests/unit/test_l13_g2_control_contract.py
L13_G2_CLAIM=P1 L13_G2_ADAPTER=tools.l13_g2_controls:ProductionWorkerAdapter L13_G2_RESULT=/ABS/FRESH-P1.json PYTHONPATH=src:. /Users/velkris/Projects/project-mai-tai/.venv/bin/python -m pytest -q -rP tests/unit/test_l13_g2_qualification.py::test_p1_real_worker_cpu_decode_and_trade_publication_60_seconds --junitxml=/ABS/FRESH-P1.xml
L13_G2_CLAIM=P3 L13_G2_ADAPTER=tools.l13_g2_controls:ProductionWorkerAdapter L13_G2_RECORDED_MANIFEST=/ABS/manifest.json L13_G2_RESULT=/ABS/FRESH-P3.json PYTHONPATH=src:. /Users/velkris/Projects/project-mai-tai/.venv/bin/python -m pytest -q -rP tests/unit/test_l13_g2_qualification.py::test_p3_recorded_batches_real_strategy_consumer_equivalence --junitxml=/ABS/FRESH-P3.xml
```

Runtime commands contain unresolved fresh-output paths and P3 packet, not READY
claims. Main owns raw log capture, exact composed head, environment isolation,
fresh outputs, serial inventory/claim and execution. No rerun or waiver implied.
No production change, tests, imports, compilation, AST, provider or app execution
was performed by G2. P1/P3 UNRUN; P2/P4 G1; P5 main; P6 live operator observations.
