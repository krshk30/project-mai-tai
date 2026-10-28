# Owned shared-sync fixture drain and isolation

Source-only correction; every changed/new control below is UNRUN.

## Ownership and evidence

- Parent: 7ed6c11c1aecf8061be22b4cfb202685435ca7cb.
- Worktree: /private/tmp/mai-tai-mom-orb-sync-fixture-drain-20261025.
- Branch: codex/mom-orb-sync-fixture-drain-20261025.
- C1 16:49 ruling: real APP lifecycle defect AND fixture isolation/drain required.
  Arendt remains sole APP lifecycle author. This change touches no APP/SQL file.
- Retained crash: /private/tmp/OCT25-F37F-SYNC-PRIORITY-FINAL-FULL-FIRST-20261025.log.
  Worker stack includes local_nonentry_activity:124, local_evidence:174,
  finish_pass delegate:321 and service._run_db:1130. Main-thread stack includes
  StaticPool.dispose and this fixture's old engine.dispose:30. This establishes
  concurrent SQL execution and disposal, not merely an unsafe-fixture hypothesis.
- The log's first `F` is immediately followed by `Fatal Python error`; there is
  no rendered first-test assertion traceback. Its exact exception remains UNKNOWN.
  Do not classify the first assertion as fixed from this static review.
- Independently, the new sync schedules native publication after public sync
  returns. The old test immediately reads the publication without yielding or
  joining. This is a source-demonstrated observation-order defect; it is not a
  recovered actual exception. All original economic/native/refusal assertions stay.

## Fixture changes and call boundaries

The existing fixture name/selectors stay, but its factory now owns a fresh private
SQLite file with NullPool, not one StaticPool connection shared by concurrent SQL.
Each checkout gets an independent physical connection. The file is reserved 0600
with O_EXCL/O_NOFOLLOW; no ambient factory/cache/shared engine is adopted.

The 12 actual-public-sync cases register their real OmsRiskService in an owned
async context. After real sync they shield-await its actual native refresh task
before snapshot/economic assertions. No production sync/store/admission/permission
implementation is replaced, and no timeout or economic threshold changes.

On every body exit, including assertion failure, the fixture awaits the actual
refresh task, calls real shutdown_refresh, then joins its FUNCTION-scoped test
loop's default executor. Executor join covers a physical to_thread DB delegate
whose asyncio waiter was already cancelled. Task cancellation alone is not join.
Do not reuse this helper with a module/session/shared loop.

Connection checkout/checkin tracking and a drained marker guard disposal. A live
connection or unjoined executor leaves the owned engine retained and raises;
cleanup errors are surfaced, not accepted as a clean stop. An original test-body
exception is re-raised, while the outer DB fixture can additionally report cleanup
failure. No foreign connection, task, process or cluster is closed.

This test fixture's executor join is NOT evidence that production shutdown joins
its DB delegates. Arendt's independently reviewed APP change and actual shutdown /
restart controls remain required before any reseal/full claim.

## Exact first selections proposed to Main

```text
tests/unit/test_mom_orb_shared_sync_compatibility.py
tests/unit/test_mom_orb_sync_fixture_lifecycle.py
```

Source inventory: existing 12 public-sync cases changed by observation/drain and
isolation; existing 3 identity negatives changed only through the factory; 5 new
mechanical lifecycle controls = 20 proposed distinct cases. Main's actual collection
is authoritative. Do not repeat unrelated prior 49/11/14 passed selections.

The five new controls cover independent simultaneous SQLite connections; retaining
an engine while a connection is live; waiting for actual publication; a cancelled
waiter with a still-running real SQL thread; and preserving an original assertion
through successful actual worker join and safe disposal. The simple service
objects in mechanical controls are not claimed as production lifecycle proof.

Main invocation requirements: fresh private owned basetemp, allow only the exact
`mom-orb-shared-sync.sqlite` basename under each case's tmp_path, exclusive 0600
reservation and inode binding; no URI/PG/other DB paths or network relaxation.
The real constructor's previously reviewed source-bound local-hostname allowance
remains Main-owned. Normal GC/INFO/plugins and all existing asserts remain.
No author execution/import/AST/compile/test/PG/provider was performed.

## Preserved additive work

After this urgent correction, resume whole-83 MOM/ORB common-input day consumer
using Gibbs's delivered pinned feed API (no historical event-join building).
G1/G4 b1fa source remains diagnostic; its indexed/pending-exit draft is preserved
separately and is not G4 qualification. Final APP binding must be explicit after
Arendt's cohesive lifecycle/scope successor, not an automatic descendant pin.
