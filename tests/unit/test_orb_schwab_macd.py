from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from project_mai_tai.db.models import StrategyBarHistory
from project_mai_tai.orb_schwab_macd import MacdVerdict, schwab_completed_bar_macd_gate

OPEN = datetime(2026, 9, 29, 13, 30, tzinfo=UTC)


def _factory() -> sessionmaker:
    engine = create_engine("sqlite://")
    StrategyBarHistory.__table__.create(engine)
    return sessionmaker(engine)


def _add_bars(factory: sessionmaker, *, symbol: str = "TEST", strategy: str = "schwab_1m_v2") -> None:
    with factory.begin() as session:
        for minute in range(-40, 0):
            close = Decimal(str(minute + 41))
            session.add(
                StrategyBarHistory(
                    strategy_code=strategy,
                    symbol=symbol,
                    interval_secs=60,
                    bar_time=OPEN + timedelta(minutes=minute),
                    open_price=close,
                    high_price=close,
                    low_price=close,
                    close_price=close,
                    volume=100,
                )
            )


def test_gate_uses_prior_completed_schwab_minute_not_forming_bar() -> None:
    factory = _factory()
    _add_bars(factory)
    with factory.begin() as session:
        session.add(
            StrategyBarHistory(
                strategy_code="schwab_1m_v2",
                symbol="TEST",
                interval_secs=60,
                bar_time=OPEN,
                open_price=Decimal("1"),
                high_price=Decimal("1"),
                low_price=Decimal("1"),
                close_price=Decimal("1"),
                volume=100,
            )
        )

    before_close = schwab_completed_bar_macd_gate(factory, "test", OPEN + timedelta(seconds=30))
    after_close = schwab_completed_bar_macd_gate(factory, "TEST", OPEN + timedelta(minutes=1))
    assert before_close[0] is MacdVerdict.ALLOWED
    assert before_close[1] == "nonnegative"
    assert after_close[0] is MacdVerdict.NEGATIVE
    assert after_close[1] == "negative"


def test_gate_rejects_missing_prior_minute_even_with_older_history() -> None:
    factory = _factory()
    _add_bars(factory)
    assert schwab_completed_bar_macd_gate(factory, "TEST", OPEN + timedelta(minutes=1)) == (
        MacdVerdict.BAR_NOT_YET,
        "missing_last_closed_schwab_bar",
        None,
    )


def test_gate_rejects_internal_one_minute_hole() -> None:
    factory = _factory()
    _add_bars(factory)
    with factory.begin() as session:
        row = session.query(StrategyBarHistory).filter_by(
            strategy_code="schwab_1m_v2", symbol="TEST", bar_time=OPEN - timedelta(minutes=7)
        ).one()
        session.delete(row)
    assert schwab_completed_bar_macd_gate(factory, "TEST", OPEN) == (
        MacdVerdict.BAR_NOT_YET,
        "missing_schwab_minute",
        None,
    )


def test_gate_never_substitutes_another_feed_or_symbol() -> None:
    factory = _factory()
    _add_bars(factory, strategy="orb")
    _add_bars(factory, symbol="OTHER")
    assert schwab_completed_bar_macd_gate(factory, "TEST", OPEN) == (
        MacdVerdict.BAR_NOT_YET,
        "insufficient_schwab_history",
        None,
    )


def test_gate_fails_closed_when_bar_store_unreadable() -> None:
    engine = create_engine("sqlite://")
    factory = sessionmaker(engine)
    assert schwab_completed_bar_macd_gate(factory, "TEST", OPEN) == (
        MacdVerdict.BAR_NOT_YET,
        "schwab_bar_read_error",
        None,
    )


def test_imcc_shaped_full_history_negative_refuses_even_when_last_26_looks_positive():
    from project_mai_tai.strategy_core.indicators import macd
    from project_mai_tai.strategy_core.schwab_1m_v2 import V2Indicators

    # Synthetic sign-divergence fixture, NOT a claim to reproduce IMCC's tape.
    closes = [5 + i * .02 for i in range(122)] + [7.42 + i * .002 for i in range(26)]
    assert macd(closes[-26:])["histogram"][-1] > 0
    expected = V2Indicators.macd(closes)[2]
    assert expected < 0
    factory = _factory()
    with factory.begin() as session:
        for i, close in enumerate(closes):
            session.add(StrategyBarHistory(
                strategy_code="schwab_1m_v2", symbol="IMCC", interval_secs=60,
                bar_time=OPEN - timedelta(minutes=len(closes) - i),
                open_price=close, high_price=close, low_price=close, close_price=close, volume=100,
            ))
    allowed, reason, value = schwab_completed_bar_macd_gate(factory, "IMCC", OPEN)
    assert allowed is MacdVerdict.NEGATIVE and reason == "negative"
    assert abs(value - expected) < 1e-10
