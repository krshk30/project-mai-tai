"""Controlled STOP-only broker responses; no network or claimed live executions."""
import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from project_mai_tai.broker_adapters.protocols import ExecutionReport, ExitPairReleaseResult
from project_mai_tai.db.models import (
    Base, AccountPosition, BotControlAttempt, BotControlCommand, BrokerAccount, BrokerOrder, Fill,
    OmsManagedPosition, Strategy, SystemIncident, TradeIntent, VirtualPosition,
)
from project_mai_tai.oms import bot_control as control
from project_mai_tai.oms import bot_control_atr as atr
from project_mai_tai.oms import bot_control_executor as executor_module
from project_mai_tai.oms import bot_control_legacy as legacy_module
from project_mai_tai.oms import service as service_module
from project_mai_tai.oms.bot_control import ControlConflict
from project_mai_tai.oms.bot_control_journal import CleanupJournal
from project_mai_tai.oms.service import OmsRiskService, oco_exit_client_order_id
from project_mai_tai.oms.service import _ManagedSellEvents
from project_mai_tai.positions_read_receipt import record_receipts
from project_mai_tai.settings import Settings


class Redis:
    async def xadd(self, *args, **kwargs):
        return b"1-1"


def test_prepared_normal_sell_with_no_wire_timestamp_fences_stop_accounting(book):
    state = book
    with state.sessions.begin() as unit:
        intent = TradeIntent(strategy_id=state.strategy, broker_account_id=state.account,
            symbol="ABC", side="sell", intent_type="close", quantity=Decimal(10),
            reason="controlled-normal-pending", status="pending")
        unit.add(intent)
        unit.flush()
        unit.add(BrokerOrder(intent_id=intent.id, strategy_id=state.strategy,
            broker_account_id=state.account, client_order_id="normal-pending-no-wire",
            symbol="ABC", side="sell", quantity=Decimal(10), order_type="market",
            time_in_force="day", status="pending", submitted_at=None,
            payload={"oms_managed_row_id": str(state.row), "managed_sell_prepared_at": state.clock.now(UTC).isoformat()}))
    with state.sessions() as unit:
        with pytest.raises(ControlConflict, match="atr_stop_existing_exit_working_UNKNOWN"):
            atr._managed(state.service, unit, state.lease, state.row)
    assert not state.submits and state.release_count == 0


@pytest.fixture(params=["schwab", "webull"])
def book(request, monkeypatch):
    class Clock(datetime):
        value = "2026-10-15T14:00:00+00:00"

        @classmethod
        def now(cls, tz=None):
            current = cls.fromisoformat(cls.value)
            return current.astimezone(tz) if tz else current.replace(tzinfo=None)

    hours = getattr(request.node, "callspec", SimpleNamespace(params={})).params.get("hours", 14)
    Clock.value = f"2026-10-15T{hours:02}:00:00+00:00"
    monkeypatch.setattr(atr, "datetime", Clock)
    monkeypatch.setattr(control, "datetime", Clock)
    monkeypatch.setattr(executor_module, "datetime", Clock)
    monkeypatch.setattr(legacy_module, "datetime", Clock)
    monkeypatch.setattr(service_module, "utcnow", lambda: Clock.now(UTC))
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine, tables=[table for table in Base.metadata.sorted_tables
        if table.name not in {"market_trade_ticks", "market_quote_ticks"}])
    sessions = sessionmaker(engine, expire_on_commit=False)
    state = SimpleNamespace(provider=request.param, clock=Clock, sessions=sessions,
                            submits=[], cancels=[], release_count=0, reject=False,
                            release_outcome="released", receipt_age=0, buy_status="working",
                            soft_status="filled", child_reports={})

    class Broker:
        async def submit_order(self, order):
            with sessions() as unit:
                attempt = unit.scalar(select(BotControlAttempt).where(
                    BotControlAttempt.operation == ("CANCEL_ENTRY" if order.intent_type == "cancel" else "SUBMIT_EXIT")))
                assert attempt.status == "MAY_HAVE_SENT" and attempt.dispatch_intent_at is not None
            if order.intent_type == "cancel":
                state.cancels.append(order)
                state.buy_status = "cancelled"
                return []  # Acknowledgment alone is deliberately not evidence.
            state.submits.append(order)
            return [ExecutionReport(event_type="rejected" if state.reject else state.soft_status, origin="broker",
                client_order_id=order.client_order_id, broker_order_id="own-soft-exit",
                broker_fill_id=None if state.reject else "own-soft-exit:10", symbol="ABC", side="sell",
                intent_type="close", quantity=order.quantity,
                filled_quantity=order.quantity if state.soft_status == "filled" and not state.reject else Decimal(0),
                fill_price=None if state.reject else Decimal("10"), reported_at=Clock.now(UTC))]

        async def fetch_order_update(self, order):
            if order.metadata["broker_order_id"] == "own-soft-exit":
                return ExecutionReport(event_type=state.soft_status, origin="broker",
                    client_order_id=order.client_order_id, broker_order_id="own-soft-exit",
                    broker_fill_id="own-soft-exit:10", symbol="ABC", side="sell", intent_type="close",
                    quantity=order.quantity, filled_quantity=order.quantity if state.soft_status == "filled" else Decimal(0),
                    fill_price=Decimal(10), reported_at=Clock.now(UTC))
            if order.side == "sell":
                if order.metadata["broker_order_id"] in state.child_reports:
                    return state.child_reports[order.metadata["broker_order_id"]](order)
                return ExecutionReport(event_type="cancelled" if state.release_count else "working", origin="broker",
                    client_order_id=order.client_order_id, broker_order_id=order.metadata["broker_order_id"],
                    symbol="ABC", side="sell", intent_type="close", quantity=order.quantity,
                    filled_quantity=Decimal(0), reported_at=Clock.now(UTC))
            return ExecutionReport(event_type=state.buy_status, origin="broker",
                client_order_id=order.client_order_id, broker_order_id=order.metadata["broker_order_id"],
                symbol="ABC", side="buy", intent_type="open", quantity=order.quantity,
                filled_quantity=Decimal(0), reported_at=Clock.now(UTC))

    state.broker = Broker()
    service = OmsRiskService(Settings(bot_control_enabled=True,
        strategy_schwab_1m_v2_country_route_enabled=True, oms_v2_exit_management_enabled=True,
        oms_v2_exit_close_on_fill_enabled=True), redis_client=Redis(), session_factory=sessions,
        broker_adapter=state.broker)
    state.service = service
    with sessions.begin() as unit:
        strategy = Strategy(code=atr.STRATEGY, name="ATR", execution_mode="live")
        account = BrokerAccount(name="live:atr-controlled", provider=state.provider, environment="live")
        unit.add_all([strategy, account])
        unit.flush()
        intent = TradeIntent(strategy_id=strategy.id, broker_account_id=account.id, symbol="ABC",
            side="buy", intent_type="open", quantity=Decimal(10), reason="controlled-entry", status="filled")
        unit.add(intent)
        unit.flush()
        entry = BrokerOrder(intent_id=intent.id, strategy_id=strategy.id, broker_account_id=account.id,
            client_order_id="own-parent", broker_order_id="own-parent-root", symbol="ABC", side="buy",
            order_type="limit", time_in_force="day", quantity=Decimal(10), status="filled",
            submitted_at=Clock.now(UTC) - timedelta(minutes=1))
        unit.add(entry)
        unit.flush()
        row = OmsManagedPosition(strategy_code=atr.STRATEGY, broker_account_name=account.name,
            symbol="ABC", entry_order_id=entry.id, entry_client_order_id=entry.client_order_id,
            entry_price=Decimal(10), original_quantity=10, current_quantity=10, status="open")
        unit.add_all([row, Fill(order_id=entry.id, strategy_id=strategy.id, broker_account_id=account.id,
            broker_fill_id="own-parent-root:1", symbol="ABC", side="buy", quantity=Decimal(10), price=Decimal(10)),
            VirtualPosition(strategy_id=strategy.id, broker_account_id=account.id,
                symbol="ABC", quantity=Decimal(10), average_price=Decimal(10)),
            AccountPosition(broker_account_id=account.id, symbol="ABC",
                quantity=Decimal(10), average_price=Decimal(10))])
        unit.flush()
        state.strategy, state.account, state.entry, state.row = strategy.id, account.id, entry.id, row.id
        state.account_name = account.name
    runtime = service._bot_control_runtime
    runtime.boot_managed_ids = frozenset()
    runtime.store.seed(state.strategy)
    receipt = runtime.store.request(strategy_id=state.strategy, command_id=uuid4(), request_id=uuid4(),
        expected_generation=0, action="STOP", account_ids=(state.account,), actor_reference="controlled",
        reason="STOP")
    with sessions.begin() as unit:
        # Model defaults use the real wall clock; this controlled command was
        # accepted on the same frozen clock as its lease and broker observations.
        unit.get(BotControlCommand, receipt.command_id).accepted_at = Clock.now(UTC)
    state.lease = runtime.store.claim_stop(receipt.command_id, "controlled-executor", now=Clock.now(UTC))
    state.journal = CleanupJournal(runtime.store)
    service._latest_quotes_by_symbol["ABC"] = {"bid": 10.05, "ask": 10.06, "received_at": Clock.now(UTC)}

    async def release(*args, **kwargs):
        state.release_count += 1
        with sessions() as unit:
            attempt = unit.scalar(select(BotControlAttempt).where(
                BotControlAttempt.binding_kind == "managed_native_tree"))
            assert attempt.status == "MAY_HAVE_SENT" and attempt.dispatch_intent_at is not None
        # A later positions wire read, not a fresh label on an old cached snapshot.
        terminal = Clock.now(UTC)
        Clock.value = (terminal + timedelta(milliseconds=1)).isoformat()
        with sessions.begin() as unit:
            record_receipts(unit, [(state.account_name,
                Clock.now(UTC) - timedelta(seconds=state.receipt_age), 1)], recorded_at=Clock.now(UTC))
        return state.release_outcome

    async def release_pair(**kwargs):
        return ExitPairReleaseResult(await release())

    monkeypatch.setattr(service, "_release_native_oco_for_cw_flip", release)
    monkeypatch.setattr(service, "_release_exit_reservation_before_close", release_pair)
    # Record_native_outcome is observed after the reader returns; advance the next
    # positions-read receipt independently before the subsequent driver pass.
    state.release = release
    state.driver = atr.AtrStopDriver(service)
    yield state
    engine.dispose()


async def drive(book):
    return await book.driver(book.lease, book.journal)


def fresh_receipt(book):
    book.clock.value = (book.clock.now(UTC) + timedelta(milliseconds=1)).isoformat()
    with book.sessions.begin() as unit:
        record_receipts(unit, [(book.account_name, book.clock.now(UTC), 1)], recorded_at=book.clock.now(UTC))


@pytest.mark.asyncio
async def test_terminal_release_requires_later_wire_read_then_exact_existing_sender(book):
    assert (await drive(book)).phase == "BLOCKED_UNKNOWN"  # No post-terminal receipt yet.
    assert book.release_count == 1 and book.submits == []
    fresh_receipt(book)
    assert (await drive(book)).phase == "EXIT_WORKING"
    assert len(book.submits) == 1 and book.submits[0].quantity == Decimal(10)
    assert book.submits[0].order_type == "market"
    assert book.release_count == 1
    with book.sessions() as unit:
        assert unit.get(OmsManagedPosition, book.row).status == "closed"
        assert unit.scalar(select(VirtualPosition.quantity)) == 0
        assert unit.scalar(select(BotControlAttempt.status).where(
            BotControlAttempt.operation == "SUBMIT_EXIT")) == "TERMINAL"
    assert (await drive(book)).phase == "SETTLED"
    assert len(book.submits) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("hours", [12, 14, 22])
async def test_real_bound_working_native_children_reach_release_not_software_block(book, hours):
    with book.sessions.begin() as unit:
        entry = unit.get(BrokerOrder, book.entry)
        for child_id in ("own-target", "own-stop"):
            unit.add(BrokerOrder(strategy_id=book.strategy, broker_account_id=book.account,
                intent_id=entry.intent_id, client_order_id=oco_exit_client_order_id(entry.client_order_id, child_id),
                broker_order_id=child_id, symbol="ABC", side="sell", order_type="oco_exit",
                time_in_force="day", quantity=Decimal(10), status="working", submitted_at=book.clock.now(UTC)))
    assert (await drive(book)).phase == "BLOCKED_UNKNOWN"  # Wait for the later positions read.
    assert book.release_count == 1 and not book.submits
    with book.sessions() as unit:
        children = unit.scalars(select(BrokerOrder).where(BrokerOrder.order_type == "oco_exit")).all()
        assert len(children) == 2 and all(child.status == "cancelled" for child in children)
        first_proofs = {child.id: child.payload["killsw1_terminal_read_at"] for child in children}
    fresh_receipt(book)
    assert (await drive(book)).phase == "EXIT_WORKING"
    assert len(book.submits) == 1 and book.submits[0].quantity == Decimal(10)
    with book.sessions() as unit:
        for identity, first_proof in first_proofs.items():
            assert unit.get(BrokerOrder, identity).payload["killsw1_terminal_read_at"] == first_proof


@pytest.mark.asyncio
async def test_minimum_broker_quantity_not_shared_total_or_old_managed_quantity(book):
    await drive(book)
    fresh_receipt(book)
    with book.sessions.begin() as unit:
        unit.scalar(select(AccountPosition)).quantity = Decimal(5)
    await drive(book)
    assert len(book.submits) == 1 and book.submits[0].quantity == Decimal(5)


@pytest.mark.asyncio
async def test_working_stop_recovers_by_exact_order_read_without_another_wire(book):
    book.soft_status = "accepted"
    await drive(book)
    fresh_receipt(book)
    assert (await drive(book)).phase == "EXIT_WORKING"
    with book.sessions() as unit:
        assert unit.scalar(select(BotControlAttempt.status).where(
            BotControlAttempt.operation == "SUBMIT_EXIT")) == "WORKING"
    book.soft_status = "filled"
    assert (await drive(book)).phase == "EXIT_WORKING"
    assert (await drive(book)).phase == "SETTLED"
    assert len(book.submits) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("resume_delay", [0, 31])
async def test_final_worker_proof_cannot_age_on_event_loop_resume_before_wire(book, monkeypatch, resume_delay):
    await drive(book)
    fresh_receipt(book)
    original = book.service._run_db
    ceilings = []
    async def delayed_resume(operation, **kwargs):
        result = await original(operation, **kwargs)
        if getattr(operation, "__name__", "") == "ceiling":
            ceilings.append(result[4])
            if len(ceilings) == 3:
                # The worker validated an actual unchanged wire receipt. Delay
                # only its return to the loop; never modify or fresh-stamp proof.
                book.clock.value = (book.clock.now(UTC) + timedelta(seconds=resume_delay)).isoformat()
        return result
    monkeypatch.setattr(book.service, "_run_db", delayed_resume)
    progress = await drive(book)
    assert len(ceilings) == 3 and len(set(ceilings)) == 1
    with book.sessions() as unit:
        attempt = unit.scalar(select(BotControlAttempt).where(BotControlAttempt.operation == "SUBMIT_EXIT"))
        assert attempt.dispatch_intent_at is not None
        if resume_delay:
            assert progress.phase == "BLOCKED_UNKNOWN" and not book.submits
            assert attempt.status == "MAY_HAVE_SENT"
            assert unit.get(OmsManagedPosition, book.row).current_quantity == 10
        else:
            assert progress.phase == "EXIT_WORKING" and len(book.submits) == 1
            assert attempt.status == "TERMINAL"


@pytest.mark.asyncio
async def test_actual_executor_schedule_persists_valid_phases_and_settlement(book):
    runtime = book.service._bot_control_runtime
    executor = runtime.executor
    executor.identity = "controlled-executor"
    book.soft_status = "accepted"
    async def cycle(expected):
        executor.schedule({atr.STRATEGY: runtime.store.hydrate(book.strategy)})
        await asyncio.gather(*executor.tasks.values())
        assert executor.errors == {}
        with book.sessions() as unit:
            assert unit.get(BotControlCommand, book.lease.command_id).phase == expected
    await cycle("BLOCKED_UNKNOWN")
    fresh_receipt(book)
    await cycle("EXIT_WORKING")
    book.soft_status = "filled"
    await cycle("EXIT_WORKING")
    await cycle("SETTLED")
    assert len(book.submits) == 1
    await executor.close()


@pytest.mark.asyncio
async def test_manual_hundred_shares_are_never_sold_as_owned(book):
    await drive(book)
    fresh_receipt(book)
    with book.sessions.begin() as unit:
        unit.scalar(select(AccountPosition)).quantity = Decimal(110)
    await drive(book)
    assert len(book.submits) == 1 and book.submits[0].quantity == Decimal(10)
    with book.sessions() as unit:
        assert unit.scalar(select(AccountPosition.quantity)) == 100


@pytest.mark.asyncio
async def test_cancel_unknown_after_dispatch_is_read_only_on_recovery(book, monkeypatch):
    with book.sessions.begin() as unit:
        unit.get(BrokerOrder, book.entry).status = "working"
        for fill in unit.scalars(select(Fill)).all():
            unit.delete(fill)
    original = book.broker.fetch_order_update
    async def read(request):
        return None if book.cancels else await original(request)
    monkeypatch.setattr(book.broker, "fetch_order_update", read)
    for _ in range(2):
        with pytest.raises(ControlConflict):
            await atr._cancel_entry(book.service, book.lease, book.journal, book.entry)
    assert len(book.cancels) == 1 and not book.submits
    with book.sessions() as unit:
        assert unit.scalar(select(BotControlAttempt.status)) == "MAY_HAVE_SENT"


@pytest.mark.asyncio
async def test_filled_target_and_working_sibling_never_close_until_every_child_terminal(book):
    with book.sessions.begin() as unit:
        entry = unit.get(BrokerOrder, book.entry)
        for child_id in ("own-target", "own-stop"):
            unit.add(BrokerOrder(strategy_id=book.strategy, broker_account_id=book.account,
                intent_id=entry.intent_id, client_order_id=oco_exit_client_order_id(entry.client_order_id, child_id),
                broker_order_id=child_id, symbol="ABC", side="sell", order_type="oco_exit",
                time_in_force="day", quantity=Decimal(10), status="working", submitted_at=book.clock.now(UTC)))
    book.release_outcome = "resolved_by_fill"
    sibling = {"status": "working"}
    def target(request):
        return ExecutionReport(event_type="filled", origin="broker", client_order_id=request.client_order_id,
            broker_order_id="own-target", broker_fill_id="own-target:1", symbol="ABC", side="sell",
            intent_type="close", quantity=Decimal(10), filled_quantity=Decimal(10),
            fill_price=Decimal(11), reported_at=book.clock.now(UTC))
    def stop(request):
        return ExecutionReport(event_type=sibling["status"], origin="broker", client_order_id=request.client_order_id,
            broker_order_id="own-stop", symbol="ABC", side="sell", intent_type="close",
            quantity=Decimal(10), filled_quantity=Decimal(0), reported_at=book.clock.now(UTC))
    book.child_reports.update({"own-target": target, "own-stop": stop})
    assert (await drive(book)).phase == "BLOCKED_UNKNOWN"
    with book.sessions() as unit:
        assert unit.get(OmsManagedPosition, book.row).status == "open"
        assert unit.scalar(select(VirtualPosition.quantity)) == 10
        assert unit.scalar(select(BotControlAttempt.status)) != "TERMINAL"
    sibling["status"] = "cancelled"
    await drive(book)
    assert (await drive(book)).phase == "SETTLED"
    assert book.release_count == 1 and not book.submits
    with book.sessions() as unit:
        assert unit.get(OmsManagedPosition, book.row).status == "closed"
        assert unit.scalar(select(VirtualPosition.quantity)) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("unknown", [None, "held"])
async def test_boot_held_or_unreadable_inventory_never_releases_or_sells(book, unknown):
    book.service._bot_control_runtime.boot_managed_ids = None if unknown is None else {str(book.row)}
    assert (await drive(book)).phase == "BLOCKED_UNKNOWN"
    assert book.release_count == 0 and not book.submits


@pytest.mark.asyncio
async def test_boot_held_alert_is_exact_critical_and_durable_once_per_command_row(book, caplog):
    book.service._bot_control_runtime.boot_managed_ids = frozenset({str(book.row)})
    for _ in range(3):
        assert (await drive(book)).phase == "BLOCKED_UNKNOWN"
    assert not book.submits and book.release_count == 0
    records = [record for record in caplog.records if record.levelname == "CRITICAL"
               and atr.HELD_BOOT_MESSAGE in record.getMessage()]
    assert len(records) == 1
    with book.sessions() as unit:
        incidents = unit.scalars(select(SystemIncident)).all()
        assert len(incidents) == 1
        assert incidents[0].severity == "critical"
        assert incidents[0].title == "STOP: held across restart \u2014 sell manually or wait for normal exit"
        assert incidents[0].payload["managed_row_id"] == str(book.row)
        assert incidents[0].payload["command_id"] == str(book.lease.command_id)


@pytest.mark.asyncio
async def test_boot_held_still_cancels_own_working_entry_before_position_block(book):
    book.service._bot_control_runtime.boot_managed_ids = frozenset({str(book.row)})
    with book.sessions.begin() as unit:
        intent = TradeIntent(strategy_id=book.strategy, broker_account_id=book.account,
            symbol="ABC", side="buy", intent_type="open", quantity=Decimal(2),
            reason="own-pending-entry", status="submitted")
        unit.add(intent)
        unit.flush()
        unit.add(BrokerOrder(intent_id=intent.id, strategy_id=book.strategy, broker_account_id=book.account,
            symbol="ABC", side="buy", client_order_id="own-pending-entry", broker_order_id="own-pending-root",
            order_type="limit", time_in_force="day", quantity=Decimal(2), status="working"))
    assert (await drive(book)).phase == "CANCELLING_ENTRIES"
    assert len(book.cancels) == 1 and book.cancels[0].client_order_id == "own-pending-entry"
    assert (await drive(book)).phase == "BLOCKED_UNKNOWN"
    assert not book.submits and book.release_count == 0


@pytest.mark.asyncio
async def test_orphan_working_entry_never_gets_cancel_authority(book, monkeypatch):
    book.service._bot_control_runtime.boot_managed_ids = frozenset({str(book.row)})
    with book.sessions.begin() as unit:
        unit.add(BrokerOrder(strategy_id=book.strategy, broker_account_id=book.account,
            symbol="ABC", side="buy", client_order_id="orphan-pending-entry", broker_order_id="orphan-root",
            order_type="limit", time_in_force="day", quantity=Decimal(2), status="working"))

    async def must_not_read(_):
        raise AssertionError("missing durable intent must refuse before broker I/O")

    monkeypatch.setattr(book.broker, "fetch_order_update", must_not_read)
    assert (await drive(book)).phase == "BLOCKED_UNKNOWN"
    assert not book.cancels and not book.submits and book.release_count == 0


@pytest.mark.asyncio
async def test_boot_held_first_does_not_starve_new_position_existing_exit_owner(book, monkeypatch):
    book.service._bot_control_runtime.boot_managed_ids = frozenset({str(book.row)})
    with book.sessions.begin() as unit:
        intent = TradeIntent(strategy_id=book.strategy, broker_account_id=book.account,
            symbol="AFTER", side="buy", intent_type="open", quantity=Decimal(3), reason="postboot", status="filled")
        unit.add(intent)
        unit.flush()
        entry = BrokerOrder(intent_id=intent.id, strategy_id=book.strategy, broker_account_id=book.account,
            symbol="AFTER", side="buy", client_order_id="postboot-parent", broker_order_id="postboot-root",
            order_type="market", time_in_force="day", quantity=Decimal(3), status="filled",
            submitted_at=book.clock.now(UTC))
        unit.add(entry)
        unit.flush()
        row = OmsManagedPosition(strategy_code=atr.STRATEGY, broker_account_name=book.account_name,
            symbol="AFTER", entry_order_id=entry.id, entry_client_order_id=entry.client_order_id,
            entry_price=Decimal(6), original_quantity=3, current_quantity=3, status="open")
        unit.add_all([row, VirtualPosition(strategy_id=book.strategy, broker_account_id=book.account,
            symbol="AFTER", quantity=Decimal(3), average_price=Decimal(6)),
            Fill(order_id=entry.id, strategy_id=book.strategy, broker_account_id=book.account,
                 symbol="AFTER", side="buy", broker_fill_id="postboot-root:1", quantity=Decimal(3), price=Decimal(6))])
        unit.flush()
        new_identity = str(row.id)
    called = []
    original_run_db = book.service._run_db
    async def old_first(operation, **kwargs):
        result = await original_run_db(operation, **kwargs)
        if getattr(operation, "__name__", "") == "inventory":
            identities, working, original_errors = result
            assert set(identities) == {str(book.row), new_identity}
            return (str(book.row), new_identity), working, original_errors
        return result
    monkeypatch.setattr(book.service, "_run_db", old_first)
    async def existing_owner_sender(unit, row, **kwargs):
        # Only the broker endpoint is paused: invocation is through the real
        # existing claimed owner. This scheduling control claims no broker fill.
        key = (book.account_name, "AFTER", new_identity)
        assert key in book.service._control_managed_exit_claims
        called.append(str(row.id))
        atr.current_stop(book.service, row.id).phase = "EXIT_WORKING"
        return _ManagedSellEvents(ownership_refused=True)
    monkeypatch.setattr(book.service, "_emit_v2_managed_sell", existing_owner_sender)
    assert (await drive(book)).phase == "BLOCKED_UNKNOWN"
    assert called == [new_identity]
    assert book.release_count == 0 and not book.submits
    with book.sessions() as unit:
        assert unit.get(OmsManagedPosition, book.row).current_quantity == 10


@pytest.mark.asyncio
async def test_uncertain_native_release_never_redispatches(book):
    book.release_outcome = "unanswerable"
    assert (await drive(book)).phase == "BLOCKED_UNKNOWN"
    assert (await drive(book)).phase == "BLOCKED_UNKNOWN"
    assert book.release_count == 1 and not book.submits


@pytest.mark.asyncio
async def test_refused_stop_stays_blocked_without_second_submit(book):
    await drive(book)
    fresh_receipt(book)
    book.reject = True
    assert (await drive(book)).phase == "BLOCKED_UNKNOWN"
    assert (await drive(book)).phase == "BLOCKED_UNKNOWN"
    assert len(book.submits) == 1 and book.release_count == 1
    with book.sessions() as unit:
        assert unit.scalar(select(VirtualPosition.quantity)) == 10
        assert unit.get(OmsManagedPosition, book.row).status == "open"


@pytest.mark.asyncio
@pytest.mark.parametrize("hours,order_type,session", [(12, "limit", "AM"), (14, "market", "NORMAL"), (22, "limit", "PM")])
async def test_stop_sessions_use_existing_broker_and_fresh_bid(book, hours, order_type, session):
    book.clock.value = f"2026-10-15T{hours:02}:00:00+00:00"
    book.service._latest_quotes_by_symbol["ABC"]["received_at"] = book.clock.now(UTC)
    # Reclaim the same command under a fresh lease after the controlled clock jump.
    book.lease = book.service._bot_control_runtime.store.claim_stop(
        book.lease.command_id, "controlled-executor", now=book.clock.now(UTC))
    await drive(book)
    fresh_receipt(book)
    await drive(book)
    assert len(book.submits) == 1
    sent = book.submits[0]
    assert sent.broker_account_name == book.account_name
    assert sent.order_type == order_type and sent.metadata["session"] == session
    if order_type == "limit":
        assert sent.metadata["limit_price"] == "10.05"


@pytest.mark.asyncio
async def test_stale_bid_never_releases_in_extended_hours(book):
    book.clock.value = "2026-10-15T22:00:00+00:00"
    book.lease = book.service._bot_control_runtime.store.claim_stop(
        book.lease.command_id, "controlled-executor", now=book.clock.now(UTC))
    assert (await drive(book)).phase == "BLOCKED_UNKNOWN"
    assert not book.submits and book.release_count == 0


@pytest.mark.parametrize("bad", [None, "foreign-fill", ""])
def test_positive_bound_child_execution_identity_never_falls_back_to_vp(book, bad):
    with book.sessions.begin() as unit:
        entry = unit.get(BrokerOrder, book.entry)
        child = BrokerOrder(strategy_id=book.strategy, broker_account_id=book.account,
            intent_id=entry.intent_id, client_order_id=oco_exit_client_order_id(entry.client_order_id, "own-child"),
            broker_order_id="own-child", symbol="ABC", side="sell", order_type="oco_exit",
            time_in_force="day", quantity=Decimal(4), status="filled", submitted_at=book.clock.now(UTC))
        unit.add(child)
        unit.flush()
        unit.add(Fill(order_id=child.id, strategy_id=book.strategy, broker_account_id=book.account,
            broker_fill_id=bad, symbol="ABC", side="sell", quantity=Decimal(4), price=Decimal(11)))
    with book.sessions() as unit, pytest.raises(ControlConflict):
        atr._managed(book.service, unit, book.lease, book.row)


@pytest.mark.asyncio
async def test_exact_entry_cancel_dispatches_once_and_uses_read_not_ack(book):
    with book.sessions.begin() as unit:
        row = unit.get(BrokerOrder, book.entry)
        row.status = "working"
        for fill in unit.scalars(select(Fill)).all():
            unit.delete(fill)
    await atr._cancel_entry(book.service, book.lease, book.journal, book.entry)
    await atr._cancel_entry(book.service, book.lease, book.journal, book.entry)
    assert len(book.cancels) == 1 and not book.submits
    assert book.cancels[0].metadata["broker_order_id"] == "own-parent-root"
    with book.sessions() as unit:
        assert unit.scalar(select(BotControlAttempt.status)) == "TERMINAL"


@pytest.mark.asyncio
async def test_unknown_cancel_read_does_not_cancel_or_synthesize_fill(book, monkeypatch):
    with book.sessions.begin() as unit:
        unit.get(BrokerOrder, book.entry).status = "working"
    async def unknown(request):
        return None
    monkeypatch.setattr(book.broker, "fetch_order_update", unknown)
    with pytest.raises(ControlConflict):
        await atr._cancel_entry(book.service, book.lease, book.journal, book.entry)
    assert not book.cancels and not book.submits
    with book.sessions() as unit:
        assert len(unit.scalars(select(Fill)).all()) == 1


def test_cumulative_average_is_converted_to_incremental_cash_not_naive_average(book):
    with book.sessions.begin() as unit:
        order = unit.get(BrokerOrder, book.entry)
        order.side = "sell"
        fill = unit.scalar(select(Fill))
        fill.side, fill.quantity, fill.price = "sell", Decimal(4), Decimal(6)
    with book.sessions() as unit:
        order = unit.get(BrokerOrder, book.entry)
        report = ExecutionReport(event_type="filled", client_order_id=order.client_order_id,
            filled_quantity=Decimal(10), fill_price=Decimal("6.6"))
        normalized = atr._incremental_report(unit, order, report)
        assert normalized.fill_price == Decimal(7)
        assert Decimal(4) * Decimal(6) + Decimal(6) * normalized.fill_price == Decimal(66)
        partial = replace(report, event_type="cancelled", filled_quantity=Decimal(7), fill_price=Decimal("6.3"))
        normalized_partial = atr._incremental_report(unit, order, partial)
        assert normalized_partial.fill_price == Decimal("6.7")
        assert Decimal(24) + Decimal(3) * normalized_partial.fill_price == Decimal("44.1")
        assert normalized_partial.reported_at == partial.reported_at
        assert normalized_partial.metadata["killsw1_cumulative_cash"] == "44.1"
        atr._incremental_report(unit, order, replace(report, filled_quantity=Decimal(4),
            fill_price=Decimal("6.000000005")))
        with pytest.raises(ControlConflict, match="cash_conflict"):
            atr._incremental_report(unit, order, replace(report, filled_quantity=Decimal(4),
                fill_price=Decimal("6.000000006")))
        with pytest.raises(ControlConflict, match="cash_conflict"):
            atr._incremental_report(unit, order, replace(report,
                filled_quantity=Decimal(4), fill_price=Decimal(7)))
