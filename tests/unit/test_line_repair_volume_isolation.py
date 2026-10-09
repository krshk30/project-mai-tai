"""[codex] Massive seed volume is isolated from every general callback reader."""
from copy import deepcopy
import importlib.util
import json
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location(
    "volume_isolation_audit", ROOT / "scripts/line_repair_volume_isolation.py")
audit_module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = audit_module
spec.loader.exec_module(audit_module)
ROWS = json.loads((ROOT / "tests/fixtures/line_repair_preopen_20261005_09.json").read_text())["rows"]


def test_scaling_is_prefix_only_and_does_not_mutate_recording():
    original = deepcopy(ROWS[0])
    changed = audit_module.scale_prefix(original, 100)
    assert original == ROWS[0]
    assert changed["schwab"] == original["schwab"]
    assert changed["chart"] == original["chart"]
    assert [bar["v"] for bar in changed["preopen"]["results"]] == [
        int(bar["v"] * 100) for bar in original["preopen"]["results"]]


@pytest.mark.asyncio
async def test_mi_prefix_stays_in_math_ledger_not_general_buffer_or_vwap():
    row = next(row for row in ROWS if row["day"] == "2026-10-09" and row["symbol"] == "MI")
    baseline = await audit_module.observe(row)
    zero = await audit_module.observe(audit_module.scale_prefix(row, 0))
    assert baseline["general_callback_prefix_bars"] == 0
    assert baseline["general_buffer_prefix_bars"] == 0
    assert baseline["relative_volume_window_prefix_bars"] == 0
    assert baseline == zero
    assert baseline["decisions"]["state"] == "short"
    assert round(baseline["decisions"]["level"], 4) == 1.3511
    assert baseline["liquidity_floor_input_volume"] == zero["liquidity_floor_input_volume"]
    assert baseline["seed_db_bar_writes"] == zero["seed_db_bar_writes"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("row", ROWS, ids=lambda row: f'{row["day"]}-{row["symbol"]}')
async def test_recorded_seed_volumes_x0_x100_do_not_reach_any_general_reader(row):
    result = await audit_module.audit(row)
    assert result["prefix_volume_decisions_identical"]
    assert result["volume_reader_inputs_identical"]
    assert result["volume_isolation"] in {"PASS", "NO_PREFIX_CONTROL"}
    for sample in result["samples"].values():
        assert sample["general_callback_prefix_bars"] == 0
        assert sample["general_buffer_prefix_bars"] == 0
        assert sample["relative_volume_window_prefix_bars"] == 0
        assert sample["seed_db_bar_writes"] == 0


@pytest.mark.asyncio
async def test_unseeded_nxts_control_is_reported_separately():
    row = next(row for row in ROWS if row["symbol"] == "NXTS")
    result = await audit_module.audit(row)
    assert result["prefix_volume_decisions_identical"]
    assert result["volume_isolation"] == "NO_PREFIX_CONTROL"
