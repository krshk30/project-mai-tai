"""Candidate settings from a read-only v2 process snapshot; no live IO."""
from copy import deepcopy
import json
from pathlib import Path

import pytest

from project_mai_tai.settings import Settings
from tests.unit.test_line_repair_volume_isolation import audit_module, ROWS

ROOT = Path(__file__).resolve().parents[2]
PROCESS = json.loads((ROOT / "tests/fixtures/line_candidate_live_flags_20261009.json").read_text())
RECORDED = {
    key.removeprefix("MAI_TAI_").lower(): int(value) if key.endswith("_MAX_RETRIES")
    else value == "true"
    for key, value in PROCESS["flags"].items()
}
RETIRED_ORB_KEYS = {"orb_intrabar_reclaim_enabled", "orb_resting_entry_enabled",
                    "orb_running_high_enabled"}
CANDIDATE = {key: value for key, value in RECORDED.items() if key not in RETIRED_ORB_KEYS}
CANDIDATE["strategy_schwab_1m_v2_line_chart_restoration_enabled"] = True


def test_candidate_changes_only_line_from_recorded_process_flags():
    assert PROCESS["pid"] == 2178316
    assert len(PROCESS["flags"]) == 100
    assert set(RECORDED) - set(Settings.model_fields) == RETIRED_ORB_KEYS
    settings = Settings(_env_file=None, **CANDIDATE)
    changed = []
    for key, value in PROCESS["flags"].items():
        name = key.removeprefix("MAI_TAI_").lower()
        if name in RETIRED_ORB_KEYS:
            continue
        recorded = int(value) if key.endswith("_MAX_RETRIES") else value == "true"
        if getattr(settings, name) != recorded:
            changed.append(name)
    assert changed == ["strategy_schwab_1m_v2_line_chart_restoration_enabled"]
    for name in ("gap_line_carry", "false_flip", "slotclear_fresh_flip", "slotclear_fresh_sell"):
        assert getattr(settings, "strategy_schwab_1m_v2_" + name + "_enabled")
    assert settings.oms_v2_webull_mirror_retained_hold_enabled
    assert not settings.strategy_schwab_1m_v2_atr_reprice_handoff_enabled
    # The older standalone seed flag is not read by the joined-session seed path.
    assert not settings.strategy_schwab_1m_v2_atr_massive_seed_enabled
    assert settings.strategy_schwab_1m_v2_retry_one_max_retries == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("row", ROWS, ids=lambda row: f'{row["day"]}-{row["symbol"]}')
async def test_joined_line_with_every_recorded_process_flag_preserves_decisions(monkeypatch, row):
    replay_module = __import__("line_repair_preopen_replay")
    baseline = await audit_module.observe(deepcopy(row))
    monkeypatch.setattr(replay_module, "LIVE_CONTROLS", CANDIDATE)
    original_setup = replay_module.setup

    def restored_control(*args, **kwargs):
        case, provider = original_setup(*args, **kwargs)
        # This composition uses the factory's empty durable-state control, not
        # the separate real-table Monday boot proof required for installation.
        case.bot._boot_state_restoration_complete = True
        case.bot._cw_boot_hold_check()
        assert not case.strategy._entries_held
        return case, provider

    monkeypatch.setattr(replay_module, "setup", restored_control)
    composed = await audit_module.observe(deepcopy(row))
    assert composed == baseline
    assert composed["general_callback_prefix_bars"] == 0
    assert composed["general_buffer_prefix_bars"] == 0
    assert composed["seed_db_bar_writes"] == 0
    assert composed["decisions"]["rebuild_buys"] == 0
