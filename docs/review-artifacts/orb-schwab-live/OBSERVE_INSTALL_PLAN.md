# One combined observe-only installation

Prepared 2026-09-29. This is a plan, NOT a deployment record or an authorization.
No production flag, file, order, subscription, or service was changed while preparing it.

## Exact revision and scope

Current merged candidate: **90106fb4ebbb1feda7247d420352114d14dcd084**.
PR #1064 was rebase-merged from independently pinned 442b492b on unchanged base fc68b238.
It is approved for observation only; the live-order flag must remain OFF.

The candidate includes #1060 (Momentum Option A/shared gateway), #1061 (restart-evidence
checker), #1059 (pager hours/incident resolution), #1064 (ORB), and already-installed PAGEGAP.
The 90-second evidence-grace follow-up is NOT in that candidate until separately reviewed and
merged. It is an activation blocker, not permission to activate after its tests pass.

#1065 was OPEN and unpinned at 012889cd when checked. It is excluded. Its base has moved after
#1064: it needs its own updated review/pin before any merge. Including it, the grace follow-up,
or any other commit changes the candidate SHA and requires a refreshed exact-SHA plan and GO.
Never substitute whatever main happens to contain when the installation starts.

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
| schwab-1m-v2 | 1329729 | No restart for this candidate; add only if separately pinned #1065 joins the plan |

The paper ORB high now ignores individual prints below 100 shares. That does not change the
proposed native order size of TWO shares. Native entry/exit logic is OFF throughout rehearsal.
No blanket target restart, schema migration, live trade, or attended broker test is included.

## Before any changes

1. Obtain operator GO for the final exact SHA, this service list, flag changes and rollback
   procedure, in one after-hours window. Independent review and CI must match that revision.
2. Freshly confirm the clean box checkout (read here as 3141bbd5e77ef17e8a7afb3d75b0bb7865a4d4fd),
   process identities, healthy gateway/Redis, no concurrent deploy or heavy study, fresh broker
   flatness on BOTH live accounts, zero open managed positions and zero armed segments.
   A historical flat reading does not qualify. Do not override an ambiguous preflight.
3. Before the shared gateway restart, run #1060's retained-subscription reconstruction proof:
   scanner and v2 owner events must be present, union preserved, and no desired owner erased.
   An unreadable/missing owner is UNKNOWN; obtain evidence rather than restart an empty feed.
4. Require the measured Momentum-inactive 07:00-09:40 baseline and review of the first-session
   slowdown thresholds in ../momentum-option-a/FIRST_SESSION_PROTOCOL.md. This turn did not
   verify that separate baseline/review. Load 3.5 is a warning under Option A, not a stop rule.
5. Check for the separately scheduled #1061 isolated installation at 18:00. Do not overlap or
   overwrite its preopen wrapper. Read its completed record and use one final wrapper pin.
   This plan does not cancel or execute that separate job.

## Installation and verification

- Back up the fleet env, affected unit files, pager file/root crontab and preopen wrapper, with
  hashes and a durable journal. Capture the prior checkout AND each running-service revision;
  the current checkout is not automatically the revision of every process.
- Use the reviewed scoped deployment path, not deploy_main.sh. Its gateway and OMS actions
  stop/start strategy; hold strategy only as explicitly covered by the combined plan, so it
  need not cycle twice. Run the existing OMS restart fence immediately before that restart.
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
  flags from the new ORB process's /proc environ, and live=false in the new OMS process.
  Require boot mode OBSERVE_ONLY/live_sending=False; missing or contradictory evidence stops
  the rollout. Do not equate an env file with a running process having loaded it.
- Verify no ORB trade intents, broker orders or new gateway subscriptions are emitted by the
  observer. Momentum remains paper-only with its 16-symbol cap and no direct Massive socket.
  Confirm gateway conditions metadata, scanner/v2 union preservation, snapshot cadence,
  service health, tracebacks/restarts, and unchanged PIDs for units outside the plan.
- Reinstall the INC1 watch using ops/health/install_unexercised_watch.sh at the approved SHA:
  installed /home/trader/unexercised_watch/watch.py hash must match the reviewed file; BOTH
  root cron hash guards must match it. Read the next INC1_STATUS. Preserve state/history.
  Installing the query is NOT proof that a phone received an ORB alert.
- Journal final checkout, all new PIDs/times, flags, file hashes and watch result; repin the
  preopen wrapper once for the final SHA/identities, preserving the reviewed three-way routing.
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
perform an automatic trading-service rollback. Pager rollback restores the installed file AND
both matching cron guards together, not just one of them.

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
