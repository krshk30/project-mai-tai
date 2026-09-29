import asyncio
from datetime import UTC, datetime, timedelta

from project_mai_tai.services.orb_schwab_app import OrbSchwabService
from project_mai_tai.settings import Settings
from project_mai_tai.strategy_core.orb_intrabar import OrbBar


OPEN = datetime(2026, 9, 29, 13, 30, tzinfo=UTC)


def _bar(index: int, level: float) -> OrbBar:
    return OrbBar(
        timestamp=OPEN - timedelta(minutes=5 - index),
        open=level,
        high=level,
        low=level,
        close=level,
        volume=200,
        breakout_high=level,
    )


def test_live_service_emits_one_open_two_reprices_and_no_paper_order(monkeypatch) -> None:
    service = OrbSchwabService(
        settings=Settings(
            orb_enabled=True,
            orb_live_schwab_orders_enabled=True,
            strategy_schwab_1m_v2_account_name="live:schwab_1m_v2",
            strategy_schwab_1m_v2_broker_provider="schwab",
        ),
        session_factory=lambda: None,
    )
    service._universe = {"CLRO"}
    monkeypatch.setattr(service, "_session_open_utc", lambda: OPEN)
    processing_at = [OPEN]
    monkeypatch.setattr(service, "_processing_time", lambda: processing_at[0])
    monkeypatch.setattr(
        "project_mai_tai.services.orb_schwab_app.schwab_completed_bar_macd_gate",
        lambda *_args: (True, "nonnegative", 0.1),
    )
    emitted = []

    async def capture(_redis, _settings, event, _now):
        emitted.append(event)

    monkeypatch.setattr(
        "project_mai_tai.services.orb_schwab_app.publish_orb_schwab_intent", capture
    )
    for index, level in enumerate((5.3, 5.4, 5.5, 5.6, 5.7)):
        bar = _bar(index, level)
        processing_at[0] = bar.timestamp + timedelta(minutes=1)
        service._on_bar("CLRO", bar, observed_at=bar.timestamp + timedelta(minutes=1))
        asyncio.run(service._process_closed_bars())
    assert [event.payload.reason for event in emitted] == [
        "ORB_FIXED_HIGH_SCHWAB_STOP_LIMIT",
        "ORB_RAISE_EXISTING_SCHWAB_STOP_LIMIT",
        "ORB_RAISE_EXISTING_SCHWAB_STOP_LIMIT",
    ]
    assert [event.payload.intent_type for event in emitted] == ["open", "cancel", "cancel"]
    assert [event.payload.metadata["stop_price"] for event in emitted] == [
        "5.50", "5.60", "5.70"
    ]
    assert service._states == {}


def test_negative_completed_schwab_bar_requests_cancel_not_reprice(monkeypatch) -> None:
    service = OrbSchwabService(
        settings=Settings(
            orb_enabled=True,
            orb_live_schwab_orders_enabled=True,
            strategy_schwab_1m_v2_account_name="live:schwab_1m_v2",
            strategy_schwab_1m_v2_broker_provider="schwab",
        ),
        session_factory=lambda: None,
    )
    service._universe = {"CLRO"}
    monkeypatch.setattr(service, "_session_open_utc", lambda: OPEN)
    processing_at = [OPEN]
    monkeypatch.setattr(service, "_processing_time", lambda: processing_at[0])
    decisions = iter(((True, "nonnegative", 0.1), (False, "negative", -0.1)))
    monkeypatch.setattr(
        "project_mai_tai.services.orb_schwab_app.schwab_completed_bar_macd_gate",
        lambda *_args: next(decisions),
    )
    emitted = []

    async def capture(_redis, _settings, event, _now):
        emitted.append(event)

    monkeypatch.setattr(
        "project_mai_tai.services.orb_schwab_app.publish_orb_schwab_intent", capture
    )
    for index in range(4):
        bar = _bar(index, 5.3 + index * 0.1)
        processing_at[0] = bar.timestamp + timedelta(minutes=1)
        service._on_bar("CLRO", bar, observed_at=bar.timestamp + timedelta(minutes=1))
        asyncio.run(service._process_closed_bars())
    assert [event.payload.intent_type for event in emitted] == ["open", "cancel"]
    assert emitted[-1].payload.metadata == {"orb_schwab_cancel": "true"}


def test_on_time_market_timestamp_does_not_hide_late_processing(monkeypatch) -> None:
    service = OrbSchwabService(
        settings=Settings(
            orb_enabled=True,
            orb_live_schwab_orders_enabled=True,
            strategy_schwab_1m_v2_account_name="live:schwab_1m_v2",
            strategy_schwab_1m_v2_broker_provider="schwab",
        ),
        session_factory=lambda: None,
    )
    service._universe = {"CLRO"}
    monkeypatch.setattr(service, "_session_open_utc", lambda: OPEN)
    monkeypatch.setattr(
        "project_mai_tai.services.orb_schwab_app.schwab_completed_bar_macd_gate",
        lambda *_args: (True, "nonnegative", 0.1),
    )
    emitted = []

    async def capture(_redis, _settings, event, _now):
        emitted.append(event)

    monkeypatch.setattr(
        "project_mai_tai.services.orb_schwab_app.publish_orb_schwab_intent", capture
    )
    for index in range(3):
        bar = _bar(index, 5.3 + index * 0.1)
        service._on_bar("CLRO", bar, observed_at=bar.timestamp + timedelta(minutes=1))
    monkeypatch.setattr(service, "_processing_time", lambda: OPEN - timedelta(minutes=1, seconds=53))
    asyncio.run(service._process_closed_bars())
    assert emitted == []
