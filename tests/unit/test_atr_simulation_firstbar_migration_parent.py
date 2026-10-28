"""Source-only parent contracts. Not Alembic traversal or PG/DDL acceptance."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_existing_unapplied_sim_revision_follows_installed_tip_not_l12():
    source = (ROOT / "sql/migrations/versions/20261023_atr_simulation_store.py").read_text()
    assert 'revision = "20261023_atr_simulation"' in source
    assert 'down_revision = "20261015_bot_control"' in source
    assert 'depends_on = None' in source
    assert "20261022_l12_retirement" not in source
    assert "brokerdecides_dispatch" not in source


def test_parent_amendment_does_not_add_duplicate_sim_revision():
    sources = [path for path in (ROOT / "sql/migrations/versions").glob("*.py")
               if 'revision = "20261023_atr_simulation"' in path.read_text().splitlines()]
    assert [path.name for path in sources] == ["20261023_atr_simulation_store.py"]
