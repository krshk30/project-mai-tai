# C1 16:12: L12 admission is an OLD stale-owner/cap-only exception

Source-only successor of f37f602c5890e930399f032234667b12110f58a4.
No tests/imports/compilation/PG/provider/runtime executed by author.
Reconstruction 708b4f22 and its 79 PASS / 23 FAIL receipt remain separate.

## Production delta

- `brokerdecides1_entry.exception_scope` reads existing OLD cap/owner state only,
  not native memory. Ordinary entry_decision is outside the exception and does
  not assess a snapshot. prepare_send returns before accounts/journal/stamps/
  reserve. Existing legacy first-rest guard body is unchanged.
- OLD cap provenance is both taken slots plus seed-cap watch marker. OLD unknown
  owner requires an existing identity. SELL-only idle/contradictory older identity
  is not generalized into a BUY gate. Successful canonical owner retirement or
  cap exception records the exact SHORT segment for final publication checks;
  this does not cache permission. Every exception attempt reassesses current
  memory, and a different segment does not inherit that marker.
- Ordinary track_removed_wait_dispatch keeps its existing attempt payload and
  accepts its legacy committed writer result. Only an actual exception adds
  brokerdecides_exception=True as an internal caller marker and requires a
  fresh-journal capability. Store strips that internal marker before the existing
  strict eight-field authority writer; durable proof schema is unchanged. The
  existing RemovedWaitStore uses its original insert/commit path otherwise.
- Actual service publication boundary is absent for ordinary entry. Its final
  synchronous callback rechecks scope AFTER L11's await; a newly appearing
  exception without fresh authority refuses before any reserve or XADD.
  Existing exception proof/commit-before-release and post-XADD UNKNOWN semantics
  are unchanged. No network or DB work was added to the final callback.
- No native cache timestamps, 20s boundary, acquisition cadence, provider budget,
  existing sync ordering, index migration, L11 guard or economic rules changed.
  f37 g2/g3 background/priority source remains inherited without modification.

## Focused controls for Main review / first execution

New module `tests/unit/test_l12_c11612_exception_scope.py`: 27 intended items
(author UNRUN). Real strategy first-rest, actual service callback/emitter, actual
store private SQLite, cap admission, ordered owner retirement, scope expiry,
unknown/economic negatives, ordinary legacy dispatch and fresh authority refusal.
Native operands are explicitly CONTROLLED, not historical/provider evidence.

Affected existing selectors (source changes, not unchanged-pass repeats):

- `test_l12_native_refresh_priority.py::test_actual_ordinary_first_rest_skips_L12_snapshot_outside_exception`
  two parameters now require both flag settings to admit ordinary entry.
- `test_brokerdecides1_publication_service_callbacks.py::test_callback_factory_failclosed_without_profile_or_storage`
  flag-OFF service boundary is now None; exception storage UNKNOWN still refuses.
- entry_hooks fixture now explicitly enables OLD owner logic for its existing
  unknown-owner identity, so those controls continue exercising an exception.
- prewire_protocol real journal fixture explicitly marks its exception attempt;
  dependent capability/cleanup controls continue using actual fresh authority.

Main should qualify affected connected controls and final combined full/cases;
this source checkpoint is not runtime acceptance or install permission.

## Preserved remaining work

708's 23 clone-generated failures concern lazily absent strategy fields; preserve
ABSENT versus PRESENT, not obsolete-field renaming or fabricated restore state.
That additive codec/readset fix follows this APP checkpoint, then concrete
WORX /103 OLD session -> predecision seal -> SAME OLD/NEW reconstructed state.
Exact startup <=08:00Z row membership and actual source clock/event/handler ports
must be concrete or individually named missing, not uppercase generic ports.
Manual20 positive projection construction remains separately reviewed: actual
FE1 copy receipt reports zero evidence and zero retirements, not positive FK proof.
122 / 34 / Friday boot / manual20 / indexed rate / impact and final full remain OPEN.
