"""Age-prune `reconciliation_findings` + `reconciliation_runs`.

⭐ WHY. Measured 2026-07-30: `reconciliation_findings` is **1148 MB / 2,567,609 rows**, oldest
`2026-03-30`, and has **never been pruned** (`last_autovacuum` 2026-05-18, `n_dead_tup` 0 -- so this
is real live data, not bloat). **2,427,331 rows (95%) are older than 30 days.** `reconciliation_runs`
is 353,959 rows of which 267,719 (76%) are older than 30 days.

These are DIAGNOSTICS. The reconciler detects drift and never repairs; nothing reads a finding from
five weeks ago. Disk is not under pressure (23% used, 89 GB free) -- this is hygiene.

⛔⭐ ORDER MATTERS -- FINDINGS BEFORE RUNS. There is a foreign key
`reconciliation_findings.reconciliation_run_id -> reconciliation_runs.id` with **no ON DELETE
CASCADE**. Deleting runs first fails outright, and deleting a run whose findings are still inside the
retention window would fail too -- so the runs pass carries a `NOT EXISTS` guard and only removes
runs that own no surviving findings. A run kept alive by a recent finding is correct, not a leak; it
disappears on a later pass once that finding ages out.

⛔ DRY-RUN BY DEFAULT, `--go` to execute -- matching `prune_strategy_bar_history.py`.
   Note `prune_market_ticks.py` uses the OPPOSITE convention (deletes unless `--dry-run`). That
   inconsistency is a foot-gun; for a DELETE the safe default is the one that does nothing.

⛔ NOT the remedy for `dashboard_snapshots`. That table looks similar (1020 MB) but has **zero** rows
older than 7 days and only ~5.7 MB of live payload -- its GB is TOAST bloat from per-message row
replacement, so it needs VACUUM FULL, not deletion. Pruning it would delete live state and reclaim
nothing. See the polygon-freeze work: the same hot write path causes both.

  cd /home/trader/project-mai-tai && .venv/bin/python scripts/prune_reconciliation.py
  cd /home/trader/project-mai-tai && .venv/bin/python scripts/prune_reconciliation.py --go
"""
from __future__ import annotations

import argparse
import os

import psycopg

# Deleting anything not in this map is a bug, not a policy choice -- and it also blocks SQL
# injection through a table name. Each entry maps table -> the column its age is judged on.
AGE_COLUMN = {
    "reconciliation_findings": "created_at",
    "reconciliation_runs": "started_at",
}

# ⛔ Findings FIRST. The FK points findings -> runs, so the child must go before the parent.
PRUNE_ORDER = ("reconciliation_findings", "reconciliation_runs")


def _dsn(arg: str | None) -> str:
    raw = arg or os.environ.get("MAI_TAI_DATABASE_URL", "")
    if not raw:
        raise SystemExit("no DSN: pass --dsn or set MAI_TAI_DATABASE_URL")
    return raw.replace("postgresql+psycopg://", "postgresql://")


def count_sql(table: str, keep_days: int) -> str:
    """Rows this pass WOULD remove. Mirrors delete_sql's predicate exactly, including the guard."""
    col = AGE_COLUMN[table]
    cutoff = f"now() - interval '{int(keep_days)} days'"
    if table == "reconciliation_runs":
        return (
            f"SELECT count(*) FROM {table} r WHERE r.{col} < {cutoff} "
            f"AND NOT EXISTS (SELECT 1 FROM reconciliation_findings f "
            f"WHERE f.reconciliation_run_id = r.id)"
        )
    return f"SELECT count(*) FROM {table} WHERE {col} < {cutoff}"


def delete_sql(table: str, keep_days: int) -> str:
    """One bounded batch. Bounded so a 2.4M-row delete never takes one giant lock or one giant
    transaction on a 2-vCPU box that is also running a live OMS."""
    col = AGE_COLUMN[table]
    cutoff = f"now() - interval '{int(keep_days)} days'"
    if table == "reconciliation_runs":
        return (
            f"DELETE FROM {table} WHERE id IN ("
            f"  SELECT r.id FROM {table} r WHERE r.{col} < {cutoff} "
            f"  AND NOT EXISTS (SELECT 1 FROM reconciliation_findings f "
            f"                  WHERE f.reconciliation_run_id = r.id) "
            f"  ORDER BY r.id LIMIT %s)"
        )
    return (
        f"DELETE FROM {table} WHERE id IN ("
        f"  SELECT id FROM {table} WHERE {col} < {cutoff} ORDER BY id LIMIT %s)"
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep-days", type=int, default=30)
    ap.add_argument("--batch", type=int, default=25_000)
    ap.add_argument("--go", action="store_true", help="actually delete (default: report only)")
    ap.add_argument("--dsn", default=None)
    args = ap.parse_args()

    if args.keep_days < 7:
        raise SystemExit(f"refusing --keep-days {args.keep_days}: too aggressive, minimum 7")

    with psycopg.connect(_dsn(args.dsn)) as conn:
        for table in PRUNE_ORDER:
            with conn.cursor() as cur:
                cur.execute(count_sql(table, args.keep_days))
                stale = cur.fetchone()[0]
            if not args.go:
                note = ""
                if table == "reconciliation_runs":
                    # ⭐ Say so explicitly. This count is taken BEFORE the findings pass has
                    # deleted anything, so every run still owning a soon-to-be-deleted finding is
                    # excluded by the guard and invisible here. Measured 2026-07-30: the dry run
                    # said 101,168 while 267,719 runs were actually older than the cutoff. A dry
                    # run that quietly under-reports by 2.6x is a number someone will act on.
                    note = (" [LOWER BOUND -- counted before the findings pass; the real run will "
                            "remove more as findings age out and release their parents]")
                print(f"{table}: {stale} rows older than {args.keep_days}d "
                      f"(DRY RUN -- nothing deleted; pass --go){note}")
                continue
            deleted = 0
            while True:
                with conn.cursor() as cur:
                    cur.execute(delete_sql(table, args.keep_days), (args.batch,))
                    n = cur.rowcount
                conn.commit()          # commit per batch: never one 2.4M-row transaction
                deleted += n
                if n < args.batch:
                    break
            print(f"{table}: deleted {deleted} rows older than {args.keep_days}d")

    if args.go:
        print("[note] space is returned to the OS only after VACUUM; autovacuum will follow up. "
              "Run VACUUM (ANALYZE) explicitly if you need the reclaim measured now.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
