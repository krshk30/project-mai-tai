# L13 P3: Claude15:31:49 revised source gate / ALL NEW CONTROLS UNRUN

Owned append from frozen9bbec5bb; no production files changed. Main must compose
with reviewed G1 f76a3f23 (heartbeat/API/deployment), not qualify this historical
bf54-derived checkout as the final production composition. L2 gets first main
runtime capacity. No author tests/imports/AST/compile/replay/DB/provider actions.

## Explicit acceptance replacement, not hidden failure removal

Preserve L13-1DA5BB47-P3-RETAINED-20261022 raw/XML/JSON untouched: actual203
VOLUME_SPIKEs versus recorded0 at batch60, after59 warmup-UNKNOWN batches. New
disposition explicitly WITHDRAWS cold-vs-live equality. Price-history readiness
cannot reconstruct last_spike_volume, volume_spike_tickers or cooldowns. Source
assessment found49/203 with prior logged volume updates and154 unbound within
the retained log; those49 timestamps do not establish continuous asof state.
No prior state is seeded, no203 alerts are filtered, no historical live parity
is claimed at any point. Existing original b7 P3 source is not modified.

OLD: pinned690 publisher _encode_snapshot_batch, same unchanged source SHA
7deeccbce10266043dd4ad118b706d57ee1558ccb3f9eae2f616e51056e59517.
NEW: actual independent spawned SnapshotWorkerService.publish_snapshot_batch_once
through the existing controlled ProductionWorkerAdapter. Both consume the same
actual SnapshotRecords/references reconstructed from each retained envelope.
Both separately constructed real StrategyEngineService consumers begin cold and
persist their own state through ALL120 batches. Actual _handle_stream_message /
MomentumAlertEngine / confirmed scanner run on both sides, no alert-engine fake.

Literal JSON payload bytes are extracted from each actual encoded publisher
envelope BEFORE Pydantic validation or any re-serialization. No sorting/numeric
canonicalization/timestamp exclusion. Only top-level envelope metadata is outside
the payload-byte contract. The completion timestamp normally generated during
publication is bound identically in both model CONSTRUCTORS to this recorded
batch's actual completed_at. The wrapper delegates all other arguments to the
unchanged real SnapshotBatchPayload. It does NOT rewrite serialized output or
patch the encoder/model serialization. This controlled replay clock is NOT a
claim of historical worker publication timing or new live P1 performance proof.
The opt-in constructor binding restores on exceptions and is None/no-op for
existing default callers including the unchanged P1 run path. Child construction
binding is process-local; parent OLD binding ends before the child is spawned.
Both real publisher payloads must also match original snapshots/reference lists
and completed_at, so equal new/old corruption cannot silently replace the inputs.

Exact parity includes emitted alerts (all types/VOLUME_SPIKE), today/recent alert
lists, all/top confirmed, retained watchlist, warmup, subscription target calls.
Deep-copy per-batch observations prevent later mutable-state aliasing from making
an earlier comparison change. Both sides use the same inferred recorded decision
clock, never a favorable native state. Log association/provenance remains strictly
validated; recorded alerts are diagnostics only, not state or expected outputs.
Raw observed records are retained in the result; no203-to0 adjustment. Runtime
log output/capture and production scanner code remain unchanged.

## Coverage and bounds

Same pinned729507460-byte raw stream /120 IDs, gzip132749809 bytes; original
manifest unchanged. One8MiB frame plus bounded lookahead and ordinary scanner
state, no full120-batch decoded list. Wire-span extraction decodes only the
current JSON frame. Final SUCCESS requires generator EOF validating raw count,
bytes, SHA, first/last IDs AND120 unique comparison rows with both scanner and
byte proof. A matching first60 or119 is insufficient. Failure journal exports in
finally after owned worker cleanup and patch restoration; status stays
FAILED_OR_INCOMPLETE. No result overwrites are permitted by supplied commands.

Input pins: compressed fa1b53391efab924cbcb43b2c0f777620e2c7ba0f3c2c2e4b6441d7edc242fe3;
raw2c51513a80015ea09ac042eb078a704b950aed863481a28e093a1ed8e541bae4;
strategy log655806e815f23e158949d5063c0df171978e36368d5493c661780db107825c47.
Directory /Users/velkris/.codex/task-queues/analysis/L13-P3-SNAPSHOT-INPUTS-20261022.
Actual lastID1792693885152-0 (not reviewer prose's later missing cycle).

## Main-only review and serial claims

35 NEW intended functional cases in test_l13_p3_cold_parity.py; all UNRUN.
Includes malformed/duplicate envelopes; semantically equal but BYTE-different
JSON;8 output-field drifts;203-spike nonfiltering; insufficient/duplicate/incomplete
coverage; failure journal; constructor restore/default behavior; actual controlled
spawned worker/old encoder same-completion byte equality. That small spawned
worker control is not the120-batch retained proof or a native performance test.
Existing retained parser/real-consumer18 controls unchanged; intended command
union53 functional cases, excluding only the explicitly separate retained replay
node. Source counts, not collected results. Original P1 strict gates unchanged;
consumed P1 receipt is not repeated. Main may add existing adapter preservation
modules as a separate reviewed inventory; no author runtime authorized.

From MAIN's separately reviewed composition containing G1+this exact G2 delta:

    sh tools/l13_g2/cold-parity-controls-command.sh

After source/control acceptance, exact authorized main-only retained claim:

    sh tools/l13_g2/cold-parity-replay-command.sh

Legacy node suffix _postwarmup is kept for command compatibility; its current
body calls revised qualification and compares ALL120, never skips cold batches.
Scripts pin fresh distinct analysis outputs and explicit interpreter/environment.
Main alone inventories/claims/runs after L2; not automatic retries or worker
execution. No PASS/release/install from source review.

Call/cost impact: test adapter only gains optional completed_at argument passed
into its existing owned spawned child. Retained driver adds one old real encode,
literal wire-span comparisons and full scanner state observation per batch. All
cost is opt-in offline P3/control work, not tick handlers or running production.
P1 worker.run path/default constructor receives no new clock binding. No order,
ownership, protection, provider, schema or service deployment code is changed.

SAIQ resume point is already committed985389bb0b438f4919ddc70568ae01d52985b29c
(15:39:57ET): exact delivered three incident bindings/38 intended controls UNRUN.
It is a separate lane bundle, not mixed into this P3 source delta. No L9 repair.
