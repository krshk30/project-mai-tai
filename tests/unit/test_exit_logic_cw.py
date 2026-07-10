"""Confirmed-window (variant CW) OMS exit legs — PR #2 of the confirmed-window ruleset.

Pins the two config-free ExitEngine methods that back the CW exit: a full +target%
close and a full -stop% close. The independence test proves check_hard_stop_pct uses
the passed pct, NOT the ladder's config.stop_loss_pct (1.5%), so CW's -5% never
disturbs the momentum-bot ladder. Settings-default test guards the tunables + the
default-off kill switch.
"""
from __future__ import annotations

from project_mai_tai.exit_logic.config import TradingConfig
from project_mai_tai.exit_logic.engine import ExitEngine
from project_mai_tai.exit_logic.position import Position
from project_mai_tai.settings import Settings


def _engine() -> ExitEngine:
    # The v2 ladder variant carries stop_loss_pct=1.5 — the CW legs must ignore it.
    return ExitEngine(TradingConfig().make_v2_variant())


def _pos(entry: float = 10.0) -> Position:
    return Position("TEST", entry, 10)


def test_cw_target_fires_at_plus_target_pct():
    eng = _engine()
    pos = _pos(10.0)  # +2% target = 10.20
    assert eng.check_full_target(pos, 10.19, 2.0) is None
    hit = eng.check_full_target(pos, 10.20, 2.0)  # exactly at target
    assert hit is not None
    assert hit["action"] == "CLOSE" and hit["reason"] == "CW_TARGET"
    assert eng.check_full_target(pos, 10.50, 2.0)["reason"] == "CW_TARGET"


def test_cw_hard_stop_fires_at_minus_stop_pct():
    eng = _engine()
    pos = _pos(10.0)  # -5% stop = 9.50
    assert eng.check_hard_stop_pct(pos, 9.51, 5.0) is None
    hit = eng.check_hard_stop_pct(pos, 9.50, 5.0)  # exactly at stop
    assert hit is not None
    assert hit["action"] == "CLOSE" and hit["reason"] == "CW_HARD_STOP"
    assert eng.check_hard_stop_pct(pos, 9.00, 5.0)["reason"] == "CW_HARD_STOP"


def test_cw_hard_stop_pct_independent_of_config_stop():
    eng = _engine()
    pos = _pos(10.0)
    # bid at -3% (9.70): the LADDER hard stop (config 1.5%) would fire...
    assert eng.check_hard_stop(pos, 9.70) is not None
    # ...but the CW -5% leg must NOT (only breaches at 9.50).
    assert eng.check_hard_stop_pct(pos, 9.70, 5.0) is None


def test_cw_legs_noop_when_pct_zero_or_no_position():
    eng = _engine()
    pos = _pos(10.0)
    assert eng.check_full_target(pos, 99.0, 0.0) is None
    assert eng.check_hard_stop_pct(pos, 0.01, 0.0) is None
    assert eng.check_full_target(None, 99.0, 2.0) is None
    assert eng.check_hard_stop_pct(None, 0.01, 5.0) is None


def test_cw_settings_defaults():
    s = Settings()
    assert s.oms_v2_cw_target_pct == 2.0
    assert s.oms_v2_cw_hard_stop_pct == 5.0
    # single switch, default off -> ladder path unchanged
    assert s.strategy_schwab_1m_v2_confirmed_window_enabled is False
