# HOTFIX1 validation and limitations

Base: 1a70da19176d2cad486e8518e1c9030f1cabb968. No production action.

## Current status: NOT READY FOR RE-PIN

The frozen runtime/test-tree 2eb55e370ba65fb46bcc22f3471a99ca8a8c8e01 run
finished **57 failed / 7,113 passed / 0 skipped**, 465.75 seconds. Main's
existing receipt is **56 failed / 7,077 passed**. One additional failed name:
`test_recorded_240_events_per_second_60_seconds_slow_db_flat_transactions[retained-on]`.
It processed 14,399 events in 60.002594 seconds (239.973/s), with one initial
transaction and zero after one second, but loop delay peaked at 158.85844 ms,
failing the unchanged <50 ms gate. Zero buys, every hard-stop-reader call
still made. Full log: `full-frozen-failed-addendum.txt`; exact names and XML
paths: `full-suite-pair-addendum.json`. This failure is not omitted or waived.

The preceding run collected the tests before the import-only Ruff cleanup:
56 failed / 7,114 passed, 449.58 seconds, identical failed names to main.
Raw XML `/tmp/hotfix1-addendum-full-20261007.xml` and comparison
`/tmp/hotfix1-addendum-preliminary-pair-20261007.json` remain local. The frozen
rerun supersedes it as the final full-suite receipt; the import alias is not
claimed as a cause of the stall.

Both GitHub Validate runs on 2eb55e37 succeeded (37644693690, 37644700623).
Independent-review-pin is not PASS; no fresh reviewer pin is claimed. Any
later docs/test-diagnostic head must be reviewed at its own exact SHA.

Test-only diagnostics now measure per-handler duration and GC durations,
without disabling GC, changing pacing, excluding setup from an existing
assertion or relaxing the 50 ms limit. Isolated passes cannot explain the
frozen failure. The cause remains UNMEASURED, so re-pin remains blocked.

Isolated diagnostic receipt `benchmark-addendum-diagnostic.txt`: both variants
pass, 14,400 events each. ON max stall 19.33179 ms, max handler 10.92283 ms,
one GC collection 0.27954 ms and one SQL transaction. OFF max stall 26.58054 ms,
max handler 10.48158 ms, no GC collections, no SQL, no retained admissions.
These are new runs, not retrospective GC attribution for the 158.858 ms failure.

## Rollback addendum: active restore and OFF admission

Runtime/tests: ace0dca73c4318997e9f7a841d459b9fc4e8a200, on top of
the originally published 92bc6a15. Opportunity startup query selects only
held/queued; terminal rows do not enter either the detached cache or owner set.
Queued tokens are invalidated on restart. Uncertain wires are restored in a
separate serial-only fence set, not as opportunities or tick actors. A retired
row with unresolved wire evidence still fences duplicate buys.

Outer callback and inner retained scheduler check the flag itself; symbol
NFQ evaluation does not call retained evaluation while OFF. OFF with an
existing held, queued, retired or uncertain row makes no retained schedule,
cache lookup or evaluation call. Existing legacy NFQ and exit/drift readers
are not disabled by this feature flag.

A new regression test exposed terminal deferred projections falling into
legacy retry admission after their retained ID was removed. Commit publication
now also removes the matching terminal projection, fenced by identity and event
time; a newer projection is not deleted. Rollback leaves membership unchanged.

Focused lifecycle run: 104 passed, two benchmark variants deselected, 14.71 s.
All 20 mutation controls RED, including seven addendum controls: all-phase
restore, outer/inner OFF bypass, terminal ID leak, dropped uncertain fence,
OFF NFQ retained evaluation, and terminal projection leak. Two initial new
mutation anchors caused HARNESS_ERROR and were repaired; only assertion-level
test failures in the rerun are counted. See mutations.json and raw receipts.

`benchmark-addendum.txt`: two isolated 60-second recorded-price replays,
14,400 events each (240/s). ON: 19.88716 ms max loop delay, one initial SQLite
transaction, zero after one second. OFF with the existing held row and its
memory index present: 25.65586 ms, zero retained scheduler admissions, zero
transactions for the whole run. Both: 14,400 hard-stop-reader calls and no
queued buys. The unchanged <50 ms threshold passes. Gate/DB-delay controls
and mocked exits are disclosed in the receipts; this is not live OMS proof.

Ruff for the four runtime/test files passes after an import-only fixture
export cleanup (alias then assignment) and removal of an unused UTC import.
No assertion, fixture object, runtime or threshold changed in that cleanup.

The following earlier sections describe the original head, not this follow-up.

## Performance

`benchmark.txt`: final isolated offline replay, 14,400 events in
60.000594875 seconds (239.9976/s), maximum watchdog delay 27.19094 ms.
One initial real SQLite transaction, zero after the first second, zero queued
buys. The owner is in-band but the authoritative gate is deliberately blocked;
its price eligibility uses a disclosed controlled reading. Quote/trade prices
are from the own bounded BIYA/SXTC pulls. Preparation sleeps 200 ms off-loop.
Hard-stop readers are counting stubs, not real broker exits. This is not a live
DB transaction-rate or whole-OMS latency measurement.

Earlier trial retained in `benchmark-contended.txt`: 80.35736 ms, FAIL against
the unchanged 50 ms requirement, despite flat DB work. A focused test suite was
running concurrently; attribution of the spike to host contention is
UNMEASURED. A preceding receipt visible in the tool outputs was 40.71536 ms;
that overwritten first file is not presented as a retained raw receipt.
The final test uses a bounded counter instead of accumulating 14,400 AsyncMock
call-history objects. The threshold is unchanged; the failed trial is retained.

## Controls

`mutations.json` and individual `mutation-*.txt` receipts: 13 RED. Each mutant
is compiled from the real method source in one isolated pytest process; source
files are not rewritten. Coverage: synchronous tick SQL, publication before
commit, rollback leakage, stale revision, wrong symbol, duplicate worker,
unchanged blocked-owner work budget, durable retired phase, shutdown token,
NFQ hold log, broker-sync cache refresh, stale working-order revalidation,
and the last tick during a busy worker.

The publication mutant was corrected after review: it now publishes the actual
pending snapshot immediately after assignment, before commit. Its receipt fails
the pre-commit cache-absence assertion, not a later missing-cache KeyError.
The original listener-registration mutant was not sufficient evidence for the
early-publication guard and is superseded by this assertion-level RED.

The first retirement-query mutation survived because the phase guard still
refused that row. That redundant query-only mutant is not counted as RED.
The final mutation targets the authoritative phase guard with a cached held
admission followed by a committed durable retirement before the worker reads.

## Compatibility

Existing exact cancellation assertions are retained. Two pre-existing tests
now explicitly drain admitted workers; the PA1 wiring test follows the new
nonblocking scheduler through to the unchanged evaluator. The serial intent
method remains introspectable through a wraps-preserving cache-refresh
decorator. No price/band/size/exit rule, actual-submission budget, flag default,
RPG ownership proof, or legacy NFQ implementation is changed.

Startup restores held/queued owners into a detached symbol map. Every owner
write publishes its committed revision; rollback publishes nothing. Terminal
owners leave the index, and older workers cannot restore them. Workers are
deduplicated, last-tick dirty wakeups recheck memory, and shutdown drains DB
work without cancelling a transaction mid-commit; queued mirror claims are
invalidated rather than emitted. Serial dispatch still verifies durable
generation/token/phase/quantity/duplicate-buy proofs.

Broker-sync, startup, serial intent completion and drift-cancel writeback
refresh a detached working-order cache. Only an actual drift candidate reaches
off-loop revalidation and broker cancellation; inside-band/unrelated/empty
ticks do not query. Exit ladder and hard-stop decisions remain on the existing
paths. Legacy NFQ's separate synchronous preparation remains outside this
patch; no blanket claim of a database-free OMS is made.

## Full-suite pair

Python 3.12, `PYTHONPATH=src`, `pytest tests/unit -q --tb=short`:

- Exact main 1a70: 56 failed / 7,077 passed / 0 skipped, 379.53 seconds.
  XML: `/tmp/main-1a70-hotfix-rpgnowire-baseline-20261007.xml` (parent's baseline).
- Final HOTFIX1 runtime/tests: 56 failed / 7,101 passed / 0 skipped,
  400.81 seconds. XML: `/tmp/hotfix1-head-final-20261007.xml`.
- Failed-name diff: no additions, no removals. The throughput failure on main
  remains a failure on head; no volatile count is suppressed.
- Focused compatibility run: 389 passed, 1 benchmark deselected, 36.83 seconds.
  The full suite includes the 60-second benchmark, which passed too.

`full-suite-pair.json` lists every baseline/head failed name and raw XML path.
Runtime and ordinary tests did not change during or after that final run.
The only test-file edit during the run was the standalone mutation plugin's
early-publication anchor; the ordinary suite does not load that plugin. A
redundant frozen-commit rerun was interrupted after the parent accepted the
exact XML pair and directed no whole-suite rerun for that harness-only change;
the interrupted rerun is not presented as a completed receipt.

The first development run was 62 failed / 7,086 passed. Its six new failures
were fixed without weakening the original cancellation assertions. Focused and
full runs still emit pending confirmation-recovery/protection task warnings;
this patch does not claim to fix those separate task lifecycles.

## Parent integration proof

Parent-reported additional proof, not this PR's own source/test receipt:
disposable `/tmp/hotfix-rpgnowire-composition-20261007` combines HOTFIX1
964ed0ca with RPGNOWIRE1 e52e238b + 74ad6c70. Result: 85 passed, 1 benchmark
deselected; XML `/tmp/hotfix-rpgnowire-parent-composition-20261007.xml`.
Its temporary `tests/unit/test_hotfix_rpgnowire_composition.py` checks committed
queued SXTC cache -> RPG release preparation -> cache eviction -> no BUY from a
stale serial copy, plus both lanes' controls and RPG/NFQ/L5/L7 composition.
That temporary test is not added to this PR; the parent owns the combined tree.
