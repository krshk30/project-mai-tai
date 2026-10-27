# L13 G2 retained-P3 successor, SOURCE ONLY / ALL NEW CONTROLS UNRUN

SUPERSEDED ACCEPTANCE NOTE: Claude15:31:49 explicitly withdraws the native-live
output comparison described below. Preserve this original protocol/failure for
audit; CURRENT gate and exact commands are in COLD-PARITY-153149.md. Current gate
compares both real publishers and both cold real scanners on ALL120 batches to
EOF, including literal payload bytes. It never treats warmup as restored native
alert state, seeds from logs, or filters the203 observed cold VOLUME_SPIKEs.

Explicit Claude14:32:24/operator ruling supersedes b7a3's proposed mandatory
historical prior-state, positive confirmed name and positive10-minute alert.
Historical prior state WAS NEVER STORED. Do not request it again or synthesize
it. This is a separate branch/worktree appended to frozen b7a3feb7; its original
four files/39functional/P1/P3 controls are unchanged. Original P3 is superseded
for THIS delivered case only; no P1/original performance assertion is weakened.

## Actual inputs, not claims from a convenience packet

Directory: /Users/velkris/.codex/task-queues/analysis/L13-P3-SNAPSHOT-INPUTS-20261022.
Main verified both SHA256SUMS14:34:54. Author bounded FILE-ONLY gzip/JSON framing
inspection (no application imports/replay) verified120triples,729507460 rawbytes,
SHA2c51513a80015ea09ac042eb078a704b950aed863481a28e093a1ed8e541bae4.
Each batch:13268snapshots,3051reference records. Largest JSON line6080486bytes.
Compressed132749809bytes SHAfa1b53391efab924cbcb43b2c0f777620e2c7ba0f3c2c2e4b6441d7edc242fe3.
Log66320bytes SHA655806e815f23e158949d5063c0df171978e36368d5493c661780db107825c47.
Exact machine-readable pins: retained-p3-input.json next to this file.

Actual first ID1792693011434-0 /produced2026-10-22T18:16:51.334452Z;
actual LAST1792693885152-0 /18:31:24.987769Z. Reviewer prose said
1792693892468-0/14:31:32; delivered rawsha matches but file ends one cycle
earlier. Reported immediately; bind file bounds, never invent the missing tail.
The log is UTC (18:25Z=14:25ET), not an ET timestamp to reinterpret again.
Author file-only summary inspection found343scanner summaries total and120
inside the actual packet interval through18:31:28.094Z. No strategy ran.

## Streaming and application path

Gzip is consumed once as raw Redis triples: numeric ID /exact data /JSON.
Each JSON line bounded8MiB, whole raw bound760MiB AND exact count/bytes/SHA/IDs
checked at EOF. Compressed/log file sizes and SHA checked before replay.
No read_bytes/read-all of729MB, no list120Pydantic frames, no simultaneous
decoding of120batches. One raw frame, its models/records and tiny next-ID
lookahead; application scanner state remains its ordinary bounded history.
Old/new real consumer states are separate and persist throughout the stream.

Old side consumes ORIGINAL captured envelope. Candidate is published by actual
G1 SnapshotWorkerService.publish_snapshot_batch_once in a spawned process via
the existing G2 adapter; exact snapshots/reference lists must equal originals.
Actual service _handle_stream_message -> actual StrategyEngineState -> real
MomentumAlertEngine/MomentumConfirmedScanner on both sides, no scanner stubs.
The old/new entire observed scanner summaries must match on every batch,
including the cold period. No broker/order/entry/protective-wire proof claimed.
Same controlled external I/O scope as b7a3: DB reads/persistence/subscription
publication controlled; bots/news disabled. No actual manual/ownership state is
inferred flat. This is scanner batch equivalence, not native entry admission.

The worker generates a NEW current envelope. Only the consumer replay clock/
produced_at is bound to the original capture; never claim native historical
publication or backdate worker readiness. Original source event/Redis identity
is preserved in output provenance. Logged decision clock is reconstructed from
first alert timestamp or summary timestamp for an empty group. Association is
explicitly INFERRED: exactly one log decision within each Redis publication
interval (next numeric Redis ID is lookahead). No native event-ID association
was logged; ambiguity/missing association FAILS, never nearest-time guessing.

## Unknown period and amended acceptance

Cold constructors contain NO claimed historical state. Only actual delivered
batches populate history; preceding13:50..14:16 log lines are EXPECTATIONS, not
price/history/cooldown seeds. Initial state and early native equivalence remain
UNKNOWN. Warmup needs both >=300actual seconds since first captured batch AND
the unchanged real scanner's squeeze_5min_ready. At configured5s, this requires
60actual batches; these arrived roughly7s apart, so actual readiness is later
than five wall minutes. Do not change the scanner interval or insert samples.
Ten-minute readiness is reported separately, UNKNOWN until120actual batches;
no unavailable positive10-minute witness is required or claimed.

After warmup, compare ALL actual emitted alerts against recorded log type,
ticker, price and every logged details field; unlogged bid/ask/etc are compared
old/new but not claimed independently recorded. Exact alert-count/log grouping
and confirmed=0 must hold. Native log confirms0 across window; replay confirmed0
is asserted after the UNKNOWN warmup. Required REAL witnesses: ATGL9.45
SQUEEZE_5MIN14:25:02, ATGL9.10 andHOST2.57 at14:30:13. No positive confirmed
event exists, so none is manufactured. Prior cooldown/volume-spike state may
still cause a mismatch: that is an honest failed/unknown recorded comparison,
not license to seed from expected outputs, filter extra alerts or claim PASS.
Native input configuration/catalyst-context limitations from b7a3 remain visible;
actual run and independent review determine whether the delivered case matches.

Metrics export after cleanup even on assertion failure; status remains
FAILED_OR_INCOMPLETE until every scoped assertion and EOF validation succeeds.
Scoped success does not prove prewarmup native behavior or all10-minute history.

## Proposed exact MAIN-only claims after source review and serial inventory

From the composed worktree containing this successor, interpreter is existing
/Users/velkris/Projects/project-mai-tai/.venv/bin/python. Fresh outputs required.

```sh
PYTHONPATH=src:. /Users/velkris/Projects/project-mai-tai/.venv/bin/python -m pytest -q tests/unit/test_l13_p3_retained.py -k 'not actual_retained_packet' --junitxml=/ABS/FRESH-retained-controls.xml
L13_G2_CLAIM=P3_RETAINED L13_G2_RETAINED_MANIFEST=tools/l13_g2/retained-p3-input.json L13_G2_RESULT=/ABS/FRESH-retained-P3.json PYTHONPATH=src:. /Users/velkris/Projects/project-mai-tai/.venv/bin/python -m pytest -q -rP tests/unit/test_l13_p3_retained.py::test_actual_retained_packet_real_worker_and_scanner_postwarmup --junitxml=/ABS/FRESH-retained-P3.xml
```

39existing functional/P1 remain unchanged/UNRUN; new parser controls and retained
P3 UNRUN. No tests, application imports, AST, compile, app/replay, provider/DB,
benchmark or network operation executed by author. Only expressly authorized
bounded stdlib FILE inspection was performed, separate from any runtime gate.
G1 final0577422c API was statically compared: unchanged injection/signatures/
health keys; bounded health I/O and lazy REST lock do not change this binding.
No deployment authority, no P1/P3 PASS, no duplicate main runtime.
