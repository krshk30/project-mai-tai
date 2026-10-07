# RETAIN1 installation / first-purge plan

No production job is staged or enabled by this build. Latest reviewer instruction:
first purge after job 2 completes on 2026-10-07, after exact-head pin and reviewed
code installation. Exact merge SHA must be filled after pin/merge. Historical
reader/export dependency disposition is accepted in ASSESSMENT_2026-10-07.md.

## Job division (no duplicate targets)

| Listable unit | Calendar | Targets |
|---|---|---|
| project-mai-tai-prune-ticks.timer | daily 05:00 America/New_York | paper PATH_PRINT/FEED_GAP only, 1 day; existing name repurposed |
| project-mai-tai-prune-capture.timer | Sunday 05:30 America/New_York | captures trades/quotes/bars, 7 days; existing job moves from daily to weekly |
| project-mai-tai-prune-retention-weekly.timer | Sunday 05:45 America/New_York | trade/quote ticks, reconciliation findings/runs, scanner history/confirmed events, 7 days; strategy bars, 45 days |

All three use Persistent=false. Calendar is ET, including DST, not fixed UTC.
Weekly service orders after an already-running capture/plumbing service; all writers
also share a nonblocking PostgreSQL advisory lock. A contending writer refuses.
Each DELETE is autocommitted separately, batch<=10,000 in the units; all tables share
the run cap (daily 1M, captures 50M, other weekly 25M) and a 3,600-second admission
budget. Connection timeout=10s, statement timeout=60s, lock timeout=1s.
Each service has a 2-hour systemd timeout. No retry loop, rollback or trading action.

## Before enabling / first write

1. Pin the patch, then fill and verify exact merge SHA and clean checkout using the
   repository procedure. Do not substitute a moving branch.
2. Record job 2's COMPLETE receipt, actual finish time and installed SHA. Elapsed time,
   scheduled time or a running job is not completion. No data deletion before the pin
   or before that receipt. Dependency scope is the reviewer's 2026-10-07 ruling:
   scheduled capture readers need recent data; studies are hand-run; pullback uses bars.
3. Capture the current three unit definitions and checksums before replacement;
   the existing tick service lives on the box but was previously absent from the repo.
4. For every target, read-only EXPLAIN of the exact count/batch selectors and a dry-run
   count/size receipt on the install SHA. SELECT-only ANALYZE is permitted before pin;
   never ANALYZE a DELETE before pin. The recorded 50k reconciliation SELECT timings
   are below two seconds but exclude DELETE/commit/FK cost. Actual batch-size selection
   uses the post-pin rehearsal below. An index change needs reviewed SQL, not an
   improvised schema change. Statement timeout/refusal is not a successful rehearsal.
5. Stop the old retention timers ONLY after reviewed replacement bytes and next-calendar
   checks exist. No app-service stop/restart is part of the pruning mechanics.

## First purge after job 2 COMPLETE tonight

First perform the now-authorized real reconciliation rehearsal, findings before runs,
one bounded invocation per target, --batch 50000 --max-rows 50000 --go with that
target's --keep-days 7. Capture source hash, exact command, counts, sizes and every
batch's elapsed_seconds. This is a real committed purge, not a harmless rollback.
rc=3 / bounded-incomplete is expected if old rows remain after the single batch.
No deletion is performed to obtain this evidence before the pin.

Retain 50,000 only if each measured DELETE batch stays approximately <=2 s. Otherwise
reduce the batch (initial units remain conservative at 10,000), rehearse the reduced
size under the same post-pin/job-2 authorization, and record the selected size and
timing before the remaining purge. If the selected recurring size differs from the
committed units' 10,000, publish the exact unit-only delta for review before enabling
them; do not silently alter pinned artifacts. Do not claim SELECT timing proves DELETE timing.
Do not enable recurring writers until that measured size is established. No additional
index, threshold waiver or unapproved retry loop is authorized here.

Use the installed script's explicit --go commands from the three service ExecStart
lines, one at a time, under supervision in the after-close batch. Run their same CLI
without --go first; retain stdout/stderr and source hashes. No arbitrary --where SQL.
Every count/delete shares one fixed cutoff for the entire pass and the same exact
event/type predicates. Parent runs with surviving findings stay, even if older than
seven days. Shared caps return rc=3 / bounded-incomplete when work remains, NOT COMPLETE.
Errors return nonzero; partial deletion is documented; no automatic extra attempt.

Ordinary VACUUM (ANALYZE), never VACUUM FULL, runs once on each changed physical table
outside a transaction. It makes dead space reusable, and does NOT promise a smaller
OS database file. Record allocated bytes before/after and eligible/deleted/remaining
rows separately; do not describe the estimated five GB as measured disk reclamation.

After successful first pass, install/enable the exact timer units and verify systemd
calendars/list-timers for the next real dates (Thursday Oct8 daily; Sunday Oct11 weekly,
if tonight's pass completes). These are expected dates, not installed NEXT receipts.
Do not force a scheduled run during session to test a timer. Record all unit hashes,
the before/after-size JSON receipt and actual outcome in the deploy journal.

## PATH_PRINT writer

Only MomentumPaperService._persist changes. Pure engine still emits/calculates PATH_PRINT;
the service no longer stores these diagnostic records, even when mixed with a fill.
All other transitions remain durable. The normal daily guard stops paper at 09:40;
the next normal startup after the reviewed code install loads the new writer. Do not
restart paper or widen its session to make this change effective early.

## Rollback / limits

Revert scripts/units/writer code only under explicit recovery authorization. Pruned
rows cannot be recreated by a code revert; recovery requires the retained export or
database backup. No trade-state data, ledger, or market subscription change is allowed.
No first-purge before/after receipt exists yet; no production performance claim or
purge completion is made by this PR.
