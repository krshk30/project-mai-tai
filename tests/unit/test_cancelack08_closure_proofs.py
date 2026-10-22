"""Supported writer-shape controls, not production/native incident receipts."""

from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.orm.attributes import flag_modified

from project_mai_tai.cancelack1 import (
    ObligationScope, _closed_with_replacements, load_proven_accounts, working_buy_present,
)
from project_mai_tai.db.models import (
    BrokerAccount, BrokerOrder, BrokerOrderEvent, DashboardSnapshot, Fill,
    OmsManagedPosition, Strategy, TradeIntent,
)
from tests.unit.test_cancelack1 import synthetic_case
from tests.unit.test_cancelack1_four_sites import confirmation_case
from tests.unit.test_cancelack1_restart_review import later_closed_episode
from tests.unit.test_v2_fresh_sell_owner_release import MIRROR, PRIMARY, instant, refresh


def envelope(order, account, *, quantity=None):
    return {"client_order_id": order.client_order_id, "symbol": order.symbol, "side": "buy",
        "broker_account_name": account, "strategy_code": "schwab_1m_v2",
        "quantity": str(order.quantity if quantity is None else quantity),
        "metadata": {"broker_order_id": order.broker_order_id}}


def partial_closed_episode(bot, request, clock, account, writer, fault):
    later_closed_episode(bot, request, clock, account)
    with bot.session_factory() as session:
        order = session.scalar(select(BrokerOrder).where(BrokerOrder.client_order_id.like("SYNTHETIC-later-%")))
        parent = session.get(TradeIntent, order.intent_id)
        fill = session.scalar(select(Fill).where(Fill.order_id == order.id))
        event = session.scalar(select(BrokerOrderEvent).where(BrokerOrderEvent.order_id == order.id))
        managed = session.scalar(select(OmsManagedPosition).where(OmsManagedPosition.entry_order_id == order.id))
        other = session.scalar(select(BrokerAccount).where(BrokerAccount.name != account))
        flag_modified(order, "updated_at")
        flag_modified(parent, "updated_at")
        flag_modified(managed, "updated_at")
        parent.quantity = order.quantity = 10
        terminal = "rejected" if writer == "webull-rejected" else "cancelled"
        parent.status = order.status = terminal
        fill.quantity, managed.original_quantity = 3, 3
        md = {"webull_terminal_with_partial_fill": terminal}
        event.event_type = terminal
        job = None
        if writer == "rpg":
            token = uuid4()
            job = DashboardSnapshot(id=token, snapshot_type="atr_reprice_handoff", payload={
                "phase": "filled", "no_rebuy": True, "old": envelope(order, account, quantity=10),
                "broker_status": "CANCELLED", "filled_quantity": "3", "recorded_filled_quantity": "3"},
                created_at=instant(clock[0] - 30_000))
            session.add(job)
            event.event_type = "partially_filled"
            md = {"atr_reprice_terminal_cancel": "true", "atr_reprice_readback": str(token)}
        event.payload = {"client_order_id": order.client_order_id, "broker_order_id": order.broker_order_id,
                         "broker_fill_id": fill.broker_fill_id, "metadata": md}
        if fault == "position_remaining":
            managed.status, managed.current_quantity = "open", 1
        elif fault == "managed_quantity_mismatch":
            managed.original_quantity = 2
        elif fault == "missing_managed":
            session.delete(managed)
        elif fault == "wrong_managed_entry":
            managed.entry_order_id = uuid4()
        elif fault == "fill_wrong_account":
            fill.broker_account_id = other.id
        elif fault == "missing_fill_id":
            fill.broker_fill_id = None
        elif fault == "fill_exceeds_order":
            fill.quantity, managed.original_quantity = 11, 11
        elif fault == "native_event_unknown":
            event.event_source = "unknown"
        elif fault == "remainder_working":
            event.event_type = "accepted"
        elif fault == "terminal_settling":
            event.event_at = instant(clock[0] - 14_999)
        elif fault == "unknown_cancel_outcome":
            event.payload = {**event.payload, "cancel_outcome": "could_not_tell"}
        elif fault == "native_identity_mismatch":
            event.payload = {**event.payload, "broker_order_id": "SYNTHETIC-wrong-native"}
        elif fault == "native_fill_identity_mismatch":
            event.payload = {**event.payload, "broker_fill_id": "SYNTHETIC-wrong-fill"}
        elif fault == "cumulative_mismatch":
            event.payload = {**event.payload, "filled_quantity": "4"}
        elif fault == "no_terminal_cancel":
            event.event_type = "partially_filled"
            event.payload = {**event.payload, "metadata": {}}
        elif fault == "native_fill_future":
            fill.filled_at = instant(clock[0] - 10_000)
        elif fault == "rpg_journal_missing":
            session.expunge(job)
        elif fault == "rpg_recorded_quantity_mismatch":
            job.payload = {**job.payload, "recorded_filled_quantity": "2"}
        elif fault == "rpg_remainder_unknown":
            job.payload = {**job.payload, "broker_status": "WORKING"}
        elif fault == "rpg_native_identity_mismatch":
            old = job.payload["old"]
            job.payload = {**job.payload, "old": {**old, "metadata": {"broker_order_id": "SYNTHETIC-wrong"}}}
        elif fault == "rpg_possible_replacement":
            job.payload = {**job.payload, "replacement": {"client_order_id": "SYNTHETIC-possibly-sent"}}
        session.commit()
        return order.id


def assert_old_scope(bot, request, clock, entry, expected):
    refresh(bot, clock)
    with bot.session_factory() as session:
        accounts = {a.id: a.name for a in session.scalars(select(BrokerAccount))}
        strategy_id = session.scalar(select(Strategy.id).where(Strategy.code == "schwab_1m_v2"))
        scope = ObligationScope(request.symbol, request.opportunity_id, clock[0], request.account_names)
        if expected:
            assert load_proven_accounts(session, scope, accounts, strategy_id) == frozenset({entry.broker_account_name})
            assert not working_buy_present(session, request.symbol, now=instant(clock[0]))
        else:
            with pytest.raises(ValueError):
                load_proven_accounts(session, scope, accounts, strategy_id)
    assert (entry.order_id in bot._closed_confirmation_entry_ids([entry])) is expected


@pytest.mark.parametrize("writer,account", [("webull", MIRROR), ("webull-rejected", MIRROR),
                                           ("rpg", PRIMARY), ("rpg", MIRROR)])
@pytest.mark.parametrize("fault", ["none", "position_remaining", "managed_quantity_mismatch", "missing_managed",
    "wrong_managed_entry", "fill_wrong_account", "missing_fill_id", "fill_exceeds_order", "native_event_unknown",
    "remainder_working", "terminal_settling", "unknown_cancel_outcome", "native_identity_mismatch",
    "native_fill_identity_mismatch", "cumulative_mismatch", "no_terminal_cancel", "native_fill_future"])
def test_actual_history_partial_cancel_requires_closed_execution_and_dead_remainder(monkeypatch, writer, account, fault):
    required = (MIRROR if account == PRIMARY else PRIMARY,)
    bot, state, clock, request, writes, engine = synthetic_case(monkeypatch, required=required)
    try:
        entry = confirmation_case(bot, request, clock, required, "none")
        order_id = partial_closed_episode(bot, request, clock, account, writer, fault)
        with bot.session_factory() as session:
            order = session.get(BrokerOrder, order_id)
            assert _closed_with_replacements(session, order, clock[0]) is (fault == "none")
        assert_old_scope(bot, request, clock, entry, fault == "none")
        assert state.flip_owner_phase == "bound" and writes == []
        assert not bot.strategy.drain_pending_intents() and not bot.strategy.drain_webull_direct_intents()
    finally:
        engine.dispose()


@pytest.mark.parametrize("account", [PRIMARY, MIRROR])
@pytest.mark.parametrize("fault", ["rpg_journal_missing", "rpg_recorded_quantity_mismatch", "rpg_remainder_unknown",
                                   "rpg_native_identity_mismatch", "rpg_possible_replacement"])
def test_partial_rpg_flag_without_exact_recorded_terminal_readback_holds(monkeypatch, account, fault):
    required = (MIRROR if account == PRIMARY else PRIMARY,)
    bot, state, clock, request, writes, engine = synthetic_case(monkeypatch, required=required)
    try:
        entry = confirmation_case(bot, request, clock, required, "none")
        partial_closed_episode(bot, request, clock, account, "rpg", fault)
        assert_old_scope(bot, request, clock, entry, False)
        assert state.flip_owner_phase == "bound" and writes == []
    finally:
        engine.dispose()


def resolved_later_replacement(bot, request, clock, account, fault):
    later_closed_episode(bot, request, clock, account)
    with bot.session_factory() as session:
        successor = session.scalar(select(BrokerOrder).where(BrokerOrder.client_order_id.like("SYNTHETIC-later-%")))
        parent = session.get(TradeIntent, successor.intent_id)
        event = session.scalar(select(BrokerOrderEvent).where(BrokerOrderEvent.order_id == successor.id))
        old = BrokerOrder(strategy_id=successor.strategy_id, broker_account_id=successor.broker_account_id,
            intent_id=parent.id, symbol=request.symbol, side="buy", quantity=1, status="replaced",
            client_order_id=f"SYNTHETIC-replaced-{uuid4()}", broker_order_id=f"SYNTHETIC-native-{uuid4()}",
            order_type="limit", time_in_force="day", payload=dict(successor.payload),
            submitted_at=successor.submitted_at, updated_at=successor.updated_at)
        session.add(old)
        session.flush()
        flag_modified(old, "updated_at")
        cancelled = BrokerOrderEvent(order_id=old.id, event_type="cancelled", event_source="broker",
            event_at=event.event_at, payload={"cancel_outcome": "confirmed"})
        session.add(cancelled)
        job = {"phase": "filled", "old": envelope(old, account), "replacement": envelope(successor, account)}
        if fault == "working_successor":
            flag_modified(successor, "updated_at")
            successor.status, event.event_type = "accepted", "accepted"
        elif fault == "unknown_successor":
            flag_modified(successor, "updated_at")
            successor.status = "unknown"
        elif fault == "unknown_job":
            job["phase"] = "submit_unknown"
        elif fault == "mismatched_account":
            job["replacement"]["broker_account_name"] = PRIMARY if account == MIRROR else MIRROR
        elif fault == "wrong_quantity":
            job["replacement"]["quantity"] = "2"
        elif fault == "wrong_strategy":
            job["replacement"]["strategy_code"] = "orb_schwab"
        elif fault == "unlinked_old":
            job["old"]["client_order_id"] = "SYNTHETIC-unknown-old"
        elif fault == "old_native_missing":
            old.broker_order_id = None
        elif fault == "old_cancel_not_final":
            cancelled.payload = {"cancel_outcome": "already_absent"}
        elif fault == "old_settling":
            cancelled.event_at = instant(clock[0] - 14_999)
        elif fault == "successor_token_mismatch":
            flag_modified(successor, "updated_at")
            successor.payload = {**successor.payload, "clearwait_dispatch_token": "SYNTHETIC-wrong"}
        if fault != "missing_journal":
            session.add(DashboardSnapshot(snapshot_type="atr_reprice_handoff", payload=job,
                                          created_at=instant(clock[0] - 30_000)))
        session.commit()
        return old.id


@pytest.mark.parametrize("account", [PRIMARY, MIRROR])
@pytest.mark.parametrize("fault", ["none", "missing_journal", "working_successor", "unknown_successor",
    "unknown_job", "mismatched_account", "wrong_quantity", "wrong_strategy", "unlinked_old", "old_native_missing",
    "old_cancel_not_final", "old_settling", "successor_token_mismatch"])
def test_foreign_query_and_global_guard_share_resolved_replacement_proof(monkeypatch, account, fault):
    required = (MIRROR if account == PRIMARY else PRIMARY,)
    bot, state, clock, request, writes, engine = synthetic_case(monkeypatch, required=required)
    try:
        entry = confirmation_case(bot, request, clock, required, "none")
        old_id = resolved_later_replacement(bot, request, clock, account, fault)
        if fault == "none":
            with bot.session_factory() as session:
                assert _closed_with_replacements(session, session.get(BrokerOrder, old_id), clock[0])
                assert not working_buy_present(session, request.symbol, now=instant(clock[0]))
        assert_old_scope(bot, request, clock, entry, fault == "none")
        assert state.flip_owner_phase == "bound" and writes == []
        assert not bot.strategy.drain_pending_intents() and not bot.strategy.drain_webull_direct_intents()
    finally:
        engine.dispose()
