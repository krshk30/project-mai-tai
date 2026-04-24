from __future__ import annotations

from datetime import datetime

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from project_mai_tai.broker_adapters.routing import RoutingBrokerAdapter
from project_mai_tai.broker_adapters.webull import WebullBrokerAdapter
from project_mai_tai.db.base import Base
from project_mai_tai.oms.service import OmsRiskService
from project_mai_tai.runtime_registry import (
    configured_broker_account_registrations,
    strategy_registration_map,
)
from project_mai_tai.services.control_plane import BOT_PAGE_META
from project_mai_tai.services.strategy_engine_app import StrategyEngineState
from project_mai_tai.settings import Settings


class FakeRedis:
    async def xadd(self, *_args, **_kwargs):
        return "1-0"

    async def xread(self, *_args, **_kwargs):
        return []

    async def ping(self):
        return True

    async def aclose(self):
        return None


def fixed_now() -> datetime:
    return datetime(2026, 4, 23, 10, 0)


def build_test_session_factory() -> sessionmaker:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        future=True,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


def test_runtime_registry_registers_webull_30s_as_live_webull() -> None:
    settings = Settings(
        oms_adapter="schwab",
        strategy_webull_30s_enabled=True,
        scanner_feed_retention_enabled=False,
    )

    registrations = strategy_registration_map(settings)
    broker_accounts = {item.name: item for item in configured_broker_account_registrations(settings)}

    assert registrations["macd_30s"].display_name == "Schwab 30 Sec Bot"
    assert registrations["webull_30s"].display_name == "Webull 30 Sec Bot"
    assert registrations["webull_30s"].execution_mode == "live"
    assert registrations["webull_30s"].metadata["provider"] == "webull"
    assert broker_accounts[settings.strategy_webull_30s_account_name].provider == "webull"


def test_strategy_state_routes_webull_30s_through_polygon_market_data_path() -> None:
    state = StrategyEngineState(
        settings=Settings(
            strategy_macd_30s_broker_provider="schwab",
            strategy_webull_30s_enabled=True,
            scanner_feed_retention_enabled=False,
        ),
        now_provider=fixed_now,
    )

    assert "webull_30s" in state.bots
    assert state.bots["webull_30s"].definition.display_name == "Webull 30 Sec Bot"
    assert state.schwab_stream_strategy_codes() == ("macd_30s",)

    state._record_bot_handoff_symbols([{"ticker": "UGRO"}], strategy_codes=["webull_30s"])
    state._resync_bot_watchlists_from_current_confirmed(strategy_codes=["webull_30s"])

    assert "UGRO" in state.bots["webull_30s"].watchlist
    assert "UGRO" in state.market_data_symbols()
    assert "UGRO" not in state.schwab_stream_symbols()


def test_oms_service_builds_webull_provider_inside_mixed_router() -> None:
    service = OmsRiskService(
        settings=Settings(
            redis_stream_prefix="test",
            oms_adapter="schwab",
            strategy_macd_30s_broker_provider="schwab",
            strategy_webull_30s_enabled=True,
        ),
        redis_client=FakeRedis(),
        session_factory=build_test_session_factory(),
    )

    assert isinstance(service.broker_adapter, RoutingBrokerAdapter)
    assert isinstance(service._build_provider_adapter("webull"), WebullBrokerAdapter)


def test_control_plane_meta_includes_webull_and_renamed_schwab_bot() -> None:
    assert BOT_PAGE_META["macd_30s"]["title"] == "Schwab 30 Sec Bot"
    assert BOT_PAGE_META["webull_30s"]["title"] == "Webull 30 Sec Bot"
    assert BOT_PAGE_META["webull_30s"]["path"] == "/bot/30s-webull"


def test_restore_confirmed_runtime_view_seeds_new_webull_bot_from_confirmed_state() -> None:
    state = StrategyEngineState(
        settings=Settings(
            strategy_macd_30s_broker_provider="schwab",
            strategy_webull_30s_enabled=True,
            scanner_feed_retention_enabled=False,
        ),
        now_provider=fixed_now,
    )

    confirmed = [{"ticker": "UGRO", "score": 7}]
    state.restore_confirmed_runtime_view(
        confirmed,
        all_confirmed=confirmed,
        bot_handoff_symbols_by_strategy={"macd_30s": ["UGRO"]},
        bot_handoff_history_by_strategy={"macd_30s": ["UGRO"]},
    )

    assert "UGRO" in state.bots["macd_30s"].watchlist
    assert "UGRO" in state.bots["webull_30s"].watchlist
