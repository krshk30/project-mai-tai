from __future__ import annotations

import csv
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from project_mai_tai.db.base import Base
from project_mai_tai.db.models import (
    AccountPosition,
    BrokerAccount,
    BrokerOrder,
    DashboardSnapshot,
    Fill,
    ReconciliationFinding,
    ReconciliationRun,
    Strategy,
    SystemIncident,
    TradeIntent,
    VirtualPosition,
)
from project_mai_tai.events import (
    HeartbeatEvent,
    HeartbeatPayload,
    MarketDataSubscriptionEvent,
    MarketDataSubscriptionPayload,
    SnapshotBatchEvent,
    SnapshotBatchPayload,
    StrategyBotStatePayload,
    StrategyStateSnapshotEvent,
    StrategyStateSnapshotPayload,
)
from project_mai_tai.services.control_plane import _render_confirmed_catalyst_cell, build_app
from project_mai_tai.services.strategy_engine_app import current_scanner_session_start_utc
from project_mai_tai.settings import Settings


class FakeRedis:
    def __init__(self, streams: dict[str, list[tuple[str, dict[str, str]]]]) -> None:
        self.streams = streams
        self.fail_next = False

    async def xrevrange(self, stream: str, count: int | None = None, **kwargs):
        del kwargs
        if self.fail_next:
            self.fail_next = False
            raise RuntimeError("temporary redis failure")
        entries = self.streams.get(stream, [])
        if count is not None:
            entries = entries[:count]
        return entries

    async def aclose(self) -> None:
        return None


class FakeLegacyClient:
    async def fetch_snapshot(self) -> dict[str, object]:
        return {
            "enabled": True,
            "connected": True,
            "fetched_at": "2026-03-28T14:00:00+00:00",
            "scanner": {
                "confirmed_symbols": ["UGRO", "SBET"],
                "count": 2,
            },
            "bots": {
                "macd_30s": {
                    "status": "running (dry run)",
                    "watched_tickers": ["UGRO", "SBET"],
                    "positions": [{"symbol": "UGRO", "quantity": 10.0}],
                    "recent_actions": [{"symbol": "UGRO", "action": "BUY"}],
                    "daily_pnl": 0,
                },
                "macd_1m": {
                    "status": "running (dry run)",
                    "watched_tickers": [],
                    "positions": [],
                    "recent_actions": [],
                    "daily_pnl": 0,
                },
                "tos": {
                    "status": "running (dry run)",
                    "watched_tickers": ["UGRO"],
                    "positions": [],
                    "recent_actions": [],
                    "daily_pnl": 0,
                },
                "runner": {
                    "status": "running (NO ALPACA)",
                    "watched_tickers": ["SBET"],
                    "positions": [],
                    "recent_actions": [],
                    "daily_pnl": 0,
                },
            },
            "errors": [],
        }


def test_render_confirmed_catalyst_cell_shows_specific_no_article_reason() -> None:
    html = _render_confirmed_catalyst_cell(
        {
            "ticker": "BTBD",
            "article_count": 0,
            "news_fetch_status": "ok",
            "catalyst_status": "no_articles",
            "catalyst_reason": "No company-specific Alpaca news article has been returned yet for BTBD in the current catalyst window.",
        }
    )

    assert "No company-specific Alpaca news article has been returned yet for BTBD" in html


def test_render_confirmed_catalyst_cell_shows_ai_shadow_overlay() -> None:
    html = _render_confirmed_catalyst_cell(
        {
            "ticker": "ROLR",
            "catalyst": "NEWS",
            "headline": "High Roller inks Crypto.com deal to launch U.S. event-based prediction markets",
            "catalyst_reason": "ROLR has 1 recent Alpaca article, but none matched a qualifying Path A catalyst pattern.",
            "article_count": 1,
            "catalyst_status": "non_qualifying_articles",
            "ai_shadow_status": "ok",
            "ai_shadow_provider": "openai",
            "ai_shadow_model": "gpt-4.1-mini",
            "ai_shadow_direction": "bullish",
            "ai_shadow_category": "DEAL/CONTRACT",
            "ai_shadow_confidence": 0.91,
            "ai_shadow_path_a_eligible": True,
            "ai_shadow_reason": "AI sees a fresh company-specific partnership catalyst.",
            "ai_shadow_positive_phrases": ["inks deal", "launch"],
        }
    )

    assert "AI shadow: bullish" in html
    assert "DEAL/CONTRACT" in html
    assert "PATH A ready" in html
    assert "AI sees a fresh company-specific partnership catalyst." in html
    assert "inks deal" in html


def build_test_session_factory() -> sessionmaker[Session]:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        future=True,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


def seed_database(session_factory: sessionmaker[Session]) -> None:
    with session_factory() as session:
        strategy = Strategy(code="macd_30s", name="MACD 30S", execution_mode="paper", metadata_json={})
        account = BrokerAccount(name="paper:macd_30s", provider="alpaca", environment="development")
        session.add_all([strategy, account])
        session.flush()

        intent = TradeIntent(
            strategy_id=strategy.id,
            broker_account_id=account.id,
            symbol="UGRO",
            side="buy",
            intent_type="open",
            quantity=Decimal("10"),
            reason="ENTRY_P1_MACD_CROSS",
            status="filled",
            payload={},
        )
        session.add(intent)
        session.flush()

        order = BrokerOrder(
            intent_id=intent.id,
            strategy_id=strategy.id,
            broker_account_id=account.id,
            client_order_id="macd_30s-UGRO-open-abc123",
            broker_order_id="sim-order-abc123",
            symbol="UGRO",
            side="buy",
            order_type="market",
            time_in_force="day",
            quantity=Decimal("10"),
            status="filled",
            payload={},
            submitted_at=datetime.now(UTC),
        )
        session.add(order)
        session.flush()

        fill = Fill(
            order_id=order.id,
            strategy_id=strategy.id,
            broker_account_id=account.id,
            broker_fill_id="sim-order-abc123-fill-1",
            symbol="UGRO",
            side="buy",
            quantity=Decimal("10"),
            price=Decimal("2.55"),
            filled_at=datetime.now(UTC),
            payload={},
        )
        virtual_position = VirtualPosition(
            strategy_id=strategy.id,
            broker_account_id=account.id,
            symbol="UGRO",
            quantity=Decimal("10"),
            average_price=Decimal("2.55"),
            realized_pnl=Decimal("0"),
            opened_at=datetime.now(UTC),
        )
        account_position = AccountPosition(
            broker_account_id=account.id,
            symbol="UGRO",
            quantity=Decimal("10"),
            average_price=Decimal("2.55"),
            market_value=Decimal("25.5"),
            source_updated_at=datetime.now(UTC),
        )
        sibling_account_position = AccountPosition(
            broker_account_id=account.id,
            symbol="SBET",
            quantity=Decimal("5"),
            average_price=Decimal("3.10"),
            market_value=Decimal("15.5"),
            source_updated_at=datetime.now(UTC),
        )
        reconciliation_run = ReconciliationRun(
            broker_account_id=account.id,
            status="completed",
            started_at=datetime.now(UTC),
            completed_at=datetime.now(UTC),
            summary={
                "cutover_confidence": 90,
                "total_findings": 1,
                "critical_findings": 0,
                "warning_findings": 1,
            },
        )
        session.add(reconciliation_run)
        session.flush()

        reconciliation_finding = ReconciliationFinding(
            reconciliation_run_id=reconciliation_run.id,
            order_id=order.id,
            severity="warning",
            finding_type="stuck_order",
            symbol="UGRO",
            payload={
                "title": "Order stuck in accepted for UGRO",
                "fingerprint": "stuck-order:test",
            },
        )
        incident = SystemIncident(
            service_name="reconciler",
            severity="warning",
            title="Order stuck in accepted for UGRO",
            status="open",
            payload={"fingerprint": "stuck-order:test"},
            opened_at=datetime.now(UTC),
        )
        dashboard_snapshot = DashboardSnapshot(
            snapshot_type="scanner_confirmed_last_nonempty",
            payload={
                "all_confirmed_candidates": [
                    {
                        "ticker": "UGRO",
                        "rank_score": 72,
                        "confirmed_at": "10:00:00 AM ET",
                        "entry_price": 2.48,
                        "price": 2.55,
                        "change_pct": 12.5,
                        "volume": 900_000,
                        "rvol": 6.1,
                        "shares_outstanding": 50_000,
                        "bid": 2.54,
                        "ask": 2.55,
                        "bid_size": 7,
                        "ask_size": 9,
                        "spread": 0.01,
                        "spread_pct": 0.42,
                        "first_spike_time": "09:55:00 AM ET",
                        "squeeze_count": 2,
                        "confirmation_path": "PATH_B_2SQ",
                        "catalyst": "DEAL/CONTRACT",
                        "catalyst_type": "DEAL/CONTRACT",
                        "headline": "Quantum Biopharma Wins Hospital Supply Agreement",
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
                    },
                    {
                        "ticker": "SBET",
                        "rank_score": 14,
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
                        "first_spike_time": "09:56:00 AM ET",
                        "squeeze_count": 2,
                        "confirmation_path": "PATH_B_2SQ",
                    },
                ],
                "top_confirmed": [
                    {
                        "ticker": "UGRO",
                        "rank_score": 72,
                        "confirmed_at": "10:00:00 AM ET",
                        "entry_price": 2.48,
                        "price": 2.55,
                        "change_pct": 12.5,
                        "volume": 900_000,
                        "rvol": 6.1,
                        "shares_outstanding": 50_000,
                        "bid": 2.54,
                        "ask": 2.55,
                        "bid_size": 7,
                        "ask_size": 9,
                        "spread": 0.01,
                        "spread_pct": 0.42,
                        "first_spike_time": "09:55:00 AM ET",
                        "squeeze_count": 2,
                        "confirmation_path": "PATH_B_2SQ",
                        "catalyst": "DEAL/CONTRACT",
                        "catalyst_type": "DEAL/CONTRACT",
                        "headline": "Quantum Biopharma Wins Hospital Supply Agreement",
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
                ],
                "watchlist": ["UGRO"],
                "cycle_count": 42,
                "persisted_at": "2026-03-28T14:00:00+00:00",
                "scanner_session_start_utc": "2026-03-28T13:00:00+00:00",
            },
        )
        session.add_all(
            [
                fill,
                virtual_position,
                account_position,
                sibling_account_position,
                reconciliation_finding,
                incident,
                dashboard_snapshot,
            ]
        )
        session.commit()


def make_streams(
    prefix: str,
    *,
    include_confirmed: bool = True,
    live_price: float = 2.55,
) -> dict[str, list[tuple[str, dict[str, str]]]]:
    heartbeat = HeartbeatEvent(
        source_service="strategy-engine",
        payload=HeartbeatPayload(
            service_name="strategy-engine",
            instance_name="test-instance",
            status="healthy",
            details={"watchlist_size": "1"},
        ),
    )
    snapshot = SnapshotBatchEvent(
        source_service="market-data-gateway",
        payload=SnapshotBatchPayload(snapshots=[], reference_data=[]),
    )
    subscription = MarketDataSubscriptionEvent(
        source_service="strategy-engine",
        payload=MarketDataSubscriptionPayload(
            consumer_name="strategy-engine",
            mode="replace",
            symbols=["UGRO"],
        ),
    )
    strategy_state = StrategyStateSnapshotEvent(
        source_service="strategy-engine",
        payload=StrategyStateSnapshotPayload(
            all_confirmed=(
                [
                    {
                        "ticker": "UGRO",
                        "rank_score": 72,
                        "confirmed_at": "10:00:12 AM ET",
                        "entry_price": 2.48,
                        "price": 2.55,
                        "change_pct": 12.5,
                        "volume": 900_000,
                        "rvol": 6.1,
                        "shares_outstanding": 50_000,
                        "bid": 2.54,
                        "ask": 2.55,
                        "bid_size": 7,
                        "ask_size": 9,
                        "spread": 0.01,
                        "spread_pct": 0.42,
                        "first_spike_time": "09:55:00 AM ET",
                        "squeeze_count": 2,
                        "confirmation_path": "PATH_B_2SQ",
                        "catalyst": "DEAL/CONTRACT",
                        "catalyst_type": "DEAL/CONTRACT",
                        "headline": "Quantum Biopharma Wins Hospital Supply Agreement",
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
                    },
                    {
                        "ticker": "SBET",
                        "rank_score": 14,
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
                        "first_spike_time": "09:56:00 AM ET",
                        "squeeze_count": 2,
                        "confirmation_path": "PATH_B_2SQ",
                    },
                ]
                if include_confirmed
                else []
            ),
            watchlist=["UGRO"],
            top_confirmed=(
                [
                    {
                        "ticker": "UGRO",
                        "rank_score": 72,
                        "confirmed_at": "10:00:12 AM ET",
                        "entry_price": 2.48,
                        "price": 2.55,
                        "change_pct": 12.5,
                        "volume": 900_000,
                        "rvol": 6.1,
                        "shares_outstanding": 50_000,
                        "bid": 2.54,
                        "ask": 2.55,
                        "bid_size": 7,
                        "ask_size": 9,
                        "spread": 0.01,
                        "spread_pct": 0.42,
                        "first_spike_time": "09:55:00 AM ET",
                        "squeeze_count": 2,
                        "confirmation_path": "PATH_B_2SQ",
                        "catalyst": "DEAL/CONTRACT",
                        "catalyst_type": "DEAL/CONTRACT",
                        "headline": "Quantum Biopharma Wins Hospital Supply Agreement",
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
                if include_confirmed
                else []
            ),
            five_pillars=[
                {
                    "ticker": "UGRO",
                    "first_seen": "09:55:00 AM ET",
                    "price": live_price,
                    "change_pct": 12.5,
                    "bid": round(live_price - 0.01, 2),
                    "ask": round(live_price, 2),
                    "spread_pct": 0.39,
                    "volume": 900_000,
                    "rvol": 6.1,
                    "shares_outstanding": 50_000,
                    "hod": 2.60,
                    "vwap": 2.40,
                    "prev_close": 2.27,
                    "data_age_secs": 4,
                }
            ],
            top_gainers=[
                {
                    "ticker": "UGRO",
                    "first_seen": "09:55:00 AM ET",
                    "price": live_price,
                    "change_pct": 12.5,
                    "bid": round(live_price - 0.01, 2),
                    "ask": round(live_price, 2),
                    "spread_pct": 0.39,
                    "volume": 900_000,
                    "rvol": 6.1,
                    "shares_outstanding": 50_000,
                    "hod": 2.60,
                    "vwap": 2.40,
                    "prev_close": 2.27,
                    "data_age_secs": 4,
                }
            ],
            recent_alerts=[
                {
                    "type": "VOLUME_SPIKE",
                    "ticker": "UGRO",
                    "price": 2.55,
                    "bid": 2.54,
                    "ask": 2.55,
                    "volume": 900_000,
                    "float": 50_000,
                    "time": "09:55:00 AM ET",
                    "details": {"spike_mult": 5.2},
                }
            ],
            alert_warmup={"fully_ready": True, "squeeze_5min_ready": True, "squeeze_10min_ready": True},
            cycle_count=42,
            bots=[
                StrategyBotStatePayload(
                    strategy_code="macd_30s",
                    account_name="paper:macd_30s",
                    watchlist=["UGRO"],
                    positions=[{"ticker": "UGRO", "quantity": 10}],
                    pending_open_symbols=[],
                    pending_close_symbols=[],
                    pending_scale_levels=[],
                    daily_pnl=125.5,
                ),
                StrategyBotStatePayload(
                    strategy_code="macd_1m",
                    account_name="paper:macd_1m",
                    watchlist=["UGRO"],
                    positions=[],
                    pending_open_symbols=[],
                    pending_close_symbols=[],
                    pending_scale_levels=[],
                    indicator_snapshots=[
                        {
                            "symbol": "UGRO",
                            "interval_secs": 60,
                            "bar_count": 128,
                            "last_bar_at": "2026-03-28T14:00:00+00:00",
                            "close": 2.55,
                            "ema9": 2.53,
                            "ema20": 2.50,
                            "macd": 0.07650,
                            "signal": 0.07432,
                            "histogram": 0.00218,
                            "vwap": 2.51,
                            "macd_above_signal": True,
                            "price_above_vwap": True,
                            "price_above_ema9": True,
                            "price_above_ema20": True,
                        }
                    ],
                ),
                StrategyBotStatePayload(
                    strategy_code="tos",
                    account_name="paper:tos_runner_shared",
                    watchlist=["UGRO"],
                    positions=[],
                    pending_open_symbols=[],
                    pending_close_symbols=[],
                    pending_scale_levels=[],
                    indicator_snapshots=[
                        {
                            "symbol": "UGRO",
                            "interval_secs": 60,
                            "bar_count": 128,
                            "last_bar_at": "2026-03-28T14:00:00+00:00",
                            "close": 2.55,
                            "ema9": 2.53,
                            "ema20": 2.50,
                            "macd": 0.07650,
                            "signal": 0.07432,
                            "histogram": 0.00218,
                            "vwap": 2.51,
                            "macd_above_signal": True,
                            "price_above_vwap": True,
                            "price_above_ema9": True,
                            "price_above_ema20": True,
                        }
                    ],
                ),
            ],
        ),
    )
    return {
        f"{prefix}:heartbeats": [("1-0", {"data": heartbeat.model_dump_json()})],
        f"{prefix}:snapshot-batches": [("1-0", {"data": snapshot.model_dump_json()})],
        f"{prefix}:market-data-subscriptions": [("1-0", {"data": subscription.model_dump_json()})],
        f"{prefix}:strategy-state": [("1-0", {"data": strategy_state.model_dump_json()})],
    }


def test_control_plane_surfaces_probe_and_reclaim_bot_pages_when_enabled() -> None:
    settings = Settings(
        redis_stream_prefix="test",
        oms_adapter="alpaca_paper",
        strategy_macd_30s_probe_enabled=True,
        strategy_macd_30s_reclaim_enabled=True,
    )
    session_factory = build_test_session_factory()
    seed_database(session_factory)
    streams = make_streams(settings.redis_stream_prefix)
    strategy_state_stream = streams[f"{settings.redis_stream_prefix}:strategy-state"]
    strategy_state_event = StrategyStateSnapshotEvent.model_validate_json(
        strategy_state_stream[0][1]["data"]
    )
    strategy_state_event.payload.bots.append(
        StrategyBotStatePayload(
            strategy_code="macd_30s_probe",
            account_name="paper:macd_30s_probe",
            watchlist=["UGRO"],
            positions=[],
            pending_open_symbols=["UGRO"],
            pending_close_symbols=[],
            pending_scale_levels=[],
            daily_pnl=12.5,
        )
    )
    strategy_state_event.payload.bots.append(
        StrategyBotStatePayload(
            strategy_code="macd_30s_reclaim",
            account_name="paper:macd_30s_reclaim",
            watchlist=["UGRO"],
            positions=[],
            pending_open_symbols=[],
            pending_close_symbols=[],
            pending_scale_levels=[],
            daily_pnl=-4.0,
        )
    )
    strategy_state_stream[0][1]["data"] = strategy_state_event.model_dump_json()
    redis = FakeRedis(streams)

    app = build_app(
        settings=settings,
        session_factory=session_factory,
        redis_client=redis,
        legacy_client=FakeLegacyClient(),
    )

    with TestClient(app) as client:
        bots = client.get("/api/bots")
        assert bots.status_code == 200
        strategy_codes = [item["strategy_code"] for item in bots.json()["bots"]]
        assert "macd_30s_probe" in strategy_codes
        assert "macd_30s_reclaim" in strategy_codes

        probe_page = client.get("/bot/30s-probe")
        assert probe_page.status_code == 200
        assert "30-Second Probe Bot" in probe_page.text
        assert "Interval:</strong> 30s" in probe_page.text

        reclaim_page = client.get("/bot/30s-reclaim")
        assert reclaim_page.status_code == 200
        assert "30-Second Reclaim Bot" in reclaim_page.text
        assert "Interval:</strong> 30s" in reclaim_page.text


def test_control_plane_ignores_stale_runtime_pending_open_without_open_broker_order() -> None:
    settings = Settings(redis_stream_prefix="test", oms_adapter="alpaca_paper")
    session_factory = build_test_session_factory()
    seed_database(session_factory)
    streams = make_streams(settings.redis_stream_prefix)
    strategy_state_stream = streams[f"{settings.redis_stream_prefix}:strategy-state"]
    strategy_state_event = StrategyStateSnapshotEvent.model_validate_json(
        strategy_state_stream[0][1]["data"]
    )
    strategy_state_event.payload.bots[0].pending_open_symbols = ["UGRO"]
    strategy_state_stream[0][1]["data"] = strategy_state_event.model_dump_json()
    redis = FakeRedis(streams)

    app = build_app(
        settings=settings,
        session_factory=session_factory,
        redis_client=redis,
        legacy_client=FakeLegacyClient(),
    )

    with TestClient(app) as client:
        bots = client.get("/api/bots")
        assert bots.status_code == 200
        bot_30s = next(item for item in bots.json()["bots"] if item["strategy_code"] == "macd_30s")
        assert bot_30s["pending_open_symbols"] == []
        assert bot_30s["pending_count"] == 0


def test_control_plane_decision_tape_shows_only_live_symbols() -> None:
    settings = Settings(redis_stream_prefix="test", oms_adapter="alpaca_paper")
    session_factory = build_test_session_factory()
    seed_database(session_factory)
    streams = make_streams(settings.redis_stream_prefix)
    strategy_state_stream = streams[f"{settings.redis_stream_prefix}:strategy-state"]
    strategy_state_event = StrategyStateSnapshotEvent.model_validate_json(
        strategy_state_stream[0][1]["data"]
    )
    runtime_bot = strategy_state_event.payload.bots[0]
    runtime_bot.watchlist = ["UGRO"]
    runtime_bot.positions = []
    runtime_bot.recent_decisions = [
        {
            "symbol": "PREWARM",
            "status": "idle",
            "reason": "no entry path matched",
            "last_bar_at": "2026-04-23T11:38:00-04:00",
        },
        {
            "symbol": "UGRO",
            "status": "idle",
            "reason": "no entry path matched",
            "last_bar_at": "2026-04-23T11:38:00-04:00",
        },
    ]
    strategy_state_stream[0][1]["data"] = strategy_state_event.model_dump_json()
    redis = FakeRedis(streams)

    app = build_app(
        settings=settings,
        session_factory=session_factory,
        redis_client=redis,
        legacy_client=FakeLegacyClient(),
    )

    with TestClient(app) as client:
        bots = client.get("/api/bots")
        assert bots.status_code == 200
        bot_30s = next(item for item in bots.json()["bots"] if item["strategy_code"] == "macd_30s")
        assert [item["symbol"] for item in bot_30s["recent_decisions"]] == ["UGRO"]
        assert bot_30s["recent_decisions"][0]["status"] == "evaluated"
        assert bot_30s["recent_decisions"][0]["reason"] == "entry evaluated; no setup matched this bar"


def test_control_plane_decision_tape_includes_live_symbol_waiting_for_evaluation() -> None:
    settings = Settings(redis_stream_prefix="test", oms_adapter="alpaca_paper")
    session_factory = build_test_session_factory()
    seed_database(session_factory)
    streams = make_streams(settings.redis_stream_prefix)
    strategy_state_stream = streams[f"{settings.redis_stream_prefix}:strategy-state"]
    strategy_state_event = StrategyStateSnapshotEvent.model_validate_json(
        strategy_state_stream[0][1]["data"]
    )
    runtime_bot = strategy_state_event.payload.bots[0]
    runtime_bot.watchlist = ["AUUD"]
    runtime_bot.positions = []
    runtime_bot.recent_decisions = []
    runtime_bot.bar_counts = {"AUUD": 27}
    runtime_bot.last_tick_at = {"AUUD": "2026-04-23 10:48:17 AM ET"}
    strategy_state_stream[0][1]["data"] = strategy_state_event.model_dump_json()
    redis = FakeRedis(streams)

    app = build_app(
        settings=settings,
        session_factory=session_factory,
        redis_client=redis,
        legacy_client=FakeLegacyClient(),
    )

    with TestClient(app) as client:
        bots = client.get("/api/bots")
        assert bots.status_code == 200
        bot_30s = next(item for item in bots.json()["bots"] if item["strategy_code"] == "macd_30s")
        assert [item["symbol"] for item in bot_30s["recent_decisions"]] == ["AUUD"]
        assert bot_30s["recent_decisions"][0]["status"] == "pending"
        assert (
            bot_30s["recent_decisions"][0]["reason"]
            == "live in bot; waiting for next completed 30s trade bar to evaluate"
        )


def test_control_plane_decision_tape_uses_polygon_wording_for_webull_bot() -> None:
    settings = Settings(
        redis_stream_prefix="test",
        oms_adapter="alpaca_paper",
        strategy_webull_30s_enabled=True,
    )
    session_factory = build_test_session_factory()
    seed_database(session_factory)
    streams = make_streams(settings.redis_stream_prefix)
    strategy_state_stream = streams[f"{settings.redis_stream_prefix}:strategy-state"]
    strategy_state_event = StrategyStateSnapshotEvent.model_validate_json(
        strategy_state_stream[0][1]["data"]
    )
    strategy_state_event.payload.bots.append(
        StrategyBotStatePayload(
            strategy_code="webull_30s",
            account_name="live:webull_30s",
            watchlist=["AUUD"],
            positions=[],
            pending_open_symbols=[],
            pending_close_symbols=[],
            pending_scale_levels=[],
            daily_pnl=0.0,
        )
    )
    strategy_state_stream[0][1]["data"] = strategy_state_event.model_dump_json()
    redis = FakeRedis(streams)

    app = build_app(
        settings=settings,
        session_factory=session_factory,
        redis_client=redis,
        legacy_client=FakeLegacyClient(),
    )

    with TestClient(app) as client:
        bots = client.get("/api/bots")
        assert bots.status_code == 200
        webull_bot = next(item for item in bots.json()["bots"] if item["strategy_code"] == "webull_30s")
        assert [item["symbol"] for item in webull_bot["recent_decisions"]] == ["AUUD"]
        assert webull_bot["recent_decisions"][0]["status"] == "pending"
        assert (
            webull_bot["recent_decisions"][0]["reason"]
            == "live in bot; waiting for Polygon market data"
        )


def test_webull_bot_page_uses_polygon_data_halt_wording() -> None:
    settings = Settings(
        redis_stream_prefix="test",
        oms_adapter="alpaca_paper",
        strategy_webull_30s_enabled=True,
    )
    session_factory = build_test_session_factory()
    seed_database(session_factory)
    streams = make_streams(settings.redis_stream_prefix)
    strategy_state_stream = streams[f"{settings.redis_stream_prefix}:strategy-state"]
    strategy_state_event = StrategyStateSnapshotEvent.model_validate_json(
        strategy_state_stream[0][1]["data"]
    )
    strategy_state_event.payload.bots.append(
        StrategyBotStatePayload(
            strategy_code="webull_30s",
            account_name="live:webull_30s",
            watchlist=["AUUD"],
            positions=[],
            pending_open_symbols=[],
            pending_close_symbols=[],
            pending_scale_levels=[],
            daily_pnl=0.0,
            data_health={
                "status": "critical",
                "halted_symbols": ["AUUD"],
                "reasons": {
                    "AUUD": "Polygon stream stale/disconnected; trading halted until live Polygon ticks recover"
                },
                "since": {"AUUD": "2026-04-24 06:00:00 AM ET"},
            },
        )
    )
    strategy_state_stream[0][1]["data"] = strategy_state_event.model_dump_json()
    redis = FakeRedis(streams)

    app = build_app(
        settings=settings,
        session_factory=session_factory,
        redis_client=redis,
        legacy_client=FakeLegacyClient(),
    )

    with TestClient(app) as client:
        bots = client.get("/api/bots")
        assert bots.status_code == 200
        webull_bot = next(item for item in bots.json()["bots"] if item["strategy_code"] == "webull_30s")
        assert webull_bot["data_health"]["status"] == "critical"

        webull_status = client.get("/botwebull")
        assert webull_status.status_code == 200
        assert webull_status.json()["listening_status"]["state"] == "DATA HALT"
        assert "Polygon stream stale/disconnected" in webull_status.json()["listening_status"]["detail"]

        webull_page = client.get("/bot/30s-webull")
        assert webull_page.status_code == 200
        assert "Polygon Data Halt" in webull_page.text
        assert "Polygon Data Health" in webull_page.text
        assert "Polygon stream stale/disconnected" in webull_page.text
        assert "Schwab Data Halt" not in webull_page.text
        assert "Schwab Data Health" not in webull_page.text


def test_control_plane_overview_and_dashboard_render() -> None:
    settings = Settings(
        redis_stream_prefix="test",
        oms_adapter="alpaca_paper",
        strategy_macd_30s_reclaim_enabled=True,
        strategy_macd_1m_enabled=True,
        strategy_tos_enabled=True,
        strategy_runner_enabled=True,
    )
    session_factory = build_test_session_factory()
    seed_database(session_factory)
    session_marker = current_scanner_session_start_utc().isoformat()
    with session_factory() as session:
        session.add(
            DashboardSnapshot(
                snapshot_type="scanner_alert_engine_state",
                payload={
                    "scanner_session_start_utc": session_marker,
                    "persisted_at": datetime.now(UTC).isoformat(),
                    "today_alerts": [
                        {
                            "type": "VOLUME_SPIKE",
                            "ticker": "UGRO",
                            "price": 2.55,
                            "bid": 2.54,
                            "ask": 2.55,
                            "volume": 900_000,
                            "float": 50_000,
                            "time": "09:55:00 AM ET",
                            "details": {"spike_mult": 5.2},
                        },
                        {
                            "type": "SQUEEZE_5MIN",
                            "ticker": "SBET",
                            "price": 3.10,
                            "bid": 3.09,
                            "ask": 3.10,
                            "volume": 1_200_000,
                            "float": 80_000,
                            "time": "09:56:30 AM ET",
                            "details": {"change_pct": 7.1, "price_5min_ago": 2.89},
                        },
                    ],
                    "recent_rejections": [
                        {
                            "ticker": "NTIP",
                            "price": 1.89,
                            "volume": 706_765,
                            "time": "08:33:10 AM ET",
                            "reasons": [
                                "volume_spike_gate_not_met",
                                "volume_gate_closed",
                                "squeeze_10min_waiting_for_volume_gate",
                            ],
                            "squeeze_5min_pct": 4.7,
                            "vol_5min": 49_000,
                            "expected_5min": 60_000,
                        }
                    ],
                },
            )
        )
        session.commit()
    redis = FakeRedis(make_streams(settings.redis_stream_prefix))

    app = build_app(
        settings=settings,
        session_factory=session_factory,
        redis_client=redis,
        legacy_client=FakeLegacyClient(),
    )

    with TestClient(app) as client:
        overview = client.get("/api/overview")
        assert overview.status_code == 200
        body = overview.json()
        assert body["counts"]["open_virtual_positions"] == 1
        assert body["virtual_positions"][0]["symbol"] == "UGRO"
        assert body["services"][0]["service_name"] == "strategy-engine"
        assert body["market_data"]["active_subscription_symbols"] == 1
        assert body["recent_orders"][0]["reason"] == "ENTRY_P1_MACD_CROSS"
        assert body["scanner"]["top_confirmed"][0]["ticker"] == "UGRO"
        assert body["scanner"]["all_confirmed_count"] == 2
        assert body["scanner"]["all_confirmed"][1]["ticker"] == "SBET"
        assert body["scanner"]["legacy_confirmed_symbols"] == ["UGRO", "SBET"]
        assert body["generated_at"].endswith("ET")
        assert body["services"][0]["observed_at"].endswith("ET")
        assert body["bots"][0]["strategy_code"] == "macd_30s"
        assert body["bots"][0]["watchlist"] == ["UGRO"]
        assert body["bots"][0]["execution_mode"] == "paper"
        assert body["bots"][0]["provider"] == "alpaca"
        assert body["bots"][0]["wiring_status"] == "paper/alpaca"
        assert body["reconciliation"]["latest_run"]["summary"]["cutover_confidence"] == 90
        assert body["reconciliation"]["findings"][0]["finding_type"] == "stuck_order"
        assert body["legacy_shadow"]["divergence"]["status"] == "drifted"
        assert body["legacy_shadow"]["divergence"]["confirmed_only_in_legacy"] == []
        assert body["strategy_runtime"]["watchlist"] == ["UGRO"]

        scanner = client.get("/api/scanner")
        assert scanner.status_code == 200
        scanner_body = scanner.json()
        assert scanner_body["scanner"]["watchlist"] == ["UGRO"]
        assert scanner_body["scanner"]["all_confirmed_count"] == 2
        assert scanner_body["scanner"]["bot_handoff_count"] == 1
        assert scanner_body["scanner"]["bot_handoff"][0]["ticker"] == "UGRO"
        assert scanner_body["scanner"]["top_confirmed"][0]["rank_score"] == 72.0
        assert scanner_body["scanner"]["top_confirmed"][0]["is_top5"] is True
        assert scanner_body["scanner"]["top_confirmed"][0]["is_handed_to_bot"] is True
        assert scanner_body["scanner"]["top_confirmed"][0]["article_count"] == 3
        assert scanner_body["scanner"]["top_confirmed"][0]["path_a_eligible"] is True
        assert scanner_body["scanner"]["five_pillars_count"] == 1
        assert scanner_body["scanner"]["recent_alerts_count"] == 1

        bots = client.get("/api/bots")
        assert bots.status_code == 200
        bots_body = bots.json()
        bot_30s = next(item for item in bots_body["bots"] if item["strategy_code"] == "macd_30s")
        bot_1m = next(item for item in bots_body["bots"] if item["strategy_code"] == "macd_1m")
        assert bot_30s["display_name"] == "Schwab 30 Sec Bot"
        assert bot_30s["recent_intents"][0]["symbol"] == "UGRO"
        assert bot_30s["legacy_status"] == "not_available"
        assert bot_30s["daily_pnl"] == 125.5
        assert bot_30s["account_summary"]["account_position_count"] == 2
        assert bot_30s["account_summary"]["non_strategy_symbol_count"] == 1
        assert bot_30s["account_summary"]["non_strategy_symbols"] == ["SBET"]
        assert bot_1m["tos_parity"]["comparison_target"] == "thinkorswim_1m"
        assert bot_1m["tos_parity"]["snapshots"][0]["symbol"] == "UGRO"

        legacy_scanner = client.get("/scanner/dashboard")
        assert legacy_scanner.status_code == 200
        assert "Momentum Scanner Dashboard" in legacy_scanner.text
        assert "Scanner Deck" in legacy_scanner.text
        assert "Dedicated scanner workspace for the new platform" in legacy_scanner.text
        assert "Mai Tai 30s Reclaim" in legacy_scanner.text
        assert "SBET" in legacy_scanner.text
        assert "5 Pillars Scanner" in legacy_scanner.text
        assert "Top Gainers" in legacy_scanner.text
        assert "Momentum Alerts" in legacy_scanner.text
        assert "Export Today CSV (2)" in legacy_scanner.text
        assert "Recent Alert Rejections" in legacy_scanner.text
        assert "NTIP" in legacy_scanner.text
        assert "Top Gainer Changes" in legacy_scanner.text
        assert "Catalyst" in legacy_scanner.text
        assert "Entry Price" in legacy_scanner.text
        assert "Quantum Biopharma Wins Hospital Supply Agreement" in legacy_scanner.text
        assert "91% conf" in legacy_scanner.text
        assert "55m old" in legacy_scanner.text
        assert "PATH A ready" in legacy_scanner.text
        assert "📰" in legacy_scanner.text
        assert "🚫" in legacy_scanner.text

        scanner_confirmed = client.get("/scanner/confirmed")
        assert scanner_confirmed.status_code == 200
        assert scanner_confirmed.json()["count"] == 2

        scanner_pillars = client.get("/scanner/pillars")
        assert scanner_pillars.status_code == 200
        assert scanner_pillars.json()["stocks"][0]["ticker"] == "UGRO"

        scanner_alerts = client.get("/scanner/alerts")
        assert scanner_alerts.status_code == 200
        assert scanner_alerts.json()["count"] == 1
        assert scanner_alerts.json()["today_alerts_count"] == 2
        assert scanner_alerts.json()["diagnostics"][0]["ticker"] == "NTIP"

        scanner_alert_export = client.get("/scanner/alerts/export.csv")
        assert scanner_alert_export.status_code == 200
        assert "text/csv" in scanner_alert_export.headers["content-type"]
        csv_rows = list(csv.DictReader(scanner_alert_export.text.splitlines()))
        assert [row["ticker"] for row in csv_rows] == ["UGRO", "SBET"]

        bot_30s_page = client.get("/bot/30s")
        assert bot_30s_page.status_code == 200
        assert "Schwab 30 Sec Bot" in bot_30s_page.text
        assert "Execution Workspace" in bot_30s_page.text
        assert "Open Positions" in bot_30s_page.text
        assert "Completed Positions" in bot_30s_page.text
        assert "Order History" in bot_30s_page.text
        assert "Decision Tape" in bot_30s_page.text
        assert "SBET" in bot_30s_page.text

        bot_1m_page = client.get("/bot/1m")
        assert bot_1m_page.status_code == 200
        assert "TOS Parity" in bot_1m_page.text
        assert "EMA9" in bot_1m_page.text
        assert "Compare on closed bars only" in bot_1m_page.text
        assert "tight" in bot_1m_page.text
        assert "watch" in bot_1m_page.text


def test_control_plane_marks_schwab_data_halt_red_on_bot_page() -> None:
    settings = Settings(
        redis_stream_prefix="test",
        oms_adapter="alpaca_paper",
        strategy_macd_30s_broker_provider="schwab",
    )
    session_factory = build_test_session_factory()
    seed_database(session_factory)
    streams = make_streams(settings.redis_stream_prefix)
    strategy_state_stream = streams[f"{settings.redis_stream_prefix}:strategy-state"]
    strategy_state_event = StrategyStateSnapshotEvent.model_validate_json(
        strategy_state_stream[0][1]["data"]
    )
    strategy_state_event.payload.bots[0].data_health = {
        "status": "critical",
        "halted_symbols": ["UGRO"],
        "reasons": {
            "UGRO": "Schwab stream stale/disconnected; trading halted until live Schwab ticks recover"
        },
        "since": {"UGRO": "2026-03-28 10:00:00 AM ET"},
    }
    strategy_state_event.payload.bots[0].positions = []
    strategy_state_stream[0][1]["data"] = strategy_state_event.model_dump_json()
    redis = FakeRedis(streams)

    app = build_app(
        settings=settings,
        session_factory=session_factory,
        redis_client=redis,
        legacy_client=FakeLegacyClient(),
    )

    with TestClient(app) as client:
        bots = client.get("/api/bots")
        assert bots.status_code == 200
        bot_30s = next(item for item in bots.json()["bots"] if item["strategy_code"] == "macd_30s")
        assert bot_30s["data_health"]["status"] == "critical"
        bot_30s_status = client.get("/bot")
        assert bot_30s_status.status_code == 200
        assert bot_30s_status.json()["listening_status"]["state"] == "DATA HALT"
        assert (
            bot_30s_status.json()["listening_status"]["detail"]
            == "Schwab stream stale/disconnected; trading halted until live Schwab ticks recover"
        )

        bot_30s_page = client.get("/bot/30s")
        assert bot_30s_page.status_code == 200
        assert "Schwab Data Halt" in bot_30s_page.text
        assert "DATA HALT" in bot_30s_page.text
        assert "there are no open positions currently exposed to the emergency-close path" in bot_30s_page.text
        assert "open positions are being closed" not in bot_30s_page.text


def test_bot_page_renders_simple_trade_summary_table() -> None:
    settings = Settings(redis_stream_prefix="test", oms_adapter="alpaca_paper")
    session_factory = build_test_session_factory()
    seed_database(session_factory)
    redis = FakeRedis(make_streams(settings.redis_stream_prefix))

    app = build_app(
        settings=settings,
        session_factory=session_factory,
        redis_client=redis,
        legacy_client=FakeLegacyClient(),
    )

    with TestClient(app) as client:
        bot_30s_page = client.get("/bot/30s")
        assert bot_30s_page.status_code == 200
        assert "Completed Positions" in bot_30s_page.text
        assert "Completed trade cycles for this bot, including positions that finished by scale-out." in bot_30s_page.text
        assert "Open Positions" in bot_30s_page.text
        assert "Order History" in bot_30s_page.text
        assert "Mai Tai Scanner" in bot_30s_page.text
        assert "Mai Tai Control Plane" in bot_30s_page.text


def test_bot_page_can_render_and_update_manual_stop_symbols() -> None:
    settings = Settings(redis_stream_prefix="test", oms_adapter="alpaca_paper")
    session_factory = build_test_session_factory()
    seed_database(session_factory)
    current_session_start = current_scanner_session_start_utc()
    with session_factory() as session:
        session.add(
            DashboardSnapshot(
                snapshot_type="bot_manual_stop_symbols",
                payload={
                    "bots": {"macd_30s": ["SBET"]},
                    "scanner_session_start_utc": current_session_start.isoformat(),
                },
            )
        )
        session.commit()
    redis = FakeRedis(make_streams(settings.redis_stream_prefix))

    app = build_app(
        settings=settings,
        session_factory=session_factory,
        redis_client=redis,
        legacy_client=FakeLegacyClient(),
    )

    with TestClient(app) as client:
        bot_page = client.get("/bot/30s")
        assert bot_page.status_code == 200
        assert "Manual Stops" in bot_page.text
        assert "SBET" in bot_page.text

        response = client.get("/bot/symbol/stop?strategy_code=macd_30s&symbol=UGRO&redirect_to=/bot/30s")
        assert response.status_code == 200

    with session_factory() as session:
        snapshot = session.scalar(
            select(DashboardSnapshot)
            .where(DashboardSnapshot.snapshot_type == "bot_manual_stop_symbols")
            .order_by(DashboardSnapshot.created_at.desc())
        )
        assert snapshot is not None
        assert snapshot.payload["bots"] == {"macd_30s": ["SBET", "UGRO"]}
        assert "scanner_session_start_utc" in snapshot.payload


def test_bot_page_renders_full_live_watchlist_without_ten_symbol_cap() -> None:
    settings = Settings(redis_stream_prefix="test", oms_adapter="alpaca_paper")
    session_factory = build_test_session_factory()
    seed_database(session_factory)
    streams = make_streams(settings.redis_stream_prefix)
    strategy_state_stream = streams[f"{settings.redis_stream_prefix}:strategy-state"]
    strategy_state_event = StrategyStateSnapshotEvent.model_validate_json(
        strategy_state_stream[0][1]["data"]
    )
    full_watchlist = [
        "APLZ",
        "ATOM",
        "AUUD",
        "CAST",
        "ENVB",
        "HKIT",
        "IONZ",
        "IQST",
        "LIDR",
        "NBIZ",
        "NTIP",
        "PBM",
    ]
    strategy_state_event.payload.bots[0].watchlist = full_watchlist
    strategy_state_event.payload.bots[0].positions = []
    strategy_state_event.payload.bots[0].pending_open_symbols = []
    strategy_state_event.payload.bots[0].pending_close_symbols = []
    strategy_state_stream[0][1]["data"] = strategy_state_event.model_dump_json()
    redis = FakeRedis(streams)

    app = build_app(
        settings=settings,
        session_factory=session_factory,
        redis_client=redis,
        legacy_client=FakeLegacyClient(),
    )

    with TestClient(app) as client:
        bot_page = client.get("/bot/30s")
        assert bot_page.status_code == 200
        for symbol in full_watchlist:
            assert symbol in bot_page.text


def test_scanner_page_can_render_and_update_global_manual_stop_symbols() -> None:
    settings = Settings(redis_stream_prefix="test", oms_adapter="alpaca_paper")
    session_factory = build_test_session_factory()
    seed_database(session_factory)
    current_session_start = current_scanner_session_start_utc()
    with session_factory() as session:
        session.add(
            DashboardSnapshot(
                snapshot_type="global_manual_stop_symbols",
                payload={
                    "symbols": ["SBET"],
                    "scanner_session_start_utc": current_session_start.isoformat(),
                },
            )
        )
        session.commit()
    redis = FakeRedis(make_streams(settings.redis_stream_prefix))

    app = build_app(
        settings=settings,
        session_factory=session_factory,
        redis_client=redis,
        legacy_client=FakeLegacyClient(),
    )

    with TestClient(app) as client:
        scanner_page = client.get("/scanner/dashboard")
        assert scanner_page.status_code == 200
        assert "Global Manual Stops" in scanner_page.text
        assert "SBET" in scanner_page.text

        response = client.get("/scanner/symbol/stop?symbol=UGRO&redirect_to=/scanner/dashboard")
        assert response.status_code == 200

    with session_factory() as session:
        snapshot = session.scalar(
            select(DashboardSnapshot)
            .where(DashboardSnapshot.snapshot_type == "global_manual_stop_symbols")
            .order_by(DashboardSnapshot.created_at.desc())
        )
        assert snapshot is not None
        assert snapshot.payload["symbols"] == ["SBET", "UGRO"]
        assert "scanner_session_start_utc" in snapshot.payload


def test_control_plane_ignores_manual_stop_snapshot_from_wrong_session_marker() -> None:
    settings = Settings(redis_stream_prefix="test", oms_adapter="alpaca_paper")
    session_factory = build_test_session_factory()
    seed_database(session_factory)
    current_session_start = current_scanner_session_start_utc()
    with session_factory() as session:
        session.add(
            DashboardSnapshot(
                snapshot_type="bot_manual_stop_symbols",
                payload={
                    "bots": {"macd_30s": ["SBET"]},
                    "scanner_session_start_utc": (current_session_start - timedelta(days=1)).isoformat(),
                },
                created_at=datetime.now(UTC),
            )
        )
        session.commit()
    redis = FakeRedis(make_streams(settings.redis_stream_prefix))

    app = build_app(
        settings=settings,
        session_factory=session_factory,
        redis_client=redis,
        legacy_client=FakeLegacyClient(),
    )

    with TestClient(app) as client:
        bots = client.get("/api/bots")
        assert bots.status_code == 200
        macd_bot = next(item for item in bots.json()["bots"] if item["strategy_code"] == "macd_30s")
        assert macd_bot["manual_stop_symbols"] == []


def test_control_plane_ignores_markerless_manual_stop_snapshot_even_if_created_this_session() -> None:
    settings = Settings(redis_stream_prefix="test", oms_adapter="alpaca_paper")
    session_factory = build_test_session_factory()
    seed_database(session_factory)
    current_session_start = current_scanner_session_start_utc()
    with session_factory() as session:
        session.add(
            DashboardSnapshot(
                snapshot_type="bot_manual_stop_symbols",
                payload={"bots": {"macd_30s": ["SBET"]}},
                created_at=current_session_start + timedelta(hours=2),
            )
        )
        session.commit()
    redis = FakeRedis(make_streams(settings.redis_stream_prefix))

    app = build_app(
        settings=settings,
        session_factory=session_factory,
        redis_client=redis,
        legacy_client=FakeLegacyClient(),
    )

    with TestClient(app) as client:
        bots = client.get("/api/bots")
        assert bots.status_code == 200
        macd_bot = next(item for item in bots.json()["bots"] if item["strategy_code"] == "macd_30s")
        assert macd_bot["manual_stop_symbols"] == []


def test_setting_bot_manual_stop_symbol_does_not_merge_markerless_snapshot() -> None:
    settings = Settings(redis_stream_prefix="test", oms_adapter="alpaca_paper")
    session_factory = build_test_session_factory()
    seed_database(session_factory)
    current_session_start = current_scanner_session_start_utc()
    with session_factory() as session:
        session.add(
            DashboardSnapshot(
                snapshot_type="bot_manual_stop_symbols",
                payload={"bots": {"macd_30s": ["SBET"]}},
                created_at=current_session_start + timedelta(hours=2),
            )
        )
        session.commit()
    redis = FakeRedis(make_streams(settings.redis_stream_prefix))

    app = build_app(
        settings=settings,
        session_factory=session_factory,
        redis_client=redis,
        legacy_client=FakeLegacyClient(),
    )

    with TestClient(app) as client:
        response = client.get("/bot/symbol/stop?strategy_code=macd_30s&symbol=UGRO&redirect_to=/bot/30s")
        assert response.status_code == 200

    with session_factory() as session:
        snapshot = session.scalar(
            select(DashboardSnapshot)
            .where(DashboardSnapshot.snapshot_type == "bot_manual_stop_symbols")
            .order_by(DashboardSnapshot.created_at.desc())
        )
        assert snapshot is not None
        assert snapshot.payload["bots"] == {"macd_30s": ["UGRO"]}
        assert snapshot.payload["scanner_session_start_utc"] == current_session_start.isoformat()


def test_control_plane_treats_fresh_market_data_as_live_when_heartbeat_lags() -> None:
    settings = Settings(
        redis_stream_prefix="test",
        oms_adapter="alpaca_paper",
        strategy_macd_1m_enabled=True,
        strategy_tos_enabled=True,
        strategy_runner_enabled=True,
    )
    session_factory = build_test_session_factory()
    seed_database(session_factory)
    with session_factory() as session:
        latest_run = session.scalar(select(ReconciliationRun))
        assert latest_run is not None
        latest_run.summary = {**latest_run.summary, "total_findings": 0}
        session.commit()
    streams = make_streams(settings.redis_stream_prefix)
    market_data_heartbeat = HeartbeatEvent(
        source_service="market-data-gateway",
        payload=HeartbeatPayload(
            service_name="market-data-gateway",
            instance_name="market-data-1",
            status="stopping",
            details={"active_symbols": "3"},
        ),
    )
    streams[f"{settings.redis_stream_prefix}:heartbeats"].insert(
        0,
        ("2-0", {"data": market_data_heartbeat.model_dump_json()}),
    )
    redis = FakeRedis(streams)

    app = build_app(
        settings=settings,
        session_factory=session_factory,
        redis_client=redis,
        legacy_client=FakeLegacyClient(),
    )

    with TestClient(app) as client:
        overview = client.get("/api/overview")
        assert overview.status_code == 200
        body = overview.json()
        market_data_service = next(
            item for item in body["services"] if item["service_name"] == "market-data-gateway"
        )
        assert body["status"] == "healthy"
        assert market_data_service["status"] == "stopping"
        assert market_data_service["effective_status"] == "healthy"
        assert "Fresh snapshot/subscription activity" in market_data_service["status_note"]
        assert body["scanner"]["feed_status"] == "live"
        assert body["scanner"]["heartbeat_active_symbols"] == 3

        dashboard = client.get("/scanner/dashboard")
        assert dashboard.status_code == 200
        assert "Feed Note:" in dashboard.text
        assert "Heartbeat raw status stopping." in dashboard.text

        bot_runner_page = client.get("/bot/runner")
        assert bot_runner_page.status_code == 200
        assert "Runner Bot" in bot_runner_page.text
        assert "Current Runner Ride" in bot_runner_page.text
        assert "Completed Positions" in bot_runner_page.text

        reconciliation = client.get("/api/reconciliation")
        assert reconciliation.status_code == 200
        reconciliation_body = reconciliation.json()
        assert reconciliation_body["reconciliation"]["findings"][0]["title"] == "Order stuck in accepted for UGRO"

        shadow = client.get("/api/shadow")
        assert shadow.status_code == 200
        shadow_body = shadow.json()
        assert shadow_body["legacy_shadow"]["divergence"]["strategies"]["runner"]["new_present"] is False

        dashboard = client.get("/")
        assert dashboard.status_code == 200
        assert "Mai Tai Project" in dashboard.text
        assert "Mai Tai System Dock" in dashboard.text
        assert "Overview" in dashboard.text
        assert "Ranked Scanner View" in dashboard.text
        assert "Handed To Bots" in dashboard.text
        assert "Bot Deck" in dashboard.text
        assert "Active strategy runtimes configured in this environment." in dashboard.text
        assert "UGRO" in dashboard.text
        assert "Schwab 30 Sec Bot" in dashboard.text
        assert "paper/alpaca" in dashboard.text
        assert "TOS Parity" in dashboard.text
        assert "thinkorswim_1m" in dashboard.text


def test_control_plane_blacklist_routes_filter_scanner_outputs() -> None:
    settings = Settings(redis_stream_prefix="test", oms_adapter="alpaca_paper")
    session_factory = build_test_session_factory()
    seed_database(session_factory)
    redis = FakeRedis(make_streams(settings.redis_stream_prefix))

    app = build_app(
        settings=settings,
        session_factory=session_factory,
        redis_client=redis,
        legacy_client=FakeLegacyClient(),
    )

    with TestClient(app) as client:
        client.get("/scanner/blacklist/add?symbol=UGRO&reason=manual_test")

        blacklist = client.get("/api/blacklist")
        assert blacklist.status_code == 200
        assert blacklist.json()["count"] == 1
        assert blacklist.json()["blacklist"][0]["symbol"] == "UGRO"

        scanner = client.get("/api/scanner")
        assert scanner.status_code == 200
        scanner_body = scanner.json()["scanner"]
        assert scanner_body["blacklist_count"] == 1
        assert scanner_body["watchlist"] == []
        assert scanner_body["top_confirmed"] == []
        assert scanner_body["five_pillars"] == []
        assert scanner_body["top_gainers"] == []
        assert scanner_body["recent_alerts"] == []

        client.get("/scanner/blacklist/remove?symbol=UGRO")
        unblocked = client.get("/api/scanner")
        assert unblocked.status_code == 200
        assert unblocked.json()["scanner"]["blacklist_count"] == 0
        assert unblocked.json()["scanner"]["top_confirmed"][0]["ticker"] == "UGRO"


def test_control_plane_reports_schwab_live_wiring() -> None:
    settings = Settings(redis_stream_prefix="test", oms_adapter="schwab")
    session_factory = build_test_session_factory()
    seed_database(session_factory)
    redis = FakeRedis(make_streams(settings.redis_stream_prefix))

    app = build_app(
        settings=settings,
        session_factory=session_factory,
        redis_client=redis,
        legacy_client=FakeLegacyClient(),
    )

    with TestClient(app) as client:
        overview = client.get("/api/overview")
        assert overview.status_code == 200
        body = overview.json()
        assert body["provider"] == "schwab"
        assert body["bots"][0]["provider"] == "schwab"
        assert body["bots"][0]["execution_mode"] == "live"
        assert body["bots"][0]["wiring_status"] == "live/schwab"
        assert body["bots"][0]["account_display_name"] == "live:macd_30s"

        bot_page = client.get("/bot/30s")
        assert bot_page.status_code == 200
        assert "Account:</strong> live:macd_30s" in bot_page.text


def test_control_plane_hides_ignored_mismatch_symbols_from_ui() -> None:
    settings = Settings(
        redis_stream_prefix="test",
        oms_adapter="alpaca_paper",
        reconciliation_ignored_position_mismatches="paper:macd_30s:CYN,CANF",
    )
    session_factory = build_test_session_factory()
    seed_database(session_factory)

    with session_factory() as session:
        strategy = session.scalar(select(Strategy).where(Strategy.code == "macd_30s"))
        account = session.scalar(select(BrokerAccount).where(BrokerAccount.name == "paper:macd_30s"))
        assert strategy is not None
        assert account is not None

        cyn_intent = TradeIntent(
            strategy_id=strategy.id,
            broker_account_id=account.id,
            symbol="CYN",
            side="buy",
            intent_type="open",
            quantity=Decimal("10"),
            reason="ENTRY_TEST",
            status="filled",
            payload={},
        )
        canf_intent = TradeIntent(
            strategy_id=strategy.id,
            broker_account_id=account.id,
            symbol="CANF",
            side="buy",
            intent_type="open",
            quantity=Decimal("12"),
            reason="ENTRY_TEST",
            status="filled",
            payload={},
        )
        session.add_all([cyn_intent, canf_intent])
        session.flush()

        cyn_order = BrokerOrder(
            intent_id=cyn_intent.id,
            strategy_id=strategy.id,
            broker_account_id=account.id,
            client_order_id="macd_30s-CYN-open-1",
            broker_order_id="cyn-order-1",
            symbol="CYN",
            side="buy",
            order_type="market",
            time_in_force="day",
            quantity=Decimal("10"),
            status="filled",
            payload={},
            submitted_at=datetime.now(UTC),
        )
        canf_order = BrokerOrder(
            intent_id=canf_intent.id,
            strategy_id=strategy.id,
            broker_account_id=account.id,
            client_order_id="macd_30s-CANF-open-1",
            broker_order_id="canf-order-1",
            symbol="CANF",
            side="buy",
            order_type="market",
            time_in_force="day",
            quantity=Decimal("12"),
            status="filled",
            payload={},
            submitted_at=datetime.now(UTC),
        )
        session.add_all([cyn_order, canf_order])
        session.flush()

        session.add_all(
            [
                Fill(
                    order_id=cyn_order.id,
                    strategy_id=strategy.id,
                    broker_account_id=account.id,
                    broker_fill_id="cyn-fill-1",
                    symbol="CYN",
                    side="buy",
                    quantity=Decimal("10"),
                    price=Decimal("1.10"),
                    filled_at=datetime.now(UTC),
                    payload={},
                ),
                Fill(
                    order_id=canf_order.id,
                    strategy_id=strategy.id,
                    broker_account_id=account.id,
                    broker_fill_id="canf-fill-1",
                    symbol="CANF",
                    side="buy",
                    quantity=Decimal("12"),
                    price=Decimal("1.20"),
                    filled_at=datetime.now(UTC),
                    payload={},
                ),
                VirtualPosition(
                    strategy_id=strategy.id,
                    broker_account_id=account.id,
                    symbol="CYN",
                    quantity=Decimal("10"),
                    average_price=Decimal("1.10"),
                    realized_pnl=Decimal("0"),
                    opened_at=datetime.now(UTC),
                ),
                VirtualPosition(
                    strategy_id=strategy.id,
                    broker_account_id=account.id,
                    symbol="CANF",
                    quantity=Decimal("12"),
                    average_price=Decimal("1.20"),
                    realized_pnl=Decimal("0"),
                    opened_at=datetime.now(UTC),
                ),
                AccountPosition(
                    broker_account_id=account.id,
                    symbol="CYN",
                    quantity=Decimal("10"),
                    average_price=Decimal("1.10"),
                    market_value=Decimal("11.0"),
                    source_updated_at=datetime.now(UTC),
                ),
                AccountPosition(
                    broker_account_id=account.id,
                    symbol="CANF",
                    quantity=Decimal("12"),
                    average_price=Decimal("1.20"),
                    market_value=Decimal("14.4"),
                    source_updated_at=datetime.now(UTC),
                ),
                SystemIncident(
                    service_name="reconciler",
                    severity="critical",
                    title="Position quantity mismatch for CYN",
                    status="closed",
                    payload={"symbol": "CYN", "broker_account_name": "paper:macd_30s"},
                    opened_at=datetime.now(UTC),
                ),
                SystemIncident(
                    service_name="reconciler",
                    severity="critical",
                    title="Position quantity mismatch for CANF",
                    status="closed",
                    payload={"symbol": "CANF", "broker_account_name": "paper:macd_30s"},
                    opened_at=datetime.now(UTC),
                ),
            ]
        )
        session.commit()

    streams = make_streams(settings.redis_stream_prefix)
    strategy_state_stream = streams[f"{settings.redis_stream_prefix}:strategy-state"]
    strategy_state_event = StrategyStateSnapshotEvent.model_validate_json(
        strategy_state_stream[0][1]["data"]
    )
    strategy_state_event.payload.bots[0].watchlist = ["UGRO", "CYN", "CANF"]
    strategy_state_event.payload.bots[0].positions = [
        {"ticker": "UGRO", "quantity": 10},
        {"ticker": "CYN", "quantity": 10},
        {"ticker": "CANF", "quantity": 12},
    ]
    strategy_state_event.payload.bots[0].pending_open_symbols = ["CYN"]
    strategy_state_event.payload.bots[0].pending_close_symbols = ["CANF"]
    strategy_state_stream[0][1]["data"] = strategy_state_event.model_dump_json()
    redis = FakeRedis(streams)

    app = build_app(
        settings=settings,
        session_factory=session_factory,
        redis_client=redis,
        legacy_client=FakeLegacyClient(),
    )

    with TestClient(app) as client:
        overview = client.get("/api/overview")
        assert overview.status_code == 200
        body = overview.json()

        hidden_symbols = {"CYN", "CANF"}
        assert body["counts"]["open_virtual_positions"] == 1
        assert body["counts"]["open_account_positions"] == 2
        assert all(item["symbol"] not in hidden_symbols for item in body["virtual_positions"])
        assert all(item["symbol"] not in hidden_symbols for item in body["account_positions"])
        assert all(item["symbol"] not in hidden_symbols for item in body["recent_orders"])
        assert all(item["symbol"] not in hidden_symbols for item in body["recent_fills"])
        assert all(item["symbol"] not in hidden_symbols for item in body["recent_intents"])
        assert all("CYN" not in item["title"] and "CANF" not in item["title"] for item in body["incidents"])

        bot_30s = next(item for item in body["bots"] if item["strategy_code"] == "macd_30s")
        assert bot_30s["watchlist"] == ["UGRO"]
        assert [item["ticker"] for item in bot_30s["positions"]] == ["UGRO"]
        assert bot_30s["pending_open_symbols"] == []
        assert bot_30s["pending_close_symbols"] == []
        assert all(item["symbol"] not in hidden_symbols for item in bot_30s["recent_orders"])
        assert all(item["symbol"] not in hidden_symbols for item in bot_30s["recent_fills"])
        assert all(item["symbol"] not in hidden_symbols for item in bot_30s["recent_intents"])

        bot_page = client.get("/bot/30s")
        assert bot_page.status_code == 200
        assert "UGRO" in bot_page.text
        assert "CYN" not in bot_page.text
        assert "CANF" not in bot_page.text


def test_control_plane_filters_recent_orders_and_fills_to_current_eastern_day(monkeypatch) -> None:
    settings = Settings(redis_stream_prefix="test", oms_adapter="alpaca_paper")
    session_factory = build_test_session_factory()
    seed_database(session_factory)

    fixed_now = datetime(2026, 3, 31, 14, 0, tzinfo=UTC)
    monkeypatch.setattr("project_mai_tai.services.control_plane.utcnow", lambda: fixed_now)

    with session_factory() as session:
        strategy = session.scalar(select(Strategy).where(Strategy.code == "macd_30s"))
        account = session.scalar(select(BrokerAccount).where(BrokerAccount.name == "paper:macd_30s"))
        assert strategy is not None
        assert account is not None

        old_order = BrokerOrder(
            intent_id=None,
            strategy_id=strategy.id,
            broker_account_id=account.id,
            client_order_id="macd_30s-OLD-open-1",
            broker_order_id="old-order-1",
            symbol="OLD",
            side="buy",
            order_type="market",
            time_in_force="day",
            quantity=Decimal("10"),
            status="filled",
            payload={},
            submitted_at=datetime(2026, 3, 30, 14, 0, tzinfo=UTC),
            updated_at=datetime(2026, 3, 30, 14, 1, tzinfo=UTC),
        )
        old_fill = Fill(
            order_id=None,
            strategy_id=strategy.id,
            broker_account_id=account.id,
            broker_fill_id="old-fill-1",
            symbol="OLD",
            side="buy",
            quantity=Decimal("10"),
            price=Decimal("1.00"),
            filled_at=datetime(2026, 3, 30, 14, 1, tzinfo=UTC),
            payload={},
        )
        today_order = BrokerOrder(
            intent_id=None,
            strategy_id=strategy.id,
            broker_account_id=account.id,
            client_order_id="macd_30s-NEW-open-1",
            broker_order_id="new-order-1",
            symbol="NEW",
            side="buy",
            order_type="market",
            time_in_force="day",
            quantity=Decimal("10"),
            status="filled",
            payload={},
            submitted_at=datetime(2026, 3, 31, 13, 0, tzinfo=UTC),
            updated_at=datetime(2026, 3, 31, 13, 1, tzinfo=UTC),
        )
        session.add_all([old_order, today_order])
        session.flush()

        old_fill.order_id = old_order.id
        today_fill = Fill(
            order_id=today_order.id,
            strategy_id=strategy.id,
            broker_account_id=account.id,
            broker_fill_id="new-fill-1",
            symbol="NEW",
            side="buy",
            quantity=Decimal("10"),
            price=Decimal("2.00"),
            filled_at=datetime(2026, 3, 31, 13, 1, tzinfo=UTC),
            payload={},
        )
        session.add_all([old_fill, today_fill])
        session.commit()

    redis = FakeRedis(make_streams(settings.redis_stream_prefix))
    app = build_app(
        settings=settings,
        session_factory=session_factory,
        redis_client=redis,
        legacy_client=FakeLegacyClient(),
    )

    with TestClient(app) as client:
        bots = client.get("/api/bots")
        assert bots.status_code == 200
        bot_30s = next(item for item in bots.json()["bots"] if item["strategy_code"] == "macd_30s")
        assert all(item["symbol"] != "OLD" for item in bot_30s["recent_orders"])
        assert all(item["symbol"] != "OLD" for item in bot_30s["recent_fills"])
        assert any(item["symbol"] == "NEW" for item in bot_30s["recent_orders"])
        assert any(item["symbol"] == "NEW" for item in bot_30s["recent_fills"])


def test_control_plane_restores_last_nonempty_confirmed_snapshot() -> None:
    settings = Settings(redis_stream_prefix="test", oms_adapter="alpaca_paper")
    session_factory = build_test_session_factory()
    seed_database(session_factory)
    redis = FakeRedis(make_streams(settings.redis_stream_prefix, include_confirmed=False, live_price=2.61))

    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(
            "project_mai_tai.services.control_plane.current_scanner_session_start_utc",
            lambda now=None: datetime(2026, 3, 28, 13, 0, tzinfo=UTC),
        )
        app = build_app(
            settings=settings,
            session_factory=session_factory,
            redis_client=redis,
            legacy_client=FakeLegacyClient(),
        )

        with TestClient(app) as client:
            scanner = client.get("/api/scanner")
            assert scanner.status_code == 200
            scanner_body = scanner.json()["scanner"]
            assert scanner_body["top_confirmed_source"] == "restored"
            assert scanner_body["top_confirmed_count"] == 1
            assert scanner_body["top_confirmed"][0]["ticker"] == "UGRO"
            assert scanner_body["watchlist"] == ["UGRO"]
            assert scanner_body["top_confirmed_snapshot_at"] == "2026-03-28T14:00:00+00:00"

            dashboard = client.get("/scanner/dashboard")
            assert dashboard.status_code == 200
            assert "UGRO" in dashboard.text
            assert "Quantum Biopharma Wins Hospital Supply Agreement" in dashboard.text


def test_control_plane_skips_restored_confirmed_snapshot_from_prior_scanner_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings(redis_stream_prefix="test", oms_adapter="alpaca_paper")
    session_factory = build_test_session_factory()
    seed_database(session_factory)
    redis = FakeRedis(make_streams(settings.redis_stream_prefix, include_confirmed=False, live_price=2.61))

    monkeypatch.setattr(
        "project_mai_tai.services.control_plane.current_scanner_session_start_utc",
        lambda now=None: datetime(2026, 3, 28, 15, 0, tzinfo=UTC),
    )

    app = build_app(
        settings=settings,
        session_factory=session_factory,
        redis_client=redis,
        legacy_client=FakeLegacyClient(),
    )

    with TestClient(app) as client:
        scanner = client.get("/api/scanner")
        assert scanner.status_code == 200
        scanner_body = scanner.json()["scanner"]
        assert scanner_body["top_confirmed_source"] == "idle"
        assert scanner_body["top_confirmed_count"] == 0
        assert scanner_body["top_confirmed"] == []


def test_legacy_divergence_uses_new_confirmed_not_watchlist() -> None:
    settings = Settings(redis_stream_prefix="test", oms_adapter="alpaca_paper")
    session_factory = build_test_session_factory()
    seed_database(session_factory)
    redis = FakeRedis(make_streams(settings.redis_stream_prefix, include_confirmed=False))

    app = build_app(
        settings=settings,
        session_factory=session_factory,
        redis_client=redis,
        legacy_client=FakeLegacyClient(),
    )

    with TestClient(app) as client:
        overview = client.get("/api/overview")
        assert overview.status_code == 200
        divergence = overview.json()["legacy_shadow"]["divergence"]
        assert divergence["status"] == "drifted"
        assert divergence["confirmed_only_in_legacy"] == ["SBET", "UGRO"]
        assert divergence["confirmed_only_in_new"] == []


def test_control_plane_recovers_after_transient_redis_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = Settings(redis_stream_prefix="test", oms_adapter="alpaca_paper")
    session_factory = build_test_session_factory()
    seed_database(session_factory)
    redis = FakeRedis(make_streams(settings.redis_stream_prefix))
    redis.fail_next = True
    monkeypatch.setattr(
        "project_mai_tai.services.control_plane.Redis.from_url",
        lambda *args, **kwargs: redis,
    )

    app = build_app(
        settings=settings,
        session_factory=session_factory,
        redis_client=redis,
        legacy_client=FakeLegacyClient(),
    )

    with TestClient(app) as client:
        response = client.get("/health")
        assert response.status_code == 200
        body = response.json()
        assert body["redis_connected"] is True
