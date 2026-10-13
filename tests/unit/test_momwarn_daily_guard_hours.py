from datetime import datetime
import importlib.util
from pathlib import Path
import sys
from zoneinfo import ZoneInfo

import pytest


PATH = Path(__file__).resolve().parents[2] / "ops/health/momwarn1_repair/optiona/G1/guard_rules.py"
SPEC = importlib.util.spec_from_file_location("daily_guard_hour_policy", PATH)
guard = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = guard
SPEC.loader.exec_module(guard)


@pytest.mark.parametrize("day", [13, 14])
@pytest.mark.parametrize("hour,index", [(4, 0), (6, 0), (7, 0), (8, 1), (9, 2), (10, 2), (17, 2)])
def test_daily_hour_baseline_mapping(day, hour, index):
    now = datetime(2026, 10, day, hour, 59, tzinfo=ZoneInfo("America/New_York"))
    assert guard.hour_index(now) == index


@pytest.mark.parametrize("hour", [3, 23])
def test_outside_observation_hours_stays_blind(hour):
    with pytest.raises(guard.Blind):
        guard.hour_index(datetime(2026, 10, 14, hour, tzinfo=ZoneInfo("America/New_York")))


def test_tail_uses_last_baseline_but_hard_end_refuses():
    assert guard.hour_index(datetime(2026, 10, 14, 18, 9, tzinfo=ZoneInfo("America/New_York"))) == 2
    with pytest.raises(guard.Blind):
        guard.hour_index(datetime(2026, 10, 14, 18, 10, tzinfo=ZoneInfo("America/New_York")))


def test_measured_hour_nine_thresholds_unchanged():
    assert guard.SNAPSHOT_BOUNDS[2] == 15.106
    assert guard.POOLED_BOUNDS[2] == 6.086
    assert {symbol: bounds[2] for symbol, bounds in guard.SYMBOL_BOUNDS.items()} == {
        "LGHL": 6.086, "NCI": 6.408, "VBIO": 6.222,
    }


def test_real_heartbeat_stop_is_not_relaxed():
    assert guard.SlowdownRules().heartbeat("healthy", guard.HEARTBEAT_AGE_BOUND + 1)
