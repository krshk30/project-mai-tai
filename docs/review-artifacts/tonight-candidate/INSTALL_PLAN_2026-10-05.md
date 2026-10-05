# PMPRINT + durable-ticket recovery: October 5 candidate

**DRAFT AWAITING FRESH PINS AND EXACT-SHA OPERATOR GO.
NOT EXECUTABLE, NOT SCHEDULED, NO PRODUCTION AUTHORIZATION.**

**14:22 ET follow-up checkpoint:** R20 and F3 are locally corrected on the
exact heads below, each one follow-up commit without rebase. Fresh main5500/56
versus RPG5692/same56 and OWN5577/same56 has failed-ID added[]/removed[],
independently checked from XML. Exact merged rehearsal tree
`17fcd4d927827825a70a3ae1b4f772911c7ddbe6` equals gitmerge-tree of both heads;
51 targeted tests pass. Real-loop R20 runtime-wake removal gives two assertion
failures; F3 deferral removal four; exact-episode pending-writer removal two;
H4 account-filter removal one; exact R15 unproven->expired two. The earlier
provisional pending-writer mutant failed by KeyError, not an assertion; the
final test uses .get and its exact rerun gives two AssertionErrors/no KeyError.
Provisional full5769/same56 differed only that assertion lookup; the exact-head
full/focused reruns are running. Validate x2 on both heads is pending, fresh
pins are missing. Historical results below do not authorize this candidate.
Neither PR is merge/install eligible now. Re-read the bounded ticket census
immediately before any separately approved execution; any later untested shape
is UNKNOWN/STOP. No execution or scheduled install is authorized by this draft.

This new document supersedes the folded three-PM-switch plan and the v2-only
two-switch rollback draft. Turning hand-off OFF on the current code does not
release durable held_unknown tickets. The required candidate is #1092 plus
#1093 recovery code, with hand-off OFF; #1094 is optional only after its complete
follow-up is independently pinned. No journal purge, database repair, age-based
ticket release, or additional trading-rule change is permitted.

## Candidate And Merge Order

| Order | Candidate | Review state | Runtime change |
|---|---|---|---|
| Existing | PMREST #1091 on main a80b51816abf0aefc269f0fdfc473468fd3fe62c | merged, dark | stays OFF |
| 1 | PMPRINT/PMFLIP #1092, 5ee9f41654d8bb3b66414c4b84c3804c8d4c47c6 | merged at 13:09:07 ET to main 7e10baf0319da796b84934fe38994f6db4fcfc0b; whole tree equals pinned tree 736a540cac3c2724fa116216913331f532ecdfa9 | still dark on the box; proposed PMPRINT ON and PMFLIP OFF |
| 2 | RPGSTUCK #1093, 7fe27daaad522bb3aa7f921212a3179532117f7c | NOT pinned; R20 runtime one-time wake + full held-transition audit; full5692/same56, focused1378; Validate x2 pending | existing ticket recovery active; no NEW hand-offs |
| 3, optional | OWNMIX #1094, 76c3b4f764c56baafed3d527a0a47dca8cbcb5b4 | NOT pinned; F3 bounded deferral and pending-writer guards restored; H4 per-account assertion; full5577/same56; Validate x2 pending | entry binding; additive schema only if included |

No merge is authorized by this draft. Final exact heads, all required checks,
ordered integration rehearsal and fresh pins after any rebase/conflict must be
recorded before the operator names APPROVED_SHA (40 hex). Never Update branch.
Exclude #1094 entirely if it is unpinned. Do not substitute a moving branch tip.
LINE=CHART is an active separate build concern, not in this candidate.

The authorized rebase checkpoints were RPGd6865168 and OWNbb4a6b22, each
onto merged main7e10; range-diff3/3 equal for each. Their subsequent follow-ups
above require fresh review records. No pinned PM commit was amended.

**Historical failed rehearsal, 13:33 ET:** clean main7e10 plus generated patches
for RPG271314de and OWN55563e6f applies cleanly in the isolated worktree
oct5-reviewed-followup-composition. The source import path is verified.
The staged rehearsal tree is `87f73bda73d9addc59f6543ca8642cb31f48fcc6`;
360 focused tests PASS, no skips, raw
`/tmp/oct5-final-composed-focused.xml` and `.txt`. This is a rehearsal tree,
not a merge commit or APPROVED_SHA. Full combined units: **5701 passed/57 failed**
versus clean main **5500 passed/56 failed**. Exact XML diff adds only
`tests/unit/test_rpgstuck1.py::test_r_t5_uncertain_webull_dispatch_reconciles_exact_client_without_resubmit[fills]`;
removed0. Actual phase remains `submit_unknown` instead of `filled`. This is a
composition BLOCKER, despite the focused pass and standalone CI greens.
An isolated five-run reproduction fails1/5. The shared SQLite StaticPool fixture
is suspected, not yet proven; no test suppression, timing sleep or runtime
workaround is authorized. The RPG sole writer is investigating. Raw XML:
`/tmp/oct5-final-composed-full.xml`, SHA256
`5120964296c48e3662d485265b8fe28318983b9580fe1e3331a55cfcb0e1865a`;
repeated raw output `/tmp/oct5-composition-fill-5-reruns.txt`.
The first tail-only diagnosis incorrectly named unattended-upgrade; exact
failed-ID comparison corrected it. That missing-Linux-tool failure belongs
to the baseline and is not the added failure. Do not label this run green.

**Fixture-only continuation,13:52 ET:** the failed SQL trace shows a serial
`phase=filled` UPDATE, another thread's rollback on the SAME physical SQLite
connection, then serial commit; the update is lost. This is the StaticPool
test fixture sharing a single DBAPI connection, not a changed production CAS
rule. With the same composed production source, the old fixture fails6/50
fresh processes; separate file-backed connections pass50/50. Checkpoint
29a17b1f changes only tests/docs, with empty production-source diff from271314de.
Parent applies that exact delta to the OWN55563e6f rehearsal; new staged tree
`8394d099bd464306a35028e9d2c47e9f4075b451`. The independent parent50-repeat
fill/no-rebuy cases plus connection-isolation test PASS51/51, raw
`/tmp/oct5-parent-composed-fill50.xml` and `.txt`. Reverting the fixture to
StaticPool is assertion-RED; exact R13 and R16 remain assertion-RED.
The corrected source is frozen at29a17b1f; finalc6548875 adds evidence only,
with empty source/test diff. No production
workaround, weakened guard, sleep or suppressed protection task is introduced.

**Final exact-source composition,13:55 ET:** main7e10 plus RPGc6548875 and
OWN55563e6f gives staged tree `5e16cf4d13ca00e82dab91a4f4f8896fb19eb7cb`.
`git merge-tree --write-tree` independently produces that EXACT tree with no
conflict. Parent full units: **5753 passed /56 failed**, zero skipped, versus
main **5500 passed /56 failed**; exact failed-ID added=[] /removed=[].
Parent focused composition: **411 passed**, zero skipped, including recorded
RPG/NFQ composition, tonight's flag set and both ownership accounts. Thus the
extra fill-reconciliation failure is resolved by fixture isolation, not a
production change. The56 baseline failures remain; do not call the entire
suite green. COMPOSITION_RESULT_2026-10-05.md retains commands, counts and hashes.
Fresh review pins and operator GO remain blocking. No merge/install
eligibility is inferred from a clean rehearsal tree.

The previous three PM characterization failures are corrected only in the
RPG follow-up after the authorized PM merge. The four recorded fixtures remain:
three proven-clear tickets release by proof; truly unproven VEEA remains blocked.
No source behavior is forced to preserve the old bug, no unknown guard is
weakened, and no test is skipped. The earlier76/3 and210/3 results remain
historical records, not this new composition's results. Fresh review is required.

The execution window is October 5 after 20:00 ET and observed rotation, only
on the operator's exact-SHA GO for the reviewed release. Bind BOX_SHA and clean
tree before writes. Last own observed box checkout was
e1ce3b3978fbcb1ff00daecdf6dc2618e6b5fb89; services still loaded bbb43604.
Re-read identities/hashes at release review and at execution. Main beyond
APPROVED_SHA may contain docs changes only; refuse non-docs drift.

## Exact Live Set

Only two primary EnvironmentFile keys change, with backup/hash and duplicate
refusal; replace an existing single definition or append when absent:

```text
MAI_TAI_STRATEGY_SCHWAB_1M_V2_PM_PRINT_ASK_CONFIRM_ENABLED=true
MAI_TAI_STRATEGY_SCHWAB_1M_V2_ATR_REPRICE_HANDOFF_ENABLED=false
```

PM_FLIP_WAIT=false, PM_REST_REPRICE=false, NFQ=true and GAP_HOLD=true must be
verified, not newly enabled. Retain 600/300/1000 sizing, NFQ mirror age 10000ms,
pre-market reactive age 2000ms, target5/stop8/floorfalse, retry-one, EOD transition,
overnight flatten, COLDSTART, ORB live; reclaim and polygon_30s stay false.
ORB's separate orb-paper.env is recorded but not changed.

OMS must also restart on the shared env with hand-off OFF: it runs the durable
coordinator and recovery, and currently loaded settings do not change when the
env file changes. Verify hand-off OFF on both new OMS and v2. PMPRINT is owned
by v2; verify its new PID's /proc, not a Settings default. #1093 recovery must
operate with OFF without admitting a NEW hand-off ticket. OFF does not erase
existing coordinators: a recovered local no-wire ticket still owns that leg
until fresh strategy authorization finishes or expires it. During the entry
window it may finish its already-existing replacement; legacy placement must
not race it. The requested after20:00 startup is outside the existing entry
window, so prove those clear tickets expire by window/segment rules instead
of submitting saved buys. The startup report must state this distinction.

Install the isolated checker and both catalogs from APPROVED_SHA with hashes.
Catalog must equal this live set, not the older all-PM-ON draft. Expected
combined FLAGGATE is 143 = 135 boolean/service checks + 8 numeric checks;
numeric separately 8/8. Confirm against the final composed catalog before
approval and print actual denominators. Neither #1093 nor #1094 adds a switch.

## Required Pre-Review Proofs

| Proof | Required exact-set assertion | Current state |
|---|---|---|
| (a) legacy reprice | first and reclaim cancel then next-pass placement with hand-off OFF; PMPRINT true, PMFLIP/PMREST false, NFQ/GAP true | merged-PM RPG source and exact three-source rehearsal pass first/reclaim plus recorded cross/hold controls |
| (b) durable startup | restore every captured October 5 ticket, prove per-leg ownership/disposition; rejected old order releases by exact identity and zero-fill proof, never age | all14 jobs/11 orders/15 intents/1 actual fill tested; strict RETO probe durably claimed once; after20:00 zero saved opens/cancels, true unknown stays blocked |
| (c) composition | NFQ ON + hand-off OFF on this tree: local no-wire recovery, held/requeued mirror, one buy per slot, true unknown remains blocked | exact new-head tree17fcd4d9 targeted51PASS; final full/focused reruns pending; prior c654+55563 full5753/same56 is historical only |
| R20 | waiting/reads29 -> runtime exhaustion -> one proof tick without restart; rejected-zero clears, unknown remains held and 180 quiet turns issue no SQL/scan/tick | eight recorded-RETO real-loop variants PASS; wake removal assertion RED2; every held transition and next-check route in REPORT_R20.md; later-ineligible exhaustion remains startup-only, not aged clear |
| OWN, if included | real submit and poll fill paths bind both IDs for both accounts; F3 bounded child-fetch retries and same-episode writer priority; no unsafe fill attribution; H4 account isolation | fresh full5577/same56; F3 real-exit tests PASS on both accounts, retry removal RED4 and writer removal RED2; H4 RED1; fresh pin and Validate required |

Own source pull at13:17:02 ET uses READ ONLY SQL,8s and64-row/table sentinel
limits. STARTUP_TICKETS.md retains paths/hashes and the previous exact broker
GETs. It includes all14 captured tickets and the actual SCKT Webull BUY280@1.06
Fill. Tests are not evidence that the production journal has already changed.

#1093 initial follow-up verification: exact main7e10 =5500 passed/56 failed;
follow-up271314de =5632 passed/same56 failed, introduced0/resolved0. Parent
independently compares full XML failed-name sets. Broad focused1318 PASS,
recorded/integration97 PASS,64 assertion-killed mutation controls; parent exact
reviewer R13 guard removal gives1 assertion failure and R16 candidates removal
gives6 assertion failures (accepted/rejected across APUS/VEEA/RETO).
REPORT_B_REVIEW.md and verification-b-review.json retain exact tests/source
hashes. No full suite is called green while the56 baseline failures remain.
That initial head's Validate x2 passed; it is superseded byc6548875.
Final fixture/source standalone =5683 passed/same56 failed;1369 focusedPASS,
67 fresh assertion-controls RED. Parent independently verifies its failed names
against the main XML. REPORT_FIXTURE_ISOLATION.md retains final pair, exact
rollback trace, tests and all67 controls. Current final-head Validate x2 is PASS,
verified14:01 ET, runs37351785657 and37351790055. Fresh pins remain required;
the independent-review-pin check is not yet PASS. No eligibility is inferred
from a prior-head check or CI alone.

B5 proof work is bounded, not ownership: one startup scan; exact-generation
committed writes wake only matching held tickets, durable hashes suppress repeats.
No periodic unknown scan/xadd/DB retry and no age clearance. Failed scans use
the existing configured broker-sync error cadence. A missed commit notification
retains the block until matching evidence or next startup; it never creates a buy.
The serial lane re-proves no-wire against all exact-generation broker rows.

#1094 fresh paired unit result: exact main7e10 =5500 passed /56 failed;
follow-up55563e6f =5570 passed /same56 failed; failed-name diff added0/removed0.
Its REPORT_2026-10-05.md maps F1/F2/U1-U4 to exact tests and verification.json
retains both complete failed-ID sets and every mutation killer. The F2 submit
test runs public process_trade_intent, not a direct fill hook, plus real polling
on both accounts, followed by owned-child exit polling. Broker replies are
controlled recorded-shape inputs, not a live trading exercise. Actual old-main7e10
nullable-schema compatibility passes on both accounts, offline in SQLite.
OWN Validate x2 PASS at13:27:34/13:27:56 ET; its fresh independent pin remains
required. OWN remains optional, not installed.

The ticket census is frozen at13:17:02 ET. A fresh bounded read immediately before
execution must include any later tickets. Do not describe these14 fixtures as
the whole live day's census.
Any new shape not covered by reviewed recovery proof is UNKNOWN/STOP.

The refreshed own read at 13:17:02 ET contains fourteen jobs, eleven orders,
fifteen intents and one actual SCKT Webull BUY fill (280 shares at 1.06).
STARTUP_TICKETS.md records the exact raw path/hash and six later MI/SCKT tickets.
The fresh follow-up must replay this entire captured population; the eight-job
proof alone is no longer enough. No disposition is inferred from elapsed age.

## Preflight Before Any Write

Require no concurrent deployment, clean exact BOX_SHA, reviewed source ancestry
and selected-PR path diff, saved preopen/checker/catalog/helper hashes, all
service PID/start/NRestarts identities. No gate edit or forced green. Record
both EnvironmentFiles and schema revision without credentials.

Use the reviewed strict-flat helper bound by blob/hash in the release: fresh
direct positions on BOTH brokers, zero open managed rows, zero nonzero virtual
positions, no working entries/exits or unresolved wire dispatch. Read-only bot
books and broker reads must agree. A held ticket is not cleared merely because
its row says refused; preserve identity-specific order/dispatch proof. No
historical NXL override or ledger adjustment. Unreadable = UNKNOWN/STOP.

Fresh flat/order proof precedes checkout/env/schema writes and EVERY scoped
stop/start/restart. Immediately before v2 stop run the blocking restart gate:

```bash
sudo /home/trader/project-mai-tai/ops/preflight/preflight_v2_restart.sh
```

Require rc0. An armed set blocks and requires an operator-named live set and
explicit Bug2 acceptance, never automatic override or an old symbol list.
Control remains up as the single token refresher. After a quiesce >25min, prove
fresh token expiry and direct reads before proceeding; do not start a competing
writer. Missing proof stops rather than refreshing by guess.

All Redis reads bounded; no multi-entry snapshot read or XINFO/EVAL payload
materialization. Before/after strategy warm-up: evicted_keys unchanged, memory
<=1.6GB, all five owners plus _migration_complete=1. Unreadable, eviction or
owner loss = UNKNOWN/STOP/page. Record actual readings, not log-marker proof.

## One Coordinated Replacement

Services: **v2 + strategy + OMS only**. No gateway, ORB, orb-schwab, paper,
guard, control, capture, reconciler, Redis or Postgres action. No watch install.

The reviewed runner must bind all commands and artifacts, use flock/exclusive
attempt directory, backup env/catalog/preopen, and remain attended through
close-out. A desktop wake-up is not a box job. No unit or timer is staged by
this draft. Approval is absent until review and exact-SHA GO.

After initial flat/gate proofs and backups, advance to APPROVED_SHA, refresh
editable runtime as trader, prove clean tree/import path; then only the two env
key changes. Settings reads use root; protected env is never read as trader.

Normal sequence, flat_now before each action: stop v2 -> stop strategy ->
restart OMS ONCE -> start v2 -> start strategy. If #1094 is included, replace
the OMS restart with stop OMS -> approved additive migration -> start OMS ONCE;
GO must explicitly include that schema-bearing sequence. Validate current
Alembic revision and exact migration base first. Add nullable entry_order_id
UUID and entry_client_order_id(128); no binding/fill/ledger backfill or downgrade.

The runner's reviewed abort trap restores strategy only on a pre-OMS abort as
authorized, verifies state, pages, and names actual v2/OMS/strategy states.
Any other stopped-service recovery needs explicit authorization, not silent
extra starts or retries. After OMS replacement begins, halt/page on failure
and stay attended. No automatic rollback or extra restart is authorized.

## Rollback By Concern

| Concern | Reviewable rollback | Safety limitation |
|---|---|---|
| PMPRINT #1092 | operator-authorized flag OFF plus v2 replacement, or exact prior reviewed code | not pre-authorized here; PMREST/PMFLIP remain OFF |
| recovery #1093 | exact prior code only after broker/order/ticket census and operator recovery ruling | old code restores durable blocks even with OFF; cannot be called a clean rollback or bypassed by purge/timeout |
| OWN #1094 | exact prior reviewed code, nullable additive columns left in place | no schema downgrade; old-code compatibility test required before eligibility; unproven attribution never creates a fill |

No rollback is run merely because this table exists. On failed post-install
proof: report actual process states, journal UNKNOWN, page and seek exact
recovery instructions. No broker write, DB edit or extra restart inferred.
Legacy unbound OWN rows may close on broker-confirmed flat; only attributing
a child fill needs the binding. Unproven exit remains unrecorded and paged.

OWN rollback compatibility must use the actual old code and actual additive
migration, not a new-code mock. The follow-up's verify_old_code_schema.py
reconstructs old main7e10baf0 via git archive, applies migration0022 to isolated
SQLite, then old ORM/store create/read/close PASS on both live-account names.
The old model has no binding columns; nullable columns remain present. This is
an offline compatibility proof, not a production PostgreSQL rollback exercise.
Code revert restores the old ownership defect and creates legacy unbound rows;
it is not a data repair. Keep schema revision/columns, do not downgrade/backfill.
Execution still requires explicit operator recovery GO and fresh flat/order proof.

OWN Q2 could not measure total peak Schwab REST calls/min or establish a primary
documented limit. Its bounded log inspection found206 position rollups with a
maximum21 successful position reads in one five-minute rollup; that is NOT a
complete REST census or evidence of headroom. The explicitly requested fallback
is used: one account-wide hint per eligible Schwab account per sync, then exact
bound-parent reads only for symbols named by the hint. The hint is never ownership
evidence; missing/failed hints do not renew stand-down, and pending exit retries
remain independent. Webull is skipped. No partial-parent scaling change.
Loaded cadence was15s normal/1s active-stop. For K hinted bound rows, nominal
stand-down reads/min are4*(1+K) normal or60*(1+K) active, plus event syncs and all
other traffic. At K=1/3/6 this is8/16/28 or120/240/420, not a safe REST budget.
No unproven numeric limit or new cadence is installed; no headroom is claimed.

## Mandatory Close-Out

New OMS/v2/strategy PID/start/NRestarts0, active, zero new-process tracebacks;
all other identities unchanged. Startup recovery census for all fourteen captured IDs
and any later tickets found by the fresh execution census:
phase/reason, proven old/replacement identity, account/symbol and ownership.
No new hand-offs with OFF. Never claim a truly unknown wire order cleared.
Fresh live:orb sync ok>0/failed0 and account_positions stamp after OMS start.
OWN binding/migration census only if included; no manual row repair.

Read flags BY KEY from /proc of the final new PIDs. Print/hash actual FLAGGATE
and numeric results. Record BOOT-HOLD/population literally; held overnight is
EXPECTED BY DESIGN with verification owner, not a release PASS.

Re-pin preopen.sh ONCE to October6/final APPROVED_SHA and three final new
identities; preserve all other identity and routing lines. REPORT must end
v2-restart-evidence-20261006.md. Exclusive backup, diff, SHA256, bash-n, mode0700.
Do not run the date-fixed October6 gate early. No Monday guard re-pin/restart.

Journal deployments-20261005.md: source/release/plan/helper hashes, flat/gate
proofs, exact commands, backups/env diff, optional migration revision, new and
unchanged identities, loaded values, complete as-of ticket dispositions, sync census,
Redis before/after, catalog/checker outputs and preopen backup/diff/hash.
COMPLETE requires actual proofs, not just active processes. Accepted UNKNOWN
log coverage and UNEXERCISED live paths stay named, never relabelled PASS.

## Next Session Ownership

Codex-2 verifies first nonempty warm-up and BOOT-HOLD release at07:00. Claude-1
independently checks bar continuity ~07:10. If strategy restarts, scanner
validation is mandatory: prefill N >= squeeze_10min_needs when retained depth
exists, first15min 5/10min squeeze counts and watchlist adds versus prior five
sessions (counts/denominators/raw sources), zero new strategy errors. Strategy
restoration warm-up bound180s, not60s; empty/young population is stated.

PMPRINT: recorded SAIQ stray blocked, no slot/latch; stream and REST crosses
both size600/300 from the same confirming ask; no new upper cap. Flip-wait OFF
keeps today's latch; PMREST OFF keeps today's disarm/re-arm. Decision-cache
timing remains UNMEASURED until live records. Hand-off remains OFF: do not claim
live cancel->readback->place latency exercised by this install. OWN child/fill
attribution and recovery after real broker errors remain separately monitored.
