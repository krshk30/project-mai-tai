# One combined observe-only installation

Updated for the operator's 2026-09-29 scope ruling: include #1065 and #1067; drop the watch install.
This is a plan, NOT a deployment record or an authorization.
No production flag, file, order, subscription, or service was changed while preparing it.

## Exact revision and scope

**MERGE HOLD: FINAL DEPLOY SHA NOT YET ASSIGNED.** The old 90106fb4 approval request is superseded.
Current main: **eeaa4a7d2d6192125286f281076b72fb9bd497eb**, after rebase-merging independently pinned
#1065 b6a0ddd6 onto 90106fb4. This is NOT the complete installation candidate: #1067 is not merged.
PR #1064 was rebase-merged from independently pinned 442b492b on unchanged base fc68b238.
It is approved for observation only; the live-order flag must remain OFF.

The final scope includes #1060 (Momentum Option A/shared gateway), #1061 (restart-evidence
checker), #1064 (ORB), #1065 (LIVE per-SELL-cycle RETRY-ONE), #1067 (90-second ORB evidence grace),
and already-installed PAGEGAP. #1059 source is present in main, but its installed watch and
auto-close behavior stay HELD until WBREAD1. #1066 / Card 10 is explicitly EXCLUDED.

#1067's pin covered 3be48c4f against 90106fb4. Moving main for #1065 required rebasing #1067;
its original patch is unchanged by range-diff, but the pin does NOT transfer to a new head/base.
These operator-requested plan corrections are included for fresh review. STOP before merging
#1067 until its new exact head/base is pinned and CI passes. After that merge, record the actual
full main SHA in the final execution-plan copy and request operator GO for that exact revision.
Never install the intermediate main or substitute a later commit under an earlier GO.

## What restarts

Read-only systemd snapshot, 2026-09-29 approximately 17:03 ET; all existing units active with
NRestarts=0. These are BEFORE identities, not promised future PIDs. Capture them afresh at
preflight and record the new PID, start time, revision and restart count after each operation.

| Unit (project-mai-tai- prefix) | PID before | Planned action |
| --- | --- | --- |
| market-data | 2202865 | Restart for #1060 shared gateway changes |
| momentum-paper | 2704889 | Restart onto Option A after gateway is healthy |
| oms | 1328348 | Restart to load #1064 routes with live ORB OFF |
| strategy | 1359274 | Stop/start companion required by scoped gateway/OMS deployment |
| orb | 2051823 | Restart existing broker-disconnected paper observer; #1064 changes its high filter |
| orb-schwab | 0, inactive | Install/start separate OBSERVE_ONLY service |
| schwab-1m-v2 | 1329729 | Restart for #1065; RETRY_ONE is already ON, so this changes LIVE trading |

The paper ORB high now ignores individual prints below 100 shares. That does not change the
proposed native order size of TWO shares. ORB native sending is OFF throughout rehearsal.
This is NOT an observation-only change to v2: #1065 changes its enabled RETRY-ONE rule.
Read-only /proc inspection confirmed MAI_TAI_STRATEGY_SCHWAB_1M_V2_RETRY_ONE_ENABLED=true on PID
1329729. Preserve and verify that same value in the new v2 process; do not toggle it.
No blanket target restart, schema migration, manual live trade, or attended broker test is included.

## Before any changes

1. Obtain operator GO for the final exact SHA, this service list, flag changes and rollback
   procedure, in one after-hours window. Independent review and CI must match that revision.
2. Freshly confirm the clean box checkout (read here as 3141bbd5e77ef17e8a7afb3d75b0bb7865a4d4fd),
   process identities, healthy gateway/Redis, no concurrent deploy or heavy study, fresh broker
   flatness on BOTH live accounts, zero open managed positions and zero armed segments.
   A historical flat reading does not qualify. Do not override an ambiguous preflight.
3. **WHOLE-INSTALL HARD STOP:** before ANY checkout/env/runtime/unit/wrapper mutation, run
   #1060's retained-subscription reconstruction proof:
   scanner and v2 owner events must be present, union preserved, and no desired owner erased.
   An unreadable/missing owner is UNKNOWN; obtain evidence rather than restart an empty feed.
   Recheck current ownership immediately before the gateway restart as well.
4. **WHOLE-INSTALL HARD STOP:** require the measured Momentum-inactive 07:00-09:40 baseline
   and explicit review of the first-session
   slowdown thresholds in ../momentum-option-a/FIRST_SESSION_PROTOCOL.md. This turn did not
   verify that separate baseline/review. Load 3.5 is a warning under Option A, not a stop rule.
   If either step 3 or 4 is unproven, stop the WHOLE combined installation before changing the
   box. No partial ORB-only, v2-only or mixed-revision deployment under this authorization.
5. Check for the separately scheduled #1061 isolated installation at 18:00. Do not overlap or
   overwrite its preopen wrapper. Let its approved isolated-file work finish and inspect its
   record before the combined window. After ALL combined restarts, re-pin the wrapper ONCE
   to the final SHA/new PIDs for 09-30; no later installer may overwrite that final pin.
   Confirm one read-only 06:20 ET gate uses that script and its final hash, not an old pin.
   If the jobs cannot be sequenced, stop for coordination. Do not cancel the other job silently.

## Installation and verification

- Back up the fleet env, affected unit files, runtime manifest and preopen wrapper, with
  hashes and a durable journal. Capture the prior checkout AND each running-service revision;
  the current checkout is not automatically the revision of every process.
- Use the reviewed scoped deployment path, not deploy_main.sh. Its gateway and OMS actions
  stop/start strategy; hold strategy only as explicitly covered by the combined plan, so it
  need not cycle twice. Run the existing OMS restart fence immediately before that restart,
  and the v2 fence plus fresh flat/zero-armed checks immediately before the v2 restart.
- Review all helper side effects before execution. deploy_service.sh refreshes the editable
  runtime and its bootstrap rebuilds the isolated paper ORB env, installs the paper ORB unit
  and target, and refreshes preflight links. Record these effects. Migrations stay OFF. The
  existing Momentum installer unconditionally migrates; do not invoke it blindly merely to
  restart an already-installed service. Use the reviewed unit/runtime without migrations.
- Start/restart only the listed units, checking gateway owner restoration and fresh heartbeat
  before Momentum starts. Preserve polygon_30s=false and every unrelated flag. Install the
  separate orb-schwab unit explicitly; it is not silently added to the live service target.
- Set exactly MAI_TAI_ORB_SCHWAB_OBSERVE_ENABLED=true and
  MAI_TAI_ORB_LIVE_SCHWAB_ORDERS_ENABLED=false. Preserve MAI_TAI_ORB_ENABLED=true and
  MAI_TAI_MOMENTUM_PAPER_ENABLED=true (both observed in the fleet env). Confirm the two new
  flags from BOTH the new ORB process's and new OMS process's /proc environ.
  Require boot mode OBSERVE_ONLY/live_sending=False; missing or contradictory evidence stops
  the rollout. Do not equate an env file with a running process having loaded it.
- Verify no ORB trade intents, broker orders or new gateway subscriptions are emitted by the
  observer. Momentum remains paper-only with its 16-symbol cap and no direct Massive socket.
  Confirm gateway conditions metadata, scanner/v2 union preservation, snapshot cadence,
  service health, tracebacks/restarts, and unchanged PIDs for units outside the plan.
- **NO INC1 WATCH INSTALL.** Do not run install_unexercised_watch.sh, replace the installed
  copy, edit either root cron guard, or activate #1059 auto-close. The installed watch remains
  /home/trader/unexercised_watch/watch.py at SHA256
  45df60c60fe55af89406d20c29de277a44634d6bc6308586a4e0b62e718b8272, independently read here.
  Verify that hash and both existing cron guards are unchanged, read-only. Observation opens
  no incidents. New ORB-source phone delivery remains a separate activation prerequisite.
- Report the new v2 PID/start time and the exact /proc line
  MAI_TAI_STRATEGY_SCHWAB_1M_V2_RETRY_ONE_ENABLED=true. Inspect REST warmup, literal BOOT-HOLD
  release and bar evidence; an off-session path not exercised is UNEXERCISED, never PASS.
- Journal final checkout, EVERY restarted unit's new PID/start time and NRestarts, flags from
  /proc, unchanged untouched-unit PIDs, backup paths with hashes, and unchanged watcher hashes.
  Re-pin the preopen wrapper once for 2026-09-30 to the final SHA plus new v2/OMS/strategy PIDs
  and start times, preserving the reviewed three-way routing. Record the final script hash and
  06:20 schedule. Send the journal for reviewer verification against the box before that gate.
  Report REAL FAILURE / EXPECTED BY DESIGN / UNKNOWN only after checking code and evidence.
  Warmup, bar continuity or BOOT-HOLD not exercised after hours remains UNEXERCISED, not PASS.

## Rollback boundaries

Approve these actions with the exact-SHA GO; none have been executed. Stop the new ORB observer
first if it misbehaves; never turn on live sending as a remedy. An Option A slowdown stops ONLY
momentum-paper, then verifies momentum-paper=[] ownership and removal of its exclusively-owned
symbols (not scanner/v2-owned overlaps). Follow the reviewed empty-replace fallback if needed.
Do not restart the gateway as an automatic response to a paper-only slowdown.

For a shared-service regression, retain evidence, keep ORB live OFF and request the separately
approved rollback to recorded per-service revisions/unit/env backups. Re-run fresh flatness
and restart gates. Do not blindly pull the old checkout and assume it matches every running
service; do not erase durable gateway owners. The scoped deploy helper intentionally does not
perform an automatic trading-service rollback. #1065 writes schema-v2 per-cycle budget records;
the prior retry-budget reader accepts only schema v1. A v2 downgrade must account for that
durable state and its fail-closed restore behavior; never delete budget history to force entry
admission. Get a reviewed rollback/state plan rather than treating a binary rollback as proven
normal operation. No pager rollback is part of this window because no pager change is authorized.

## Tomorrow's evidence, not trades

On 2026-09-30 examine 09:25-10:00 ET observation records, after verifying the approved install
actually occurred and the live flag is still OFF. Report candidates and source-bar coverage,
breakout levels, completed-bar MACD decisions, proposed place/upward-reprice/cancel decisions,
conditional exits, pending/expired/missing-data counts, denominators and raw paths. Read
/var/log/project-mai-tai/orb-schwab.log; observation crosses are NOT broker fills or P&L.
No candidates/signals or an uninstalled observer means NOT VALIDATED, not a successful test.

Still required before activation: independently reviewed grace fix, attended authorized
Schwab place/reprice/cancel, early-close support, proven phone delivery for all new ORB INC1
sources, partial-fill/held-symbol coverage checks, and a separate activation decision.
