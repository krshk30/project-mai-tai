from __future__ import annotations

import json
from importlib import import_module
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from project_mai_tai.db.base import Base
from project_mai_tai.db.models import (
    BrokerAccount,
    BrokerOrder,
    DashboardSnapshot,
    SchwabIneligibleToday,
    Strategy,
    StrategyBarHistory,
    TradeIntent,
    VirtualPosition,
)
from project_mai_tai.events import (
    HistoricalBarPayload,
    HistoricalBarsEvent,
    HistoricalBarsPayload,
    LiveBarEvent,
    LiveBarPayload,
    MarketSnapshotPayload,
    OrderEventEvent,
    OrderEventPayload,
    SnapshotBatchEvent,
    TradeIntentEvent,
    TradeIntentPayload,
    TradeTickEvent,
    TradeTickPayload,
)
from project_mai_tai.services.strategy_engine_app import (
    StrategyBotRuntime,
    StrategyDefinition,
    StrategyEngineService,
    StrategyEngineState,
    current_scanner_session_start_utc,
    order_routing_metadata,
    snapshot_from_payload,
)
from project_mai_tai.settings import Settings
from project_mai_tai.market_data.massive_indicator_provider import MassiveIndicatorProvider
from project_mai_tai.market_data.models import HistoricalBarRecord, LiveBarRecord, QuoteTickRecord, TradeTickRecord
from project_mai_tai.market_data.taapi_indicator_provider import TaapiIndicatorProvider
from project_mai_tai.strategy_core import IndicatorConfig, OHLCVBar, ReferenceData, TradingConfig
from project_mai_tai.strategy_core.exit import ExitEngine
from project_mai_tai.strategy_core.feed_retention import FeedRetentionMetrics
from project_mai_tai.strategy_core.time_utils import EASTERN_TZ


def make_test_settings(**kwargs) -> Settings:
    """Build Settings for unit tests: disable scanner feed retention unless a retention field is set.

    When :func:`scanner_feed_retention` is enabled, :meth:`StrategyBotRuntime.set_watchlist` syncs
    from lifecycle state; tests that call ``set_watchlist`` directly need retention off.
    """
    if not any(str(k).startswith("scanner_feed_retention") for k in kwargs):
        kwargs = {**kwargs, "scanner_feed_retention_enabled": False}
    settings_cls = import_module("project_mai_tai.settings").Settings
    return settings_cls(**kwargs)


def fixed_now() -> datetime:
    return datetime(2026, 3, 28, 10, 0)


class FakeRedis:
    def __init__(self) -> None:
        self.entries: list[tuple[str, str]] = []
        self.stream_entries: dict[str, list[tuple[str, dict[str, str]]]] = {}

    async def xadd(self, stream: str, fields: dict[str, str], **kwargs) -> str:
        del kwargs
        self.entries.append((stream, fields["data"]))
        self.stream_entries.setdefault(stream, []).insert(0, ("1-0", dict(fields)))
        return "1-0"

    async def xrevrange(self, stream: str, count: int | None = None, **kwargs):
        del kwargs
        entries = list(self.stream_entries.get(stream, []))
        if count is not None:
            entries = entries[:count]
        return entries

    async def xread(self, streams: dict[str, str], block: int | None = None, count: int | None = None):
        del block

        def _message_id_key(value: str) -> tuple[int, int]:
            major, _, minor = str(value).partition("-")
            try:
                return int(major), int(minor or 0)
            except ValueError:
                return 0, 0

        results: list[tuple[str, list[tuple[str, dict[str, str]]]]] = []
        remaining = None if count is None else max(0, int(count))
        for stream, offset in streams.items():
            entries = list(reversed(self.stream_entries.get(stream, [])))
            unread = [
                (message_id, dict(fields))
                for message_id, fields in entries
                if _message_id_key(message_id) > _message_id_key(offset)
            ]
            if remaining is not None:
                unread = unread[:remaining]
                remaining -= len(unread)
            if unread:
                results.append((stream, unread))
            if remaining == 0:
                break
        return results

    async def aclose(self) -> None:
        return None


def build_test_session_factory() -> sessionmaker[Session]:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        future=True,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


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


@pytest.mark.asyncio
async def test_initialize_stream_offsets_anchors_to_latest_ids() -> None:
    settings = make_test_settings()
    redis = FakeRedis()
    redis.stream_entries = {
        "test:market-data": [("11-0", {"data": "m"})],
        "test:order-events": [("22-0", {"data": "o"})],
        "test:snapshot-batches": [("33-0", {"data": "s"})],
        "test:runtime-controls": [],
    }
    service = StrategyEngineService(
        settings=settings.model_copy(update={"redis_stream_prefix": "test"}),
        redis_client=redis,
    )

    await service._initialize_stream_offsets()

    assert service._stream_offsets == {
        "test:market-data": "11-0",
        "test:order-events": "22-0",
        "test:snapshot-batches": "33-0",
        "test:runtime-controls": "0-0",
    }


@pytest.mark.asyncio
async def test_read_stream_group_reads_priority_stream_without_waiting_on_market_data() -> None:
    settings = make_test_settings(redis_stream_prefix="test")
    redis = FakeRedis()
    redis.stream_entries = {
        "test:market-data": [
            ("11-0", {"data": json.dumps({"event_type": "trade_tick", "payload": {}})}),
            ("10-0", {"data": json.dumps({"event_type": "trade_tick", "payload": {}})}),
        ],
        "test:snapshot-batches": [
            ("21-0", {"data": json.dumps({"event_type": "snapshot_batch", "payload": {}})})
        ],
        "test:order-events": [],
        "test:runtime-controls": [],
    }
    service = StrategyEngineService(settings=settings, redis_client=redis)
    service._stream_offsets = {
        "test:market-data": "10-0",
        "test:snapshot-batches": "20-0",
        "test:order-events": "0-0",
        "test:runtime-controls": "0-0",
    }
    seen: list[tuple[str, dict[str, str]]] = []

    async def fake_handle_stream_message(stream: str, fields: dict[str, str]) -> None:
        seen.append((stream, dict(fields)))

    service._handle_stream_message = fake_handle_stream_message  # type: ignore[method-assign]

    handled = await service._read_stream_group(["test:snapshot-batches"], block_ms=1)

    assert handled is True
    assert seen == [("test:snapshot-batches", {"data": json.dumps({"event_type": "snapshot_batch", "payload": {}})})]
    assert service._stream_offsets["test:snapshot-batches"] == "21-0"


def test_order_routing_metadata_uses_extended_hours_limit_in_premarket() -> None:
    metadata = order_routing_metadata(
        price="2.55",
        side="buy",
        now=datetime(2026, 3, 31, 7, 0, tzinfo=UTC),
    )

    assert metadata == {
        "session": "AM",
        "order_type": "limit",
        "time_in_force": "day",
        "extended_hours": "true",
        "limit_price": "2.55",
        "reference_price": "2.55",
        "price_source": "ask",
    }


def test_order_routing_metadata_uses_market_in_regular_session() -> None:
    metadata = order_routing_metadata(
        price="2.55",
        side="buy",
        now=datetime(2026, 3, 31, 14, 0, tzinfo=UTC),
    )

    assert metadata == {}


def test_macd_runtime_uses_quote_anchored_limit_prices_in_extended_hours(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "project_mai_tai.services.strategy_engine_app.utcnow",
        lambda: datetime(2026, 3, 31, 11, 0, tzinfo=UTC),
    )
    runtime = StrategyBotRuntime(
        StrategyDefinition(
            code="macd_30s",
            display_name="30s",
            account_name="paper:test",
            interval_secs=30,
            trading_config=TradingConfig(),
            indicator_config=IndicatorConfig(),
        ),
        now_provider=lambda: datetime(2026, 3, 31, 11, 0, tzinfo=UTC),
    )
    runtime.update_market_snapshots(
        [
            snapshot_from_payload(
                MarketSnapshotPayload(
                    symbol="KIDZ",
                    last_trade_price=Decimal("3.10"),
                    bid_price=Decimal("3.11"),
                    ask_price=Decimal("3.12"),
                )
            )
        ]
    )
    runtime.positions.open_position("KIDZ", 3.10, quantity=100, path="P1")

    open_intent = runtime._emit_open_intent(
        {"ticker": "KIDZ", "price": 3.10, "path": "P1_MACD_CROSS", "score": 5, "score_details": "x"}
    )
    close_intent = runtime._emit_close_intent({"ticker": "KIDZ", "price": 3.10, "reason": "TEST"})

    assert open_intent.payload.metadata["limit_price"] == "3.12"
    assert open_intent.payload.metadata["price_source"] == "ask"
    assert open_intent.payload.metadata["session"] == "AM"
    assert close_intent.payload.metadata["limit_price"] == "3.11"
    assert close_intent.payload.metadata["price_source"] == "bid"
    assert close_intent.payload.metadata["session"] == "AM"


def test_macd_runtime_blocks_extended_hours_entry_without_live_ask(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "project_mai_tai.services.strategy_engine_app.utcnow",
        lambda: datetime(2026, 3, 31, 11, 0, tzinfo=UTC),
    )
    runtime = StrategyBotRuntime(
        StrategyDefinition(
            code="macd_30s",
            display_name="30s",
            account_name="paper:test",
            interval_secs=30,
            trading_config=TradingConfig(),
            indicator_config=IndicatorConfig(),
        ),
        now_provider=lambda: datetime(2026, 3, 31, 11, 0, tzinfo=UTC),
    )
    runtime.update_market_snapshots(
        [
            snapshot_from_payload(
                MarketSnapshotPayload(
                    symbol="KIDZ",
                    last_trade_price=Decimal("3.10"),
                    bid_price=Decimal("3.11"),
                )
            )
        ]
    )

    with pytest.raises(RuntimeError, match="missing ask quote for extended-hours entry"):
        runtime._emit_open_intent(
            {"ticker": "KIDZ", "price": 3.10, "path": "P1_MACD_CROSS", "score": 5, "score_details": "x"}
        )


def test_macd_runtime_blocks_p4_entry_when_live_price_breaks_down_after_signal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "project_mai_tai.services.strategy_engine_app.utcnow",
        lambda: datetime(2026, 5, 4, 17, 56, tzinfo=UTC),
    )
    runtime = StrategyBotRuntime(
        StrategyDefinition(
            code="macd_30s",
            display_name="30s",
            account_name="paper:test",
            interval_secs=30,
            trading_config=TradingConfig(),
            indicator_config=IndicatorConfig(),
        ),
        now_provider=lambda: datetime(2026, 5, 4, 17, 56, tzinfo=UTC),
    )
    runtime.update_market_snapshots(
        [
            snapshot_from_payload(
                MarketSnapshotPayload(
                    symbol="DARE",
                    last_trade_price=Decimal("3.11"),
                    bid_price=Decimal("3.11"),
                    ask_price=Decimal("3.12"),
                )
            )
        ]
    )

    with pytest.raises(RuntimeError, match="P4 follow-through veto"):
        runtime._emit_open_intent(
            {"ticker": "DARE", "price": 3.18, "path": "P4_BURST", "score": 6, "score_details": "x"}
        )


def test_macd_runtime_allows_p4_entry_when_live_price_holds_within_breakdown_tolerance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "project_mai_tai.services.strategy_engine_app.utcnow",
        lambda: datetime(2026, 5, 4, 17, 56, tzinfo=UTC),
    )
    runtime = StrategyBotRuntime(
        StrategyDefinition(
            code="macd_30s",
            display_name="30s",
            account_name="paper:test",
            interval_secs=30,
            trading_config=TradingConfig(),
            indicator_config=IndicatorConfig(),
        ),
        now_provider=lambda: datetime(2026, 5, 4, 17, 56, tzinfo=UTC),
    )
    runtime.update_market_snapshots(
        [
            snapshot_from_payload(
                MarketSnapshotPayload(
                    symbol="DARE",
                    last_trade_price=Decimal("3.13"),
                    bid_price=Decimal("3.13"),
                    ask_price=Decimal("3.14"),
                )
            )
        ]
    )

    open_intent = runtime._emit_open_intent(
        {"ticker": "DARE", "price": 3.18, "path": "P4_BURST", "score": 6, "score_details": "x"}
    )

    assert open_intent.payload.symbol == "DARE"
    assert open_intent.payload.reason == "ENTRY_P4_BURST"
    assert open_intent.payload.metadata["reference_price"] == "3.18"


def test_macd_runtime_does_not_apply_p4_breakdown_veto_to_other_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "project_mai_tai.services.strategy_engine_app.utcnow",
        lambda: datetime(2026, 5, 4, 17, 56, tzinfo=UTC),
    )
    runtime = StrategyBotRuntime(
        StrategyDefinition(
            code="macd_30s",
            display_name="30s",
            account_name="paper:test",
            interval_secs=30,
            trading_config=TradingConfig(),
            indicator_config=IndicatorConfig(),
        ),
        now_provider=lambda: datetime(2026, 5, 4, 17, 56, tzinfo=UTC),
    )
    runtime.update_market_snapshots(
        [
            snapshot_from_payload(
                MarketSnapshotPayload(
                    symbol="KIDZ",
                    last_trade_price=Decimal("3.11"),
                    bid_price=Decimal("3.11"),
                    ask_price=Decimal("3.12"),
                )
            )
        ]
    )

    open_intent = runtime._emit_open_intent(
        {"ticker": "KIDZ", "price": 3.18, "path": "P1_MACD_CROSS", "score": 5, "score_details": "x"}
    )

    assert open_intent.payload.symbol == "KIDZ"
    assert open_intent.payload.reason == "ENTRY_P1_MACD_CROSS"
    assert open_intent.payload.metadata["reference_price"] == "3.18"


def test_macd_runtime_quote_tick_triggers_panic_priced_hard_stop_in_extended_hours() -> None:
    runtime = StrategyBotRuntime(
        StrategyDefinition(
            code="macd_30s",
            display_name="30s",
            account_name="paper:test",
            interval_secs=30,
            trading_config=TradingConfig(),
            indicator_config=IndicatorConfig(),
        ),
        now_provider=lambda: datetime(2026, 3, 31, 11, 0, tzinfo=UTC),
    )
    runtime.positions.open_position("KIDZ", 4.00, quantity=10, path="P1")

    intents = runtime.handle_quote_tick(
        "KIDZ",
        bid_price=3.93,
        ask_price=3.95,
    )

    assert len(intents) == 1
    intent = intents[0]
    assert intent.payload.intent_type == "close"
    assert intent.payload.reason == "HARD_STOP"
    assert intent.payload.metadata["stop_guard"] == "true"
    assert intent.payload.metadata["stop_trigger_source"] == "bid"
    assert intent.payload.metadata["price_source"] == "bid"
    assert intent.payload.metadata["limit_price"] == "3.91"
    assert intent.payload.metadata["panic_buffer_pct"] == "0.5"
    assert "KIDZ" in runtime.pending_close_symbols


def test_macd_runtime_quote_tick_triggers_limit_hard_stop_in_regular_hours() -> None:
    runtime = StrategyBotRuntime(
        StrategyDefinition(
            code="macd_30s",
            display_name="30s",
            account_name="paper:test",
            interval_secs=30,
            trading_config=TradingConfig(),
            indicator_config=IndicatorConfig(),
        ),
        now_provider=lambda: datetime(2026, 3, 31, 14, 0, tzinfo=UTC),
    )
    runtime.positions.open_position("KIDZ", 4.00, quantity=10, path="P1")

    intents = runtime.handle_quote_tick(
        "KIDZ",
        bid_price=3.93,
        ask_price=3.95,
    )

    assert len(intents) == 1
    intent = intents[0]
    assert intent.payload.reason == "HARD_STOP"
    assert intent.payload.metadata["order_type"] == "limit"
    assert intent.payload.metadata["limit_price"] == "3.91"
    assert intent.payload.metadata["price_source"] == "bid"
    assert "extended_hours" not in intent.payload.metadata
    assert "session" not in intent.payload.metadata


def test_macd_runtime_hard_stop_uses_last_price_when_bid_quote_is_stale() -> None:
    current_time = datetime(2026, 3, 31, 11, 0, tzinfo=UTC)

    runtime = StrategyBotRuntime(
        StrategyDefinition(
            code="macd_30s",
            display_name="30s",
            account_name="paper:test",
            interval_secs=30,
            trading_config=TradingConfig(),
            indicator_config=IndicatorConfig(),
        ),
        now_provider=lambda: current_time,
    )
    runtime.positions.open_position("KIDZ", 4.00, quantity=10, path="P1")
    runtime.handle_quote_tick(
        "KIDZ",
        bid_price=3.96,
        ask_price=3.98,
    )
    current_time = current_time + timedelta(seconds=3)

    close_intent = runtime._emit_close_intent(
        {
            "ticker": "KIDZ",
            "price": 3.93,
            "reason": "HARD_STOP",
            "stop_guard": "true",
            "stop_trigger_source": "last",
            "stop_trigger_price": 3.93,
            "stop_price": 3.94,
            "panic_buffer_pct": 0.5,
        }
    )

    assert close_intent.payload.metadata["limit_price"] == "3.91"
    assert close_intent.payload.metadata["price_source"] == "last"


def test_macd_runtime_trade_tick_triggers_extended_hours_scale_without_fresh_quote() -> None:
    runtime = StrategyBotRuntime(
        StrategyDefinition(
            code="macd_30s",
            display_name="30s",
            account_name="paper:test",
            interval_secs=30,
            trading_config=TradingConfig(),
            indicator_config=IndicatorConfig(),
        ),
        now_provider=lambda: datetime(2026, 3, 31, 11, 0, tzinfo=UTC),
    )
    runtime.positions.open_position("KIDZ", 4.00, quantity=10, path="P5")

    intents = runtime.handle_trade_tick("KIDZ", 4.20, size=10)

    assert len(intents) == 1
    intent = intents[0]
    assert intent.payload.intent_type == "scale"
    assert intent.payload.reason == "SCALE_FAST4"
    assert intent.payload.metadata["limit_price"] == "4.20"
    assert intent.payload.metadata["price_source"] == "reference"


def test_macd_runtime_trade_tick_triggers_extended_hours_close_without_fresh_quote() -> None:
    runtime = StrategyBotRuntime(
        StrategyDefinition(
            code="macd_30s",
            display_name="30s",
            account_name="paper:test",
            interval_secs=30,
            trading_config=TradingConfig(),
            indicator_config=IndicatorConfig(),
        ),
        now_provider=lambda: datetime(2026, 3, 31, 11, 0, tzinfo=UTC),
    )
    runtime.positions.open_position("KIDZ", 4.00, quantity=10, path="P5")
    position = runtime.positions.get_position("KIDZ")
    assert position is not None
    position.floor_pct = 5.0
    position.floor_price = 4.18

    intents = runtime.handle_trade_tick("KIDZ", 4.15, size=10)

    assert len(intents) == 1
    intent = intents[0]
    assert intent.payload.intent_type == "close"
    assert intent.payload.reason == "FLOOR_BREACH"
    assert intent.payload.metadata["limit_price"] == "4.15"
    assert intent.payload.metadata["price_source"] == "reference"


def test_runtime_blocks_close_retries_after_duplicate_exit_reject() -> None:
    runtime = StrategyBotRuntime(
        StrategyDefinition(
            code="macd_30s",
            display_name="30s",
            account_name="paper:test",
            interval_secs=30,
            trading_config=TradingConfig(),
            indicator_config=IndicatorConfig(),
        ),
        now_provider=lambda: datetime(2026, 3, 31, 14, 0, tzinfo=UTC),
    )

    runtime.pending_close_symbols.add("ELAB")
    runtime.apply_order_status(
        symbol="ELAB",
        intent_type="close",
        status="rejected",
        reason="duplicate_exit_in_flight",
    )

    assert "ELAB" not in runtime.pending_close_symbols
    assert runtime._is_exit_retry_blocked("ELAB") is True


def test_exit_engine_tolerates_missing_stoch_exit_keys() -> None:
    engine = ExitEngine(TradingConfig())

    class Position:
        ticker = "UGRO"
        tier = 1
        current_price = 4.25
        current_profit_pct = 1.2

        def is_floor_breached(self) -> bool:
            return False

        def get_scale_action(self, config):
            del config
            return None

    signal = engine.check_exit(
        Position(),
        {
            "macd_cross_below": False,
        },
    )

    assert signal is None


def test_snapshot_batch_keeps_single_confirmed_name_in_watchlist(monkeypatch) -> None:
    state = StrategyEngineState(now_provider=fixed_now)
    state.confirmed_scanner._confirmed = [
        {
            "ticker": "UGRO",
            "confirmed_at": "10:00:00 AM ET",
            "entry_price": 2.25,
            "price": 2.4,
            "change_pct": 24.5,
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
        [snapshot_from_payload(make_snapshot_payload(symbol="UGRO", price=2.7, volume=900_000))],
        {"UGRO": ReferenceData(shares_outstanding=50_000, avg_daily_volume=390_000)},
    )

    assert summary["watchlist"] == ["UGRO"]


def test_snapshot_batch_hands_confirmed_symbols_to_bots_without_rank_threshold(monkeypatch) -> None:
    state = StrategyEngineState(now_provider=fixed_now)
    state.confirmed_scanner._confirmed = [
        {
            "ticker": "CMND",
            "confirmed_at": "10:00:00 AM ET",
            "entry_price": 1.21,
            "price": 1.22,
            "change_pct": 69.5,
            "volume": 75_000_000,
            "rvol": 70.0,
            "shares_outstanding": 158_076,
            "bid": 1.22,
            "ask": 1.23,
            "spread": 0.01,
            "spread_pct": 0.82,
            "hod": 1.24,
            "vwap": 1.223,
            "prev_close": 0.7196,
            "avg_daily_volume": 1_862_169,
            "first_spike_time": "09:55:00 AM ET",
            "first_spike_price": 1.1,
            "squeeze_count": 2,
            "data_age_secs": 0,
            "confirmation_path": "PATH_B_2SQ",
            "force_watchlist": False,
        },
        {
            "ticker": "ENVB",
            "confirmed_at": "10:00:00 AM ET",
            "entry_price": 3.84,
            "price": 3.73,
            "change_pct": 104.9,
            "volume": 22_900_000,
            "rvol": 66.5,
            "shares_outstanding": 1_887_535,
            "bid": 3.72,
            "ask": 3.73,
            "spread": 0.01,
            "spread_pct": 0.27,
            "hod": 3.99,
            "vwap": 3.90,
            "prev_close": 1.82,
            "avg_daily_volume": 595_857,
            "first_spike_time": "",
            "first_spike_price": 0.0,
            "squeeze_count": 1,
            "data_age_secs": 0,
            "confirmation_path": "PATH_C_EXTREME_MOVER",
            "force_watchlist": True,
        },
    ]
    monkeypatch.setattr(state.alert_engine, "check_alerts", lambda snapshots, reference_data: [])
    monkeypatch.setattr(
        state.confirmed_scanner,
        "process_alerts",
        lambda alerts, reference_data, snapshot_lookup: [],
    )

    summary = state.process_snapshot_batch(
        [
            snapshot_from_payload(make_snapshot_payload(symbol="CMND", price=1.22, volume=75_000_000)),
            snapshot_from_payload(make_snapshot_payload(symbol="ENVB", price=3.73, volume=22_900_000)),
        ],
        {
            "CMND": ReferenceData(shares_outstanding=158_076, avg_daily_volume=1_862_169),
            "ENVB": ReferenceData(shares_outstanding=1_887_535, avg_daily_volume=595_857),
        },
    )

    assert [item["ticker"] for item in summary["top_confirmed"]] == ["ENVB", "CMND"]
    assert summary["watchlist"] == ["CMND", "ENVB"]


def test_bot_watchlist_backfills_next_confirmed_symbol_after_manual_stop_filter() -> None:
    state = StrategyEngineState(now_provider=fixed_now)
    state.all_confirmed = [
        {"ticker": "AKAN"},
        {"ticker": "ELPW"},
        {"ticker": "AGPU"},
        {"ticker": "TORO"},
        {"ticker": "WBUY"},
        {"ticker": "GNLN"},
    ]
    state.current_confirmed = [
        {"ticker": "AKAN"},
        {"ticker": "ELPW"},
        {"ticker": "AGPU"},
        {"ticker": "TORO"},
        {"ticker": "WBUY"},
    ]

    state._seed_bot_handoff_state(state.all_confirmed, strategy_codes=["macd_30s"])
    state.apply_manual_stop_symbols({"macd_30s": {"ELPW", "TORO", "WBUY"}})
    state._resync_bot_watchlists_from_current_confirmed(strategy_codes=["macd_30s"])

    assert state.bots["macd_30s"].watchlist == {"AGPU", "AKAN", "GNLN"}


def test_bot_watchlist_includes_all_confirmed_symbols_without_top5_cap() -> None:
    state = StrategyEngineState(now_provider=fixed_now)
    state.all_confirmed = [
        {"ticker": "AKAN"},
        {"ticker": "AGPU"},
        {"ticker": "GNLN"},
        {"ticker": "MASK"},
        {"ticker": "RENX"},
        {"ticker": "SBET"},
    ]
    state.current_confirmed = [
        {"ticker": "AKAN"},
        {"ticker": "AGPU"},
        {"ticker": "GNLN"},
        {"ticker": "MASK"},
        {"ticker": "RENX"},
    ]

    state._seed_bot_handoff_state(state.all_confirmed, strategy_codes=["macd_30s"])
    state._resync_bot_watchlists_from_current_confirmed(strategy_codes=["macd_30s"])

    assert state.bots["macd_30s"].watchlist == {
        "AGPU",
        "AKAN",
        "GNLN",
        "MASK",
        "RENX",
        "SBET",
    }
    assert state.retained_watchlist == [
        "AGPU",
        "AKAN",
        "GNLN",
        "MASK",
        "RENX",
        "SBET",
    ]


def test_scanner_session_roll_clears_state_without_snapshot_batch() -> None:
    now_box = {"value": datetime(2026, 4, 23, 3, 59, tzinfo=EASTERN_TZ)}
    state = StrategyEngineState(
        settings=make_test_settings(strategy_macd_30s_enabled=True),
        now_provider=lambda: now_box["value"],
    )
    state.confirmed_scanner.seed_confirmed_candidates([{"ticker": "GNLN"}])
    state.all_confirmed = [{"ticker": "GNLN"}]
    state.current_confirmed = [{"ticker": "GNLN"}]
    state.retained_watchlist = ["GNLN"]
    state.recent_alerts = [{"ticker": "GNLN"}]
    state._add_market_data_archive_symbols(["GNLN"])
    state.alert_engine.record_snapshot(
        [snapshot_from_payload(make_snapshot_payload(symbol="GNLN", price=5.0, volume=500_000))]
    )
    state.alert_engine._volume_spike_tickers.add("GNLN")
    state.top_gainers_tracker.update(
        [snapshot_from_payload(make_snapshot_payload(symbol="GNLN", price=5.0, volume=500_000))],
        {"GNLN": ReferenceData(avg_daily_volume=50_000)},
        now=now_box["value"],
    )
    state.feed_retention_states["GNLN"] = state.feed_retention_policy.promote("GNLN", now_box["value"], None)
    state.bots["macd_30s"].set_watchlist(["GNLN"])
    state.bots["macd_30s"].recent_decisions = [{"symbol": "GNLN", "status": "blocked"}]

    now_box["value"] = datetime(2026, 4, 23, 4, 1, tzinfo=EASTERN_TZ)

    assert state._roll_scanner_session_if_needed() is True
    assert state.confirmed_scanner.get_all_confirmed() == []
    assert state.all_confirmed == []
    assert state.current_confirmed == []
    assert state.retained_watchlist == []
    assert state.market_data_archive_symbols == []
    assert state.recent_alerts == []
    assert state.alert_engine.get_warmup_status()["history_cycles"] == 0
    assert state.alert_engine._volume_spike_tickers == set()
    assert state.top_gainers_tracker.update([], {}, now=now_box["value"])[0] == []
    assert state.feed_retention_states == {}
    assert state.bots["macd_30s"].watchlist == set()
    assert state.bots["macd_30s"].recent_decisions == []


def test_retention_cooldown_keeps_feed_alive_but_blocks_entries(monkeypatch: pytest.MonkeyPatch) -> None:
    now_box = {"value": datetime(2026, 4, 17, 10, 0)}
    state = StrategyEngineState(
        settings=make_test_settings(
            scanner_feed_retention_structure_bars=3,
            scanner_feed_retention_no_activity_minutes=5,
            scanner_feed_retention_cooldown_volume_ratio=0.5,
            scanner_feed_retention_cooldown_max_5m_range_pct=1.5,
            scanner_feed_retention_resume_hold_bars=2,
            scanner_feed_retention_resume_min_5m_range_pct=2.5,
            scanner_feed_retention_resume_min_5m_volume_abs=100_000,
            scanner_feed_retention_drop_cooldown_minutes=10,
            scanner_feed_retention_drop_max_5m_volume_abs=50_000,
        ),
        now_provider=lambda: now_box["value"],
    )
    monkeypatch.setattr(state.alert_engine, "check_alerts", lambda snapshots, reference_data: [])
    monkeypatch.setattr(
        state.confirmed_scanner,
        "process_alerts",
        lambda alerts, reference_data, snapshot_lookup: [],
    )

    state.confirmed_scanner._confirmed = [
        {
            "ticker": "UGRO",
            "confirmed_at": "10:00:00 AM ET",
            "entry_price": 2.25,
            "price": 2.40,
            "change_pct": 24.5,
            "volume": 900_000,
            "rvol": 6.2,
            "shares_outstanding": 50_000,
            "confirmation_path": "PATH_B_2SQ",
        }
    ]
    summary = state.process_snapshot_batch(
        [snapshot_from_payload(make_snapshot_payload(symbol="UGRO", price=2.40, volume=900_000))],
        {"UGRO": ReferenceData(shares_outstanding=50_000, avg_daily_volume=390_000)},
    )

    assert summary["watchlist"] == ["UGRO"]
    assert state.bots["macd_30s"].watchlist == {"UGRO"}
    state.bots["macd_30s"].lifecycle_states["UGRO"].active_reference_5m_volume = 200_000

    state.confirmed_scanner._confirmed = []
    builder = state.bots["macd_30s"].builder_manager.get_or_create("UGRO")
    state.bots["macd_30s"].last_indicators["UGRO"] = {
        "price": 6.22,
        "selected_vwap": 6.60,
        "vwap": 6.60,
        "ema20": 6.40,
    }

    for idx, minutes_ahead in enumerate((6, 7, 8), start=1):
        builder.bars = [
            OHLCVBar(
                open=6.25,
                high=6.28,
                low=6.20,
                close=6.22,
                volume=2_000,
                timestamp=1_700_000_000.0 + bar_idx * 30 + idx,
            )
            for bar_idx in range(10)
        ]
        now_box["value"] = datetime(2026, 4, 17, 10, minutes_ahead)
        summary = state.process_snapshot_batch(
            [snapshot_from_payload(make_snapshot_payload(symbol="UGRO", price=6.22, volume=20_000))],
            {"UGRO": ReferenceData(shares_outstanding=50_000, avg_daily_volume=390_000)},
        )

    assert summary["watchlist"] == ["UGRO"]
    assert summary["top_confirmed"] == []
    assert summary["retention_states"][0]["state"] == "cooldown"
    assert state.bots["macd_30s"].watchlist == {"UGRO"}
    assert state.bots["macd_30s"].entry_blocked_symbols == {"UGRO"}


def test_retention_resume_unblocks_entries_after_reclaim(monkeypatch: pytest.MonkeyPatch) -> None:
    now_box = {"value": datetime(2026, 4, 17, 10, 0)}
    state = StrategyEngineState(
        settings=make_test_settings(
            strategy_macd_1m_enabled=True,
            strategy_tos_enabled=True,
            scanner_feed_retention_structure_bars=3,
            scanner_feed_retention_no_activity_minutes=5,
            scanner_feed_retention_cooldown_volume_ratio=0.5,
            scanner_feed_retention_cooldown_max_5m_range_pct=1.5,
            scanner_feed_retention_resume_hold_bars=2,
            scanner_feed_retention_resume_min_5m_range_pct=2.5,
            scanner_feed_retention_resume_min_5m_volume_ratio=1.2,
            scanner_feed_retention_resume_min_5m_volume_abs=100_000,
            scanner_feed_retention_drop_cooldown_minutes=10,
            scanner_feed_retention_drop_max_5m_volume_abs=50_000,
        ),
        now_provider=lambda: now_box["value"],
    )
    monkeypatch.setattr(state.alert_engine, "check_alerts", lambda snapshots, reference_data: [])
    monkeypatch.setattr(
        state.confirmed_scanner,
        "process_alerts",
        lambda alerts, reference_data, snapshot_lookup: [],
    )

    state.restore_confirmed_runtime_view([{"ticker": "UGRO"}])
    retained = state.bots["macd_30s"].lifecycle_states["UGRO"]
    retained.state = "cooldown"
    retained.cooldown_started_at = now_box["value"]
    retained.state_changed_at = now_box["value"]
    retained.active_reference_5m_volume = 200_000

    builder = state.bots["macd_30s"].builder_manager.get_or_create("UGRO")
    builder.bars = [
        OHLCVBar(
            open=6.80,
            high=7.05,
            low=6.78,
            close=6.98,
            volume=40_000,
            timestamp=1_700_000_000.0 + idx * 30 + 1,
        )
        for idx in range(10)
    ]
    state.bots["macd_30s"].last_indicators["UGRO"] = {
        "price": 6.98,
        "selected_vwap": 6.82,
        "vwap": 6.82,
        "ema20": 6.79,
    }

    state.confirmed_scanner._confirmed = []
    now_box["value"] = datetime(2026, 4, 17, 10, 12)
    summary = state.process_snapshot_batch(
        [snapshot_from_payload(make_snapshot_payload(symbol="UGRO", price=6.98, volume=220_000))],
        {"UGRO": ReferenceData(shares_outstanding=50_000, avg_daily_volume=390_000)},
    )

    assert state.bots["macd_30s"].lifecycle_states["UGRO"].state == "resume_probe"
    assert state.bots["macd_30s"].entry_blocked_symbols == {"UGRO"}

    now_box["value"] = datetime(2026, 4, 17, 10, 12, 30)
    builder.bars = [
        OHLCVBar(
            open=6.84,
            high=7.09,
            low=6.82,
            close=7.02,
            volume=42_000,
            timestamp=1_700_000_000.0 + idx * 30 + 2,
        )
        for idx in range(10)
    ]
    state.bots["macd_30s"].last_indicators["UGRO"] = {
        "price": 7.02,
        "selected_vwap": 6.83,
        "vwap": 6.83,
        "ema20": 6.80,
    }
    summary = state.process_snapshot_batch(
        [snapshot_from_payload(make_snapshot_payload(symbol="UGRO", price=7.02, volume=230_000))],
        {"UGRO": ReferenceData(shares_outstanding=50_000, avg_daily_volume=390_000)},
    )

    assert state.bots["macd_30s"].lifecycle_states["UGRO"].state == "active"
    assert state.bots["macd_30s"].entry_blocked_symbols == set()
    assert summary["watchlist"] == ["UGRO"]
    for code in ("macd_30s", "macd_1m", "tos"):
        assert state.bots[code].watchlist == {"UGRO"}


def test_retention_drops_symbol_without_indicators_after_inactivity(monkeypatch: pytest.MonkeyPatch) -> None:
    now_box = {"value": datetime(2026, 4, 17, 10, 0)}
    state = StrategyEngineState(
        settings=make_test_settings(
            scanner_feed_retention_enabled=True,
            scanner_feed_retention_no_activity_minutes=5,
            scanner_feed_retention_drop_cooldown_minutes=10,
        ),
        now_provider=lambda: now_box["value"],
    )
    monkeypatch.setattr(state.alert_engine, "check_alerts", lambda snapshots, reference_data: [])
    monkeypatch.setattr(
        state.confirmed_scanner,
        "process_alerts",
        lambda alerts, reference_data, snapshot_lookup: [],
    )

    state.confirmed_scanner._confirmed = [
        {
            "ticker": "UGRO",
            "confirmed_at": "10:00:00 AM ET",
            "entry_price": 2.25,
            "price": 2.40,
            "change_pct": 24.5,
            "volume": 900_000,
            "rvol": 6.2,
            "shares_outstanding": 50_000,
            "confirmation_path": "PATH_B_2SQ",
        }
    ]
    summary = state.process_snapshot_batch(
        [snapshot_from_payload(make_snapshot_payload(symbol="UGRO", price=2.40, volume=900_000))],
        {"UGRO": ReferenceData(shares_outstanding=50_000, avg_daily_volume=390_000)},
    )

    assert summary["watchlist"] == ["UGRO"]
    assert state.bots["macd_30s"].watchlist == {"UGRO"}
    assert "UGRO" not in state.bots["macd_30s"].last_indicators

    state.confirmed_scanner._confirmed = []
    now_box["value"] = datetime(2026, 4, 17, 10, 6)
    summary = state.process_snapshot_batch(
        [snapshot_from_payload(make_snapshot_payload(symbol="SNOA", price=3.10, volume=120_000))],
        {"SNOA": ReferenceData(shares_outstanding=90_000, avg_daily_volume=80_000)},
    )
    assert summary["watchlist"] == ["UGRO"]
    assert state.bots["macd_30s"].lifecycle_states["UGRO"].state == "cooldown"

    now_box["value"] = datetime(2026, 4, 17, 10, 16)
    summary = state.process_snapshot_batch(
        [snapshot_from_payload(make_snapshot_payload(symbol="SNOA", price=3.05, volume=110_000))],
        {"SNOA": ReferenceData(shares_outstanding=90_000, avg_daily_volume=80_000)},
    )

    assert summary["watchlist"] == []
    assert state.bots["macd_30s"].watchlist == set()
    assert state.bots["macd_30s"].lifecycle_states["UGRO"].state == "dropped"


def test_cooldown_blocks_non_p4_signal_below_structure(monkeypatch: pytest.MonkeyPatch) -> None:
    state = StrategyEngineState(now_provider=fixed_now)
    state.restore_confirmed_runtime_view([{"ticker": "UGRO"}])
    bot = state.bots["macd_30s"]
    retained = bot.lifecycle_states["UGRO"]
    retained.state = "cooldown"
    retained.cooldown_started_at = fixed_now()
    retained.state_changed_at = fixed_now()
    bot._sync_watchlist_from_lifecycle()
    state.seed_bars("macd_30s", "UGRO", seed_trending_bars())

    monkeypatch.setattr(
        bot.indicator_engine,
        "calculate",
        lambda bars: {
            "price": 6.22,
            "selected_vwap": 6.60,
            "vwap": 6.60,
            "ema20": 6.40,
            "bar_timestamp": float(bars[-1]["timestamp"]),
        },
    )
    monkeypatch.setattr(
        bot.entry_engine,
        "check_entry",
        lambda symbol, indicators, bar_index, runtime: {
            "action": "BUY",
            "ticker": symbol,
            "path": "P2_VWAP",
            "price": indicators["price"],
            "score": 5,
            "score_details": "test",
        },
    )
    monkeypatch.setattr(
        bot.entry_engine,
        "pop_last_decision",
        lambda _symbol: {"status": "signal", "reason": "P2_VWAP", "path": "P2_VWAP", "score": "5"},
    )

    intents = bot._evaluate_completed_bar("UGRO")

    assert intents == []
    assert bot.lifecycle_states["UGRO"].state == "cooldown"
    assert "UGRO" in bot.entry_blocked_symbols
    assert bot.recent_decisions[0]["reason"] == "bot lifecycle cooldown active: waiting for P4 or VWAP/EMA20 reclaim"


def test_cooldown_allows_p4_override_below_structure(monkeypatch: pytest.MonkeyPatch) -> None:
    state = StrategyEngineState(now_provider=fixed_now)
    state.restore_confirmed_runtime_view([{"ticker": "UGRO"}])
    bot = state.bots["macd_30s"]
    retained = bot.lifecycle_states["UGRO"]
    retained.state = "cooldown"
    retained.cooldown_started_at = fixed_now()
    retained.state_changed_at = fixed_now()
    bot._sync_watchlist_from_lifecycle()
    state.seed_bars("macd_30s", "UGRO", seed_trending_bars())
    bot.latest_quotes["UGRO"] = {"ask": 6.22}

    monkeypatch.setattr(
        bot.indicator_engine,
        "calculate",
        lambda bars: {
            "price": 6.22,
            "selected_vwap": 6.60,
            "vwap": 6.60,
            "ema20": 6.40,
            "bar_timestamp": float(bars[-1]["timestamp"]),
        },
    )
    monkeypatch.setattr(
        bot.entry_engine,
        "check_entry",
        lambda symbol, indicators, bar_index, runtime: {
            "action": "BUY",
            "ticker": symbol,
            "path": "P4_BURST",
            "price": indicators["price"],
            "score": 5,
            "score_details": "test",
        },
    )
    monkeypatch.setattr(
        bot.entry_engine,
        "pop_last_decision",
        lambda _symbol: {"status": "signal", "reason": "P4_BURST", "path": "P4_BURST", "score": "5"},
    )

    intents = bot._evaluate_completed_bar("UGRO")

    assert len(intents) == 1
    assert intents[0].payload.reason == "ENTRY_P4_BURST"
    assert bot.lifecycle_states["UGRO"].state == "active"
    assert "UGRO" not in bot.entry_blocked_symbols


def test_cooldown_allows_non_p4_after_structure_reclaim(monkeypatch: pytest.MonkeyPatch) -> None:
    state = StrategyEngineState(now_provider=fixed_now)
    state.restore_confirmed_runtime_view([{"ticker": "UGRO"}])
    bot = state.bots["macd_30s"]
    retained = bot.lifecycle_states["UGRO"]
    retained.state = "cooldown"
    retained.cooldown_started_at = fixed_now()
    retained.state_changed_at = fixed_now()
    bot._sync_watchlist_from_lifecycle()
    state.seed_bars("macd_30s", "UGRO", seed_trending_bars())
    bot.latest_quotes["UGRO"] = {"ask": 6.98}

    monkeypatch.setattr(
        bot.indicator_engine,
        "calculate",
        lambda bars: {
            "price": 6.98,
            "selected_vwap": 6.82,
            "vwap": 6.82,
            "ema20": 6.79,
            "bar_timestamp": float(bars[-1]["timestamp"]),
        },
    )
    monkeypatch.setattr(
        bot.entry_engine,
        "check_entry",
        lambda symbol, indicators, bar_index, runtime: {
            "action": "BUY",
            "ticker": symbol,
            "path": "P2_VWAP",
            "price": indicators["price"],
            "score": 5,
            "score_details": "test",
        },
    )
    monkeypatch.setattr(
        bot.entry_engine,
        "pop_last_decision",
        lambda _symbol: {"status": "signal", "reason": "P2_VWAP", "path": "P2_VWAP", "score": "5"},
    )

    intents = bot._evaluate_completed_bar("UGRO")

    assert len(intents) == 1
    assert intents[0].payload.reason == "ENTRY_P2_VWAP"
    assert bot.lifecycle_states["UGRO"].state == "active"
    assert "UGRO" not in bot.entry_blocked_symbols


def test_snapshot_batch_applies_reclaim_specific_excluded_symbols(monkeypatch) -> None:
    state = StrategyEngineState(
        settings=make_test_settings(
            strategy_macd_30s_reclaim_enabled=True,
            strategy_macd_30s_reclaim_excluded_symbols="UGRO",
        ),
        now_provider=fixed_now,
    )
    state.confirmed_scanner._confirmed = [
        {
            "ticker": "UGRO",
            "confirmed_at": "10:00:00 AM ET",
            "entry_price": 2.25,
            "price": 2.4,
            "change_pct": 24.5,
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
        [snapshot_from_payload(make_snapshot_payload(symbol="UGRO", price=2.7, volume=900_000))],
        {"UGRO": ReferenceData(shares_outstanding=50_000, avg_daily_volume=390_000)},
    )

    assert summary["watchlist"] == ["UGRO"]
    assert state.bots["macd_30s"].watchlist == {"UGRO"}
    assert state.bots["macd_30s_reclaim"].watchlist == set()


def test_snapshot_batch_preserves_low_score_confirmed_without_feeding_bots(monkeypatch) -> None:
    state = StrategyEngineState(
        settings=make_test_settings(
            strategy_macd_1m_enabled=True,
            strategy_tos_enabled=True,
            strategy_runner_enabled=True,
        ),
        now_provider=fixed_now,
    )
    state.confirmed_scanner._confirmed = [
        {
            "ticker": "UGRO",
            "confirmed_at": "10:00:00 AM ET",
            "entry_price": 2.25,
            "price": 2.4,
            "change_pct": 24.5,
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
            "squeeze_count": 2,
            "data_age_secs": 0,
            "confirmation_path": "PATH_B_2SQ",
        },
        {
            "ticker": "SBET",
            "confirmed_at": "10:01:00 AM ET",
            "entry_price": 3.10,
            "price": 3.02,
            "change_pct": 4.5,
            "volume": 250_000,
            "rvol": 1.2,
            "shares_outstanding": 1_500_000,
            "bid": 3.01,
            "ask": 3.03,
            "spread": 0.02,
            "spread_pct": 0.66,
            "hod": 3.15,
            "vwap": 3.08,
            "prev_close": 2.89,
            "avg_daily_volume": 500_000,
            "first_spike_time": "09:56:00 AM ET",
            "squeeze_count": 2,
            "data_age_secs": 0,
            "confirmation_path": "PATH_B_2SQ",
        },
    ]
    monkeypatch.setattr(state.alert_engine, "check_alerts", lambda snapshots, reference_data: [])
    monkeypatch.setattr(
        state.confirmed_scanner,
        "process_alerts",
        lambda alerts, reference_data, snapshot_lookup: [],
    )

    summary = state.process_snapshot_batch(
        [
            snapshot_from_payload(make_snapshot_payload(symbol="UGRO", price=2.7, volume=900_000)),
            snapshot_from_payload(make_snapshot_payload(symbol="SBET", price=3.02, volume=250_000)),
        ],
        {
            "UGRO": ReferenceData(shares_outstanding=50_000, avg_daily_volume=390_000),
            "SBET": ReferenceData(shares_outstanding=1_500_000, avg_daily_volume=500_000),
        },
    )

    assert [item["ticker"] for item in summary["all_confirmed"]] == ["UGRO", "SBET"]
    assert [item["ticker"] for item in summary["top_confirmed"]] == ["UGRO", "SBET"]
    assert summary["watchlist"] == ["SBET", "UGRO"]
    assert state.confirmed_scanner.get_all_confirmed()[0]["rank_score"] == 100.0
    assert state.confirmed_scanner.get_all_confirmed()[1]["rank_score"] == 0.0
    for code in ("macd_30s", "macd_1m", "tos", "runner"):
        assert state.bots[code].watchlist == {"UGRO", "SBET"}


def test_alert_engine_state_persists_and_restores_from_dashboard_snapshot() -> None:
    session_factory = build_test_session_factory()
    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(
            "project_mai_tai.services.strategy_engine_app.utcnow",
            lambda: datetime(2026, 4, 1, 10, 5, tzinfo=UTC),
        )
        monkeypatch.setattr(
            "project_mai_tai.services.strategy_engine_app.current_scanner_session_start_utc",
            lambda now=None: datetime(2026, 4, 1, 8, 0, tzinfo=UTC),
        )

        service = StrategyEngineService(
            settings=make_test_settings(),
            redis_client=FakeRedis(),
            session_factory=session_factory,
        )

        snapshots = [
            snapshot_from_payload(make_snapshot_payload(symbol="MASK", price=2.5, volume=200_000)),
            snapshot_from_payload(make_snapshot_payload(symbol="ELAB", price=3.1, volume=300_000)),
        ]
        service.state.alert_engine.record_snapshot(snapshots)
        service.state.alert_engine._volume_spike_tickers.add("MASK")
        service.state.alert_engine._last_spike_volume["MASK"] = 200_000
        service.state.recent_alerts = [
            {"ticker": "mask", "type": "VOLUME_SPIKE", "time": "06:01:00 AM ET"},
            {"ticker": "elab", "type": "SQUEEZE_5MIN", "time": "06:02:00 AM ET"},
        ]
        service.state.top_gainer_changes = [
            {"ticker": "mask", "type": "NEW", "time": "06:01:00 AM ET"},
        ]
        service.state._first_seen_by_ticker["mask"] = "06:00:30 AM ET"

        service._persist_scanner_snapshots(
            {
                "top_confirmed": [],
                "watchlist": [],
                "cycle_count": 1,
            }
        )
        with session_factory() as session:
            snapshot = session.scalar(
                select(DashboardSnapshot).where(
                    DashboardSnapshot.snapshot_type == "scanner_alert_engine_state"
                )
            )
            assert snapshot is not None
            assert snapshot.payload["scanner_session_start_utc"] == "2026-04-01T08:00:00+00:00"

        restored = StrategyEngineService(
            settings=make_test_settings(),
            redis_client=FakeRedis(),
            session_factory=session_factory,
        )
        restored._restore_alert_engine_state_from_dashboard_snapshot()

        warmup = restored.state.alert_engine.get_warmup_status()
        assert warmup["history_cycles"] == 1
        assert "MASK" in restored.state.alert_engine._volume_spike_tickers
        assert restored.state.alert_engine._last_spike_volume["MASK"] == 200_000
        assert restored.state.recent_alerts == [
            {"ticker": "MASK", "type": "VOLUME_SPIKE", "time": "06:01:00 AM ET"},
            {"ticker": "ELAB", "type": "SQUEEZE_5MIN", "time": "06:02:00 AM ET"},
        ]
        assert restored.state._pending_recent_alert_replay is True
        assert restored.state.top_gainer_changes == [
            {"ticker": "MASK", "type": "NEW", "time": "06:01:00 AM ET"},
        ]
        assert restored.state._first_seen_by_ticker == {"MASK": "06:00:30 AM ET"}


def test_snapshot_batch_stream_default_covers_alert_warmup_window() -> None:
    settings = make_test_settings()
    state = StrategyEngineState(now_provider=fixed_now)

    required_cycles = int(state.alert_engine.get_warmup_status()["squeeze_10min_needs"])

    assert settings.redis_snapshot_batch_stream_maxlen >= required_cycles


def test_snapshot_batch_replays_restored_recent_alerts_into_confirmed_candidates() -> None:
    state = StrategyEngineState(now_provider=fixed_now)
    state.recent_alerts = [
        {
            "ticker": "RENX",
            "type": "VOLUME_SPIKE",
            "time": "07:31:05 AM ET",
            "price": 2.05,
            "volume": 237_057,
            "float": 2_318_049,
            "bid": 2.02,
            "ask": 2.05,
            "bid_size": 100,
            "ask_size": 500,
        },
        {
            "ticker": "RENX",
            "type": "SQUEEZE_5MIN",
            "time": "07:31:05 AM ET",
            "price": 2.05,
            "volume": 237_057,
            "float": 2_318_049,
            "bid": 2.02,
            "ask": 2.05,
            "bid_size": 100,
            "ask_size": 500,
            "details": {"change_pct": 12.1},
        },
        {
            "ticker": "RENX",
            "type": "SQUEEZE_10MIN",
            "time": "07:31:05 AM ET",
            "price": 2.05,
            "volume": 237_057,
            "float": 2_318_049,
            "bid": 2.02,
            "ask": 2.05,
            "bid_size": 100,
            "ask_size": 500,
            "details": {"change_pct": 12.0},
        },
    ]
    state._pending_recent_alert_replay = True

    renx_snapshot = snapshot_from_payload(make_snapshot_payload(symbol="RENX", price=2.39, volume=14_798_300))
    renx_snapshot.previous_close = 1.78

    summary = state.process_snapshot_batch(
        [renx_snapshot],
        {"RENX": ReferenceData(shares_outstanding=2_318_049, avg_daily_volume=784_680.24)},
    )

    assert state._pending_recent_alert_replay is False
    assert [item["ticker"] for item in state.confirmed_scanner.get_all_confirmed()] == ["RENX"]
    assert [item["ticker"] for item in summary["top_confirmed"]] == ["RENX"]


def test_alert_engine_restore_skips_prior_session_alert_tape() -> None:
    session_factory = build_test_session_factory()
    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(
            "project_mai_tai.services.strategy_engine_app.utcnow",
            lambda: datetime(2026, 4, 1, 7, 30, tzinfo=UTC),
        )
        monkeypatch.setattr(
            "project_mai_tai.services.strategy_engine_app.current_scanner_session_start_utc",
            lambda now=None: datetime(2026, 4, 1, 8, 0, tzinfo=UTC),
        )

        service = StrategyEngineService(
            settings=make_test_settings(),
            redis_client=FakeRedis(),
            session_factory=session_factory,
        )
        service.state.alert_engine.now_provider = lambda: datetime(2026, 4, 1, 7, 30, tzinfo=UTC)
        service.state.alert_engine.record_snapshot(
            [snapshot_from_payload(make_snapshot_payload(symbol="MASK", price=2.5, volume=200_000))]
        )
        service.state.recent_alerts = [{"ticker": "MASK", "type": "VOLUME_SPIKE"}]
        service._persist_scanner_snapshots(
            {
                "top_confirmed": [],
                "watchlist": [],
                "cycle_count": 1,
            }
        )

        restored = StrategyEngineService(
            settings=make_test_settings(),
            redis_client=FakeRedis(),
            session_factory=session_factory,
        )
        restored._restore_alert_engine_state_from_dashboard_snapshot()

        assert restored.state.alert_engine.get_warmup_status()["history_cycles"] == 0
        assert restored.state.recent_alerts == []
        assert restored.state.top_gainer_changes == []
        assert restored.state._first_seen_by_ticker == {}


def test_alert_engine_restore_skips_unmarked_snapshot_even_if_recent(monkeypatch) -> None:
    session_factory = build_test_session_factory()
    with session_factory() as session:
        session.add(
            DashboardSnapshot(
                snapshot_type="scanner_alert_engine_state",
                payload={
                    "persisted_at": datetime(2026, 4, 1, 10, 5, tzinfo=UTC).isoformat(),
                    "history": [{"MASK": [2.5, 200_000]}],
                    "recent_alerts": [{"ticker": "MASK", "type": "VOLUME_SPIKE"}],
                },
            )
        )
        session.commit()

    monkeypatch.setattr(
        "project_mai_tai.services.strategy_engine_app.utcnow",
        lambda: datetime(2026, 4, 1, 10, 10, tzinfo=UTC),
    )
    monkeypatch.setattr(
        "project_mai_tai.services.strategy_engine_app.current_scanner_session_start_utc",
        lambda now=None: datetime(2026, 4, 1, 8, 0, tzinfo=UTC),
    )

    restored = StrategyEngineService(
        settings=make_test_settings(),
        redis_client=FakeRedis(),
        session_factory=session_factory,
    )
    restored._restore_alert_engine_state_from_dashboard_snapshot()

    assert restored.state.alert_engine.get_warmup_status()["history_cycles"] == 0
    assert restored.state.recent_alerts == []


def test_snapshot_batch_keeps_runner_aligned_to_visible_confirmed_names(monkeypatch) -> None:
    state = StrategyEngineState(
        settings=make_test_settings(
            strategy_macd_1m_enabled=True,
            strategy_tos_enabled=True,
            strategy_runner_enabled=True,
        ),
        now_provider=fixed_now,
    )
    visible_confirmed = [
        {
            "ticker": "ELAB",
            "confirmed_at": "10:00:00 AM ET",
            "entry_price": 3.19,
            "price": 2.78,
            "change_pct": 66.5,
            "volume": 7_200_000,
            "rvol": 12.0,
            "shares_outstanding": 541_500,
            "bid": 2.76,
            "ask": 2.77,
            "spread": 0.01,
            "spread_pct": 0.36,
            "hod": 3.19,
            "vwap": 2.81,
            "prev_close": 1.67,
            "avg_daily_volume": 600_000,
            "first_spike_time": "09:45:00 AM ET",
            "first_spike_price": 2.20,
            "squeeze_count": 3,
            "data_age_secs": 0,
            "confirmation_path": "PATH_B_2SQ",
            "rank_score": 75.0,
        }
    ]
    hidden_confirmed = [
        *visible_confirmed,
        {
            "ticker": "ABCD",
            "rank_score": 20.0,
            "change_pct": 18.0,
            "confirmed_at": "09:40:00 AM ET",
            "confirmation_path": "PATH_B_2SQ",
        },
        {
            "ticker": "WXYZ",
            "rank_score": 15.0,
            "change_pct": 14.0,
            "confirmed_at": "09:41:00 AM ET",
            "confirmation_path": "PATH_B_2SQ",
        },
        {
            "ticker": "MNOP",
            "rank_score": 10.0,
            "change_pct": 11.0,
            "confirmed_at": "09:42:00 AM ET",
            "confirmation_path": "PATH_A_NEWS",
        },
    ]

    monkeypatch.setattr(state.alert_engine, "check_alerts", lambda snapshots, reference_data: [])
    monkeypatch.setattr(
        state.confirmed_scanner,
        "process_alerts",
        lambda alerts, reference_data, snapshot_lookup: [],
    )
    monkeypatch.setattr(
        state.confirmed_scanner,
        "get_all_confirmed",
        lambda: list(hidden_confirmed),
    )
    monkeypatch.setattr(state.confirmed_scanner, "get_ranked_confirmed", lambda *args, **kwargs: list(visible_confirmed))

    summary = state.process_snapshot_batch(
        [snapshot_from_payload(make_snapshot_payload(symbol="ELAB", price=2.78, volume=7_200_000))],
        {"ELAB": ReferenceData(shares_outstanding=541_500, avg_daily_volume=600_000)},
    )

    assert summary["watchlist"] == ["ABCD", "ELAB", "MNOP", "WXYZ"]
    for code in ("macd_30s", "macd_1m", "tos", "runner"):
        assert state.bots[code].watchlist == {"ELAB", "ABCD", "WXYZ", "MNOP"}
    assert state.bots["runner"]._candidates == {"ELAB": visible_confirmed[0]}


def test_snapshot_batch_retains_removed_symbols_in_bot_watchlists_for_session_continuity(monkeypatch) -> None:
    state = StrategyEngineState(
        settings=make_test_settings(
            strategy_macd_1m_enabled=True,
            strategy_tos_enabled=True,
            strategy_runner_enabled=True,
        ),
        now_provider=fixed_now,
    )
    first_confirmed = [
        {"ticker": "ELAB", "rank_score": 80.0, "change_pct": 40.0, "confirmed_at": "09:45:00 AM ET"},
        {"ticker": "UGRO", "rank_score": 70.0, "change_pct": 32.0, "confirmed_at": "09:50:00 AM ET"},
    ]
    second_confirmed = [
        {"ticker": "ELAB", "rank_score": 82.0, "change_pct": 42.0, "confirmed_at": "09:45:00 AM ET"}
    ]
    current_all = {"value": list(first_confirmed)}
    current_top = {"value": list(first_confirmed)}

    monkeypatch.setattr(state.alert_engine, "check_alerts", lambda snapshots, reference_data: [])
    monkeypatch.setattr(
        state.confirmed_scanner,
        "process_alerts",
        lambda alerts, reference_data, snapshot_lookup: [],
    )
    monkeypatch.setattr(
        state.confirmed_scanner,
        "get_all_confirmed",
        lambda: list(current_all["value"]),
    )
    monkeypatch.setattr(state.confirmed_scanner, "get_ranked_confirmed", lambda *args, **kwargs: list(current_top["value"]))

    state.process_snapshot_batch(
        [
            snapshot_from_payload(make_snapshot_payload(symbol="ELAB", price=2.78, volume=7_200_000)),
            snapshot_from_payload(make_snapshot_payload(symbol="UGRO", price=2.40, volume=900_000)),
        ],
        {
            "ELAB": ReferenceData(shares_outstanding=541_500, avg_daily_volume=600_000),
            "UGRO": ReferenceData(shares_outstanding=50_000, avg_daily_volume=390_000),
        },
    )

    for code in ("macd_30s", "macd_1m", "tos"):
        assert state.bots[code].watchlist == {"ELAB", "UGRO"}

    current_all["value"] = list(second_confirmed)
    current_top["value"] = list(second_confirmed)

    state.process_snapshot_batch(
        [snapshot_from_payload(make_snapshot_payload(symbol="ELAB", price=2.82, volume=7_400_000))],
        {"ELAB": ReferenceData(shares_outstanding=541_500, avg_daily_volume=600_000)},
    )

    for code in ("macd_30s", "macd_1m", "tos"):
        assert state.bots[code].watchlist == {"ELAB", "UGRO"}
    assert state.bots["runner"]._candidates == {"ELAB": second_confirmed[0]}


def test_snapshot_batch_keeps_low_score_confirmed_visible_but_out_of_watchlist(monkeypatch) -> None:
    state = StrategyEngineState(
        settings=make_test_settings(
            strategy_macd_1m_enabled=True,
            strategy_tos_enabled=True,
        ),
        now_provider=fixed_now,
    )
    low_score_confirmed = [
        {"ticker": "RENX", "rank_score": 32.0, "change_pct": 34.0, "confirmed_at": "07:31:05 AM ET"},
        {"ticker": "BCG", "rank_score": 28.0, "change_pct": 57.0, "confirmed_at": "07:10:00 AM ET"},
    ]

    monkeypatch.setattr(state.alert_engine, "check_alerts", lambda snapshots, reference_data: [])
    monkeypatch.setattr(
        state.confirmed_scanner,
        "process_alerts",
        lambda alerts, reference_data, snapshot_lookup: [],
    )
    monkeypatch.setattr(state.confirmed_scanner, "get_all_confirmed", lambda: list(low_score_confirmed))
    monkeypatch.setattr(state.confirmed_scanner, "get_ranked_confirmed", lambda *args, **kwargs: list(low_score_confirmed))

    summary = state.process_snapshot_batch(
        [
            snapshot_from_payload(make_snapshot_payload(symbol="RENX", price=2.39, volume=14_798_300)),
            snapshot_from_payload(make_snapshot_payload(symbol="BCG", price=2.47, volume=36_000_000)),
        ],
        {
            "RENX": ReferenceData(shares_outstanding=2_318_049, avg_daily_volume=784_680.24),
            "BCG": ReferenceData(shares_outstanding=7_800_000, avg_daily_volume=1_200_000),
        },
    )

    assert [item["ticker"] for item in summary["all_confirmed"]] == ["RENX", "BCG"]
    assert summary["watchlist"] == ["BCG", "RENX"]
    assert [item["ticker"] for item in summary["top_confirmed"]] == ["RENX", "BCG"]
    for code in ("macd_30s", "macd_1m", "tos"):
        assert state.bots[code].watchlist == {"RENX", "BCG"}


def test_global_manual_stop_blocks_handoff_to_all_bots(monkeypatch) -> None:
    state = StrategyEngineState(now_provider=fixed_now)
    state.apply_global_manual_stop_symbols({"ELAB"})
    confirmed = [
        {"ticker": "ELAB", "rank_score": 80.0, "change_pct": 40.0, "confirmed_at": "09:45:00 AM ET"},
        {"ticker": "UGRO", "rank_score": 70.0, "change_pct": 32.0, "confirmed_at": "09:50:00 AM ET"},
    ]

    monkeypatch.setattr(state.alert_engine, "check_alerts", lambda snapshots, reference_data: [])
    monkeypatch.setattr(
        state.confirmed_scanner,
        "process_alerts",
        lambda alerts, reference_data, snapshot_lookup: [],
    )
    monkeypatch.setattr(
        state.confirmed_scanner,
        "get_all_confirmed",
        lambda: list(confirmed),
    )
    monkeypatch.setattr(
        state.confirmed_scanner,
        "get_top_n",
        lambda *args, **kwargs: list(confirmed),
    )

    summary = state.process_snapshot_batch(
        [
            snapshot_from_payload(make_snapshot_payload(symbol="ELAB", price=2.78, volume=7_200_000)),
            snapshot_from_payload(make_snapshot_payload(symbol="UGRO", price=2.40, volume=900_000)),
        ],
        {
            "ELAB": ReferenceData(shares_outstanding=541_500, avg_daily_volume=600_000),
            "UGRO": ReferenceData(shares_outstanding=50_000, avg_daily_volume=390_000),
        },
    )

    assert [item["ticker"] for item in summary["all_confirmed"]] == ["UGRO"]
    assert summary["watchlist"] == ["UGRO"]
    for code in state.bots:
        assert state.bots[code].watchlist == {"UGRO"}
        assert "ELAB" in state.bots[code].manual_stop_symbols


def test_scanner_session_roll_clears_manual_stop_state() -> None:
    clock = {
        "now": datetime(2026, 4, 21, 3, 59, tzinfo=UTC),
    }
    state = StrategyEngineState(now_provider=lambda: clock["now"])
    state.apply_global_manual_stop_symbols({"ELAB"})
    state.apply_manual_stop_symbols({"macd_30s": {"UGRO"}, "tos": {"QNRX"}})

    assert state.global_manual_stop_symbols == {"ELAB"}
    assert state.manual_stop_symbols_by_strategy["macd_30s"] == {"UGRO"}
    assert state.bots["macd_30s"].manual_stop_symbols == {"ELAB", "UGRO"}

    clock["now"] = datetime(2026, 4, 21, 8, 1, tzinfo=UTC)
    state._roll_scanner_session_if_needed()

    assert state.global_manual_stop_symbols == set()
    assert state.manual_stop_symbols_by_strategy == {}
    for code in state.bots:
        assert state.bots[code].manual_stop_symbols == set()


def test_manual_stop_update_removes_symbol_from_live_watchlist_immediately() -> None:
    state = StrategyEngineState(now_provider=fixed_now)
    state.restore_confirmed_runtime_view(
        [
            {"ticker": "AGPU", "rank_score": 80.0, "change_pct": 40.0, "confirmed_at": "09:45:00 AM ET"},
            {"ticker": "WBUY", "rank_score": 70.0, "change_pct": 32.0, "confirmed_at": "09:50:00 AM ET"},
        ]
    )

    assert "AGPU" in state.bots["macd_30s"].watchlist
    assert "WBUY" in state.bots["macd_30s"].watchlist

    state.apply_manual_stop_update(
        scope="bot",
        action="stop",
        strategy_code="macd_30s",
        symbol="AGPU",
    )

    assert "AGPU" in state.bots["macd_30s"].manual_stop_symbols
    assert "AGPU" not in state.bots["macd_30s"].watchlist
    assert "WBUY" in state.bots["macd_30s"].watchlist


def test_manual_stop_resume_readds_symbol_to_live_watchlist_immediately() -> None:
    state = StrategyEngineState(now_provider=fixed_now)
    state.restore_confirmed_runtime_view(
        [
            {"ticker": "AGPU", "rank_score": 80.0, "change_pct": 40.0, "confirmed_at": "09:45:00 AM ET"},
            {"ticker": "WBUY", "rank_score": 70.0, "change_pct": 32.0, "confirmed_at": "09:50:00 AM ET"},
        ]
    )
    state.apply_manual_stop_update(
        scope="bot",
        action="stop",
        strategy_code="macd_30s",
        symbol="AGPU",
    )

    state.apply_manual_stop_update(
        scope="bot",
        action="resume",
        strategy_code="macd_30s",
        symbol="AGPU",
    )

    assert "AGPU" not in state.bots["macd_30s"].manual_stop_symbols
    assert "AGPU" in state.bots["macd_30s"].watchlist
    assert "WBUY" in state.bots["macd_30s"].watchlist


def test_restore_confirmed_runtime_view_keeps_manual_stops_out_of_watchlist() -> None:
    state = StrategyEngineState(now_provider=fixed_now)
    state.apply_manual_stop_symbols({"macd_30s": {"AGPU"}})

    state.restore_confirmed_runtime_view(
        [
            {"ticker": "AGPU", "rank_score": 80.0, "change_pct": 40.0, "confirmed_at": "09:45:00 AM ET"},
            {"ticker": "WBUY", "rank_score": 70.0, "change_pct": 32.0, "confirmed_at": "09:50:00 AM ET"},
        ]
    )

    assert "AGPU" in state.bots["macd_30s"].manual_stop_symbols
    assert "AGPU" not in state.bots["macd_30s"].watchlist
    assert "WBUY" in state.bots["macd_30s"].watchlist


def test_service_preloads_manual_stops_before_post_restart_trading() -> None:
    session_factory = build_test_session_factory()
    current_session_start = current_scanner_session_start_utc(datetime(2026, 4, 22, 13, 49, 0, tzinfo=UTC))
    with session_factory() as session:
        session.add(
            DashboardSnapshot(
                snapshot_type="bot_manual_stop_symbols",
                payload={
                    "bots": {"macd_30s": ["AGPU"]},
                    "scanner_session_start_utc": current_session_start.isoformat(),
                },
                created_at=datetime(2026, 4, 22, 13, 47, 24, tzinfo=UTC),
            )
        )
        session.commit()

    service = StrategyEngineService(
        settings=make_test_settings(
            redis_url="redis://localhost:6379/0",
            strategy_macd_30s_enabled=True,
            strategy_macd_1m_enabled=False,
            strategy_tos_enabled=False,
            strategy_runner_enabled=False,
            strategy_macd_30s_broker_provider="schwab",
            strategy_macd_30s_probe_enabled=False,
            strategy_macd_30s_reclaim_enabled=False,
            strategy_macd_30s_retest_enabled=False,
        ),
        redis_client=FakeRedis(),
        session_factory=session_factory,
        now_provider=lambda: datetime(2026, 4, 22, 13, 49, 0, tzinfo=UTC),
    )

    service.state.restore_confirmed_runtime_view(
        [
            {"ticker": "AGPU", "rank_score": 80.0, "change_pct": 40.0, "confirmed_at": "09:45:00 AM ET"},
            {"ticker": "WBUY", "rank_score": 70.0, "change_pct": 32.0, "confirmed_at": "09:50:00 AM ET"},
        ]
    )
    assert "AGPU" in service.state.bots["macd_30s"].watchlist

    service._preload_manual_stop_state()

    assert "AGPU" in service.state.bots["macd_30s"].manual_stop_symbols
    assert "AGPU" not in service.state.bots["macd_30s"].watchlist
    assert "WBUY" in service.state.bots["macd_30s"].watchlist


def test_restore_alert_engine_state_does_not_seed_schwab_prewarm_from_old_recent_alerts(monkeypatch) -> None:
    session_factory = build_test_session_factory()
    current_session_start = current_scanner_session_start_utc(datetime(2026, 4, 22, 13, 49, 0, tzinfo=UTC))
    monkeypatch.setattr(
        "project_mai_tai.services.strategy_engine_app.current_scanner_session_start_utc",
        lambda now=None: current_session_start,
    )
    with session_factory() as session:
        session.add(
            DashboardSnapshot(
                snapshot_type="scanner_alert_engine_state",
                payload={
                    "persisted_at": datetime(2026, 4, 22, 13, 47, 24, tzinfo=UTC).isoformat(),
                    "scanner_session_start_utc": current_session_start.isoformat(),
                    "recent_alerts": [
                        {"ticker": "AGPU", "type": "VOLUME_SPIKE", "price": 2.5, "volume": 100000},
                        {"ticker": "WBUY", "type": "VOLUME_SPIKE", "price": 2.8, "volume": 150000},
                    ],
                    "history_cycles": 5,
                },
                created_at=datetime(2026, 4, 22, 13, 47, 24, tzinfo=UTC),
            )
        )
        session.commit()

    service = StrategyEngineService(
        settings=make_test_settings(
            redis_url="redis://localhost:6379/0",
            dashboard_snapshot_persistence_enabled=True,
            strategy_macd_30s_enabled=True,
            strategy_macd_30s_broker_provider="schwab",
            strategy_macd_1m_enabled=False,
            strategy_tos_enabled=False,
            strategy_runner_enabled=False,
        ),
        redis_client=FakeRedis(),
        session_factory=session_factory,
        now_provider=lambda: datetime(2026, 4, 22, 13, 49, 0, tzinfo=UTC),
    )

    monkeypatch.setattr(service.state.alert_engine, "restore_state", lambda payload: True)

    service._restore_alert_engine_state_from_dashboard_snapshot()

    assert [item["ticker"] for item in service.state.recent_alerts] == ["AGPU", "WBUY"]
    assert service.state.schwab_prewarm_symbols == []
    assert service.state.bots["macd_30s"].prewarm_symbols == set()


def test_service_ignores_and_purges_markerless_manual_stop_snapshot_from_current_session() -> None:
    session_factory = build_test_session_factory()
    now = datetime(2026, 4, 22, 13, 49, 0, tzinfo=UTC)
    current_session_start = current_scanner_session_start_utc(now)
    with session_factory() as session:
        session.add(
            DashboardSnapshot(
                snapshot_type="bot_manual_stop_symbols",
                payload={"bots": {"macd_30s": ["AGPU"]}},
                created_at=current_session_start + timedelta(hours=1),
            )
        )
        session.commit()

    service = StrategyEngineService(
        settings=make_test_settings(
            redis_url="redis://localhost:6379/0",
            strategy_macd_30s_enabled=True,
            strategy_macd_1m_enabled=False,
            strategy_tos_enabled=False,
            strategy_runner_enabled=False,
            strategy_macd_30s_broker_provider="schwab",
            strategy_macd_30s_probe_enabled=False,
            strategy_macd_30s_reclaim_enabled=False,
            strategy_macd_30s_retest_enabled=False,
        ),
        redis_client=FakeRedis(),
        session_factory=session_factory,
        now_provider=lambda: now,
    )

    service._purge_stale_manual_stop_snapshots()
    service._preload_manual_stop_state()

    assert service.state.bots["macd_30s"].manual_stop_symbols == set()
    with session_factory() as session:
        snapshot = session.scalar(
            select(DashboardSnapshot).where(
                DashboardSnapshot.snapshot_type == "bot_manual_stop_symbols"
            )
        )
        assert snapshot is None


def test_state_ignores_order_updates_for_unknown_strategy_code() -> None:
    state = StrategyEngineState(now_provider=fixed_now)

    state.apply_order_status(
        strategy_code="macd_30s_reclaim",
        symbol="UGRO",
        intent_type="open",
        status="filled",
    )
    state.apply_execution_fill(
        strategy_code="macd_30s_reclaim",
        client_order_id="abc123",
        symbol="UGRO",
        intent_type="open",
        status="filled",
        side="buy",
        quantity=10,
        price=2.5,
    )

    assert "UGRO" not in state.bots["macd_30s"].positions.get_all_positions()


def test_snapshot_batch_keeps_faded_confirmed_symbols_in_bot_watchlists_for_session_continuity() -> None:
    state = StrategyEngineState(
        settings=make_test_settings(
            strategy_macd_1m_enabled=True,
            strategy_tos_enabled=True,
            strategy_runner_enabled=True,
        ),
        now_provider=fixed_now,
    )
    state.confirmed_scanner.seed_confirmed_candidates(
        [
            {
                "ticker": "POLA",
                "confirmed_at": "08:00:00 AM ET",
                "entry_price": 2.30,
                "price": 2.32,
                "change_pct": 24.0,
                "volume": 900_000,
                "rvol": 8.0,
                "shares_outstanding": 1_000_000,
                "bid": 2.31,
                "ask": 2.32,
                "spread": 0.01,
                "spread_pct": 0.43,
                "first_spike_time": "07:45:00 AM ET",
                "squeeze_count": 2,
                "confirmation_path": "PATH_B_2SQ",
                "rank_score": 72.0,
                "prev_close": 2.0,
            }
        ]
    )
    state.confirmed_scanner._tracking["POLA"] = {
        "has_volume_spike": True,
        "first_spike_time": "07:45:00 AM ET",
        "first_spike_price": 2.1,
        "first_spike_volume": 500_000,
        "squeezes": [{"time": "08:00:00 AM ET", "price": 2.32, "volume": 900_000}],
        "confirmed": True,
        "confirmed_at": "08:00:00 AM ET",
        "confirmed_price": 2.32,
    }

    state.process_snapshot_batch(
        [snapshot_from_payload(make_snapshot_payload(symbol="POLA", price=2.10, volume=950_000))],
        {"POLA": ReferenceData(shares_outstanding=1_000_000, avg_daily_volume=200_000)},
    )

    assert [item["ticker"] for item in state.confirmed_scanner.get_all_confirmed()] == ["POLA"]
    for code in ("macd_30s", "macd_1m", "tos", "runner"):
        assert state.bots[code].watchlist == {"POLA"}
    assert set(state.bots["runner"]._candidates) == {"POLA"}


def test_bot_runtime_clears_ghost_position_on_no_position_reject() -> None:
    state = StrategyEngineState(now_provider=fixed_now)
    bot = state.bots["macd_30s"]
    bot.positions.open_position("ASTC", 5.31, quantity=10, path="P1_MACD_CROSS")
    bot.pending_close_symbols.add("ASTC")

    bot.apply_order_status(
        symbol="ASTC",
        intent_type="close",
        status="rejected",
        reason='asset "ASTC" cannot be sold short',
    )

    assert bot.positions.get_position("ASTC") is None
    assert "ASTC" not in bot.pending_close_symbols


def test_bot_runtime_clears_ghost_position_on_no_strategy_position_reject() -> None:
    state = StrategyEngineState(now_provider=fixed_now)
    bot = state.bots["macd_30s"]
    bot.positions.open_position("BFRG", 1.83, quantity=24, path="P3_MACD_SURGE")
    bot.pending_scale_levels.add(("BFRG", "FAST4"))

    bot.apply_order_status(
        symbol="BFRG",
        intent_type="scale",
        status="rejected",
        level="FAST4",
        reason="no strategy position available to sell",
    )

    assert bot.positions.get_position("BFRG") is None
    assert ("BFRG", "FAST4") not in bot.pending_scale_levels


def test_bot_runtime_clears_position_on_final_close_fill_even_if_qty_differs() -> None:
    state = StrategyEngineState(now_provider=fixed_now)
    bot = state.bots["macd_30s"]
    bot.positions.open_position("BFRG", 1.28, quantity=10, path="P1_MACD_CROSS")
    bot.pending_close_symbols.add("BFRG")

    bot.apply_execution_fill(
        client_order_id="macd_30s-BFRG-close-1",
        symbol="BFRG",
        intent_type="close",
        status="filled",
        side="sell",
        quantity=Decimal("9"),
        price=Decimal("1.28"),
    )

    assert bot.positions.get_position("BFRG") is None
    assert "BFRG" not in bot.pending_close_symbols


def test_bot_runtime_preserves_strategy_close_reason_on_filled_close() -> None:
    state = StrategyEngineState(
        settings=make_test_settings(strategy_macd_30s_reclaim_enabled=True),
        now_provider=fixed_now,
    )
    bot = state.bots["macd_30s_reclaim"]
    bot.positions.reset()
    bot.positions.open_position("ROLR", 7.25, quantity=25, path="PRETRIGGER_RECLAIM")
    bot.pending_close_symbols.add("ROLR")

    bot.apply_execution_fill(
        client_order_id="macd_30s_reclaim-ROLR-close-1",
        symbol="ROLR",
        intent_type="close",
        status="filled",
        side="sell",
        quantity=Decimal("25"),
        price=Decimal("7.31"),
        reason="STOCHK_TIER1",
    )

    closed = bot.positions.get_closed_today()
    assert len(closed) == 1
    assert closed[0]["reason"] == "STOCHK_TIER1"
    assert closed[0]["path"] == "PRETRIGGER_RECLAIM"


def test_bot_runtime_finalizes_trade_when_scale_fill_arrives_after_partial_close() -> None:
    state = StrategyEngineState(now_provider=fixed_now)
    bot = state.bots["macd_30s"]
    bot.positions.reset()
    bot.positions.open_position("LOCL", 2.97, quantity=10, path="P1_CROSS")
    bot.pending_close_symbols.add("LOCL")
    bot.pending_scale_levels.add(("LOCL", "PCT1"))

    bot.apply_execution_fill(
        client_order_id="macd_30s-LOCL-close-1",
        symbol="LOCL",
        intent_type="close",
        status="partially_filled",
        side="sell",
        quantity=Decimal("8"),
        price=Decimal("2.91"),
        reason="HARD_STOP",
    )

    assert bot.positions.get_position("LOCL") is not None
    assert bot.positions.get_position("LOCL").quantity == 2

    bot.apply_execution_fill(
        client_order_id="macd_30s-LOCL-scale-1",
        symbol="LOCL",
        intent_type="scale",
        status="filled",
        side="sell",
        quantity=Decimal("2"),
        price=Decimal("2.99"),
        level="PCT1",
        reason="SCALE_PCT1",
    )

    assert bot.positions.get_position("LOCL") is None
    assert "LOCL" not in bot.pending_close_symbols
    assert ("LOCL", "PCT1") not in bot.pending_scale_levels
    closed = bot.positions.get_closed_today()
    assert len(closed) == 1
    assert closed[0]["ticker"] == "LOCL"
    assert closed[0]["reason"] == "SCALE_PCT1"
    assert bot.entry_engine._last_exit_bar["LOCL"] == 0


def test_bot_runtime_snapshots_degraded_scale_profile_on_open_fill() -> None:
    state = StrategyEngineState(now_provider=fixed_now)
    bot = state.bots["macd_30s"]
    bot.set_watchlist(["ENVB"])
    object.__setattr__(bot.lifecycle_policy.config, "degraded_enabled", True)
    bot.lifecycle_states["ENVB"].degraded_mode = True

    bot.apply_execution_fill(
        client_order_id="macd_30s-ENVB-open-1",
        symbol="ENVB",
        intent_type="open",
        status="filled",
        side="buy",
        quantity=Decimal("10"),
        price=Decimal("4.12"),
        path="P3_MACD_SURGE",
    )

    position = bot.positions.get_position("ENVB")
    assert position is not None
    assert position.scale_profile == "DEGRADED"


def test_bot_runtime_uses_normal_scale_profile_when_degraded_disabled() -> None:
    state = StrategyEngineState(now_provider=fixed_now)
    bot = state.bots["macd_30s"]
    bot.set_watchlist(["ENVB"])
    bot.lifecycle_states["ENVB"].degraded_mode = True

    bot.apply_execution_fill(
        client_order_id="macd_30s-ENVB-open-1",
        symbol="ENVB",
        intent_type="open",
        status="filled",
        side="buy",
        quantity=Decimal("10"),
        price=Decimal("4.12"),
        path="P3_MACD_SURGE",
    )

    position = bot.positions.get_position("ENVB")
    assert position is not None
    assert position.scale_profile == "NORMAL"


def test_trade_tick_generates_open_intent_for_confirmed_watchlist(monkeypatch) -> None:
    state = StrategyEngineState(
        settings=make_test_settings(strategy_macd_30s_live_aggregate_bars_enabled=False),
        now_provider=fixed_now,
    )
    bot = state.bots["macd_30s"]
    bot.set_watchlist(["UGRO"])
    bot.definition.trading_config.entry_intrabar_enabled = True
    bot.definition.trading_config.p4_prev_bar_entry_enabled = False
    bot.latest_quotes["UGRO"] = {"bid": 2.79, "ask": 2.80}
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


def test_trade_tick_records_blocked_decision_reason(monkeypatch) -> None:
    state = StrategyEngineState(
        settings=make_test_settings(strategy_macd_30s_live_aggregate_bars_enabled=False),
        now_provider=fixed_now,
    )
    bot = state.bots["macd_30s"]
    bot.set_watchlist(["UGRO"])
    bot.definition.trading_config.entry_intrabar_enabled = False
    bot.definition.trading_config.p4_prev_bar_entry_enabled = True
    state.seed_bars(
        "macd_30s",
        "UGRO",
        seed_trending_bars(start_timestamp=1_700_000_000.0, interval_secs=30),
    )
    monkeypatch.setattr(
        bot.indicator_engine,
        "calculate",
        lambda bars: {
            "price": 2.8,
            "price_above_ema20": True,
            "macd_cross_above": False,
            "price_cross_above_vwap": False,
            "macd_above_signal": True,
            "macd_increasing": True,
            "macd_delta": 0.0,
            "macd_delta_accelerating": False,
            "histogram": 0.0,
            "price_above_ema9": True,
            "volume": 20_000,
            "histogram_growing": False,
            "stoch_k_rising": False,
            "price_above_vwap": True,
            "price_above_both_emas": True,
            "macd": 0.1,
            "signal": 0.05,
            "stoch_k": 40.0,
            "ema9": 2.7,
            "ema20": 2.6,
        },
    )

    intents = state.handle_trade_tick(
        symbol="UGRO",
        price=2.8,
        size=200,
        timestamp_ns=1_700_001_500_000_000_000,
    )

    assert intents == []
    assert bot.summary()["recent_decisions"] == []


def test_trade_tick_can_emit_intrabar_scale_intent() -> None:
    runtime = StrategyBotRuntime(
        StrategyDefinition(
            code="macd_30s",
            display_name="30s",
            account_name="paper:test",
            interval_secs=30,
            trading_config=TradingConfig(),
            indicator_config=IndicatorConfig(),
        ),
        now_provider=lambda: datetime(2026, 4, 18, 10, 0, tzinfo=UTC),
    )
    runtime.positions.open_position("ELAB", 1.00, quantity=10, path="P1")
    runtime.latest_quotes["ELAB"] = {"bid": 1.02, "ask": 1.03}

    intents = runtime.handle_trade_tick(
        symbol="ELAB",
        price=1.02,
        size=100,
        timestamp_ns=1_700_001_500_000_000_000,
    )

    scale_intents = [intent for intent in intents if intent.payload.intent_type == "scale"]
    assert len(scale_intents) == 1
    assert scale_intents[0].payload.reason == "SCALE_PCT2"
    assert ("ELAB", "PCT2") in runtime.pending_scale_levels


def test_trade_tick_does_not_stack_second_scale_while_first_scale_pending() -> None:
    runtime = StrategyBotRuntime(
        StrategyDefinition(
            code="macd_30s",
            display_name="30s",
            account_name="paper:test",
            interval_secs=30,
            trading_config=TradingConfig(),
            indicator_config=IndicatorConfig(),
        ),
        now_provider=lambda: datetime(2026, 4, 18, 10, 0, tzinfo=UTC),
    )
    runtime.positions.open_position("SST", 2.8899, quantity=10, path="P3")
    runtime.pending_scale_levels.add(("SST", "PCT2"))
    runtime.latest_quotes["SST"] = {"bid": 3.03, "ask": 3.04}

    intents = runtime.handle_trade_tick(
        symbol="SST",
        price=3.035,
        size=100,
        timestamp_ns=1_700_001_501_000_000_000,
    )

    scale_intents = [intent for intent in intents if intent.payload.intent_type == "scale"]
    assert scale_intents == []
    assert runtime.pending_scale_levels == {("SST", "PCT2")}


def test_trade_tick_can_emit_intrabar_floor_breach_close() -> None:
    config = TradingConfig(
        scale_fast4_pct=100.0,
        scale_normal2_pct=100.0,
        scale_4after2_pct=100.0,
    )
    runtime = StrategyBotRuntime(
        StrategyDefinition(
            code="macd_30s",
            display_name="30s",
            account_name="paper:test",
            interval_secs=30,
            trading_config=config,
            indicator_config=IndicatorConfig(),
        ),
        now_provider=lambda: datetime(2026, 4, 18, 10, 0, tzinfo=UTC),
    )
    runtime.positions.open_position("ELAB", 1.00, quantity=10, path="P1")
    runtime.latest_quotes["ELAB"] = {"bid": 1.014, "ask": 1.015}

    warmup_intents = runtime.handle_trade_tick(
        symbol="ELAB",
        price=1.03,
        size=100,
        timestamp_ns=1_700_001_500_000_000_000,
    )
    assert warmup_intents == []

    intents = runtime.handle_trade_tick(
        symbol="ELAB",
        price=1.014,
        size=100,
        timestamp_ns=1_700_001_501_000_000_000,
    )

    close_intents = [intent for intent in intents if intent.payload.intent_type == "close"]
    assert len(close_intents) == 1
    assert close_intents[0].payload.reason == "FLOOR_BREACH"
    assert "ELAB" in runtime.pending_close_symbols


def test_trade_tick_uses_monotonic_bar_count_after_history_trim(monkeypatch) -> None:
    state = StrategyEngineState(
        settings=make_test_settings(strategy_macd_30s_live_aggregate_bars_enabled=False),
        now_provider=fixed_now,
    )
    bot = state.bots["macd_30s"]
    bot.set_watchlist(["UGRO"])
    bot.definition.trading_config.entry_intrabar_enabled = True
    start_timestamp = 1_700_000_000.0
    state.seed_bars(
        "macd_30s",
        "UGRO",
        seed_trending_bars(
            count=2_105,
            start_timestamp=start_timestamp,
            interval_secs=30,
        ),
    )

    monkeypatch.setattr(bot.indicator_engine, "calculate", lambda bars: {"price": 2.8})
    bar_indices: list[int] = []

    def fake_check_entry(symbol, indicators, bar_index, position_tracker):
        del symbol, indicators, position_tracker
        bar_indices.append(bar_index)
        return None

    monkeypatch.setattr(bot.entry_engine, "check_entry", fake_check_entry)

    first_tick = int((start_timestamp + 2_105 * 30 + 1) * 1_000_000_000)
    second_tick = int((start_timestamp + 2_106 * 30 + 1) * 1_000_000_000)

    state.handle_trade_tick(symbol="UGRO", price=2.8, size=200, timestamp_ns=first_tick)
    state.handle_trade_tick(symbol="UGRO", price=2.81, size=200, timestamp_ns=second_tick)

    assert bar_indices == [2_001, 2_001, 2_002]


def test_trimmed_history_does_not_lock_out_new_open_after_cancel(monkeypatch) -> None:
    state = StrategyEngineState(
        settings=make_test_settings(strategy_macd_30s_live_aggregate_bars_enabled=False),
        now_provider=fixed_now,
    )
    bot = state.bots["macd_30s"]
    bot.set_watchlist(["UGRO"])
    bot.definition.trading_config.entry_intrabar_enabled = True
    bot.definition.trading_config.confirm_bars = 0
    bot.definition.trading_config.min_score = 0
    start_timestamp = 1_700_000_000.0
    state.seed_bars(
        "macd_30s",
        "UGRO",
        seed_trending_bars(
            count=2_105,
            start_timestamp=start_timestamp,
            interval_secs=30,
        ),
    )
    monkeypatch.setattr(
        bot.indicator_engine,
        "calculate",
        lambda bars: {
            "price": 2.8,
            "price_above_ema20": True,
            "macd_cross_above": True,
            "price_cross_above_vwap": False,
            "macd_above_signal": True,
            "macd_increasing": True,
            "macd_delta": 0.01,
            "macd_delta_accelerating": True,
            "histogram": 0.01,
            "price_above_ema9": True,
            "volume": 20_000,
            "histogram_growing": True,
            "stoch_k_rising": True,
            "price_above_vwap": True,
            "vwap": 2.75,
            "extended_vwap": 2.75,
            "price_above_both_emas": True,
            "macd": 0.1,
            "signal": 0.05,
            "stoch_k": 40.0,
            "ema9": 2.7,
            "ema20": 2.6,
            "macd_was_below_3bars": True,
        },
    )
    monkeypatch.setattr(
        bot.entry_engine,
        "check_entry",
        lambda symbol, indicators, bar_index, runtime: {
            "ticker": symbol,
            "price": float(indicators["price"]),
            "path": "P1_MACD_CROSS",
            "score": 5,
            "score_details": "trim-history-test",
        },
    )

    first_tick = int((start_timestamp + 2_105 * 30 + 1) * 1_000_000_000)
    second_tick = int((start_timestamp + 2_106 * 30 + 1) * 1_000_000_000)

    first_intents = state.handle_trade_tick(symbol="UGRO", price=2.8, size=200, timestamp_ns=first_tick)
    first_open_intents = [intent for intent in first_intents if intent.payload.intent_type == "open"]
    assert len(first_open_intents) == 1
    assert "UGRO" in bot.pending_open_symbols

    bot.apply_order_status(symbol="UGRO", intent_type="open", status="cancelled")

    second_intents = state.handle_trade_tick(symbol="UGRO", price=2.81, size=200, timestamp_ns=second_tick)
    second_open_intents = [intent for intent in second_intents if intent.payload.intent_type == "open"]
    assert len(second_open_intents) == 1


def test_flush_completed_bars_evaluates_due_bar_without_waiting_for_next_trade(monkeypatch) -> None:
    current = datetime(2026, 4, 2, 7, 0, tzinfo=UTC)

    def now_provider() -> datetime:
        return current

    state = StrategyEngineState(
        settings=make_test_settings(
            strategy_macd_30s_live_aggregate_bars_enabled=False,
            strategy_macd_30s_tick_bar_close_grace_seconds=0,
        ),
        now_provider=now_provider,
    )
    bot = state.bots["macd_30s"]
    bot.set_watchlist(["UGRO"])
    bot.definition.trading_config.entry_intrabar_enabled = True
    state.seed_bars(
        "macd_30s",
        "UGRO",
        seed_trending_bars(start_timestamp=current.timestamp() - 50 * 30, interval_secs=30),
    )
    bot.latest_quotes["UGRO"] = {"bid": 2.79, "ask": 2.80}
    bot.definition.trading_config.confirm_bars = 0
    bot.definition.trading_config.min_score = 0
    bot.definition.trading_config.entry_intrabar_enabled = False

    monkeypatch.setattr(
        bot.entry_engine,
        "check_entry",
        lambda ticker, indicators, bar_index, position_tracker: {
            "ticker": ticker,
            "price": float(indicators["price"]),
            "path": "P1_MACD_CROSS",
            "score": 5,
            "score_details": "test",
        },
    )

    tick_timestamp_ns = int(current.timestamp() * 1_000_000_000)
    initial_intents = state.handle_trade_tick(
        symbol="UGRO",
        price=2.80,
        size=200,
        timestamp_ns=tick_timestamp_ns,
    )
    assert initial_intents == []

    current = datetime(2026, 4, 2, 7, 0, 31, tzinfo=UTC)
    flushed_intents, completed_count = state.flush_completed_bars()

    assert completed_count >= 1
    open_intents = [intent for intent in flushed_intents if intent.payload.intent_type == "open"]
    assert len(open_intents) == 1
    assert open_intents[0].payload.symbol == "UGRO"


def test_live_second_bars_can_generate_open_intent_for_30s_bot(monkeypatch) -> None:
    state = StrategyEngineState(
        settings=make_test_settings(strategy_macd_30s_live_aggregate_bars_enabled=True),
        now_provider=fixed_now,
    )
    bot = state.bots["macd_30s"]
    bot.set_watchlist(["UGRO"])
    bot.definition.trading_config.confirm_bars = 0
    state.seed_bars(
        "macd_30s",
        "UGRO",
        seed_trending_bars(count=49, start_timestamp=1_700_000_000.0, interval_secs=30),
    )

    signaled = {"done": False}

    def check_entry(symbol, indicators, bar_index, runtime):
        del runtime, bar_index
        if signaled["done"]:
            return None
        signaled["done"] = True
        return {
            "action": "BUY",
            "ticker": symbol,
            "price": indicators["price"],
            "path": "P3_MACD_SURGE",
            "score": 5,
            "score_details": "test",
        }

    monkeypatch.setattr(bot.entry_engine, "check_entry", check_entry)

    intents = []
    for offset in range(31):
        intents.extend(
            state.handle_live_bar(
                symbol="UGRO",
                interval_secs=1,
                open_price=2.70 + offset * 0.001,
                high_price=2.71 + offset * 0.001,
                low_price=2.69 + offset * 0.001,
                close_price=2.705 + offset * 0.001,
                volume=500,
                timestamp=1_700_001_480.0 + offset,
                trade_count=1,
            )
        )

    open_intents = [intent for intent in intents if intent.payload.intent_type == "open"]
    assert len(open_intents) == 1
    assert open_intents[0].payload.symbol == "UGRO"


def test_live_second_bars_can_generate_open_intent_for_polygon_30s_bot(monkeypatch) -> None:
    state = StrategyEngineState(
        settings=make_test_settings(
            strategy_macd_30s_enabled=False,
            strategy_polygon_30s_enabled=True,
            strategy_polygon_30s_live_aggregate_bars_enabled=True,
        ),
        now_provider=fixed_now,
    )
    bot = state.bots["polygon_30s"]
    bot.set_watchlist(["UGRO"])
    bot.definition.trading_config.confirm_bars = 0
    state.seed_bars(
        "polygon_30s",
        "UGRO",
        seed_trending_bars(count=49, start_timestamp=1_700_000_000.0, interval_secs=30),
    )

    signaled = {"done": False}

    def check_entry(symbol, indicators, bar_index, runtime):
        del runtime, bar_index
        if signaled["done"]:
            return None
        signaled["done"] = True
        return {
            "action": "BUY",
            "ticker": symbol,
            "price": indicators["price"],
            "path": "P3_MACD_SURGE",
            "score": 5,
            "score_details": "test",
        }

    monkeypatch.setattr(bot.entry_engine, "check_entry", check_entry)

    intents = []
    for offset in range(31):
        intents.extend(
            state.handle_live_bar(
                symbol="UGRO",
                interval_secs=1,
                open_price=2.70 + offset * 0.001,
                high_price=2.71 + offset * 0.001,
                low_price=2.69 + offset * 0.001,
                close_price=2.705 + offset * 0.001,
                volume=500,
                timestamp=1_700_001_480.0 + offset,
                trade_count=10,
                strategy_codes=["polygon_30s"],
            )
        )

    open_intents = [intent for intent in intents if intent.payload.intent_type == "open"]
    assert len(open_intents) == 1
    assert open_intents[0].payload.symbol == "UGRO"


def test_tick_built_macd_30s_ignores_live_bar_packets() -> None:
    state = StrategyEngineState(
        settings=make_test_settings(
            strategy_macd_30s_enabled=True,
            strategy_macd_30s_live_aggregate_bars_enabled=False,
        ),
        now_provider=fixed_now,
    )
    bot = state.bots["macd_30s"]
    bot.set_watchlist(["UGRO"])
    bot.definition.trading_config.entry_intrabar_enabled = True
    state.seed_bars(
        "macd_30s",
        "UGRO",
        seed_trending_bars(count=49, start_timestamp=1_700_000_000.0, interval_secs=30),
    )

    builder = bot.builder_manager.get_builder("UGRO")
    assert builder is not None
    before_bars = [bar.copy() for bar in builder.get_bars_with_current_as_dicts()]

    intents = state.handle_live_bar(
        symbol="UGRO",
        interval_secs=30,
        open_price=2.90,
        high_price=2.95,
        low_price=2.85,
        close_price=2.92,
        volume=5_000,
        timestamp=1_700_001_500.0,
        trade_count=12,
    )

    after_bars = builder.get_bars_with_current_as_dicts()
    assert intents == []
    assert after_bars == before_bars


def test_polygon_tick_built_sparse_ticks_do_not_synthesize_gap_bars(monkeypatch) -> None:
    state = StrategyEngineState(
        settings=make_test_settings(
            strategy_macd_30s_enabled=False,
            strategy_polygon_30s_enabled=True,
            strategy_polygon_30s_live_aggregate_bars_enabled=False,
            strategy_polygon_30s_force_tick_built_mode=True,
        ),
        now_provider=lambda: datetime.fromtimestamp(1_700_001_900.0, UTC),
    )
    bot = state.bots["polygon_30s"]
    bot.set_watchlist(["UGRO"])
    state.seed_bars(
        "polygon_30s",
        "UGRO",
        seed_trending_bars(count=49, start_timestamp=1_700_000_000.0, interval_secs=30),
    )

    synthetic_quiet_bars: list[str] = []
    real_completed_bars: list[str] = []
    monkeypatch.setattr(
        bot,
        "_finalize_synthetic_quiet_completed_bar",
        lambda symbol: synthetic_quiet_bars.append(symbol),
    )
    monkeypatch.setattr(
        bot,
        "_evaluate_completed_bar",
        lambda symbol, *, completed_bar=None: real_completed_bars.append(symbol) or [],
    )

    for timestamp_ns in (
        1_700_001_470_000_000_000,
        1_700_001_650_000_000_000,
        1_700_001_830_000_000_000,
    ):
        state.handle_trade_tick(
            symbol="UGRO",
            price=2.80,
            size=100,
            timestamp_ns=timestamp_ns,
            strategy_codes=["polygon_30s"],
        )

    assert synthetic_quiet_bars == []
    assert real_completed_bars


def test_polygon_tick_built_persists_real_completed_bars_during_warmup() -> None:
    session_factory = build_test_session_factory()
    clock = {"now": datetime(2026, 5, 14, 12, 0, 0, tzinfo=UTC)}
    state = StrategyEngineState(
        settings=make_test_settings(
            dashboard_snapshot_persistence_enabled=False,
            strategy_history_persistence_enabled=True,
            strategy_macd_30s_enabled=False,
            strategy_polygon_30s_enabled=True,
            strategy_polygon_30s_live_aggregate_bars_enabled=False,
            strategy_polygon_30s_force_tick_built_mode=True,
        ),
        now_provider=lambda: clock["now"],
        session_factory=session_factory,
    )
    bot = state.bots["polygon_30s"]
    bot.set_watchlist(["TEST"])
    bot._ensure_history_seeded = lambda _symbol: None  # type: ignore[method-assign]

    timestamps = [
        datetime(2026, 5, 14, 12, 0, 0, tzinfo=UTC).timestamp(),
        datetime(2026, 5, 14, 12, 0, 30, tzinfo=UTC).timestamp(),
        datetime(2026, 5, 14, 12, 1, 0, tzinfo=UTC).timestamp(),
    ]

    for ts in timestamps:
        state.handle_trade_tick(
            symbol="TEST",
            price=2.5,
            size=100,
            timestamp_ns=int(ts * 1_000_000_000),
            strategy_codes=["polygon_30s"],
        )

    clock["now"] = datetime(2026, 5, 14, 12, 1, 33, tzinfo=UTC)
    _intents, completed_count = state.flush_completed_bars()

    assert completed_count == 1
    assert bot.recent_decisions
    assert bot.recent_decisions[0]["status"] == "blocked"
    assert bot.recent_decisions[0]["reason"] == f"warmup (3/{bot.required_history_bars()} bars)"

    with session_factory() as session:
        rows = list(
            session.scalars(
                select(StrategyBarHistory)
                .where(
                    StrategyBarHistory.strategy_code == "polygon_30s",
                    StrategyBarHistory.symbol == "TEST",
                    StrategyBarHistory.interval_secs == 30,
                )
                .order_by(StrategyBarHistory.bar_time.asc())
            )
        )

    assert [row.bar_time.replace(tzinfo=UTC) for row in rows] == [
        datetime(2026, 5, 14, 12, 0, 0, tzinfo=UTC),
        datetime(2026, 5, 14, 12, 0, 30, tzinfo=UTC),
        datetime(2026, 5, 14, 12, 1, 0, tzinfo=UTC),
    ]
    assert all(row.volume == 100 for row in rows)
    assert all(row.trade_count == 1 for row in rows)
    assert all(row.decision_status == "blocked" for row in rows)
    assert all(row.decision_reason.startswith("warmup (") for row in rows)


def test_polygon_env_drift_logs_warning_when_live_aggregate_bars_enabled(caplog) -> None:
    caplog.set_level("WARNING", logger="project_mai_tai.services.strategy_engine_app")
    StrategyEngineState(
        settings=make_test_settings(
            strategy_macd_30s_enabled=False,
            strategy_polygon_30s_enabled=True,
            strategy_polygon_30s_live_aggregate_bars_enabled=True,
        ),
        now_provider=fixed_now,
    )
    matching = [
        record
        for record in caplog.records
        if record.levelname == "WARNING"
        and "MAI_TAI_STRATEGY_POLYGON_30S_LIVE_AGGREGATE_BARS_ENABLED=true" in record.message
    ]
    assert matching, "expected env-drift WARNING when polygon aggregate-bar opt-in is set"


def test_polygon_env_drift_warning_silent_in_default_tick_built_mode(caplog) -> None:
    caplog.set_level("WARNING", logger="project_mai_tai.services.strategy_engine_app")
    StrategyEngineState(
        settings=make_test_settings(
            strategy_macd_30s_enabled=False,
            strategy_polygon_30s_enabled=True,
        ),
        now_provider=fixed_now,
    )
    assert not any(
        "MAI_TAI_STRATEGY_POLYGON_30S_LIVE_AGGREGATE_BARS_ENABLED" in record.message
        for record in caplog.records
    ), "tick-built default must not emit the env-drift WARNING"


def test_bot_runtime_prunes_symbol_state_when_symbol_is_dropped_from_bot_lifecycle() -> None:
    state = StrategyEngineState(now_provider=fixed_now)
    bot = state.bots["macd_30s"]
    bot.set_watchlist(["UGRO", "BFRG"])
    state.seed_bars(
        "macd_30s",
        "UGRO",
        seed_trending_bars(start_timestamp=1_700_000_000.0, interval_secs=30),
    )
    state.seed_bars(
        "macd_30s",
        "BFRG",
        seed_trending_bars(start_price=3.0, start_timestamp=1_700_100_000.0, interval_secs=30),
    )
    bot.last_indicators["UGRO"] = {"price": 2.5}
    bot.last_indicators["BFRG"] = {"price": 3.5}
    bot.latest_quotes["UGRO"] = {"ask": 2.5}
    bot.latest_quotes["BFRG"] = {"ask": 3.5}
    bot.entry_engine._recent_bars["UGRO"] = [{"price": 2.5, "high": 2.5, "volume": 1.0, "ema9": 2.4, "ema20": 2.3, "vwap": 2.4}]
    bot.entry_engine._recent_bars["BFRG"] = [{"price": 3.5, "high": 3.5, "volume": 1.0, "ema9": 3.4, "ema20": 3.3, "vwap": 3.4}]

    bot.set_watchlist(["UGRO"])
    bot.lifecycle_states.pop("BFRG", None)
    bot._sync_watchlist_from_lifecycle()
    bot._prune_runtime_state()

    assert "UGRO" in bot.builder_manager.get_all_tickers()
    assert "BFRG" not in bot.builder_manager.get_all_tickers()
    assert "UGRO" in bot.last_indicators
    assert "BFRG" not in bot.last_indicators
    assert "UGRO" in bot.latest_quotes
    assert "BFRG" not in bot.latest_quotes
    assert "UGRO" in bot.entry_engine._recent_bars
    assert "BFRG" not in bot.entry_engine._recent_bars


def test_bot_runtime_keeps_handoff_symbol_in_feed_when_lifecycle_wants_to_drop_it() -> None:
    state = StrategyEngineState(now_provider=fixed_now)
    state.restore_confirmed_runtime_view([{"ticker": "CANF"}])
    bot = state.bots["macd_30s"]
    retained = bot.lifecycle_states["CANF"]
    retained.state = "cooldown"
    retained.cooldown_started_at = fixed_now() - timedelta(minutes=31)
    retained.state_changed_at = fixed_now() - timedelta(minutes=31)
    retained.active_reference_5m_volume = 200_000.0

    bot._update_symbol_lifecycle(
        "CANF",
        metrics=FeedRetentionMetrics(
            price=1.80,
            ema20=2.00,
            vwap=2.05,
            rolling_5m_volume=10_000.0,
            rolling_5m_range_pct=0.4,
            bar_timestamp=1_700_000_000.0,
        ),
    )

    assert bot.lifecycle_states["CANF"].state == "cooldown"
    assert "CANF" in bot.watchlist
    assert "CANF" in bot.entry_blocked_symbols


def test_strategy_summary_includes_indicator_snapshots_for_1m_parity(monkeypatch) -> None:
    state = StrategyEngineState(
        settings=make_test_settings(strategy_macd_1m_enabled=True),
        now_provider=fixed_now,
    )
    bot = state.bots["macd_1m"]
    bot.set_watchlist(["UGRO"])
    state.seed_bars(
        "macd_1m",
        "UGRO",
        seed_trending_bars(start_timestamp=1_700_000_000.0, interval_secs=60),
    )
    monkeypatch.setattr(
        bot.indicator_engine,
        "calculate",
        lambda bars: {
            "price": 2.8,
            "ema9": 2.7,
            "ema20": 2.55,
            "macd": 0.08231,
            "signal": 0.07411,
            "histogram": 0.0082,
            "vwap": 2.61,
            "macd_above_signal": True,
            "price_above_vwap": True,
            "price_above_ema9": True,
            "price_above_ema20": True,
        },
    )
    monkeypatch.setattr(bot.entry_engine, "check_entry", lambda *args, **kwargs: None)

    state.handle_trade_tick(
        symbol="UGRO",
        price=2.8,
        size=200,
        timestamp_ns=1_700_003_000_000_000_000,
    )

    summary = state.summary()
    indicator_snapshots = summary["bots"]["macd_1m"]["indicator_snapshots"]
    assert indicator_snapshots
    assert indicator_snapshots[0]["symbol"] == "UGRO"
    assert indicator_snapshots[0]["interval_secs"] == 60
    assert indicator_snapshots[0]["macd_above_signal"] is True


def test_macd_1m_taapi_provider_is_enabled_by_setting() -> None:
    state = StrategyEngineState(
        settings=make_test_settings(
            taapi_secret="test-secret",
            massive_api_key="polygon-secret",
            strategy_macd_1m_enabled=True,
            strategy_tos_enabled=True,
            strategy_macd_1m_taapi_indicator_source_enabled=True,
        ),
        now_provider=fixed_now,
    )

    assert isinstance(state.bots["macd_1m"].indicator_overlay_provider, TaapiIndicatorProvider)
    assert state.bots["macd_30s"].indicator_overlay_provider is None
    assert state.bots["tos"].indicator_overlay_provider is None


def test_macd_30s_defaults_to_trade_tick_bars_without_massive_overlay() -> None:
    state = StrategyEngineState(
        settings=make_test_settings(
            massive_api_key="test-key",
        ),
        now_provider=fixed_now,
    )

    assert state.bots["macd_30s"].use_live_aggregate_bars is False
    assert state.bots["macd_30s"].indicator_overlay_provider is None


def test_macd_30s_probe_reclaim_and_retest_can_be_enabled_as_separate_bots() -> None:
    state = StrategyEngineState(
        settings=make_test_settings(
            massive_api_key="test-key",
            strategy_macd_30s_probe_enabled=True,
            strategy_macd_30s_reclaim_enabled=True,
            strategy_macd_30s_retest_enabled=True,
        ),
        now_provider=fixed_now,
    )

    assert "macd_30s_probe" in state.bots
    assert "macd_30s_reclaim" in state.bots
    assert "macd_30s_retest" in state.bots
    assert state.bots["macd_30s_probe"].definition.display_name == "MACD Bot 30S Probe"
    assert state.bots["macd_30s_reclaim"].definition.display_name == "MACD Bot 30S Reclaim"
    assert state.bots["macd_30s_retest"].definition.display_name == "MACD Bot 30S Retest"
    assert state.bots["macd_30s_probe"].definition.interval_secs == 30
    assert state.bots["macd_30s_reclaim"].definition.interval_secs == 30
    assert state.bots["macd_30s_retest"].definition.interval_secs == 30
    assert state.bots["macd_30s_probe"].use_live_aggregate_bars is False
    assert state.bots["macd_30s_reclaim"].use_live_aggregate_bars is False
    assert state.bots["macd_30s_retest"].use_live_aggregate_bars is False
    assert state.bots["macd_30s_probe"].indicator_overlay_provider is None
    assert state.bots["macd_30s_reclaim"].indicator_overlay_provider is None
    assert state.bots["macd_30s_retest"].indicator_overlay_provider is None


def test_macd_30s_core_can_be_disabled_while_reclaim_remains_enabled() -> None:
    state = StrategyEngineState(
        settings=make_test_settings(
            massive_api_key="test-key",
            strategy_macd_30s_enabled=False,
            strategy_macd_30s_reclaim_enabled=True,
        ),
        now_provider=fixed_now,
    )

    assert "macd_30s" not in state.bots
    assert "macd_30s_reclaim" in state.bots
    assert state.bots["macd_30s_reclaim"].definition.account_name == "paper:macd_30s_reclaim"


def test_strategy_state_can_enable_ai_shadow_catalyst_evaluator() -> None:
    state = StrategyEngineState(
        settings=make_test_settings(
            alpaca_macd_30s_api_key="alpaca-key",
            alpaca_macd_30s_secret_key="alpaca-secret",
            news_enabled=True,
            news_ai_shadow_enabled=True,
            news_ai_api_key="openai-key",
            news_ai_model="gpt-4.1-mini",
        ),
        now_provider=fixed_now,
    )

    assert state.catalyst_engine is not None
    assert state.catalyst_engine.ai_evaluator is not None
    assert state.catalyst_engine.promote_ai_result is False
    assert state.catalyst_engine.ai_evaluator.config.model == "gpt-4.1-mini"


def test_seed_bars_hydrates_pretrigger_recent_bar_memory() -> None:
    state = StrategyEngineState(
        settings=make_test_settings(
            strategy_macd_30s_reclaim_enabled=True,
        ),
        now_provider=fixed_now,
    )

    state.seed_bars(
        "macd_30s_reclaim",
        "UGRO",
        seed_trending_bars(
            count=60,
            start_timestamp=1_700_000_000.0,
            interval_secs=30,
        ),
    )

    recent = state.bots["macd_30s_reclaim"].entry_engine._recent_bars.get("UGRO", [])
    assert len(recent) >= 14


def test_30s_family_applies_common_and_variant_trading_overrides() -> None:
    state = StrategyEngineState(
        settings=make_test_settings(
            strategy_macd_30s_probe_enabled=True,
            strategy_macd_30s_reclaim_enabled=True,
            strategy_macd_30s_retest_enabled=True,
            strategy_macd_30s_common_config_overrides_json='{"pretrigger_entry_size_factor": 0.4}',
            strategy_macd_30s_probe_config_overrides_json='{"pretrigger_confirm_entry_size_factor": 0.8}',
            strategy_macd_30s_reclaim_config_overrides_json='{"pretrigger_reclaim_allow_current_bar_touch": false, "pretrigger_reclaim_touch_lookback_bars": 5, "pretrigger_reclaim_min_pullback_low_above_prespike_pct": 0.03, "pretrigger_reclaim_pullback_volume_max_spike_ratio": 0.5, "pretrigger_reclaim_min_held_spike_gain_ratio": 0.6, "pretrigger_fail_fast_on_macd_below_signal": false, "pretrigger_fail_fast_on_price_below_ema9": false, "pretrigger_reclaim_require_location": false, "pretrigger_reclaim_require_momentum": false, "pretrigger_reclaim_use_leg_retrace_gate": true, "pretrigger_reclaim_min_retrace_fraction_of_leg": 0.25, "pretrigger_reclaim_max_retrace_fraction_of_leg": 0.9, "pretrigger_reclaim_soft_min_close_pos_pct": 0.4, "pretrigger_reclaim_arm_break_lookahead_bars": 2}',
            strategy_macd_30s_retest_config_overrides_json='{"pretrigger_retest_min_breakout_pct": 0.006, "pretrigger_retest_arm_break_lookahead_bars": 2, "pretrigger_retest_require_dual_anchor": false}',
        ),
        now_provider=fixed_now,
    )

    assert state.bots["macd_30s"].definition.trading_config.pretrigger_entry_size_factor == 0.4
    assert state.bots["macd_30s_probe"].definition.trading_config.pretrigger_entry_size_factor == 0.4
    assert state.bots["macd_30s_probe"].definition.trading_config.pretrigger_confirm_entry_size_factor == 0.8
    assert state.bots["macd_30s_reclaim"].definition.trading_config.pretrigger_entry_size_factor == 0.4
    assert state.bots["macd_30s_retest"].definition.trading_config.pretrigger_entry_size_factor == 0.4
    assert state.bots["macd_30s_reclaim"].definition.trading_config.pretrigger_reclaim_allow_current_bar_touch is False
    assert state.bots["macd_30s_reclaim"].definition.trading_config.pretrigger_reclaim_touch_lookback_bars == 5
    assert state.bots["macd_30s_reclaim"].definition.trading_config.pretrigger_reclaim_min_pullback_low_above_prespike_pct == 0.03
    assert state.bots["macd_30s_reclaim"].definition.trading_config.pretrigger_reclaim_pullback_volume_max_spike_ratio == 0.5
    assert state.bots["macd_30s_reclaim"].definition.trading_config.pretrigger_reclaim_min_held_spike_gain_ratio == 0.6
    assert state.bots["macd_30s_reclaim"].definition.trading_config.pretrigger_fail_fast_on_macd_below_signal is False
    assert state.bots["macd_30s_reclaim"].definition.trading_config.pretrigger_fail_fast_on_price_below_ema9 is False
    assert state.bots["macd_30s_reclaim"].definition.trading_config.pretrigger_reclaim_require_location is False
    assert state.bots["macd_30s_reclaim"].definition.trading_config.pretrigger_reclaim_require_momentum is False
    assert state.bots["macd_30s_reclaim"].definition.trading_config.pretrigger_reclaim_use_leg_retrace_gate is True
    assert state.bots["macd_30s_reclaim"].definition.trading_config.pretrigger_reclaim_min_retrace_fraction_of_leg == 0.25
    assert state.bots["macd_30s_reclaim"].definition.trading_config.pretrigger_reclaim_max_retrace_fraction_of_leg == 0.9
    assert state.bots["macd_30s_reclaim"].definition.trading_config.pretrigger_reclaim_soft_min_close_pos_pct == 0.4
    assert state.bots["macd_30s_reclaim"].definition.trading_config.pretrigger_reclaim_arm_break_lookahead_bars == 2
    assert state.bots["macd_30s_retest"].definition.trading_config.pretrigger_retest_min_breakout_pct == 0.006
    assert state.bots["macd_30s_retest"].definition.trading_config.pretrigger_retest_arm_break_lookahead_bars == 2
    assert state.bots["macd_30s_retest"].definition.trading_config.pretrigger_retest_require_dual_anchor is False


def test_live_aggregate_30s_falls_back_to_trade_ticks_when_stream_is_missing(monkeypatch) -> None:
    state = StrategyEngineState(
        settings=make_test_settings(
            strategy_macd_30s_live_aggregate_bars_enabled=True,
            strategy_macd_30s_live_aggregate_stale_after_seconds=3,
        ),
        now_provider=fixed_now,
    )
    bot = state.bots["macd_30s"]
    bot.set_watchlist(["UGRO"])
    bot.definition.trading_config.entry_intrabar_enabled = True
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
            "score_details": "hist+ stK+ vwap+ vol+ macd+ emas+",
        },
    )

    intents = state.handle_trade_tick(
        symbol="UGRO",
        price=2.8,
        size=200,
        timestamp_ns=1_700_001_500_000_000_000,
    )

    assert bot.use_live_aggregate_bars is True
    assert [intent.payload.intent_type for intent in intents] == ["open"]


def test_live_aggregate_30s_still_emits_intrabar_open_from_trade_tick_when_stream_is_fresh(monkeypatch) -> None:
    state = StrategyEngineState(
        settings=make_test_settings(
            strategy_macd_30s_live_aggregate_bars_enabled=True,
            strategy_macd_30s_live_aggregate_stale_after_seconds=60,
        ),
        now_provider=fixed_now,
    )
    bot = state.bots["macd_30s"]
    bot.set_watchlist(["UGRO"])
    bot.definition.trading_config.entry_intrabar_enabled = True
    state.seed_bars(
        "macd_30s",
        "UGRO",
        seed_trending_bars(start_timestamp=1_700_000_000.0, interval_secs=30),
    )
    bot.latest_quotes["UGRO"] = {"bid": 2.79, "ask": 2.80}
    bot.handle_live_bar(
        symbol="UGRO",
        open_price=2.78,
        high_price=2.79,
        low_price=2.77,
        close_price=2.79,
        volume=2_000,
        timestamp=1_700_001_500.0,
        trade_count=10,
    )
    bot.pending_open_symbols.clear()

    captured: dict[str, float | int] = {}

    assert bot._should_fallback_to_trade_ticks("UGRO") is True

    def fake_calculate(bars):
        captured["last_bar_timestamp"] = float(bars[-1]["timestamp"])
        captured["last_bar_close"] = float(bars[-1]["close"])
        return {
            "price": float(bars[-1]["close"]),
            "bar_timestamp": float(bars[-1]["timestamp"]),
        }

    def fake_check_entry(symbol, indicators, bar_index, position_tracker):
        del position_tracker
        captured["bar_index"] = bar_index
        return {
            "action": "BUY",
            "ticker": symbol,
            "path": "P4_BURST",
            "price": indicators["price"],
            "score": 6,
            "score_details": "intrabar",
        }

    monkeypatch.setattr(bot.indicator_engine, "calculate", fake_calculate)
    monkeypatch.setattr(bot.entry_engine, "check_entry", fake_check_entry)
    monkeypatch.setattr(
        bot.entry_engine,
        "pop_last_decision",
        lambda _symbol: {"status": "signal", "reason": "P1_CROSS", "path": "P1_CROSS", "score": "6"},
    )

    intents = state.handle_trade_tick(
        symbol="UGRO",
        price=2.81,
        size=200,
        timestamp_ns=1_700_001_505_000_000_000,
    )

    assert bot.use_live_aggregate_bars is True
    assert [intent.payload.intent_type for intent in intents] == ["open"]
    assert captured["last_bar_timestamp"] == 1_700_001_480.0
    assert captured["last_bar_close"] == 2.81
    assert captured["bar_index"] == bot.builder_manager.get_builder("UGRO").get_bar_count() + 1


def test_live_aggregate_30s_prev_bar_intrabar_mode_blocks_non_p4_paths(monkeypatch) -> None:
    state = StrategyEngineState(
        settings=make_test_settings(
            strategy_macd_30s_live_aggregate_bars_enabled=True,
            strategy_macd_30s_live_aggregate_stale_after_seconds=60,
        ),
        now_provider=fixed_now,
    )
    bot = state.bots["macd_30s"]
    bot.set_watchlist(["UGRO"])
    bot.definition.trading_config.entry_intrabar_enabled = False
    bot.definition.trading_config.p4_prev_bar_entry_enabled = True
    state.seed_bars(
        "macd_30s",
        "UGRO",
        seed_trending_bars(start_timestamp=1_700_000_000.0, interval_secs=30),
    )
    bot.latest_quotes["UGRO"] = {"bid": 2.79, "ask": 2.80}
    bot.handle_live_bar(
        symbol="UGRO",
        open_price=2.78,
        high_price=2.79,
        low_price=2.77,
        close_price=2.79,
        volume=2_000,
        timestamp=1_700_001_500.0,
        trade_count=10,
    )
    bot.pending_open_symbols.clear()

    monkeypatch.setattr(
        bot.indicator_engine,
        "calculate",
        lambda bars: {
            "price": float(bars[-1]["close"]),
            "bar_timestamp": float(bars[-1]["timestamp"]),
        },
    )
    monkeypatch.setattr(
        bot.entry_engine,
        "check_entry",
        lambda symbol, indicators, bar_index, position_tracker: {
            "action": "BUY",
            "ticker": symbol,
            "path": "P1_CROSS",
            "price": indicators["price"],
            "score": 6,
            "score_details": "intrabar",
        },
    )
    monkeypatch.setattr(
        bot.entry_engine,
        "pop_last_decision",
        lambda _symbol: {"status": "signal", "reason": "P1_CROSS", "path": "P1_CROSS", "score": "6"},
    )

    intents = state.handle_trade_tick(
        symbol="UGRO",
        price=2.81,
        size=200,
        timestamp_ns=1_700_001_505_000_000_000,
    )

    assert intents == []


def test_live_aggregate_30s_falls_back_to_trade_ticks_when_bar_progress_stalls(monkeypatch) -> None:
    current = datetime.fromtimestamp(1_700_001_505.0, UTC)

    def now_provider() -> datetime:
        return current

    state = StrategyEngineState(
        settings=make_test_settings(
            strategy_macd_30s_live_aggregate_bars_enabled=True,
            strategy_macd_30s_live_aggregate_stale_after_seconds=60,
        ),
        now_provider=now_provider,
    )
    bot = state.bots["macd_30s"]
    bot.set_watchlist(["UGRO"])
    bot.definition.trading_config.entry_intrabar_enabled = True
    state.seed_bars(
        "macd_30s",
        "UGRO",
        seed_trending_bars(start_timestamp=1_700_000_000.0, interval_secs=30),
    )
    bot.latest_quotes["UGRO"] = {"bid": 2.79, "ask": 2.80}
    bot.handle_live_bar(
        symbol="UGRO",
        open_price=2.78,
        high_price=2.79,
        low_price=2.77,
        close_price=2.79,
        volume=2_000,
        timestamp=1_700_001_470.0,
        trade_count=10,
    )
    current = datetime.fromtimestamp(1_700_001_575.0, UTC)

    captured: dict[str, float | int] = {}

    def fake_calculate(bars):
        captured["last_bar_timestamp"] = float(bars[-1]["timestamp"])
        captured["last_bar_close"] = float(bars[-1]["close"])
        return {
            "price": float(bars[-1]["close"]),
            "bar_timestamp": float(bars[-1]["timestamp"]),
        }

    def fake_check_entry(symbol, indicators, bar_index, position_tracker):
        del position_tracker
        captured["bar_index"] = bar_index
        return {
            "action": "BUY",
            "ticker": symbol,
            "path": "P4_BURST",
            "price": indicators["price"],
            "score": 6,
            "score_details": "intrabar",
        }

    monkeypatch.setattr(bot.indicator_engine, "calculate", fake_calculate)
    monkeypatch.setattr(bot.entry_engine, "check_entry", fake_check_entry)
    monkeypatch.setattr(
        bot.entry_engine,
        "pop_last_decision",
        lambda _symbol: {"status": "signal", "reason": "P1_CROSS", "path": "P1_CROSS", "score": "6"},
    )

    assert bot._should_fallback_to_trade_ticks("UGRO") is True
    intents = state.handle_trade_tick(
        symbol="UGRO",
        price=2.81,
        size=200,
        timestamp_ns=1_700_001_575_000_000_000,
    )

    assert bot.use_live_aggregate_bars is True
    assert [intent.payload.intent_type for intent in intents] == ["open"]
    assert bot.builder_manager.get_builder("UGRO").get_bars_with_current_as_dicts()[-1]["timestamp"] == 1_700_001_570.0
    assert bot.builder_manager.get_builder("UGRO").get_bars_with_current_as_dicts()[-1]["close"] == 2.81


def test_reclaim_runtime_checks_pretrigger_logic_while_position_is_open(monkeypatch) -> None:
    state = StrategyEngineState(
        settings=make_test_settings(
            strategy_macd_30s_reclaim_enabled=True,
            strategy_macd_30s_live_aggregate_bars_enabled=False,
        ),
        now_provider=fixed_now,
    )
    bot = state.bots["macd_30s_reclaim"]
    bot.set_watchlist(["UGRO"])
    state.seed_bars(
        "macd_30s_reclaim",
        "UGRO",
        seed_trending_bars(start_timestamp=1_700_000_000.0, interval_secs=30),
    )
    bot.positions.open_position("UGRO", 2.33, quantity=25, path="PRETRIGGER_RECLAIM")
    monkeypatch.setattr(bot.indicator_engine, "calculate", lambda bars: {"price": 2.25})
    monkeypatch.setattr(
        bot.entry_engine,
        "check_entry",
        lambda symbol, indicators, bar_index, position_tracker: {
            "action": "SELL",
            "ticker": symbol,
            "reason": "PRETRIGGER_FAIL_FAST",
            "price": 2.25,
        },
    )
    monkeypatch.setattr(bot.exit_engine, "check_exit", lambda position, indicators: None)

    intents = bot._evaluate_completed_bar("UGRO")

    assert [intent.payload.intent_type for intent in intents] == ["close"]
    assert intents[0].payload.strategy_code == "macd_30s_reclaim"


def test_macd_1m_taapi_provider_requires_polygon_secret() -> None:
    state = StrategyEngineState(
        settings=make_test_settings(
            taapi_secret="test-secret",
            strategy_macd_1m_enabled=True,
            strategy_macd_1m_taapi_indicator_source_enabled=True,
        ),
        now_provider=fixed_now,
    )

    assert state.bots["macd_1m"].indicator_overlay_provider is None


def test_macd_1m_massive_provider_remains_available_as_fallback() -> None:
    state = StrategyEngineState(
        settings=make_test_settings(
            massive_api_key="test-key",
            strategy_macd_1m_enabled=True,
            strategy_macd_1m_massive_indicator_overlay_enabled=True,
        ),
        now_provider=fixed_now,
    )

    assert isinstance(state.bots["macd_1m"].indicator_overlay_provider, MassiveIndicatorProvider)


def test_strategy_summary_includes_taapi_indicator_fields_for_1m(monkeypatch) -> None:
    now_box = {"value": fixed_now()}
    state = StrategyEngineState(
        settings=make_test_settings(strategy_macd_1m_enabled=True),
        now_provider=lambda: now_box["value"],
    )
    bot = state.bots["macd_1m"]
    bot.set_watchlist(["UGRO"])
    state.seed_bars(
        "macd_1m",
        "UGRO",
        seed_trending_bars(start_timestamp=1_700_000_000.0, interval_secs=60),
    )

    monkeypatch.setattr(
        bot.indicator_engine,
        "calculate",
        lambda bars: {
            "price": 2.8,
            "price_prev": 2.74,
            "ema9": 2.7,
            "ema20": 2.55,
            "macd": 0.08231,
            "macd_prev": 0.07011,
            "macd_prev2": 0.06011,
            "signal": 0.07411,
            "signal_prev": 0.06911,
            "signal_prev2": 0.05811,
            "histogram": 0.0082,
            "histogram_prev": 0.001,
            "stoch_k": 61.0,
            "stoch_k_prev": 58.0,
            "stoch_k_prev2": 54.0,
            "stoch_d": 57.0,
            "stoch_d_prev": 54.0,
            "vwap": 2.61,
            "extended_vwap": 2.59,
            "macd_above_signal": True,
            "price_above_vwap": True,
            "price_above_extended_vwap": True,
            "price_above_ema9": True,
            "price_above_ema20": True,
            "price_above_both_emas": True,
            "price_cross_above_vwap": True,
            "price_cross_above_extended_vwap": True,
            "macd_was_below_3bars": False,
        },
    )

    class FakeProvider:
        def fetch_minute_indicators(self, symbol, *, bar_time, indicator_config):
            del symbol, bar_time, indicator_config
            return {
                "provider_source": "taapi",
                "provider_status": "ready",
                "provider_last_bar_at": "2026-03-28T10:00:00+00:00",
                "provider_macd": 0.07231,
                "provider_macd_prev": 0.06231,
                "provider_macd_prev2": 0.05231,
                "provider_macd_prev3": 0.04231,
                "provider_signal": 0.06411,
                "provider_signal_prev": 0.06111,
                "provider_signal_prev2": 0.05111,
                "provider_signal_prev3": 0.04111,
                "provider_histogram": 0.00820,
                "provider_histogram_prev": 0.004,
                "provider_ema9": 2.69,
                "provider_ema20": 2.54,
                "provider_stoch_k": 63.0,
                "provider_stoch_k_prev": 59.0,
                "provider_stoch_k_prev2": 55.0,
                "provider_stoch_d": 58.0,
                "provider_stoch_d_prev": 55.0,
                "provider_vwap": 2.6,
                "provider_vwap_prev": 2.58,
                "provider_supported_inputs": list(TaapiIndicatorProvider.SUPPORTED_INPUTS),
                "provider_missing_inputs": list(TaapiIndicatorProvider.MISSING_INPUTS),
            }

    bot.indicator_overlay_provider = FakeProvider()
    monkeypatch.setattr(bot.entry_engine, "check_entry", lambda *args, **kwargs: None)

    state.handle_trade_tick(
        symbol="UGRO",
        price=2.8,
        size=200,
        timestamp_ns=1_700_003_000_000_000_000,
    )
    now_box["value"] = fixed_now() + timedelta(seconds=61)
    state.flush_completed_bars()

    summary = state.summary()
    indicator_snapshots = summary["bots"]["macd_1m"]["indicator_snapshots"]
    assert indicator_snapshots
    assert indicator_snapshots[0]["provider_source"] == "taapi"
    assert indicator_snapshots[0]["provider_status"] == "ready"
    assert indicator_snapshots[0]["provider_macd"] == pytest.approx(0.07231)
    assert indicator_snapshots[0]["provider_ema20"] == pytest.approx(2.54)
    assert indicator_snapshots[0]["provider_macd_diff"] == pytest.approx(0.01)
    assert indicator_snapshots[0]["provider_stoch_k"] == pytest.approx(63.0)
    assert indicator_snapshots[0]["provider_vwap"] == pytest.approx(2.6)
    assert indicator_snapshots[0]["provider_stoch_k_diff"] == pytest.approx(-2.0)
    assert indicator_snapshots[0]["provider_vwap_diff"] == pytest.approx(0.01)
    assert indicator_snapshots[0]["provider_missing_inputs"] == ["extended_vwap"]


def test_strategy_summary_includes_massive_aggregate_fields_for_30s(monkeypatch) -> None:
    event_start = datetime(2026, 3, 28, 14, 0, tzinfo=UTC)
    now_box = {"value": event_start}
    state = StrategyEngineState(
        settings=make_test_settings(
            massive_api_key="test-key",
            strategy_macd_30s_live_aggregate_bars_enabled=True,
            strategy_macd_30s_tick_bar_close_grace_seconds=0,
        ),
        now_provider=lambda: now_box["value"],
    )
    bot = state.bots["macd_30s"]
    bot.set_watchlist(["UGRO"])
    state.seed_bars(
        "macd_30s",
        "UGRO",
        seed_trending_bars(start_timestamp=1_700_000_000.0, interval_secs=30),
    )

    monkeypatch.setattr(
        bot.indicator_engine,
        "calculate",
        lambda bars: {
            "price": 2.8,
            "ema9": 2.7,
            "ema20": 2.55,
            "macd": 0.08231,
            "signal": 0.07411,
            "histogram": 0.0082,
            "vwap": 2.61,
            "bar_volume": 22200,
            "macd_above_signal": True,
            "price_above_vwap": True,
            "price_above_ema9": True,
            "price_above_ema20": True,
        },
    )

    class FakeProvider:
        SOURCE = "massive"
        SUPPORTED_INPUTS = ("open", "high", "low", "close", "volume", "vwap")
        MISSING_INPUTS = ("macd", "signal", "histogram", "ema9", "ema20", "stoch_k", "stoch_d", "extended_vwap")

        def fetch_aggregate_overlay(self, symbol, *, bar_time, interval_secs):
            del symbol, bar_time, interval_secs
            return {
                "provider_source": "massive",
                "provider_status": "ready",
                "provider_interval_secs": 30,
                "provider_last_bar_at": "2026-03-28T10:00:00+00:00",
                "provider_open": 2.71,
                "provider_high": 2.83,
                "provider_low": 2.68,
                "provider_close": 2.48,
                "provider_volume": 22000,
                "provider_vwap": 2.60,
                "provider_supported_inputs": list(self.SUPPORTED_INPUTS),
                "provider_missing_inputs": list(self.MISSING_INPUTS),
            }

    bot.indicator_overlay_provider = FakeProvider()
    monkeypatch.setattr(bot.entry_engine, "check_entry", lambda *args, **kwargs: None)

    state.handle_live_bar(
        symbol="UGRO",
        interval_secs=1,
        open_price=2.79,
        high_price=2.80,
        low_price=2.78,
        close_price=2.80,
        volume=200,
        timestamp=event_start.timestamp(),
        coverage_started_at=(event_start.timestamp() // 30) * 30,
    )
    now_box["value"] = event_start + timedelta(seconds=31)
    state.flush_completed_bars()

    summary = state.summary()
    indicator_snapshots = summary["bots"]["macd_30s"]["indicator_snapshots"]
    assert indicator_snapshots
    assert indicator_snapshots[0]["provider_source"] == "massive"
    assert indicator_snapshots[0]["provider_status"] == "ready"
    assert indicator_snapshots[0]["provider_close"] == pytest.approx(2.48)
    assert indicator_snapshots[0]["provider_volume"] == pytest.approx(22000)
    assert indicator_snapshots[0]["provider_close_diff"] == pytest.approx(0.32)
    assert indicator_snapshots[0]["provider_vwap_diff"] == pytest.approx(0.01)


def test_massive_overlay_does_not_change_30s_trading_inputs(monkeypatch) -> None:
    event_start = datetime(2026, 3, 28, 14, 0, tzinfo=UTC)
    now_box = {"value": event_start}
    state = StrategyEngineState(
        settings=make_test_settings(
            massive_api_key="test-key",
            strategy_macd_30s_live_aggregate_bars_enabled=True,
            strategy_macd_30s_tick_bar_close_grace_seconds=0,
        ),
        now_provider=lambda: now_box["value"],
    )
    bot = state.bots["macd_30s"]
    bot.set_watchlist(["UGRO"])
    state.seed_bars(
        "macd_30s",
        "UGRO",
        seed_trending_bars(start_timestamp=1_700_000_000.0, interval_secs=30),
    )

    monkeypatch.setattr(
        bot.indicator_engine,
        "calculate",
        lambda bars: {
            "price": 2.8,
            "ema9": 2.7,
            "ema20": 2.55,
            "macd": 0.08231,
            "signal": 0.07411,
            "histogram": 0.0082,
            "vwap": 2.61,
            "bar_volume": 22200,
            "macd_above_signal": True,
            "price_above_vwap": True,
            "price_above_ema9": True,
            "price_above_ema20": True,
        },
    )

    class FakeProvider:
        SOURCE = "massive"
        SUPPORTED_INPUTS = ("open", "high", "low", "close", "volume", "vwap")
        MISSING_INPUTS = ("macd", "signal", "histogram", "ema9", "ema20", "stoch_k", "stoch_d", "extended_vwap")

        def fetch_aggregate_overlay(self, symbol, *, bar_time, interval_secs):
            del symbol, bar_time, interval_secs
            return {
                "provider_source": "massive",
                "provider_status": "ready",
                "provider_close": 9.99,
                "provider_vwap": 9.88,
                "provider_volume": 999999,
                "provider_supported_inputs": list(self.SUPPORTED_INPUTS),
                "provider_missing_inputs": list(self.MISSING_INPUTS),
            }

    captured: dict[str, object] = {}

    def fake_check_entry(symbol, indicators, bar_count, runtime):
        del symbol, bar_count, runtime
        captured.update(indicators)
        return None

    bot.indicator_overlay_provider = FakeProvider()
    monkeypatch.setattr(bot.entry_engine, "check_entry", fake_check_entry)

    state.handle_live_bar(
        symbol="UGRO",
        interval_secs=1,
        open_price=2.79,
        high_price=2.80,
        low_price=2.78,
        close_price=2.80,
        volume=200,
        timestamp=event_start.timestamp(),
        coverage_started_at=(event_start.timestamp() // 30) * 30,
    )
    now_box["value"] = event_start + timedelta(seconds=31)
    state.flush_completed_bars()

    assert captured["price"] == pytest.approx(2.8)
    assert captured["vwap"] == pytest.approx(2.61)
    assert captured["macd"] == pytest.approx(0.08231)
    assert captured["provider_close"] == pytest.approx(9.99)
    assert captured["provider_vwap"] == pytest.approx(9.88)
    assert captured["provider_status"] == "ready"


def test_taapi_source_changes_1m_trading_inputs(monkeypatch) -> None:
    now_box = {"value": fixed_now()}
    state = StrategyEngineState(
        settings=make_test_settings(strategy_macd_1m_enabled=True),
        now_provider=lambda: now_box["value"],
    )
    bot = state.bots["macd_1m"]
    bot.set_watchlist(["UGRO"])
    state.seed_bars(
        "macd_1m",
        "UGRO",
        seed_trending_bars(start_timestamp=1_700_000_000.0, interval_secs=60),
    )

    monkeypatch.setattr(
        bot.indicator_engine,
        "calculate",
        lambda bars: {
            "price": 2.8,
            "price_prev": 2.7,
            "ema9": 2.7,
            "ema20": 2.55,
            "macd": 0.08231,
            "macd_prev": 0.07011,
            "macd_prev2": 0.06011,
            "signal": 0.07411,
            "signal_prev": 0.06911,
            "signal_prev2": 0.05811,
            "histogram": 0.0082,
            "histogram_prev": 0.001,
            "stoch_k": 61.0,
            "stoch_k_prev": 58.0,
            "stoch_k_prev2": 54.0,
            "stoch_d": 57.0,
            "stoch_d_prev": 54.0,
            "vwap": 2.61,
            "extended_vwap": 2.59,
            "macd_above_signal": True,
            "price_above_vwap": True,
            "price_above_extended_vwap": True,
            "price_above_ema9": True,
            "price_above_ema20": True,
            "price_above_both_emas": True,
            "price_cross_above_vwap": True,
            "price_cross_above_extended_vwap": True,
            "macd_was_below_3bars": False,
        },
    )

    class FakeProvider:
        def fetch_minute_indicators(self, symbol, *, bar_time, indicator_config):
            del symbol, bar_time, indicator_config
            return {
                "provider_source": "taapi",
                "provider_status": "ready",
                "provider_macd": -9.0,
                "provider_macd_prev": -10.0,
                "provider_macd_prev2": -11.0,
                "provider_macd_prev3": -12.0,
                "provider_signal": -8.0,
                "provider_signal_prev": -9.0,
                "provider_signal_prev2": -10.0,
                "provider_signal_prev3": -11.0,
                "provider_histogram": -1.0,
                "provider_histogram_prev": -2.0,
                "provider_ema9": 99.0,
                "provider_ema20": 88.0,
                "provider_stoch_k": 11.0,
                "provider_stoch_k_prev": 10.0,
                "provider_stoch_k_prev2": 9.0,
                "provider_stoch_d": 10.0,
                "provider_stoch_d_prev": 9.0,
                "provider_vwap": 77.0,
                "provider_vwap_prev": 76.0,
            }

    captured: dict[str, object] = {}

    def fake_check_entry(symbol, indicators, bar_index, runtime):
        del symbol, bar_index, runtime
        captured.update(indicators)
        return None

    bot.indicator_overlay_provider = FakeProvider()
    monkeypatch.setattr(bot.entry_engine, "check_entry", fake_check_entry)

    state.handle_trade_tick(
        symbol="UGRO",
        price=2.8,
        size=200,
        timestamp_ns=1_700_003_000_000_000_000,
    )
    now_box["value"] = fixed_now() + timedelta(seconds=61)
    state.flush_completed_bars()

    assert captured["macd"] == pytest.approx(-9.0)
    assert captured["signal"] == pytest.approx(-8.0)
    assert captured["ema9"] == pytest.approx(99.0)
    assert captured["stoch_k"] == pytest.approx(11.0)
    assert captured["vwap"] == pytest.approx(77.0)
    assert captured["extended_vwap"] == pytest.approx(2.59)
    assert captured["provider_macd"] == pytest.approx(-9.0)
    assert captured["provider_ema9"] == pytest.approx(99.0)


@pytest.mark.asyncio
async def test_order_event_fill_opens_position_and_clears_pending_state() -> None:
    redis = FakeRedis()
    service = StrategyEngineService(
        settings=make_test_settings(redis_stream_prefix="test", dashboard_snapshot_persistence_enabled=False),
        redis_client=redis,
    )
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
    assert any(stream == "test:strategy-state" for stream, _payload in redis.entries)


@pytest.mark.asyncio
async def test_order_event_fill_uses_incremental_quantity_for_cumulative_reports() -> None:
    redis = FakeRedis()
    service = StrategyEngineService(
        settings=make_test_settings(redis_stream_prefix="test", dashboard_snapshot_persistence_enabled=False),
        redis_client=redis,
    )
    bot = service.state.bots["macd_30s"]
    bot.pending_open_symbols.add("ELAB")

    partial_fill = OrderEventEvent(
        source_service="oms-risk",
        payload=OrderEventPayload(
            strategy_code="macd_30s",
            broker_account_name="paper:macd_30s",
            client_order_id="macd_30s-ELAB-open-cumulative",
            broker_order_id="broker-order-1",
            broker_fill_id="fill-1",
            symbol="ELAB",
            side="buy",
            intent_type="open",
            status="partially_filled",
            quantity=Decimal("100"),
            filled_quantity=Decimal("19"),
            fill_price=Decimal("3.95"),
            reason="ENTRY_P1_MACD_CROSS",
            metadata={"path": "P1_MACD_CROSS", "reference_price": "3.95"},
        ),
    )
    final_fill = OrderEventEvent(
        source_service="oms-risk",
        payload=OrderEventPayload(
            strategy_code="macd_30s",
            broker_account_name="paper:macd_30s",
            client_order_id="macd_30s-ELAB-open-cumulative",
            broker_order_id="broker-order-1",
            broker_fill_id="fill-2",
            symbol="ELAB",
            side="buy",
            intent_type="open",
            status="filled",
            quantity=Decimal("100"),
            filled_quantity=Decimal("100"),
            fill_price=Decimal("3.95"),
            reason="ENTRY_P1_MACD_CROSS",
            metadata={"path": "P1_MACD_CROSS", "reference_price": "3.95"},
        ),
    )

    await service._handle_stream_message("test:order-events", {"data": partial_fill.model_dump_json()})
    await service._handle_stream_message("test:order-events", {"data": final_fill.model_dump_json()})

    position = bot.positions.get_position("ELAB")
    assert position is not None
    assert position.quantity == 100
    assert position.original_quantity == 100


@pytest.mark.asyncio
async def test_historical_bars_hydrate_matching_strategy_intervals() -> None:
    redis = FakeRedis()
    service = StrategyEngineService(
        settings=make_test_settings(redis_stream_prefix="test", dashboard_snapshot_persistence_enabled=False),
        redis_client=redis,
    )

    historical_30s = HistoricalBarsEvent(
        source_service="market-data-gateway",
        payload=HistoricalBarsPayload(
            symbol="UGRO",
            interval_secs=30,
            bars=[
                HistoricalBarPayload(
                    open=Decimal("2.00"),
                    high=Decimal("2.10"),
                    low=Decimal("1.99"),
                    close=Decimal("2.05"),
                    volume=20_000,
                    timestamp=1_700_000_000.0,
                ),
                HistoricalBarPayload(
                    open=Decimal("2.05"),
                    high=Decimal("2.15"),
                    low=Decimal("2.04"),
                    close=Decimal("2.12"),
                    volume=22_000,
                    timestamp=1_700_000_030.0,
                ),
            ],
        ),
    )
    historical_runner = HistoricalBarsEvent(
        source_service="market-data-gateway",
        payload=HistoricalBarsPayload(
            symbol="UGRO",
            interval_secs=60,
            bars=[
                HistoricalBarPayload(
                    open=Decimal("2.00"),
                    high=Decimal("2.20"),
                    low=Decimal("1.95"),
                    close=Decimal("2.15"),
                    volume=80_000,
                    timestamp=1_700_000_000.0,
                ),
                HistoricalBarPayload(
                    open=Decimal("2.15"),
                    high=Decimal("2.25"),
                    low=Decimal("2.10"),
                    close=Decimal("2.22"),
                    volume=85_000,
                    timestamp=1_700_000_060.0,
                ),
            ],
        ),
    )

    await service._handle_stream_message("test:market-data", {"data": historical_30s.model_dump_json()})
    await service._handle_stream_message("test:market-data", {"data": historical_runner.model_dump_json()})

    assert len(service.state.bots["macd_30s"].builder_manager.get_bars("UGRO")) == 1
    assert len(service.state.bots["macd_1m"].builder_manager.get_bars("UGRO")) == 1
    assert len(service.state.bots["tos"].builder_manager.get_bars("UGRO")) == 1
    assert len(service.state.bots["runner"].builder_manager.get_bars("UGRO")) == 2


@pytest.mark.asyncio
async def test_snapshot_batch_history_prefill_restores_alert_warmup() -> None:
    redis = FakeRedis()
    service = StrategyEngineService(
        settings=make_test_settings(
            redis_stream_prefix="test",
            dashboard_snapshot_persistence_enabled=False,
            market_data_snapshot_interval_seconds=30,
        ),
        redis_client=redis,
    )

    snapshot_stream = "test:snapshot-batches"
    for index in range(20):
        event = SnapshotBatchEvent(
            source_service="market-data-gateway",
            payload={
                "snapshots": [
                    make_snapshot_payload(
                        symbol="UGRO",
                        price=2.40 + index * 0.01,
                        volume=900_000 + index * 10_000,
                    )
                ],
                "reference_data": [
                    {
                        "symbol": "UGRO",
                        "shares_outstanding": 50_000,
                        "avg_daily_volume": "390000",
                    }
                ],
            },
        )
        redis.stream_entries.setdefault(snapshot_stream, []).insert(
            0,
            (f"{index + 1}-0", {"data": event.model_dump_json()}),
        )

    await service._prefill_alert_history_from_snapshot_batches()

    warmup = service.state.alert_warmup
    assert warmup["history_cycles"] == 20
    assert warmup["squeeze_5min_ready"] is True
    assert warmup["squeeze_10min_ready"] is True
    assert warmup["fully_ready"] is True
    assert service.state.recent_alerts
    assert service.state.recent_alerts[-1]["ticker"] == "UGRO"
    assert service.state.recent_alerts[-1]["type"] == "VOLUME_SPIKE"
    assert service.state._first_seen_by_ticker["UGRO"]


@pytest.mark.asyncio
async def test_subscription_sync_replays_recent_historical_bars_for_active_symbols() -> None:
    redis = FakeRedis()
    service = StrategyEngineService(
        settings=make_test_settings(redis_stream_prefix="test", dashboard_snapshot_persistence_enabled=False),
        redis_client=redis,
    )
    service.state.bots["macd_1m"].set_watchlist(["UGRO"])
    service.state.bots["tos"].set_watchlist(["UGRO"])
    service.state.bots["runner"].set_watchlist(["UGRO"])

    historical_30s = HistoricalBarsEvent(
        source_service="market-data-gateway",
        payload=HistoricalBarsPayload(
            symbol="UGRO",
            interval_secs=30,
            bars=[
                HistoricalBarPayload(
                    open=Decimal("2.00"),
                    high=Decimal("2.10"),
                    low=Decimal("1.99"),
                    close=Decimal("2.05"),
                    volume=20_000,
                    timestamp=1_700_000_000.0,
                ),
                HistoricalBarPayload(
                    open=Decimal("2.05"),
                    high=Decimal("2.15"),
                    low=Decimal("2.04"),
                    close=Decimal("2.12"),
                    volume=22_000,
                    timestamp=1_700_000_030.0,
                ),
            ],
        ),
    )
    historical_60s = HistoricalBarsEvent(
        source_service="market-data-gateway",
        payload=HistoricalBarsPayload(
            symbol="UGRO",
            interval_secs=60,
            bars=[
                HistoricalBarPayload(
                    open=Decimal("2.00"),
                    high=Decimal("2.20"),
                    low=Decimal("1.95"),
                    close=Decimal("2.15"),
                    volume=80_000,
                    timestamp=1_700_000_000.0,
                ),
                HistoricalBarPayload(
                    open=Decimal("2.15"),
                    high=Decimal("2.25"),
                    low=Decimal("2.10"),
                    close=Decimal("2.22"),
                    volume=85_000,
                    timestamp=1_700_000_060.0,
                ),
            ],
        ),
    )
    redis.stream_entries.setdefault("test:market-data", []).extend(
        [
            ("2-0", {"data": historical_30s.model_dump_json()}),
            ("3-0", {"data": historical_60s.model_dump_json()}),
        ]
    )

    await service._sync_market_data_subscriptions(["UGRO"])

    assert service.state.bots["macd_30s"].builder_manager.get_bars("UGRO") == []
    assert len(service.state.bots["macd_1m"].builder_manager.get_bars("UGRO")) == 1
    assert len(service.state.bots["tos"].builder_manager.get_bars("UGRO")) == 1
    assert len(service.state.bots["runner"].builder_manager.get_bars("UGRO")) == 2


@pytest.mark.asyncio
async def test_subscription_sync_persists_replayed_polygon_historical_bars() -> None:
    redis = FakeRedis()
    session_factory = build_test_session_factory()
    service = StrategyEngineService(
        settings=make_test_settings(
            redis_stream_prefix="test",
            dashboard_snapshot_persistence_enabled=False,
            strategy_history_persistence_enabled=True,
            strategy_polygon_30s_enabled=True,
            strategy_polygon_30s_live_aggregate_bars_enabled=True,
        ),
        redis_client=redis,
        session_factory=session_factory,
    )
    service.state.bots["polygon_30s"].set_watchlist(["UGRO"])

    historical_30s = HistoricalBarsEvent(
        source_service="market-data-gateway",
        payload=HistoricalBarsPayload(
            symbol="UGRO",
            interval_secs=30,
            bars=[
                HistoricalBarPayload(
                    open=Decimal("2.00"),
                    high=Decimal("2.10"),
                    low=Decimal("1.99"),
                    close=Decimal("2.05"),
                    volume=20_000,
                    timestamp=1_700_000_000.0,
                    trade_count=5,
                ),
                HistoricalBarPayload(
                    open=Decimal("2.05"),
                    high=Decimal("2.15"),
                    low=Decimal("2.04"),
                    close=Decimal("2.12"),
                    volume=22_000,
                    timestamp=1_700_000_030.0,
                    trade_count=6,
                ),
            ],
        ),
    )
    redis.stream_entries.setdefault("test:market-data", []).extend(
        [("2-0", {"data": historical_30s.model_dump_json()})]
    )

    await service._sync_market_data_subscriptions(["UGRO"])

    with session_factory() as session:
        records = list(
            session.scalars(
                select(StrategyBarHistory)
                .where(
                    StrategyBarHistory.strategy_code == "polygon_30s",
                    StrategyBarHistory.symbol == "UGRO",
                    StrategyBarHistory.interval_secs == 30,
                )
                .order_by(StrategyBarHistory.bar_time.asc())
            )
        )

    assert len(records) == 2
    assert records[0].bar_time.replace(tzinfo=UTC) == datetime.fromtimestamp(1_700_000_000.0, UTC)
    assert records[0].trade_count == 5
    assert records[1].bar_time.replace(tzinfo=UTC) == datetime.fromtimestamp(1_700_000_030.0, UTC)
    assert records[1].trade_count == 6


def test_polygon_late_live_second_revises_persisted_closed_bar_without_redecision() -> None:
    session_factory = build_test_session_factory()
    clock = {"now": datetime(2026, 4, 23, 15, 27, 0, tzinfo=UTC)}
    state = StrategyEngineState(
        settings=make_test_settings(
            dashboard_snapshot_persistence_enabled=False,
            strategy_history_persistence_enabled=True,
            strategy_polygon_30s_enabled=True,
            strategy_polygon_30s_live_aggregate_bars_enabled=True,
        ),
        now_provider=lambda: clock["now"],
        session_factory=session_factory,
    )
    polygon_bot = state.bots["polygon_30s"]
    polygon_bot.set_watchlist(["CTNT"])
    state.seed_bars(
        "polygon_30s",
        "CTNT",
        seed_trending_bars(
            count=55,
            start_timestamp=datetime(2026, 4, 23, 14, 59, 0, tzinfo=UTC).timestamp(),
            interval_secs=30,
        ),
    )

    observed_decision_bars: list[float] = []

    def fake_calculate(bars):
        last_bar = bars[-1]
        return {
            "price": float(last_bar["close"]),
            "bar_timestamp": float(last_bar["timestamp"]),
        }

    def fake_check_entry(_symbol, _indicators, _bar_index, _runtime):
        return None

    def fake_pop_last_decision(_symbol):
        observed_decision_bars.append(
            float(polygon_bot.builder_manager.get_builder("CTNT").bars[-1].timestamp)  # type: ignore[union-attr]
        )
        return {
            "status": "idle",
            "reason": "no entry path matched",
        }

    polygon_bot.indicator_engine.calculate = fake_calculate
    polygon_bot.entry_engine.check_entry = fake_check_entry
    polygon_bot.entry_engine.pop_last_decision = fake_pop_last_decision

    first_bucket_start = datetime(2026, 4, 23, 15, 26, 30, tzinfo=UTC)

    state.handle_live_bar(
        symbol="CTNT",
        interval_secs=1,
        open_price=3.21,
        high_price=3.25,
        low_price=3.2032,
        close_price=3.23,
        volume=14_122,
        timestamp=first_bucket_start.timestamp(),
        trade_count=171,
        strategy_codes=["polygon_30s"],
    )
    state.handle_live_bar(
        symbol="CTNT",
        interval_secs=1,
        open_price=3.23,
        high_price=3.24,
        low_price=3.22,
        close_price=3.23,
        volume=500,
        timestamp=datetime(2026, 4, 23, 15, 27, 0, tzinfo=UTC).timestamp(),
        trade_count=5,
        strategy_codes=["polygon_30s"],
    )

    with session_factory() as session:
        records = list(
            session.scalars(
                select(StrategyBarHistory)
                .where(
                    StrategyBarHistory.strategy_code == "polygon_30s",
                    StrategyBarHistory.symbol == "CTNT",
                    StrategyBarHistory.interval_secs == 30,
                )
                .order_by(StrategyBarHistory.bar_time.asc())
            )
        )

    assert observed_decision_bars == [first_bucket_start.timestamp()]
    assert len(records) == 1
    assert records[0].bar_time.replace(tzinfo=UTC) == first_bucket_start
    assert records[0].volume == 14_122
    assert records[0].trade_count == 171

    state.handle_live_bar(
        symbol="CTNT",
        interval_secs=1,
        open_price=3.23,
        high_price=3.24,
        low_price=3.21,
        close_price=3.23,
        volume=1_538,
        timestamp=datetime(2026, 4, 23, 15, 26, 59, tzinfo=UTC).timestamp(),
        trade_count=30,
        strategy_codes=["polygon_30s"],
    )

    with session_factory() as session:
        records = list(
            session.scalars(
                select(StrategyBarHistory)
                .where(
                    StrategyBarHistory.strategy_code == "polygon_30s",
                    StrategyBarHistory.symbol == "CTNT",
                    StrategyBarHistory.interval_secs == 30,
                )
                .order_by(StrategyBarHistory.bar_time.asc())
            )
        )

    assert observed_decision_bars == [first_bucket_start.timestamp()]
    assert len(records) == 1
    assert records[0].volume == 15_660
    assert records[0].trade_count == 201


@pytest.mark.asyncio
async def test_hydrate_generic_history_from_provider_seeds_polygon_when_replay_is_missing() -> None:
    session_factory = build_test_session_factory()
    service = StrategyEngineService(
        settings=make_test_settings(
            redis_stream_prefix="test",
            dashboard_snapshot_persistence_enabled=False,
            strategy_history_persistence_enabled=True,
            strategy_polygon_30s_enabled=True,
            strategy_polygon_30s_live_aggregate_bars_enabled=True,
            massive_api_key="test-key",
        ),
        redis_client=FakeRedis(),
        session_factory=session_factory,
    )
    polygon_bot = service.state.bots["polygon_30s"]
    polygon_bot.set_watchlist(["UGRO"])

    class _FakeSnapshotProvider:
        def fetch_historical_bars(
            self,
            symbol: str,
            *,
            interval_secs: int,
            lookback_calendar_days: int,
            limit: int,
        ) -> list[HistoricalBarRecord]:
            assert symbol == "UGRO"
            assert interval_secs == 30
            assert lookback_calendar_days >= 3
            assert limit >= 120
            start = 1_700_000_000.0
            return [
                HistoricalBarRecord(
                    open=2.0 + (index * 0.01),
                    high=2.02 + (index * 0.01),
                    low=1.99 + (index * 0.01),
                    close=2.01 + (index * 0.01),
                    volume=20_000 + index,
                    timestamp=start + (index * 30),
                    trade_count=5,
                )
                for index in range(80)
            ]

    service._massive_snapshot_provider = _FakeSnapshotProvider()

    hydrated = await service._hydrate_generic_history_from_provider({("UGRO", 30)})

    assert hydrated is True
    builder = polygon_bot.builder_manager.get_builder("UGRO")
    assert builder is not None
    assert builder.get_bar_count() == 80
    assert "UGRO" in polygon_bot.last_indicators
    with session_factory() as session:
        records = list(
            session.scalars(
                select(StrategyBarHistory)
                .where(
                    StrategyBarHistory.strategy_code == "polygon_30s",
                    StrategyBarHistory.symbol == "UGRO",
                    StrategyBarHistory.interval_secs == 30,
                )
                .order_by(StrategyBarHistory.bar_time)
            )
        )
    assert len(records) == 80
    assert records[0].bar_time.replace(tzinfo=UTC) == datetime.fromtimestamp(1_700_000_000.0, UTC)
    assert records[-1].bar_time.replace(tzinfo=UTC) == datetime.fromtimestamp(1_700_000_000.0 + (79 * 30), UTC)


def test_restore_runtime_bar_history_from_database_includes_polygon_provider_bot() -> None:
    session_factory = build_test_session_factory()
    service = StrategyEngineService(
        settings=make_test_settings(
            dashboard_snapshot_persistence_enabled=False,
            strategy_history_persistence_enabled=False,
            strategy_polygon_30s_enabled=True,
            strategy_polygon_30s_broker_provider="webull",
        ),
        redis_client=FakeRedis(),
        session_factory=session_factory,
    )
    polygon_bot = service.state.bots["polygon_30s"]
    polygon_bot.set_watchlist(["UGRO"])

    session_start_utc = current_scanner_session_start_utc(service.state.alert_engine.now_provider())
    with session_factory() as session:
        for index in range(80):
            bar_time = session_start_utc + timedelta(seconds=index * 30)
            session.add(
                StrategyBarHistory(
                    strategy_code="polygon_30s",
                    symbol="UGRO",
                    interval_secs=30,
                    bar_time=bar_time,
                    open_price=Decimal("2.00") + (Decimal("0.01") * index),
                    high_price=Decimal("2.02") + (Decimal("0.01") * index),
                    low_price=Decimal("1.99") + (Decimal("0.01") * index),
                    close_price=Decimal("2.01") + (Decimal("0.01") * index),
                    volume=20_000 + index,
                    trade_count=5,
                    position_state="flat",
                    position_quantity=0,
                    decision_status="idle",
                    decision_reason="seed",
                    decision_path="",
                    decision_score="",
                    decision_score_details="",
                    indicators_json={},
                )
            )
        session.commit()

    service._restore_runtime_bar_history_from_database()

    builder = polygon_bot.builder_manager.get_builder("UGRO")
    assert builder is not None
    assert builder.get_bar_count() == 80
    assert "UGRO" in polygon_bot.last_indicators


def test_market_data_symbols_exclude_schwab_native_macd_30s() -> None:
    state = StrategyEngineState(
        settings=make_test_settings(
            strategy_macd_30s_enabled=True,
            strategy_macd_30s_broker_provider="schwab",
        ),
        now_provider=fixed_now,
    )

    state.bots["macd_30s"].set_watchlist(["ELAB"])

    assert state.market_data_symbols() == []
    assert state.schwab_stream_symbols() == ["ELAB"]


def test_broker_blocked_symbols_filter_only_schwab_backed_watchlists() -> None:
    state = StrategyEngineState(
        settings=make_test_settings(
            strategy_macd_30s_enabled=True,
            strategy_macd_30s_broker_provider="schwab",
            strategy_polygon_30s_enabled=True,
            strategy_polygon_30s_broker_provider="simulated",
        ),
        now_provider=fixed_now,
    )

    state.restore_confirmed_runtime_view(
        [{"ticker": "UGRO", "score": 7}],
        all_confirmed=[{"ticker": "UGRO", "score": 7}],
    )
    state.set_broker_blocked_symbols_by_strategy({"macd_30s": {"UGRO"}})

    assert state.bots["macd_30s"].watchlist == set()
    assert state.bots["polygon_30s"].watchlist == {"UGRO"}


def test_service_loads_schwab_ineligible_symbols_per_strategy_account() -> None:
    session_factory = build_test_session_factory()
    settings = make_test_settings(
        strategy_macd_30s_enabled=True,
        strategy_macd_30s_broker_provider="schwab",
        strategy_polygon_30s_enabled=True,
        strategy_polygon_30s_broker_provider="simulated",
    )
    service = StrategyEngineService(
        settings=settings,
        redis_client=FakeRedis(),
        session_factory=session_factory,
        now_provider=fixed_now,
    )

    with session_factory() as session:
        macd_account = BrokerAccount(
            name=settings.strategy_macd_30s_account_name,
            provider="schwab",
            environment=settings.environment,
        )
        polygon_account = BrokerAccount(
            name=settings.strategy_polygon_30s_account_name,
            provider="simulated",
            environment=settings.environment,
        )
        session.add_all([macd_account, polygon_account])
        session.flush()
        session.add(
            SchwabIneligibleToday(
                symbol="UGRO",
                session_date="2026-03-28",
                broker_account_id=macd_account.id,
                reason_text="Opening transactions for this security must be placed with a broker. Contact us",
                hit_count=1,
            )
        )
        session.commit()

    blocked = service._load_schwab_ineligible_symbols_by_strategy()

    assert blocked == {"macd_30s": {"UGRO"}}


def test_market_data_archive_retention_keeps_symbols_for_ttl(monkeypatch: pytest.MonkeyPatch) -> None:
    now_box = {"value": datetime(2026, 5, 5, 13, 0, tzinfo=UTC)}
    monkeypatch.setattr("project_mai_tai.services.strategy_engine_app.utcnow", lambda: now_box["value"])
    state = StrategyEngineState(
        settings=make_test_settings(
            strategy_macd_30s_enabled=True,
            strategy_macd_30s_broker_provider="schwab",
            market_data_archive_retention_enabled=True,
            market_data_archive_retention_minutes=30,
            market_data_archive_retention_max_symbols=5,
        ),
        now_provider=fixed_now,
    )

    state._add_market_data_archive_symbols(["UGRO", "WBUY"])
    assert state.market_data_symbols() == ["UGRO", "WBUY"]

    now_box["value"] = now_box["value"] + timedelta(minutes=31)
    assert state.market_data_symbols() == []


def test_confirmed_schwab_symbol_is_retained_in_market_data_archive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = StrategyEngineState(
        settings=make_test_settings(
            strategy_macd_30s_enabled=True,
            strategy_macd_30s_broker_provider="schwab",
            market_data_archive_retention_enabled=True,
            market_data_archive_retention_minutes=120,
            market_data_archive_retention_max_symbols=10,
        ),
        now_provider=fixed_now,
    )

    monkeypatch.setattr(
        state.alert_engine,
        "check_alerts",
        lambda snapshots, reference_data: [
            {
                "ticker": "UGRO",
                "type": "VOLUME_SPIKE",
                "price": 2.70,
                "volume": 900_000,
            }
        ],
    )
    monkeypatch.setattr(
        state.confirmed_scanner,
        "process_alerts",
        lambda alerts, reference_data, snapshot_lookup: [{"ticker": "UGRO", "price": 2.70}],
    )
    monkeypatch.setattr(
        state.confirmed_scanner,
        "get_all_confirmed",
        lambda: [{"ticker": "UGRO", "price": 2.70}],
    )
    monkeypatch.setattr(state.confirmed_scanner, "update_live_prices", lambda snapshot_lookup: None)
    monkeypatch.setattr(state.confirmed_scanner, "prune_faded_candidates", lambda: None)

    state.process_snapshot_batch(
        [snapshot_from_payload(make_snapshot_payload(symbol="UGRO", price=2.7, volume=900_000))],
        {"UGRO": ReferenceData(shares_outstanding=50_000, avg_daily_volume=390_000)},
    )

    assert state.market_data_symbols() == ["UGRO"]


def test_raw_momentum_alert_adds_schwab_prewarm_without_bot_watchlist(monkeypatch: pytest.MonkeyPatch) -> None:
    state = StrategyEngineState(
        settings=make_test_settings(
            strategy_macd_30s_enabled=True,
            strategy_macd_30s_broker_provider="schwab",
        ),
        now_provider=fixed_now,
    )

    monkeypatch.setattr(
        state.alert_engine,
        "check_alerts",
        lambda snapshots, reference_data: [
            {
                "ticker": "UGRO",
                "type": "VOLUME_SPIKE",
                "price": 2.70,
                "volume": 900_000,
            }
        ],
    )
    monkeypatch.setattr(
        state.confirmed_scanner,
        "process_alerts",
        lambda alerts, reference_data, snapshot_lookup: [],
    )

    summary = state.process_snapshot_batch(
        [snapshot_from_payload(make_snapshot_payload(symbol="UGRO", price=2.7, volume=900_000))],
        {"UGRO": ReferenceData(shares_outstanding=50_000, avg_daily_volume=390_000)},
    )

    assert summary["watchlist"] == []
    assert summary["schwab_prewarm_symbols"] == ["UGRO"]
    assert state.bots["macd_30s"].watchlist == set()
    assert state.bots["macd_30s"].prewarm_symbols == {"UGRO"}
    assert state.schwab_stream_symbols() == ["UGRO"]


def test_schwab_prewarm_symbols_expire_and_do_not_accumulate_indefinitely(monkeypatch: pytest.MonkeyPatch) -> None:
    now_box = {"value": datetime(2026, 4, 24, 11, 0, tzinfo=UTC)}
    monkeypatch.setattr("project_mai_tai.services.strategy_engine_app.utcnow", lambda: now_box["value"])
    state = StrategyEngineState(
        settings=make_test_settings(
            strategy_macd_30s_enabled=True,
            strategy_macd_30s_broker_provider="schwab",
        ),
        now_provider=fixed_now,
    )

    state._add_schwab_prewarm_symbols(["UGRO", "WBUY"])
    assert state.schwab_stream_symbols() == ["UGRO", "WBUY"]

    now_box["value"] = now_box["value"] + timedelta(minutes=11)
    state._sync_schwab_prewarm_symbols()

    assert state.schwab_prewarm_symbols == []
    assert state.bots["macd_30s"].prewarm_symbols == set()
    assert state.schwab_stream_symbols() == []


def test_schwab_prewarm_trade_ticks_build_bars_without_entry_checks(monkeypatch: pytest.MonkeyPatch) -> None:
    service = StrategyEngineService(
        settings=make_test_settings(
            redis_stream_prefix="test",
            strategy_history_persistence_enabled=False,
            strategy_macd_30s_broker_provider="schwab",
            strategy_macd_30s_live_aggregate_bars_enabled=True,
        ),
        redis_client=FakeRedis(),
        now_provider=fixed_now,
    )
    runtime = service.state.bots["macd_30s"]
    runtime.set_prewarm_symbols(["UGRO"])

    def fail_entry_check(*_args, **_kwargs):
        raise AssertionError("prewarm-only symbols must not evaluate entries")

    def fail_indicator_calculation(*_args, **_kwargs):
        raise AssertionError("prewarm-only symbols must not calculate indicators")

    monkeypatch.setattr(runtime.entry_engine, "check_entry", fail_entry_check)
    monkeypatch.setattr(runtime.indicator_engine, "calculate", fail_indicator_calculation)
    persisted: list[str] = []
    monkeypatch.setattr(
        runtime,
        "_persist_bar_history",
        lambda **kwargs: persisted.append(str(kwargs.get("symbol", ""))),
    )

    first_tick = 1_700_000_000_000_000_000
    second_tick = first_tick + 31_000_000_000

    first_intents = runtime.handle_trade_tick("UGRO", price=2.70, size=100, timestamp_ns=first_tick)
    second_intents = runtime.handle_trade_tick("UGRO", price=2.73, size=150, timestamp_ns=second_tick)

    assert first_intents == []
    assert second_intents == []
    assert runtime.watchlist == set()
    assert runtime.builder_manager.get_builder("UGRO").get_bar_count() == 1
    assert "UGRO" not in runtime.last_indicators
    assert persisted == []
    assert runtime.stream_symbols() == {"UGRO"}


def test_global_manual_stop_removes_schwab_prewarm_symbol() -> None:
    state = StrategyEngineState(
        settings=make_test_settings(
            strategy_macd_30s_enabled=True,
            strategy_macd_30s_broker_provider="schwab",
        ),
        now_provider=fixed_now,
    )

    state._add_schwab_prewarm_symbols(["UGRO"])
    state.apply_manual_stop_update(scope="global", action="stop", symbol="UGRO")

    assert state.schwab_prewarm_symbols == []
    assert state.bots["macd_30s"].prewarm_symbols == set()
    assert state.schwab_stream_symbols() == []


def test_global_stop_resume_restores_previously_handed_off_symbol_to_bot_watchlist() -> None:
    state = StrategyEngineState(now_provider=fixed_now)
    state.restore_confirmed_runtime_view(
        [
            {"ticker": "SST", "rank_score": 80.0, "change_pct": 40.0, "confirmed_at": "09:45:00 AM ET"},
        ]
    )

    state.apply_manual_stop_update(scope="global", action="stop", symbol="SST")

    assert "SST" not in state.bots["macd_30s"].watchlist
    assert "SST" not in state.bot_handoff_symbols_by_strategy["macd_30s"]

    state.current_confirmed = []
    state.all_confirmed = []
    state.apply_manual_stop_update(scope="global", action="resume", symbol="SST")

    assert "SST" in state.bot_handoff_symbols_by_strategy["macd_30s"]
    assert "SST" in state.bots["macd_30s"].watchlist


@pytest.mark.asyncio
async def test_sync_subscription_targets_includes_schwab_symbols_when_stream_fallback_is_active() -> None:
    redis = FakeRedis()
    service = StrategyEngineService(
        settings=make_test_settings(
            redis_stream_prefix="test",
            dashboard_snapshot_persistence_enabled=False,
            strategy_macd_30s_broker_provider="schwab",
        ),
        redis_client=redis,
    )
    service.state.bots["macd_30s"].set_watchlist(["ELAB"])

    class FakeStreamClient:
        connected = False
        connection_failures = 1

        async def sync_subscriptions(self, symbols, *, chart_symbols=None, timesale_symbols=None):
            self.symbols = symbols
            self.chart_symbols = chart_symbols
            self.timesale_symbols = timesale_symbols

    service._schwab_stream_client = FakeStreamClient()

    await service._sync_subscription_targets()

    stream_entries = [data for stream, data in redis.entries if stream == "test:market-data-subscriptions"]
    assert stream_entries
    payload = json.loads(stream_entries[-1])
    assert payload["payload"]["symbols"] == ["ELAB"]
    assert service._schwab_stream_client.timesale_symbols == []


@pytest.mark.asyncio
async def test_sync_subscription_targets_excludes_prewarm_from_generic_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = StrategyEngineService(
        settings=make_test_settings(
            redis_stream_prefix="test",
            dashboard_snapshot_persistence_enabled=False,
            strategy_history_persistence_enabled=False,
            strategy_macd_30s_broker_provider="schwab",
        ),
        redis_client=FakeRedis(),
    )
    service.state._add_schwab_prewarm_symbols(["UGRO"])
    captured: dict[str, list[str]] = {}

    class FakeStreamClient:
        connected = False
        connection_failures = 1

    async def fake_market_data_subscriptions(symbols):
        captured["market_data"] = list(symbols)

    async def fake_schwab_subscriptions(symbols):
        captured["schwab"] = list(symbols)

    service._schwab_stream_client = FakeStreamClient()
    monkeypatch.setattr(service, "_sync_market_data_subscriptions", fake_market_data_subscriptions)
    monkeypatch.setattr(service, "_sync_schwab_stream_subscriptions", fake_schwab_subscriptions)

    await service._sync_subscription_targets()

    assert captured["market_data"] == []
    assert captured["schwab"] == ["UGRO"]


@pytest.mark.asyncio
async def test_trade_tick_stream_routes_to_schwab_native_macd_30s_when_stream_fallback_is_active(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis = FakeRedis()
    service = StrategyEngineService(
        settings=make_test_settings(
            redis_stream_prefix="test",
            dashboard_snapshot_persistence_enabled=False,
            strategy_history_persistence_enabled=False,
            strategy_macd_30s_broker_provider="schwab",
            strategy_macd_30s_live_aggregate_bars_enabled=False,
        ),
        redis_client=redis,
    )

    class FakeStreamClient:
        connected = False
        connection_failures = 1

    service._schwab_stream_client = FakeStreamClient()
    captured: dict[str, object] = {}

    def fake_handle_trade_tick(*, symbol, price, size, timestamp_ns=None, cumulative_volume=None, strategy_codes=None, exclude_codes=None):
        captured.update(
            {
                "symbol": symbol,
                "price": price,
                "size": size,
                "timestamp_ns": timestamp_ns,
                "cumulative_volume": cumulative_volume,
                "strategy_codes": tuple(strategy_codes or ()),
                "exclude_codes": exclude_codes,
            }
        )
        return []

    monkeypatch.setattr(service.state, "handle_trade_tick", fake_handle_trade_tick)

    event = TradeTickEvent(
        source_service="market-data-gateway",
        payload=TradeTickPayload(
            symbol="UGRO",
            price=Decimal("2.80"),
            size=200,
            timestamp_ns=1_700_001_500_000_000_000,
            cumulative_volume=40_000,
        ),
    )

    await service._handle_stream_message("test:market-data", {"data": event.model_dump_json()})

    assert captured["symbol"] == "UGRO"
    assert "macd_30s" in captured["strategy_codes"]
    assert captured["exclude_codes"] is None


@pytest.mark.asyncio
async def test_live_bar_publishes_strategy_snapshot_for_generic_bot_activity_without_intents(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis = FakeRedis()
    service = StrategyEngineService(
        settings=make_test_settings(
            redis_stream_prefix="test",
            dashboard_snapshot_persistence_enabled=False,
            strategy_history_persistence_enabled=False,
            strategy_macd_30s_broker_provider="schwab",
            strategy_polygon_30s_enabled=True,
            strategy_polygon_30s_broker_provider="webull",
        ),
        redis_client=redis,
    )
    captured: dict[str, object] = {}

    def fake_handle_live_bar(**kwargs):
        captured.update(kwargs)
        return []

    monkeypatch.setattr(service.state, "handle_live_bar", fake_handle_live_bar)

    event = LiveBarEvent(
        source_service="market-data-gateway",
        payload=LiveBarPayload(
            symbol="UGRO",
            interval_secs=30,
            open=Decimal("2.75"),
            high=Decimal("2.84"),
            low=Decimal("2.70"),
            close=Decimal("2.80"),
            volume=5000,
            timestamp=1_700_001_530.0,
            trade_count=25,
        ),
    )

    await service._handle_stream_message("test:market-data", {"data": event.model_dump_json()})

    assert captured["symbol"] == "UGRO"
    assert captured["strategy_codes"] == ("polygon_30s",)
    assert captured["coverage_started_at"] is None
    assert any(stream.endswith("strategy-state") for stream, _ in redis.entries)


@pytest.mark.asyncio
async def test_live_bar_event_forwards_provider_coverage_timestamp(monkeypatch) -> None:
    redis = FakeRedis()
    service = StrategyEngineService(
        settings=make_test_settings(
            redis_stream_prefix="test",
            dashboard_snapshot_persistence_enabled=False,
            strategy_history_persistence_enabled=False,
            strategy_macd_30s_broker_provider="schwab",
            strategy_polygon_30s_enabled=True,
            strategy_polygon_30s_broker_provider="webull",
        ),
        redis_client=redis,
    )
    captured: dict[str, object] = {}

    def fake_handle_live_bar(**kwargs):
        captured.update(kwargs)
        return []

    monkeypatch.setattr(service.state, "handle_live_bar", fake_handle_live_bar)

    event = LiveBarEvent(
        source_service="market-data-gateway",
        payload=LiveBarPayload(
            symbol="IONZ",
            interval_secs=30,
            open=Decimal("5.10"),
            high=Decimal("5.14"),
            low=Decimal("5.09"),
            close=Decimal("5.13"),
            volume=4200,
            timestamp=1_700_001_590.0,
            trade_count=5,
            coverage_started_at=1_700_001_560.0,
        ),
    )

    await service._handle_stream_message("test:market-data", {"data": event.model_dump_json()})

    assert captured["symbol"] == "IONZ"
    assert captured["coverage_started_at"] == 1_700_001_560.0


@pytest.mark.asyncio
async def test_schwab_live_bar_publishes_strategy_snapshot_for_generic_bot_activity_without_intents(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis = FakeRedis()
    service = StrategyEngineService(
        settings=make_test_settings(
            redis_stream_prefix="test",
            dashboard_snapshot_persistence_enabled=False,
            strategy_history_persistence_enabled=False,
            strategy_macd_30s_enabled=False,
            strategy_polygon_30s_enabled=False,
            strategy_macd_1m_enabled=False,
            strategy_schwab_1m_enabled=True,
        ),
        redis_client=redis,
    )
    captured: dict[str, object] = {}
    service.state.bots["schwab_1m"].set_watchlist(["CNSP"])

    def fake_handle_live_bar(**kwargs):
        captured.update(kwargs)
        return []

    monkeypatch.setattr(service.state, "handle_live_bar", fake_handle_live_bar)
    service._enqueue_schwab_live_bar(
        LiveBarRecord(
            symbol="CNSP",
            interval_secs=60,
            open=8.10,
            high=8.30,
            low=8.05,
            close=8.20,
            volume=12_000,
            timestamp=1_700_001_560.0,
            trade_count=18,
        )
    )

    intent_count, event_count = await service._drain_schwab_stream_queues()

    assert intent_count == 0
    assert event_count == 1
    assert captured["symbol"] == "CNSP"
    assert captured["strategy_codes"] == ("schwab_1m",)
    assert any(stream.endswith("strategy-state") for stream, _ in redis.entries)


@pytest.mark.asyncio
async def test_schwab_stream_queue_drain_is_bounded() -> None:
    service = StrategyEngineService(
        settings=make_test_settings(
            redis_stream_prefix="test",
            dashboard_snapshot_persistence_enabled=False,
            strategy_history_persistence_enabled=False,
            strategy_macd_30s_broker_provider="schwab",
        ),
        redis_client=FakeRedis(),
    )
    service._schwab_stream_drain_max_events = 3

    for index in range(5):
        service._enqueue_schwab_trade_tick(
            TradeTickRecord(
                symbol=f"Q{index}",
                price=1.02 + index,
                size=100,
                timestamp_ns=1_700_000_000_000_000_000 + index,
            )
        )

    intent_count, event_count = await service._drain_schwab_stream_queues()

    assert intent_count == 0
    assert event_count == 3
    assert service._schwab_trade_queue.qsize() == 2


def test_schwab_quote_enqueue_skips_prewarm_only_symbols() -> None:
    service = StrategyEngineService(
        settings=make_test_settings(
            redis_stream_prefix="test",
            dashboard_snapshot_persistence_enabled=False,
            strategy_history_persistence_enabled=False,
            strategy_macd_30s_broker_provider="schwab",
        ),
        redis_client=FakeRedis(),
    )
    runtime = service.state.bots["macd_30s"]
    runtime.set_prewarm_symbols(["UGRO"])

    service._enqueue_schwab_quote_tick(QuoteTickRecord(symbol="UGRO", bid_price=2.01, ask_price=2.02))
    assert service._schwab_quote_queue.qsize() == 0

    runtime.set_watchlist(["UGRO"])
    service._enqueue_schwab_quote_tick(QuoteTickRecord(symbol="UGRO", bid_price=2.03, ask_price=2.04))

    assert service._schwab_quote_queue.qsize() == 1


def test_market_data_symbols_exclude_schwab_backed_tos() -> None:
    state = StrategyEngineState(
        settings=make_test_settings(
            strategy_macd_1m_enabled=True,
            strategy_tos_enabled=True,
            strategy_runner_enabled=True,
            strategy_tos_broker_provider="schwab",
        ),
        now_provider=fixed_now,
    )

    if "macd_30s" in state.bots:
        state.bots["macd_30s"].set_watchlist([])
    state.bots["macd_1m"].set_watchlist([])
    state.bots["tos"].set_watchlist(["ELAB"])
    state.bots["runner"].set_watchlist([])

    assert state.market_data_symbols() == []
    assert state.schwab_stream_symbols() == ["ELAB"]


def test_tos_uses_configured_default_quantity() -> None:
    state = StrategyEngineState(
        settings=make_test_settings(
            strategy_tos_enabled=True,
            strategy_tos_default_quantity=10,
        ),
        now_provider=fixed_now,
    )

    assert state.bots["tos"].definition.trading_config.default_quantity == 10


def test_quote_tick_updates_latest_quotes_for_macd_30s() -> None:
    state = StrategyEngineState(now_provider=fixed_now)

    state.handle_quote_tick(
        symbol="ELAB",
        bid_price=2.11,
        ask_price=2.12,
    )

    assert state.bots["macd_30s"].latest_quotes["ELAB"] == {"bid": 2.11, "ask": 2.12}


def test_gateway_quote_tick_can_exclude_schwab_native_macd_30s() -> None:
    state = StrategyEngineState(now_provider=fixed_now)

    state.handle_quote_tick(
        symbol="ELAB",
        bid_price=2.11,
        ask_price=2.12,
        exclude_codes=("macd_30s",),
    )

    assert "ELAB" not in state.bots["macd_30s"].latest_quotes


def test_macd_30s_uses_configured_tick_bar_close_grace() -> None:
    state = StrategyEngineState(
        settings=make_test_settings(
            strategy_macd_30s_broker_provider="schwab",
            strategy_macd_30s_tick_bar_close_grace_seconds=2.0,
        ),
        now_provider=fixed_now,
    )

    runtime = state.bots["macd_30s"]

    assert getattr(runtime.builder_manager, "close_grace_seconds", None) == pytest.approx(2.0)
    assert getattr(runtime.builder_manager, "fill_gap_bars", None) is False
    assert getattr(runtime, "trade_tick_service", None) == "LEVELONE_EQUITIES"


def test_gateway_quote_tick_can_exclude_schwab_backed_tos() -> None:
    state = StrategyEngineState(
        settings=make_test_settings(
            strategy_tos_enabled=True,
            strategy_tos_broker_provider="schwab",
        ),
        now_provider=fixed_now,
    )

    state.handle_quote_tick(
        symbol="ELAB",
        bid_price=2.11,
        ask_price=2.12,
        exclude_codes=state.schwab_stream_strategy_codes(),
    )

    assert "ELAB" not in state.bots["tos"].latest_quotes


@pytest.mark.asyncio
async def test_service_uses_fallback_quotes_for_stale_schwab_open_positions() -> None:
    settings = make_test_settings(
        strategy_macd_30s_broker_provider="schwab",
        redis_stream_prefix="test",
        dashboard_snapshot_persistence_enabled=False,
        strategy_history_persistence_enabled=False,
        schwab_stream_symbol_stale_after_seconds=1.0,
        schwab_stream_symbol_quote_poll_interval_seconds=0.5,
        schwab_stream_symbol_resubscribe_interval_seconds=1.0,
    )
    service = StrategyEngineService(settings=settings, redis_client=FakeRedis())
    runtime = service.state.bots["macd_30s"]
    runtime.positions.open_position("ENVB", 4.0, quantity=10, path="ENTRY")
    old = datetime.now(UTC) - timedelta(seconds=40)
    service._schwab_symbol_last_stream_trade_at["ENVB"] = old
    service._schwab_symbol_last_stream_quote_at["ENVB"] = old

    published: list[TradeIntent] = []

    class FakeStreamClient:
        def __init__(self) -> None:
            self.force_resubscribe_calls = 0

        async def force_resubscribe(self) -> None:
            self.force_resubscribe_calls += 1

    class FakeQuotePollAdapter:
        async def fetch_quotes(self, symbols):
            assert symbols == ["ENVB"]
            return {
                "ENVB": {
                    "bid_price": 4.20,
                    "ask_price": 4.22,
                    "last_price": 4.21,
                    "bid_size": 1000.0,
                    "ask_size": 900.0,
                }
            }

    fake_stream_client = FakeStreamClient()
    service._schwab_stream_client = fake_stream_client
    service._schwab_quote_poll_adapter = FakeQuotePollAdapter()

    async def fake_publish_intent(intent):
        published.append(intent)

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(service, "_publish_intent", fake_publish_intent)
    try:
        intent_count = await service._monitor_schwab_symbol_health()
    finally:
        monkeypatch.undo()

    assert intent_count == 2
    assert fake_stream_client.force_resubscribe_calls == 1
    assert runtime.latest_quotes["ENVB"] == {"bid": 4.2, "ask": 4.22}
    assert runtime.data_health_summary()["status"] == "critical"
    assert runtime.data_health_summary()["halted_symbols"] == ["ENVB"]
    assert published[0].payload.intent_type == "close"
    assert published[0].payload.symbol == "ENVB"
    assert published[0].payload.reason == "SCHWAB_DATA_STALE_EMERGENCY_CLOSE"


@pytest.mark.asyncio
async def test_service_skips_emergency_close_when_rest_quote_proves_stream_lag() -> None:
    settings = make_test_settings(
        strategy_macd_30s_broker_provider="schwab",
        redis_stream_prefix="test",
        dashboard_snapshot_persistence_enabled=False,
        strategy_history_persistence_enabled=False,
        schwab_stream_symbol_stale_after_seconds=1.0,
        schwab_stream_symbol_quote_poll_interval_seconds=0.5,
        schwab_stream_symbol_resubscribe_interval_seconds=1.0,
    )
    service = StrategyEngineService(settings=settings, redis_client=FakeRedis())
    runtime = service.state.bots["macd_30s"]
    runtime.positions.open_position("ENVB", 4.0, quantity=10, path="ENTRY")
    old = datetime.now(UTC) - timedelta(seconds=40)
    service._schwab_symbol_last_stream_trade_at["ENVB"] = old
    service._schwab_symbol_last_stream_quote_at["ENVB"] = old

    published: list[TradeIntent] = []
    fresh_trade_time_ms = (datetime.now(UTC) - timedelta(seconds=2)).timestamp() * 1000.0

    class FakeStreamClient:
        def __init__(self) -> None:
            self.force_resubscribe_calls = 0

        async def force_resubscribe(self) -> None:
            self.force_resubscribe_calls += 1

    class FakeQuotePollAdapter:
        async def fetch_quotes(self, symbols):
            return {
                "ENVB": {
                    "bid_price": 4.20,
                    "ask_price": 4.22,
                    "last_price": 4.21,
                    "trade_time_ms": fresh_trade_time_ms,
                    "quote_time_ms": fresh_trade_time_ms,
                }
            }

    service._schwab_stream_client = FakeStreamClient()
    service._schwab_quote_poll_adapter = FakeQuotePollAdapter()

    async def fake_publish_intent(intent):
        published.append(intent)

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(service, "_publish_intent", fake_publish_intent)
    try:
        await service._monitor_schwab_symbol_health()
    finally:
        monkeypatch.undo()

    close_intents = [p for p in published if p.payload.intent_type == "close"]
    assert close_intents == [], (
        "REST poll showed fresh data; emergency close must not fire"
    )
    assert "ENVB" not in service._schwab_stale_symbols
    assert runtime.data_health_summary()["halted_symbols"] == []
    last_quote_at = service._schwab_symbol_last_stream_quote_at["ENVB"]
    assert (datetime.now(UTC) - last_quote_at).total_seconds() < 5.0, (
        "stream baseline should advance to the fresh REST poll timestamp"
    )


@pytest.mark.asyncio
async def test_service_falls_through_to_emergency_close_when_rest_quote_also_stale() -> None:
    settings = make_test_settings(
        strategy_macd_30s_broker_provider="schwab",
        redis_stream_prefix="test",
        dashboard_snapshot_persistence_enabled=False,
        strategy_history_persistence_enabled=False,
        schwab_stream_symbol_stale_after_seconds=1.0,
        schwab_stream_symbol_quote_poll_interval_seconds=0.5,
        schwab_stream_symbol_resubscribe_interval_seconds=1.0,
    )
    service = StrategyEngineService(settings=settings, redis_client=FakeRedis())
    runtime = service.state.bots["macd_30s"]
    runtime.positions.open_position("ENVB", 4.0, quantity=10, path="ENTRY")
    old = datetime.now(UTC) - timedelta(seconds=40)
    service._schwab_symbol_last_stream_trade_at["ENVB"] = old
    service._schwab_symbol_last_stream_quote_at["ENVB"] = old

    published: list[TradeIntent] = []
    stale_trade_time_ms = (datetime.now(UTC) - timedelta(seconds=120)).timestamp() * 1000.0

    class FakeStreamClient:
        async def force_resubscribe(self) -> None:
            return None

        async def sync_subscriptions(self, *args, **kwargs) -> None:
            return None

    class FakeQuotePollAdapter:
        async def fetch_quotes(self, symbols):
            return {
                "ENVB": {
                    "bid_price": 4.20,
                    "ask_price": 4.22,
                    "last_price": 4.21,
                    "trade_time_ms": stale_trade_time_ms,
                    "quote_time_ms": stale_trade_time_ms,
                }
            }

    service._schwab_stream_client = FakeStreamClient()
    service._schwab_quote_poll_adapter = FakeQuotePollAdapter()

    async def fake_publish_intent(intent):
        published.append(intent)

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(service, "_publish_intent", fake_publish_intent)
    try:
        await service._monitor_schwab_symbol_health()
    finally:
        monkeypatch.undo()

    close_intents = [p for p in published if p.payload.intent_type == "close"]
    assert len(close_intents) == 1
    assert close_intents[0].payload.symbol == "ENVB"
    assert close_intents[0].payload.reason == "SCHWAB_DATA_STALE_EMERGENCY_CLOSE"


@pytest.mark.asyncio
async def test_service_emergency_close_rescue_can_be_disabled_via_setting() -> None:
    settings = make_test_settings(
        strategy_macd_30s_broker_provider="schwab",
        redis_stream_prefix="test",
        dashboard_snapshot_persistence_enabled=False,
        strategy_history_persistence_enabled=False,
        schwab_stream_symbol_stale_after_seconds=1.0,
        schwab_stream_symbol_quote_poll_interval_seconds=0.5,
        schwab_stream_symbol_resubscribe_interval_seconds=1.0,
        schwab_emergency_close_rest_rescue_enabled=False,
    )
    service = StrategyEngineService(settings=settings, redis_client=FakeRedis())
    runtime = service.state.bots["macd_30s"]
    runtime.positions.open_position("ENVB", 4.0, quantity=10, path="ENTRY")
    old = datetime.now(UTC) - timedelta(seconds=40)
    service._schwab_symbol_last_stream_trade_at["ENVB"] = old
    service._schwab_symbol_last_stream_quote_at["ENVB"] = old

    published: list[TradeIntent] = []
    fresh_trade_time_ms = (datetime.now(UTC) - timedelta(seconds=2)).timestamp() * 1000.0

    class FakeStreamClient:
        async def force_resubscribe(self) -> None:
            return None

        async def sync_subscriptions(self, *args, **kwargs) -> None:
            return None

    class FakeQuotePollAdapter:
        async def fetch_quotes(self, symbols):
            return {
                "ENVB": {
                    "bid_price": 4.20,
                    "ask_price": 4.22,
                    "last_price": 4.21,
                    "trade_time_ms": fresh_trade_time_ms,
                    "quote_time_ms": fresh_trade_time_ms,
                }
            }

    service._schwab_stream_client = FakeStreamClient()
    service._schwab_quote_poll_adapter = FakeQuotePollAdapter()

    async def fake_publish_intent(intent):
        published.append(intent)

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(service, "_publish_intent", fake_publish_intent)
    try:
        await service._monitor_schwab_symbol_health()
    finally:
        monkeypatch.undo()

    close_intents = [p for p in published if p.payload.intent_type == "close"]
    assert len(close_intents) == 1
    assert close_intents[0].payload.reason == "SCHWAB_DATA_STALE_EMERGENCY_CLOSE"


@pytest.mark.asyncio
async def test_publish_intent_drops_protected_symbols_before_publish() -> None:
    """Defense-in-depth: even before OMS sees it, the strategy service must
    never publish an intent (open OR close) for a symbol in
    MAI_TAI_PROTECTED_SYMBOLS. Verifies the Redis stream stays clean."""
    settings = make_test_settings(
        redis_stream_prefix="test",
        dashboard_snapshot_persistence_enabled=False,
        strategy_history_persistence_enabled=False,
        protected_symbols="CYN, ABC",
    )
    redis = FakeRedis()
    service = StrategyEngineService(settings=settings, redis_client=redis)

    for intent_type, side in [("open", "buy"), ("close", "sell"), ("scale", "buy")]:
        await service._publish_intent(
            TradeIntentEvent(
                source_service="strategy-engine",
                payload=TradeIntentPayload(
                    strategy_code="macd_30s",
                    broker_account_name="paper:macd_30s",
                    symbol="cyn",
                    side=side,
                    quantity=Decimal("10"),
                    intent_type=intent_type,
                    reason="ENTRY_P1_MACD_CROSS",
                    metadata={},
                ),
            )
        )

    intent_entries = [e for e in redis.entries if "strategy-intents" in e[0]]
    assert intent_entries == [], (
        f"protected-symbol intents must not reach the stream: {intent_entries}"
    )

    await service._publish_intent(
        TradeIntentEvent(
            source_service="strategy-engine",
            payload=TradeIntentPayload(
                strategy_code="macd_30s",
                broker_account_name="paper:macd_30s",
                symbol="OTHER",
                side="buy",
                quantity=Decimal("10"),
                intent_type="open",
                reason="ENTRY_P1_MACD_CROSS",
                metadata={},
            ),
        )
    )
    unprotected_entries = [e for e in redis.entries if "strategy-intents" in e[0]]
    assert len(unprotected_entries) == 1, "unprotected symbol must still publish"
    assert '"symbol":"OTHER"' in unprotected_entries[0][1]


@pytest.mark.asyncio
async def test_service_skips_stale_quote_poll_when_adapter_lacks_fetch_quotes() -> None:
    settings = make_test_settings(
        strategy_macd_30s_broker_provider="schwab",
        redis_stream_prefix="test",
        dashboard_snapshot_persistence_enabled=False,
        strategy_history_persistence_enabled=False,
        schwab_stream_symbol_stale_after_seconds=1.0,
        schwab_stream_symbol_quote_poll_interval_seconds=0.5,
        schwab_stream_symbol_resubscribe_interval_seconds=1.0,
    )
    service = StrategyEngineService(settings=settings, redis_client=FakeRedis())
    runtime = service.state.bots["macd_30s"]
    runtime.positions.open_position("ENVB", 4.0, quantity=10, path="ENTRY")
    old = datetime.now(UTC) - timedelta(seconds=40)
    service._schwab_symbol_last_stream_trade_at["ENVB"] = old
    service._schwab_symbol_last_stream_quote_at["ENVB"] = old

    class FakeStreamClient:
        async def force_resubscribe(self) -> None:
            return None

    service._schwab_stream_client = FakeStreamClient()
    service._schwab_quote_poll_adapter = object()

    intent_count = await service._monitor_schwab_symbol_health()

    assert intent_count == 1
    assert runtime.data_health_summary()["status"] == "critical"
    assert runtime.data_health_summary()["halted_symbols"] == ["ENVB"]


@pytest.mark.asyncio
async def test_service_halts_stale_schwab_watchlist_symbol_without_open_position(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = make_test_settings(
        strategy_macd_30s_broker_provider="schwab",
        redis_stream_prefix="test",
        dashboard_snapshot_persistence_enabled=False,
        strategy_history_persistence_enabled=False,
        schwab_stream_symbol_stale_after_seconds=1.0,
        schwab_stream_symbol_stale_after_seconds_without_position=30.0,
        schwab_stream_symbol_quote_poll_interval_seconds=0.5,
        schwab_stream_symbol_resubscribe_interval_seconds=1.0,
    )
    service = StrategyEngineService(settings=settings, redis_client=FakeRedis())
    runtime = service.state.bots["macd_30s"]
    runtime.set_watchlist(["ENVB"])
    fixed_now = datetime(2026, 4, 24, 20, 0, 0, tzinfo=UTC)
    old = fixed_now - timedelta(seconds=40)
    service._schwab_symbol_last_stream_trade_at["ENVB"] = old
    service._schwab_symbol_last_stream_quote_at["ENVB"] = old

    class FakeStreamClient:
        async def force_resubscribe(self) -> None:
            return None

    service._schwab_stream_client = FakeStreamClient()
    monkeypatch.setattr("project_mai_tai.services.strategy_engine_app.utcnow", lambda: fixed_now)

    activity_count = await service._monitor_schwab_symbol_health()

    assert activity_count == 1
    assert service._schwab_stale_symbols == {"ENVB"}
    assert runtime.data_health_summary()["status"] == "critical"
    assert "ENVB" in runtime.data_halt_symbols

    service._record_schwab_stream_activity("ENVB", activity_kind="trade")

    assert "ENVB" not in service._schwab_stale_symbols
    assert runtime.data_health_summary()["status"] == "healthy"


@pytest.mark.asyncio
async def test_service_gives_flat_schwab_watchlist_symbol_extended_stale_window() -> None:
    settings = make_test_settings(
        strategy_macd_30s_broker_provider="schwab",
        redis_stream_prefix="test",
        dashboard_snapshot_persistence_enabled=False,
        strategy_history_persistence_enabled=False,
        schwab_stream_symbol_stale_after_seconds=8.0,
        schwab_stream_symbol_stale_after_seconds_without_position=90.0,
        schwab_stream_symbol_resubscribe_interval_seconds=1.0,
    )
    service = StrategyEngineService(settings=settings, redis_client=FakeRedis())
    runtime = service.state.bots["macd_30s"]
    runtime.set_watchlist(["APLZ"])
    old = datetime.now(UTC) - timedelta(seconds=40)
    service._schwab_symbol_last_stream_trade_at["APLZ"] = old
    service._schwab_symbol_last_stream_quote_at["APLZ"] = old

    class FakeStreamClient:
        connected = True

        async def force_resubscribe(self) -> None:
            return None

    service._schwab_stream_client = FakeStreamClient()

    activity_count = await service._monitor_schwab_symbol_health()

    assert activity_count == 0
    assert service._schwab_stale_symbols == set()
    assert runtime.data_health_summary()["status"] == "healthy"
    assert runtime.data_health_summary()["halted_symbols"] == []


@pytest.mark.asyncio
async def test_service_does_not_halt_flat_schwab_symbol_outside_trading_hours(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = make_test_settings(
        strategy_macd_30s_broker_provider="schwab",
        redis_stream_prefix="test",
        dashboard_snapshot_persistence_enabled=False,
        strategy_history_persistence_enabled=False,
        schwab_stream_symbol_stale_after_seconds=8.0,
        schwab_stream_symbol_stale_after_seconds_without_position=90.0,
        schwab_stream_symbol_resubscribe_interval_seconds=1.0,
    )
    service = StrategyEngineService(settings=settings, redis_client=FakeRedis())
    runtime = service.state.bots["macd_30s"]
    runtime.set_watchlist(["APLZ"])
    fixed_now = datetime(2026, 4, 24, 22, 20, 43, tzinfo=UTC)
    old = fixed_now - timedelta(seconds=122)
    service._schwab_symbol_last_stream_trade_at["APLZ"] = old
    service._schwab_symbol_last_stream_quote_at["APLZ"] = old

    class FakeStreamClient:
        connected = True

        async def force_resubscribe(self) -> None:
            return None

    service._schwab_stream_client = FakeStreamClient()
    monkeypatch.setattr("project_mai_tai.services.strategy_engine_app.utcnow", lambda: fixed_now)

    activity_count = await service._monitor_schwab_symbol_health()

    assert activity_count == 0
    assert service._schwab_stale_symbols == set()
    assert runtime.data_health_summary()["status"] == "healthy"
    assert runtime.data_health_summary()["halted_symbols"] == []


@pytest.mark.asyncio
async def test_service_clears_data_halt_when_stale_symbol_leaves_active_set() -> None:
    settings = make_test_settings(
        strategy_macd_30s_broker_provider="schwab",
        redis_stream_prefix="test",
        dashboard_snapshot_persistence_enabled=False,
        strategy_history_persistence_enabled=False,
        schwab_stream_symbol_stale_after_seconds=1.0,
        schwab_stream_symbol_stale_after_seconds_without_position=30.0,
        schwab_stream_symbol_resubscribe_interval_seconds=1.0,
    )
    service = StrategyEngineService(settings=settings, redis_client=FakeRedis())
    runtime = service.state.bots["macd_30s"]
    runtime.set_watchlist(["ENVB"])
    old = datetime.now(UTC) - timedelta(seconds=40)
    service._schwab_symbol_last_stream_trade_at["ENVB"] = old
    service._schwab_symbol_last_stream_quote_at["ENVB"] = old

    class FakeStreamClient:
        connected = True

        async def force_resubscribe(self) -> None:
            return None

    service._schwab_stream_client = FakeStreamClient()

    activity_count = await service._monitor_schwab_symbol_health()

    assert activity_count == 1
    assert runtime.data_health_summary()["halted_symbols"] == ["ENVB"]

    runtime.set_manual_stop_symbols(["ENVB"])
    runtime.set_watchlist(["ELAB"])
    recent = datetime.now(UTC)
    service._schwab_symbol_last_stream_trade_at["ELAB"] = recent
    service._schwab_symbol_last_stream_quote_at["ELAB"] = recent

    activity_count = await service._monitor_schwab_symbol_health()

    assert activity_count == 0
    assert service._schwab_stale_symbols == set()
    assert runtime.data_health_summary()["status"] == "healthy"
    assert runtime.data_health_summary()["halted_symbols"] == []


@pytest.mark.asyncio
async def test_service_reactivated_symbol_gets_fresh_schwab_stale_grace_window() -> None:
    settings = make_test_settings(
        strategy_macd_30s_broker_provider="schwab",
        redis_stream_prefix="test",
        dashboard_snapshot_persistence_enabled=False,
        strategy_history_persistence_enabled=False,
        schwab_stream_symbol_stale_after_seconds=1.0,
        schwab_stream_symbol_stale_after_seconds_without_position=30.0,
        schwab_stream_symbol_resubscribe_interval_seconds=1.0,
    )
    service = StrategyEngineService(settings=settings, redis_client=FakeRedis())
    runtime = service.state.bots["macd_30s"]
    runtime.set_watchlist(["ENVB"])
    old = datetime.now(UTC) - timedelta(seconds=40)
    service._schwab_symbol_last_stream_trade_at["ENVB"] = old
    service._schwab_symbol_last_stream_quote_at["ENVB"] = old

    class FakeStreamClient:
        connected = True

        async def force_resubscribe(self) -> None:
            return None

    service._schwab_stream_client = FakeStreamClient()

    activity_count = await service._monitor_schwab_symbol_health()

    assert activity_count == 1
    assert runtime.data_health_summary()["halted_symbols"] == ["ENVB"]

    runtime.set_manual_stop_symbols(["ENVB"])
    runtime.set_watchlist([])
    activity_count = await service._monitor_schwab_symbol_health()

    assert activity_count == 0
    assert "ENVB" not in service._schwab_symbol_last_stream_trade_at
    assert "ENVB" not in service._schwab_symbol_last_stream_quote_at
    assert runtime.data_health_summary()["halted_symbols"] == []

    runtime.set_manual_stop_symbols([])
    runtime.set_watchlist(["ENVB"])
    activity_count = await service._monitor_schwab_symbol_health()

    assert activity_count == 0
    assert service._schwab_stale_symbols == set()
    assert runtime.data_health_summary()["status"] == "healthy"
    assert runtime.data_health_summary()["halted_symbols"] == []


@pytest.mark.asyncio
async def test_service_does_not_halt_quiet_schwab_symbol_inside_grace_window() -> None:
    settings = make_test_settings(
        strategy_macd_30s_broker_provider="schwab",
        redis_stream_prefix="test",
        dashboard_snapshot_persistence_enabled=False,
        strategy_history_persistence_enabled=False,
        schwab_stream_symbol_stale_after_seconds=1.0,
    )
    service = StrategyEngineService(settings=settings, redis_client=FakeRedis())
    runtime = service.state.bots["macd_30s"]
    runtime.set_watchlist(["ELAB"])
    recent = datetime.now(UTC) - timedelta(seconds=5)
    service._schwab_symbol_last_stream_trade_at["ELAB"] = recent
    service._schwab_symbol_last_stream_quote_at["ELAB"] = recent

    class FakeStreamClient:
        connected = True

    service._schwab_stream_client = FakeStreamClient()

    activity_count = await service._monitor_schwab_symbol_health()

    assert activity_count == 0
    assert service._schwab_stale_symbols == set()
    assert runtime.data_health_summary()["status"] == "healthy"


@pytest.mark.asyncio
async def test_service_default_stale_threshold_tolerates_brief_quiet_gap() -> None:
    settings = make_test_settings(
        strategy_macd_30s_broker_provider="schwab",
        redis_stream_prefix="test",
        dashboard_snapshot_persistence_enabled=False,
        strategy_history_persistence_enabled=False,
    )
    service = StrategyEngineService(settings=settings, redis_client=FakeRedis())
    runtime = service.state.bots["macd_30s"]
    runtime.set_watchlist(["FTFT"])
    recent = datetime.now(UTC) - timedelta(seconds=5)
    service._schwab_symbol_last_stream_trade_at["FTFT"] = recent
    service._schwab_symbol_last_stream_quote_at["FTFT"] = recent

    class FakeStreamClient:
        connected = True

    service._schwab_stream_client = FakeStreamClient()

    activity_count = await service._monitor_schwab_symbol_health()

    assert activity_count == 0
    assert service._schwab_stale_symbols == set()
    assert runtime.data_health_summary()["status"] == "healthy"
    assert runtime.data_health_summary()["halted_symbols"] == []


@pytest.mark.asyncio
async def test_service_brief_schwab_stream_disconnect_stays_inside_data_halt_grace_window() -> None:
    settings = make_test_settings(
        strategy_macd_30s_broker_provider="schwab",
        redis_stream_prefix="test",
        dashboard_snapshot_persistence_enabled=False,
        strategy_history_persistence_enabled=False,
        schwab_stream_symbol_stale_after_seconds=1.0,
    )
    service = StrategyEngineService(settings=settings, redis_client=FakeRedis())
    runtime = service.state.bots["macd_30s"]
    runtime.set_watchlist(["FTFT"])

    class FakeStreamClient:
        connected = False

    service._schwab_stream_client = FakeStreamClient()

    activity_count = await service._monitor_schwab_symbol_health()

    assert activity_count == 0
    assert service._schwab_stream_disconnected_since is not None
    assert service._schwab_stale_symbols == set()
    assert runtime.data_health_summary()["status"] == "healthy"
    assert runtime.data_health_summary()["halted_symbols"] == []


@pytest.mark.asyncio
async def test_service_persistent_schwab_stream_disconnect_halts_symbols_after_grace_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = make_test_settings(
        strategy_macd_30s_broker_provider="schwab",
        redis_stream_prefix="test",
        dashboard_snapshot_persistence_enabled=False,
        strategy_history_persistence_enabled=False,
        schwab_stream_symbol_stale_after_seconds=1.0,
        schwab_stream_symbol_stale_after_seconds_without_position=30.0,
        schwab_stream_symbol_resubscribe_interval_seconds=1.0,
    )
    service = StrategyEngineService(settings=settings, redis_client=FakeRedis())
    runtime = service.state.bots["macd_30s"]
    runtime.set_watchlist(["FTFT"])
    fixed_now = datetime(2026, 4, 24, 20, 0, 0, tzinfo=UTC)
    service._schwab_symbol_active_first_seen_at["FTFT"] = fixed_now - timedelta(seconds=40)
    service._schwab_stream_disconnected_since = fixed_now - timedelta(seconds=40)

    class FakeStreamClient:
        connected = False

        async def force_resubscribe(self) -> None:
            return None

    service._schwab_stream_client = FakeStreamClient()
    monkeypatch.setattr("project_mai_tai.services.strategy_engine_app.utcnow", lambda: fixed_now)

    activity_count = await service._monitor_schwab_symbol_health()

    assert activity_count == 1
    assert service._schwab_stale_symbols == {"FTFT"}
    assert runtime.data_health_summary()["status"] == "critical"
    assert runtime.data_health_summary()["halted_symbols"] == ["FTFT"]


def test_generic_market_data_never_targets_schwab_native_bot_when_stream_is_stale() -> None:
    settings = make_test_settings(
        strategy_macd_30s_broker_provider="schwab",
        strategy_macd_1m_enabled=True,
        redis_stream_prefix="test",
        dashboard_snapshot_persistence_enabled=False,
        strategy_history_persistence_enabled=False,
        schwab_stream_symbol_stale_after_seconds=1.0,
    )
    service = StrategyEngineService(settings=settings, redis_client=FakeRedis())
    old = datetime.now(UTC) - timedelta(seconds=40)
    service._schwab_symbol_last_stream_trade_at["ENVB"] = old
    service._schwab_symbol_last_stream_quote_at["ENVB"] = old

    assert "macd_30s" not in service._generic_market_data_strategy_codes("ENVB")


def test_snapshot_batch_does_not_push_polygon_quotes_into_schwab_native_macd_30s(monkeypatch) -> None:
    state = StrategyEngineState(
        settings=make_test_settings(
            strategy_macd_30s_enabled=True,
            strategy_macd_30s_broker_provider="schwab",
            strategy_macd_1m_enabled=True,
        ),
        now_provider=fixed_now,
    )
    state.confirmed_scanner._confirmed = [
        {
            "ticker": "UGRO",
            "confirmed_at": "10:00:00 AM ET",
            "entry_price": 2.25,
            "price": 2.4,
            "change_pct": 24.5,
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

    state.process_snapshot_batch(
        [
            snapshot_from_payload(
                MarketSnapshotPayload(
                    symbol="UGRO",
                    last_trade_price=Decimal("2.40"),
                    bid_price=Decimal("2.39"),
                    ask_price=Decimal("2.40"),
                    day_close=Decimal("2.40"),
                    day_volume=900_000,
                )
            )
        ],
        {"UGRO": ReferenceData(shares_outstanding=50_000, avg_daily_volume=390_000)},
    )

    assert "UGRO" not in state.bots["macd_30s"].latest_quotes
    assert state.bots["macd_1m"].latest_quotes["UGRO"] == {"bid": 2.39, "ask": 2.4}


def test_snapshot_batch_does_not_push_polygon_quotes_into_schwab_backed_tos(monkeypatch) -> None:
    state = StrategyEngineState(
        settings=make_test_settings(
            strategy_macd_1m_enabled=True,
            strategy_tos_enabled=True,
            strategy_tos_broker_provider="schwab",
        ),
        now_provider=fixed_now,
    )
    state.confirmed_scanner._confirmed = [
        {
            "ticker": "UGRO",
            "confirmed_at": "10:00:00 AM ET",
            "entry_price": 2.25,
            "price": 2.4,
            "change_pct": 24.5,
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

    state.process_snapshot_batch(
        [
            snapshot_from_payload(
                MarketSnapshotPayload(
                    symbol="UGRO",
                    last_trade_price=Decimal("2.40"),
                    bid_price=Decimal("2.39"),
                    ask_price=Decimal("2.40"),
                    day_close=Decimal("2.40"),
                    day_volume=900_000,
                )
            )
        ],
        {"UGRO": ReferenceData(shares_outstanding=50_000, avg_daily_volume=390_000)},
    )

    assert "UGRO" not in state.bots["tos"].latest_quotes
    assert state.bots["macd_1m"].latest_quotes["UGRO"] == {"bid": 2.39, "ask": 2.4}


@pytest.mark.asyncio
async def test_strategy_state_snapshot_persists_last_nonempty_confirmed_snapshot() -> None:
    redis = FakeRedis()
    session_factory = build_test_session_factory()
    service = StrategyEngineService(
        settings=make_test_settings(redis_stream_prefix="test", dashboard_snapshot_persistence_enabled=True),
        redis_client=redis,
        session_factory=session_factory,
    )
    service.state.current_confirmed = [
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
            "first_spike_time": "09:55:00 AM ET",
            "squeeze_count": 2,
            "confirmation_path": "PATH_B_2SQ",
            "headline": "Quantum Biopharma Wins Hospital Supply Agreement",
            "catalyst": "DEAL/CONTRACT",
            "catalyst_type": "DEAL/CONTRACT",
            "sentiment": "bullish",
            "direction": "bullish",
            "news_url": "https://example.com/ugro-news",
            "news_date": "03/27 05:05PM ET",
            "news_window_start": "03/27 04:00PM ET",
            "catalyst_reason": "Bullish DEAL/CONTRACT catalyst across 2 article(s), latest 55m old.",
            "catalyst_confidence": 0.91,
            "article_count": 3,
            "real_catalyst_article_count": 2,
            "freshness_minutes": 55,
            "is_generic_roundup": False,
            "has_real_catalyst": True,
            "path_a_eligible": True,
        }
    ]
    service.state.confirmed_scanner.seed_confirmed_candidates(
        [
            {
                "ticker": "UGRO",
                "rank_score": 72.0,
                "confirmed_at": "10:00:00 AM ET",
                "entry_price": 2.25,
                "price": 2.40,
                "change_pct": 12.5,
                "volume": 900_000,
                "rvol": 6.2,
                "shares_outstanding": 50_000,
                "bid": 2.39,
                "ask": 2.40,
                "spread": 0.01,
                "spread_pct": 0.42,
                "squeeze_count": 2,
                "confirmation_path": "PATH_B_2SQ",
            },
            {
                "ticker": "ELAB",
                "rank_score": 82.0,
                "confirmed_at": "10:05:00 AM ET",
                "entry_price": 3.05,
                "price": 3.82,
                "change_pct": 128.7,
                "volume": 26_400_000,
                "rvol": 13.0,
                "shares_outstanding": 541_461,
                "bid": 3.81,
                "ask": 3.82,
                "spread": 0.01,
                "spread_pct": 0.26,
                "squeeze_count": 2,
                "confirmation_path": "PATH_B_2SQ",
            },
        ]
    )

    await service._publish_strategy_state_snapshot()

    with session_factory() as session:
        snapshot = session.scalar(
            select(DashboardSnapshot).where(
                DashboardSnapshot.snapshot_type == "scanner_confirmed_last_nonempty"
            )
        )

    assert snapshot is not None
    assert snapshot.payload["top_confirmed"][0]["ticker"] == "UGRO"
    assert len(snapshot.payload["all_confirmed_candidates"]) == 2
    assert snapshot.payload["scanner_session_start_utc"]
    assert snapshot.payload["top_confirmed"][0]["headline"] == "Quantum Biopharma Wins Hospital Supply Agreement"
    assert snapshot.payload["top_confirmed"][0]["path_a_eligible"] is True


def test_seeded_confirmed_candidates_are_revalidated_into_fresh_top_confirmed(monkeypatch) -> None:
    session_factory = build_test_session_factory()
    with session_factory() as session:
        session.add(
            DashboardSnapshot(
                snapshot_type="scanner_confirmed_last_nonempty",
                payload={
                    "persisted_at": datetime(2026, 3, 30, 10, 5, tzinfo=UTC).isoformat(),
                    "scanner_session_start_utc": datetime(2026, 3, 30, 8, 0, tzinfo=UTC).isoformat(),
                    "all_confirmed_candidates": [
                        {
                            "ticker": "UGRO",
                            "rank_score": 72.0,
                            "confirmed_at": "10:00:00 AM ET",
                            "entry_price": 2.25,
                            "price": 2.40,
                            "change_pct": 24.5,
                            "volume": 900_000,
                            "rvol": 6.2,
                            "shares_outstanding": 50_000,
                            "bid": 2.39,
                            "ask": 2.40,
                            "spread": 0.01,
                            "spread_pct": 0.42,
                            "squeeze_count": 2,
                            "confirmation_path": "PATH_B_2SQ",
                        },
                        {
                            "ticker": "ELAB",
                            "rank_score": 82.0,
                            "confirmed_at": "10:05:00 AM ET",
                            "entry_price": 3.05,
                            "price": 3.82,
                            "change_pct": 128.7,
                            "volume": 26_400_000,
                            "rvol": 13.0,
                            "shares_outstanding": 541_461,
                            "bid": 3.81,
                            "ask": 3.82,
                            "spread": 0.01,
                            "spread_pct": 0.26,
                            "squeeze_count": 2,
                            "confirmation_path": "PATH_B_2SQ",
                        },
                    ],
                    "top_confirmed": [
                            {
                                "ticker": "UGRO",
                                "rank_score": 72.0,
                                "confirmed_at": "10:00:00 AM ET",
                                "entry_price": 2.25,
                                "price": 2.40,
                                "change_pct": 24.5,
                                "volume": 900_000,
                                "rvol": 6.2,
                                "shares_outstanding": 50_000,
                            "bid": 2.39,
                            "ask": 2.40,
                            "spread": 0.01,
                            "spread_pct": 0.42,
                            "squeeze_count": 2,
                            "confirmation_path": "PATH_B_2SQ",
                        }
                    ]
                },
            )
        )
        session.commit()

    monkeypatch.setattr(
        "project_mai_tai.services.strategy_engine_app.utcnow",
        lambda: datetime(2026, 3, 30, 14, 0, tzinfo=UTC),
    )

    service = StrategyEngineService(
        settings=make_test_settings(
            redis_stream_prefix="test",
            dashboard_snapshot_persistence_enabled=True,
            strategy_macd_1m_enabled=True,
            strategy_tos_enabled=True,
            strategy_runner_enabled=True,
        ),
        redis_client=FakeRedis(),
        session_factory=session_factory,
    )
    service._seed_confirmed_candidates_from_dashboard_snapshot()

    assert [item["ticker"] for item in service.state.all_confirmed] == ["UGRO", "ELAB"]
    assert [item["ticker"] for item in service.state.current_confirmed] == ["ELAB", "UGRO"]
    for code in ("macd_30s", "macd_1m", "tos", "runner"):
        assert service.state.bots[code].watchlist == {"ELAB", "UGRO"}
    assert set(service.state.market_data_symbols()) == {"ELAB", "UGRO"}

    summary = service.state.process_snapshot_batch(
        [
            snapshot_from_payload(make_snapshot_payload(symbol="UGRO", price=2.62, volume=1_100_000)),
            snapshot_from_payload(make_snapshot_payload(symbol="ELAB", price=3.90, volume=28_000_000)),
        ],
        {
            "UGRO": ReferenceData(shares_outstanding=50_000, avg_daily_volume=390_000),
            "ELAB": ReferenceData(shares_outstanding=541_461, avg_daily_volume=1_941_514.84),
        },
    )

    assert service.state._seeded_confirmed_pending_revalidation is False
    assert [item["ticker"] for item in service.state.confirmed_scanner.get_all_confirmed()] == ["UGRO", "ELAB"]
    assert [item["ticker"] for item in summary["all_confirmed"]] == ["UGRO", "ELAB"]
    assert summary["watchlist"] == ["ELAB", "UGRO"]
    assert [item["ticker"] for item in summary["top_confirmed"]] == ["ELAB", "UGRO"]
    assert summary["top_confirmed"][0]["price"] == 3.90
    assert service.state.confirmed_scanner.get_all_confirmed()[0]["volume"] == 1_100_000


def test_seeded_confirmed_candidates_drop_when_missing_from_fresh_snapshots() -> None:
    session_factory = build_test_session_factory()
    with session_factory() as session:
        session.add(
            DashboardSnapshot(
                snapshot_type="scanner_confirmed_last_nonempty",
                payload={
                    "persisted_at": datetime(2026, 3, 30, 10, 5, tzinfo=UTC).isoformat(),
                    "scanner_session_start_utc": datetime(2026, 3, 30, 8, 0, tzinfo=UTC).isoformat(),
                    "top_confirmed": [
                        {
                            "ticker": "UGRO",
                            "rank_score": 72.0,
                            "confirmed_at": "10:00:00 AM ET",
                            "entry_price": 2.25,
                            "price": 2.40,
                            "change_pct": 12.5,
                            "volume": 900_000,
                            "rvol": 6.2,
                            "shares_outstanding": 50_000,
                            "bid": 2.39,
                            "ask": 2.40,
                            "spread": 0.01,
                            "spread_pct": 0.42,
                            "squeeze_count": 2,
                            "confirmation_path": "PATH_B_2SQ",
                        }
                    ]
                },
            )
        )
        session.commit()

    service = StrategyEngineService(
        settings=make_test_settings(
            redis_stream_prefix="test",
            dashboard_snapshot_persistence_enabled=True,
            strategy_runner_enabled=True,
        ),
        redis_client=FakeRedis(),
        session_factory=session_factory,
    )
    service._seed_confirmed_candidates_from_dashboard_snapshot()

    summary = service.state.process_snapshot_batch(
        [snapshot_from_payload(make_snapshot_payload(symbol="ELAB", price=2.62, volume=1_100_000))],
        {"ELAB": ReferenceData(shares_outstanding=50_000, avg_daily_volume=390_000)},
    )

    assert service.state._seeded_confirmed_pending_revalidation is False
    assert summary["watchlist"] == []
    assert summary["top_confirmed"] == []


def test_seeded_confirmed_candidates_restore_watchlist_from_all_confirmed_when_top_confirmed_empty() -> None:
    session_factory = build_test_session_factory()
    with session_factory() as session:
        session.add(
            DashboardSnapshot(
                snapshot_type="scanner_confirmed_last_nonempty",
                payload={
                    "persisted_at": datetime(2026, 3, 30, 10, 5, tzinfo=UTC).isoformat(),
                    "scanner_session_start_utc": datetime(2026, 3, 30, 8, 0, tzinfo=UTC).isoformat(),
                    "all_confirmed_candidates": [
                        {
                            "ticker": "BIYA",
                            "rank_score": 0.0,
                            "confirmed_at": "10:00:00 AM ET",
                            "entry_price": 2.25,
                            "price": 2.40,
                            "change_pct": 12.5,
                            "volume": 900_000,
                            "rvol": 6.2,
                            "shares_outstanding": 50_000,
                            "bid": 2.39,
                            "ask": 2.40,
                            "spread": 0.01,
                            "spread_pct": 0.42,
                            "squeeze_count": 2,
                            "confirmation_path": "PATH_B_2SQ",
                        }
                    ],
                    "top_confirmed": [],
                },
            )
        )
        session.commit()

    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(
            "project_mai_tai.services.strategy_engine_app.utcnow",
            lambda: datetime(2026, 3, 30, 14, 0, tzinfo=UTC),
        )

        service = StrategyEngineService(
            settings=make_test_settings(
                redis_stream_prefix="test",
                dashboard_snapshot_persistence_enabled=True,
                strategy_macd_1m_enabled=True,
                strategy_tos_enabled=True,
                strategy_runner_enabled=True,
            ),
            redis_client=FakeRedis(),
            session_factory=session_factory,
        )

        service._seed_confirmed_candidates_from_dashboard_snapshot()

        assert [item["ticker"] for item in service.state.all_confirmed] == ["BIYA"]
        assert [item["ticker"] for item in service.state.current_confirmed] == ["BIYA"]
        for code in ("macd_30s", "macd_1m", "tos", "runner"):
            assert service.state.bots[code].watchlist == {"BIYA"}


def test_restore_confirmed_runtime_view_prefers_persisted_bot_handoff_state() -> None:
    state = StrategyEngineState(
        settings=make_test_settings(
            strategy_macd_1m_enabled=True,
            strategy_tos_enabled=True,
            strategy_runner_enabled=True,
        ),
        now_provider=fixed_now,
    )

    state.restore_confirmed_runtime_view(
        [{"ticker": "SST"}],
        bot_handoff_symbols_by_strategy={
            "macd_30s": ["SST", "ELAB"],
            "macd_1m": ["SST"],
            "tos": ["SST"],
            "runner": ["SST"],
        },
        bot_handoff_history_by_strategy={
            "macd_30s": ["SST", "ELAB"],
            "macd_1m": ["SST"],
            "tos": ["SST"],
            "runner": ["SST"],
        },
    )

    assert state.bots["macd_30s"].watchlist == {"SST", "ELAB"}
    assert state.bots["macd_1m"].watchlist == {"SST"}
    assert state.bots["tos"].watchlist == {"SST"}
    assert state.bots["runner"].watchlist == {"SST"}


def test_seeded_confirmed_candidates_skip_prior_session_snapshot(monkeypatch) -> None:
    session_factory = build_test_session_factory()
    with session_factory() as session:
        session.add(
            DashboardSnapshot(
                snapshot_type="scanner_confirmed_last_nonempty",
                payload={
                    "persisted_at": datetime(2026, 3, 30, 1, 0, tzinfo=UTC).isoformat(),
                    "top_confirmed": [
                        {
                            "ticker": "ELAB",
                            "rank_score": 72.0,
                            "confirmed_at": "06:03:59 PM ET",
                            "entry_price": 3.73,
                            "price": 3.32,
                            "change_pct": 98.8,
                            "volume": 249_300,
                        }
                    ],
                },
            )
        )
        session.commit()

    monkeypatch.setattr(
        "project_mai_tai.services.strategy_engine_app.utcnow",
        lambda: datetime(2026, 3, 31, 9, 0, tzinfo=UTC),
    )

    service = StrategyEngineService(
        settings=make_test_settings(redis_stream_prefix="test", dashboard_snapshot_persistence_enabled=True),
        redis_client=FakeRedis(),
        session_factory=session_factory,
    )
    service._seed_confirmed_candidates_from_dashboard_snapshot()

    assert service.state.confirmed_scanner.get_all_confirmed() == []
    assert service.state._seeded_confirmed_pending_revalidation is False


def test_seeded_confirmed_candidates_skip_unmarked_snapshot_even_if_recent(monkeypatch) -> None:
    session_factory = build_test_session_factory()
    with session_factory() as session:
        session.add(
            DashboardSnapshot(
                snapshot_type="scanner_confirmed_last_nonempty",
                payload={
                    "persisted_at": datetime(2026, 3, 30, 10, 5, tzinfo=UTC).isoformat(),
                    "all_confirmed_candidates": [{"ticker": "GNLN"}],
                },
            )
        )
        session.commit()

    monkeypatch.setattr(
        "project_mai_tai.services.strategy_engine_app.utcnow",
        lambda: datetime(2026, 3, 30, 10, 10, tzinfo=UTC),
    )

    service = StrategyEngineService(
        settings=make_test_settings(redis_stream_prefix="test", dashboard_snapshot_persistence_enabled=True),
        redis_client=FakeRedis(),
        session_factory=session_factory,
    )
    service._seed_confirmed_candidates_from_dashboard_snapshot()

    assert service.state.confirmed_scanner.get_all_confirmed() == []
    assert service.state.bots["macd_30s"].watchlist == set()


def test_publish_strategy_state_persists_scanner_cycle_history_snapshot() -> None:
    session_factory = build_test_session_factory()
    service = StrategyEngineService(
        settings=make_test_settings(
            redis_stream_prefix="test",
            dashboard_snapshot_persistence_enabled=True,
            dashboard_scanner_history_retention=10,
        ),
        redis_client=FakeRedis(),
        session_factory=session_factory,
    )
    service.state.current_confirmed = [
        {
            "ticker": "ELAB",
            "rank_score": 82.0,
            "confirmed_at": "10:05:00 AM ET",
            "entry_price": 3.05,
            "price": 3.82,
            "change_pct": 128.7,
            "volume": 26_400_000,
            "rvol": 13.0,
            "shares_outstanding": 541_461,
            "confirmation_path": "PATH_B_2SQ",
        }
    ]
    service.state.confirmed_scanner.seed_confirmed_candidates(list(service.state.current_confirmed))
    service.state.five_pillars = [
        {
            "ticker": "ELAB",
            "price": 3.82,
            "change_pct": 128.7,
            "volume": 26_400_000,
            "rvol": 13.0,
            "shares_outstanding": 541_461,
            "data_age_secs": 0,
        }
    ]
    service.state.top_gainers = [
        {
            "ticker": "ELAB",
            "price": 3.82,
            "change_pct": 128.7,
            "volume": 26_400_000,
            "rvol": 13.0,
            "shares_outstanding": 541_461,
            "data_age_secs": 0,
        }
    ]
    service.state.restore_confirmed_runtime_view(list(service.state.current_confirmed))

    awaitable = service._publish_strategy_state_snapshot()
    import asyncio
    asyncio.run(awaitable)

    with session_factory() as session:
        snapshots = session.scalars(
            select(DashboardSnapshot)
            .where(DashboardSnapshot.snapshot_type == "scanner_cycle_history")
            .order_by(DashboardSnapshot.created_at)
        ).all()

    assert len(snapshots) == 1
    payload = snapshots[0].payload
    assert payload["watchlist"] == ["ELAB"]
    assert payload["bot_handoff_symbols_by_strategy"]["macd_30s"] == ["ELAB"]
    assert payload["all_confirmed_tickers"] == ["ELAB"]
    assert payload["top_confirmed_tickers"] == ["ELAB"]
    assert payload["five_pillars_tickers"] == ["ELAB"]
    assert payload["top_gainers_tickers"] == ["ELAB"]
    assert payload["top_confirmed"][0]["confirmed_at"] == "10:05:00 AM ET"


def test_scanner_cycle_history_retention_and_dedup() -> None:
    session_factory = build_test_session_factory()
    service = StrategyEngineService(
        settings=make_test_settings(
            redis_stream_prefix="test",
            dashboard_snapshot_persistence_enabled=True,
            dashboard_scanner_history_retention=2,
        ),
        redis_client=FakeRedis(),
        session_factory=session_factory,
    )

    summary_one = {
        "top_confirmed": [],
        "five_pillars": [{"ticker": "ELAB", "price": 3.8, "change_pct": 100, "volume": 1_000_000, "rvol": 5, "shares_outstanding": 10_000_000, "data_age_secs": 0}],
        "top_gainers": [],
        "watchlist": [],
        "cycle_count": 1,
    }
    summary_two = {
        "top_confirmed": [{"ticker": "ELAB", "confirmed_at": "10:05:00 AM ET", "confirmation_path": "PATH_B_2SQ", "rank_score": 82.0, "price": 3.82, "change_pct": 128.7, "volume": 26_400_000, "rvol": 13.0}],
        "five_pillars": [{"ticker": "ELAB", "price": 3.82, "change_pct": 128.7, "volume": 26_400_000, "rvol": 13.0, "shares_outstanding": 541_461, "data_age_secs": 0}],
        "top_gainers": [{"ticker": "ELAB", "price": 3.82, "change_pct": 128.7, "volume": 26_400_000, "rvol": 13.0, "shares_outstanding": 541_461, "data_age_secs": 0}],
        "watchlist": ["ELAB"],
        "cycle_count": 2,
    }
    summary_three = {
        "top_confirmed": [{"ticker": "MSTP", "confirmed_at": "10:19:06 AM ET", "confirmation_path": "PATH_B_2SQ", "rank_score": 55.0, "price": 2.45, "change_pct": 25.0, "volume": 8_000_000, "rvol": 3.3}],
        "five_pillars": [{"ticker": "MSTP", "price": 2.45, "change_pct": 25.0, "volume": 8_000_000, "rvol": 3.3, "shares_outstanding": 6_000_000, "data_age_secs": 0}],
        "top_gainers": [],
        "watchlist": ["MSTP"],
        "cycle_count": 3,
    }

    service._persist_scanner_snapshots(summary_one)
    service._persist_scanner_snapshots(summary_one)
    service._persist_scanner_snapshots(summary_two)
    service._persist_scanner_snapshots(summary_three)

    with session_factory() as session:
        snapshots = session.scalars(
            select(DashboardSnapshot)
            .where(DashboardSnapshot.snapshot_type == "scanner_cycle_history")
            .order_by(DashboardSnapshot.created_at)
        ).all()

    assert len(snapshots) == 2
    assert snapshots[0].payload["top_confirmed_tickers"] == ["ELAB"]
    assert snapshots[1].payload["top_confirmed_tickers"] == ["MSTP"]


def test_strategy_state_rolls_scanner_session_at_four_am_et() -> None:
    current = datetime(2026, 3, 31, 23, 59, tzinfo=UTC)

    def now_provider() -> datetime:
        return current

    state = StrategyEngineState(now_provider=now_provider)
    state.current_confirmed = [{"ticker": "MASK"}]
    state.five_pillars = [{"ticker": "MASK"}]
    state.top_gainers = [{"ticker": "MASK"}]
    state.top_gainer_changes = [{"ticker": "MASK"}]
    state.recent_alerts = [{"ticker": "MASK"}]
    state.latest_snapshots = {"MASK": object()}  # type: ignore[assignment]
    state._first_seen_by_ticker["MASK"] = "03:01:46 PM ET"
    state._seeded_confirmed_pending_revalidation = True
    state.confirmed_scanner.seed_confirmed_candidates([{"ticker": "MASK"}])

    current = datetime(2026, 4, 1, 8, 1, tzinfo=UTC)
    summary = state.process_snapshot_batch([], {})

    assert summary["top_confirmed"] == []
    assert summary["watchlist"] == []
    assert state.confirmed_scanner.get_all_confirmed() == []
    assert state.five_pillars == []
    assert state.top_gainers == []
    assert state.top_gainer_changes == []
    assert state.recent_alerts == []
    assert state.latest_snapshots == {}
    assert state._first_seen_by_ticker == {}
    assert state._seeded_confirmed_pending_revalidation is False


def test_current_scanner_session_start_uses_prior_day_before_four_am_et() -> None:
    assert current_scanner_session_start_utc(datetime(2026, 4, 14, 3, 59, tzinfo=UTC)) == datetime(
        2026,
        4,
        13,
        8,
        0,
        tzinfo=UTC,
    )
    assert current_scanner_session_start_utc(datetime(2026, 4, 14, 7, 59, tzinfo=UTC)) == datetime(
        2026,
        4,
        13,
        8,
        0,
        tzinfo=UTC,
    )
    assert current_scanner_session_start_utc(datetime(2026, 4, 14, 8, 1, tzinfo=UTC)) == datetime(
        2026,
        4,
        14,
        8,
        0,
        tzinfo=UTC,
    )


def test_strategy_state_does_not_roll_scanner_session_at_midnight_et() -> None:
    current = datetime(2026, 4, 14, 3, 59, tzinfo=UTC)

    def now_provider() -> datetime:
        return current

    state = StrategyEngineState(now_provider=now_provider)
    state.current_confirmed = [{"ticker": "MASK"}]
    state.all_confirmed = [{"ticker": "MASK"}]
    state.confirmed_scanner.seed_confirmed_candidates([{"ticker": "MASK"}])

    current = datetime(2026, 4, 14, 4, 1, tzinfo=UTC)
    summary = state.process_snapshot_batch([], {})

    assert [item["ticker"] for item in summary["all_confirmed"]] == ["MASK"]
    assert [item["ticker"] for item in summary["top_confirmed"]] == ["MASK"]
    assert [item["ticker"] for item in state.confirmed_scanner.get_all_confirmed()] == ["MASK"]


def test_strategy_service_restores_runtime_positions_and_pending_from_database() -> None:
    session_factory = build_test_session_factory()
    with session_factory() as session:
        strategy_macd = Strategy(code="macd_30s", name="MACD Bot", execution_mode="paper", metadata_json={})
        strategy_runner = Strategy(code="runner", name="Runner Bot", execution_mode="paper", metadata_json={})
        account_macd = BrokerAccount(name="paper:macd_30s", provider="alpaca", environment="test")
        account_runner = BrokerAccount(name="paper:tos_runner_shared", provider="alpaca", environment="test")
        session.add_all([strategy_macd, strategy_runner, account_macd, account_runner])
        session.flush()

        session.add_all(
            [
                VirtualPosition(
                    strategy_id=strategy_macd.id,
                    broker_account_id=account_macd.id,
                    symbol="UGRO",
                    quantity=Decimal("10"),
                    average_price=Decimal("2.55"),
                ),
                VirtualPosition(
                    strategy_id=strategy_runner.id,
                    broker_account_id=account_runner.id,
                    symbol="IPW",
                    quantity=Decimal("100"),
                    average_price=Decimal("1.61"),
                ),
            ]
        )

        intent_open = TradeIntent(
            strategy_id=strategy_macd.id,
            broker_account_id=account_macd.id,
            symbol="ELAB",
            side="buy",
            intent_type="open",
            quantity=Decimal("10"),
            reason="ENTRY",
            status="accepted",
            payload={"metadata": {"path": "P1_MACD_CROSS"}},
        )
        intent_close = TradeIntent(
            strategy_id=strategy_runner.id,
            broker_account_id=account_runner.id,
            symbol="IPW",
            side="sell",
            intent_type="close",
            quantity=Decimal("100"),
            reason="EXIT",
            status="accepted",
            payload={"metadata": {}},
        )
        intent_scale = TradeIntent(
            strategy_id=strategy_macd.id,
            broker_account_id=account_macd.id,
            symbol="UGRO",
            side="sell",
            intent_type="scale",
            quantity=Decimal("5"),
            reason="SCALE_FAST4",
            status="accepted",
            payload={"metadata": {"level": "FAST4"}},
        )
        session.add_all([intent_open, intent_close, intent_scale])
        session.flush()

        session.add_all(
            [
                BrokerOrder(
                    intent_id=intent_open.id,
                    strategy_id=strategy_macd.id,
                    broker_account_id=account_macd.id,
                    client_order_id="open-order",
                    symbol="ELAB",
                    side="buy",
                    order_type="limit",
                    time_in_force="day",
                    quantity=Decimal("10"),
                    status="accepted",
                    payload={"path": "P1_MACD_CROSS"},
                ),
                BrokerOrder(
                    intent_id=intent_close.id,
                    strategy_id=strategy_runner.id,
                    broker_account_id=account_runner.id,
                    client_order_id="close-order",
                    symbol="IPW",
                    side="sell",
                    order_type="limit",
                    time_in_force="day",
                    quantity=Decimal("100"),
                    status="accepted",
                    payload={},
                ),
                BrokerOrder(
                    intent_id=intent_scale.id,
                    strategy_id=strategy_macd.id,
                    broker_account_id=account_macd.id,
                    client_order_id="scale-order",
                    symbol="UGRO",
                    side="sell",
                    order_type="limit",
                    time_in_force="day",
                    quantity=Decimal("5"),
                    status="accepted",
                    payload={"level": "FAST4"},
                ),
            ]
        )
        session.commit()

    service = StrategyEngineService(
        settings=make_test_settings(redis_stream_prefix="test", dashboard_snapshot_persistence_enabled=True),
        redis_client=FakeRedis(),
        session_factory=session_factory,
    )

    service._restore_runtime_state_from_database()

    macd = service.state.bots["macd_30s"]
    runner = service.state.bots["runner"]

    assert macd.positions.get_position("UGRO") is not None
    assert macd.positions.get_position("UGRO").quantity == 10
    assert "ELAB" in macd.pending_open_symbols
    assert ("UGRO", "FAST4") in macd.pending_scale_levels
    assert runner.summary()["positions"][0]["ticker"] == "IPW"
    assert runner.summary()["pending_close_symbols"] == ["IPW"]


def test_strategy_service_reconcile_restores_missing_runtime_position_from_virtual_state() -> None:
    session_factory = build_test_session_factory()
    with session_factory() as session:
        strategy_macd = Strategy(code="macd_30s_reclaim", name="Reclaim", execution_mode="paper", metadata_json={})
        account_macd = BrokerAccount(
            name="paper:macd_30s_reclaim",
            provider="alpaca",
            environment="test",
        )
        session.add_all([strategy_macd, account_macd])
        session.flush()
        session.add(
            VirtualPosition(
                strategy_id=strategy_macd.id,
                broker_account_id=account_macd.id,
                symbol="UGRO",
                quantity=Decimal("25"),
                average_price=Decimal("2.55"),
            )
        )
        session.commit()

    service = StrategyEngineService(
        settings=make_test_settings(
            redis_stream_prefix="test",
            dashboard_snapshot_persistence_enabled=True,
            strategy_macd_30s_reclaim_enabled=True,
        ),
        redis_client=FakeRedis(),
        session_factory=session_factory,
    )
    bot = service.state.bots["macd_30s_reclaim"]
    assert bot.positions.get_position("UGRO") is None

    changed = service._reconcile_runtime_state_from_database(log_when_changed=False)

    assert changed is True
    restored = bot.positions.get_position("UGRO")
    assert restored is not None
    assert restored.quantity == 25
    assert restored.entry_price == 2.55


def test_strategy_service_reconcile_restores_runtime_position_path_from_latest_open_intent() -> None:
    session_factory = build_test_session_factory()
    with session_factory() as session:
        strategy_macd = Strategy(code="macd_30s_reclaim", name="Reclaim", execution_mode="paper", metadata_json={})
        account_macd = BrokerAccount(
            name="paper:macd_30s_reclaim",
            provider="alpaca",
            environment="test",
        )
        session.add_all([strategy_macd, account_macd])
        session.flush()
        session.add(
            VirtualPosition(
                strategy_id=strategy_macd.id,
                broker_account_id=account_macd.id,
                symbol="UGRO",
                quantity=Decimal("25"),
                average_price=Decimal("2.55"),
            )
        )
        session.add(
            TradeIntent(
                strategy_id=strategy_macd.id,
                broker_account_id=account_macd.id,
                symbol="UGRO",
                side="buy",
                intent_type="open",
                quantity=Decimal("25"),
                reason="ENTRY_P5_PULLBACK",
                status="filled",
                payload={"metadata": {"path": "P5_PULLBACK"}},
            )
        )
        session.commit()

    service = StrategyEngineService(
        settings=make_test_settings(
            redis_stream_prefix="test",
            dashboard_snapshot_persistence_enabled=True,
            strategy_macd_30s_reclaim_enabled=True,
        ),
        redis_client=FakeRedis(),
        session_factory=session_factory,
    )

    changed = service._reconcile_runtime_state_from_database(log_when_changed=False)

    assert changed is True
    restored = service.state.bots["macd_30s_reclaim"].positions.get_position("UGRO")
    assert restored is not None
    assert restored.entry_path == "P5_PULLBACK"


def test_strategy_service_reconcile_clears_stale_runtime_position_without_virtual_backing() -> None:
    session_factory = build_test_session_factory()
    with session_factory() as session:
        strategy_macd = Strategy(code="macd_30s_reclaim", name="Reclaim", execution_mode="paper", metadata_json={})
        account_macd = BrokerAccount(
            name="paper:macd_30s_reclaim",
            provider="alpaca",
            environment="test",
        )
        session.add_all([strategy_macd, account_macd])
        session.commit()

    service = StrategyEngineService(
        settings=make_test_settings(
            redis_stream_prefix="test",
            dashboard_snapshot_persistence_enabled=True,
            strategy_macd_30s_reclaim_enabled=True,
        ),
        redis_client=FakeRedis(),
        session_factory=session_factory,
    )
    bot = service.state.bots["macd_30s_reclaim"]
    bot.positions.open_position("UGRO", 2.55, quantity=25, path="PRETRIGGER_RECLAIM")
    bot.pending_close_symbols.add("UGRO")

    changed = service._reconcile_runtime_state_from_database(log_when_changed=False)

    assert changed is True
    assert bot.positions.get_position("UGRO") is None
    assert "UGRO" not in bot.pending_close_symbols


def test_restore_runtime_state_reseeds_schwab_bar_history_for_midday_restart() -> None:
    session_factory = build_test_session_factory()
    start_30s = datetime(2026, 3, 28, 13, 0, tzinfo=UTC)
    start_60s = datetime(2026, 3, 28, 13, 0, tzinfo=UTC)

    with session_factory() as session:
        for index in range(50):
            close = Decimal(str(2.00 + index * 0.01))
            session.add(
                StrategyBarHistory(
                    strategy_code="macd_30s",
                    symbol="UGRO",
                    interval_secs=30,
                    bar_time=start_30s + timedelta(seconds=index * 30),
                    open_price=close - Decimal("0.01"),
                    high_price=close + Decimal("0.02"),
                    low_price=close - Decimal("0.02"),
                    close_price=close,
                    volume=20_000 + index * 100,
                    trade_count=10,
                )
            )
        for index in range(35):
            close = Decimal(str(3.00 + index * 0.01))
            session.add(
                StrategyBarHistory(
                    strategy_code="tos",
                    symbol="UGRO",
                    interval_secs=60,
                    bar_time=start_60s + timedelta(seconds=index * 60),
                    open_price=close - Decimal("0.01"),
                    high_price=close + Decimal("0.02"),
                    low_price=close - Decimal("0.02"),
                    close_price=close,
                    volume=30_000 + index * 100,
                    trade_count=12,
                )
            )
        session.commit()

    service = StrategyEngineService(
        settings=make_test_settings(
            redis_stream_prefix="test",
            dashboard_snapshot_persistence_enabled=True,
            strategy_macd_30s_broker_provider="schwab",
            strategy_tos_enabled=True,
            strategy_tos_broker_provider="schwab",
        ),
        redis_client=FakeRedis(),
        session_factory=session_factory,
    )
    service.state.alert_engine.now_provider = fixed_now
    service.state.restore_confirmed_runtime_view([{"ticker": "UGRO"}])

    service._restore_runtime_state_from_database()

    macd_runtime = service.state.bots["macd_30s"]
    tos_runtime = service.state.bots["tos"]

    assert macd_runtime.builder_manager.get_builder("UGRO") is not None
    assert tos_runtime.builder_manager.get_builder("UGRO") is not None
    macd_builder = macd_runtime.builder_manager.get_builder("UGRO")
    tos_builder = tos_runtime.builder_manager.get_builder("UGRO")

    assert macd_builder.get_current_price() == pytest.approx(2.49)
    assert tos_builder.get_current_price() == pytest.approx(3.34)
    assert macd_builder._current_bar is None
    assert tos_builder._current_bar is None

    macd_bars = macd_builder.get_bars_as_dicts()
    tos_bars = tos_builder.get_bars_as_dicts()

    assert macd_runtime.indicator_engine.calculate(macd_bars) is not None
    assert tos_runtime.indicator_engine.calculate(tos_bars) is not None


def test_restore_runtime_state_reseeds_full_30s_session_history_for_session_aware_vwap() -> None:
    session_factory = build_test_session_factory()
    start_30s = datetime(2026, 3, 28, 8, 0, tzinfo=UTC)

    with session_factory() as session:
        for index in range(120):
            close = Decimal(str(2.00 + index * 0.01))
            session.add(
                StrategyBarHistory(
                    strategy_code="macd_30s",
                    symbol="UGRO",
                    interval_secs=30,
                    bar_time=start_30s + timedelta(seconds=index * 30),
                    open_price=close - Decimal("0.01"),
                    high_price=close + Decimal("0.02"),
                    low_price=close - Decimal("0.02"),
                    close_price=close,
                    volume=20_000 + index * 100,
                    trade_count=10,
                )
            )
        session.commit()

    service = StrategyEngineService(
        settings=make_test_settings(
            redis_stream_prefix="test",
            dashboard_snapshot_persistence_enabled=True,
            strategy_macd_30s_broker_provider="schwab",
        ),
        redis_client=FakeRedis(),
        session_factory=session_factory,
    )
    service.state.alert_engine.now_provider = fixed_now
    service.state.restore_confirmed_runtime_view([{"ticker": "UGRO"}])

    service._restore_runtime_state_from_database()

    macd_runtime = service.state.bots["macd_30s"]
    macd_builder = macd_runtime.builder_manager.get_builder("UGRO")

    assert macd_builder is not None
    assert macd_builder._current_bar is None
    assert len(macd_builder.get_bars_as_dicts()) == 120
    assert macd_builder.get_current_price() == pytest.approx(3.19)


def test_seed_bars_populates_last_indicator_snapshot() -> None:
    service = StrategyEngineService(
        settings=make_test_settings(
            redis_stream_prefix="test",
            strategy_macd_30s_broker_provider="schwab",
        ),
        redis_client=FakeRedis(),
    )
    runtime = service.state.bots["macd_30s"]

    runtime.seed_bars(
        "UGRO",
        seed_trending_bars(
            count=60,
            start_timestamp=datetime(2026, 3, 28, 13, 0, tzinfo=UTC).timestamp(),
            interval_secs=30,
        ),
    )

    snapshot = runtime.last_indicators.get("UGRO")
    assert snapshot is not None
    assert snapshot["price"] == pytest.approx(2.59)
    assert snapshot["bar_timestamp"] == pytest.approx(
        datetime(2026, 3, 28, 13, 29, 30, tzinfo=UTC).timestamp()
    )


def test_lazy_history_seed_rehydrates_session_bars_after_restart() -> None:
    session_factory = build_test_session_factory()
    start_30s = datetime(2026, 3, 28, 13, 0, tzinfo=UTC)

    with session_factory() as session:
        for index in range(60):
            close = Decimal(str(2.00 + index * 0.01))
            session.add(
                StrategyBarHistory(
                    strategy_code="macd_30s",
                    symbol="UGRO",
                    interval_secs=30,
                    bar_time=start_30s + timedelta(seconds=index * 30),
                    open_price=close - Decimal("0.01"),
                    high_price=close + Decimal("0.02"),
                    low_price=close - Decimal("0.02"),
                    close_price=close,
                    volume=20_000 + index * 100,
                    trade_count=10,
                )
            )
        session.commit()

    service = StrategyEngineService(
        settings=make_test_settings(
            redis_stream_prefix="test",
            strategy_macd_30s_broker_provider="schwab",
        ),
        redis_client=FakeRedis(),
        session_factory=session_factory,
    )
    runtime = service.state.bots["macd_30s"]
    runtime.now_provider = fixed_now

    builder = runtime.builder_manager.get_or_create("UGRO")
    assert builder.get_bar_count() == 0
    assert runtime.last_indicators.get("UGRO") is None

    runtime._ensure_history_seeded("UGRO")

    assert builder.get_bar_count() == 60
    assert runtime.last_indicators["UGRO"]["price"] == pytest.approx(2.59)


def test_strategy_bot_runtime_loads_closed_trades_for_daily_pnl(monkeypatch) -> None:
    calls: list[str] = []

    def fake_load_closed_trades(self) -> None:
        calls.append(self.config.__class__.__name__)
        self._daily_pnl = 42.5

    monkeypatch.setattr(
        "project_mai_tai.services.strategy_engine_app.PositionTracker.load_closed_trades",
        fake_load_closed_trades,
    )

    runtime = StrategyBotRuntime(
        StrategyDefinition(
            code="macd_30s",
            display_name="MACD Bot",
            account_name="paper:macd_30s",
            interval_secs=30,
            trading_config=TradingConfig(),
            indicator_config=IndicatorConfig(),
        ),
        now_provider=fixed_now,
    )

    assert calls == ["TradingConfig"]
    assert runtime.positions.get_daily_pnl() == 42.5


def test_strategy_bot_runtime_uses_strategy_specific_trade_history(tmp_path, monkeypatch) -> None:
    repo_dir = tmp_path / "project-mai-tai"
    data_dir = tmp_path / "project-mai-tai-data" / "history"
    repo_dir.mkdir()
    data_dir.mkdir(parents=True)
    monkeypatch.chdir(repo_dir)

    (data_dir / "macdbot_closed_2026-03-30.csv").write_text(
        "ticker,entry_price,exit_price,quantity,pnl,pnl_pct,reason,entry_time,exit_time,peak_profit_pct,tier,scales_done,path\n"
        "ELAB,3.00,3.10,100,10.0,3.33,OMS_FILL,09:30:00 AM ET,09:31:00 AM ET,4.0,1,,P1_MACD_CROSS\n",
        encoding="utf-8",
    )
    (data_dir / "macd_1m_closed_2026-03-30.csv").write_text(
        "ticker,entry_price,exit_price,quantity,pnl,pnl_pct,reason,entry_time,exit_time,peak_profit_pct,tier,scales_done,path\n"
        "ASTC,4.00,4.50,100,50.0,12.50,OMS_FILL,09:35:00 AM ET,09:36:00 AM ET,10.0,2,,P3_MACD_SURGE\n",
        encoding="utf-8",
    )
    (data_dir / "tos_closed_2026-03-30.csv").write_text(
        "ticker,entry_price,exit_price,quantity,pnl,pnl_pct,reason,entry_time,exit_time,peak_profit_pct,tier,scales_done,path\n"
        "BFRG,1.00,0.95,100,-5.0,-5.00,OMS_FILL,09:40:00 AM ET,09:41:00 AM ET,2.0,1,,P1_MACD_CROSS\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "project_mai_tai.strategy_core.position_tracker.today_eastern_str",
        lambda: "2026-03-30",
    )

    def make_runtime(strategy_code: str) -> StrategyBotRuntime:
        return StrategyBotRuntime(
            StrategyDefinition(
                code=strategy_code,
                display_name=strategy_code,
                account_name=f"paper:{strategy_code}",
                interval_secs=30 if strategy_code == "macd_30s" else 60,
                trading_config=TradingConfig(),
                indicator_config=IndicatorConfig(),
            )
        )

    assert make_runtime("macd_30s").summary()["daily_pnl"] == 10.0
    assert make_runtime("macd_1m").summary()["daily_pnl"] == 50.0
    assert make_runtime("tos").summary()["daily_pnl"] == -5.0


def test_strategy_bot_runtime_rolls_daily_pnl_and_closed_trades_at_new_session_after_eight_pm_et(monkeypatch) -> None:
    active_day = {"value": "2026-03-30"}

    monkeypatch.setattr(
        "project_mai_tai.services.strategy_engine_app.session_day_eastern_str",
        lambda *_args, **_kwargs: active_day["value"],
    )
    monkeypatch.setattr(
        "project_mai_tai.services.strategy_engine_app.PositionTracker.load_closed_trades",
        lambda self: None,
    )

    runtime = StrategyBotRuntime(
        StrategyDefinition(
            code="macd_30s",
            display_name="macd_30s",
            account_name="paper:macd_30s",
            interval_secs=30,
            trading_config=TradingConfig(),
            indicator_config=IndicatorConfig(),
        )
    )
    runtime.positions._daily_pnl = 12.5
    runtime.positions._closed_today = [{"ticker": "ELAB"}]

    active_day["value"] = "2026-03-31"

    summary = runtime.summary()

    assert summary["daily_pnl"] == 0.0
    assert summary["closed_today"] == []


def test_strategy_bot_runtime_uses_eastern_bar_timestamps() -> None:
    runtime = StrategyBotRuntime(
        StrategyDefinition(
            code="macd_30s",
            display_name="MACD Bot",
            account_name="paper:macd_30s",
            interval_secs=30,
            trading_config=TradingConfig().make_30s_variant(),
            indicator_config=IndicatorConfig(),
        ),
        now_provider=fixed_now,
    )
    runtime.set_watchlist(["UGRO"])

    runtime.seed_bars(
        "UGRO",
        [
            {
                "open": 2.35,
                "high": 2.40,
                "low": 2.34,
                "close": 2.39,
                "volume": 18_000,
                "timestamp": datetime(2026, 3, 28, 13, 59, 30, tzinfo=UTC).timestamp(),
            },
            {
                "open": 2.40,
                "high": 2.45,
                "low": 2.39,
                "close": 2.44,
                "volume": 20_000,
                "timestamp": datetime(2026, 3, 28, 14, 0, tzinfo=UTC).timestamp(),
            }
        ],
    )
    runtime.last_indicators["UGRO"] = {
        "price": 2.44,
        "ema9": 2.40,
        "ema20": 2.35,
        "macd": 0.03,
        "signal": 0.02,
        "histogram": 0.01,
        "vwap": 2.38,
        "macd_above_signal": True,
        "price_above_vwap": True,
        "price_above_ema9": True,
        "price_above_ema20": True,
    }
    runtime._record_decision(
        symbol="UGRO",
        status="idle",
        reason="no entry path matched",
        indicators=runtime.last_indicators["UGRO"],
    )
    runtime._record_decision(
        symbol="PREWARM",
        status="idle",
        reason="no entry path matched",
        indicators={"price": 1.23},
    )

    summary = runtime.summary()

    assert [item["symbol"] for item in summary["recent_decisions"]] == ["UGRO"]
    assert summary["recent_decisions"][0]["status"] == "evaluated"
    assert summary["recent_decisions"][0]["reason"] == "entry evaluated; no setup matched this bar"
    assert summary["recent_decisions"][0]["last_bar_at"].endswith("-04:00")
    assert summary["indicator_snapshots"][0]["last_bar_at"].endswith("-04:00")


def test_tos_runtime_emits_intrabar_open_on_current_bar(monkeypatch) -> None:
    service = StrategyEngineService(
        settings=make_test_settings(
            redis_stream_prefix="test",
            strategy_tos_enabled=True,
            strategy_tos_broker_provider="schwab",
        ),
        redis_client=FakeRedis(),
        now_provider=fixed_now,
    )
    runtime = service.state.bots["tos"]
    runtime.definition.trading_config.entry_intrabar_enabled = True
    runtime.set_watchlist(["CMND"])
    runtime.seed_bars("CMND", seed_trending_bars(count=40, interval_secs=60))

    captured: dict[str, int] = {}

    def fake_calculate(_bars):
        return {
            "price": 2.80,
            "bar_timestamp": 1_700_002_340.0,
        }

    def fake_check_entry(symbol, indicators, bar_index, position_tracker):
        del position_tracker
        captured["bar_index"] = bar_index
        captured["price"] = indicators["price"]
        return {
            "action": "BUY",
            "ticker": symbol,
            "path": "P2_VWAP_BREAKOUT",
            "price": indicators["price"],
            "score": 0,
            "score_details": "intrabar",
        }

    monkeypatch.setattr(runtime.indicator_engine, "calculate", fake_calculate)
    monkeypatch.setattr(runtime.entry_engine, "check_entry", fake_check_entry)
    monkeypatch.setattr(
        runtime.entry_engine,
        "pop_last_decision",
        lambda _symbol: {"status": "signal", "reason": "P2_VWAP_BREAKOUT", "path": "P2_VWAP_BREAKOUT"},
    )

    intents = runtime.handle_trade_tick(
        "CMND",
        price=2.81,
        size=100,
        timestamp_ns=1_700_002_401_000_000_000,
    )

    assert len(intents) == 1
    assert captured["bar_index"] == 41
    assert intents[0].payload.symbol == "CMND"
    assert intents[0].payload.reason == "ENTRY_P2_VWAP_BREAKOUT"
    assert "CMND" in runtime.pending_open_symbols
    assert runtime.recent_decisions[0]["last_bar_at"].endswith("+00:00") is False


def test_schwab_native_30s_runtime_does_not_emit_intrabar_open_when_intrabar_disabled(monkeypatch) -> None:
    service = StrategyEngineService(
        settings=make_test_settings(
            redis_stream_prefix="test",
            strategy_macd_30s_broker_provider="schwab",
        ),
        redis_client=FakeRedis(),
        now_provider=fixed_now,
    )
    runtime = service.state.bots["macd_30s"]
    runtime.set_watchlist(["CMND"])
    runtime.seed_bars("CMND", seed_trending_bars(count=55, interval_secs=30))

    captured: dict[str, int] = {}

    def fake_calculate(_bars):
        return {
            "price": 1.35,
            "bar_timestamp": 1_700_001_620.0,
        }

    def fake_check_entry(symbol, indicators, bar_index, position_tracker):
        del position_tracker
        captured["bar_index"] = bar_index
        captured["price"] = indicators["price"]
        return {
            "action": "BUY",
            "ticker": symbol,
            "path": "P3_SURGE",
            "price": indicators["price"],
            "score": 5,
            "score_details": "intrabar",
        }

    monkeypatch.setattr(runtime.indicator_engine, "calculate", fake_calculate)
    monkeypatch.setattr(runtime.entry_engine, "check_entry", fake_check_entry)
    monkeypatch.setattr(
        runtime.entry_engine,
        "pop_last_decision",
        lambda _symbol: {"status": "signal", "reason": "P3_SURGE", "path": "P3_SURGE", "score": "5"},
    )

    intents = runtime.handle_trade_tick(
        "CMND",
        price=1.36,
        size=100,
        timestamp_ns=1_700_001_651_000_000_000,
        cumulative_volume=50_000,
    )

    assert intents == []
    assert captured == {}
    assert "CMND" not in runtime.pending_open_symbols
    assert runtime.definition.trading_config.confirm_bars == 1
    assert runtime.definition.trading_config.entry_intrabar_enabled is False
    assert runtime.definition.trading_config.schwab_native_use_confirmation is True


def _build_runtime_with_session(session_factory):
    state = StrategyEngineState(
        settings=make_test_settings(
            dashboard_snapshot_persistence_enabled=False,
            strategy_history_persistence_enabled=True,
            strategy_polygon_30s_enabled=True,
        ),
        session_factory=session_factory,
    )
    bot = state.bots["polygon_30s"]
    bot.set_watchlist(["UGRO"])
    return state, bot


def test_persist_bar_history_skips_placeholder_zero_volume_bar() -> None:
    session_factory = build_test_session_factory()
    _, bot = _build_runtime_with_session(session_factory)

    placeholder_ts = 1_700_001_650.0
    seeded = seed_trending_bars(count=5, start_timestamp=1_700_000_000.0, interval_secs=30)
    seeded.append({
        "open": 2.05,
        "high": 2.05,
        "low": 2.05,
        "close": 2.05,
        "volume": 0,
        "timestamp": placeholder_ts,
        "trade_count": 0,
    })
    bot.seed_bars("UGRO", seeded)

    bot._persist_bar_history(
        symbol="UGRO",
        indicators={"price": 2.05},
        decision={"status": "idle", "reason": "no entry path matched"},
    )

    with session_factory() as session:
        records = list(
            session.scalars(
                select(StrategyBarHistory).where(
                    StrategyBarHistory.strategy_code == "polygon_30s",
                    StrategyBarHistory.symbol == "UGRO",
                    StrategyBarHistory.bar_time == datetime.fromtimestamp(placeholder_ts, UTC),
                )
            )
        )
    assert records == []


def test_persist_bar_history_persists_real_volume_bar() -> None:
    session_factory = build_test_session_factory()
    _, bot = _build_runtime_with_session(session_factory)

    real_ts = 1_700_001_650.0
    seeded = seed_trending_bars(count=5, start_timestamp=1_700_000_000.0, interval_secs=30)
    seeded.append({
        "open": 2.05,
        "high": 2.10,
        "low": 2.04,
        "close": 2.08,
        "volume": 1000,
        "timestamp": real_ts,
        "trade_count": 5,
    })
    bot.seed_bars("UGRO", seeded)

    bot._persist_bar_history(
        symbol="UGRO",
        indicators={"price": 2.08},
        decision={"status": "idle", "reason": "no entry path matched"},
    )

    with session_factory() as session:
        records = list(
            session.scalars(
                select(StrategyBarHistory).where(
                    StrategyBarHistory.strategy_code == "polygon_30s",
                    StrategyBarHistory.symbol == "UGRO",
                    StrategyBarHistory.bar_time == datetime.fromtimestamp(real_ts, UTC),
                )
            )
        )
    assert len(records) == 1
    assert records[0].volume == 1000
    assert records[0].trade_count == 5


def test_persist_bar_history_persists_zero_volume_bar_with_nonzero_trade_count() -> None:
    session_factory = build_test_session_factory()
    _, bot = _build_runtime_with_session(session_factory)

    ts = 1_700_001_650.0
    seeded = seed_trending_bars(count=5, start_timestamp=1_700_000_000.0, interval_secs=30)
    seeded.append({
        "open": 2.05,
        "high": 2.05,
        "low": 2.05,
        "close": 2.05,
        "volume": 0,
        "timestamp": ts,
        "trade_count": 1,
    })
    bot.seed_bars("UGRO", seeded)

    bot._persist_bar_history(
        symbol="UGRO",
        indicators={"price": 2.05},
        decision={"status": "idle", "reason": "no entry path matched"},
    )

    with session_factory() as session:
        records = list(
            session.scalars(
                select(StrategyBarHistory).where(
                    StrategyBarHistory.strategy_code == "polygon_30s",
                    StrategyBarHistory.symbol == "UGRO",
                    StrategyBarHistory.bar_time == datetime.fromtimestamp(ts, UTC),
                )
            )
        )
    assert len(records) == 1
    assert records[0].trade_count == 1


def test_flush_completed_polygon_bar_persists_real_bar_before_synthetic_gap_fill() -> None:
    session_factory = build_test_session_factory()
    clock = {"now": datetime(2026, 4, 23, 15, 26, 59, tzinfo=UTC)}
    state = StrategyEngineState(
        settings=make_test_settings(
            dashboard_snapshot_persistence_enabled=False,
            strategy_history_persistence_enabled=True,
            strategy_polygon_30s_enabled=True,
            strategy_polygon_30s_live_aggregate_bars_enabled=True,
            strategy_polygon_30s_tick_bar_close_grace_seconds=0.0,
        ),
        now_provider=lambda: clock["now"],
        session_factory=session_factory,
    )
    bot = state.bots["polygon_30s"]
    bot.set_watchlist(["UGRO"])
    state.seed_bars(
        "polygon_30s",
        "UGRO",
        seed_trending_bars(
            count=55,
            start_timestamp=datetime(2026, 4, 23, 14, 59, 0, tzinfo=UTC).timestamp(),
            interval_secs=30,
        ),
    )

    bot.indicator_engine.calculate = lambda bars: {
        "price": float(bars[-1]["close"]),
        "bar_timestamp": float(bars[-1]["timestamp"]),
    }
    bot.entry_engine.check_entry = lambda *_args, **_kwargs: None
    bot.entry_engine.pop_last_decision = lambda _symbol: None

    bot.handle_live_bar(
        symbol="UGRO",
        open_price=4.00,
        high_price=4.04,
        low_price=3.99,
        close_price=4.02,
        volume=900,
        timestamp=datetime(2026, 4, 23, 15, 26, 35, tzinfo=UTC).timestamp(),
        trade_count=6,
        coverage_started_at=datetime(2026, 4, 23, 15, 26, 0, tzinfo=UTC).timestamp(),
    )
    bot.handle_live_bar(
        symbol="UGRO",
        open_price=4.02,
        high_price=4.05,
        low_price=4.00,
        close_price=4.03,
        volume=1_100,
        timestamp=datetime(2026, 4, 23, 15, 26, 59, tzinfo=UTC).timestamp(),
        trade_count=7,
        coverage_started_at=datetime(2026, 4, 23, 15, 26, 0, tzinfo=UTC).timestamp(),
    )

    clock["now"] = datetime(2026, 4, 23, 15, 28, 5, tzinfo=UTC)
    _intents, completed_count = bot.flush_completed_bars()

    with session_factory() as session:
        persisted = session.scalar(
            select(StrategyBarHistory).where(
                StrategyBarHistory.strategy_code == "polygon_30s",
                StrategyBarHistory.symbol == "UGRO",
                StrategyBarHistory.interval_secs == 30,
                StrategyBarHistory.bar_time == datetime(2026, 4, 23, 15, 26, 30, tzinfo=UTC),
            )
        )
        synthetic = session.scalar(
            select(StrategyBarHistory).where(
                StrategyBarHistory.strategy_code == "polygon_30s",
                StrategyBarHistory.symbol == "UGRO",
                StrategyBarHistory.interval_secs == 30,
                StrategyBarHistory.bar_time == datetime(2026, 4, 23, 15, 27, 30, tzinfo=UTC),
            )
        )

    assert completed_count == 3
    assert persisted is not None
    assert persisted.volume == 2_000
    assert persisted.trade_count == 13
    assert synthetic is None


def test_drop_placeholder_bars_filters_zero_volume_and_zero_trade_count() -> None:
    bars = [
        {"open": 1.10, "high": 1.10, "low": 1.10, "close": 1.10, "volume": 0, "trade_count": 0, "timestamp": 1_778_577_840.0},
        {"open": 1.32, "high": 1.44, "low": 1.29, "close": 1.44, "volume": 324_283, "trade_count": 46, "timestamp": 1_778_577_900.0},
        {"open": 1.49, "high": 1.55, "low": 1.49, "close": 1.50, "volume": 157_327, "trade_count": 37, "timestamp": 1_778_577_960.0},
    ]

    kept = StrategyEngineService._drop_placeholder_bars(bars)

    assert [bar["timestamp"] for bar in kept] == [1_778_577_900.0, 1_778_577_960.0]


def test_drop_placeholder_bars_keeps_bars_with_volume_only() -> None:
    bars = [{"open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "volume": 100}]
    assert StrategyEngineService._drop_placeholder_bars(bars) == bars


def test_drop_placeholder_bars_keeps_bars_with_trade_count_only() -> None:
    bars = [{"open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "trade_count": 3}]
    assert StrategyEngineService._drop_placeholder_bars(bars) == bars


def test_drop_placeholder_bars_drops_bars_with_missing_volume_and_trade_count() -> None:
    bars = [{"open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0}]
    assert StrategyEngineService._drop_placeholder_bars(bars) == []


def test_drop_placeholder_bars_drops_bars_with_none_volume() -> None:
    bars = [{"open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "volume": None, "trade_count": None}]
    assert StrategyEngineService._drop_placeholder_bars(bars) == []


def test_set_broker_blocked_symbols_evicts_from_lifecycle_and_watchlist() -> None:
    state = StrategyEngineState(
        settings=make_test_settings(strategy_polygon_30s_enabled=True),
        now_provider=fixed_now,
    )
    bot = state.bots["polygon_30s"]
    bot.set_watchlist(["AEHL", "WOK"])
    assert "AEHL" in bot.watchlist
    assert "WOK" in bot.watchlist

    bot.set_broker_blocked_symbols(["AEHL"])

    assert "AEHL" not in bot.watchlist
    assert "WOK" in bot.watchlist
    assert "AEHL" not in bot.lifecycle_states
    assert "AEHL" in bot.broker_blocked_symbols
    assert "AEHL" in bot.entry_blocked_symbols


def test_broker_blocked_symbol_records_blocked_decision_instead_of_emitting_intent(monkeypatch) -> None:
    state = StrategyEngineState(
        settings=make_test_settings(strategy_polygon_30s_enabled=True),
        now_provider=fixed_now,
    )
    bot = state.bots["polygon_30s"]
    bot.set_watchlist(["AEHL"])
    state.seed_bars(
        "polygon_30s",
        "AEHL",
        seed_trending_bars(count=55, start_timestamp=1_700_000_000.0, interval_secs=30),
    )

    def fake_calculate(bars):
        return {"price": float(bars[-1]["close"]), "bar_timestamp": float(bars[-1]["timestamp"])}

    def fake_check_entry(symbol, indicators, bar_index, runtime):
        return {
            "action": "BUY",
            "ticker": symbol,
            "path": "P4_BURST",
            "price": indicators["price"],
            "score": 6,
            "score_details": "burst",
        }

    monkeypatch.setattr(bot.indicator_engine, "calculate", fake_calculate)
    monkeypatch.setattr(bot.entry_engine, "check_entry", fake_check_entry)
    monkeypatch.setattr(
        bot.entry_engine,
        "pop_last_decision",
        lambda _symbol: {"status": "signal", "reason": "P4_BURST", "path": "P4_BURST", "score": "6"},
    )

    bot.set_broker_blocked_symbols(["AEHL"])

    intents = bot.handle_trade_tick(
        "AEHL",
        price=2.5,
        size=100,
        timestamp_ns=1_700_001_650_000_000_000,
        cumulative_volume=50_000,
    )

    assert intents == []
    assert "AEHL" not in bot.watchlist
    assert "AEHL" in bot.broker_blocked_symbols


def test_set_broker_blocked_symbols_by_strategy_routes_to_bots() -> None:
    state = StrategyEngineState(
        settings=make_test_settings(strategy_polygon_30s_enabled=True),
        now_provider=fixed_now,
    )
    state.bots["polygon_30s"].set_watchlist(["AEHL", "WOK"])

    state.set_broker_blocked_symbols_by_strategy({"polygon_30s": {"AEHL"}})

    assert "AEHL" in state.bots["polygon_30s"].broker_blocked_symbols
    assert "AEHL" not in state.bots["polygon_30s"].watchlist
    assert "AEHL" not in state.bots["polygon_30s"].lifecycle_states
