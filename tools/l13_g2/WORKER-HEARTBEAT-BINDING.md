# G2 binding to G1 f76a3f23, source-only append / ALL UNRUN

Dependency: actual worker source f76a3f23d22490a0f19eb34504e73c46d0ac1409
(14:48:41 ET), integrated by MAIN with0577422c before any runtime claim.
This G2 worktree inherits bf54, NOT f76 production files. Do not execute its
old worker and expect the new heartbeat witness; compose the exact G1 successor.
No G1/production changes made by G2. Frozen b7a3 and824dd worktrees untouched.

SnapshotReceiptRedis and CaptureSnapshotRedis now accept ONLY the actual two
streams, validating distinct schemas/identity. Snapshot envelope source remains
market-data-gateway. Heartbeat envelope AND payload service must both be
market-data-snapshot-worker; role independent_snapshot_worker, actual child PID,
UUID instance/hostname, timezone-aware process start and healthy progress fields
are checked. Unknown stream/kind or identity mismatch raises, never ignored.
Actual production heartbeat serialization/XADD validation work remains enabled
in the child; no heartbeat task/cadence/logger/GC suppression or no-op patch.

P1 collects heartbeats in a separate worker_heartbeats bank, not fetch/publish
receipts or snapshot cycle counts. Retains all lifecycle receipts plus >=3
healthy receipts DURING the unchanged60s window at existing15s cadence;
starting/stopping and stable same-child instance are mandatory. Existing240/s,
14400events,13kCPUdecode,>=200/s60s,p99<300ms,stall<50ms gates unchanged.
Short real-worker installer control now requires heartbeat+fetch+publish and
post-cleanup stopping witness. Its normal cleanup/child PID assertions remain.
The existing valid utility metric fixture adds explicit heartbeat witnesses;
one additional negative parameter proves absent heartbeat cannot qualify.

P3 one-batch path calls actual publish_snapshot_batch_once(), not run(), so
the current production method normally emits ZERO heartbeat events. It does
not disable a heartbeat task; none is started by that one-method path. Capture
sink nevertheless validates/counts any actual heartbeat, keeping snapshot bytes
and exactly-once snapshot publication separate. Child returns those receipts;
retained-P3 output reports them per original source stream ID, including zero.
Do not claim this method-only P3 is independent worker-health/liveness proof;
P1 full run() + G1 heartbeat/lifecycle controls cover that separately.

Recorded P3 parser/metadata/scanner acceptance from824dd unchanged. Actual input
remains120triples729507460rawbytes; first~5min UNKNOWN plus genuine60-batch
scanner readiness, then actual emitted alerts+confirmed0. No unavailable prior
state, positive confirmed or positive10-minute alert required/invented.
No new runtime, tests, imports, replay, provider, DB or benchmark by author.

## Main source review and serial claim inventory

Existing contract module now40 intended functional controls (39 preserved plus
one absent-heartbeat negative); new heartbeat-sink module12; retained module18
functional plus one separate actual-P3 node. ALL UNRUN. P1 one60s node UNRUN;
old mandatory-prior-state P3 remains superseded for this approved delivered case.
No qualification from static checks or fixture-only success.

```sh
PYTHONPATH=src:. /Users/velkris/Projects/project-mai-tai/.venv/bin/python -m pytest -q tests/unit/test_l13_g2_control_contract.py tests/unit/test_l13_g2_worker_heartbeat_sink.py tests/unit/test_l13_p3_retained.py -k 'not actual_retained_packet' --junitxml=/ABS/FRESH-G2-controls.xml
```

P1 exact opt-in node/command remains in original README; retained-P3 exact node,
input manifest and command remain in RETAINED-P3.md. MAIN pins composed HEAD,
fresh raw/XML/JSON outputs, separate serial claim and no overlapping runtime.
