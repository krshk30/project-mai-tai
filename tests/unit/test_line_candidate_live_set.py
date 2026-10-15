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
SAFETY = "strategy_schwab_1m_v2_cw_armed_segment_safety_enabled"
# FTFT operator fresh-flip rule, 76154752 (#993), retained by LINE=CHART.
# These nine retained SELLs precede this factory's watch/boot boundary. They
# are not proof of the actual historical scanner/ownership state.
PREWATCH_CASES = {
    ("2026-10-06", "OLOX"), ("2026-10-06", "RUBI"), ("2026-10-06", "WHLR"),
    ("2026-10-07", "BIYA"), ("2026-10-07", "MTEN"), ("2026-10-07", "SXTC"),
    ("2026-10-08", "IPW"), ("2026-10-08", "MEDS"), ("2026-10-09", "MI"),
}


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
    baseline_controls = {**replay_module.LIVE_CONTROLS, SAFETY: True}
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
    # 76154752 + reviewer C7 19:5x: compare the same reconstructed-slot policy,
    # not safety-OFF fresh claims against safety-ON consumed claims. All original
    # bars, decision fields and volume-isolation assertions remain unchanged.
    monkeypatch.setattr(replay_module, "LIVE_CONTROLS", baseline_controls)
    baseline = await audit_module.observe(deepcopy(row))
    monkeypatch.setattr(replay_module, "LIVE_CONTROLS", CANDIDATE)
    composed = await audit_module.observe(deepcopy(row))
    assert composed == baseline
    assert composed["general_callback_prefix_bars"] == 0
    assert composed["general_buffer_prefix_bars"] == 0
    assert composed["seed_db_bar_writes"] == 0
    assert composed["decisions"]["rebuild_buys"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("row", [r for r in ROWS if (r["day"], r["symbol"]) in PREWATCH_CASES],
                         ids=lambda row: f'{row["day"]}-{row["symbol"]}')
async def test_prewatch_sell_cannot_retain_safety_off_claims(monkeypatch, row):
    """76154752 / FTFT fresh-flip card: old parity would erase this safety gate."""
    replay_module = __import__("line_repair_preopen_replay")
    claims = []
    for enabled in (False, True):
        monkeypatch.setattr(replay_module, "LIVE_CONTROLS", {**CANDIDATE, SAFETY: enabled})
        case, _ = replay_module.setup(deepcopy(row), row["boundary_ms"], admission_control=True)
        case.bot._boot_state_restoration_complete = True
        case.bot._cw_boot_hold_check()
        assert await replay_module.repair(deepcopy(row), case, row["boundary_ms"])
        state = case.strategy.watchlist_state(row["symbol"])
        assert state.atr_state == "short"
        assert state.atr_short_flip_bar_ts <= case.strategy._boot_ms
        assert state.position_qty_held == 0
        assert case.snapshot()["buy_count"] == 0
        claims.append((state.cw_resting_taken, state.cw_reclaim_taken))
    assert claims == [(False, False), (True, True)]
