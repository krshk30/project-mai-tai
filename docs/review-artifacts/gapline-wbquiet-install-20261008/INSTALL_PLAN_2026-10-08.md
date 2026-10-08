# October 8: GAPLINE / WBQUIET / LINESRC2 / ORBLIVE install mechanics

## Authority, Exact Candidate, and Current State

Lane C is the sole writer of this directory. This is an isolated tested
mechanics package, NOT a staged job, install approval receipt, or COMPLETE.
No box write, timer staging, deployment, order, DB/Redis mutation, pin or merge
is permitted before the close. Parent owns shared handoff reporting.

Operator standing GO covers reviewed work after the close when the real
trading gates pass. Final APP must be the exact reviewed main landing AFTER
LINESRC2 #1127, plus lane A only if independently pinned and merged. Never
substitute a moving main, a draft, or an old morning flatness receipt.

- GAPLINE #1126: reviewed head 00bd0c64325ec25c11642d913c4a0ae2b35aad0f;
  landing 1ed10831508e9a251aa3440a973f49c8bb2c18c9.
- WBQUIET #1124 and ORBLIVE lane 1 #1125 landed as reviewed UNION #1129:
  head 2e848426d398adbfd1816c087617360d1b8af740, main
  1e15adb03c3758e647d333828bbe3090e24e832e. Original cherry-picked WB heads
  are not required ancestors. The union's whole tree and binary diff must
  equal the pinned union at its landing; both source PRs are required.
- LINESRC2 #1127: final pin and merge are still required. No final APP or
  manifest/approval hash is asserted while that landing is unavailable.

`make_release.py` requires full SHAs, explicit candidate-review JSON, the
committed review-pins ledger, exact landing trees/diffs, actual independent
pin verification, and review/Validate provenance. Required landings are
#1126, #1129 (containing #1124 AND #1125), and #1127. The final APP must itself
be a named reviewed landing. Missing/changed content refuses release generation.
It embeds the final APP's official restart collector as a hashed artifact,
not the old box collector whose default population still includes paper ORB.
Every job blob is read from the plan COMMIT, not dirty working files.
The review receipt also requires `box_baseline`: exact box SHA, actual
capture timestamp, authorization source, and all thirteen identities from
`proof_readonly.py baseline`. Release refuses missing fleet/start fields;
runner compares those actual baseline identities and refuses drift rather
than adopting a later arbitrary process. Re-read before the final write.

## Literal Sequence

First remote staging/write is October 8 AFTER 16:00 ET. Held bot positions
wait for their close, never for a fixed wake-up time. Native repository gates
remain unchanged. No clock override, migration, ledger write, snapshot
unarchival, trading-code edit, rollback or automatic recovery is included.
After the first application write, clock advancement alone does not abort.

1. Generate package from the committed plan and exact final APP; publish an
   immutable release ref pointing to that APP. `stage.py --check-only` is
   local only. Real staging verifies every byte and refuses before close or
   on another date BEFORE making any remote directory/unit write. It installs
   only the named dated one-shot/timer, never directly starts the installer.
2. Runner verifies manifest, approval, candidate-review binding and every
   artifact; takes the nonblocking deployment lock and an exclusive attempt.
   Source check requires clean exact box SHA, exact immutable fetched APP,
   reviewed source hashes and the baseline deploy-script hash. No force reset.
3. Capture actual whole-fleet identities, process flags, log offsets, bounded
   Redis owners/marker/evictions/memory, PostgreSQL counters, and the official
   snapshot using the staged FINAL-APP collector. Its default fleet excludes
   retired paper ORB. Do not adopt an old ORB PID into that fleet.
4. Fresh trading gate immediately before first write; claim the attempt.
   Back up env and box catalog with hashes. Change ONLY GAP_LINE_CARRY=true
   and LINE_CHART_RESTORATION=true; RPG handoff remains false. An operator
   LINE veto requires an explicitly false, regenerated manifest; no implicit
   waiver. Preserve all other env bytes, RETRY_ONE=true/MAX_RETRIES=0, and
   archived snapshots. Box catalog becomes the reviewed complete inventory,
   with GAP/LINE expectations true and RPG false. Removed paper ORB Settings
   and ownership rows are not retained by appending to the stale inventory.
5. Fresh gate; deploy OMS using the repo's literal command below. The repo
   stops strategy, runs native OMS preflight, restarts OMS, then starts
   strategy. No extra strategy restart is added. Bootstrap may refresh units
   and derive orb-paper.env; its before/after hashes are recorded. Schema
   migration is explicitly disabled.

   ```sh
   sudo -u trader env MAI_TAI_EXPECTED_SHA="$APP" MAI_TAI_RUN_MIGRATIONS=0 \
     MAI_TAI_ALLOW_LIVE_RESTART=0 bash /home/trader/project-mai-tai/ops/systemd/deploy_service.sh \
     /home/trader/project-mai-tai "$IMMUTABLE_RELEASE_BRANCH" oms
   ```

6. Fresh gate before EACH remaining target, same literal command with
   `schwab-1m-v2`, then `orb-schwab`, then `control`. Preserve each native
   identity/health check. No permission-mode, exact CancelledError signature,
   control-page ownership, or arbitrary log-signature self-stop is added.
   A clean systemd stop is proof. A genuine failed start remains a stop.
7. Fresh gate, capture actual five-consumer owner hash/marker and paper ORB
   identity. `systemctl disable --now project-mai-tai-orb.service`. Publish
   EXACTLY ONE normal COLDSTART empty `consumer_name=orb, mode=replace`
   using final APP `OrbService._sync_gateway_subscription([])`. No raw HDEL,
   DEL, hardcoded symbols, second producer, or restart of another unit.
   Require paper ORB disabled/inactive/PID0, empty owner tombstone, valid
   Redis applied cursor >= OUR request ID, and byte-identical other four
   owners and migration marker. An unrelated older cursor advance does not
   prove our event, even if ORB was already empty. A publish-started receipt
   forbids a resend following a timeout; no implicit recovery.
   Socket/read/XADD waits are bounded; failure never re-publishes implicitly.
8. Require five actual new active/NRestarts0 PIDs/starts; exact checkout,
   requested flags and preserved live settings. Compare untouched services;
   paper ORB has its separate positive retirement proof, not an unchanged
   identity claim. Fresh trading read, then ten-minute observation for each
   new process; read live AND rotated files and new-PID journals. Record
   errors/tracebacks, WB shadow-per-sync coverage, sync ms/DB tx/s, scanner
   status, actual bar-hole minutes and Redis before/after. Incomplete telemetry
   and after-hours coverage remain UNKNOWN, never manufactured PASS.
9. Run real boolean and numeric expected_flags_check on live /proc. Require
   ZERO mismatches; only by-design inactive momentum-paper UNKNOWN may remain.
   No legacy ORB mismatch waiver or paper restart to get green. Actual reviewed
   inventory determines denominator; counts are reported, never guessed.
10. Seal actual action journal and whole-fleet install record bound to the
    official pre-snapshot. Restarted group EXACTLY OMS/v2/strategy/orb-schwab/
    control; retired paper ORB is separately bound in retirement receipt.
    Re-pin daily preopen SHA, all five PIDs/starts, SNAPSHOT/INSTALL_RECORD,
    actual expected flags for that group, and runtime dependency hashes.
    Remove ONLY paper ORB identity declarations/check, preserve untouched
    identities and dynamic date/report/paper shape. Old orb-schwab Redis
    upgrade ACK is preserved as history and retired as CURRENT admission;
    new authorized active/NRestarts0 identity is pinned normally. Root mode,
    adapter hash, official wrapper and all required evidence remain bound.
    Backups and atomic writes, runtime last; no fake daily PASS. `bash -n`
    only, no early run of tomorrow's gate. Existing daily timer is unchanged.
11. COMPLETE only after measured post-checks and re-pin receipts. Seal once;
    no second installer. ABORT pages actual stage/rc/states/log paths and starts
    nothing. No partial deploy retry or unreviewed recovery. Parent posts C-row.

## Trading Gates and Retry Policy

| Gate | Requirement | Failure handling |
| --- | --- | --- |
| Both brokers | Fresh direct complete account-bound positions, flat BOT positions | Holding waits before write; measured blocker stops after write |
| Operator holdings | ZERO session bot orders AND fills, not net-zero round trip | No generic waiver of bot holdings or unowned SELL |
| Working orders | Complete fresh Schwab and Webull/list-today responses | Any working/unknown order blocks |
| Managed/virtual book | Zero open managed and nonzero virtual rows | Never waive |
| Intent/census | No in-flight/unconfirmed dispatch; positive terminal local-abort proof | No age clearing, DB edit or archived-row change |
| Native repository gates | Executed unchanged by deploy_service | Preserve exact refusing output |
| Starts/proc/Redis | New active PIDs/NRestarts0, requested switches, no new eviction | Stop/page; no recovery |

Every gate receipt preserves stdout AND stderr, rc and hashes. ONLY unreadable
rc2 retries: at most three attempts 60 seconds apart. Measured rc1 never
retries inside a service step. Before first write measured work waits for a
later timer tick; an exhausted unreadable source stops. No new broad allowance.
MI180/NXL2 decided discrepancies remain unchanged; no ledger write.

## Packaging and Evidence Contract

Standalone Python helpers require final APP installed dependencies (shared
project venv: redis/SQLAlchemy/Settings), native systemctl/journalctl/git/bash,
and approved repository source. Runner strips inherited MAI_TAI overrides and
TZ; ET is explicit only in window calculations. The helper imports are all
included in manifest (including retire_orb.py). Official snapshot collector
comes from exact APP. Immutable dated package:
`/home/trader/after-hours/2026-10-08/gapline-wbquiet/job`.

```sh
python job/make_release.py --plan PLAN_SHA --approved-sha APP --box-sha BOX \
  --review-receipt candidate-review.json --review-ledger REVIEW_PINS_WORKTREE \
  --output-directory EXCLUSIVE_LOCAL_OUTPUT
python job/stage.py --plan PLAN_SHA --approved-sha APP --box-sha BOX \
  --review-receipt candidate-review.json --review-ledger REVIEW_PINS_WORKTREE --check-only
```

No valid candidate-review receipt is fabricated while #1127 is unmerged.
The named dated timer is `project-mai-tai-gapline-wbquiet-20261008.timer`;
October8 minute checks 16..23 ET, Persistent=false. Neither committed units
nor a check-only package is evidence of a box timer being installed.

Final receipt: plan/manifest/approval/artifact hashes, actual start/end times,
five new identities/proc flags, untouched identities, retirement request/hash,
all gates/raw receipts, flags/numeric outcome, ten-minute logs/journals and
file paths, Redis/DB/scanner/bar continuity, preopen diff/backups/hash/runtime
bindings, and COMPLETE or exact ABORT. Tomorrow scanner/live outcomes remain
UNMEASURED until observed.

## Read-Only Baseline and Honest Limits

October8 09:14:49 ET bounded read via `ssh mai-tai-vps sudo`: clean box
`d244f602491e5d316a05670b205315466aed2a4d`. Actual states:

| Unit | PID | Start UTC | NRestarts |
| --- | --- | --- | --- |
| oms | 1408231 | Oct7 22:12:50 | 0 |
| v2 | 1941193 | Oct8 12:58:11 | 0 |
| strategy | 1408242 | Oct7 22:12:50 | 0 |
| orb-schwab | 765206 | Oct7 06:31:14 | 1 (historical upgrade ACK) |
| control | 1464286 | Oct7 23:43:00 | 0 |
| paper ORB | 1322003 | Oct7 20:14:37 | 0 |

All active at that read. Earlier v2 PID1895743 and morning assumptions are
obsolete. Parent must bind the authorized 08:58:11 restart acknowledgement
and re-read actual after-close baseline; this observation is NOT an install
approval, flat proof, or permission to adopt arbitrary changed identities.

Prior08:38 clear gate and08:17 AIXI holding receipt remain historical morning
evidence only. Latest local mechanics results and mutation receipt accompany
the final package commit. Parent's retained exact-main full-unit baseline is
1e15adb0: 48 failed /7674 passed /55 skipped, failed-name hash
e0b9fc7478ff484d88d9882194d3525632c0d2d43353d515b28f63a28464e9a9.
No duplicate baseline is run and no global parity claimed without comparison.
