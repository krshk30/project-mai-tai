# RPG1 + COLDSTART1 joint install, Sunday 2026-10-04

Status: DRAFT FOR INDEPENDENT REVIEW. No approval receipt, production staging,
timer installation, merge or restart is performed by this document. Target is
Sunday 2026-10-04 10:00 America/New_York. The executable release and its exact
merged application SHA must be reviewed before the timer is enabled for execution.
An unresolved release field means NOT READY, never permission to use branch tips.

## 1. Scope and release boundary

- Starting main/box: `250ab18458f4d806aa8bcdf98d787fff5bb57df4`, clean.
- #1085 RPG1: rebased candidate `7a9957bfef8a7e0be624bbeb9d04e9d6fd1b3bbf`
  on that base resolves NFQ1/SNAPHIST1 conflicts and addresses P3/P4/P8/F1.
  Local evidence: 907 focused pass, 12 actual composition cases without skips,
  39 mutations RED; full5303 pass/56 fail with the exact untouched baseline
  failed-name set. Independent new-head pin and exact-head green CI must still
  be verified before merge. Neither this candidate nor old6f3f000c is approved
  for installation by this draft.
- #1088 COLDSTART1: pinned `85cf3f8eca0e836e8365ad88204a9820b44f571b`.
  HOLD merge until this joint plan is reviewed and the install window opens.
- Restart only OMS and the five subscription consumers: strategy, schwab-1m-v2,
  orb, orb-schwab, momentum-paper. Gateway, control/token refresher, market-capture,
  reconciler, Redis and PostgreSQL stay running. No reboot or gateway restart.
- RPG1 retains cancel -> exact broker read -> re-place only after confirmed
  cancellation with explicit cumulative zero. A partial fill is a position;
  the remainder is not re-bought. No place-first, native replace, artificial
  order, threshold/price/size/exit change, migration or database repair.
- COLDSTART1 announces each consumer's restored/current set, even empty. No
  owner replay, Redis write by the installer, subscription repair or RDB change.
  Redis persistence remains OFF. The gateway already runs #1077/#1084/SNAPHIST1.
- No watch reinstall, additional restart, rollback or automatic recovery is
  authorized. Incomplete/ambiguous state stops the sequence and pages the operator.

### Two approvals, not a moving-main deployment

First review this plan and authorize the two in-window repository merges.
Recheck exact head/base, independent-review-pin and both Validate checks before
each merge. Use rebase-merge, never Update branch or admin bypass. If the first
merge changes the second PR's base/range, or a conflict/new head is required,
STOP for the second exact-head/base re-pin. A reviewed integrated test tree is
not a substitute for the repository's pin procedure. Delete superseded records
only in separate records-only commits under that procedure.

Dry `git merge-tree` of candidate7a9957bf and pinned85cf3f8e on October3
found one content conflict: `ops/health/expected_flags.json` (both add their new
flag near the start). Settings, v2 startup and flag tests auto-merge, but that
does not certify their composition. Preserve BOTH catalog entries in a reviewed
resolution. Do not touch the pinned COLDSTART branch before its rebase is
authorized; the resulting changed head requires a fresh exact-head/base pin.
This dry check did not alter any branch, index, worktree or production state.

After both approved merges, record `RPG_MERGE_SHA`, `COLDSTART_MERGE_SHA`, final
`APPROVED_SHA`, tree, ordered commits and full path diff from BOX_SHA. Require
only the reviewed two PRs, their tested conflict resolutions and any separately
identified docs commits. Run the joint RPG1 x NFQ1 x COLDSTART regression set on
that exact tree. No additional code merge before completion. Origin/main may
be ahead of APPROVED_SHA only by docs paths; it must contain APPROVED_SHA.

Then claude-1 reviews a final release manifest binding the plan commit,
APPROVED_SHA/tree, both pin records and every executable artifact's SHA256.
Only that final approval unlocks production changes. If review cannot finish
inside the window, leave the box unchanged and report the missed window.

### Listable approval-gated job contract

Names reserved for the reviewed runner, not installed by this draft:

```text
project-mai-tai-rpg1-coldstart1-install-20261004.service
project-mai-tai-rpg1-coldstart1-install-20261004.timer
/home/trader/after-hours/2026-10-04/rpg1-coldstart1-job/release.json
/home/trader/after-hours/2026-10-04/rpg1-coldstart1-job/approval.json
/home/trader/after-hours/2026-10-04/rpg1-coldstart1-run/
/home/trader/fleet_health/deployments-20261004.md
```

Proposed window: 10:00-11:59 ET, with no service action starting after 11:30.
The reviewer must approve that end time as well as the target start. Timer:
`OnCalendar=2026-10-04 10..11:*:00 America/New_York`, `Persistent=false`,
`AccuracySec=1s`, `RandomizedDelaySec=0`. Root oneshot, `Restart=no`,
`RemainAfterExit=yes`, restrictive umask, deploy lock, exclusive attempt claim.
Missing approval skips; an attempted run never automatically retries. Disable
the timer after completion/refusal, not merely the application service.

The receipt is root-owned mode 0600, reviewer `claude-1`, decision `APPROVED`,
exact date/window, final application/plan hashes, artifact manifest and explicit
Sunday clock-only exception below. An invalid/missing receipt or changed
artifact prevents all writes. Do not ship approval.json as a committed artifact.
Staging must show `systemctl cat`, `list-timers --all`, NEXT and all file hashes.
This draft does not claim a timer exists; literal runner/validator, tests and
staged hashes are a mandatory next review artifact before execution.

## 2. Verified starting evidence and hard stops

Read-only October 3 evidence, to re-verify immediately before execution:

| Service | PID | Start UTC | Action |
| --- | ---: | --- | --- |
| market-data | 2907 | 2026-10-03 22:03:17 | Untouched |
| oms | 2910 | 2026-10-03 22:03:17 | Restart |
| strategy | 2911 | 2026-10-03 22:03:17 | Restart |
| schwab-1m-v2 | 2912 | 2026-10-03 22:03:17 | Restart |
| orb | 2913 | 2026-10-03 22:03:17 | Restart |
| momentum-paper | 2914 | 2026-10-03 22:03:17 | Restart |
| orb-schwab | 2915 | 2026-10-03 22:03:17 | Restart |
| control | 2916 | 2026-10-03 22:03:17 | Untouched |
| market-capture | 2917 | 2026-10-03 22:03:17 | Untouched |
| reconciler | 2918 | 2026-10-03 22:03:17 | Untouched |
| redis-server | 926 | 2026-10-03 21:41:33 | Untouched |
| postgresql@16-main | 1037 | 2026-10-03 21:41:36 | Untouched |

All had NRestarts=0 in the resize completion journal. Latest small read during
this draft: Redis evicted_keys=0, used_memory=758850064, maxmemory=2147483648;
five empty owner sets, `_migration_complete=1`, checkpoint `1791065000836-0`.
These are starting observations, NOT Sunday preflight or startup proof.

Before any write require no competing install, clean BOX_SHA, unchanged
identities/start/InvocationID, fresh healthy gateway heartbeat and readable
durable bot state. Capture all twelve identities and the deliberately inactive
tv-alerts disposition. A drift is UNKNOWN and requires review, not silent re-pin.
Record env, installed checkers/catalogs, unit files and the following hashes:

```text
preopen.sh: 79d500d82c73271d68c1ff75ab64f9533713e1116d42944baa524d6762faa239
expected_flags.json: d15b588540000b18253a8d6cc4d01d9e389a83799d02d219dbdab317da1a1eff
expected_numeric.json: 934fd2cc177cd7dc900ebc1b85543ae08e3857364cd0a60f99f2d5a5cb2c2084
Monday start-guard-20261005.sh: f9ae601b2477813fdc02f31286b8c61c473b3e71386908145ba181931e9b1a81
```

### Flat, orders, ownership and armed state

Use reviewed `/home/trader/after-hours/2026-10-01/option-a-strict-flat-check.py`
SHA256 `831913206678c5bd2fa6718173c4c5d7b5887c62e44217ad10b6776851f0381e`.
Require fresh direct **both broker** reads, zero holdings, zero managed rows,
zero nonzero virtual positions, zero net bot fills, no unreadable/partial read.
No NXL/manual-close override is authorized for Sunday. Run before checkout/env
writes and immediately before EVERY stop/start/restart. A nonzero rc stops;
there is no unattended 300-second loop after the first service action.

Also require zero working bot BUY orders, pending drafts, active NFQ holds and
unresolved RPG handoffs/jobs (completed historical audit records do not block).
Inventory statuses from the final code, not an incomplete fixed status list.
OMS sync reads DB-known orders individually; it does NOT provide a complete
broker open-order enumeration. The Schwab native-OCO read is also not such an
enumeration (time/status/symbol limits). Do not describe either as proof of zero
unknown broker-side orders. A year of history is not an approved substitute.
The final release needs a reviewed bounded reader OR an explicit reviewer/
operator disposition of this enumeration limit, with all observed unexplained
orders listed. This is an execution blocker until resolved, not a runtime
feature request folded into these PRs. Record exact requests, rows and bytes.

Intent drainage is a separate execution blocker: OMS starts its stream reader
at `$`, advances a cursor before handling, and can submit before DB commit.
Empty books and stable stream tail alone do NOT prove all submitted intents
were processed. Before any writes the release review must establish a bounded
drain proof or explicitly accept the Sunday-idle disposition used in the resize:
fresh v2 weekend/entry-window probe, ORB rejection/universe probe, paper
disconnected/no-engine probe, no pending drafts, and archive/disposition of
retained intent IDs (not replay). The prior Saturday disposition is evidence,
not automatic Sunday approval. After quiescence, a new actionable/unaccounted
intent or ambiguous submit always stops. Preserve all DB state and never claim
that archiving tail IDs itself proves safe drainage.

Immediately before stopping v2 require `preflight_v2_restart.sh` rc0. Sunday
10:00 is before its 18:00 proxy, so final approval must explicitly authorize:

```bash
sudo bash /home/trader/project-mai-tai/ops/preflight/preflight_v2_restart.sh \
  --clock-override 'Sun 2026-10-04, no session; zero armed, zero managed, both brokers flat' \
  --i-accept-clock
```

This overrides CLOCK ONLY. Record all three gate lines and the exact override.
Armed segments remain blocking. If nonzero, STOP for the operator's newly named
live set and explicit Bug 2 acceptance; do not synthesize or reuse an arm override.
The same invocation must observe/compare that exact set if separately authorized.

### Token lifetime and single-writer discipline

Control stays active throughout, so its refresher remains the sole token writer.
Record token `expires_at` only, never token bytes. Before each fresh broker read
require a readable unexpired token. If quiescence exceeds 25 minutes, require
evidence of a successful refresh by control after quiescence began and a new
expiry > now + 25 minutes before continuing. Wait for the existing refresher's
normal cycle inside the window; it refreshes near expiry, not on demand here.
Missing refresh/expiry or control identity drift => STOP and page. Do not run a
one-off grant concurrently with control. If control is down, any separate
single-writer refresh/atomic-write continuation needs operator review first.

## 3. Ordered install and abort behavior

The eventual runner must implement this exact sequence with dated approval,
exclusive attempt claim, `flock /run/lock/project-mai-tai-deploy.lock`, pipefail,
timeouts and an EXIT trap. No undefined helper may be accepted in the runnable
release. All proof functions below must be committed, tested and hash-bound.

1. Snapshot/flat/token/order/Redis checks and v2 gate before writes; archive
   env, preopen, Monday script/unit/timer, gate catalogs and twelve identities
   to exclusive root-only files. Preserve file owner/mode; journal all hashes.
2. Validate merged release and reviewed diffs. Set only the two reviewed feature
   switches below, refusing duplicate/case-conflicting env definitions. Record
   a redacted two-key diff; every numeric/exit/ORB setting is unchanged.
3. Switch box to APPROVED_SHA and refresh editable metadata as trader. Require
   clean tree, correct import path, exact installed source hashes. Existing
   processes still run old code until their listed restart. Do not call this
   intermediate state completed.
4. Fresh flat/token/orders, then v2 restart gate. Stop v2; record stopped identity
   and log device/inode/offset after exit. Fresh flat before stopping strategy,
   orb-schwab, orb and momentum-paper, individually in that order.
5. Require all five producers PID0, the approved drain/idle disposition still
   valid with no unaccounted new intent/draft/job/hold, fresh flat, Redis safety
   and unchanged untouched identities. Record per-service log
   offsets after exit. Then restart OMS once; require new healthy identity,
   NRestarts0, no new-process traceback, post-start heartbeat and loaded flags.
6. Fresh flat; Redis checkpoint; start strategy once. Check Redis each second
   through warm-up and post-start healthy heartbeat (180 seconds maximum).
   Then fresh flat before each start: v2, orb, orb-schwab, momentum-paper.
   Verify each new identity, post-start log and its own startup replace before
   moving on. Use heartbeats only where that service actually emits them in
   this phase. Paper's weekend path returns before its heartbeat; ORB-Schwab's
   run loop does not emit the inherited heartbeat. Their proof is stable new
   identity, fresh replace, readable post-start logs and the weekend/live-mode
   state, not a fabricated missing-heartbeat FAIL. Paper must remain
   weekend-idle, with empty subscriptions/no websocket or
   broker route; this is a service restart, not a treatment session start.
7. Final six-new/six-unchanged identity census, fresh both-broker flat/order
   checks, owners/heartbeat proof, /proc audit, numeric/FLAGGATE, restart evidence,
   v2 checklist, one preopen change and one Monday re-pin. Journal COMPLETE only
   after all mandatory proofs, naming any accepted UNEXERCISED items explicitly.

Literal checkout commands in step 3 (REPO and APPROVED_SHA supplied only by the
verified release, root runner; imports do not read the root env):

```bash
sudo -u trader git -C "$REPO" switch --detach "$APPROVED_SHA"
sudo -u trader "$REPO/.venv/bin/python" -m pip install --no-deps --disable-pip-version-check -e "$REPO"
test "$(sudo -u trader git -C "$REPO" rev-parse HEAD)" = "$APPROVED_SHA"
test -z "$(sudo -u trader git -C "$REPO" status --porcelain)"
sudo -u trader "$REPO/.venv/bin/python" -c 'import pathlib, project_mai_tai; p=pathlib.Path(project_mai_tai.__file__).resolve(); print(p); assert str(p)=="/home/trader/project-mai-tai/src/project_mai_tai/__init__.py"'
```

Feature settings (must match the final reviewed RPG1/COLDSTART1 head):

```text
MAI_TAI_MARKET_DATA_SUBSCRIPTION_STARTUP_ENABLED=true
MAI_TAI_STRATEGY_SCHWAB_1M_V2_ATR_REPRICE_HANDOFF_ENABLED=true
```

The revised RPG candidate adds the second switch to v2's admission of NEW
handoffs. Its code default is false; this install explicitly turns it ON.
Existing durable ownership continues draining under the safety gates when OFF;
it must not be abandoned by a rollback switch. This new setting and its OFF
behavior are part of the new head's independent review, not the old approval.
Verify explicit process env plus effective Settings(_env_file=None),
using only that PID's environment with identity checks on both sides of the read.
COLDSTART is checked on all five consumers; RPG is checked on its v2 reader.
Also record both env keys in the new OMS environment without claiming the
RPG admission switch is an OMS reader when it is not.

Any failed stop, timeout, broker UNKNOWN, failed startup, unexpected identity,
new traceback, memory-margin stop or eviction triggers the trap: capture real
states, stage/last successful command, page result and raw paths; mark INCOMPLETE.
Never silently restart stopped producers into mixed code, advance to later
services, roll back, repair state or repeat an attempt. Operator continuation
is required. Keep the install attended through the final snapshot. Row-47
CancelledError on a stopped orb-schwab is investigated by code/design; neither
reset-failed nor ignoring exit1 is automatically authorized by this plan.

## 4. Bounded proofs and denominators

Installer Redis reads: INFO stats/memory (<100 KB), owner HGETALL (<1 MB),
heartbeat XREVRANGE COUNT25 (<1 MB), subscription XRANGE COUNT1 (<1 MB per event,
at most 1000 entries over the install; overflow/trim/malformed = UNKNOWN).
Capture the pre-stop subscription tail once via XREVRANGE COUNT1. Match each
startup event by consumer and produced_at after the corresponding new process
start; old retained records do not qualify. No snapshot-batches payload read,
XINFO STREAM or Lua snapshot inspection by installer. Small reply bounds must
be checked by the implementation; a client post-read byte assertion alone is
not a server-side memory guarantee. Check HLEN and the seven known HSTRLEN
values before HGETALL (five consumers + two metadata fields); an unexpected
field/count or total >1 MB refuses. Producer payload limits and small-event
stream contracts must be checked in the runner review; never apply these
heartbeat/owner bounds to snapshot-batches. Unexpected large owner/heartbeat
payload is a refusal, not permission to increase limits.

The strategy's existing warm-up still reads up to120 snapshot payloads
(historically about660 MB); this is the known application read, not duplicated
by the installer. Before/after and during it require evicted_keys unchanged,
used_memory <=1,600,000,000, all five owners + `_migration_complete=1`, valid
checkpoint, and no disappearing preexisting stream. Keep the 1.6 GB margin.
A transient crossing is UNKNOWN/STOP even if memory later recovers; no automatic
retry or approved continuation is inferred from the October3 exception.

COLDSTART proof: 5/5 fresh replace announcements within60 seconds of each
consumer's actual new service start, with start-to-announce wall time reported.
A failure to prove within60 seconds is UNKNOWN and stops the install for
review; do not move the clock past slow initialization to get PASS. For each,
compare actual set with read-only restored/current input evidence, ensure other
four sets unchanged by that event, then prove final owner hash equals latest
five announcements. Empty is valid; missing is not. Check paper cap <=16 and
weekend-empty. Require healthy gateway heartbeat newer than last announcement,
age<30s, active_symbols exactly size(union of five sets + configured static).
Gateway PID/start/NRestarts/InvocationID must remain unchanged throughout.
This is a five-consumer warm-restart proof. The separate empty-Redis cold-boot
scenario remains unit-tested/live-UNEXERCISED; do not clear Redis to demonstrate it.
No event replay to manufacture this evidence. No trading ticks on Sunday is
UNEXERCISED by design, not a delivery failure or live-trading PASS.

v2 startup and close-out: new PID/start, active/running, NRestarts0, zero traceback
in bytes after old process exit. Record exact BOOT-HOLD reason and population.
Empty population: `held, expected by design, population=0`; never PASS/released.
Once nonempty Monday, Codex-2 checks REST warm-up/feed lines and literal release;
claude-1 owns Monday07:10 bar continuity. Unresolved hold at entry window is
investigated/page, not forcibly released. Window settings read from running v2
and OMS must match the reviewed F1 behavior, not installer 09:30/15:45 literals.

Loaded retained values: v2 AND OMS notional600/300/max1000; OMS NFQtrue/10000ms,
reactive premarket2000ms, target5.0/hardstop8.0/floorfalse, RETRY_ONEtrue,
EOD transitiontrue/OVERNIGHT_FLATTENtrue/CW target-staytrue; ORB livetrue,
observefalse; strategy polygon_30sfalse; unchanged gateway retention120.
Install isolated expected_flags_check.py/expected_flags.json/expected_numeric.json
from exact APPROVED_SHA, back up and compare Git blob/file hashes.
Numeric expectation remains8/8 unless reviewed RPG settings add numeric readers.
Combined FLAGGATE baseline134 (126 boolean +8 numeric readings) plus COLDSTART
five +RPG one =140 (132 boolean +8 numeric). Derive and freeze exact
denominators from the FINAL merged catalogs in release.json, then require ALL
entries match. Any difference from140/140 plus numeric-only8/8 needs review;
do not quietly change the expected denominator to fit a partial result.
Read and print actual rc, source(env/default), per-service values and Final call.

## 5. Re-pins and Monday delivery/scanner checks

Capture a fresh complete restart-evidence snapshot before this install and a
snapshot-relative record classifying EVERY monitored unit. Restarted: oms,
strategy,v2,orb,orb-schwab. Untouched: gateway,control,market-capture,reconciler,
tv-alerts(intentionally inactive). Paper is restarted and journaled separately
if not monitored by that checker. Validate against the actual service list;
an omitted/unknown unit is UNKNOWN. Update the preopen snapshot/record paths
and restarted arguments together, preserving the checked three-way routing.

One backed-up preopen replacement: date2026-10-05, final APPROVED_SHA, new
OMS/strategy/v2/orb/orb-schwab pins and unchanged gateway2907/start. Preserve
trader ownership0700, bash-n, full diff/hash. Do not run the date-fixed full gate
on Sunday as proof of Monday readiness; run the component checks and preserve
their actual results. Monday06:20 as trader reads the final state, no force-green.

Back up/re-pin existing Monday start script once: final APPROVED_SHA, new paper
PID/start, unchanged gateway2907/start and /proc path. Re-check unit/source hashes,
bash-n and diff. Timer date stays2026-10-05 03:40ET; no Sunday guard launch.
Capture `systemctl list-timers --all` showing NEXT and ensure exactly one timer.
The existing failure action stops ONLY paper and pages. Do not reuse expired
guard@2026-10-02 or clear its history. Guard checks sampler + RedisSafety before
07:00, full treatment signals from07:00. Paper prepares03:55/streams04:00 itself.

Mandatory scanner validation (Codex-2 produces evidence; claude-1 independently
checks), with raw paths and valid-minute denominators:

1. Read actual strategy scan interval and compute squeeze_10min_needs using
   MomentumAlertEngine, not a guessed120. Require needs<=120 at live5s. Record
   retained history count using XLEN only. After Sunday restart require log
   `prefilled momentum alert history from N snapshot batches` with N>=needs
   when that many populated batches existed. Unlike the empty resize boot,
   do not excuse a short warm-up automatically on this running gateway.
2. Count 5-minute and10-minute squeeze alerts in the predeclared Monday
   04:00-04:15 ET window and compare the SAME clock interval against the prior five sessions:
   Sep28,29,30,Oct1,2. Report each day's count/observed eligible minutes, baseline
   range/median, live scanner population and gap minutes. Do not move the
   measurement window to hide a late start. If coverage is absent, UNMEASURED;
   add a separately labelled 07:00-07:15 entry-window check, not a replacement.
   No missing log = zero.
   Zero Monday alerts with nonempty eligible scanner input, or counts outside
   baseline range, triggers code/data investigation, not an invented pass/fail
   threshold or automatic rollback. Cross-check alert persistence and emitted
   IDs so duplicates/replays are not counted as fresh alerts.
3. Count distinct watchlist ADD events in that first15-minute window and the
   whole Monday session versus the same five sessions. Distinguish total adds,
   unique symbols and re-adds; historical gaps = UNMEASURED. Publish provisional
   first15 result then the session total only when the session ends.
4. Require0 new strategy ERROR/traceback since restart and in the Monday window;
   inspect warnings and timing continuity too. Use strategy.log post-start byte
   range/rotation-aware timestamps plus persisted alerts/watchlist evidence,
   never bulk snapshot reads. Raw report paths and queries go in the journal.
5. Monday03:40 guard,04:00 paper and first-nonempty owner/tick delivery,06:20
   gate,07:10 v2 bar read are separate observed checks. RPG cancel-to-new-order
   latency per broker, duplicate count and partial-fill safe ending stay
   UNEXERCISED until real reprices/fills. Report sample size and guard reasons;
   no fabricated weekend orders or guaranteed venue latency from unit tests.

## 6. Completion and outstanding review items

Return journal path, release/approval hashes, both merges and final SHA/tree;
six new and six untouched identities with starts/NRestarts; flat/order/token
results; v2 preflight/clock override; six post-start log/identity checks plus
the service-specific heartbeat evidence (paper/ORB-Schwab exception above);
five startup event IDs/sets and gateway union; Redis before/peak/after with
unchanged evictions; explicit /proc flags and numeric/FLAGGATE exact denominators;
BOOTHOLD/warm-up disposition; catalog hashes; single preopen and Monday script
diffs/hashes/NEXT; accepted unexercised gaps and Monday owners. Say INSTALL
COMPLETE only when all mandatory evidence is present. Preserve current INC1
coverage gaps; installing code does not install its pager sources.

Before this draft can become executable: resolve RPG review findings and pin
the final RPG head including its new switch, review combined tree/merge order, resolve broker-order
enumeration and intent-drain/idle dispositions, commit and test
literal runner/approval validator/proofs/re-pin helpers and named unit/timer,
stage exact hashes, then obtain final release approval. No such execution
approval is inferred from this drafting request. No production write occurred.

Draft validation: the two embedded Bash blocks parse with `bash -n`.
The live host's read-only `systemd-analyze calendar --iterations=3` resolves the
proposed timer to 2026-10-04 14:00/14:01/14:02 UTC (10:00/10:01/10:02 ET).
This verifies calendar syntax, not an installed or scheduled job.
