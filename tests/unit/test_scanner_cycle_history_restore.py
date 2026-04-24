from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from project_mai_tai.db.base import Base
from project_mai_tai.db.models import DashboardSnapshot
from project_mai_tai.services.strategy_engine_app import (
    StrategyEngineService,
    current_scanner_session_start_utc,
)
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


def build_test_session_factory() -> sessionmaker:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        future=True,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


def test_scanner_cycle_history_restore_skips_watchlist_only_snapshot_without_session_handoff_marker(
    monkeypatch,
) -> None:
    fixed_now = datetime(2026, 4, 24, 10, 5, tzinfo=UTC)
    session_start = current_scanner_session_start_utc(fixed_now)
    session_factory = build_test_session_factory()

    with session_factory() as session:
        session.add(
            DashboardSnapshot(
                snapshot_type="scanner_cycle_history",
                payload={
                    "persisted_at": fixed_now.isoformat(),
                    "scanner_session_start_utc": session_start.isoformat(),
                    "watchlist": ["AMST"],
                    "bot_handoff_symbols_by_strategy": {
                        "macd_30s": ["AMST"],
                        "webull_30s": ["AMST"],
                    },
                    "bot_handoff_history_by_strategy": {
                        "macd_30s": ["AMST"],
                        "webull_30s": ["AMST"],
                    },
                },
            )
        )
        session.commit()

    monkeypatch.setattr(
        "project_mai_tai.services.strategy_engine_app.utcnow",
        lambda: fixed_now,
    )

    service = StrategyEngineService(
        settings=Settings(
            redis_stream_prefix="test",
            dashboard_snapshot_persistence_enabled=True,
            scanner_feed_retention_enabled=False,
            strategy_webull_30s_enabled=True,
        ),
        redis_client=FakeRedis(),
        session_factory=session_factory,
    )
    service._restore_watchlist_from_scanner_cycle_history()

    assert service.state.bots["macd_30s"].watchlist == set()
    assert service.state.bots["webull_30s"].watchlist == set()


def test_scanner_cycle_history_restore_seeds_watchlist_when_session_handoff_marker_is_true(
    monkeypatch,
) -> None:
    fixed_now = datetime(2026, 4, 24, 10, 5, tzinfo=UTC)
    session_start = current_scanner_session_start_utc(fixed_now)
    session_factory = build_test_session_factory()

    with session_factory() as session:
        session.add(
            DashboardSnapshot(
                snapshot_type="scanner_cycle_history",
                payload={
                    "persisted_at": fixed_now.isoformat(),
                    "scanner_session_start_utc": session_start.isoformat(),
                    "watchlist": ["AMST"],
                    "session_handoff_active": True,
                    "bot_handoff_symbols_by_strategy": {
                        "macd_30s": ["AMST"],
                        "webull_30s": ["AMST"],
                    },
                    "bot_handoff_history_by_strategy": {
                        "macd_30s": ["AMST"],
                        "webull_30s": ["AMST"],
                    },
                },
            )
        )
        session.commit()

    monkeypatch.setattr(
        "project_mai_tai.services.strategy_engine_app.utcnow",
        lambda: fixed_now,
    )

    service = StrategyEngineService(
        settings=Settings(
            redis_stream_prefix="test",
            dashboard_snapshot_persistence_enabled=True,
            scanner_feed_retention_enabled=False,
            strategy_webull_30s_enabled=True,
        ),
        redis_client=FakeRedis(),
        session_factory=session_factory,
    )
    service._restore_watchlist_from_scanner_cycle_history()

    assert service.state.bots["macd_30s"].watchlist == {"AMST"}
    assert service.state.bots["webull_30s"].watchlist == {"AMST"}

    with session_factory() as session:
        snapshots = session.scalars(
            select(DashboardSnapshot).where(DashboardSnapshot.snapshot_type == "scanner_cycle_history")
        ).all()
    assert len(snapshots) == 1
