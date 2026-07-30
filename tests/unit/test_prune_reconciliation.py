"""The reconciliation pruner must respect the FK direction and never delete a live parent.

⭐ WHY IT EXISTS. `reconciliation_findings.reconciliation_run_id -> reconciliation_runs.id` has
**no ON DELETE CASCADE**. Two ways to get this wrong, both silent until they hit production data:
  1. delete runs before findings -> the DELETE errors out, or worse, half the prune lands
  2. delete an old run whose findings are still inside the retention window -> FK violation
So the order and the NOT EXISTS guard are the load-bearing parts, and they are what these tests pin.
"""
from __future__ import annotations

import pytest

import scripts.prune_reconciliation as mod


def test_findings_are_pruned_before_runs() -> None:
    """THE REGRESSION: the child must precede the parent, or the delete cannot succeed."""
    assert mod.PRUNE_ORDER.index("reconciliation_findings") < \
           mod.PRUNE_ORDER.index("reconciliation_runs")


def test_runs_delete_refuses_to_orphan_a_surviving_finding() -> None:
    """A run older than the cutoff whose findings are still recent must be LEFT ALONE. Without the
    guard this is an FK violation; with it, the run simply disappears on a later pass."""
    sql = mod.delete_sql("reconciliation_runs", 30)
    assert "NOT EXISTS" in sql
    assert "reconciliation_findings" in sql
    assert "f.reconciliation_run_id = r.id" in sql


def test_findings_delete_needs_no_guard() -> None:
    """Findings are the CHILD -- nothing references them, so a guard there would only slow it."""
    sql = mod.delete_sql("reconciliation_findings", 30)
    assert "NOT EXISTS" not in sql


def test_count_and_delete_share_the_same_predicate() -> None:
    """⛔ A dry run that counts rows the real run would not delete is worse than no dry run at all:
    it reports a number nobody can act on. Both must carry the guard for runs."""
    for table in mod.PRUNE_ORDER:
        c, d = mod.count_sql(table, 30), mod.delete_sql(table, 30)
        assert ("NOT EXISTS" in c) == ("NOT EXISTS" in d), table
        assert mod.AGE_COLUMN[table] in c and mod.AGE_COLUMN[table] in d


def test_each_table_ages_on_its_own_column() -> None:
    """`runs` has no created_at -- it ages on started_at. Assuming a shared column name silently
    prunes the wrong thing (or errors)."""
    assert mod.AGE_COLUMN["reconciliation_findings"] == "created_at"
    assert mod.AGE_COLUMN["reconciliation_runs"] == "started_at"


def test_an_unknown_table_cannot_reach_the_sql() -> None:
    """AGE_COLUMN is the allowlist AND the injection guard -- a table name never reaches SQL
    unless it is a key here."""
    with pytest.raises(KeyError):
        mod.delete_sql("dashboard_snapshots; DROP TABLE fills", 30)
    with pytest.raises(KeyError):
        mod.delete_sql("dashboard_snapshots", 30)   # ⛔ bloat, not retention -- wrong remedy


def test_keep_days_is_interpolated_as_an_int() -> None:
    """keep_days is formatted into the SQL string, so it must not be able to carry anything else."""
    sql = mod.delete_sql("reconciliation_findings", 30)
    assert "interval '30 days'" in sql
