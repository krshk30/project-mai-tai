from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from project_mai_tai.db.base import Base
from project_mai_tai.db.models import BrokerAccount, BrokerOrder, DashboardSnapshot, Strategy, TradeIntent, VirtualPosition
from project_mai_tai.events import (
    HistoricalBarPayload,
    HistoricalBarsEvent,
    HistoricalBarsPayload,
    MarketSnapshotPayload,
    OrderEventEvent,
    OrderEventPayload,
    SnapshotBatchEvent,
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
from project_mai_tai.market_data.taapi_indicator_provider import TaapiIndicatorProvider
from project_mai_tai.strategy_core import IndicatorConfig, ReferenceData, TradingConfig


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


def test_order_routing_metadata_uses_extended_hours_limit_in_premarket() -> None:
    metadata = order_routing_metadata(
        price="2.55",
        side="buy",
        now=datetime(2026, 3, 31, 7, 0, tzinfo=UTC),
    )

    assert metadata == {
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
    assert close_intent.payload.metadata["limit_price"] == "3.11"
    assert close_intent.payload.metadata["price_source"] == "bid"


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
    assert [item["ticker"] for item in summary["top_confirmed"]] == ["UGRO"]
    assert state.confirmed_scanner.get_all_confirmed()[0]["rank_score"] == 100.0
    for code in ("macd_30s", "macd_1m", "tos", "runner"):
        assert state.bots[code].watchlist == {"UGRO"}


def test_snapshot_batch_applies_reclaim_specific_excluded_symbols(monkeypatch) -> None:
    state = StrategyEngineState(
        settings=Settings(
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
    assert [item["ticker"] for item in summary["top_confirmed"]] == ["UGRO"]
    assert summary["watchlist"] == ["UGRO"]
    assert state.confirmed_scanner.get_all_confirmed()[0]["rank_score"] == 100.0
    assert state.confirmed_scanner.get_all_confirmed()[1]["rank_score"] == 0.0
    for code in ("macd_30s", "macd_1m", "tos", "runner"):
        assert state.bots[code].watchlist == {"UGRO"}


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
            settings=Settings(),
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

        restored = StrategyEngineService(
            settings=Settings(),
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
    settings = Settings()
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
            settings=Settings(),
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
            settings=Settings(),
            redis_client=FakeRedis(),
            session_factory=session_factory,
        )
        restored._restore_alert_engine_state_from_dashboard_snapshot()

        assert restored.state.alert_engine.get_warmup_status()["history_cycles"] == 0
        assert restored.state.recent_alerts == []
        assert restored.state.top_gainer_changes == []
        assert restored.state._first_seen_by_ticker == {}


def test_snapshot_batch_keeps_runner_aligned_to_visible_confirmed_names(monkeypatch) -> None:
    state = StrategyEngineState(now_provider=fixed_now)
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
    monkeypatch.setattr(
        state.confirmed_scanner,
        "get_top_n",
        lambda *args, **kwargs: list(visible_confirmed),
    )

    summary = state.process_snapshot_batch(
        [snapshot_from_payload(make_snapshot_payload(symbol="ELAB", price=2.78, volume=7_200_000))],
        {"ELAB": ReferenceData(shares_outstanding=541_500, avg_daily_volume=600_000)},
    )

    assert summary["watchlist"] == ["ELAB"]
    for code in ("macd_30s", "macd_1m", "tos", "runner"):
        assert state.bots[code].watchlist == {"ELAB"}
    assert state.bots["runner"]._candidates == {"ELAB": visible_confirmed[0]}


def test_snapshot_batch_releases_removed_symbols_from_all_bot_watchlists(monkeypatch) -> None:
    state = StrategyEngineState(now_provider=fixed_now)
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
    monkeypatch.setattr(
        state.confirmed_scanner,
        "get_top_n",
        lambda *args, **kwargs: list(current_top["value"]),
    )

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

    for code in ("macd_30s", "macd_1m", "tos", "runner"):
        assert state.bots[code].watchlist == {"ELAB", "UGRO"}

    current_all["value"] = list(second_confirmed)
    current_top["value"] = list(second_confirmed)

    state.process_snapshot_batch(
        [snapshot_from_payload(make_snapshot_payload(symbol="ELAB", price=2.82, volume=7_400_000))],
        {"ELAB": ReferenceData(shares_outstanding=541_500, avg_daily_volume=600_000)},
    )

    for code in ("macd_30s", "macd_1m", "tos", "runner"):
        assert state.bots[code].watchlist == {"ELAB"}
    assert state.bots["runner"]._candidates == {"ELAB": second_confirmed[0]}


def test_snapshot_batch_keeps_low_score_confirmed_visible_but_out_of_watchlist(monkeypatch) -> None:
    state = StrategyEngineState(now_provider=fixed_now)
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
    monkeypatch.setattr(state.confirmed_scanner, "get_ranked_confirmed", lambda **kwargs: list(low_score_confirmed))
    monkeypatch.setattr(state.confirmed_scanner, "get_top_n", lambda *args, **kwargs: [])

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
    assert summary["watchlist"] == []
    assert summary["top_confirmed"] == []
    for code in ("macd_30s", "macd_1m", "tos", "runner"):
        assert state.bots[code].watchlist == set()


def test_snapshot_batch_keeps_faded_confirmed_symbols_in_bot_watchlists_for_session_continuity() -> None:
    state = StrategyEngineState(now_provider=fixed_now)
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
    state = StrategyEngineState(now_provider=fixed_now)
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


def test_trade_tick_generates_open_intent_for_confirmed_watchlist(monkeypatch) -> None:
    state = StrategyEngineState(
        settings=Settings(strategy_macd_30s_live_aggregate_bars_enabled=False),
        now_provider=fixed_now,
    )
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


def test_trade_tick_records_blocked_decision_reason(monkeypatch) -> None:
    state = StrategyEngineState(
        settings=Settings(strategy_macd_30s_live_aggregate_bars_enabled=False),
        now_provider=fixed_now,
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
    recent_decision = bot.summary()["recent_decisions"][0]
    assert recent_decision["status"] == "idle"
    assert recent_decision["reason"] == "no entry path matched"


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
        settings=Settings(strategy_macd_30s_live_aggregate_bars_enabled=False),
        now_provider=fixed_now,
    )
    bot = state.bots["macd_30s"]
    bot.set_watchlist(["UGRO"])
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

    assert bar_indices == [2_001, 2_002]


def test_trimmed_history_does_not_lock_out_new_open_after_cancel(monkeypatch) -> None:
    state = StrategyEngineState(
        settings=Settings(strategy_macd_30s_live_aggregate_bars_enabled=False),
        now_provider=fixed_now,
    )
    bot = state.bots["macd_30s"]
    bot.set_watchlist(["UGRO"])
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
        settings=Settings(strategy_macd_30s_live_aggregate_bars_enabled=False),
        now_provider=now_provider,
    )
    bot = state.bots["macd_30s"]
    bot.set_watchlist(["UGRO"])
    state.seed_bars(
        "macd_30s",
        "UGRO",
        seed_trending_bars(start_timestamp=current.timestamp() - 49 * 30, interval_secs=30),
    )
    bot.definition.trading_config.confirm_bars = 0
    bot.definition.trading_config.min_score = 0

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
        settings=Settings(strategy_macd_30s_live_aggregate_bars_enabled=True),
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

    initial_bar_count = bot.builder_manager.get_or_create("UGRO").get_bar_count()

    def check_entry(symbol, indicators, bar_index, runtime):
        del runtime
        if bar_index <= initial_bar_count + 1:
            return None
        return {
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
                timestamp=1_700_001_470.0 + offset,
                trade_count=1,
            )
        )

    open_intents = [intent for intent in intents if intent.payload.intent_type == "open"]
    assert len(open_intents) == 1
    assert open_intents[0].payload.symbol == "UGRO"


def test_bot_runtime_prunes_symbol_state_when_watchlist_shrinks() -> None:
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

    assert "UGRO" in bot.builder_manager.get_all_tickers()
    assert "BFRG" not in bot.builder_manager.get_all_tickers()
    assert "UGRO" in bot.last_indicators
    assert "BFRG" not in bot.last_indicators
    assert "UGRO" in bot.latest_quotes
    assert "BFRG" not in bot.latest_quotes
    assert "UGRO" in bot.entry_engine._recent_bars
    assert "BFRG" not in bot.entry_engine._recent_bars


def test_strategy_summary_includes_indicator_snapshots_for_1m_parity(monkeypatch) -> None:
    state = StrategyEngineState(now_provider=fixed_now)
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
        settings=Settings(
            taapi_secret="test-secret",
            massive_api_key="polygon-secret",
            strategy_macd_1m_taapi_indicator_source_enabled=True,
        ),
        now_provider=fixed_now,
    )

    assert isinstance(state.bots["macd_1m"].indicator_overlay_provider, TaapiIndicatorProvider)
    assert state.bots["macd_30s"].indicator_overlay_provider is None
    assert state.bots["tos"].indicator_overlay_provider is None


def test_macd_30s_defaults_to_trade_tick_bars_without_massive_overlay() -> None:
    state = StrategyEngineState(
        settings=Settings(
            massive_api_key="test-key",
        ),
        now_provider=fixed_now,
    )

    assert state.bots["macd_30s"].use_live_aggregate_bars is False
    assert state.bots["macd_30s"].indicator_overlay_provider is None


def test_macd_30s_probe_reclaim_and_retest_can_be_enabled_as_separate_bots() -> None:
    state = StrategyEngineState(
        settings=Settings(
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
        settings=Settings(
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
        settings=Settings(
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
        settings=Settings(
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
        settings=Settings(
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
        settings=Settings(
            strategy_macd_30s_live_aggregate_bars_enabled=True,
            strategy_macd_30s_live_aggregate_stale_after_seconds=3,
        ),
        now_provider=fixed_now,
    )
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


def test_reclaim_runtime_checks_pretrigger_logic_while_position_is_open(monkeypatch) -> None:
    state = StrategyEngineState(
        settings=Settings(
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
        settings=Settings(
            taapi_secret="test-secret",
            strategy_macd_1m_taapi_indicator_source_enabled=True,
        ),
        now_provider=fixed_now,
    )

    assert state.bots["macd_1m"].indicator_overlay_provider is None


def test_macd_1m_massive_provider_remains_available_as_fallback() -> None:
    state = StrategyEngineState(
        settings=Settings(
            massive_api_key="test-key",
            strategy_macd_1m_massive_indicator_overlay_enabled=True,
        ),
        now_provider=fixed_now,
    )

    assert isinstance(state.bots["macd_1m"].indicator_overlay_provider, MassiveIndicatorProvider)


def test_strategy_summary_includes_taapi_indicator_fields_for_1m(monkeypatch) -> None:
    state = StrategyEngineState(now_provider=fixed_now)
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
    state = StrategyEngineState(
        settings=Settings(
            massive_api_key="test-key",
            strategy_macd_30s_live_aggregate_bars_enabled=True,
        ),
        now_provider=fixed_now,
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
        timestamp=datetime(2026, 3, 28, 14, 0, tzinfo=UTC).timestamp(),
    )

    summary = state.summary()
    indicator_snapshots = summary["bots"]["macd_30s"]["indicator_snapshots"]
    assert indicator_snapshots
    assert indicator_snapshots[0]["provider_source"] == "massive"
    assert indicator_snapshots[0]["provider_status"] == "ready"
    assert indicator_snapshots[0]["provider_close"] == pytest.approx(2.48)
    assert indicator_snapshots[0]["provider_volume"] == pytest.approx(22000)
    assert indicator_snapshots[0]["provider_close_diff"] == pytest.approx(0.01)
    assert indicator_snapshots[0]["provider_vwap_diff"] == pytest.approx(0.01)


def test_massive_overlay_does_not_change_30s_trading_inputs(monkeypatch) -> None:
    state = StrategyEngineState(
        settings=Settings(
            massive_api_key="test-key",
            strategy_macd_30s_live_aggregate_bars_enabled=True,
        ),
        now_provider=fixed_now,
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
        timestamp=datetime(2026, 3, 28, 14, 0, tzinfo=UTC).timestamp(),
    )

    assert captured["price"] == pytest.approx(2.8)
    assert captured["vwap"] == pytest.approx(2.61)
    assert captured["macd"] == pytest.approx(0.08231)
    assert captured["provider_close"] == pytest.approx(9.99)
    assert captured["provider_vwap"] == pytest.approx(9.88)
    assert captured["provider_status"] == "ready"


def test_taapi_source_changes_1m_trading_inputs(monkeypatch) -> None:
    state = StrategyEngineState(now_provider=fixed_now)
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
        settings=Settings(redis_stream_prefix="test", dashboard_snapshot_persistence_enabled=False),
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
        settings=Settings(redis_stream_prefix="test", dashboard_snapshot_persistence_enabled=False),
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
        settings=Settings(redis_stream_prefix="test", dashboard_snapshot_persistence_enabled=False),
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
        settings=Settings(
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
        settings=Settings(redis_stream_prefix="test", dashboard_snapshot_persistence_enabled=False),
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


def test_market_data_symbols_exclude_schwab_native_macd_30s() -> None:
    state = StrategyEngineState(
        settings=Settings(
            strategy_macd_30s_enabled=True,
            strategy_macd_30s_broker_provider="schwab",
        ),
        now_provider=fixed_now,
    )

    state.bots["macd_30s"].set_watchlist(["ELAB"])
    state.bots["macd_1m"].set_watchlist([])
    state.bots["tos"].set_watchlist([])
    state.bots["runner"].set_watchlist([])

    assert state.market_data_symbols() == []
    assert state.schwab_stream_symbols() == ["ELAB"]


def test_market_data_symbols_exclude_schwab_backed_tos() -> None:
    state = StrategyEngineState(
        settings=Settings(strategy_tos_broker_provider="schwab"),
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
        settings=Settings(strategy_tos_default_quantity=10),
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


def test_gateway_quote_tick_can_exclude_schwab_backed_tos() -> None:
    state = StrategyEngineState(
        settings=Settings(strategy_tos_broker_provider="schwab"),
        now_provider=fixed_now,
    )

    state.handle_quote_tick(
        symbol="ELAB",
        bid_price=2.11,
        ask_price=2.12,
        exclude_codes=state.schwab_stream_strategy_codes(),
    )

    assert "ELAB" not in state.bots["tos"].latest_quotes


def test_snapshot_batch_does_not_push_polygon_quotes_into_schwab_native_macd_30s(monkeypatch) -> None:
    state = StrategyEngineState(
        settings=Settings(
            strategy_macd_30s_enabled=True,
            strategy_macd_30s_broker_provider="schwab",
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
        settings=Settings(strategy_tos_broker_provider="schwab"),
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
        settings=Settings(redis_stream_prefix="test", dashboard_snapshot_persistence_enabled=True),
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
        settings=Settings(redis_stream_prefix="test", dashboard_snapshot_persistence_enabled=True),
        redis_client=FakeRedis(),
        session_factory=session_factory,
    )
    service._seed_confirmed_candidates_from_dashboard_snapshot()

    assert [item["ticker"] for item in service.state.all_confirmed] == ["ELAB", "UGRO"]
    assert [item["ticker"] for item in service.state.current_confirmed] == ["ELAB"]
    for code in ("macd_30s", "macd_1m", "tos", "runner"):
        assert service.state.bots[code].watchlist == {"ELAB"}
    assert set(service.state.market_data_symbols()) == {"ELAB"}

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
    assert [item["ticker"] for item in summary["all_confirmed"]] == ["ELAB", "UGRO"]
    assert summary["watchlist"] == ["ELAB"]
    assert [item["ticker"] for item in summary["top_confirmed"]] == ["ELAB"]
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
        settings=Settings(redis_stream_prefix="test", dashboard_snapshot_persistence_enabled=True),
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
        settings=Settings(redis_stream_prefix="test", dashboard_snapshot_persistence_enabled=True),
        redis_client=FakeRedis(),
        session_factory=session_factory,
    )
    service._seed_confirmed_candidates_from_dashboard_snapshot()

    assert service.state.confirmed_scanner.get_all_confirmed() == []
    assert service.state._seeded_confirmed_pending_revalidation is False


def test_publish_strategy_state_persists_scanner_cycle_history_snapshot() -> None:
    session_factory = build_test_session_factory()
    service = StrategyEngineService(
        settings=Settings(
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
    for bot in service.state.bots.values():
        bot.set_watchlist(["ELAB"])

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
    assert payload["all_confirmed_tickers"] == ["ELAB"]
    assert payload["top_confirmed_tickers"] == ["ELAB"]
    assert payload["five_pillars_tickers"] == ["ELAB"]
    assert payload["top_gainers_tickers"] == ["ELAB"]
    assert payload["top_confirmed"][0]["confirmed_at"] == "10:05:00 AM ET"


def test_scanner_cycle_history_retention_and_dedup() -> None:
    session_factory = build_test_session_factory()
    service = StrategyEngineService(
        settings=Settings(
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
        settings=Settings(redis_stream_prefix="test", dashboard_snapshot_persistence_enabled=True),
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
        settings=Settings(
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
        settings=Settings(
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
        status="pending",
        reason="testing eastern time",
        indicators=runtime.last_indicators["UGRO"],
    )

    summary = runtime.summary()

    assert summary["recent_decisions"][0]["last_bar_at"].endswith("-04:00")
    assert summary["indicator_snapshots"][0]["last_bar_at"].endswith("-04:00")
