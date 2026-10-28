# C11540 native refresh priority: exact d16 successor, source only

Base d16c7ffe2dacafd7febd7661a75194e85adbde8a. This branch changes five
APP files, affected lifecycle/SDK-fixture controls, one new control module, and
this handoff. No settings, economic guards, migration, provider endpoint, native
parser/completeness or admission-age change. Author ran no imports/tests/PG.
Main's frozen d16/g1 and all original failures remain untouched.

## Actual callgraph and bounds

OLD d16: sync_broker_state awaits begin_pass (Redis), original
_sync_broker_state_pass, finish_pass (two sequential native account reads, local
census, Redis). Original position/order result is delayed by L12 completion.

Successor: original _sync_broker_state_pass and wbquiet_shadow.finish complete
FIRST. Only then schedule_refresh creates one retained, context-isolated task;
the sync caller never awaits L12 Redis, native HTTP or local census. The original
sync counter stays positive until publication/diagnostics complete, including
overlapping sync calls. Failed/cancelled original passes do not schedule L12.

schedule_refresh uses real monotonic start-to-start >=30s, including failures,
and never overlaps a live worker. Successful books stay in the existing Redis
replacement packet with ORIGINAL acquisition times, not restamped cache ages.
begin_pass still invalidates the previous generation before new collection.
The 20,000ms bound/exact-boundary refusal remains entirely unchanged. There is
no on-demand signal in the existing publisher protocol: this increment chooses
the approved bounded background option, not a new per-tick/Redis request path.

Native requests recheck the original-sync priority callback before each HTTP
attempt and before local census. A request already in flight cannot be un-sent;
this is not preemption of a blocking SDK delegate. Main g4 must measure actual
resource contention; moving an await is not proof of equal sync freshness.

Webull claim_background shares the ACTUAL existing process host/app-key budget:
no waiting-queue insertion; reject any pending ordinary queue, live terminal
proof, reserved foreground read, existing endpoint permit or backoff. Its separate
cap admits at most ONE attempt per endpoint/30s across aliases and consumes ZERO
ordinary permits, leaving BOTH normal two-second permits available. Existing ordinary claim/strict/detail ceilings are
unchanged. Position/status/cancel-status/OCO-fill blocking reads reserve priority
before their delegate starts, retaining it through cancelled waiters until the
actual delegate ends. A missing legacy fixture budget uses the same host/app-key
shared budget; native collection itself has NO fallback from background claim.

Webull upper bound in the two-account publisher: one list-open and one positions
attempt per30s = at most four attempts/minute at steady state. A second positions
page within that window is refused: the entire book stays UNKNOWN, never partial
COMPLETE. Nonempty Webull open-item proof remains UNKNOWN as before. Schwab keeps
all existing 32 order-query/4s/row/page/window bounds; at most two acquisitions
per minute, each with account-map + positions + <=32 order requests. These are
source ceilings, NOT actual g1 counts, measured vendor quotas or a rate PASS.

shutdown_refresh closes new scheduling, cancels/joins its owned worker, and gives
retained shielded native tasks one bounded5s drain. Unfinished delegates remain
retained and explicitly native_join_UNKNOWN; no false thread-termination claim.
Existing OMS tick/protection/control shutdown runs before that native drain.

## Material branch-scope finding: ordinary entry is NOT proven neutral

Unchanged core _strict_first_rest_admitted calls entry_decision FIRST whenever
the flag is ON, before reconstructed_first_admitted and before legacy gates.
Core's other entry call and brokerdecides1_entry.before_entry_publish also assess
the same memory. Therefore a book older than20s can hold an ordinary v2 BUY, not
only the cap-only stale-owner exception. With a30s refresh there can be a stale
interval even after a successful acquisition. No rule narrowing was authorized
or implemented here. Main/C1 must disposition this factual mismatch with the
statement 'only delays L12 admission, never a normal trade.' Flag-OFF ordinary
entry is unaffected. New real-method CONTROLLED countercases expose both paths.

## Focused FIRST selection for Main review/claim (all author UNRUN)

18 new concurrency/budget/completeness/age/cleanup controls plus2 branch-scope
countercases =20 in tests/unit/test_l12_native_refresh_priority.py.

Three changed actual OMS lifecycle variants (none/sync/schedule):
tests/unit/test_brokerdecides1_lifecycle.py::test_actual_sync_lifecycle_retains_diagnostics_and_existing_result

SDK boundary fixture now exposes claim_background, not ordinary claim. Affected
source-bound selectors (12 intended):
test_actual_collector_uses_pinned_open_sdk_empty_envelope_and_private_positions_pages (1)
test_actual_collector_never_converts_protocol_failure_to_empty_book (9)
test_actual_collector_respects_existing_query_budget_without_http (1)
test_missing_positions_sdk_cursor_support_is_unknown_not_first_page_success (1)
in tests/unit/test_brokerdecides1_webull_sdk_boundary.py. Its injected unlimited
CONTROLLED budget tests grammar only; the NEW actual QueryBudget adverse control
separately refuses the second page. Do not claim those fixtures prove production
pagination has a permit. Other previously qualified controls need not repeat.
The separate cap is an application policy, not a separate vendor quota: actual
Webull physical rate/429 impact still requires Main's g1/g4 observations.

Main g1 frozen9c/d16 measurement and paired MOM controls remain separate claims.
Changed candidate requires g4/indexed rates/final full/reseal and real-entry
impact. Source controls alone qualify none of those. No production authority.

## Preempted L12 work (preserved, resumes after this source packet)

Root /private/tmp/mai-tai-l12-c11509-reconstructed-world-20261025 at
dd4576ba140ce3af8b9f6a39b80f803d7799a18e (15:18:08 ET), with dirty branch-adapter
forward/typed-clone edits and two untracked control modules intact. Its25 boundary
controls remain UNRUN. The15:53-16:08 reconstruction ETA is suspended by urgent
C11540 preemption, not silently shifted or claimed complete. Same owner resumes
real OLD forward/predecision/same-state clone + finite122 invocation next, then
additive native/whole-Alembic/fresh-copy/manual20/FK/Fridayboot recipes. No scope
or original-case acceptance was dropped.
