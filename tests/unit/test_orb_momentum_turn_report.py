import sys
from datetime import date, time, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from orb_momentum_turn_report import (  # noqa: E402
    BarPoint,
    PopulationName,
    QuotePoint,
    admissible_seed_bars,
    at_et,
    replay_rows,
    summarize,
)
from project_mai_tai.strategy_core.schwab_1m_v2 import V2Indicators  # noqa: E402

DAY = date(2026, 8, 26)
NAME = PopulationName(DAY, "CRE", time(9, 30, 42), Decimal("10"), "B")


def bar(minute: int, close: str, volume: int = 100) -> BarPoint:
    value = Decimal(close)
    return BarPoint(
        at_et(DAY, time(9, minute)),
        value,
        value + Decimal("0.10"),
        value - Decimal("0.10"),
        value,
        volume,
        "live",
    )


def settings():
    return SimpleNamespace(
        strategy_schwab_1m_v2_atr_flip_period=5,
        strategy_schwab_1m_v2_atr_flip_factor=3.5,
        strategy_schwab_1m_v2_atr_flip_variant="B",
        strategy_schwab_1m_v2_atr_flip_quantity=1,
        strategy_schwab_1m_v2_atr_flip_vol_floor=0,
        strategy_schwab_1m_v2_atr_flip_use_max_state_age=False,
        strategy_schwab_1m_v2_atr_flip_max_state_age=5,
        strategy_schwab_1m_v2_atr_flip_rearm_enabled=False,
        strategy_schwab_1m_v2_hold_confirm_enabled=False,
        strategy_schwab_1m_v2_confirmed_window_enabled=False,
        strategy_schwab_1m_v2_cw_v2_enabled=False,
        strategy_schwab_1m_v2_macd_probe_symbols="",
        strategy_schwab_1m_v2_atr_flip_probe_symbols="",
        strategy_schwab_1m_v2_atr_only_mode=True,
    )


def full_history(closes: list[str]) -> list[BarPoint]:
    start = at_et(DAY, time(8, 45))
    result = []
    for index, close in enumerate(closes):
        value = Decimal(close)
        result.append(
            BarPoint(
                start + timedelta(minutes=index),
                value,
                value + Decimal("0.10"),
                value - Decimal("0.10"),
                value,
                100 + index,
                "live",
            )
        )
    return result


def test_report_uses_deployed_macd_math_and_detects_cross_down() -> None:
    closes = [str(10 + index * 0.1) for index in range(44)] + [
        "14.0",
        "13.0",
        "12.0",
        "11.0",
        "10.0",
    ]
    bars = full_history(closes)

    rows = replay_rows(settings=settings(), name=NAME, bars=bars, halts=[], quotes=[])
    final = next(row for row in rows if row.minute == bars[-1].at)
    expected = V2Indicators.macd([float(value) for value in closes], 12, 26, 9)

    assert expected is not None
    assert final.macd == Decimal(str(expected[0]))
    assert final.signal == Decimal(str(expected[1]))
    assert final.histogram == Decimal(str(expected[2]))
    assert any(row.macd_cross_down for row in rows)


def test_volume_average_is_v2_trailing_twenty_including_current_bar() -> None:
    bars = full_history(["10"] * 46)

    rows = replay_rows(settings=settings(), name=NAME, bars=bars, halts=[], quotes=[])
    final = next(row for row in rows if row.minute == bars[-1].at)
    expected = sum(item.volume for item in bars[-20:]) / 20

    assert final.average_volume == Decimal(str(expected))
    assert final.volume_ratio == Decimal(final.volume) / Decimal(str(expected))


def test_missing_minutes_are_retained_and_marked_no_data() -> None:
    rows = replay_rows(
        settings=settings(),
        name=NAME,
        bars=[bar(25, "10"), bar(27, "11")],
        halts=[],
        quotes=[],
    )

    missing = next(row for row in rows if row.minute == at_et(DAY, time(9, 26)))
    assert len(rows) == 36
    assert missing.close is None
    assert missing.macd is None


def test_halt_minute_is_flagged_without_removing_its_row() -> None:
    from project_mai_tai.market_halts import HaltWindow

    halt = HaltWindow(at_et(DAY, time(9, 25)), at_et(DAY, time(9, 30)), 3)
    rows = replay_rows(
        settings=settings(),
        name=NAME,
        bars=[bar(26, "10")],
        halts=[halt],
        quotes=[],
    )

    row = next(item for item in rows if item.minute == at_et(DAY, time(9, 26)))
    assert row.halted is True
    assert row.close == Decimal("10")


def test_summary_compares_first_cross_and_atr_flip_with_low_minute() -> None:
    rows = replay_rows(
        settings=settings(),
        name=NAME,
        bars=full_history(["10"] * 44 + ["11", "9"]),
        halts=[],
        quotes=[QuotePoint(at_et(DAY, time(9, 30)), Decimal("10"))],
    )
    report = summarize(NAME, rows)

    assert report.high_at is not None
    assert report.low_at is not None
    assert report.macd_cross_at is not None
    assert report.macd_cross_at <= report.low_at
    assert report.complete_through_ten is False


class _ScalarResult:
    def __init__(self, value: bool) -> None:
        self.value = value

    def scalar(self) -> bool:
        return self.value


class _CalendarSession:
    def __init__(self, answers: list[bool]) -> None:
        self.answers = iter(answers)
        self.calls = []

    def execute(self, _statement, params):
        self.calls.append(params)
        return _ScalarResult(next(self.answers))


def test_stale_seed_is_rejected_like_the_deployed_v2_boundary_guard() -> None:
    seed = [bar(29, "10"), bar(28, "9")]
    session = _CalendarSession([True])

    kept = admissible_seed_bars(session, seed, DAY + timedelta(days=2))

    assert kept == []
    assert len(session.calls) == 1


def test_contiguous_seed_is_replayed_oldest_first() -> None:
    newest = bar(29, "10")
    oldest = bar(28, "9")
    session = _CalendarSession([])

    kept = admissible_seed_bars(session, [newest, oldest], DAY + timedelta(days=1))

    assert kept == [oldest, newest]
    assert len(session.calls) == 0
