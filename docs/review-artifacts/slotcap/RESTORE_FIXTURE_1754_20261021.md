# SLOTCAP-SEED-RESTORE-20261021: 17:54 restore fixture correction

Source: Claude TOP17:54:19 + human priority redirect; queue and TOP read first.
Priority: sole lane immediate correction; all OLOX/state/restart04 scope additive.
Owner: Maxwell author; main queue/status/LATEST/runtime; reviewer repeat not duplicated.
Parent: frozen35dfe921b1db34388e46899188cedffd4b63c4c6.
WT: /private/tmp/mai-tai-slotcap-restore-fixture-20261021.
Branch: codex/slotcap-restore-fixture-20261021.
Status: NEW fixture corrected, production unchanged; all new-head controls UNRUN.
Next action: reviewer same-selection repeat/new-control; original OLOX prep resumes.
Completion criterion: exact local head/bundle/cause/impact for reviewer; NOT GREEN/GO.

External result (not rerun): Claude35df same-selection2163P1F. The remaining NEW
test asserted identity123 but observed0 while UNKNOWN held. File-only source
assessment finds incomplete fixture startup, not dropped installed durable state.

## Exact cause and real restore example

src/project_mai_tai/services/schwab_1m_v2_bot.py:1412 restores fanout first via
_configure_fanout_identity_store; :1413 then passes active_segments into owner setup.
At :926 the first method calls strategy.configure_fanout_identity_persistence with
the actual store's restored_segments, before returning the census.

src/project_mai_tai/strategy_core/schwab_1m_v2.py:1062 stores durable identities in
_restored_fanout_segment_ids. _restore_flip_owner at :1242 reads that installed
cache (then actual state.fanout_segment_id) when a matching owner is absent or
unreadable, sets phase UNKNOWN and refuses entry. active_segments in owner setup
is the consistency census, not the identity-cache installer. A caller skipping the
first configuration leaves cache/state empty, hence identity0 and UNKNOWN.

Concrete established restore fixture: tests/unit/test_slotclear1_soft_rest_boot.py
:47-52 configures fanout with recorded AIXI opportunity1791462062227 FIRST, then
owner restore with matching active_segments. No real native call is involved in
that source example and no example was executed by this worker.

The failing NEW test at tests/unit/test_slotcap_real_owner_state.py:64 skipped
fanout configuration. Correction installs that exact missing startup prerequisite
before owner setup, asserts installed cache123, and KEEPS all original ==123,
UNKNOWN-phase and held-cap assertions. No xfail/removal/relaxed assertion/default.
Additional control preserves the incomplete-caller behavior: without installed
cache the owner ID remains0 but phase UNKNOWN and cap stays held, never invent123
or convert UNKNOWN to idle. Both complete and incomplete startup cases are explicit.

## Impact and preserved resume point

Production delta0; no ownership/restore/safety/persistence change. Original63
controls preserved plus1 incomplete-caller control =64 manually counted intended
cases, ALL NEW HEAD UNRUN. New test-only diff and this cause note are the only
changed paths versus35df. Re-push means local commit/bundle only, no network push.

Original OLOX analysis survives at separate WT
/private/tmp/mai-tai-slotcap-olox-input-prep-20261021, untracked document
docs/review-artifacts/slotcap/ORIGINAL_WINDOW_BINDINGS_20261021.md already written.
Resume: finish bounded COPY input adapter and service-bar harness/source controls;
26 actual OHLCV bars/seed and historical full owner/readability/gap/line operands
still missing. Fixed archive market_capture_bars COPY is EMPTY; strategy_bar_history
data excluded. Found32checkpoint+32bar-close snapshots; watch1792602058349;
SELL1792602240000; resume1792602540000; required BUY bar1792603740000 logged
17:30:02.582Z. Never infer fresh flat owner from20:37 account state or snapshot gaps.
No source changes to35df worktree; no new tests/import/AST/app/replay/PG/provider/
runtime; baseline full remains externally active per latest instructions, not polled.

All original OLOX17:05..17:30, GAP-RESUME release and17:29BAR waiting-BUY, restart04,
full0NEWvs556/composedALLON/sourceimpact/reviewerGO remain required and unproven.
Source-complete fixture result is reported now, then input-prep continues in its
own WT. Main records result/report in same stable queueID; worker does not edit it.
