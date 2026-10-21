# SLOTCAP-SEED-RESTORE-20261021: unresolved restore must retain slots

Source: Claude TOP19:19:36, main19:26:49 and human urgent SOURCE correction.
Queue then newest TOP read before assessment. Priority: immediate sole lane sourcefix.
Owner: Maxwell source; main queue/status/LATEST/runtime; Claude focused repeat.
Parentdc52279817d4fa2215edd12a4cc9268cf032e513 remains frozen RED in its WT.
WT/private/tmp/mai-tai-slotcap-unresolved-restore-hold-20261021.
Branchcodex/slotcap-unresolved-restore-hold-20261021.
Status: corrective source/controls authored; ALL new-head execution UNRUN.
Next action: minimal source fix plus additive controls; commit/bundle and immediately
report exact head/counts/WT/handoff/UNRUN, then reviewer repeat. No worker runtime.
Completion criterion: author packet for review, not acceptance/GREEN/GO.

Main-bound external JUnit: bf21196782ca6373de95542b979ada62bf0978443be525b6c54c34e2c0c22c28,
2228total/2164P1F0E63S; exact failure new missing-identity-cache sibling. No rerun.
Original durable-cache-present ==123 control passed in that external repeat.

## Actual cause

configure_flip_entry_ownership installs None in _restored_flip_owners for an active
census identity whose owner record is absent/mismatched. _restore_flip_owner popped
that marker, read no installed fanout identity and set phase UNKNOWN/id0. It thereby
discarded the provenance distinguishing unresolved durable restore from a benign
empty UNKNOWN state.

apply_flip_position_book -> _apply_flip_position_evidence -> _recover_unknown_flip_owner
then saw id0/no rows/no fills/no position IDs and called _clear_flip_owner_memory,
changing UNKNOWN to idle via reason empty_owner_state_cleared. The next GAP-RESUME
_retry_slotclear_deferred_sell -> shared cap release found an idle owner and cleared
both consumed bits. So the immediate slot-release conjunction was not itself
missing a phase check; UNKNOWN had already been incorrectly erased by the poll.

The earlier fixture-only assessment correctly located missing fanout setup in its
specific ==123 case, but did not establish this zero-ID recovery path was safe.
This newly exercised sibling requires a production correction, not a test waiver.

## Proposed source and impact

Keep a None marker in the EXISTING _restored_flip_owners pending-restore map when
restore has no usable installed identity. Do not invent an opportunity from a
census or add arbitrary SymbolState fields. _recover_unknown_flip_owner must keep
UNKNOWN/id0 when that pending restore marker exists, even with fresh empty books
or terminal evidence for an unrelated identity. It must not call the benign empty
owner reset. Existing positive-identity native closure/reconciliation stays on its
original path; normal successful restore still consumes its pending record.

No global restoration flag is weakened. A pending unresolved restore is cleared
by subsequent explicit restore configuration, not by a flat position read, quote,
SELL, GAP-RESUME or session roll. Reconfiguration validity remains subject to the
existing store contract; the valid-binding control verifies that path. No general
ban on the pre-existing empty UNKNOWN recovery: when there is no pending durable
restore marker, the original empty-owner branch remains unchanged. Existing
test_empty_unknown_owner_state_clears_and_allows_a_first_rest stays byte-identical.

Original two sibling tests and all64 application controls remain unchanged. New
controls exercise repeated flat reads, unrelated terminal identity, live SELL
owner handling, 04/session roll, the benign no-restore empty UNKNOWN path and
explicit valid-owner reconfiguration preserving the actual durable identity.
No assertion relaxations/fixture toggles to hide the unsafe SOURCE direction.

Original OLOX/prep/restart04 remains additive: prep head3b2411bce815392162b3170928037a70ec13c87f
in its own WT, missing actual26OHLCV/seed/fresh owner/line/gap operands and explicit
BUY-bar timing retained. No PG/provider/export/replay work in this correction.
CDCE/shared adapters/queue/status/LATEST untouched. No tests/import/AST/compile/
app/replay/PG/provider/networkpush/production/performance or quiet-host claim.

## Exact authored packet

Versusdc52: production ONEfile+8/-0, NEWtest ONEfile+75/-0, this sourceimpact note.
Cumulative production versus556 +71/-4. All64 original intended application cases
preserved unchanged;6new =70manually counted intended cases, NOT collected/run.
Both required sibling controls remain byte-identical to dc52 (whole original
test_slotcap_real_owner_state.py SHA497c6bedbc409b9131c85d1f554414ec10c618102cd41dc43b9b07495d53f8ca).
No modification of existing benign-empty-owner baseline test or other original tests.

Changed source sites: _restore_flip_owner :1246-1249 retains unresolvedNone marker;
_recover_unknown_flip_owner :2287-2289 refuses the generic empty-owner shortcut
for that pending restore. Existing owner phase/readability/position/inflight gates,
original SELL provenance, slotclear and dispatch code are unchanged in this delta.
No schema/new SymbolState field, fabricated ID, defaultFalse, assertion waiver or
new native connection. Existing pending-map type already supportsNone; soft-rest
candidate enumeration already filtersNone. Positive-identity exact terminal/native
recovery remains on its existing path. Affected callers include position poll,
watchlist state restore, SELL owner handling and session roll; new controls cover
the source-supported paths but do NOT establish actual restart04 qualification.

SHA256 production:
ef8ee814e49a0afa348ed2e3aec3917df5cea8d44b2f35af87cb1a9345aee5fc
SHA256 new control module:
aa687bc35e7dd9cd7368ea8a307f4ab8b48d2da87b347e5cd8b01fd2c8dfd484

Reviewer must repeat BOTHsiblings, new module and same selection on exact newhead;
main later owns unfilteredfull0NEWvs556/originalOLOX/restart04/composedALLON/native
qualification and reviewerGO. Static diff inspection is NOT PASS. New committed
head/counts/bundle will be announced immediately; worker never posts LATEST or
shared queue/status and does not duplicate reviewer execution. Earlier19:07 redelivery
ofdc52 was not accepted as green: retain actual externalRED and this corrective delta.
