"""A hole in the v2 bar series silently corrupts ATR — fleet health must see it.

⭐ WHY (2026-07-30, live money). True range references `prev.close`, so a gap makes ONE bar carry
the whole outage. v2 stopped 10:12-11:33 ET; NUWE's ATR read 0.149 vs a true ~0.06, and
`loss = 3.5 * ATR` put the resting buy-stop at 4.74 while the operator's chart showed ~4.40.
⛔ Gaps are NOT restart-only — same day, no outage: CRWU 25 min, AXTU 2-13 min, SNDG 3 min.
Nobody was checking, so nobody knew.
"""
from __future__ import annotations

from ops.health.fleet_health_check import classify_bar_continuity as c


def test_no_bars_is_never_a_fault() -> None:
    """⛔ THE NO-FALSE-ALARM GUARD: off-hours / empty watchlist must not red. A check that cries
    wolf every evening gets ignored by the morning."""
    assert c(None, None, 0)[0] == "GREEN"
    assert c(85, 4, 0)[0] == "GREEN"


def test_contiguous_is_green() -> None:
    assert c(0, 0, 300)[0] == "GREEN"
    assert c(None, 0, 300)[0] == "GREEN"


def test_a_small_gap_is_amber_not_red() -> None:
    """SNDG's real 3-minute gap: worth seeing, not worth paging."""
    level, detail = c(3, 1, 200)
    assert level == "AMBER"
    assert "3min" in detail


def test_the_restart_hole_is_RED() -> None:
    """THE REGRESSION: the 85-minute hole that mispriced every resting order."""
    level, detail = c(85, 6, 200)
    assert level == "RED"
    assert "85min" in detail
    # RETARGETED 2026-08-03: this used to pin `"Restart v2" in detail`. That advice was WRONG —
    # #620 guards live ATR, and a restart punches a fresh hole, which is the condition this very
    # check detects. A characterization test that pins a since-killed behaviour is a landmine:
    # anyone "fixing" the code to satisfy it would restore the harmful advice.
    assert "Restart v2" not in detail
    assert "Do NOT restart on this alert alone" in detail


def test_the_thresholds_are_pinned() -> None:
    """Pin the VALUES, not just the ordering — 2min amber / 10min red."""
    assert c(1, 1, 200)[0] == "GREEN"
    assert c(2, 1, 200)[0] == "AMBER"
    assert c(9, 1, 200)[0] == "AMBER"
    assert c(10, 1, 200)[0] == "RED"


def test_the_detail_names_the_consequence_not_just_the_number() -> None:
    """An operator reading this at 09:31 must know WHY it matters without opening the code."""
    # The consequence is now stated honestly: the DB series is holed (the backtest/parity/recorder
    # read it), while LIVE ATR is guarded by #620. The old text claimed live ATR was "materially
    # wrong", which was false once #620 shipped.
    assert "backtest" in c(85, 6, 200)[1]
    assert "#620" in c(85, 6, 200)[1]
    assert "ATR" in c(3, 1, 200)[1]
