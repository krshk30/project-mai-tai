# NFQ2 released residuals: local source freeze and bounded proof

Starting published head c6004bd59f06fd856b1a8bb5a1ff6d96cbe094dc. This follow-up
remains a linear stack on unchanged SLOT9951b473f8203590f48c667770c07e1f9c3096b6.
Main994f08aee2c35b3809b3c3384e0d8628807dcfb7 remains unchanged. No SLOT runtime,
catalog, ORB, RPGSTALE/#1115, handoff, staged-job or production changes.
Only runtime delta is src/project_mai_tai/oms/eh_fresh_price.py; only added test
module is tests/unit/test_nfq2_recovery_legacy.py. Remaining changes are receipts
and mutation runners. One Codex marker in the follow-up commit.

Frozen file SHA256:
- eh_fresh_price.py: 1d920d4575a47f0daa2a5e7aeba7643556fc17f176bed7e0a10d6ac5bab2c8ee
- test_nfq2_recovery_legacy.py: 34c2272702626cf1968cd6b9fb0e65ac3c386c057f18fc219151879faa6bac6e

## Proof policy

Serial durable CAS stores event UUID, deterministic client ID, retry generation
and hold ID BEFORE wire. Recovery binds exact durable intent+order, account,
strategy v2, symbol, side BUY, quantity, slot/segment and generation. Broker
requests use existing read-only exact-parent BUY detail readers, not a cancel
submission. Terminal cancelled/rejected requires explicit cumulative zero and
exact broker identity; labels, ACKs, not-found, absent order, generic cancel
events, unknown quantities, working orders and unsafe identity never clear.
Post-read runtime generation recheck plus locked durable recheck/CAS prevents
retiring a changed owner. Positive fills are sticky no-rebuy evidence; recovery
does not fabricate Fill/position records or attempt broker fill accounting.

Recovery retires only the proven-clear NFQ attempt. It NEVER automatically
queues/resends its saved BUY, guesses a new segment, clears another owner or
releases an RPG ticket. Existing read-only local RPG no-wire proof is accepted
only with exact NFQ intent/order binding; no handoff/table/ticket writes.
Older bound NFQ snapshots without dispatch metadata derive identity only from
their exact linked durable intent/order and stored retry generation. Unbound
legacy inputs are refused while NFQ ON, even with fresh ask. Only an exact
pending/created pre-wire intent with no linked order may be drained. Held,
filled or order-owned intents remain unchanged. OFF/non-EH behavior is unchanged
unless an already-existing uncertain v2 owner blocks the same account+symbol;
turning the flag OFF cannot erase that obligation.

## Frequency and Unknown Budget

Recovery is periodic only, behind the existing broker-sync sweep cadence:
max(1s, configured oms_broker_sync_interval_seconds), default **5s**. At most
one in-flight read per hold; each await is bounded at **2s**. No ticks drive
recovery SQL/network. Default steady-state upper rate is **12 reads/minute per
eligible uncertain hold** (an initial sweep can occur immediately), proportional
to eligible holds, not ticks. Two eligible account holds imply at most 24/minute;
requests are sequential in the sweep, not concurrently fanned out. Delays only
reduce this rate. There is no lifetime attempt cap, exponential backoff or
age-based release: repeated unknowns remain owned and may be reread at cadence
until positive proof appears. Missing binding/reader and sticky-fill owners do
not issue broker reads. This is explicit fail-closed policy, not guaranteed
recovery of every legacy order or an aggregate venue-rate certification.

Controlled unknown-budget receipt read-budget-01: virtual elapsed 0/5/10s
produced exactly 1/2/3 reads, 18/36/54 SQL statements, 3/6/9 DB transactions,
1/2/3 commits, all SQL off-loop. Intermediate 0.1/4.999/5.1 sweeps changed none.
400 subsequent uncertain/unrelated ticks produced **0 SQL, 0 transactions,
0 commits, 0 reads**. One unknown attempt's 3 periodic transactions include
two read-only transactions and one final recheck commit; this periodic work is
not tick work. Default one-hold steady-state budget is thus 216 statements,
36 transactions and 12 commits/minute in this SQLite control. These figures
exclude setup/restore; they are not production SQL costs.

## Recorded Boundaries

Unchanged recorded broker bodies: Schwab AMOD terminal-zero, Webull AIXI
terminal-zero and Webull AMOD filled. NFQ hold, dispatch event, ledger link,
clock, uncertainty and transport are controlled test scaffolding. Webull
recorded client prefix is projected into a controlled full event UUID (unknown
historical suffix padded with zero); that full UUID is NOT recorded. These are
strict recorded-body compatibility controls, NOT historical BIYA recoveries,
actual placements or live OMS performance. BIYA original intent shape remains
recorded; no invented bars/prints. SLOT recorded SXTC/DKI and present restored
RPG-ticket controls retain their prior explicit historical/scaffold boundaries.

## Fast Results and Preserved Failures

Final working-source fast-focused-02: **504 passed, 2 deselected, 12.40s**,
UTC20:45:38.739537-20:45:52.187200, raw SHA256
42e2d1d71e962b12ad277581e42cd6073bff3050872f6c1d2f357c9d8df4b3b7.
Includes 86 new residual tests and existing NFQ/SLOT BUY+SELL/HOTFIX/NFQ1/
recorded exact-parent readers/OMS EH/flip-owner controls; the two deselected
cases are 60-second benchmarks. Ruff and diff whitespace checks pass.

Current named mutation failures: R1-R11 and L1-L6 (17 new), N1-N17 (17 NFQ),
C1-C3 (3 joint compatibility): **37/37 RED**, no import/setup failures counted.
R4 initial selector count failure is preserved in mutations-01/R4; corrected
selector killed in mutations-02/R4. R11/N8/C1-C3 in mutations-03. Original N8
selection became NOT_RED because its malformed next-slot input is now refused
by the independent legacy guard; old-mutations-01/N8 is preserved. N8 now
targets a valid existing uncertain owner with flag OFF and kills in
mutations-03/N8. Neither runtime safety guard nor test assertion was weakened.

FIRST_FAST_FAILURE.txt preserves earlier duplicate-control >50ms failure
(62.725ms), unchanged-threshold rerun passed; no concurrency cause claimed.
fast-new-01 preserves 37 setup failures/20 pass due to nonexistent ORM
relationship access; corrected test setup passed57 then74 then83. None of
these failed receipts are erased or promoted to passing evidence.

## Reproduction and Remaining Gates

Use /Users/velkris/Projects/project-mai-tai/.venv/bin/python from this worktree;
PYTHONPATH=src:. for focused controls. Exact commands and UTC/raw hashes are in
each result.json. Mutation commands run check_mutations.py <ID> in isolated
child processes and never modify checkout source.

After parent CPU release, freeze this source and run sequentially:
```
env PYTHONPATH=src /Users/velkris/Projects/project-mai-tai/.venv/bin/python docs/review-artifacts/nfq2-hotfix1-proof/run_proof.py benchmark --label recovery-activehold-01
env PYTHONPATH=src PROOF_TIMEOUT_SECONDS=900 /Users/velkris/Projects/project-mai-tai/.venv/bin/python docs/review-artifacts/slotclear1/run_receipt.py /tmp/codex-nfq2-on-slotclear1-linear-20261007 /tmp/nfq2-recovery-final-full-20261007 /Users/velkris/Projects/project-mai-tai/.venv/bin/python -m pytest tests/unit -q --tb=short -vv
```
Full command has no observer/plugin/tee. Reuse actual current-main994 baseline
/tmp/slotclear-current-main-994-unit-receipt/output.txt: 7114PASS/56FAIL455.80s;
do not rerun unless candidate failed names differ. Use existing
docs/review-artifacts/ownmix1/verify_suite_pair.py suite(Path) exact-node parser
to avoid async RuntimeWarning text appended to a parameterized failed node.

New source is NOT equivalent to prior5d77/c600 runtime, so prior7247PASS56FAIL
full proof and prior29.0855ms benchmark do not certify this follow-up. New final
benchmark/full pair/fresh CI and independent review remain pending. Publish
only one tested follow-up head, no merge, no pin/deploy and no readiness claim
until actual safety/CI/review prerequisites are met. Later raw full/performance
receipts may stay outside committed tree and be linked by PR comment without
avoidable head churn. Parent owns handoff.
