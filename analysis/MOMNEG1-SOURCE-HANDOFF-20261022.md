# MOMNEG1 restored live-only rule

Owner Cicero. Operator/Claude ruling 2026-10-22 14:10:54 ET.
Base aa16266823e7ef0ebb58a44e8efb64af417fc2bc; separate codex worktree.

The paper engine `_summary` seals `event.prior_close`, originally from
`engine.prior_closes`, into each record. `LiveHandoff.offer` consumes that existing
in-memory record before country routing, ledger claim, queueing or any await.
Unknown/nonfinite/nonpositive prior close refuses; signal below known prior close
refuses. Equal or higher retains all existing admission gates. No new I/O.
Both warning windows and broker routes share this boundary, PM and RTH.
Paper engine, warning candidate selection, exits and OMS actors are unchanged.
PRICE1 ownership remains Avicenna; no edits to engine/warning consumer/OMS.

Existing synthetic handoff fixtures omitting `payload.prior_close` will now refuse
as unknown, intentionally. They need faithful prior-close input for positive
controls; no permissive fallback is allowed. Actual-case fixture values are
reviewer-reported, not newly replayed or native attestations.

P1 source controls and P2 old/new timing driver plus local composition controls
are authored separately. ALL execution, imports, tests, performance, actual
trade replay and qualification are UNRUN; main owns execution. No PASS claim.

## Main-only command and output contract

P1/local composition, 49 authored parameter cases (not collected):
`PYTHONPATH=src:. python -m pytest -vv -o addopts= tests/unit/test_momneg1_live_admission.py`

P2, three authored cases: copy the IDENTICAL new timing test file into a separate
main-owned old aa162668 checkout and run there, then on the new source:
`PYTHONPATH=src:. python -m pytest -vv -s -o addopts= tests/unit/test_momneg1_signal_timing.py`
Retain raw output/XML/head/source/test hashes and one claim per old/new run.
The test records 1000 actual `LiveHandoff.offer` calls per shape with p50/p99/max,
outcomes and a strict maximum below 50ms. Setup is outside timed admission.
This local offer microbenchmark is NOT the original VPS full signal-path proof;
main must also run the original native timing selection unchanged. P3 actual
ALL-ON composition with PRICE1/L2/L11/L12/L13 as qualified is still UNRUN.

Source P1 data: exact reviewer-reported FUFU/EYPTx2/CRMT signal/prior prices;
PM/RTH and both broker route shapes. GIXI 5.25@08:19 uses explicitly synthetic
prior5.00 for policy control, NOT actual GIXI replay acceptance: actual prior
close and retained decision packet needed from main/Claude. No unknown converted
to fake up-day proof. Engine-generated bucket record controls establish memory
prior serialization and unmodified paper state. No real tape replay is claimed.

## Preserved actual122 checkpoint

Temporarily QUEUED, not cancelled, by explicit urgent MOMNEG1 preemption.
Assessment WT `/private/tmp/mai-tai-l11-l12-replay-assessment-20261022`, head
987e0633e6a9276b755de53ba13ccad0e37f4245, committed 14:11:17 ET.
Original 8f10666a contract retained. `analysis/audit_actual122_files.py` is
uncommitted and unrun. Independently bulk-verified cases: ZERO (not an empty
dataset claim). Delivered denominator remains reported 122:57/A27/B17/C12/D9.
Resume after this source increment: verify eight input hashes, inspect complete
case schema/CROSS-ACCOUNT fields, then prioritize B13 and emit all122 evidence
table. Historic native books remain UNKNOWN/not stored; do not await them.
No shared queue/status/LATEST write; main records preemption and resumption.
