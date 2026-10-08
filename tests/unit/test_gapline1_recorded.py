"""Recorded history controls; stored bars do not attest historical delivery timing."""

import json
import importlib.util
import re
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from project_mai_tai.settings import Settings
from project_mai_tai.strategy_core.schwab_1m_v2 import (
    OHLCVBar, SchwabV2Strategy, session_start_ts_ms,
)

FIXTURE = json.loads((Path(__file__).parents[1] / "fixtures/gapline1_recorded_holds.json").read_text())
ET = ZoneInfo("America/New_York")


def recorded_prefix(case):
    day = datetime.fromtimestamp(case["detected_ms"] / 1000, UTC).astimezone(ET).date().isoformat()
    rows = FIXTURE["sessions"].get(day + ":" + case["symbol"], [])
    bars = [OHLCVBar(int(datetime.fromisoformat(row[0]).timestamp() * 1000),
                    *map(float, row[1:5]), int(row[5])) for row in rows]
    match = re.search(r"ts_ms=(\d+)", case["prior"] or "")
    # A missing prior probe is not evidence of a delivered pre-hole bar.
    if match is None:
        return []
    prior = int(match.group(1))
    if session_start_ts_ms(prior) != session_start_ts_ms(case["detected_ms"]):
        return []
    return [bar for bar in bars if bar.timestamp_ms <= prior]


@pytest.mark.parametrize("case", FIXTURE["holds"], ids=lambda row: (
    f'{row["kind"]}-{row["symbol"]}-{row["detected_ms"]}'
))
def test_recorded_hold_preserves_available_session_indicator_without_a_new_seed(case):
    bars = recorded_prefix(case)
    if not bars:
        pytest.skip("UNMEASURED: no retained same-session prefix/prior probe for this hold")
    strategy = SchwabV2Strategy(Settings(
        strategy_schwab_1m_v2_gap_hold_enabled=True,
        strategy_schwab_1m_v2_gap_line_carry_enabled=True,
        strategy_schwab_1m_v2_line_chart_restoration_enabled=True,
    ))
    strategy._now_ms = lambda: case["detected_ms"]
    state = strategy.watchlist_state(case["symbol"])
    for bar in bars:
        strategy._update_atr_state(state, bar, state_only=True)
    before = strategy._atr_indicator_snapshot(state)
    if case["kind"] == "initial":
        assert strategy.begin_gap_hold(
            state.symbol, detected_at_ms=case["detected_ms"],
            last_bar_age_s=91, last_print_age_s=1,
        )
    else:
        # Recovery-gap detector has already held the symbol. Use the recorded
        # last delivered bar and the current closed minute, not wall-time as a bar.
        state.gap_hold_active = True
        state.gap_hold_last_live_bar_ms = bars[-1].timestamp_ms
        current_ms = case["detected_ms"] // 60_000 * 60_000 - 60_000
        day = datetime.fromtimestamp(case["detected_ms"] / 1000, UTC).astimezone(ET).date().isoformat()
        current = next((row for row in FIXTURE["sessions"].get(day + ":" + state.symbol, [])
                        if int(datetime.fromisoformat(row[0]).timestamp() * 1000) == current_ms), None)
        if current is None or current_ms - bars[-1].timestamp_ms <= 90_000:
            pytest.skip("UNMEASURED: recovery pair not retained with this prior probe")
        strategy._prepare_gap_hold_bar(state, OHLCVBar(current_ms, *map(float, current[1:5]), int(current[5])))
        assert state.gap_hold_contiguous_bars == 1
    assert strategy._atr_indicator_snapshot(state) == before
    assert state.gap_hold_active
    assert state.line_restore_reset_after_ms == 0
    assert not strategy._pending_intents
    assert not strategy._pending_webull_direct_intents
    assert not strategy._pending_webull_fanout_intents


def test_recorded_population_keeps_the_full_denominator_and_raw_provenance():
    assert len(FIXTURE["holds"]) == 133
    assert sum(row["kind"] == "initial" for row in FIXTURE["holds"]) == 96
    assert sum(row["kind"] == "recovery" for row in FIXTURE["holds"]) == 37
    assert FIXTURE["raw_sha256"] == "fe2cda938daa5768344351ce8ef587f3ba194e7e27a77e1ee329a5573199f9b8"


def test_recorded_carry_state_and_first_flip_population(capsys):
    path = Path(__file__).parents[2] / "docs/review-artifacts/gapline1/replay_build.py"
    spec = importlib.util.spec_from_file_location("gapline1_build_replay", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.main()
    result = json.loads(capsys.readouterr().out)
    assert len(result["readings"]) == 14
    assert all(row["same_colour"] and row["open_drafts"] == 0 for row in result["readings"])
    # The eight named mathematical flips include MEDS: its recorded probe was
    # late, not absent. This does not relabel that case a recovered live entry.
    named = [row for row in result["readings"]
             if row["symbol"] in {"CLRO", "EGG", "MEDS", "APUS", "SXTC"}
             and row["oracle_first_buy_ms"] is not None]
    assert len(named) == 8
    assert all(row["first_buy_mathematically_visible"] for row in named)
    assert len(result["entry_controls"]) == 13
    assert all(row["same_colour"] for row in result["entry_controls"])
