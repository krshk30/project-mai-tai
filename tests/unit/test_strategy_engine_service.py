from __future__ import annotations

from datetime import datetime
from decimal import Decimal

import pytest

from project_mai_tai.events import MarketSnapshotPayload, OrderEventEvent, OrderEventPayload
from project_mai_tai.services.strategy_engine_app import StrategyEngineService, StrategyEngineState, snapshot_from_payload
from project_mai_tai.strategy_core import ReferenceData


def fixed_now() -> datetime:
    return datetime(2026, 3, 28, 10, 0)


class FakeRedis:
    def __init__(self) -> None:
        self.entries: list[tuple[str, str]] = []

    async def xadd(self, stream: str, fields: dict[str, str]) -> str:
        self.entries.append((stream, fields["data"]))
        return "1-0"


def make_snapshot_payload(*, symbol: str, price: float, volume: int) -> MarketSnapshotPayload:
    return MarketSnapshotPayload(
        symbol=symbol,
        day_close=Decimal("2.10"),
        day_volume=volume,
        day_high=Decimal(str(price)),
        day_vwap=Decimal("2.22"),
        minute_close=Decimal(str(price)),
        minute_accumulated_volume=volume,
        minute_high=Decimal(str(price)),
        minute_vwap=Decimal("2.22"),
        last_trade_price=Decimal(str(price)),
        todays_change_percent=Decimal("12.5"),
    )


def seed_trending_bars(
    start_price: float = 2.0,
    count: int = 50,
    *,
    start_timestamp: float = 1_700_000_000.0,
    interval_secs: int = 30,
) -> list[dict[str, float | int]]:
    bars = []
    for index in range(count):
        close = start_price + index * 0.01
        bars.append(
            {
                "open": close - 0.01,
                "high": close + 0.02,
                "low": close - 0.02,
                "close": close,
                "volume": 20_000 + index * 50,
                "timestamp": start_timestamp + index * interval_secs,
            }
        )
    return bars


def test_snapshot_batch_keeps_single_confirmed_name_in_watchlist(monkeypatch) -> None:
    state = StrategyEngineState(now_provider=fixed_now)
    state.confirmed_scanner._confirmed = [
        {
            "ticker": "UGRO",
            "confirmed_at": "10:00:00 AM ET",
            "entry_price": 2.25,
            "price": 2.4,
            "change_pct": 12.5,
            "volume": 900_000,
            "rvol": 6.2,
            "shares_outstanding": 50_000,
            "bid": 2.39,
            "ask": 2.40,
            "spread": 0.01,
            "spread_pct": 0.42,
            "hod": 2.45,
            "vwap": 2.31,
            "prev_close": 2.13,
            "avg_daily_volume": 390_000,
            "first_spike_time": "09:55:00 AM ET",
            "first_spike_price": 2.10,
            "squeeze_count": 2,
            "data_age_secs": 0,
            "confirmation_path": "PATH_B_2SQ",
        }
    ]
    monkeypatch.setattr(state.alert_engine, "check_alerts", lambda snapshots, reference_data: [])
    monkeypatch.setattr(
        state.confirmed_scanner,
        "process_alerts",
        lambda alerts, reference_data, snapshot_lookup: [],
    )

    summary = state.process_snapshot_batch(
        [snapshot_from_payload(make_snapshot_payload(symbol="UGRO", price=2.4, volume=900_000))],
        {"UGRO": ReferenceData(shares_outstanding=50_000, avg_daily_volume=390_000)},
    )

    assert summary["watchlist"] == ["UGRO"]
    assert summary["top_confirmed"][0]["rank_score"] == 0.0
    assert "UGRO" in state.bots["macd_30s"].watchlist


def test_trade_tick_generates_open_intent_for_confirmed_watchlist(monkeypatch) -> None:
    state = StrategyEngineState(now_provider=fixed_now)
    bot = state.bots["macd_30s"]
    bot.set_watchlist(["UGRO"])
    state.seed_bars(
        "macd_30s",
        "UGRO",
        seed_trending_bars(start_timestamp=1_700_000_000.0, interval_secs=30),
    )
    monkeypatch.setattr(bot.indicator_engine, "calculate", lambda bars: {"price": 2.8})
    monkeypatch.setattr(
        bot.entry_engine,
        "check_entry",
        lambda symbol, indicators, bar_index, position_tracker: {
            "action": "BUY",
            "ticker": symbol,
            "path": "P1_MACD_CROSS",
            "price": 2.8,
            "score": 5,
            "score_details": "hist+ stK+ vwap+ vol+ macd+ emas-",
        },
    )

    intents = state.handle_trade_tick(
        symbol="UGRO",
        price=2.8,
        size=200,
        timestamp_ns=1_700_001_500_000_000_000,
    )

    assert intents
    open_intents = [intent for intent in intents if intent.payload.intent_type == "open"]
    assert open_intents
    assert open_intents[0].payload.symbol == "UGRO"
    assert open_intents[0].payload.strategy_code == "macd_30s"
    assert "UGRO" in bot.pending_open_symbols


@pytest.mark.asyncio
async def test_order_event_fill_opens_position_and_clears_pending_state() -> None:
    redis = FakeRedis()
    service = StrategyEngineService(redis_client=redis)
    bot = service.state.bots["macd_30s"]
    bot.pending_open_symbols.add("UGRO")

    order_event = OrderEventEvent(
        source_service="oms-risk",
        payload=OrderEventPayload(
            strategy_code="macd_30s",
            broker_account_name="paper:macd_30s",
            client_order_id="macd_30s-UGRO-open-abc123",
            symbol="UGRO",
            side="buy",
            intent_type="open",
            status="filled",
            quantity=Decimal("10"),
            filled_quantity=Decimal("10"),
            fill_price=Decimal("2.55"),
            reason="ENTRY_P1_MACD_CROSS",
            metadata={"path": "P1_MACD_CROSS", "reference_price": "2.55"},
        ),
    )

    await service._handle_stream_message(
        "test:order-events",
        {"data": order_event.model_dump_json()},
    )

    position = bot.positions.get_position("UGRO")
    assert position is not None
    assert position.quantity == 10
    assert position.entry_price == 2.55
    assert "UGRO" not in bot.pending_open_symbols
