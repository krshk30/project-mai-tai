## RETAIN1: isolated build, no production change

Operator-approved per-table retention from RETENTION_2026-10-07.md on handoff #1096.
Ready for review under the 2026-10-07 dependency disposition. No pin or purge claimed.

- Forever tables refused; scanner_cycle_history is an exact-type alias into dashboard_snapshots, not a standalone table and not permission to prune trade state.
- Calculation tape / reconciliation / scanner targets 7d; bars 45d by bar_time; paper PATH_PRINT/FEED_GAP only 1d. All other paper events preserved.
- Per-target keep-days overrides with protected floors, dry-run default, explicit --go, batched deletes with elapsed-time receipts, fixed cutoffs, global row/time admission caps, nonblocking writer lock, FK-safe findings-before-runs, count/size/remaining receipts, ordinary VACUUM ANALYZE outside transactions.
- Two existing timer names reused without duplicate targets: daily plumbing 05:00 ET; Sunday captures 05:30 ET; one added Sunday 05:45 ET timer for the other weekly targets. Persistent=false, explicit America/New_York.
- PATH_PRINT persistence OFF at the service writer only. Pure engine, trade/PnL transitions, feed and snapshots unchanged; no new switch or sampling interval.

## Step 0 / first-purge prerequisite

Reviewer disposed of the dependency blocker: scheduled capture readers need recent data;
historical studies are hand-run; pullback uses retained bars. Historical readers still
exist; no export receipt is invented. Current-session repair stays within the window.

Read-only 50k reconciliation SELECT measurements: findings 1.159158 / 0.481628 s;
runs 0.813721 s. Actual DELETE timing is NOT measured, since nothing may be deleted
before pin. The first pinned pass after job 2 COMPLETE tonight measures 50k DELETE
batches and retains only a size meeting approximately two seconds. No index/DDL applied.

## Verification

- 218 focused pass (retention, batch elapsed-time receipt, Momentum service/engine, scanner restore, LINESRC1, R6, gateway handoff).
- 10 initial isolated mutations RED; new elapsed-time mutation RED, restored.
- Full unit base 7,114 pass / 56 fail; follow-up 7,185 pass / 56 fail; identical failed names. No failures waived.
- Ruff and diff check pass.
- PostgreSQL accepted paper and scanner DELETE syntax via read-only EXPLAIN WITHOUT ANALYZE; no DELETE was executed.
- Systemd calendar parsing confirms Sunday 05:45 EDT/EST and daily 05:00 ET; no timer is installed or claimed scheduled.

Receipts: ASSESSMENT_2026-10-07.md, INSTALL_PLAN.md, VERIFICATION.md,
FULL_UNIT_PAIR.json and READ_ONLY_PLANNER_2026-10-07.md in this directory.
FULL_UNIT_FOLLOWUP.json records the new elapsed-time receipt's full-suite comparison.

Not installed, no purge, no before/after deletion sizes, no paper restart. First purge
is authorized only after exact-head pin and job 2's completion tonight (October 7).
