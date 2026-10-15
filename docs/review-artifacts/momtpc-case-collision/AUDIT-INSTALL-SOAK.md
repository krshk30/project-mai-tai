# T16:0x Build-Only Followup

Current base `ec4dc414`; accepted guards transplanted cleanly as `a0961267` and
`4abf9d00`. Original1781 packet untouched; no L gateway/publisher/source changes.

## Audited Local Plan / COPY Tool

Module `project_mai_tai.momentum_paper.case_collision_tool` has file-only inputs
`--capture`, `--manifest`, `--reference`, `--output`. Default is a dry-run plan;
optional `--rehearse-copy` appends only in a newly created in-memory SQLite COPY.
No production URL, provider, live execute, DELETE, UPDATE or schema-change interface.
Exactly two supplied, distinct Oct13 TPC FILLED UUIDs/logical trades must bind to
event keys, strategies and payload hashes. Reviewer/review ID and the fixed reason
`case_collision_TpC` are required. No discovery by ticker and no made-up target IDs.

Plan produces exactly two INVALIDATED audit rows plus one SESSION_REFERENCE_CORRECTED
audit. Original filled/final/session rows are immutable; audit keys are idempotent;
changed preimages or record hashes refuse append. Invalidations bind the actual
filled UUID and event key, not all TPC trades. Page/history/P&L/grade queries omit
only these bound logical trades; malformed/unreviewed audits do not hide trades.
Session restoration also omits the targets' original rows from in-memory P&L and
re-entry seeding, while retaining the two audit records in the restored view.

Reference input must be the retained adjusted grouped close body for the saved
prior-close date, with matching hash/source/acquisition evidence. Select exact `T`
keys TPC and BCPC, never uppercased TpC/BCpC. Read-time cache overlay changes only
those two keys after the audit; same-day restart keeps the correct saved values
without an API call. Old SESSION_READY is not updated/deleted.

Material native-input gap: worker's retained paper-rows file stops before the
13:26ET fills. Current exact two FILLED UUIDs/keys/preimages and actual BCPC common
prior-close proof have NOT been supplied/captured here. The test UUIDs and BCPC81.12
are controlled synthetic data, not production targets/prices. Main must obtain,
verify and approve those current bindings; this worker performs no DB/API read.
Tool output is a plan/COPY proof, not a reviewed live-execution interface. Main's
normal append-only install action still needs explicit current preflight/review.

## Prepared Performance Gate

`scripts/benchmark_momtpc_warning_a.py` is PREPARED BUT NOT RUN. It requires
`--run-approved --expected-head <exact-composed-head> --output <fresh-file>`.
Only main may run it after the serialized window clears. It exercises actual
provider warning-A filter and gateway/publisher using offline doubles, 250 valid
A/s plus rejected preferred frames,60s,strict stall<50ms, zero reverts/drops and
13,269-record snapshot batches. No clock/gate override, provider or Redis network.
It measures a local synthetic source gate, not historical WS peak/live warning.
Validate/run on the final composed GIL/L/T source, not an obsolete T-only head.
No execution, timing result, PASS or production qualification is claimed here.

## Main Post-Restart Soak Plan

1. After reviewer approval and a serial gate PASS, record exact composed/installed
   gateway+paper heads/PIDs/source hashes, current reference/audit manifest hashes,
   exactly two target UUIDs/logical IDs and the three appended audit UUIDs.
2. Before/after page proof: two target trades absent from normal page rows and
   gross P&L/grades; original events remain in the append-only table. A legitimate
   later TPC trade and all unrelated/warning-ledger rows remain visible.
3. Same-day paper restart: saved reference overlay resolves TPC to verified common
   prior close and BCPC to its verified common value. No new provider request solely
   for repair and no reload of the pre-correction15.62 value.
4. Use main's existing capture connection only. Observe30min from the verified
   new process start; count raw rejected TpC/BCpC separately from accepted uppercase
   TPC/BCPC snapshots/A frames and real trade callbacks. Preserve raw case/source,
   timestamps, sampling denominator and both common/prior-close source hashes.
5. Acceptance: zero accepted preferred-derived/alternating15.x<->82.x TPC samples
   or BCPC class collisions; zero new case-collision detections/fills; >=1 real
   non-collision warning reaches paper; strict1s/2s/1008 gates unchanged,0 reverts.
   Sparse/no TPC or BCPC observations are UNEXERCISED, not a zero-alternation PASS.
6. Stop/revert through the already-approved normal path on gate failure; keep raw
   failure proof, no blind re-arm or favorable rerun. Scanner/strategy class filter
   remains outside T until the reviewer/operator future card.

No retirement/cache correction/deployment/re-arm/soak/benchmark/queue write has
been performed. P is explicitly preempted at its durable CHECKPOINT until T freezes.
