# RETAIN1 independent Step 0

Base: origin/main 994f08aee2c35b3809b3c3384e0d8628807dcfb7.
Read-only inspection on 2026-10-07. Isolated writer branch:
codex/retain1-table-retention. No production changes or purge performed.

## Verdicts

| Claim | Verdict | Evidence / consequence |
|---|---|---|
| No scheduled consumer needs captures older than seven days | AGREE with narrowed claim | fleet_health_check.py reads the last 10 minutes of capture trades; orphan_order_check.py reads the latest quote, not a historical window. Historical capture readers still exist in hand-run studies; the reviewer explicitly disposes of them as outside the scheduled-reader requirement. |
| LINESRC1 repair needs current-session data | AGREE | schwab_1m_v2_bot.py _read_line_session_bars bounds anchor to current bar; _read_line_gap_trades checks the same session gaps in capture trades / trade ticks. Seven days preserves current-session tape. |
| No live calculation needs persisted PATH_PRINT rows | AGREE for inspected code | The engine computes path/MFE/MAE before emitting the records. Store recovery uses DETECTED, SESSION_READY and FINAL/NO_FILL/UNANSWERABLE; re-entry boundaries are seeded from terminal records. load_session currently loads PATH_PRINT too, but these records do not drive these consumers. momentum_live_rule_baseline counts PATH_PRINT from its own generated replay, not stored paper rows. |
| PATH_PRINT writer can be off | AGREE | Drop PATH_PRINT at MomentumPaperService._persist only. Engine, paper orders, path mathematics, feed subscription, runtime snapshots and all other event writes remain unchanged. No new flag or sampling interval. |
| scanner_cycle_history is a standalone table | DISAGREE | Schema inspection and strategy_engine_app.py show DashboardSnapshot.snapshot_type='scanner_cycle_history'. The pruner accepts that logical name only and adds this exact predicate to count AND delete. Direct dashboard_snapshots target is refused. |
| Seven-day scanner history retention preserves restart fallback | AGREE | _restore_watchlist_from_scanner_cycle_history requires the current scanner session marker and persisted_at within that session. All current rows remain. No changes to its writer, row-count retention or restore path. |
| Forty-five calendar days protects the seed and repair | AGREE for current-session consumers | Age on bar_time, not ingestion created_at. Tests preserve 250 recent seed bars and the exact 45-day boundary; today's entire repair series remains. Seed/session-skip code is untouched. |
| Pullback export dependency permits this policy | ACCEPTED reviewer disposition | Reviewer 2026-10-07 RETAIN1 blockers-resolved ruling identifies strategy_bar_history as the export input and retains 45 days. No immutable export has been measured here; the prior export prerequisite is withdrawn under that ruling, not certified as an export receipt. |
| Reconciliation runs can be pruned independently | DISAGREE | findings -> runs has a non-cascading FK. Findings are visited first; an old run with ANY surviving child is retained. Dry-run parent count is labelled a lower bound before child deletion. |

## Preserved evidence

All physical trade tables are outside the prune allowlist. Paper deletion permits ONLY
PATH_PRINT and FEED_GAP, preserving not only FILLED/EXITED/FINAL/DETECTED but also
NO_FILL, UNANSWERABLE, SESSION_READY, SESSION_CLOSED and unknown future event types.
All dashboard state except the exact scanner_cycle_history type is protected, including
RPG jobs, retained mirror holds, scanner state and last-nonempty snapshots.

## Existing jobs and indexes (read-only box observations)

systemctl cat shows prune-ticks at 09:00 UTC with keep-days=30 and prune-capture
at 09:30 UTC with keep-days=14, both Persistent=true. The new definitions use ET
explicitly and Persistent=false so a missed overnight run cannot replay at market open.

pg_indexes: capture tables have received_at indexes; paper has event_type and observed_at
indexes; bars has a bar_time index; dashboard has snapshot_type/created_at/id.
No age-leading index was observed for tick received_at, finding created_at, run started_at
or scanner event_at. The 2026-10-07 17:15-17:16 ET read-only 50,000-row reconciliation
selectors took 1.159158 s and 0.481628 s for findings, 0.813721 s for runs. These are
SELECT measurements, not DELETE timings. No index/DDL or data deletion was performed.

## Resolved dependency / remaining execution prerequisite

Reviewer 2026-10-07: fleet-health and orphan checks are recent-only scheduled readers;
readc/scanner_capture/oco_capture cron paths write; historical study scripts are hand-run;
pullback reads retained bars. The seven-day rule stands. Dependency blocker is closed
by this explicit disposition, not by pretending the historical readers do not exist.

Nothing may be deleted before exact-head pin. Therefore the requested real 50,000-row
DELETE timing is deferred until after that pin AND job 2's actual completion tonight.
The new batch receipt measures DELETE elapsed time. Keep a batch size only after actual
timings meet approximately two seconds; selector timings alone cannot prove that.
