## RETAIN1: isolated build, no production change

Operator-approved per-table retention from RETENTION_2026-10-07.md on handoff #1096.
This is a DRAFT because the Step-0 historical-input/export check is NOT clear.

- Forever tables refused; scanner_cycle_history is an exact-type alias into dashboard_snapshots, not a standalone table and not permission to prune trade state.
- Calculation tape / reconciliation / scanner targets 7d; bars 45d by bar_time; paper PATH_PRINT/FEED_GAP only 1d. All other paper events preserved.
- Per-target keep-days overrides with protected floors, dry-run default, explicit --go, batched deletes, fixed cutoffs, global row/time admission caps, nonblocking writer lock, FK-safe findings-before-runs, count/size/remaining receipts, ordinary VACUUM ANALYZE outside transactions.
- Two existing timer names reused without duplicate targets: daily plumbing 05:00 ET; Sunday captures 05:30 ET; one added Sunday 05:45 ET timer for the other weekly targets. Persistent=false, explicit America/New_York.
- PATH_PRINT persistence OFF at the service writer only. Pure engine, trade/PnL transitions, feed and snapshots unchanged; no new switch or sampling interval.

## Step 0 / first-purge blockers

Historical capture readers DO exist (parked research scripts). The protected 30-session
pullback study also uses confirmation-relative context; shortening scanner evidence
cannot be called harmless without its exported input receipt. Need an immutable
export/dependency disposition before enabling timers or any --go. Request made; no
receipt is invented. Current-session repair remains within the retained window.

Read-only planner inspection also found reconciliation selection scans/sorts the
whole table without an age-leading index. Actual selector cost/index disposition
must be rehearsed before first purge; no unapproved index/DDL was applied.

## Verification

- 217 focused pass (retention, Momentum service/engine, scanner restore, LINESRC1, R6, gateway handoff).
- 10/10 isolated mutations RED, each reverted.
- Full unit base 7,114 pass / 56 fail; head 7,184 pass / 56 fail; identical failed names. No failures waived.
- Ruff and diff check pass.
- PostgreSQL accepted paper and scanner DELETE syntax via read-only EXPLAIN WITHOUT ANALYZE; no DELETE was executed.
- Systemd calendar parsing confirms Sunday 05:45 EDT/EST and daily 05:00 ET; no timer is installed or claimed scheduled.

Receipts: ASSESSMENT_2026-10-07.md, INSTALL_PLAN.md, VERIFICATION.md,
FULL_UNIT_PAIR.json and READ_ONLY_PLANNER_2026-10-07.md in this directory.

Not installed, no purge, no before/after deletion sizes, no paper restart. Target is
the separately reviewed October 8 after-close batch only after the dependencies clear.
