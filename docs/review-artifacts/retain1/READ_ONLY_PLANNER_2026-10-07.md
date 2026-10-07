# RETAIN1 read-only planner / calendar receipts

Observed 2026-10-07, approximately 20:51-20:53 UTC. No production DELETE, VACUUM,
DDL, service or timer action. PostgreSQL EXPLAIN used WITHOUT ANALYZE, inside
BEGIN READ ONLY / ROLLBACK for the delete statements; statement_timeout=5s.
These are query-shape checks, NOT an execution-time or first-purge receipt.

## Paper deletion

Exact generated shape, literal cutoff 2026-10-06 20:51 UTC, LIMIT 10000,
event_type IN ('PATH_PRINT','FEED_GAP') in both candidate and outer guards.

- Inner Index Scan using ix_momentum_paper_events_observed_at, Incremental Sort
  (observed_at,id), Limit 10000.
- Outer Index Scan using pk_momentum_paper_events, with repeated age/type filter.
- Estimated candidate population 898,807 (planner estimate only).
- PostgreSQL accepted the statement; transaction rolled back without execution.

## Scanner snapshot deletion

Exact generated shape, literal cutoff 2026-09-30 21:00 UTC, LIMIT 10000,
snapshot_type='scanner_cycle_history' in both candidate and outer guards.

- Inner Index Only Scan Backward using ix_dashboard_snapshots_type_created_id_desc.
- Outer Bitmap Index Scan using ix_dashboard_snapshots_snapshot_type, exact type
  condition and age filter.
- Estimated candidate population 3,755 (planner estimate only).
- PostgreSQL accepted the statement; transaction rolled back without execution.

## Reconciliation candidate selector

SELECT id FROM reconciliation_findings WHERE created_at < 2026-09-30 21:00 UTC
ORDER BY created_at,id LIMIT 10000:

- Parallel Seq Scan -> Sort -> Gather Merge -> Limit.
- Estimated qualifying rows 1,219,244; estimated pre-limit sort cost 163,438.58.
- This does NOT establish safe execution cost. A batch limit bounds deleted rows,
  not the scan/sort needed to choose them. First-purge rehearsal must resolve this
  and the other missing age-leading indexes before enabling weekly writes.

## Systemd calendar parsing on the box (read only)

| Expression | Base UTC | Next UTC | ET |
|---|---|---|---|
| Sun *-*-* 05:45:00 America/New_York | 2026-10-09 00:00 | 2026-10-11 09:45 | 05:45 EDT |
| Sun *-*-* 05:45:00 America/New_York | 2026-11-01 00:00 | 2026-11-01 10:45 | 05:45 EST |
| *-*-* 05:00:00 America/New_York | 2026-10-09 00:00 | 2026-10-09 09:00 | 05:00 EDT |

No unit was installed or enabled; these NEXT values are parser results, NOT
systemctl list-timers evidence of a scheduled job.
