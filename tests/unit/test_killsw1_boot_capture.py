"""Boot membership freezes owned identities, never a symbol-wide blacklist."""
import asyncio
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from project_mai_tai.db.models import (
    Base, BrokerAccount, BrokerOrder, OmsManagedPosition, Strategy, VirtualPosition,
)
from project_mai_tai.oms.bot_control_runtime import BotControlRuntime
from project_mai_tai.oms.bot_control import ControlConflict


@pytest.fixture
def inventory():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine, tables=[table for table in Base.metadata.sorted_tables
        if table.name not in {"market_trade_ticks", "market_quote_ticks"}])
    sessions = sessionmaker(engine, expire_on_commit=False)
    runtime = BotControlRuntime(sessions)
    with sessions.begin() as unit:
        strategies = [Strategy(code=code, name=code, execution_mode="live") for code in
                      ("schwab_1m_v2", "orb_schwab", "momentum_warn_live")]
        account = BrokerAccount(name="live:orb", provider="webull", environment="live")
        unit.add_all([*strategies, account])
        unit.flush()
        managed = OmsManagedPosition(strategy_code="schwab_1m_v2", broker_account_name=account.name,
            symbol="ATR", entry_price=Decimal(6), original_quantity=10, current_quantity=10, status="open")
        position = VirtualPosition(strategy_id=strategies[1].id, broker_account_id=account.id,
            symbol="ORB", quantity=Decimal(10), average_price=Decimal(6))
        entries = [BrokerOrder(strategy_id=strategies[1].id, broker_account_id=account.id,
            client_order_id=coid, broker_order_id=coid, symbol="ORB", side="buy", order_type="limit",
            time_in_force="day", quantity=Decimal(10), status="filled") for coid in ("older-entry", "latest-entry")]
        unit.add_all([managed, position, *entries])
        unit.flush()
        ids = dict(managed=managed.id, position=position.id, entries={str(row.id) for row in entries},
                   atr=strategies[0].id, orb=strategies[1].id, mom=strategies[2].id, account=account.id)
    for key in ("atr", "orb", "mom"):
        runtime.store.seed(ids[key])
    yield runtime, sessions, ids
    engine.dispose()


@pytest.mark.asyncio
async def test_first_capture_never_refreshes_old_ids_or_blocks_future_entry_identity(inventory):
    runtime, sessions, ids = inventory
    assert runtime.boot_managed_ids is None and runtime.boot_orb_entry_ids is None
    await runtime.refresh()
    assert runtime.boot_managed_ids == frozenset({str(ids["managed"])})
    assert runtime.boot_orb_entry_ids == frozenset(ids["entries"])
    with sessions.begin() as unit:
        unit.get(OmsManagedPosition, ids["managed"]).status = "closed"
        new_atr = OmsManagedPosition(strategy_code="schwab_1m_v2", broker_account_name="live:orb",
            symbol="AFTER", entry_price=Decimal(6), original_quantity=10, current_quantity=10, status="open")
        new_orb = BrokerOrder(strategy_id=ids["orb"], broker_account_id=ids["account"], symbol="ORB",
            client_order_id="postboot-entry", broker_order_id="postboot-root", side="buy", order_type="limit",
            time_in_force="day", quantity=Decimal(10), status="filled")
        unit.add_all([new_atr, new_orb])
        unit.flush()
        postboot_atr, postboot_orb = str(new_atr.id), str(new_orb.id)
    await runtime.refresh()
    assert runtime.boot_managed_ids == frozenset({str(ids["managed"])})
    assert runtime.boot_orb_entry_ids == frozenset(ids["entries"])
    assert postboot_atr not in runtime.boot_managed_ids
    assert postboot_orb not in runtime.boot_orb_entry_ids
    assert runtime.admission_open("momentum_warn_live")


@pytest.mark.asyncio
async def test_concurrent_refreshes_capture_both_sets_in_one_statement_once(inventory):
    runtime, sessions, ids = inventory
    captures = []
    def observe(connection, cursor, statement, parameters, context, executemany):
        if "UNION ALL" in statement:
            captures.append(statement)
    event.listen(sessions.kw["bind"], "before_cursor_execute", observe)
    try:
        await asyncio.gather(runtime.refresh(), runtime.refresh())
    finally:
        event.remove(sessions.kw["bind"], "before_cursor_execute", observe)
    assert len(captures) == 1
    assert "oms_managed_positions" in captures[0] and "virtual_positions" in captures[0]
    assert runtime.boot_managed_ids == frozenset({str(ids["managed"])})
    assert runtime.boot_orb_entry_ids == frozenset(ids["entries"])


@pytest.mark.asyncio
async def test_orphan_held_orb_keeps_both_sets_unknown_without_changing_momentum(inventory):
    runtime, sessions, ids = inventory
    with sessions.begin() as unit:
        for order in unit.scalars(select(BrokerOrder)).all():
            unit.delete(order)
    await runtime.refresh()
    assert runtime.boot_managed_ids is None and runtime.boot_orb_entry_ids is None
    assert runtime.admission_open("momentum_warn_live")
    for code in ("schwab_1m_v2", "orb_schwab"):
        assert not runtime.admission_open(code)
        with pytest.raises(ControlConflict, match="entry_scope_not_running"):
            runtime.stamp({}, code, datetime.now(UTC))
    with sessions.begin() as unit:
        unit.get(VirtualPosition, ids["position"]).quantity = Decimal(0)
    await runtime.refresh()
    assert runtime.boot_managed_ids == frozenset({str(ids["managed"])})
    assert runtime.boot_orb_entry_ids == frozenset()
    assert runtime.admission_open("schwab_1m_v2") and runtime.admission_open("orb_schwab")
    for code in ("schwab_1m_v2", "orb_schwab", "momentum_warn_live"):
        metadata = {}
        runtime.stamp(metadata, code, datetime.now(UTC))
        assert metadata["bot_control_generation"] == "0"


@pytest.mark.asyncio
async def test_unreadable_capture_never_publishes_partial_sets(inventory, monkeypatch):
    runtime, sessions, ids = inventory
    def fail():
        raise OSError("controlled unavailable database")
    monkeypatch.setattr(runtime, "sessions", fail)
    await runtime.refresh()
    assert runtime.boot_managed_ids is None and runtime.boot_orb_entry_ids is None
    monkeypatch.setattr(runtime, "sessions", sessions)
    await runtime.refresh()
    assert runtime.boot_managed_ids == frozenset({str(ids["managed"])})
    assert runtime.boot_orb_entry_ids == frozenset(ids["entries"])


@pytest.mark.asyncio
@pytest.mark.parametrize("environment", ["paper", "shadow"])
@pytest.mark.parametrize("quantity", [10, -1])
async def test_paper_managed_rows_neither_enter_nor_poison_live_boot_sets(inventory, environment, quantity):
    runtime, sessions, ids = inventory
    with sessions.begin() as unit:
        account = BrokerAccount(name=f"{environment}:atr", provider="schwab", environment=environment)
        unit.add(account)
        unit.flush()
        paper = OmsManagedPosition(strategy_code="schwab_1m_v2", broker_account_name=account.name,
            symbol="PAPER", entry_price=Decimal(6), original_quantity=10, current_quantity=quantity, status="open")
        unit.add(paper)
        unit.flush()
        paper_id = str(paper.id)
    await runtime.refresh()
    assert runtime.boot_managed_ids == frozenset({str(ids["managed"])})
    assert runtime.boot_orb_entry_ids == frozenset(ids["entries"])
    assert paper_id not in runtime.boot_managed_ids
    assert runtime.admission_open("momentum_warn_live")


@pytest.mark.asyncio
async def test_unknown_managed_account_is_not_silently_exempted_as_paper(inventory):
    runtime, sessions, ids = inventory
    with sessions.begin() as unit:
        unit.get(OmsManagedPosition, ids["managed"]).broker_account_name = "unbound:unknown"
    await runtime.refresh()
    assert runtime.boot_managed_ids is None and runtime.boot_orb_entry_ids is None
    assert runtime.admission_open("momentum_warn_live")
