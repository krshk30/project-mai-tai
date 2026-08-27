from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from project_mai_tai.db.models import Base, DashboardSnapshot
from project_mai_tai.fanout_segment_store import FanoutSegmentIdentityStore
from project_mai_tai.services.schwab_1m_v2_bot import SchwabV2BotService
from project_mai_tai.settings import Settings


NOW = datetime(2026, 8, 27, 15, 0, tzinfo=UTC)


def _session_factory():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine, tables=[DashboardSnapshot.__table__])
    return sessionmaker(bind=engine, expire_on_commit=False)


def test_active_identity_survives_a_new_store_instance_then_release_clears_it() -> None:
    factory = _session_factory()
    first_process = FanoutSegmentIdentityStore(factory)
    first_process.record("yygh", 1787830200000, True, "segment_bind", now=NOW)

    restarted_process = FanoutSegmentIdentityStore(factory)
    assert restarted_process.restore_active(now=NOW + timedelta(minutes=5)) == {
        "YYGH": 1787830200000
    }

    restarted_process.record(
        "YYGH",
        1787830200000,
        False,
        "entry-window-close",
        now=NOW + timedelta(minutes=6),
    )
    assert restarted_process.restore_active(now=NOW + timedelta(minutes=7)) == {}


def test_previous_session_identity_is_not_restored() -> None:
    factory = _session_factory()
    store = FanoutSegmentIdentityStore(factory)
    store.record("DAIC", 1787743800000, True, "segment_bind", now=NOW)

    assert store.restore_active(now=NOW + timedelta(days=1)) == {}


def test_bot_startup_path_holds_restore_until_the_real_strategy_needs_it() -> None:
    factory = _session_factory()
    store = FanoutSegmentIdentityStore(factory)
    store.record("YYGH", 1787830200000, True, "segment_bind")
    bot = SchwabV2BotService(
        Settings(strategy_schwab_1m_v2_dual_broker_fanout_enabled=True),
        session_factory=factory,
    )

    bot._configure_fanout_identity_store()

    state = bot.strategy.watchlist_state("YYGH")
    assert state.fanout_segment_id == 0
    assert bot.strategy._restored_fanout_segment_ids == {"YYGH": 1787830200000}
    assert bot.strategy._ensure_fanout_segment_id(state) == 1787830200000
    assert state.fanout_segment_id == 1787830200000
    assert bot.strategy._restored_fanout_segment_ids == {}
