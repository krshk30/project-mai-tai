from datetime import UTC, datetime, timedelta
from decimal import Decimal

from project_mai_tai.strategy_core.orb_intrabar import OrbBar
from project_mai_tai.strategy_core.orb_schwab_open import OrbSchwabOpeningOrder


OPEN = datetime(2026, 9, 29, 13, 30, tzinfo=UTC)


def _bar(index: int, high: float, *, qualified: bool = True) -> OrbBar:
    return OrbBar(
        timestamp=OPEN - timedelta(minutes=5 - index),
        open=high - 0.1,
        high=high,
        low=high - 0.2,
        close=high - 0.05,
        volume=1000,
        breakout_high=high if qualified else 0.0,
    )


def _close(order: OrbSchwabOpeningOrder, index: int, high: float, **kwargs):
    bar = _bar(index, high, qualified=kwargs.pop("qualified", True))
    return order.on_closed_bar(
        bar,
        observed_at=bar.timestamp + timedelta(minutes=1, seconds=kwargs.pop("lag", 0)),
        macd_allowed=kwargs.pop("macd_allowed", True),
        macd_reason=kwargs.pop("macd_reason", "nonnegative"),
    )


def test_place_after_three_then_raise_after_fourth_and_fifth() -> None:
    order = OrbSchwabOpeningOrder(OPEN)
    order.bars[_bar(0, 5.3).timestamp] = _bar(0, 5.3)
    order.bars[_bar(1, 5.4).timestamp] = _bar(1, 5.4)
    assert _close(order, 2, 5.5).kind == "place"
    assert order.last_requested_level == Decimal("5.5")
    assert _close(order, 3, 5.6).kind == "reprice"
    assert _close(order, 4, 5.7).kind == "reprice"
    assert order.last_requested_level == Decimal("5.7")


def test_never_place_without_first_three_complete_bars() -> None:
    order = OrbSchwabOpeningOrder(OPEN)
    order.bars[_bar(0, 5.3).timestamp] = _bar(0, 5.3)
    assert _close(order, 2, 5.5) is None
    assert _close(order, 3, 5.6) is None
    assert not order.placed


def test_negative_completed_bar_cancels_existing_order_not_second_buy() -> None:
    order = OrbSchwabOpeningOrder(OPEN)
    for index in (0, 1):
        order.bars[_bar(index, 5.3).timestamp] = _bar(index, 5.3)
    assert _close(order, 2, 5.5).kind == "place"
    action = _close(order, 3, 5.6, macd_allowed=False, macd_reason="negative")
    assert action.kind == "cancel"
    assert action.reason == "macd_negative"
    assert _close(order, 4, 5.7) is None


def test_late_bar_cannot_place_or_reprice() -> None:
    order = OrbSchwabOpeningOrder(OPEN)
    for index in (0, 1):
        order.bars[_bar(index, 5.3).timestamp] = _bar(index, 5.3)
    assert _close(order, 2, 5.5, lag=6) is None
    assert not order.placed

    order = OrbSchwabOpeningOrder(OPEN)
    for index in (0, 1):
        order.bars[_bar(index, 5.3).timestamp] = _bar(index, 5.3)
    assert _close(order, 2, 5.5).kind == "place"
    assert _close(order, 3, 5.6, lag=6).kind == "cancel"


def test_only_qualified_prints_set_level_and_no_downward_reprice() -> None:
    order = OrbSchwabOpeningOrder(OPEN)
    order.bars[_bar(0, 5.3).timestamp] = _bar(0, 5.3)
    order.bars[_bar(1, 6.0, qualified=False).timestamp] = _bar(1, 6.0, qualified=False)
    action = _close(order, 2, 5.5)
    assert action.level == Decimal("5.5")
    assert _close(order, 3, 5.4) is None
