"""Shared CW-v2 exit decision (exit_logic.cw_exit) — used by BOTH the OMS and the backtest,
so this pins the single source of truth (backtest==live parity, 2026-07-14)."""
from __future__ import annotations

from project_mai_tai.exit_logic.cw_exit import cw_exit_decision

# entry 100, target +2%, stop -5%, floor +2%.
KW = dict(target_pct=2.0, stop_pct=5.0, floor_pct=2.0)


# --- floor OFF: current live behaviour (hard target / stop / flip), byte-identical ---


def test_off_hard_target():
    assert cw_exit_decision(100, 102.0, False, floor_enabled=False, flip_pending=False, **KW) == ("target", False)


def test_off_hard_stop():
    assert cw_exit_decision(100, 95.0, False, floor_enabled=False, flip_pending=False, **KW) == ("stop", False)


def test_off_flip_and_hold():
    assert cw_exit_decision(100, 100.5, False, floor_enabled=False, flip_pending=True, **KW) == ("flip", False)
    assert cw_exit_decision(100, 100.5, False, floor_enabled=False, flip_pending=False, **KW) == ("hold", False)


def test_off_target_beats_stop_and_flip():
    # a bid at +2% with a pending flip still takes the +2% target (precedence)
    assert cw_exit_decision(100, 102.0, False, floor_enabled=False, flip_pending=True, **KW) == ("target", False)


# --- floor ON: arm at +2%, ride, exit on fall-back-to-floor ---


def test_floor_arms_at_target_not_exit():
    assert cw_exit_decision(100, 102.0, False, floor_enabled=True, flip_pending=False, **KW) == ("arm", True)


def test_floor_pre_arm_stop_and_flip_and_hold():
    assert cw_exit_decision(100, 95.0, False, floor_enabled=True, flip_pending=False, **KW) == ("stop", False)
    assert cw_exit_decision(100, 101.0, False, floor_enabled=True, flip_pending=True, **KW) == ("flip", False)
    assert cw_exit_decision(100, 101.0, False, floor_enabled=True, flip_pending=False, **KW) == ("hold", False)


def test_floor_rides_above_floor():
    # armed, well above the floor -> keep holding (the point: don't cap at +2%)
    assert cw_exit_decision(100, 105.0, True, floor_enabled=True, flip_pending=False, **KW) == ("hold", True)


def test_floor_exits_on_fallback_to_floor():
    # floor = 100*(1+2/100) = 102; a bid at/under 102 while armed -> floor exit
    assert cw_exit_decision(100, 102.0, True, floor_enabled=True, flip_pending=False, **KW) == ("floor", False)
    assert cw_exit_decision(100, 101.9, True, floor_enabled=True, flip_pending=False, **KW) == ("floor", False)


def test_floor_flip_while_riding():
    # armed, above floor, a bar-close flip -> exit on the flip (captures the ride)
    assert cw_exit_decision(100, 104.0, True, floor_enabled=True, flip_pending=True, **KW) == ("flip", False)
