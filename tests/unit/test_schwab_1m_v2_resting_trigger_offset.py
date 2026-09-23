"""Cross-path contracts for the resting trigger offset."""

from __future__ import annotations

import inspect

import pytest

from project_mai_tai.settings import Settings
from project_mai_tai.strategy_core.schwab_1m_v2 import SchwabV2Strategy, SymbolState


def test_trigger_offset_defaults_to_zero() -> None:
    assert Settings().strategy_schwab_1m_v2_cw_v2_resting_trigger_offset_pct == 0.0


def test_fill_latch_clears_line_and_trigger_together() -> None:
    state = SymbolState(symbol="TEST")
    state.resting_active = True
    state.resting_level = 10.0
    state.resting_trigger = 10.05

    assert SchwabV2Strategy._clear_resting_fill_latch(state) is True
    assert state.resting_level == 0.0
    assert state.resting_trigger == 0.0


@pytest.mark.parametrize(
    ("method", "marker"),
    [
        (SchwabV2Strategy._queue_resting_place, "[V2-RESTING-PLACE]"),
        (SchwabV2Strategy._queue_resting_cancel, "[V2-RESTING-CANCEL]"),
        (SchwabV2Strategy._queue_resting_place, "[V2-RESTING-EH-ARM]"),
        (SchwabV2Strategy._eh_resting_cross_check, "[V2-RESTING-EH-CROSS]"),
        (SchwabV2Strategy._queue_resting_cancel, "[V2-RESTING-EH-DISARM]"),
        (SchwabV2Strategy._queue_resting_place, "[V2-WEBULL-RESTING-PLACE]"),
        (SchwabV2Strategy._queue_resting_cancel, "[V2-WEBULL-RESTING-CANCEL]"),
        (SchwabV2Strategy._fanout_rth_resting_cross, "[V2-FANOUT-RTH-RESTING]"),
        (SchwabV2Strategy._resting_stop_ask_allows, "[V2-STOP-ASK-PRICE-CHECK]"),
    ],
)
def test_resting_markers_expose_line_trigger_band_and_offset(method, marker: str) -> None:
    source = inspect.getsource(method)
    start = source.rindex(marker)
    marker_format = source[start : start + 700]

    assert "line=%.4f" in marker_format
    assert "trigger=%.4f" in marker_format
    assert "band_pct=%.2f" in marker_format
    assert "offset_pct=%.2f" in marker_format


def test_every_price_cross_and_fill_anchor_reads_the_trigger() -> None:
    eh = inspect.getsource(SchwabV2Strategy._eh_resting_cross_check)
    assert "px < trigger" in eh
    assert "cap = trigger *" in eh

    rth = inspect.getsource(SchwabV2Strategy._fanout_rth_resting_cross)
    assert "obs_px < trigger" in rth
    assert "obs_px >= trigger" in rth
    assert "px < trigger" in rth
    assert "band_anchor=trigger" in rth

    fill = inspect.getsource(SchwabV2Strategy.update_position)
    assert "entry_px=resting_trigger" in fill
    assert "band_anchor=resting_trigger" in fill
