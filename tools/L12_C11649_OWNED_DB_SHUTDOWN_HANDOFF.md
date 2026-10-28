# f37 crash: actual L12 DB worker ownership through shutdown

Source-only successor of 78770cde6e1c3b8e86000fab61cc4a3c62ff8de1.
f37 SIGSEGV -11 / full43% / StaticPool teardown raw retained by Main. This is a
source-confirmed cancellation defect, not proof that every crash cause is known.
No author imports/tests/DB/provider/signals/runtime. No underlying timeout growth.

## Actual cause and correction

Original finish_pass awaited service._run_db directly. _run_db awaits to_thread;
cancelling its coroutine does not stop the SQLAlchemy unit/session inside that
thread. f37 shutdown tracked SDK tasks only and could return while local_evidence
was still executing. Fixture engine.dispose could therefore race its worker.

The new L12-owned DB task wraps the ACTUAL _run_db coroutine, shields that task
from its waiter cancellation, and retains it until normal/error completion. That
coroutine only completes after the worker's session context exits. No generic
OMS _run_db API/body changes. The consumer's read_terminal to_thread is owned in
the same way; both paths retain UNKNOWN if the waiter is cancelled. Orphan work
blocks another acquisition/delegate rather than spawning overlapping census.

Public begin_pass, finish_pass and refresh_consumer register their actual caller
task for lifecycle shutdown. Closing stops new passes. shutdown_refresh cancels
only waiters / background refresh, NEVER SDK/DB delegate tasks, then joins those
actual delegates. C1 16:49 supersedes the old five-second waiter drain: there is
NO False return to ignore and NO disposal permission before actual worker exit.
Statement timeout750ms / lock250ms and existing native timeouts are unchanged;
this does not add a higher query/provider timeout or terminate a thread.

Shutdown's own cancellation is deferred until the join finishes, then re-raised.
An externally cancelled DB delegate is retained as UNKNOWN and raises
L12_delegate_externally_cancelled_join_UNKNOWN_NO_DISPOSE, never joined=True.
Owners must NOT cancel delegate tasks or dispose an engine on this refusal.
These are service-owned tasks, not part of the ordinary task cancellation set.
OMS already calls shutdown_refresh after old task shutdown; strategy now also
calls it after cancelling/gathering its poll tasks and before later teardown.
Local consumer memory becomes UNKNOWN on closing. An interrupted begin/census
cannot emit an orphan COMPLETE packet after shutdown, and restart retains UNKNOWN.

## Main / Cicero fixture seam

Actual test_mom_orb_shared_sync_compatibility fixture is Cicero's sole source.
This checkpoint does NOT edit it. Its owner must await shutdown_refresh(service)
while the event loop is alive BEFORE engine.dispose; awaiting only the public
sync result or cancelling a refresh waiter is insufficient. No timeouts/guard
assertions should be weakened. Awaiting join may expose actual SQL timeout or
worker failure; preserve those failures, never dispose a still-active session.

## Controls (author UNRUN)

10 new items in tests/unit/test_l12_owned_db_shutdown.py. Actual OmsRiskService
_run_db, real SQLAlchemy Session/StaticPool and SELECT1 under controlled thread
barriers: cancellation during census, shutdown-owned finish cancellation,
shutdown cancellation, consumer terminal read, DB failure cleanup, no overlap,
idempotence/new-work refusal, and begin-publication cancellation. First census
control also verifies UNKNOWN readback on restarted local memory. Two extra
CONTROLLED cases ensure externally cancelled delegates BEFORE or DURING the join
never count as actual thread exit; no engine/disposal occurs in those cases.
No benchmarks,
native/PG/full/boot or original122 qualification is inferred from these controls.

One changed previously qualified selector:
test_l12_native_refresh_priority.py::test_shutdown_cannot_return_before_actual_native_delegate_finishes
replaces the old test that accepted False after a five-second waiter timeout.
No unrelated old controls requested again. Main owns source review/first claims,
fixture integration, new final combined reseal/full and actual g1/g4 measurements.

## Preserved work

Ordinary scope / same-segment correction is inherited exactly78770cde6. f37's
30s acquisition/20s evidence age/foreground priority are unchanged. No model,
migration, native endpoint, permission or economic-rule changes.
708 absence successor0f1bf1229 remains separate/frozen (11new/23changed UNRUN).
WORX103 concrete input port list / Gibbs common day feed remains preempted, not
complete: 207unique closed bars11:00-14:26UTC, original log13:51-14:29UTC, no
complete quote/membership stream in the case record. 122 /34 /manual20/FK /
Friday boot / original impact / final full remain OPEN.
