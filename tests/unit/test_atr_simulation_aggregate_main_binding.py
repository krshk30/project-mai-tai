"""Main package/graph wiring; no DB, model, migration application or boot."""

from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory
import sqlalchemy as sa

from project_mai_tai.atr_simulation import models
from project_mai_tai.atr_simulation.store_schema_20261023 import simulation_metadata as initial_metadata
from project_mai_tai.atr_simulation.store_schema_20261023_aggregate import CHECKS_AFTER, CHECKS_BEFORE


def test_current_package_uses_additive_checks_without_changing_initial_metadata():
    def checks(table):
        return {item.name: str(item.sqltext) for item in table.constraints
                if isinstance(item, sa.CheckConstraint)}
    assert checks(models.rows) == CHECKS_AFTER
    assert checks(initial_metadata().tables["atr_simulation_rows"]) == CHECKS_BEFORE
    assert set(models.metadata.tables) == set(initial_metadata().tables)


def test_main_graph_has_one_head_with_the_additive_child_after_initial_l14():
    root = Path(__file__).resolve().parents[2]
    config = Config()
    config.set_main_option("script_location", str(root / "sql/migrations"))
    script = ScriptDirectory.from_config(config)
    heads = script.get_heads()
    assert len(heads) == 1
    assert script.get_revision("20261023_atr_aggregate").down_revision == "20261023_atr_simulation"
    assert script.get_revision("20261023_atr_simulation").down_revision == "20261015_bot_control"
    assert "20261023_atr_aggregate" in {
        item.revision for item in script.iterate_revisions(heads[0], "base")}
