# EH2RTH1 corrective local packet

Sole R writer Popper by explicit human handoff. Full reviewer15:0x ruling
read. OldPlato3c3debe6 remains frozen/closed; oldpacket/source read only.
New isolated codex/eh2rth1-r-corrective-20261013 worktree uses ec4dc414,
which retains bd7 FALSEFLIP2/RW. Only four frozen R commits replayed onto
that base; no other worker source adopted. H reporting nit explicitly
preempted R, froze separately at823b05c1, then R resumed intact.
Final audit found ec4 did not have bd7 as a Git ancestor even though its
relevant v2/OMS source and flag inventory were already byte-identical. An
ancestry-only merge of the explicitly required installed bd7 changed0 files;
both ec4 and bd7 are now ancestors. Inventory55 controls PASS.

## Five Neighbours

Five tests PASS at b6b9bc05 and FAIL after the full oldR contract on ec4.
They are controlled positive fixture gaps, not evidence of a proven live
primary being dropped. New proof requires broker_order boolean plus exact
generation/segment/slot/slot_id/entry_slot. The old synthetic fixtures supplied
only working+broker_id; two helpers also omitted the broker boolean (affecting
four of the five cases). Completed the positive fixture evidence, retaining
EVERY original test assertion. The shared-identity test uses its actual queued
primary's metadata, not a separately invented identity.

| Existing Test | Gap | Result |
| --- | --- | --- |
| rth_resting_cross_queues_webull_market_leg | local broker flag + complete identity missing | PASS |
| rth_resting_cross_waits_for_the_offset_trigger | same helper | PASS; offset assertion unchanged |
| rth_resting_primary_and_cross_fired_webull_leg_share_identity | exact queued identity absent from controlled broker proof | PASS; shared-identity assertion unchanged |
| fanout_claim_resets_on_position_close | same helper | PASS; claim-reset assertions unchanged |
| no_observation_without_a_live_mirror | broker flag + segment/slot proof absent | PASS; observation/emission assertions unchanged |

Both entire neighbour files52PASS. A real RTH cross without fresh exact proof
IS deliberately refused under the reviewed contract. UNKNOWN/absence cannot
be upgraded by an in-memory broker flag; original no-proof controls unchanged.

## Terminal Feedback

Selected the reviewer's explicit alternative: prove EXISTING feedback, not
infer terminal-zero from absence. Existing HandoffJournal.reconcile_feedback
and SchwabV2Strategy.rpg_handoff_authorization consume exact per-leg feedback,
retire the matching primary generation/quantity and clear the broker boolean
when no sibling quantity remains. Once ownership feedback is applied, the
ordinary _cw_v2_resting_track -> _queue_resting_place queues exactly ONE new
Schwab STOP_LIMIT in the SAME economic slot, with a NEW generation. Three
repeated ordinary passes produce no second primary. Source is unchanged.

Controls cover cancellation and rejection, normal same-slot replacement,
status-only cancellation/rejection refusing replacement, and existing OMS
other-live-BUY dedup. All acceptance/fills/ledger setup are CONTROLLED, not
venue execution. Rejection initially failed because its status-only fixture
lacked the exact intent broker_reject audit + identity-bearing broker event;
retained the failed XML and completed that fixture without changing its
positive assertion. Separate raw-status negatives remain mandatory.

This proves the existing RPG journal path, NOT arbitrary broker cancellation
outside that path. Missing journal/ambiguous feedback remains UNKNOWN and
must not resubmit. A live Webull sibling remains owned; no blanket flag clear,
claim release, account-wide cancel or manual-row adoption was introduced.

## Cache, Base, Boot

Retained today753 stage receipts12:30:04.223Z-13:35:55.661Z, original file SHA
d5a6085e29081598b8e9eefaf4e21cf4dbdabfbfabf53a38ef7d2dd61e5162cb;
every selected line SHA verified.752 adjacent POSITION-BOOK stage intervals:
min5069ms, median5224ms, p99nearest-rank6253ms, max7795ms;0>10s.
These are stage-to-stage cadence, NOT exact individual full-pass duration.
Actual full pass/primary-book refresh latency UNKNOWN: no paired start/end
logs, and this captured runtime preceded the R book and bd7 installation.
The later retained main15:33-15:47 capture has0 POSITION-BOOK/poll markers,
so cannot establish current bd7 pass duration either. No new box capture.

Conservative local reviewer-requested candidate: primary proof cache15s =
three configured5s polls, measured age STILL starts BEFORE bounded offloop
read at the START of _position_poll_pass. Idless initial real submit ACK's
independent15s cap unchanged. Functional clock controls0/10001/15000 permit,
15001/future refuse; all exact identity/origin/event/terminal/PG timeout/cap
controls preserved. This is NOT a measured three-full-pass guarantee. Main
must review the cache choice and obtain native current-pass timing if required.

Two next-day controlled source/private-DB replays: prior-session working BUY
remains boot-held; an explicitly controlled cleared prior order + current
restoration permits a NEW current-session identity. Old book cannot prove
the new generation; only a fresh matching controlled proof permits fanout.
The ordinary boot-release gate is NOT bypassed by cache absence. Selected
native boot restoration, FF2 durable boot/unknown, RW old working GTC and
offloop-generation fences, OMS duplicate guards35PASS. Actual next-day boot
on real box tables is UNKNOWN/unexecuted and remains main owed.

Local results158 core/neighbor +35 selected boot/FF2/RW/OMS +55 inventory =248PASS.
No API/provider/prod DB/restart/queue/status edits, container or benchmark,
sustained/timing test, or source from another lane. PG checks compile/bounds
with private SQLite; NOT real PostgreSQL. Main current exact all-ON composition,
review/PG/real box boot/serial timing/install remain pending. Local freeze only.

Evidence:
/Users/velkris/.codex/task-queues/analysis/eh2rth1-r-corrective-popper-20261013/
