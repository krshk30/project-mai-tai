from __future__ import annotations

import asyncio
import ast
import inspect
import textwrap
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from project_mai_tai.db.models import (
    Base,
    BrokerAccount,
    BrokerOrder,
    DashboardSnapshot,
    Fill,
    OmsManagedPosition,
    Strategy,
    TradeIntent,
)
from project_mai_tai.fanout_identity import fanout_slot_id
from project_mai_tai.services.schwab_1m_v2_bot import SchwabV2BotService
from project_mai_tai.settings import Settings
from project_mai_tai.strategy_core.schwab_1m_v2 import SchwabV2Strategy
from project_mai_tai.v2_flip_entry_ownership import (
    FlipEntryOwnershipRecord,
    FlipEntryOwnershipStore,
    FlipPositionBook,
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
        tables=[
            DashboardSnapshot.__table__,
            OmsManagedPosition.__table__,
            Strategy.__table__,
            BrokerAccount.__table__,
            TradeIntent.__table__,
            BrokerOrder.__table__,
            Fill.__table__,
        ],
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


def test_consumed_owner_round_trips_until_a_real_sell_flip_retires_it() -> None:
    store = FlipEntryOwnershipStore(_factory())
    consumed = replace(_record(), phase="consumed", flip_bar_ts=0)

    store.record(consumed, active=True, reason="stop_close_consumed", now=NOW)

    assert store.restore_active(now=NOW + timedelta(minutes=1)) == {"FTFT": consumed}


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


def _seed_first_rest_open(
    session,
    *,
    strategy: Strategy,
    account: BrokerAccount,
    symbol: str,
    opportunity_id: int,
    status: str,
    order_status: str | None,
    with_fill: bool = False,
    terminal_age_seconds: int = 30,
) -> None:
    terminal_at = datetime.now(UTC) - timedelta(seconds=terminal_age_seconds)
    slot_id = fanout_slot_id(
        strategy_code="schwab_1m_v2",
        symbol=symbol,
        segment_id=opportunity_id,
        slot="resting",
    )
    intent = TradeIntent(
        strategy_id=strategy.id,
        broker_account_id=account.id,
        symbol=symbol,
        side="buy",
        intent_type="open",
        quantity=Decimal("1"),
        reason="schwab_1m_v2 ATR Flip CW-v2-resting",
        status=status,
        payload={
            "metadata": {
                "fanout_leg": "webull" if account.provider == "webull" else "primary",
                "fanout_segment_id": str(opportunity_id),
                "fanout_slot": "resting",
                "fanout_slot_id": slot_id,
            }
        },
        created_at=datetime.now(UTC),
        updated_at=terminal_at,
    )
    session.add(intent)
    session.flush()
    if order_status is None:
        return
    order = BrokerOrder(
        intent_id=intent.id,
        strategy_id=strategy.id,
        broker_account_id=account.id,
        client_order_id=f"{symbol}-{account.provider}-{opportunity_id}",
        broker_order_id=f"broker-{symbol}-{account.provider}-{opportunity_id}",
        symbol=symbol,
        side="buy",
        order_type="stop_limit",
        time_in_force="day",
        quantity=Decimal("1"),
        status=order_status,
        payload={},
        updated_at=terminal_at,
    )
    session.add(order)
    session.flush()
    if with_fill:
        session.add(
            Fill(
                order_id=order.id,
                strategy_id=strategy.id,
                broker_account_id=account.id,
                broker_fill_id=f"fill-{symbol}-{account.provider}-{opportunity_id}",
                symbol=symbol,
                side="buy",
                quantity=Decimal("1"),
                price=Decimal("2.78"),
                filled_at=datetime.now(UTC),
                payload={},
            )
        )


def _terminal_evidence_bot(factory) -> SchwabV2BotService:
    return SchwabV2BotService(
        Settings(
            strategy_schwab_1m_v2_flip_owned_first_entry_enabled=True,
            strategy_schwab_1m_v2_dual_broker_fanout_enabled=True,
            strategy_schwab_1m_v2_webull_resting_mirror_enabled=True,
            strategy_schwab_1m_v2_account_name="live:schwab_1m_v2",
            strategy_schwab_1m_v2_webull_account_name="live:orb",
        ),
        session_factory=factory,
    )


def test_position_book_proves_both_first_rest_legs_terminal_without_a_fill(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level("INFO")
    factory = _factory()
    opportunity_id = int(datetime.now(UTC).timestamp() * 1000)
    with factory() as session:
        strategy = Strategy(code="schwab_1m_v2", name="v2", execution_mode="live")
        primary = BrokerAccount(
            name="live:schwab_1m_v2", provider="schwab", environment="production"
        )
        webull = BrokerAccount(
            name="live:orb", provider="webull", environment="production"
        )
        session.add_all([strategy, primary, webull])
        session.flush()
        for account in (primary, webull):
            _seed_first_rest_open(
                session,
                strategy=strategy,
                account=account,
                symbol="FTFT",
                opportunity_id=opportunity_id,
                status="cancelled",
                order_status="cancelled",
            )
        session.commit()

    book = _terminal_evidence_bot(factory)._fetch_flip_position_book(
        {"FTFT": opportunity_id}
    )

    assert book.terminal_unfilled_opportunities_by_symbol == {
        "FTFT": frozenset({opportunity_id})
    }
    assert "unfilled_opportunities_evaluated=1 terminal_unfilled=1" in caplog.text


def test_position_poll_queries_terminal_orders_only_for_exact_unknown_owners(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bot = _terminal_evidence_bot(None)
    state = bot.strategy.watchlist_state("FTFT")
    state.flip_owner_phase = "unknown"
    state.flip_owner_opportunity_id = 12345
    captured: list[dict[str, int]] = []
    monkeypatch.setattr(bot, "_fetch_position_maps", lambda: ({}, {}))
    monkeypatch.setattr(bot, "_fetch_managed_symbols", lambda: set())
    monkeypatch.setattr(bot, "_roll_stale_session_state", lambda *_args: None)
    monkeypatch.setattr(
        bot,
        "_fetch_flip_position_book",
        lambda candidates: captured.append(dict(candidates))
        or FlipPositionBook(
            observed_at_ms=int(datetime.now(UTC).timestamp() * 1000),
            readable=True,
            legs_by_symbol={},
        ),
    )

    asyncio.run(bot._position_poll_pass())

    assert captured == [{"FTFT": 12345}]


def test_position_book_skips_entry_history_when_no_owner_is_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bot = _terminal_evidence_bot(_factory())
    monkeypatch.setattr(
        bot,
        "_terminal_unfilled_first_rest_opportunities",
        lambda *_args, **_kwargs: pytest.fail("entry history was scanned without an unknown owner"),
    )

    book = bot._fetch_flip_position_book()

    assert book.readable is True
    assert book.terminal_unfilled_opportunities_by_symbol == {}


@pytest.mark.parametrize(
    "incomplete", ["missing_sibling", "working", "filled", "still_settling"]
)
def test_position_book_refuses_incomplete_or_filled_terminal_evidence(
    incomplete: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level("INFO")
    factory = _factory()
    opportunity_id = int(datetime.now(UTC).timestamp() * 1000)
    with factory() as session:
        strategy = Strategy(code="schwab_1m_v2", name="v2", execution_mode="live")
        primary = BrokerAccount(
            name="live:schwab_1m_v2", provider="schwab", environment="production"
        )
        webull = BrokerAccount(
            name="live:orb", provider="webull", environment="production"
        )
        session.add_all([strategy, primary, webull])
        session.flush()
        _seed_first_rest_open(
            session,
            strategy=strategy,
            account=primary,
            symbol="FTFT",
            opportunity_id=opportunity_id,
            status="cancelled",
            order_status="cancelled",
            with_fill=incomplete == "filled",
            terminal_age_seconds=0 if incomplete == "still_settling" else 30,
        )
        if incomplete != "missing_sibling":
            _seed_first_rest_open(
                session,
                strategy=strategy,
                account=webull,
                symbol="FTFT",
                opportunity_id=opportunity_id,
                status="submitted" if incomplete == "working" else "cancelled",
                order_status="working" if incomplete == "working" else "cancelled",
                terminal_age_seconds=0 if incomplete == "still_settling" else 30,
            )
        session.commit()

    book = _terminal_evidence_bot(factory)._fetch_flip_position_book(
        {"FTFT": opportunity_id}
    )

    assert book.terminal_unfilled_opportunities_by_symbol == {}
    assert "unfilled_opportunities_evaluated=1 terminal_unfilled=0" in caplog.text


def test_malformed_open_managed_row_still_fails_the_position_book_closed() -> None:
    factory = _factory()
    with factory() as session:
        session.add(
            OmsManagedPosition(
                strategy_code="schwab_1m_v2",
                broker_account_name="live:schwab_1m_v2",
                symbol="BROKEN",
                entry_price=Decimal("2.31"),
                original_quantity=2,
                current_quantity=0,
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

    assert book.readable is False
    assert book.legs_by_symbol == {}


@pytest.mark.parametrize(
    ("slot_id", "managed_row_id", "expected_counts", "expected_reason"),
    [
        ("", "legacy-managed-row", "skipped_unbound=1 malformed=0", "missing_fanout_slot_id"),
        ("legacy-slot", "", "skipped_unbound=0 malformed=1", "malformed_confirmation_close"),
    ],
)
def test_unbound_or_malformed_confirmation_close_cannot_poison_unrelated_entries(
    caplog,
    slot_id: str,
    managed_row_id: str,
    expected_counts: str,
    expected_reason: str,
) -> None:
    factory = _factory()
    now = datetime.now(UTC)
    with factory() as session:
        strategy = Strategy(code="schwab_1m_v2", name="v2", execution_mode="live")
        account = BrokerAccount(
            name="live:schwab_1m_v2", provider="schwab", environment="production"
        )
        session.add_all([strategy, account])
        session.flush()
        intent = TradeIntent(
            strategy_id=strategy.id,
            broker_account_id=account.id,
            symbol="LEGACY",
            side="sell",
            intent_type="close",
            quantity=Decimal("2"),
            reason="oms_v2_managed_exit:CONFIRMATION_EXIT",
            status="filled",
            payload={
                "metadata": {
                    "flip_owner_confirmation_exit": "true",
                    "confirmation_fanout_slot_id": slot_id,
                    "confirmation_managed_row_id": managed_row_id,
                }
            },
        )
        session.add(intent)
        session.flush()
        order = BrokerOrder(
            intent_id=intent.id,
            strategy_id=strategy.id,
            broker_account_id=account.id,
            client_order_id="legacy-dark-confirmation-close",
            broker_order_id="legacy-dark-confirmation-order",
            symbol="LEGACY",
            side="sell",
            order_type="market",
            time_in_force="day",
            quantity=Decimal("2"),
            status="filled",
            payload={},
        )
        session.add(order)
        session.flush()
        session.add(
            Fill(
                order_id=order.id,
                strategy_id=strategy.id,
                broker_account_id=account.id,
                broker_fill_id="legacy-dark-confirmation-fill",
                symbol="LEGACY",
                side="sell",
                quantity=Decimal("2"),
                price=Decimal("5.40"),
                filled_at=now,
                payload={},
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
    assert book.confirmation_closes_by_symbol == {}
    assert f"evaluated=1 known=0 {expected_counts}" in caplog.text
    assert f"reason={expected_reason}" in caplog.text

    owner = SchwabV2Strategy(
        Settings(
            strategy_schwab_1m_v2_flip_owned_first_entry_enabled=True,
            strategy_schwab_1m_v2_account_name="live:schwab_1m_v2",
            strategy_schwab_1m_v2_webull_account_name="live:orb",
        )
    )
    owner._now_ms = lambda: book.observed_at_ms
    owner.configure_flip_entry_ownership(lambda *_args: None, restore_readable=True)
    unrelated = [owner.watchlist_state(symbol) for symbol in ("AAA", "BBB")]
    owner.apply_flip_position_book(book)

    assert all(owner._strict_first_rest_admitted(state, slot="first") for state in unrelated)


def test_position_book_reads_only_fully_filled_bound_confirmation_closes() -> None:
    factory = _factory()
    now = datetime.now(UTC)
    with factory() as session:
        strategy = Strategy(code="schwab_1m_v2", name="v2", execution_mode="live")
        account = BrokerAccount(
            name="live:orb", provider="webull", environment="production"
        )
        session.add_all([strategy, account])
        session.flush()
        intent = TradeIntent(
            strategy_id=strategy.id,
            broker_account_id=account.id,
            symbol="DBGI",
            side="sell",
            intent_type="close",
            quantity=Decimal("2"),
            reason="oms_v2_managed_exit:CONFIRMATION_EXIT",
            status="filled",
            payload={},
        )
        session.add(intent)
        session.flush()
        order = BrokerOrder(
            intent_id=intent.id,
            strategy_id=strategy.id,
            broker_account_id=account.id,
            client_order_id="dbgi-confirmation-close",
            broker_order_id="dbgi-confirmation-order",
            symbol="DBGI",
            side="sell",
            order_type="market",
            time_in_force="day",
            quantity=Decimal("2"),
            status="filled",
            payload={
                "flip_owner_confirmation_exit": "true",
                "confirmation_fanout_slot_id": "dbgi-first-slot",
                "confirmation_managed_row_id": "dbgi-managed-row",
            },
        )
        session.add(order)
        session.flush()
        session.add(
            Fill(
                order_id=order.id,
                strategy_id=strategy.id,
                broker_account_id=account.id,
                broker_fill_id="dbgi-confirmation-fill",
                symbol="DBGI",
                side="sell",
                quantity=Decimal("1"),
                price=Decimal("5.40"),
                filled_at=now,
                payload={},
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

    partial_book = bot._fetch_flip_position_book()

    assert "DBGI" not in partial_book.confirmation_closes_by_symbol

    with factory() as session:
        strategy = session.scalar(select(Strategy).where(Strategy.code == "schwab_1m_v2"))
        account = session.scalar(
            select(BrokerAccount).where(BrokerAccount.name == "live:orb")
        )
        order = session.scalar(
            select(BrokerOrder).where(
                BrokerOrder.client_order_id == "dbgi-confirmation-close"
            )
        )
        assert strategy is not None and account is not None and order is not None
        session.add(
            Fill(
                order_id=order.id,
                strategy_id=strategy.id,
                broker_account_id=account.id,
                broker_fill_id="dbgi-confirmation-fill-2",
                symbol="DBGI",
                side="sell",
                quantity=Decimal("1"),
                price=Decimal("5.40"),
                filled_at=now,
                payload={},
            )
        )
        session.commit()

    book = bot._fetch_flip_position_book()

    assert book.readable is True
    assert book.confirmation_closes_by_symbol["DBGI"][0].managed_row_id == (
        "dbgi-managed-row"
    )
    assert book.confirmation_closes_by_symbol["DBGI"][0].fanout_slot_id == (
        "dbgi-first-slot"
    )


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
