"""Fail-closed ORB MACD input from v2's Schwab one-minute bars.

This module reads decisions' market data only. It cannot submit an order.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from math import isfinite
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from project_mai_tai.db.models import StrategyBarHistory
from project_mai_tai.strategy_core.orb_intrabar import OrbBar, completed_bar_macd_gate

logger = logging.getLogger(__name__)
_ET = ZoneInfo("America/New_York")
_SCHWAB_STRATEGY = "schwab_1m_v2"
_MACD_BARS = 35
_ONE_MINUTE = timedelta(minutes=1)
BAR_WAIT = timedelta(seconds=90)


class MacdVerdict(StrEnum):
    ALLOWED = "allowed"
    NEGATIVE = "negative"
    BAR_NOT_YET = "bar_not_yet"


def last_closed_bar_close(evaluated_at: datetime) -> datetime:
    return evaluated_at.astimezone(UTC).replace(second=0, microsecond=0)


def schwab_completed_bar_macd_gate(
    session_factory: sessionmaker[Session], symbol: str, evaluated_at: datetime
) -> tuple[MacdVerdict, str, float | None]:
    """Use only today's persisted Schwab bars through the prior closed minute.

    A missing minute or a delayed v2 write is not filled from gateway/Massive
    prices. The caller may retry when Schwab history arrives, but must not arm
    or retain a still-unfilled order beyond the bounded missing-bar wait.
    """
    if evaluated_at.tzinfo is None:
        return MacdVerdict.BAR_NOT_YET, "invalid_evaluation_time", None
    ticker = symbol.strip().upper()
    if not ticker:
        return MacdVerdict.BAR_NOT_YET, "invalid_symbol", None

    evaluated_utc = evaluated_at.astimezone(UTC)
    last_minute = evaluated_utc.replace(second=0, microsecond=0) - _ONE_MINUTE
    morning_start = evaluated_at.astimezone(_ET).replace(
        hour=7, minute=0, second=0, microsecond=0
    ).astimezone(UTC)
    if last_minute < morning_start:
        return MacdVerdict.BAR_NOT_YET, "before_schwab_morning_history", None

    try:
        with session_factory() as session:
            rows = session.scalars(
                select(StrategyBarHistory)
                .where(
                    StrategyBarHistory.strategy_code == _SCHWAB_STRATEGY,
                    StrategyBarHistory.symbol == ticker,
                    StrategyBarHistory.interval_secs == 60,
                    StrategyBarHistory.bar_time >= morning_start,
                    StrategyBarHistory.bar_time <= last_minute,
                )
                .order_by(StrategyBarHistory.bar_time)
            ).all()
    except Exception:
        logger.exception("[ORB-SCHWAB-MACD] bar read unavailable symbol=%s", ticker)
        return MacdVerdict.BAR_NOT_YET, "schwab_bar_read_error", None

    if len(rows) < _MACD_BARS:
        return MacdVerdict.BAR_NOT_YET, "insufficient_schwab_history", None
    bars: list[OrbBar] = []
    for row in rows:
        bar_time = row.bar_time
        if bar_time.tzinfo is None:
            # SQLite test fixtures discard UTC tzinfo; production Postgres retains it.
            bar_time = bar_time.replace(tzinfo=UTC)
        bar_time = bar_time.astimezone(UTC)
        close = float(row.close_price)
        if not isfinite(close) or close <= 0:
            return MacdVerdict.BAR_NOT_YET, "invalid_schwab_close", None
        bars.append(
            OrbBar(
                timestamp=bar_time,
                open=close,
                high=close,
                low=close,
                close=close,
                volume=float(row.volume),
            )
        )

    if bars[-1].timestamp != last_minute:
        return MacdVerdict.BAR_NOT_YET, "missing_last_closed_schwab_bar", None
    recent = bars[-_MACD_BARS:]
    if any(right.timestamp - left.timestamp != _ONE_MINUTE for left, right in zip(recent, recent[1:])):
        return MacdVerdict.BAR_NOT_YET, "missing_schwab_minute", None
    allowed, reason, histogram = completed_bar_macd_gate(bars, evaluated_utc)
    if reason == "negative":
        return MacdVerdict.NEGATIVE, reason, histogram
    if allowed:
        return MacdVerdict.ALLOWED, reason, histogram
    return MacdVerdict.BAR_NOT_YET, reason, histogram
