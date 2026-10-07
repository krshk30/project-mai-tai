# NFQ2 + HOTFIX1 active-hold proof: scoped offline PASS

This closes the requested combined quote-handler benchmark proof only. It is
not NFQ2 readiness, full-source certification, installation approval, live OMS
performance, or a waiver of HOTFIX1's separate intermittent timing failures.
No production, publish, merge, pin, source-branch edit or rebase occurred.

## Exact trees and source paths

| Identity | Commit | Tree |
| --- | --- | --- |
| HOTFIX1 APP baseline | 994f08aee2c35b3809b3c3384e0d8628807dcfb7 | 7d827a407825d1d93aae8c3946d8b55320b9680f |
| Pinned equal whole tree | 5525739a | 7d827a407825d1d93aae8c3946d8b55320b9680f |
| NFQ2 original source | 0318ef84dc11b1b052073092828b80ae23c1bf54 | Original worktree remains clean at that head |
| Clean combined runtime | 494bbeefdc0cd392747f528010c4f73043d2477b | 5563510bce5b6e255d8daceca7835b58b87991d3 |
| First test freeze, runs 1/2 | ba265afe4085f301b0182cf800bfa38cdab2c80f | d2236af7bd35044639c695a8496380d9a87933ec |
| UTC diagnostic test freeze, final run/controls | a5479b42ac633f144d70930b44e99d5417c86d10 | 8736a3d0df5b327b540690f3eca8c5882c39fbb0 |

Proof worktree:
`/Users/velkris/.codex/worktrees/nfq2-hotfix1-activehold-proof/project-mai-tai`.
Original NFQ2 source:
`/Users/velkris/Projects/project-mai-tai-nfq2-side`, branch
`codex/nfq2-eh-hold-side`. It was read, never edited.

Actual imports and source/test/runner SHA256 values are frozen in every run's
environment.json. Runtime source paths under the proof worktree:
`src/project_mai_tai/oms/service.py` (actual full quote handler, enabled drift),
`src/project_mai_tai/oms/eh_fresh_price.py` (NFQ2 eligibility/CAS/periodic proof).
Owned test: `tests/unit/test_nfq2_hotfix1_combined_proof.py`.
All edits after clean NFQ-only cherry-picks are owned tests/receipt files;
`git diff 494bbeef HEAD -- src ops` is empty. NFQ2 runtime/catalog blobs other
than the HOTFIX1-composed service.py exactly match original 0318ef84.
HOTFIX1 mirror_retained_hold.py is byte-identical to 994f08ae.

The first cherry-pick omitted the NFQ assessment prerequisite and hit a
documentation modify/delete conflict. The failure/abort is preserved; the
complete NFQ-only sequence 0239726f, 16c435b0, 0318ef84 then applied cleanly.
No runtime conflict was manually resolved. Worktree creation succeeded; app
attachment failed twice due to its >100 attachment limit, so the returned
managed checkout was used directly.

## Measured combined quote-handler results

| Receipt | Events | Real seconds | Events/s | Max loop stall ms | Max handler ms | Tick transactions | Periodic transactions |
| --- | --- | --- | --- | --- | --- | --- | --- |
| benchmark-01 | 14400 | 60.000104708 | 239.999581 | 48.385250 | 49.830625 | 0 | 24 |
| benchmark-02 | 14400 | 60.000434500 | 239.998262 | 35.860500 | 16.770542 | 0 | 24 |
| benchmark-03-utc | 14400 | 60.000599000 | 239.997604 | 29.370750 | 19.790000 | 0 | 24 |

All three pass the unchanged >=200 events/s, >=60 seconds and <50ms loop-stall
gates. Final exact gate UTC: **2026-10-07T18:44:53.335307+00:00 through
2026-10-07T18:45:53.336535+00:00** (14:44:53.335307--14:45:53.336535 ET).
Final subprocess: 18:44:52.737196--18:45:53.584243 UTC.
Runs 1/2 lack exact gate wall-clock boundaries; their recorded launch times
and monotonic durations are retained without fabricating retrospective UTC.

Each run has 7,200 matching stale BIYA quotes and 7,200 fresh unrelated OTHER
quotes. Final actual per-second buckets range 237--243 (all 60 >=200).
One NFQ2 hold stayed held at every tick, zero phase violations and zero retries.
Durable end state remains held, and the drift working order remains accepted.
Drift is ENABLED at 1 cent, with a real cached same-symbol 2.55 working limit:
14,400 actual drift-reader calls and 7,200 actual cached price checks each run.

Each run has zero tick sessions, zero tick transaction begins, zero tick SQL
statements and zero loop-thread SQL. Measured periodic work: 12 NFQ2 ownership
proof transactions /24 statements plus 12 drift cache transactions /60
statements, all on worker threads at the configured 5-second cadence.
All 60 per-second tick-transaction counts are zero. The excluded periodic
budget is explicitly 0.4 transactions/s total; it does not scale with ticks.
Setup seed/hold/initial cache SQL and the final state-inspection SQL are outside
the timed gate, as disclosed in PROOF_SCOPE.md and environment.json.

Final benchmark raw-log SHA256:
`6e64c951328c38acae8c5a8fd3e47ed603cd4d6946ffbe0c304e94fadf1756d7`.
All result.json receipts assert files_and_head_unchanged=true for their runs.
Authored Python/Markdown/JSON pass diff whitespace checks and the new test/runner
pass Ruff. A whole receipt-commit diff whitespace check reports trailing spaces
in raw pytest failure output. Those .txt receipts remain byte-for-byte intact
with their original hashes; they were not reformatted to hide failed controls.

## Functional and mutation controls

Timestamped fast run: **9 passed /1 benchmark deselected**, 0.97s.
Focused existing NFQ2/HOTFIX1/EH/mirror/catalog compatibility suite:
**361 passed /3 benchmarks deselected**, 19.75s. This is not a full suite.

Fast cases: 1,000 irrelevant-active, 1,000 stale-active and 1,000 no-hold
quotes each open zero sessions/SQL. A 150ms delayed eligible persistence unit
runs off-loop; 1,000 concurrent duplicate quotes take 10.8305ms and reserve
one retry via one worker transaction. Exact duplicate gate UTC:
18:44:44.990118--18:44:45.000955. A real drift worker receives 1,000 duplicate
drift quotes plus 1,000 unrelated quotes: one simulated cancel, zero buys,
3 worker transactions /17 statements, active NFQ2 BIYA hold untouched.
All four EH path/account variants process 101 quotes, queue one retry, accept
two duplicate serial deliveries, and create exactly one simulated filled order.

**23/23 in-memory controls RED**: six new combined controls plus prior N1--N17.
NFQ2 and drift restored tick SQL each cause all three 1,000-event admission
cases to fail with 1,000 on-loop transactions apiece. Replacing eligible worker
SQL with synchronous SQL fails its worker-thread assertion. Irrelevant-symbol
reading, removal of the in-flight fence, and disabling drift also fail their
specific assertions. Duplicate-fence mutants additionally stress SQLite's
shared connection and log DB errors; their assertion-level failure remains
preserved, not attributed to production. No mutation rewrites source files.

The initial fast-expanded.txt harness failure is retained: shutdown was called
before admitted drift dispatch, and correctly suppressed the cancel. Only the
test's drain ordering was corrected; no runtime fix was made. Repaired and
frozen fast runs pass. No unexpected source/test failure remains in this lane.

## Boundaries and blockers

Real full quote handler, real NFQ2 memory admission, real SQLAlchemy ORM/SQL,
real drift predicates/scheduling/read/write; simulated broker and fake Redis;
SQLite memory DB/StaticPool, fixed logical NOW and AM helpers, real elapsed
time/GC. No drift stub or disabling, no accelerated timing or relaxed gates.
Retained-mirror flag ON but no retained-mirror hold is seeded. No active exit
positions/armed stops. Periodic task calls actual NFQ2 proof and drift cache
refresh; full broker sync, live OMS, PostgreSQL, exits and live broker wires are
not benchmarked. Serial replay's fillability/collision boundaries and drift
broker-boundary replacement are explicit. Current serial claim/intent SQL
remains synchronous outside the measured tick window.

Parent reported exact-994 focused result: 1 failed /260 passed /142.04s;
retained-ON gate has 14,400 events, 239.9988/s, startup SQL 1 /after-first-second
SQL 0, max handler 20.516ms, GC 0, watchdog **79.737ms**, failing <50ms.
Retained-OFF passes. Reported ON gate is approximately 14:42:19--14:43:19 ET.
This is user-provided evidence, not a copied/independently parsed parent raw
receipt. The parent owns its first failure receipt and subsequent rerun.
It is preserved as a standing failure, with no concurrency cause claimed.
Original HOTFIX1 80.357ms and 158.858ms failures remain in the baseline tree.
Prior NFQ2 enabled-drift failure is copied verbatim with its matching SHA256.

Observed parent PID 42330 belonged to
/private/tmp/codex-linesrc-hotfix-install-tree-20261007; it was absent at
18:44:44 UTC. No matching Python pytest/proof process was observed immediately
before the final launch at 18:44:52 UTC (empty process-observation receipt).
No other tests were launched by this lane during any of its three benchmarks.
This does not assert global host idleness or explain any performance spike.
Parent was explicitly notified after final completion; no additional benchmark
is planned in this lane, allowing its ON rerun without our benchmark overlap.

Remaining: parent HOTFIX1 repeat-performance qualification and install-runner
staging, exact NFQ2+SLOTCLEAR1 composition, current-main/Linux CI/full suite,
uncertain-wire recovery, legacy identity/drain policy, delayed feedback and
simultaneous restart orderings, and live OMS qualification. SLOTCLEAR1 #1113
head d68d25e0bf8cca33d6c22da39fa550b5977b5da3 was read only; source-level cap
policies align, but shared producer/cancel lifecycle needs a real composition.
See SLOTCLEAR_COMPATIBILITY_READONLY.md. No readiness or deployment claim.

Reproducible commands, exact flags and all disclosed stubs are in PROOF_SCOPE.md
and run environment.json; each label preserves its own outcome exclusively.
To rerun the exact final freeze in a fresh proof checkout, start at a5479b42,
use its src first on PYTHONPATH and run the same runner benchmark command with
a new label. Final delivery adds receipts only; source/tests/runner are unchanged.
