"""Real C6 journal/census with controlled brokers, never a live order test."""

import asyncio
from datetime import UTC, datetime
from decimal import Decimal
import logging
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from project_mai_tai.broker_adapters.protocols import ExecutionReport
from project_mai_tai.db.models import (
    AccountPosition, Base, BotControlAttempt, BrokerAccount, BrokerOrder, Fill,
    OmsManagedPosition, Strategy, SystemIncident, TradeIntent, VirtualPosition,
)
from project_mai_tai.oms.bot_control import BotControlStore
from project_mai_tai.oms.bot_control_journal import CleanupJournal
from project_mai_tai.oms.bot_control_legacy import HELD_MESSAGE, LegacyStopDriver


@pytest.fixture(params=["schwab_1m_v2", "orb_schwab"])
def legacy(request):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine, tables=[table for table in Base.metadata.sorted_tables
        if table.name not in {"market_trade_ticks", "market_quote_ticks"}])
    sessions = sessionmaker(engine, expire_on_commit=False)
    code = request.param
    with sessions.begin() as unit:
        strategy = Strategy(code=code, name=code, execution_mode="live")
        other = Strategy(code="foreign", name="foreign", execution_mode="live")
        accounts = [BrokerAccount(name="live:schwab_1m_v2", provider="schwab", environment="production")]
        if code == "schwab_1m_v2":
            accounts.append(BrokerAccount(name="live:orb", provider="webull", environment="production"))
        unit.add_all([strategy, other, *accounts])
        unit.flush()
        identity, foreign_id = strategy.id, other.id
        account_ids = tuple(account.id for account in accounts)
        entries, protection = [], []
        for index, account in enumerate(accounts):
            intent = TradeIntent(strategy_id=identity, broker_account_id=account.id,
                symbol="ABC", side="buy", intent_type="open",
                quantity=Decimal(10), reason="legacy rest", status="submitted")
            unit.add(intent)
            unit.flush()
            entry = BrokerOrder(strategy_id=identity, broker_account_id=account.id,
                intent_id=intent.id, client_order_id=f"owned-{index}", broker_order_id=f"root-{index}",
                symbol="ABC", side="buy", order_type="limit", time_in_force="day",
                quantity=Decimal(10), status="working", payload={
                    "bracket": "true" if code == "orb_schwab" else "false",
                    "native_oco_bracket": "true" if code == "orb_schwab" else "false"})
            unit.add(entry)
            unit.flush()
            entries.append(entry.id)
            child = BrokerOrder(strategy_id=identity, broker_account_id=account.id,
                intent_id=intent.id, client_order_id=f"child-{index}", broker_order_id=f"protect-{index}",
                symbol="ABC", side="sell", order_type="stop", time_in_force="day",
                quantity=Decimal(2), status="working")
            unit.add(child)
            unit.flush()
            protection.append(child.id)
            unit.add(AccountPosition(broker_account_id=account.id, symbol="ABC",
                quantity=Decimal(1000), average_price=Decimal(6)))
        unit.add(BrokerOrder(strategy_id=foreign_id, broker_account_id=accounts[0].id,
            client_order_id="foreign-entry", broker_order_id="foreign-root", symbol="ABC", side="buy",
            order_type="limit", time_in_force="day", quantity=Decimal(1000), status="working"))
    store = BotControlStore(sessions)
    store.seed(identity)
    command = store.request(strategy_id=identity, command_id=uuid4(), request_id=uuid4(),
        expected_generation=0, action="STOP", account_ids=account_ids,
        actor_reference="operator", reason="controlled legacy STOP")
    lease = store.claim_stop(command.command_id, "legacy-test", now=datetime.now(UTC))

    class Broker:
        def __init__(self):
            self.sent, self.reads = [], []
            self.terminal, self.uncertain = set(), False
            self.bad_identity = False
            self.filled = Decimal(0)
            self.read_event = None

        async def fetch_order_update(self, query):
            self.reads.append(query)
            return ExecutionReport(client_order_id=query.client_order_id,
                broker_order_id="foreign-root" if self.bad_identity else query.metadata["broker_order_id"],
                symbol=query.symbol, side="buy", intent_type="open", quantity=query.quantity,
                filled_quantity=self.filled,
                event_type=self.read_event or ("cancelled" if query.client_order_id in self.terminal else "working"),
                origin="broker", reported_at=datetime.now(UTC))

        async def submit_order(self, query):
            assert query.intent_type == "cancel" and query.side == "buy"
            with sessions() as unit:
                target = unit.scalar(select(BrokerOrder).where(BrokerOrder.client_order_id == query.client_order_id))
                attempt = unit.scalar(select(BotControlAttempt).where(
                    BotControlAttempt.command_id == lease.command_id,
                    BotControlAttempt.target_order_id == target.id,
                    BotControlAttempt.operation == "CANCEL_ENTRY"))
                assert attempt is not None and attempt.status == "MAY_HAVE_SENT"
                assert query.metadata["broker_order_id"] == target.broker_order_id
            self.sent.append(query)
            if self.uncertain:
                raise TimeoutError("unknown broker cancel result")
            self.terminal.add(query.client_order_id)
            return []  # ACK is not terminal proof; driver must READ again.

        async def release_native_oco_for_close(self, *args, **kwargs):
            raise AssertionError("legacy standalone must not release held protection")

    async def run(work, *, commit=True):
        def invoke():
            with sessions() as unit:
                result = work(unit)
                if commit:
                    unit.commit()
                return result
        return await asyncio.to_thread(invoke)

    broker = Broker()
    service = SimpleNamespace(session_factory=sessions, _run_db=run, broker_adapter=broker,
                              logger=logging.getLogger("controlled-legacy"))
    yield SimpleNamespace(service=service, sessions=sessions, broker=broker,
        code=code, store=store, lease=lease, journal=CleanupJournal(store),
        accounts=account_ids, entries=entries, protection=protection, strategy=identity)
    engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("ledger", ["virtual", "managed"])
async def test_cancels_all_own_entry_legs_but_never_sells_or_releases_any_held_position(legacy, ledger):
    if ledger == "managed" and legacy.code == "orb_schwab":
        # ORB's owned ledger is its VP, not an ATR managed row.
        ledger = "virtual"
    with legacy.sessions.begin() as unit:
        for account in legacy.accounts:
            if ledger == "virtual":
                unit.add(VirtualPosition(strategy_id=legacy.strategy, broker_account_id=account,
                    symbol="ABC", quantity=Decimal(2), average_price=Decimal(6)))
            else:
                name = unit.get(BrokerAccount, account).name
                unit.add(OmsManagedPosition(strategy_code=legacy.code, broker_account_name=name,
                    symbol="ABC", entry_price=Decimal(6), original_quantity=2, current_quantity=2, status="open"))
    driver = LegacyStopDriver(legacy.service, legacy.code)
    assert (await driver(legacy.lease, legacy.journal)).phase == "BLOCKED_UNKNOWN"
    assert {query.client_order_id for query in legacy.broker.sent} == {
        f"owned-{index}" for index in range(len(legacy.accounts))}
    assert all(query.side == "buy" and query.intent_type == "cancel" for query in legacy.broker.sent)
    count = len(legacy.broker.sent)
    assert (await LegacyStopDriver(legacy.service, legacy.code)(legacy.lease, legacy.journal)).phase == "BLOCKED_UNKNOWN"
    assert len(legacy.broker.sent) == count
    with legacy.sessions() as unit:
        incidents = unit.scalars(select(SystemIncident)).all()
        assert len(incidents) == len(legacy.accounts)
        assert all(row.severity == "critical" and row.title == HELD_MESSAGE for row in incidents)
        assert all(unit.get(BrokerOrder, identity).status == "working" for identity in legacy.protection)
        assert unit.scalar(select(BrokerOrder).where(BrokerOrder.client_order_id == "foreign-entry")).status == "working"
        assert all(row.quantity == 1000 for row in unit.scalars(select(AccountPosition)))
    assert legacy.store.hydrate(legacy.strategy).desired_state == "STOP"


@pytest.mark.asyncio
async def test_flat_census_requires_terminal_protection_and_actual_fill_conservation(legacy):
    driver = LegacyStopDriver(legacy.service, legacy.code)
    assert (await driver(legacy.lease, legacy.journal)).phase == "CANCELLING_ENTRIES"
    assert (await driver(legacy.lease, legacy.journal)).phase == "BLOCKED_UNKNOWN"
    with legacy.sessions.begin() as unit:
        for identity in legacy.protection:
            unit.get(BrokerOrder, identity).status = "cancelled"
    progress = await driver(legacy.lease, legacy.journal)
    assert progress.phase == "SETTLED"
    assert set(progress.census) == {str(value) for value in legacy.accounts}
    with legacy.sessions.begin() as unit:
        entry = unit.get(BrokerOrder, legacy.entries[0])
        unit.add(Fill(order_id=entry.id, strategy_id=entry.strategy_id,
            broker_account_id=entry.broker_account_id, broker_fill_id="unattributed-held-fill",
            symbol=entry.symbol, side="buy", quantity=Decimal(1), price=Decimal(6), filled_at=datetime.now(UTC)))
    assert (await driver(legacy.lease, legacy.journal)).phase == "BLOCKED_UNKNOWN"


@pytest.mark.asyncio
async def test_uncertain_entry_cancel_reads_original_once_without_resending(legacy):
    legacy.broker.uncertain = True
    driver = LegacyStopDriver(legacy.service, legacy.code)
    assert (await driver(legacy.lease, legacy.journal)).phase == "BLOCKED_UNKNOWN"
    sent = len(legacy.broker.sent)
    assert sent == len(legacy.accounts)
    assert (await LegacyStopDriver(legacy.service, legacy.code)(legacy.lease, legacy.journal)).phase == "BLOCKED_UNKNOWN"
    assert len(legacy.broker.sent) == sent


@pytest.mark.asyncio
async def test_wrong_broker_entry_identity_never_cancels(legacy):
    legacy.broker.bad_identity = True
    assert (await LegacyStopDriver(legacy.service, legacy.code)(legacy.lease, legacy.journal)).phase == "BLOCKED_UNKNOWN"
    assert not legacy.broker.sent


@pytest.mark.asyncio
async def test_unrecorded_buy_fill_cannot_be_declared_flat(legacy):
    legacy.broker.filled = Decimal(2)
    result = await LegacyStopDriver(legacy.service, legacy.code)(legacy.lease, legacy.journal)
    assert result.phase == "BLOCKED_UNKNOWN"
    with legacy.sessions() as unit:
        assert all(row.status != "TERMINAL" for row in unit.scalars(select(BotControlAttempt)))


@pytest.mark.asyncio
@pytest.mark.parametrize("quantity", [Decimal(0), Decimal(3)])
async def test_contradictory_filled_read_does_not_persist_false_terminal_or_strand_recovery(legacy, quantity):
    legacy.broker.read_event, legacy.broker.filled = "filled", quantity
    if quantity:
        with legacy.sessions.begin() as unit:
            for identity in legacy.entries:
                entry = unit.get(BrokerOrder, identity)
                unit.add(Fill(order_id=entry.id, strategy_id=entry.strategy_id,
                    broker_account_id=entry.broker_account_id, broker_fill_id=f"partial-{entry.id}",
                    symbol=entry.symbol, side="buy", quantity=quantity, price=Decimal(6), filled_at=datetime.now(UTC)))
    driver = LegacyStopDriver(legacy.service, legacy.code)
    assert (await driver(legacy.lease, legacy.journal)).phase == "BLOCKED_UNKNOWN"
    assert not legacy.broker.sent
    with legacy.sessions() as unit:
        assert all(unit.get(BrokerOrder, identity).status == "working" for identity in legacy.entries)
        assert all(row.status == "PREPARED" for row in unit.scalars(select(BotControlAttempt)))
    legacy.broker.read_event = None
    reads = len(legacy.broker.reads)
    await driver(legacy.lease, legacy.journal)
    assert len(legacy.broker.reads) > reads
    if legacy.code == "orb_schwab" and quantity:
        assert not legacy.broker.sent  # Approved partial native-parent block, not a lost recovery row.
        with legacy.sessions.begin() as unit:
            for identity in legacy.entries:
                entry = unit.get(BrokerOrder, identity)
                assert entry.status == "working"
                unit.add(Fill(order_id=entry.id, strategy_id=entry.strategy_id,
                    broker_account_id=entry.broker_account_id, broker_fill_id=f"remainder-{entry.id}",
                    symbol=entry.symbol, side="buy", quantity=Decimal(10) - quantity,
                    price=Decimal(6), filled_at=datetime.now(UTC)))
        legacy.broker.read_event, legacy.broker.filled = "filled", Decimal(10)
        await driver(legacy.lease, legacy.journal)
        with legacy.sessions() as unit:
            assert all(unit.get(BrokerOrder, identity).status == "filled" for identity in legacy.entries)
            assert all(row.status == "TERMINAL" for row in unit.scalars(select(BotControlAttempt)))
        assert not legacy.broker.sent
        return
    assert len(legacy.broker.sent) == len(legacy.accounts)
    with legacy.sessions() as unit:
        assert all(unit.get(BrokerOrder, identity).status == "cancelled" for identity in legacy.entries)


@pytest.mark.asyncio
async def test_partial_native_schwab_parent_is_blocked_while_webull_bare_remainder_cancels(legacy):
    with legacy.sessions.begin() as unit:
        for identity in legacy.entries:
            entry = unit.get(BrokerOrder, identity)
            account = unit.get(BrokerAccount, entry.broker_account_id)
            entry.payload = {"bracket": "true", "native_oco_bracket": "true"} if account.provider == "schwab" else {
                "bracket": "false", "native_oco_bracket": "false"}
            unit.add(Fill(order_id=entry.id, strategy_id=entry.strategy_id,
                broker_account_id=entry.broker_account_id, broker_fill_id=f"native-partial-{identity}",
                symbol=entry.symbol, side="buy", quantity=Decimal(2), price=Decimal(6), filled_at=datetime.now(UTC)))
            unit.add(VirtualPosition(strategy_id=legacy.strategy, broker_account_id=entry.broker_account_id,
                symbol=entry.symbol, quantity=Decimal(2), average_price=Decimal(6)))
    legacy.broker.filled = Decimal(2)
    assert (await LegacyStopDriver(legacy.service, legacy.code)(legacy.lease, legacy.journal)).phase == "BLOCKED_UNKNOWN"
    assert all(query.broker_account_name == "live:orb" for query in legacy.broker.sent)
    assert len(legacy.broker.sent) == (1 if legacy.code == "schwab_1m_v2" else 0)
    with legacy.sessions() as unit:
        assert all(unit.get(BrokerOrder, identity).status == "working" for identity in legacy.protection)
        assert all(row.severity == "critical" for row in unit.scalars(select(SystemIncident)))
