"""Backfilling the backtest's ground-truth table must be insert-only and provenance-stamped.

⭐ WHY (2026-07-30). 27 gaps / 761 missing bars in one day. The REST warmup repairs the strategy's
in-memory deque but never writes the bars back, so the DB stays holed and the backtest, the parity
study and the recorder's mfe/mae all read a discontinuous series.

⛔ A backfilled bar is NOT byte-identical to a live-built one — Polygon vs Schwab bars agreed on
only 54.2% of ATR flips. Without provenance, filling a hole makes the parity study silently compare
two sources while looking clean.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import scripts.report_bar_gaps as mod


def _t(minute: int) -> datetime:
    return datetime(2026, 7, 30, 14, minute, tzinfo=UTC)


def test_missing_minutes_excludes_both_present_bars() -> None:
    """THE OFF-BY-ONE THAT MATTERS: the two bars bounding a hole EXIST. Re-inserting them would
    collide with live rows; the unique key would save us, but the arithmetic must be right."""
    out = mod.missing_minutes(_t(10), _t(14))
    assert out == [_t(11), _t(12), _t(13)]


def test_an_adjacent_pair_has_nothing_missing() -> None:
    assert mod.missing_minutes(_t(10), _t(11)) == []


def test_the_85_minute_hole_yields_84_bars() -> None:
    """The real shape: 10:11 -> 11:36 was reported as 84 bars missing."""
    start = datetime(2026, 7, 30, 14, 11, tzinfo=UTC)
    assert len(mod.missing_minutes(start, start + timedelta(minutes=85))) == 84


def test_the_insert_is_provenance_stamped() -> None:
    """Every backfilled row must be identifiable — it is also the only clean undo."""
    assert "'rest'" in mod.INSERT_SQL
    assert "source" in mod.INSERT_SQL


def test_the_insert_can_never_overwrite_a_live_bar() -> None:
    """⛔ THE LOAD-BEARING SAFETY. A live-recorded bar is the truth for what the bot actually saw.
    ON CONFLICT DO NOTHING makes that structural, not a matter of the gap arithmetic being right."""
    sql = mod.INSERT_SQL.upper()
    assert "ON CONFLICT" in sql and "DO NOTHING" in sql
    assert "UPDATE" not in sql and "DELETE" not in sql


def test_it_only_touches_the_v2_series() -> None:
    assert mod.STRATEGY_CODE == "schwab_1m_v2"
    assert mod.INTERVAL_SECS == 60


def test_tiny_jitter_is_not_treated_as_a_hole() -> None:
    """A 1-minute 'gap' is just the next bar; REST round trips are not free."""
    assert mod.MIN_GAP_MINUTES >= 2


def test_a_single_missing_bar_IS_repairable() -> None:
    """⛔⭐ THE DEAD BAND (2026-08-03). The comparison must be INCLUSIVE.

    The detector that pages on this same series alerts on `nxt - bar_time > 1 minute`, i.e. one
    missing bar. This repairer used to fill on `> MIN_GAP_MINUTES`, i.e. two. A gap of EXACTLY
    2 minutes therefore alerted every 15 minutes and could never be repaired — live that morning
    EDBL paged AMBER while the repair printed "series is contiguous" in the same log line, and the
    alert told the operator to restart a healthy real-money service.

    A watch that cannot fix what it reports trains the operator to ignore the channel.
    """
    assert ">= make_interval" in mod.GAPS_SQL, (
        "GAPS_SQL must select gaps INCLUSIVE of MIN_GAP_MINUTES; `>` re-opens the dead band "
        "against the detector's >1-minute alert threshold"
    )


def test_the_repairer_covers_everything_the_detector_alerts_on() -> None:
    """The two thresholds must stay reconcilable, so state the relationship as an assertion.

    detector alerts when  gap >  1 min   (>= 1 missing bar)
    repairer fills when   gap >= MIN_GAP_MINUTES

    Any repairer floor above 2 leaves gaps that alert forever and never repair.
    """
    detector_alerts_at_minutes = 2  # first gap size satisfying the detector's `> 1 minute`
    assert mod.MIN_GAP_MINUTES <= detector_alerts_at_minutes


def test_the_insert_supplies_every_NOT_NULL_column() -> None:
    """⛔ `indicators` is NOT NULL with NO default and was missing from the first version — the
    live repair aborted on it. Pins the whole set so the next added column fails HERE, not at
    02:00 in a cron nobody is watching."""
    required = (
        "strategy_code", "symbol", "interval_secs", "bar_time",
        "open_price", "high_price", "low_price", "close_price",
        "volume", "position_state", "indicators",
    )
    cols = mod.INSERT_SQL.split("VALUES")[0]
    for c in required:
        assert c in cols, f"NOT NULL column {c!r} missing from the insert"


def test_backfilled_indicators_are_EMPTY_not_invented() -> None:
    """A bar we never evaluated has no indicator snapshot. Fabricating one would put invented
    decision state into the table the backtest treats as ground truth."""
    assert "'{}'::json" in mod.INSERT_SQL
