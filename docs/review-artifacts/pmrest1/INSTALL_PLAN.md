# PMREST1 + PMPRINT1 + PMFLIP1 - single v2 evening install review plan

REVIEW REQUIRED; NOT install authorization. Exact head pin, merge and operator
exact-SHA GO required. Intended window Monday 2026-10-05 after 20:00 ET and the
observed log rotation. No unattended install is scheduled by this document.
Only `project-mai-tai-schwab-1m-v2.service` may restart, once. No OMS/strategy/
gateway/orb/orb-schwab/paper/control/reconciler restart, migration or watch install.

## Preflight

Bind BOX_SHA=e1ce3b3978fbcb1ff00daecdf6dc2618e6b5fb89 and APPROVED_SHA to the
operator-named final merge SHA, not a moving branch. Merge order: #1091 PMREST1
(merged a80b5181), then the independently pinned combined PMPRINT1/PMFLIP1 PR.
If that PR is not pinned in time, install #1091 alone using its original plan
at 1135f8d9, one switch and denominator 141; do not install unpinned work.
This folded plan is for the three-card final SHA only. Require a clean box,
ancestry, exact reviewed delta and no concurrent deploy. Refuse any executable
delta beyond settings, v2 strategy, services/schwab_1m_v2_bot.py,
strategy_core/entry_gate.py and the expected-flags catalog; docs/tests
are allowed. Record every service PID/start/NRestarts before any write. Verify
editable import resolves to this checkout. If box identities moved, STOP for
review, do not silently re-pin.

Immediately before checkout/environment writes and again before the v2 restart,
perform fresh direct broker position AND working-order reads on both live
accounts (`live:schwab_1m_v2` and `live:orb`). Require both broker accounts flat,
no working orders, zero managed rows and zero nonzero virtual positions, with
fresh readable evidence. SQL runs read-only. No historical NXL manual-close
exception applies on October 5. UNKNOWN or exposure = STOP, no state edit.
Do not rely on a cached account_positions zero or a logs-only flat assertion.
Capture direct read request, reply size, timestamp and working-order IDs.

Run the repository restart preflight immediately before the restart, with
pipefail and the real exit code; it must return zero:

```bash
sudo /home/trader/project-mai-tai/ops/preflight/preflight_v2_restart.sh
```

Armed state blocks: ask the operator for the exact live set and Bug-2 acceptance;
no pre-authorized armed override, retry loop or automatic read-and-override.
Unreadable/stale state also blocks. No clock override needed after 20:00.
All Redis reads are bounded; never bulk-read snapshot-batches.

## Backups and one restart

Record SHA256/ownership/mode and preserve exclusive backups of the primary env,
isolated FLAGGATE catalog and `/home/trader/preopen.sh`. Append exactly one
each of these three keys, with exactly one true definition, to
`/etc/project-mai-tai/project-mai-tai.env`; refuse duplicate definitions. No other
env line changes:

```text
MAI_TAI_STRATEGY_SCHWAB_1M_V2_PM_REST_REPRICE_ENABLED=true
MAI_TAI_STRATEGY_SCHWAB_1M_V2_PM_PRINT_ASK_CONFIRM_ENABLED=true
MAI_TAI_STRATEGY_SCHWAB_1M_V2_PM_FLIP_WAIT_ENABLED=true
```

Reclaim remains OFF, amounts 600/300, band/offset/reprice 0.5.
Advance the production checkout to the exact APPROVED_SHA only; clean-tree and
import-path checks repeated. Do not use deploy_service.sh (companion restart).
After the second flat/working-order proof and passing preflight:

```bash
sudo systemctl restart project-mai-tai-schwab-1m-v2.service
sudo systemctl is-active --quiet project-mai-tai-schwab-1m-v2.service
sudo systemctl show project-mai-tai-schwab-1m-v2.service \
  -p MainPID -p ActiveEnterTimestamp -p NRestarts
```

Remain attended. Failure after a write/restart: STOP, page the actual states and
seek instructions. No rollback or extra restart is pre-authorized. After-hours
restart creates no bar hole because Schwab bars end at 20:00; this is not proof
of next-day continuity.

## Loaded settings and re-pin

Read the NEW v2 `/proc/<MainPID>/environ`, not the env file alone. Journal the new
all three PMREST/PMPRINT/PMFLIP switches explicitly true and unchanged sizing
600/300/1000, reprice/offset/
band 0.5, retry-one, flip-owner, gap hold, stream-cross, entry window 07:00-15:45,
reclaim=false and polygon_30s=false. Compare unaffected service identities to the
preflight census. Capture only new-process log bytes; require zero new Traceback,
NRestarts=0, exact BOOT-HOLD and warm-up lines. Empty overnight population means
`held, EXPECTED BY DESIGN, population=<n>`, NOT release PASS.

Install only the isolated expected_flags.json from APPROVED_SHA, compare its
Git blob and file SHA256; checker/numeric paths and hashes remain unchanged.
Run the reviewed read-only FLAGGATE with that catalog. Three new v2 boolean checks:
catalog has 127 bool definitions; previous per-service boolean denominator
140 becomes 143 (numeric 8 unchanged). Print the actual full denominator and
Final call; mismatch/unreadable fails or UNKNOWN, never force green.

Single preopen.sh re-pin to 2026-10-06 and APPROVED_SHA/new v2 PID/start;
preserve every freshly verified unchanged identity, routing/checker/catalog
path and mode 0700. REPORT MUST be
`/home/trader/known_defect_regression_watch/v2-restart-evidence-20261006.md`.
That report-date defect was already corrected this morning; retain the correct
line, do not make a second unrelated change. Current preopen hash before this
planned evening re-pin is
`2ef22340d34ebe5630d140729384409e69c8953485d3ee409e7c5551832f5af3`;
recheck before use. Back up, review diff, bash -n, record new hash. Do not run
the date-fixed October 6 gate early. No Monday guard re-pin/service action.

## Completion and next-session proof

Journal `/home/trader/fleet_health/deployments-20261005.md`: exact app/plan SHAs,
fresh flat/read/working-order proofs, preflight rc, backups/hashes and three-key
env diff, new v2 PID/start/NRestarts, every untouched identity, /proc settings,
new log bytes/BOOT-HOLD population, catalog blob/file hashes, FLAGGATE/numeric
Final calls and denominators, preopen backup/diff/hash/report-date line.
Say INSTALL COMPLETE only when these are established; list unknowns honestly.

Codex-2 owns Tuesday 10-06 07:00/first-nonempty-watchlist warm-up and BOOT-HOLD
release verification; claude-1 independently checks bar continuity after first
bars (07:10 if populated). First live meaningful software reprice must show one
EH-MOVE with old/new values, no reprice disarm and no next-bar hole. Compare
Schwab and Webull intents on both REST and stream crosses, confirming ask/source/
age and once-only latch. Verify flip-seen/cross-taken separately, and the existing
next-bar take-down. A1 unchanged-rest boundary, A2 ORB latency, F2 later re-entry
and low-print exits are not in this install. Real next-session delivery/fills
are UNEXERCISED at evening close-out, never inferred from flag or gate PASS.
