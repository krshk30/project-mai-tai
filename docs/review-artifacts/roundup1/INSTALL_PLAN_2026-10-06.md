# ROUNDUP1 And Daily Preopen - October 6 Window

**DRAFT; application target not yet bindable.** This records the reviewer/operator
instruction, not a staged runner or a scheduled application install. No session
production write. First stop only after 20:00 ET October 6, observed rotation,
fresh flat/working-order gates and a reviewed exact application SHA.

## Current Refresh - 08:16 ET

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
has just started; fresh reviewer pin/mutations and Validate x2 remain required.
Do not reuse the old6727fff5 pin or call this merged/staged. After the fresh pin,
merge the exact head and bind the real resulting main SHA before execution.

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

Before execution, replace the pending target with the verified actual merge
SHA and record its tree, exact refreshed head/pin and both Validate results.
Rerun the seven-key composition on that tree. No unreviewed source or default-OFF
LINE=CHART draft is included. No file is staged on the box by this draft.

- Application processes changed: **v2 and OMS only**. Strategy, gateway, ORB,
  orb-schwab, paper, daily guard, control, capture, reconciler, Redis and PostgreSQL
  are not restarted. OMS needs the flag for PA1 resubmit rounding and wire cap.
- One env key changes to true:
  `MAI_TAI_STRATEGY_SCHWAB_1M_V2_RESTING_BUY_ROUND_UP_ENABLED`.
  Retain all four October 5 switches, NFQ, GAP_HOLD, sizing600/300/1000,
  target5/stop8 and existing ORB flags. No other switch or trading rule edit.
- Schema0022 is already installed: verify it; do not rerun a migration.
- Catalog expectation is FLAGGATE148; numeric remains8. Verify the real measured
  denominator/result, including UNKNOWN rows; never start paper to force green.

## Literal Runner Requirements Before Staging

Use the October 5 tested discipline: release bound to plan/app SHA and every
artifact hash; named one-shot/timer if scheduled, approval gate, exclusive
attempt directory, nonblocking deploy lock and attended sequence. Publish the
literal bytes and hashes before any production call; this draft is not that runner.

All read-only gates run before the first write: clean exact box/source binding,
ancestry/path allowlist, stable full census, strict fresh broker/books flat,
working orders/in-flight intents, reviewed ticket census, Redis/owners and
unmodified OMS/v2 restart preflights. Standing MI/NXL and off-hours admissions
retain their reviewed exact shapes and audit lines. Broker UNKNOWN gets at most
three reads60s apart; a measured blocker stops immediately. No bulk Redis stream
reads or unbounded broker histories.

Back up source/env/catalogs/gate with hashes and duplicate-key refusal, advance
only to APPROVED_SHA, edit the one env key, take old log offsets and restart
snapshot. Fresh flat and working-order proof before every stop/start; require
the unmodified v2 gate immediately before stop, zero armed, no automatic override.
Scoped sequence: **stop v2 -> stop OMS -> start OMS once -> start v2 once**.
No clock abort after the first stop. Abort trap pages actual states and starts
nothing; no rollback/recovery/extra restart is pre-authorized.

Verify new identities/starts, active/NRestarts0, no new-process tracebacks and
no startup buy. Read ROUND_UP and all retained switches by key from both new
OMS/v2 `/proc`; verify all untouched identities. Redis evictions unchanged,
memory within bound and owners five consumers plus migration marker. Re-read
ticket dispositions and preserve proof-dependent unknown ownership. Run pinned
isolated FLAGGATE/numeric catalogs and record their actual outcomes.

After-hours bar continuity is **N/A, no bars expected after20:00**, not PASS.
Quote log source paths/offsets after observed rotation. Report literal BOOT-HOLD
and warm-up population/release evidence, with next-session07:00 verification
owned by Codex and bar continuity independently checked by claude-1. No strategy
restart means no fabricated scanner-restart validation requirement; scanner
health is observed without restarting it.

## Single Close-Out And Daily Gate

The reviewed date/paper shape in
`../preopen-daily/PLAN_2026-10-06.md` remains exact. Re-pin once to the final
application SHA and new OMS/v2 identities; strategy and untouched identities
remain constants. Remove non-restarted-service `--expect-flag` arguments from
the restart-only group; their flags remain checked by the catalog.
Paper is not assigned a constant PID: active/NRestarts0, start >= today's03:40ET
and >= active daily guard start. Date/report comes from today's ET box clock.

Build/test/install the daily preopen mechanics only after20:00, in this same
window: root `project-mai-tai-preopen.service`, weekday06:20ET timer with
Persistent=false and existing-adapter failure unit. Start **timer only**, not
the gate service. No future-dated real gate run tonight. Backups, diff, mode700,
bash syntax, manifest and gate hashes; actual NEXTWed10-07 06:20ET receipt.
Wednesday first real timer invocation is the rehearsal; claude-1 retains06:22
hand verification until green. Journal `deployments-20261006.md`, raw runner
hash, flags/numeric, Redis, gate diff/hash and final identities before COMPLETE.

Rollback, if later authorized: ROUND_UP=false requires coordinated OMS/v2
restart and a matching catalog. This line is not rollback authorization.
