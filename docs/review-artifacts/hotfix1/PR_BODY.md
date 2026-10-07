## Independent Step 0

**NOT READY FOR RE-PIN: frozen full-suite performance gate failed.**
The functional addendum is implemented, but the frozen rerun is **57 failed /
7,113 passed** versus main **56 / 7,077**: the one new failure is the retained-ON
60-second benchmark, **158.858 ms** maximum loop delay against **<50 ms**.
The failure is preserved in `full-frozen-failed-addendum.txt` and the exact
failed-name diff in `full-suite-pair-addendum.json`. Flat DB work and passing
isolated trials do not turn that failure into PASS. Cause remains UNMEASURED;
no attribution to host contention or GC is asserted. No merge or deployment.

### Rollback addendum: AGREE; fresh exact-head review required

My own code read reproduced the all-phase startup selection and ownership-based
OFF bypass. Startup now restores held/queued opportunities only. Uncertain
wire evidence remains a separate serial duplicate-buy fence, never a tick actor.
Committed terminal rows lose both opportunity IDs and stale deferred projections
so they cannot resurrect as legacy retries. Both retained tick admission points
check the flag directly. The archived production snapshot was not touched.

New receipt: **20/20 mutation controls RED**, including seven addendum controls;
**334 focused passes**, two benchmark variants deselected. Each isolated
60-second rate test processed **14,400 events (240/s)**. ON max stall **19.887 ms**,
one initial SQL transaction and none after one second. OFF with an existing held
row/index: max stall **25.656 ms**, **zero retained scheduler admissions / zero SQL
transactions** for the whole run. Zero buys; exits are mocked, not broker proof.
Ruff passes on all four runtime/test files. Raw receipts and limitations are in
`benchmark-addendum.txt`, `focused-addendum.txt`, `mutations.json`, `VALIDATION.md`.

Feature-OFF means quote/trade ticks admit no new retained work; it does not
disable legacy NFQ, exit/drift readers, periodic retirement or serial
reconciliation of an already uncertain wire. Those safety fences are preserved.

Issue **AGREE** from my own main-1a70 code read and bounded read-only production
pull. Retained-mirror quote/trade callbacks synchronously scan/lock held rows;
drift cancellation also reads candidates on eligible ticks even with the
retained-hold flag off. Own BIYA/SXTC intents, quotes/trades, logs and whole-DB
counter samples are committed under `docs/review-artifacts/hotfix1/`.

Fix **AGREE in principle**, with commit-only revision publication, detached
symbol caches, background deduplication, and durable dispatch revalidation.
Awaiting a slow DB thread from a serial callback is not enough: ticks now
schedule work and return. My first draft did not satisfy that requirement.
Historical open latency/py-spy percentages and decision-cache timing remain
**UNMEASURED** independently. Whole-DB counters are not path attribution.
`ASSESSMENT.md` records the assessment timing and paused/resumed implementation.

## Scoped change

- Retained-owner startup/hold/queue/report/retire writes publish detached
  committed revisions; rollback publishes nothing; old revisions cannot
  resurrect cancellation. Tick memory gates are symbol-specific.
- Deduplicated background workers retain last-tick wakeups and drain on
  shutdown. Queued claims are invalidated instead of emitted during shutdown.
  Durable generation/token/phase/quantity fences still gate serial dispatch.
- Drift working-order snapshots refresh at startup, broker-sync, intent
  completion and cancel writeback; actual cancellations revalidate off-loop.
- Restore `[OMS-NFQ1]` held/cache-age/source logging.

No trading rule, price, size, wire-attempt cap, flag default, RPG source, or
production state changed. The legacy NFQ synchronous preparation path is
separately owned and remains outside this patch: this is not a claim that the
whole OMS is SQL-free on every tick. Exit decisions remain on existing paths.

## Initial-head Evidence (92bc6a15)

Focused: **389 passed**, one benchmark deselected. Full Python 3.12 pair:
main **56 failed / 7,077 passed**, head **56 failed / 7,101 passed**, zero skipped,
exactly identical failed names (`full-suite-pair.json`). The full run includes
the passing 60-second performance test. Existing task warnings are disclosed.

Offline isolated replay: **14,400 events / 60.000594875 seconds = 239.9976/s**;
maximum loop delay **27.19094 ms**; **one** initial real SQLite transaction,
**zero** after the first second; zero queued buys. Includes a real recorded
held symbol with controlled in-band eligibility and a blocked authoritative
gate, plus controlled 200 ms DB delay. **Exits are mocked; this is not
production latency or live DB transaction-rate proof.** Failed prior trial
**80.35736 ms** is retained and disclosed, not discarded or threshold-relaxed.

**13 mutation controls RED**, including actual pending-snapshot publication
before commit (assertion failure, not KeyError). Receipts cover rollback,
revision/retirement, symbol isolation, deduplication, blocked-gate work budget,
last-tick wakeup, shutdown token fencing, hold logs, broker-sync refresh and
stale working-order revalidation. See `VALIDATION.md` and `mutation-*.txt`.

Parent-reported combined HOTFIX1/RPGNOWIRE1 proof: **85 passed / 1 benchmark
deselected**, `/tmp/hotfix-rpgnowire-parent-composition-20261007.xml`.
The parent's disposable test verifies queued SXTC cache -> RPG release ->
eviction -> stale serial copy cannot BUY. It is separate from this PR's tests.

Key worker receipts:
`test_last_tick_new_committed_generation_is_not_lost_while_worker_busy`,
`test_shutdown_drains_db_and_invalidates_reserved_token`,
`test_durable_retirement_fences_already_admitted_stale_worker`,
`test_unknown_gate_is_once_per_revision_not_per_tick`.

Review only: no merge/deploy authorization here. Live retained-hold flag stays
false until the separately reviewed shipping decision. Installing this code
requires an OMS restart under the parent install plan, not this build lane.
