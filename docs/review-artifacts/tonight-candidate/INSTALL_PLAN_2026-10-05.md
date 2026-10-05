# PMPRINT + durable-ticket recovery: October 5 candidate

**DRAFT FOR REVIEW. NOT EXECUTABLE, NOT SCHEDULED, NO PRODUCTION AUTHORIZATION.**

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
| 2 | RPGSTUCK #1093, 7f0729ef67e97566d7c254273802665c760adf08 | NOT pinned; eight-ticket startup proof complete locally; Validate pending | existing ticket recovery active; no NEW hand-offs |
| 3, optional | OWNMIX #1094, 55563e6f3abe004d484195c202b5118ea75e0169 | NOT pinned; F1/F2/U1-U4 plus Q2 conservative hint follow-up complete; Validate in progress as of 13:22 ET | entry binding; additive schema only if included |

No merge is authorized by this draft. Final exact heads, all required checks,
ordered integration rehearsal and fresh pins after any rebase/conflict must be
recorded before the operator names APPROVED_SHA (40 hex). Never Update branch.
Exclude #1094 entirely if it is unpinned. Do not substitute a moving branch tip.
LINE=CHART is an active separate build concern, not in this candidate.

As of 13:17 ET the authorized rebase checkpoints are RPG
`d68651686a72200913c6e7e498c574d22907ea18` and OWN
`bb4a6b22d592f350f0e851faae6a86d989944f3e`, each onto merged main
`7e10baf0319da796b84934fe38994f6db4fcfc0b`; range-diff 3/3 equal for each.
These are NOT final review heads: B1-B6 work bounding / fourteen-ticket replay
and OWN Q2 measured call-budget / hint filtering are in progress. The earlier
test pairs below remain historical, not results for those new final heads.

**Integration blocker:** the immutable pinned #1092 contains three old-code
characterization assertions that require proven-clear tickets to remain owned.
Those expectations conflict with the reviewed #1093 correction. Exact pinned
#1092 plus this #1093 source applies cleanly, but the targeted combined run is
76 passed / 3 failed, with no test skipped. Raw:
`/tmp/oct5-final-rpg-pm-composition-verified.txt`. The three failing cases are
fbfd692d, bd6ac0b9 and a007716c in
`test_current_rpg_off_startup_still_restores_recorded_blocking_ticket`.
The genuinely unproven faa55c1f case remains blocking. Keep #1092 frozen;
a separately reviewed test-expectation integration or a fresh #1092 pin is
required. Do not force production to preserve the defect to make tests green.
This draft cannot become an executable release while that blocker remains.
Adding exact OWN follow-up0e896505 to that rehearsal also applies cleanly;
targeted three-head tests give210 passed / the same3 failed, zero skips.
Raw `/tmp/oct5-three-head-composition-verified.txt`, as-of12:53 ET.

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
| (a) legacy reprice | first and reclaim cancel then next-pass placement with hand-off OFF; PMPRINT true, PMFLIP/PMREST false, NFQ/GAP true | #1093 first/reclaim and gap-hold tests PASS locally; standalone settings are not a claim of PM source composition |
| (b) durable startup | restore every October 5 ticket, prove per-leg ownership/disposition; rejected old order releases by exact identity and zero-fill proof, never age | all eight recorded tickets and five deferred intents tested; exact rejected-zero proof uses one durably claimed GET; after20:00 zero saved opens |
| (c) composition | NFQ ON + hand-off OFF on this tree: local no-wire recovery, held/requeued mirror, one buy per slot, true unknown remains blocked | targeted combined run 76 PASS / 3 obsolete-characterization FAIL; integration blocked pending review |
| OWN, if included | real submit and poll fill paths bind both IDs for both accounts; flat closes lifecycle without unsafe attribution; U1-U4, conservative hint and old-code/schema compatibility | local569 focused PASS;15/15 review-equivalent mutants RED; fresh pin and Validate required |

Own source pull at 12:10:51 and 12:11:28 ET used read-only SQL, 8-second statement
limit and at most64 rows per table. Raw paths are in STARTUP_TICKETS.md. It found
eight tickets: the seven requested plus RETO Webull ba108172. Include the eighth
in the disposition census; do not call seven-ticket proof complete while hiding
it. Tests are not evidence that the production journal has already changed.

#1093 local verification: exact main a80b5181 = 5412 passed / 56 failed;
follow-up = 5515 passed / 56 failed, identical failed node IDs (zero added,
zero removed). Broad focused 1165 passed; recorded startup module 54 passed;
32/32 assertion-killed mutations. See its REPORT_STARTUP.md and
verification-startup.json for frozen source/fixture hashes and exact names.
Fresh four-parent broker GET bodies and the complete SQL pull are independently
retained in STARTUP_TICKETS.md. These local results do not substitute for CI,
a fresh exact-head pin, or the unresolved combined-tree expectation review.

#1094 fresh paired unit result: exact main7e10 =5500 passed /56 failed;
follow-up55563e6f =5570 passed /same56 failed; failed-name diff added0/removed0.
Its REPORT_2026-10-05.md maps F1/F2/U1-U4 to exact tests and verification.json
retains both complete failed-ID sets and every mutation killer. The F2 submit
test runs public process_trade_intent, not a direct fill hook, plus real polling
on both accounts, followed by owned-child exit polling. Broker replies are
controlled recorded-shape inputs, not a live trading exercise. Actual old-main7e10
nullable-schema compatibility passes on both accounts, offline in SQLite.
CI is pending, not green; OWN remains optional and unpinned.

The ticket census is frozen at12:13 ET. A fresh bounded read immediately before
execution must include later tickets (handoff M56/M58 report MI/SCKT after that
capture). Do not describe these eight fixtures as the whole live day's census.
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
