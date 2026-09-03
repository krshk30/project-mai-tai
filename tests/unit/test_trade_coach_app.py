from __future__ import annotations

from datetime import UTC
from datetime import datetime
from datetime import timedelta
from pathlib import Path

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


@pytest.mark.asyncio
async def test_disabled_trade_coach_exits_before_review_scoring_or_writes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = TradeCoachApp(
        Settings(
            trade_coach_enabled=False,
            trade_coach_api_key="test-key",
        )
    )

    async def fail_if_called(**kwargs) -> None:
        del kwargs
        raise AssertionError("disabled Trade Coach started a review cycle")

    monkeypatch.setattr(app.service, "run_review_cycle", fail_if_called)

    await app.run()


def test_trade_coach_unit_does_not_force_enable_switch() -> None:
    unit_path = (
        Path(__file__).resolve().parents[2]
        / "ops"
        / "systemd"
        / "project-mai-tai-trade-coach.service"
    )
    unit = unit_path.read_text(encoding="utf-8")

    assert "MAI_TAI_TRADE_COACH_ENABLED=true" not in unit
    assert "EnvironmentFile=/etc/project-mai-tai/project-mai-tai.env" in unit
