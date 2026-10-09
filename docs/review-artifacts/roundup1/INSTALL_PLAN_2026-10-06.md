# ROUNDUP1, LINE=CHART And Daily Preopen - October 6 Window

**Application bound; literal release not yet staged.** APPROVED_SHA is
`3ebde364d4634fdad45992e2ab1cbdf43ffeb221`, tree
`972271218693e095f99257e0e39ab525090fd0ef`. Its whole tree equals pinned
LINE=CHART head `65010dffffaa28c5f6ae22df0c48cf9358a6bdbf`.
ROUNDUP1 is already merged as `a7fed34d97732e1c1379ec77d89fd83f886ce2d8`;
#1097 merged after it at 10:53:42 ET October 6. Hosted independent-review-pin
and both Validate runs were PASS on that exact head before the non-admin
matched-head rebase merge. Five committed pin records independently cover all
reviewer/Codex segments. Whole-tree equality is verified; production remains
`7823a6fa7f63649b3f75ae3bcd07e16dc9b5dfaf`, not this new main.

## Three-Item Set - October 6 Merge Receipt

Only ROUNDUP1, LINE=CHART Restoration and daily preopen mechanics are in the
current candidate. Reviewer-approved latest set: all eight installed switches
ON, FLAGGATE149. The pre-install composed run is
`tests/unit/test_all_on_pm.py`: **41/41 PASS**, 2.53s, on the identical
pinned/merged tree. Raw `/tmp/codex-1097-merged-eight-switch-20261006.log`,
SHA256 `d37059c4e19a2869c60bb805af9d662a743e11956149e0ecc885bcb245c24eb4`.
This is a controlled completed-worker composition, not a substitute for the
real-factory historical proof. The unchanged reviewer acceptance also passes
123 cases on eight switches, 0 line mismatches/errors/incomplete buys or
permissions; +10=98 MATCH,25 n/a,0 HELD. Full source/evidence scope is recorded in
`../line-chart-restoration/REVIEW_DELIVERY_2026-10-06.md`.

T43 one-leg recovery/reject-text is NOT in this SHA. If independently pinned
and merged before the window, bind a new exact application SHA/tree, update
scope/restart list, rerun its composition and regenerate the literal release
before staging. Do not silently substitute moving main. Otherwise T43 waits
for the next reviewed window. Issue3 is assessment only. Pause classification
and ORB-purple rule are excluded.

The operator's October 6 after-close ruling supersedes the fixed evening window.
First stop is **strictly after 16:00 ET October 6, whenever ready and all gates
pass**, expiring at midnight ET. A position waits for its actual close, not for
a scheduled clock time. Log rotation is not an admission gate. No production
write or application restart during the session. The Codex readiness wake-up is
not a staged/listable box job; name any eventual box unit and publish its bytes.

## Earlier ROUNDUP1 Refresh - 08:16 ET

Reviewer-authorized rebase/test refresh is pushed as
`fa54579b4794d5fc6b275ab3233c0161d51110c7`, based on exact main03b26293.
All ten rebased commits are patch-equal. Diff from6727fff5 under `src ops scripts`
is empty. The sole follow-up file is `tests/unit/test_all_on_pm.py`: ROUND_UP
is true in the shared seven-key fixture; the first/reclaim parametrized PMREST
check uses exact8.28 and8.01 for its two recorded moves, no tolerance, with the
requested ceiling comment. Accounting, no-emission and no-handoff checks stay.

Actual composed run **36/36 PASS**,2.45s. Composed plus ROUNDUP **456/456 PASS**,
5.31s. Ruff/diff-whitespace PASS. These supersede the prior34/36 composition
failure below; source was not changed to satisfy the tests. CI on the new head
was subsequently green on both runs. Fresh pin record `a024c810` covers the
exact refreshed head; the stale record was retired. Merge completed at08:26ET
on October6 and the exact application/tree binding above was independently
read from Git. The ROUNDUP1 SHA/tree in that earlier receipt is now an ancestor
of the three-item application above. This is a repository merge, not an
installed application claim.

## Historical Preparation Blockers - Superseded By The Refresh

At 08:08 ET, #1095 is the exact pinned head
`6727fff57fa00cf2d8a7194097f3b2fd5fc024d7`. Local committed pin verification
PASS (review-pins `d874fe5d`), hosted independent-review-pin PASS and both
Validate PASS. The ordinary local ROUNDUP + ALL-ON files pass 456 tests.
Main is `03b26293624f87e43eb167431fc308b1aec309b3`; its delta from the PR's
application base `7823a6fa` is three docs-only paths.

The non-admin exact-head rebase-merge refuses:

```text
Pull request krshk30/project-mai-tai#1095 is not mergeable:
the head branch is not up to date with the base branch.
```

No admin bypass, Update branch, pinned-head rebase, merge, application change or
production action was performed. A clean docs-only rebase requires a new exact
head pin before another merge attempt; the original pin cannot be carried over.

The shared `test_all_on_pm.ALL_ON` dictionary has six keys and does not enable
ROUND_UP. A process-only pytest collection plugin adds ROUND_UP=true before
fixtures run, leaving the pinned files unchanged. Actual seven-key result:
**34 passed / 2 failed**. Both failures are
`test_all_on_pmrest_software_move_owns_no_handoff_preserves_accounting[first/reclaim]`:
actual trigger 8.28 versus the old unrounded assertion 8.2792905. This is not
a green composed-run receipt. The test-only refresh must assert the reviewed
rounded trigger while retaining the slot, retry, no-handoff and no-emission
assertions; source must not be changed to satisfy the obsolete price expectation.

## Binding And Scope

Before execution, verify the literal release binds the actual merge SHA/tree
above and rerun the eight-key composition on that tree. No unreviewed source
is included. LINE=CHART source default remains OFF; this reviewed install turns
it ON alongside ROUNDUP1. No file is staged on the box by this draft.

- Application processes changed: **v2 and OMS only**. Strategy, gateway, ORB,
  orb-schwab, paper, daily guard, control, capture, reconciler, Redis and PostgreSQL
  are not restarted. OMS needs the flag for PA1 resubmit rounding and wire cap.
- Exactly two env keys change to true:
  `MAI_TAI_STRATEGY_SCHWAB_1M_V2_RESTING_BUY_ROUND_UP_ENABLED` and
  `MAI_TAI_STRATEGY_SCHWAB_1M_V2_LINE_CHART_RESTORATION_ENABLED`.
  Retain all four October 5 switches, NFQ, GAP_HOLD, sizing600/300/1000,
  target5/stop8 and existing ORB flags. No other switch or trading rule edit.
- Schema0022 is already installed: verify it; do not rerun a migration.
- Catalog expectation is FLAGGATE149 (141 boolean +8 numeric); numeric remains8. Verify the real measured
  denominator/result, including UNKNOWN rows; never start paper to force green.

## Literal Runner Requirements Before Staging

Local mechanics checkpoint: `job/runner_mechanics.sh` implements the dated
first-stop window, clock-only gate invocation and bounded rc2 retries. This is
a sourced component, **not a staged/executable full install**. Full actions,
dynamic preopen gate/timer and immutable release still require assembly and
testing before any box call. Do not substitute the October5 three-service/
migration runner: today's sequence has two services and no migration.

Local116-test control PASS (53 unchanged MI/NXL controls, exact-session/clock
and retry controls). Eleven in-memory policy mutations each fail an assertion:
session, flow, loop health, exceptions, streamer, enable, warm-up, population,
bar bound, heartbeat and afterhours anchor. Raw helper hash
`a4ee789e0fc516ebd6477632f0a2120ee1c306ec375e34eecd89d0ebdace11ae`;
runner-component hash
`d555d24f2da6ec52c0618373d9761d944be62b441d97325bdc50f8dc0f2950f9`.
These are local committed inputs, not on-box staged/rehearsal receipts. Shell
syntax and whitespace PASS. Seven-key composed rerun on the identical pinned/
merged application tree:36/36PASS,2.41s. Production remains7823a6fa.
That is the earlier seven-switch receipt; the new eight-switch proof is the
41/41 receipt above. Full literal runner/gate/timer release is still not staged.

Use the October 5 tested discipline: release bound to plan/app SHA and every
artifact hash; named one-shot/timer if scheduled, approval gate, exclusive
attempt directory, nonblocking deploy lock and attended sequence. Publish the
literal bytes and hashes before any production call; this draft is not that runner.

All read-only gates run before the first write: clean exact box/source binding,
ancestry/path allowlist, stable full census, strict fresh broker/books flat,
working orders/in-flight intents, reviewed ticket census, Redis/owners and
unmodified OMS/v2 restart preflights. Standing MI/NXL admissions retain their
reviewed exact shapes and audit lines. Broker UNKNOWN gets at most
three reads60s apart; a measured blocker stops immediately. No bulk Redis stream
reads or unbounded broker histories.

Back up source/env/catalogs/gate with hashes and duplicate-key refusal, advance
only to APPROVED_SHA, edit the two named env keys with duplicate refusal, take old log offsets and restart
snapshot. Fresh flat and working-order proof before every stop/start; require
the unmodified v2 gate immediately before stop and zero armed. Its existing
18:00 clock proxy cannot enforce the new operator window. Only before18:00,
use its documented `--clock-override` / `--i-accept-clock` with the literal reason
`Operator 2026-10-06 after-close ruling; after16:00, zero armed, zero rows, both brokers flat`.
Print/journal the gate's [OVERRIDE] line and all three green gate lines. This is
a clock-only admission under that ruling, never an armed-segment override;
any live armed set or other measured gate block waits/stops without a waiver.
Scoped sequence: **stop v2 -> stop OMS -> start OMS once -> start v2 once**.
No clock abort after the first stop. Abort trap pages actual states and starts
nothing; no rollback/recovery/extra restart is pre-authorized.

Verify new identities/starts, active/NRestarts0, no new-process tracebacks and
no startup buy. Read ROUND_UP, LINE_CHART_RESTORATION and all retained switches
by key from both new OMS/v2 `/proc`; all eight must be true in their shared env.
OMS uses ROUND_UP for PA1/wire prices; Restoration is enforced by v2's worker,
line admission and draft-version gates, not by inventing an OMS line reader.
Verify all untouched identities. Redis evictions unchanged,
memory within bound and owners five consumers plus migration marker. Re-read
ticket dispositions and preserve proof-dependent unknown ownership. Run pinned
isolated FLAGGATE/numeric catalogs and record their actual outcomes.

Between16:00 and20:00, bars still flow: measure the exact process downtime and
affected bar-minute interval, reporting any hole rather than claiming N/A.
Only an actual after20:00 restart with no scheduled/live bars may be labelled
N/A, never PASS. Track log device/inode/offset before stops; read the live file
and its rotated copy if copytruncate occurs during/after the proofs. Quote every
source path and byte range; rotation itself never delays admission. Report literal BOOT-HOLD
and warm-up population/release evidence, with next-session07:00 verification
owned by Codex and bar continuity independently checked by claude-1. No strategy
restart means no fabricated scanner-restart validation requirement; scanner
health is observed without restarting it. Claude owns tomorrow's07:00-07:15
scanner validation. Record Restoration's provider/read/worker completion,
per-symbol proof/version and entry_allowed literally; an incomplete rebuild
remains entry_allowed=0 with protective exits unchanged, never forced green.
Next-session live re-add/backfill delivery is future proof, not historical
replay PASS. Preserve no late historical flip, no resurrected canceled order,
no unconsumed consumed slot and existing missing-data hold behavior.

## Single Close-Out And Daily Gate

The reviewed date/paper shape in
`../preopen-daily/PLAN_2026-10-06.md` remains exact. Re-pin once to the final
application SHA and new OMS/v2 identities; strategy and untouched identities
remain constants. Remove non-restarted-service `--expect-flag` arguments from
the restart-only group; their flags remain checked by the catalog.
Paper is not assigned a constant PID: active/NRestarts0, start >= today's03:40ET
and >= active daily guard start. Date/report comes from today's ET box clock.

Build/test/install the daily preopen mechanics in this same after-close
window: root `project-mai-tai-preopen.service`, weekday06:20ET timer with
Persistent=false and existing-adapter failure unit. Start **timer only**, not
the gate service. No future-dated real gate run tonight. Backups, diff, mode700,
bash syntax, manifest and gate hashes; actual NEXTWed10-07 06:20ET receipt.
Wednesday first real timer invocation is the rehearsal; claude-1 retains06:22
hand verification until green. Journal `deployments-20261006.md`, raw runner
hash, flags/numeric, Redis, gate diff/hash and final identities before COMPLETE.

Rollback, if later authorized: ROUND_UP=false requires coordinated OMS/v2
restart and a matching catalog. This line is not rollback authorization.
Restoration=false requires a v2 restart and matching catalog; retain every
other key and read the actual resulting identities/line state. No rollback,
extra restart, ticket purge or recovery action is pre-authorized.

## Exact After-Hours Admission

Healthy/flowing needs no waiver. For a fresh <=120s v2 heartbeat only, degraded
is admitted in memory when raw/effective status are degraded and all documented
details match: `stalled_offhours_rest_dry`, healthy loop, zero loop exceptions,
connected streamer, enabled, positive integer watchlist with warmed==watchlist,
finite nonnegative bar age. From16:00 to20:00 the exact session is `afterhours`
(the source spelling), and bar age must be <= elapsed since16:00 today +300s.
From20:00 the exact session is `closed`, with the existing elapsed-since20:00
+300s bound. Reject regular, premature/wrong session, stall starting before its
anchor, stale/future heartbeat, bad field or any other degraded service.
Print every detail, chosen anchor and bound in [STANDING-ALLOWANCE]; preserve
the raw evidence and adjust only the input copy. No lower health threshold,
trading-code edit or MI/NXL allowance change.
