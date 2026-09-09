from __future__ import annotations

import ast
import inspect
import textwrap
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from project_mai_tai.db.models import Base, DashboardSnapshot, OmsManagedPosition
from project_mai_tai.services.schwab_1m_v2_bot import SchwabV2BotService
from project_mai_tai.settings import Settings
from project_mai_tai.v2_flip_entry_ownership import (
    FlipEntryOwnershipRecord,
    FlipEntryOwnershipStore,
    SNAPSHOT_TYPE,
)


NOW = datetime(2026, 9, 9, 15, 0, tzinfo=UTC)


def _factory():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(
        engine,
        tables=[DashboardSnapshot.__table__, OmsManagedPosition.__table__],
    )
    return sessionmaker(bind=engine, expire_on_commit=False)


def _record() -> FlipEntryOwnershipRecord:
    return FlipEntryOwnershipRecord(
        symbol="FTFT",
        opportunity_id=1_788_973_000_000,
        phase="bound",
        flip_bar_ts=1_788_973_060_000,
        provisional_started_ms=1_788_973_009_000,
        fill_accounts=("live:orb", "live:schwab_1m_v2"),
        position_ids={
            "live:schwab_1m_v2": "schwab-row",
            "live:orb": "webull-row",
        },
        position_entry_ms={
            "live:schwab_1m_v2": 1_788_973_009_000,
            "live:orb": 1_788_973_010_000,
        },
    )


def test_active_owner_round_trips_and_inactive_transition_removes_it() -> None:
    store = FlipEntryOwnershipStore(_factory())
    record = _record()
    store.record(record, active=True, reason="buy_flip_bound", now=NOW)

    assert store.restore_active(now=NOW + timedelta(minutes=1)) == {"FTFT": record}

    store.record(record, active=False, reason="sell_flip_flat", now=NOW + timedelta(minutes=2))
    assert store.restore_active(now=NOW + timedelta(minutes=3)) == {}


def test_malformed_current_session_owner_fails_the_whole_restore_closed() -> None:
    factory = _factory()
    with factory() as session:
        session.add(
            DashboardSnapshot(
                snapshot_type=SNAPSHOT_TYPE,
                payload={"schema_version": 1, "symbol": "FTFT", "active": True},
                created_at=NOW,
            )
        )
        session.commit()

    with pytest.raises(ValueError, match="invalid flip ownership snapshot"):
        FlipEntryOwnershipStore(factory).restore_active(now=NOW + timedelta(minutes=1))


def test_previous_session_owner_is_not_restored() -> None:
    store = FlipEntryOwnershipStore(_factory())
    store.record(_record(), active=True, reason="buy_flip_bound", now=NOW)

    assert store.restore_active(now=NOW + timedelta(days=1)) == {}


def test_service_position_book_reads_both_live_accounts_in_one_population() -> None:
    factory = _factory()
    with factory() as session:
        for account, quantity in (("live:schwab_1m_v2", 2), ("live:orb", 1)):
            session.add(
                OmsManagedPosition(
                    strategy_code="schwab_1m_v2",
                    broker_account_name=account,
                    symbol="FTFT",
                    entry_price=Decimal("2.31"),
                    original_quantity=quantity,
                    current_quantity=quantity,
                    entry_path="ATR Flip",
                    entry_time=NOW,
                    status="open",
                )
            )
        session.commit()
    bot = SchwabV2BotService(
        Settings(
            strategy_schwab_1m_v2_flip_owned_first_entry_enabled=True,
            strategy_schwab_1m_v2_account_name="live:schwab_1m_v2",
            strategy_schwab_1m_v2_webull_account_name="live:orb",
        ),
        session_factory=factory,
    )

    book = bot._fetch_flip_position_book()

    assert book.readable is True
    assert {leg.account_name for leg in book.legs_by_symbol["FTFT"]} == {
        "live:schwab_1m_v2",
        "live:orb",
    }


def test_service_restores_owner_between_identity_and_outcome_bootstrap() -> None:
    tree = ast.parse(textwrap.dedent(inspect.getsource(SchwabV2BotService.run)))
    calls = [
        node.func.attr
        for node in sorted(
            (
                value
                for value in ast.walk(tree)
                if isinstance(value, ast.Call) and isinstance(value.func, ast.Attribute)
            ),
            key=lambda value: value.lineno,
        )
    ]

    identity = calls.index("_configure_fanout_identity_store")
    ownership = calls.index("_configure_flip_entry_ownership_store")
    outcomes = calls.index("_configure_fanout_outcome_journal")
    assert identity < ownership < outcomes


def test_flag_off_does_not_construct_or_read_the_owner_store() -> None:
    bot = SchwabV2BotService(
        Settings(strategy_schwab_1m_v2_flip_owned_first_entry_enabled=False),
        session_factory=_factory(),
    )

    bot._configure_flip_entry_ownership_store({"FTFT": 123})

    assert bot.flip_entry_ownership_store is None
