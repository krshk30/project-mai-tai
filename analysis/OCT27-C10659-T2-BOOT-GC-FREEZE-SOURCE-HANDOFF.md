# C10659 T2(b): minimal boot-release GC freeze SOURCE proposal

REVIEW FIRST. No author imports/tests/collection/build/runtime/PG/providers.
Not integration, installed APP, performance qualification or OPERATOR GO.

## Identity and scope

Isolated ordinary local clone (not worktree API):
`/private/tmp/oct27-l11-t2-boot-gc-freeze-source-20261027`.
Baseline ecd2e90e799009a9750a379b2dfa182bd613d41b; frozen ecd2 root untouched.
Only APP change is service import gc, one process-wide lock/attempt latch/helper,
and helper call immediately AFTER the existing actual BOOT-HOLD released log.
No state/epoch/pins/strategy/order/provider/STOP/country/send math changed.
No retention bound: allocation owner/growth evidence T2(a) is still pending.

## Release-path audit

Original service `_cw_boot_hold_check`2083-2146 has ONE release site2131-2136.
The helper is ONLY there; no constructor, scanner, timeout, restoration latch or
entry-readiness path calls it early. Restoration latch5433 alone is not release.

- Safety disabled: unchanged immediate return, no freeze.
- Restoration incomplete: unchanged re-hold/rate-limited warning, no freeze.
- Same call's actual retry completes restoration: normal no-danger release may
  then freeze; no fabricated completion/ready assignment introduced.
- Restored but dangerous: unchanged hold/ERROR, no freeze.
- Restored/safe but already open: unchanged no new release log, no freeze.
- Actual restored/safe held-to-open transition: original assignment and release
  log FIRST, then freeze once and `[V2-GC-FREEZE] frozen=<actual count>`.
- Repeated checks, later re-hold/re-release, multiple service instances in ONE
  process: process latch prevents any second freeze attempt. Fresh process may
  freeze again after its own real release. No daily/session reset.

The latch is claimed UNDER a threading.Lock before calling freeze, so failures
cannot lead to repeated freezes on later release. Exceptions from freeze or
count propagate visibly; no silent pass/retry. The pre-existing release state/log
has already occurred; the proposal does NOT roll it back or weaken safety gates.
A failed attempt means no successful freeze proof; log/gate review must not treat
the latch as evidence that freezing succeeded. Main/C1 must accept this explicit
failure policy or request a reviewed change before execution/adoption.

## Process-wide cyclic reclamation risk (NOT risk-free)

The permanent generation affects ALL tracked objects in the process at this
point, including non-L11 objects, mutable caches, other-thread objects, and
potentially unreachable startup cycles. It is not a selected immutable-cache
freeze and not evidence those objects are all truly long-lived.
Normal reference-count reclamation continues for acyclic unreachable objects;
automatic cyclic collection no longer scans the frozen generation. Cycles made
entirely from frozen objects can remain unreclaimed after external references
are dropped. Resources reachable only through such cycles/finalizers may remain
retained; reference-graph changes involving frozen objects need lifecycle review.
Post-freeze allocations remain normally tracked/collected: this does NOT bound
new L11/history/diagnostic heap growth or guarantee all-day gen2 latency.

No gc.collect before freeze (forbidden scope), no gc.disable/set_threshold,
unfreeze, periodic thaw or shutdown policy added. No assertion of memory-leak
freedom or production memory budget from once/equality unit tests. Reversing the
process-wide freeze via fresh process would require Main/operator lifecycle
authority; this proposal does not restart a process. Existing freeze count375
in T1b is not a reason to skip the explicitly authorized post-release freeze.

## Proposed focused controls: AUTHOR UNRUN

New `tests/unit/test_v2_boot_gc_freeze.py`: planned16 cases (10-case literal-old-
vs-new release equality matrix +6 focused functions), NOT an actual collection.
Tests mock gc.freeze/count; they do NOT freeze the test runner or alter thresholds.

Equality loads ONLY the exact original method AST from frozen ecd2 service after
whole-file SHA37aee964 verification, not a hand-reconstructed baseline or foreign
APP import. That old callable runs only inside a Main-executed CONTROL; it never
replaces an APP method in the service. Inputs explicitly isolate boot-hold branch
behavior rather than claim native restoration/provider/PG/production acceptance.
Compare release/hold/restoration/log-throttle state, retry/completion calls and
all original guard log messages; only the new GC line is excluded from log equality.

Additional controls: release-log/restoration ordering; repeated checks/re-hold/
second instance once; incomplete/dangerous no premature freeze; freeze/count
failure visible and no retry; AST single release-callsite and no other GC policy.
Real cyclic-memory lifetime behavior is NOT covered by mocked GC controls.
Existing `tests/unit/test_v2_boot_restoration_guard.py` is pertinent Main focused
coverage, not claimed run here; its normal release now reaches real freeze unless
Main explicitly reviews an isolated controlled GC boundary for those functional
controls. Do not silently run a process-wide freeze in an unrelated shared test
interpreter or infer that mocks constitute actual GC-performance proof.

## Required next gates, unchanged

Main/C1 whole diff/risk/failure-policy review FIRST. Main sole focused controls,
normal quiet ALLON60 >=200ev/s and strict MAX<50, later approved micro960, T2(a)
growth-source evidence before ANY retention change. T4 uses changed boot path,
so notify Gibbs/Arendt/Main for the stipulated fixed-head boot re-proof; no author
runtime or independent cross-lane edits. BOLT+AHT smoke/original anchors and
other-case carry only on C1's exact equality/scope disposition. No automatic
ALLON/economics/install pass or waived old failure. T2 target07:34:56 remains.
No install/restart/flags until all original gates and C1 posts OPERATOR GO.
