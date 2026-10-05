# OWN MIX / RPG STUCK / PM cards - conditional single evening plan

**SUPERSEDED: see ../tonight-candidate/INSTALL_PLAN_2026-10-05.md.**
This earlier all-PM-ON/RPG-ON draft is retained as historical evidence only.

**DRAFT FOR REVIEW, NOT EXECUTABLE APPROVAL OR A SCHEDULED JOB.**
Monday October 5 after 20:00 ET and verified log rotation, both live accounts
flat. Only independently pinned final heads may enter an operator-named exact
application SHA. Populate the table and a committed approval-gated runner
before asking for execution. No approval or automatic rollback is implied.

## Eligibility And Merge Order

| Change | Head / state at draft | Required restart | Migration / switch |
|---|---|---|---|
| PMREST1 #1091 | merged a80b5181, dark in production | v2 | PM_REST_REPRICE_ENABLED=true |
| PMPRINT1/PMFLIP1 #1092 | 71f5f6bc, Validate x2 PASS, NOT pinned | v2 | PM_PRINT_ASK_CONFIRM_ENABLED=true and PM_FLIP_WAIT_ENABLED=true |
| OWNMIX1 #1094 | b94bd0ff0c2c7da681da45d748db61db82250b7c, review-ready, NOT pinned | OMS + strategy companion | additive managed-entry binding migration; no ledger repair |
| RPGSTUCK1 #1093 | 73e9e4c8f92f672ecc03e7c460b134123015840a, review-ready, NOT pinned | OMS + strategy companion + v2 | existing RPG switch retained ON; no new trading rule |
| GAPKEEP1 | Step 0 e90a7073, STOPPED on scope/safety differences | excluded | no flag or source build |

Proposed order, not a merge authorization: #1091 already merged; #1092 if pinned;
OWNMIX1 if pinned; RPGSTUCK1 if pinned. Any rebase/conflict resolution requires
a fresh exact-head pin. No GitHub Update branch. No merge of unpinned work.
Both OMS fixes touch service.py; PM and RPG fixes touch the v2 strategy.
Run the ordered integration rehearsal and composition tests on the exact
combined tree before final pins/GO; clean pairwise merge-tree is insufficient.
No shared source branch has two writers. Excluded PRs wait for a later window.

Own ordered composition rehearsal is recorded in COMPOSITION_2026-10-05.md:
#1092 then #1094 then #1093 applies cleanly; full unit 5549 passed / the same
56 baseline failures. This is a source/test rehearsal, NOT an independent
review pin, executable migration proof, or live broker timing evidence.
OWNMIX schema tests and legacy refusal run in the composed unit suite; the
actual production migration has not been run. Both RPG and OWN Validate checks
PASS at their exact heads (OWN runs37331123062/37331114821 completed15:19Z).
Neither has an independent review pin; recheck exact heads before approval.

APPROVED_SHA must be the final reviewed merge tree, 40 hex, supplied by the
operator. BOX_SHA observed10:45 ET is
e1ce3b3978fbcb1ff00daecdf6dc2618e6b5fb89 (loaded app bbb43604). Re-read before
review/execution; do not substitute moving main or silently change a box pin.
Main may be ahead of APPROVED_SHA only by docs paths. Any code drift stops.
Application diff allowlist must be generated from the selected pinned PRs,
including OWNMIX's explicit models/protocol/adapter/store changes and migration.

The one combined restart, if both OMS fixes are included, is three services:
v2, strategy, OMS. If neither OMS fix is eligible, use the separately reviewed
#1091/#1092 v2-only plan instead. Do not perform both plans or restart v2 twice.
No gateway, ORB, orb-schwab, paper, control, capture, reconciler, Redis or
Postgres restart; no watch installation, subscriptions replay or guard action.

## Fresh Evidence Before Any Write

Require no concurrent deployment; exact clean BOX_SHA; pinned preopen/catalog/
checker/helper hashes; all service PID/start/NRestarts identities recorded.
Record both EnvironmentFiles (primary env and orb-paper.env) even though this
plan does not change ORB's file. Run Python Settings reads as root; editable
runtime pip refresh as trader. No secret token or account credential output.

Fresh direct positions and working/open-order reads on Schwab and Webull, plus
read-only bot books: managed rows zero, nonzero virtual positions zero, no
working bot entries, no NFQ/RPG live-entry ambiguity. Record request, bounded
reply size, timestamps and all order IDs. Use the reviewed strict-flat helper
whose exact blob/hash will be bound in the executable release, not a newly
weakened variant. Unreadable = UNKNOWN/STOP. Any known ledger mismatch that
prevents this proof is a blocker, not an excuse to edit the ledger or waive
flatness. No historical NXL/manual-close override is inherited by this plan.

Apply fresh flat/working-order proof BEFORE checkout/env/schema writes and
immediately before every service stop/start/restart. Check the token expires
more than25 minutes ahead before a broker read after a long quiesce. Control
stays up as token refresher/single writer; no competing one-off token writer.
If it cannot refresh/read successfully, STOP for operator recovery instructions.

Immediately before stopping v2, run with pipefail:

```bash
sudo /home/trader/project-mai-tai/ops/preflight/preflight_v2_restart.sh
```

Require rc0. An armed set blocks; only the operator may name the exact live
set and accept Bug2 in a separate reviewed override at the instant of the run.
No automatic override, old symbol list, retry loop or clock waiver. Do not
alter armed segments. All Redis reads remain bounded: no bulk snapshot read,
no XINFO/EVAL materialization of snapshot payloads.

## OWNMIX1 Schema And Legacy Rows

Candidate migration20261005_0022 adds nullable entry_order_id (UUID) and
entry_client_order_id (128 chars) to oms_managed_positions. Confirm the final
reviewed migration blob before release; revision/base must match the live DB.
Back up schema metadata and record the current Alembic version/column census.
Do not populate a legacy binding with latest account+symbol BUY or quantity.
No fill/position/virtual-row/ledger backfill or correction is authorized.

Stop v2 and strategy only after their preflight/flat gates, then stop OMS if
needed to keep old/new ORM writers away during the approved migration. The
executable reviewed runner must pin the exact stop/migration/start sequence:
an explicit OMS stop followed by one start is ONE OMS replacement, not a
second discretionary restart. The operator's GO must name the additive schema
change and this OMS-stop variant; otherwise the schema-bearing PR is excluded.

Legacy unbound rows remain unbound and managed. Entry-specific resolution,
native-OCO stand-down and resolved-symbol sets must refuse unprovable ownership
with the mismatch evidence, never guess. This is why open managed rows must be
zero before migration/restart. Verify the future post-open binding through its
real strategy/order/client IDs; migrations alone do not prove future ownership.

Candidate literal migration command after exact checkout and runtime refresh:

```bash
sudo -u trader git -C /home/trader/project-mai-tai switch --detach "$APPROVED_SHA"
sudo -u trader /home/trader/project-mai-tai/.venv/bin/pip install --no-deps -e /home/trader/project-mai-tai
sudo /home/trader/project-mai-tai/.venv/bin/python - <<'PY'
import os
from alembic import command
from alembic.config import Config
from project_mai_tai.settings import Settings, get_settings

os.chdir('/home/trader/project-mai-tai')
settings = Settings(_env_file='/etc/project-mai-tai/project-mai-tai.env')
os.environ['MAI_TAI_DATABASE_URL'] = settings.database_url
get_settings.cache_clear()
config = Config('/home/trader/project-mai-tai/alembic.ini')
command.upgrade(config, '20261005_0022')
PY
```

These commands are a review candidate, not a runnable release: first confirm
the old revision on the box read-only. Source inspection confirms env.py
overrides sqlalchemy.url from get_settings(), whose default env_file is .env;
therefore the candidate reads the protected production env explicitly as root,
sets only its database URL in this short-lived process, clears the Settings
cache, and sets cwd for the relative migration directory. No credential value
is printed or placed in shell arguments. Do not run these commands from this
draft. Verify the exact composed runtime import/config route before release.

## Runtime Sequence And Failure Policy

Commit the attended runner with exclusive attempt dir, flock, release manifest
and absent approval.json before scheduling. Named box timer/unit must be
listable if later authorized; never claim merely a desktop reminder as a
scheduled install. No runtime write or stop while drafting/staging unapproved.

After all preflight proofs: exclusive backups/hashes; exact checkout/runtime
advance; env/candidate schema as approved; stop v2 -> stop strategy -> replace
OMS once -> start v2 -> start strategy. Flat_now precedes EACH command. Keep
the existing strategy-restoration abort trap, and extend it explicitly for the
v2 stop if required by the reviewed sequence. If any abort would leave a live
service stopped, the reviewed runner must name that state and page; do not
invent extra recovery starts/restarts from this draft. Remain attended through
the final census and proofs. No automatic rollback/schema downgrade authorized.

Immediately before/after strategy initialization: evicted_keys unchanged,
used_memory below reviewed1.6GB bound, every consumer field plus
_migration_complete=1 still present in owners hash. The gateway is current
#1084 code after Saturday's resize, but self-heal is not a waiver of this
proof. The120-batch warm-up can be expensive; no Redis eviction is excused.
Stop/report UNKNOWN on eviction, missing owner state or unreadable safety data.

## Settings, Close-Out And Re-Pins

If #1092 is eligible, append exactly one true definition per primary-env key,
refusing duplicates and preserving all unrelated lines:

```text
MAI_TAI_STRATEGY_SCHWAB_1M_V2_PM_REST_REPRICE_ENABLED=true
MAI_TAI_STRATEGY_SCHWAB_1M_V2_PM_PRINT_ASK_CONFIRM_ENABLED=true
MAI_TAI_STRATEGY_SCHWAB_1M_V2_PM_FLIP_WAIT_ENABLED=true
```

If only #1091 is eligible, the latter two must not be installed/enabled. No
other new flag is authorized by this draft. Existing sizing600/300/1000,
target5/stop8/floorfalse, RETRY_ONE, EOD/overnight flatten, RPG, NFQ10,000,
COLDSTART and ORB flags remain unchanged; reclaimfalse and polygon_30sfalse.
Verify all relevant loaded values from each NEW process's /proc, not only
Settings defaults or env text. Catalogue denominator143/143 with all three
PM switches (141/141 if #1091 alone), numeric8/8; print actual denominators.
Any mismatch must stop, not be labelled green. Installed catalog is exact
APPROVED_SHA Git blob, isolated checker/numeric routes hash-verified.

New OMS/v2/strategy PID/start/NRestarts0, active, zero new-process tracebacks;
unchanged identities for every service outside scope. Record boot hold and
population literally; empty overnight population may hold until first bars,
EXPECTED BY DESIGN, not release PASS. Fresh live:orb sync census ok>0/failed0
and account_positions stamp after OMS start. Verify no ownership fallback in
new-row schema/runtime; no manual DB repair to make that proof succeed.

One preopen.sh re-pin to2026-10-06/final APPROVED_SHA and three final new
identities; all other pins and router paths preserved. REPORT must end
v2-restart-evidence-20261006.md. Exclusive backup/hash, one-line identity/date
diff audit, bash-n, mode0700. Do not run the October6 date-fixed full gate
early. No second guard/Monday re-pin: today's guard is not restarted by this.

Journal actual steps in deployments-20261005.md: source/plan/release hashes,
strict flat proofs, v2 preflight rc, backups/env diff, migration revision/schema
proof, new/untouched identities, /proc, sync evidence, Redis before/after,
catalog/checker/numeric results, preopen backup/diff/hash. COMPLETE only on
actual proof, never on a process restart alone. Unknowns and unexercised
ownership/reprice/partial-fill/live-cross paths remain explicit.

## Next Session And Scanner Validation

Codex-2: first nonempty v2 warm-up/boot-hold release; per-broker RPG cancel ->
readback -> replace latency; canonical APUS/VEEA values; refused/expired ticket
with proven-clear old order admits normal placement; unresolved dispatch still
blocks only its own leg; no duplicate/rest remainder re-buy. OWN binding must
match true parent for both accounts; legacy mismatch line is not a close.

Scanner checks: actual 'prefilled momentum alert history from N snapshot
batches', N >= squeeze_10min_needs unless documented population is genuinely
too young; compare first15-minute5/10-minute squeeze counts and watchlist adds
with prior5 sessions, report denominators; zero new strategy errors. Warm-up
bound for strategy is180s, not60s. Do not call low early population a proven
scanner failure without reading the design and feeds. Claude independently
owns first-bar continuity and scanner comparison. No snapshot payload bulk
read to validate the scanner.

PM cross proof: strict LOWER check only, both premarket paths, same confirming
ask sizes both legs; existing stream upper cap/OMS repricing preserved; no
fresh/lower ask means no latch/slot/buy. VEEA post-flip window latches once;
existing next-bar take-down unchanged. Regular-hours order paths unchanged.
Real entry/fill/cache timing is UNEXERCISED until observed next session.
