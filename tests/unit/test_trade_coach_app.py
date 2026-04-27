from __future__ import annotations

from datetime import UTC
from datetime import datetime
from datetime import timedelta

import pytest

from project_mai_tai.services.trade_coach_app import TradeCoachApp
from project_mai_tai.settings import Settings


@pytest.fixture
def fixed_session_start(monkeypatch: pytest.MonkeyPatch) -> datetime:
    session_start = datetime(2026, 4, 27, 8, 0, tzinfo=UTC)
    monkeypatch.setattr(
        "project_mai_tai.services.trade_coach_app.current_scanner_session_start_utc",
        lambda now=None: session_start,
    )
    return session_start


def test_trade_coach_review_window_defaults_to_all_completed_trade_history(
    fixed_session_start: datetime,
) -> None:
    app = TradeCoachApp(
        Settings(
            trade_coach_enabled=True,
            trade_coach_api_key="test-key",
            trade_coach_completed_trade_lookback_days=0,
        )
    )

    review_start, review_end = app._review_window_bounds()

    assert review_start == datetime(2000, 1, 1, tzinfo=UTC)
    assert review_end == fixed_session_start + timedelta(days=1)


def test_trade_coach_review_window_can_be_limited_to_recent_days(
    fixed_session_start: datetime,
) -> None:
    app = TradeCoachApp(
        Settings(
            trade_coach_enabled=True,
            trade_coach_api_key="test-key",
            trade_coach_completed_trade_lookback_days=3,
        )
    )

    review_start, review_end = app._review_window_bounds()

    assert review_start == fixed_session_start - timedelta(days=2)
    assert review_end == fixed_session_start + timedelta(days=1)
