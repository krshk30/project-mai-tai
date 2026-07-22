"""Tests for the study output constraint (backtest/study_report.py).

The point is not that the reporter runs — it is that each VALUE rule REFUSES its violation. Every
test breaks the rule and asserts the guard fires; a guard that has only ever passed proves nothing
(the mutation lesson). The dollar refusal, the unbounded-window refusal, and the name-vs-trade
drop-one are each pinned to the specific 2026-07-17 failure they encode.
"""
from __future__ import annotations

import pytest

from project_mai_tai.backtest.study_report import (
    BareDollarError,
    UnboundedWindowError,
    pinned_window,
    read_threshold_from_proc,
    report_returns,
)


# --------------------------------------------------------- 1. percentages, not dollars

def test_report_returns_refuses_empty_pct():
    """A dollar total with no per-trade % is exactly the banned output."""
    with pytest.raises(BareDollarError):
        report_returns("x", [])


def test_report_returns_refuses_dollars_without_pct():
    with pytest.raises(BareDollarError):
        report_returns("x", [], dollars_beside=[10.0, -3.0])


def test_report_returns_leads_with_median_and_returns_stats(capsys):
    stats = report_returns("cw", [-5.1, 2.0, 1.3, -4.7, 2.2], names=["A", "B", "C", "D", "E"])
    out = capsys.readouterr().out
    assert "MEDIAN" in out and out.index("MEDIAN") < out.index("mean")
    assert stats["n"] == 5 and stats["median"] == 1.3  # median of [-5.1,-4.7,1.3,2.0,2.2]


def test_dollars_only_ever_beside_never_headline(capsys):
    report_returns("cw", [2.0, -5.0], names=["A", "B"], dollars_beside=[25.0, -1.0])
    out = capsys.readouterr().out
    # the % headline comes first; dollars are explicitly labeled not-the-finding
    assert out.index("MEDIAN") < out.index("dollars")
    assert "NOT the finding" in out


# --------------------------------------------------------- 2. pin both date bounds

def test_pinned_window_ok():
    assert pinned_window("2026-07-10", "2026-07-16") == ("2026-07-10", "2026-07-16")


@pytest.mark.parametrize("lo,hi", [(None, "2026-07-16"), ("2026-07-10", None), (None, None)])
def test_pinned_window_refuses_missing_bound(lo, hi):
    with pytest.raises(UnboundedWindowError):
        pinned_window(lo, hi)


@pytest.mark.parametrize("bad", ["CURRENT_DATE-8", "now", "today", "infinity", ""])
def test_pinned_window_refuses_relative_bound(bad):
    """The exact 2026-07-17 bug: CURRENT_DATE-8 made decompose_11 unreproducible."""
    with pytest.raises(UnboundedWindowError):
        pinned_window(bad, "2026-07-16")


@pytest.mark.parametrize("relative", ["now", "today", "infinity", "current_date"])
def test_relative_bound_gives_the_GUIDING_message(relative):
    """The explicit relative-date check exists for its MESSAGE (fromisoformat would also reject these,
    but with a generic 'not an ISO date'). Pin the guidance so removing the check — which mutation-
    testing showed still RAISES via the fromisoformat backstop — is caught by the worse message here."""
    with pytest.raises(UnboundedWindowError, match="relative/open"):
        pinned_window(relative, "2026-07-16")


def test_pinned_window_refuses_inverted():
    with pytest.raises(UnboundedWindowError):
        pinned_window("2026-07-16", "2026-07-10")


# --------------------------------------------------------- name-level drop-one (the CW flip)

def test_dropone_is_name_level_not_trade_level(capsys):
    """The 2026-07-17 finding: a trade-level drop-one flattered CW's median while removing whole
    NAMES flipped its sign. Construct that: one name (BIG) carries the positive median; drop it and
    the median goes negative. A trade-level drop-one would not catch this."""
    pct = [10.0, 9.0, 8.0, -4.0, -5.0, -6.0]          # BIG's 3 wins hold the median positive
    names = ["BIG", "BIG", "BIG", "a", "b", "c"]
    stats = report_returns("flip", pct, names=names)
    lo, hi, kind = stats["dropone"]
    assert kind == "name-level"
    assert lo < 0 < hi          # dropping BIG flips the median negative -> the guard shows the flip
    assert "FLIPS the sign" in capsys.readouterr().out


def test_dropone_falls_back_to_trade_level_without_names(capsys):
    report_returns("nonames", [1.0, 2.0, 3.0, 4.0])
    assert "trade-level (no names given)" in capsys.readouterr().out


def test_median_mean_sign_disagreement_is_flagged(capsys):
    """The bimodal tell: median positive, mean negative (or vice versa) -> the reporter flags it so
    the distribution gets inspected rather than the headline trusted."""
    report_returns("bimodal", [1.0, 1.0, 1.0, -20.0], names=["a", "b", "c", "d"])
    assert "SIGN DISAGREE" in capsys.readouterr().out


# --------------------------------------------------------- 3. read the live env, not the default

def test_read_threshold_reports_source_on_miss():
    val, src = read_threshold_from_proc("definitely_not_a_real_setting_xyz", default="7")
    assert val == "7" and src.startswith("DEFAULT")


# --------------------------------------------------------- mutation: the guards must actually bite

def test_guards_are_not_vacuous():
    """A guard that never raises is not a guard. Assert the two load-bearing refusals raise on the
    exact violations, so a future edit that softens them (e.g. `if pct:` -> `if True:`) fails here."""
    with pytest.raises(BareDollarError):
        report_returns("v", [])
    with pytest.raises(UnboundedWindowError):
        pinned_window("CURRENT_DATE-8", "2026-07-16")
