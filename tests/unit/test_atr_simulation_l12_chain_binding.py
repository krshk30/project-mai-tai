"""Main schema-only composition; no production, copy or service qualification."""

from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory
import pytest


def scripts():
    config = Config()
    config.set_main_option("script_location", str(Path(__file__).resolve().parents[2] / "sql/migrations"))
    return ScriptDirectory.from_config(config)


def test_l12_schema_composition_is_one_forward_chain_after_both_sim_revisions():
    script = scripts()
    assert script.get_heads() == ["20261022_l12_retirement"]
    tip = list(script.iterate_revisions("20261022_l12_retirement", "20261015_bot_control"))
    assert [revision.revision for revision in tip] == [
        "20261022_l12_retirement", "20261023_atr_aggregate", "20261023_atr_simulation"]
    assert [revision.down_revision for revision in tip] == [
        "20261023_atr_aggregate", "20261023_atr_simulation", "20261015_bot_control"]


def test_composed_l12_retains_explicit_evidence_preserving_downgrade_refusal():
    revision = scripts().get_revision("20261022_l12_retirement")
    with pytest.raises(RuntimeError, match="retain L12 closure evidence"):
        revision.module.downgrade()
