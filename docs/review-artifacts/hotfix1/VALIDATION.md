# HOTFIX1 validation and limitations

Base: 1a70da19176d2cad486e8518e1c9030f1cabb968. No production action.

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

Full-suite pair: final receipt pending. The first development run was
62 failed / 7,086 passed against 56 failed / 7,077 passed; its six new failures
were diagnosed rather than hidden. Final compatibility fixes require a fresh
pair before this branch is review-ready.
