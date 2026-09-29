from __future__ import annotations

from datetime import UTC, datetime, timedelta

from project_mai_tai.strategy_core.orb_intrabar import OrbBar, completed_bar_macd_gate


OPEN = datetime(2026, 9, 29, 13, 30, tzinfo=UTC)


def _bar(minute: int, close: float) -> OrbBar:
    return OrbBar(
        timestamp=OPEN + timedelta(minutes=minute),
        open=close,
        high=close,
        low=close,
        close=close,
        volume=100,
    )


def test_macd_gate_ignores_forming_bar_then_reads_it_after_close() -> None:
    bars = [_bar(minute, float(minute + 35)) for minute in range(-34, 0)]
    bars.append(_bar(0, 1.0))

    at_open = completed_bar_macd_gate(bars, OPEN + timedelta(seconds=30))
    after_close = completed_bar_macd_gate(bars, OPEN + timedelta(minutes=1))

    assert at_open[0] is True
    assert at_open[1] == "nonnegative"
    assert after_close[0] is False
    assert after_close[1] == "negative"


def test_macd_gate_fails_closed_without_fresh_or_sufficient_history() -> None:
    short = [_bar(minute, float(minute + 10)) for minute in range(-9, 0)]
    assert completed_bar_macd_gate(short, OPEN) == (
        False,
        "insufficient_macd_history",
        None,
    )
    stale = [_bar(minute, float(minute + 35)) for minute in range(-35, -1)]
    assert completed_bar_macd_gate(stale, OPEN) == (False, "missing_last_closed_bar", None)
