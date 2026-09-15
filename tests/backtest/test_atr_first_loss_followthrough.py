from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest

from project_mai_tai.backtest.atr_first_loss_followthrough import (
    AtrOpportunity,
    SymbolDayStudy,
    _eligible_window,
    _post_stop_recovery,
    _rule_assessment,
    evaluate_opportunity,
    evaluate_symbol_day,
    run_study,
)
from project_mai_tai.backtest.atr_oracle import Bar
from project_mai_tai.backtest.confirmed_selection_census import ConfirmEvent, SymbolDayInput
from project_mai_tai.backtest.watch_start import WatchWindow

ET = ZoneInfo("America/New_York")
DAY = date(2026, 9, 15)


def _at(hour: int, minute: int = 0) -> datetime:
    return datetime.combine(DAY, time(hour, minute), tzinfo=ET)


def _bar(
    hour: int,
    minute: int,
    close: float,
    *,
    high: float | None = None,
    low: float | None = None,
) -> Bar:
    at = _at(hour, minute)
    return Bar(
        ts=int(at.timestamp() * 1000),
        open=close,
        high=close if high is None else high,
        low=close if low is None else low,
        close=close,
        volume=100,
    )


def _rows(*flips: str | None) -> list[dict[str, object]]:
    return [{"flip": flip} for flip in flips]


def _window(start: datetime | None = None, end: datetime | None = None) -> WatchWindow:
    return WatchWindow(
        int((start or _at(7)).timestamp() * 1000),
        int(end.timestamp() * 1000) if end else None,
    )


def test_flip_requires_a_preexisting_membership_that_survives_bar_close() -> None:
    flip = _at(8)
    decision = _at(8, 1)

    assert _eligible_window([_window()], flip, decision) is not None
    assert _eligible_window([_window(start=flip)], flip, decision) is None
    assert _eligible_window([_window(end=_at(8, 1))], flip, decision) is None


def test_stop_mfe_excludes_the_unordered_stop_bar_but_reports_its_possible_high() -> None:
    bars = [_bar(8, 0, 100), _bar(8, 1, 100, high=104, low=91)]

    result = evaluate_opportunity(
        day=DAY,
        symbol="TEST",
        sequence=1,
        bars=bars,
        atr_rows=_rows("BUY", None),
        index=0,
        window=_window(),
    )

    assert result.exit_kind == "STOP_8"
    assert result.return_pct == -8
    assert result.guaranteed_mfe_before_stop_pct == 0
    assert result.possible_mfe_before_stop_pct == pytest.approx(4)


def test_target_and_stop_in_one_minute_is_unknown_not_target_or_stop() -> None:
    bars = [_bar(8, 0, 100), _bar(8, 1, 100, high=106, low=91)]

    result = evaluate_opportunity(
        day=DAY,
        symbol="TEST",
        sequence=1,
        bars=bars,
        atr_rows=_rows("BUY", None),
        index=0,
        window=_window(),
    )

    assert result.exit_kind == "UNKNOWN_INTRABAR_ORDER"
    assert result.return_pct is None


def test_missing_bar_while_open_is_unknown_before_the_next_visible_price() -> None:
    bars = [_bar(8, 0, 100), _bar(8, 2, 106, high=106, low=100)]

    result = evaluate_opportunity(
        day=DAY,
        symbol="TEST",
        sequence=1,
        bars=bars,
        atr_rows=_rows("BUY", None),
        index=0,
        window=_window(),
    )

    assert result.exit_kind == "UNKNOWN_BAR_GAP"
    assert result.return_pct is None


def test_resting_target_or_stop_beats_the_bar_close_sell() -> None:
    target = evaluate_opportunity(
        day=DAY,
        symbol="TEST",
        sequence=1,
        bars=[_bar(8, 0, 100), _bar(8, 1, 103, high=106, low=99)],
        atr_rows=_rows("BUY", "SELL"),
        index=0,
        window=_window(),
    )
    sell = evaluate_opportunity(
        day=DAY,
        symbol="TEST",
        sequence=1,
        bars=[_bar(8, 0, 100), _bar(8, 1, 99, high=101, low=98)],
        atr_rows=_rows("BUY", "SELL"),
        index=0,
        window=_window(),
    )

    assert target.exit_kind == "TARGET_5"
    assert target.exit_at == _at(8, 1)
    assert sell.exit_kind == "ATR_SELL"
    assert sell.exit_at == _at(8, 2)
    assert sell.return_pct == pytest.approx(-1)


@pytest.mark.parametrize("non_buy", [None, "SELL"])
def test_false_touch_without_buy_flip_never_enters_the_population(
    monkeypatch, non_buy: str | None
) -> None:
    bars = (_bar(8, 0, 100), _bar(8, 1, 100), _bar(8, 2, 100))
    item = SymbolDayInput(
        DAY,
        "TEST",
        (ConfirmEvent("CONFIRM", _at(7), 10.0, 30.0, "PATH_C_EXTREME_MOVER"),),
        bars,
    )
    monkeypatch.setattr(
        "project_mai_tai.backtest.atr_first_loss_followthrough.compute_atr_trail",
        lambda *_args, **_kwargs: _rows(non_buy, None, None),
    )

    result = evaluate_symbol_day(item)

    assert result.opportunities == ()


def test_post_stop_recovery_does_not_count_the_stop_bars_high(monkeypatch) -> None:
    bars = (
        _bar(8, 0, 100),
        _bar(8, 1, 100, high=104, low=91),
        _bar(8, 2, 95, high=99, low=94),
    )
    item = SymbolDayInput(
        DAY,
        "TEST",
        (ConfirmEvent("CONFIRM", _at(7), 10.0, 30.0, "PATH_C_EXTREME_MOVER"),),
        bars,
    )
    monkeypatch.setattr(
        "project_mai_tai.backtest.atr_first_loss_followthrough.compute_atr_trail",
        lambda *_args, **_kwargs: _rows("BUY", None, "SELL"),
    )

    result = evaluate_symbol_day(item)

    assert result.opportunities[0].exit_kind == "STOP_8"
    assert result.post_first_stop_mfe_pct == pytest.approx(-1)
    assert result.post_first_stop_reached_original_target is None


def test_post_stop_no_recovery_is_false_only_with_complete_rest_of_day() -> None:
    first = _opportunity(1, -8, "STOP_8")
    assert first.exit_at is not None
    first = replace(first, exit_at=_at(8, 1))
    complete = [
        _bar(at.hour, at.minute, 99, high=99, low=94)
        for at in (
            _at(8, 2) + timedelta(minutes=index)
            for index in range(int((_at(15, 59) - _at(8, 2)).total_seconds() // 60) + 1)
        )
    ]

    mfe, recovered = _post_stop_recovery(complete, first)

    assert mfe == pytest.approx(-1)
    assert recovered is False


class _Source:
    def __init__(self, items: list[SymbolDayInput]) -> None:
        self.items = items

    def load(self, _start: date, _end: date) -> list[SymbolDayInput]:
        return self.items


def test_first_stop_then_later_real_flip_winner_is_reported(monkeypatch) -> None:
    bars = (
        _bar(8, 0, 100),
        _bar(8, 1, 100, high=103, low=91),
        _bar(8, 2, 95),
        _bar(8, 3, 100),
        _bar(8, 4, 105, high=106, low=99),
    )
    item = SymbolDayInput(
        DAY,
        "MYSZ",
        (ConfirmEvent("CONFIRM", _at(7), 10.0, 30.0, "PATH_C_EXTREME_MOVER"),),
        bars,
    )
    monkeypatch.setattr(
        "project_mai_tai.backtest.atr_first_loss_followthrough.compute_atr_trail",
        lambda *_args, **_kwargs: _rows("BUY", None, "SELL", "BUY", None),
    )

    report = run_study(_Source([item]), DAY, DAY)
    study = report.symbol_days[0]

    assert [row.exit_kind for row in study.opportunities] == ["STOP_8", "TARGET_5"]
    assert study.post_first_stop_reached_original_target is True
    stop_group = next(group for group in report.groups if group.first_outcome == "STOP_8")
    assert stop_group.with_later_opportunity == 1
    assert stop_group.later_targets == 1
    assert stop_group.later_return_sum_pct_points == 5


def test_unknown_first_trade_is_not_replaced_by_a_later_gradable_trade(monkeypatch) -> None:
    bars = (
        _bar(8, 0, 100),
        _bar(8, 1, 100, high=106, low=91),
        _bar(8, 2, 95),
        _bar(8, 3, 100),
        _bar(8, 4, 105, high=106, low=99),
    )
    item = SymbolDayInput(
        DAY,
        "TEST",
        (ConfirmEvent("CONFIRM", _at(7), 10.0, 30.0, "PATH_C_EXTREME_MOVER"),),
        bars,
    )
    monkeypatch.setattr(
        "project_mai_tai.backtest.atr_first_loss_followthrough.compute_atr_trail",
        lambda *_args, **_kwargs: _rows("BUY", None, "SELL", "BUY", None),
    )

    report = run_study(_Source([item]), DAY, DAY)

    assert report.first_outcomes == {"UNKNOWN_INTRABAR_ORDER": 1}


def _opportunity(sequence: int, return_pct: float, kind: str) -> AtrOpportunity:
    at = _at(8) + timedelta(minutes=sequence * 2)
    return AtrOpportunity(
        DAY,
        "TEST",
        sequence,
        at,
        at + timedelta(minutes=1),
        100,
        _at(7),
        kind,
        at + timedelta(minutes=1),
        100 * (1 + return_pct / 100),
        return_pct,
        0,
        0,
        0 if kind == "STOP_8" else None,
        0 if kind == "STOP_8" else None,
    )


def test_rule_requires_both_sample_floors_and_drop_one_negative_return() -> None:
    days = []
    for index in range(20):
        later = 2 if index < 10 else 1
        opportunities = [_opportunity(1, -8, "STOP_8")]
        opportunities.extend(_opportunity(offset + 2, -8, "STOP_8") for offset in range(later))
        days.append(
            SymbolDayStudy(
                DAY + timedelta(days=index),
                f"S{index:02d}",
                100,
                0,
                tuple(opportunities),
                0,
                False,
            )
        )

    result = _rule_assessment(days)

    assert result.later_gradable == 30
    assert result.first_stop_days_with_later == 20
    assert result.sample_floor_met is True
    assert result.survives_drop_one_symbol is True
    assert result.survives_drop_one_day is True
    assert result.pass_criterion is True

    below_floor = _rule_assessment(days[:-1])
    assert below_floor.sample_floor_met is False
    assert below_floor.pass_criterion is False

    positive = replace(
        days[0],
        opportunities=(days[0].opportunities[0], _opportunity(2, 1000, "ATR_SELL")),
    )
    positive_result = _rule_assessment([positive, *days[1:]])
    assert positive_result.later_pnl_negative is False
    assert positive_result.pass_criterion is False
