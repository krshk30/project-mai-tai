# C12309 lifecycle and sequential-arm source checkpoint

SOURCE/CONTROL AUTHORING ONLY. All new controls UNRUN by author. No PG,
provider, replay, import/compile/test or production action was performed.

## Binding and Authority

Parent is exact boot B `0c7c6c6913ea89e9f1c2756a8e71193709b87f08`.
APP/sql/ops/INI stay byte-identical to that parent/F. This is NOT the corrected
Galileo APP carry. C1 23:09 approves the lifecycle and sequential restart design;
23:10 records failed APP0f2 FIRST18=14P4F. **APP carry remains PENDING** until
actual corrected APP/control/source review. No oldB/F receipt is relabelled.

No historical modules, completed controls, old roots or Main queue were edited.
New tools root is `/private/tmp/mai-tai-l12-c12309-publish-lifecycle-20261024`.

## Existing Driver Changes

`tools/l12_recorded_boot_driver.py` adds explicit phase0 five-second lifecycle
events from modeled04:00 through the last reviewed event. Caller supplies
`maximum_publish_boundaries` and the actual C12309 ruling SHA. A16-hour arm has
11521 boundaries including04:00 and20:00. The combined100000-event bound remains;
there is no automatic budget growth. Original event dictionaries/order/clocks
are retained; same-clock mutation-after-observation refuses, not sort-repairs.

At each boundary, source-owned scheduler settlement of the prior same-clock
membership/history/owner/line callbacks precedes the real hold check. The
delegate does not add a history read, line rebuild, owner drain or admission call
to force progress. Pending work remains visible to the APP ordering fence.
Exactly one real class-bound `_cw_boot_hold_check` is invoked, with copy guards
before/after; primitive pre/post fields and exceptions are retained. Wrong clock,
duplicate/foreign boundary, wrapped method, guard loss or call failure stops.

Scope is `ACTUAL_HOLD_CHECK_AT_MODELED_PUBLISH_BOUNDARY`. It is NOT full publisher,
Redis delivery, reportable-state DB read, modeled monotonic warmup timeout,
performance or install proof. Actual APP `STATE_PUBLISH_INTERVAL_SECONDS` must
remain5. Samples never invoke the delegate. Existing real non-owner wait_for
deadlines and clock ports are unchanged. No callback sets readiness or admission.

The actual controlled service now requires this boundary budget. It also requires
the exact active boot arm and refuses reuse of a stopped/consumed service,
scanner, driver or tracker. Each arm needs fresh actual objects; no old instance
is reset to simulate restart. Global cleanup remains the existing helper's path.

## Existing Lease Extension, No New Wrapper

`tools/l12_nonce_copy_lease.py` retains original locks/target/expiry/source/census,
O_EXCL stage marker, consumed-copy marker, journal and session accounting.
Main creates/starts ONE boot lease exactly as before. The additional methods are:

1. `begin_boot_arm('EMPTY_FLAT', preimage_sha256=..., readback_guard=...)`.
2. Existing `replay_service_boot(...)` with arm1 packet/new objects, then actual
   independent post-readback and `finish_boot_arm`.
3. `begin_boot_arm('ONE_WORKING_BUY', preimage_sha256=ARM1_POST_SHA, ...)`.
4. Existing helper with arm2 packet and fresh case/service/scanner/driver/tracker,
   then independent post-readback and `finish_boot_arm`.
5. One `finish_postcopy` for a result binding BOTH outcomes; close once.

`finish_boot_arm` requires actual post-readback/result/fixed allowed-mutation
review SHAs and a separately reviewed Main `readback_guard`. It DOES NOT infer
acceptance from shape/hash or successful lifecycle return. Begin guard receives
`(target, variant, preimage_sha256)`; finish guard receives
`(target, active_arm_dict, postimage_sha256, result_sha256, mutation_review_sha256)`.
Main must bind actual artifact bytes/readbacks/source/settings/target epoch,
allowed04roll/tick/store mutations, and arm2 owner conservation/no duplicate BUY/
working-recognized hold or refusal/never resend. Tests use UNIT guards only.

Any failed arm/guard stops progression. Main calls `fail_boot_arm()` on ANY
caller/preflight/readback exception, including failures before the helper's
active service try block, then records FAILED/UNKNOWN and preserves all evidence.
Inside the active service block errors already mark the arm failed. No retry,
rewind, reseed, new stage/copy or tombstone removal. Both arms consume the same
finite session budget and real expiry. Arm2 is explicitly sequential, not an
identical-original-close replay. Original122 must precede these boot writes.

## New Control Selection

Two new whole modules, intended39 expanded controls, all UNRUN:

- `tests/unit/test_l12_c12309_publish_control.py`: intended25; phase0/grid/order,
  no observation delegation, source pin/clock/method integrity, finite budgets,
  once-only behavior, guard/exception propagation, real frozen-F hold kernel,
  existing owner-scheduler settlement. Frozen-F test asserts still held, NOT
  that a corrected APP has qualified.
- `tests/unit/test_l12_c12309_sequential_arms.py`: intended14; real private
  file/journal lease fixture (NO PG), same post/pre hash, order/no reset,
  failed-owner/mutation/no-resend acceptance, one-arm incompleteness, shared
  session budget and driver reuse. This is not actual owner/native readback.

Main may use the existing memory/network/process DENY fence with plugin autoload
OFF and explicit pytest-asyncio, fresh XML/raw/temp, complete pre/post source pins.
No author execution and no repeat0c7-33 or older controls. Example selector ONLY:

```
tests/unit/test_l12_c12309_publish_control.py
tests/unit/test_l12_c12309_sequential_arms.py
```

## Remaining Gates and Resume

Whole source review, first new controls, corrected-APP ordering/full/originalLNKS
and36/03:40/rate, exact new consumer carry, Main finite actual lease/role/session/
owner/causal/source/expiry claims, actual34boot and arm2 working-BUY/owner readbacks
remain OPEN. No source token or UNIT result is actual approval.

122 readset assessment was completed privately before preemption:
`/private/tmp/OCT24-L12-C12249-INDEPENDENT-ASSESSMENT-CzsAZ5/predicate-readsets.txt`
SHA `cfc020a6d8d5d3b2bcfd910099ffe297a821f8d2362186f6ab302ebe692c9076`.
a83/actual16c71776/26902c5b remain unchanged:117 mathematical fragments/5none,
all122 full historical packets unbound; case-specific readset does NOT waive
full-packet/readback acceptance. Resume that lane after this critical build.
