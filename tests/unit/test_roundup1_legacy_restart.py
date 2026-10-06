"""Real startup/independent-reader controls for generic no-ticket recovery.

Prices come from recorded SCKT/PFSA rows; order lifecycles and broker-origin
reports are controlled permutations, not historical accepted-restart claims.
"""

from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
import importlib.util
from pathlib import Path

import pytest
from sqlalchemy import select

from project_mai_tai.db.models import BrokerOrderEvent, Fill, Strategy
from project_mai_tai.events import TradeIntentEvent, TradeIntentPayload
from project_mai_tai.oms.store import OmsStore
from project_mai_tai.services.schwab_1m_v2_bot import SchwabV2BotService


DIAGNOSTIC = Path(__file__).parents[2] / "docs/review-artifacts/roundup1/test_legacy_restart_gap.py"
spec = importlib.util.spec_from_file_location("roundup1_restart_diagnostic", DIAGNOSTIC)
diagnostic = importlib.util.module_from_spec(spec)
spec.loader.exec_module(diagnostic)
local_book = diagnostic.local_book
restart = diagnostic.restart


def assert_owned_blocked(bot, state, leg, slot):
    bot.strategy._queue_resting_place(state, state.atr_trail, slot=slot)
    primary = bot.strategy.drain_pending_intents()
    mirror = bot.strategy.drain_webull_direct_intents()
    assert not (primary if leg == "schwab" else mirror)
    return mirror if leg == "schwab" else primary


def terminal_report(session, order, intent, md, row, clock, *, source="broker", filled="0",
                    client=None, broker=None, kind="cancelled"):
    order.status = kind
    payload = {"client_order_id": client or order.client_order_id,
               "broker_order_id": broker or order.broker_order_id, "metadata": md}
    if filled is not None:
        payload["filled_quantity"] = filled
    session.add(BrokerOrderEvent(order_id=order.id, event_type=kind, event_source=source,
                                event_at=clock + timedelta(microseconds=1), payload=payload))


@pytest.mark.asyncio
@pytest.mark.parametrize("leg", ["schwab", "webull"])
@pytest.mark.parametrize("slot", ["first", "reclaim"])
async def test_discovered_no_ticket_wire_is_hydrated_before_entry_callbacks(monkeypatch, local_book, leg, slot):
    expected = (3.77, 3.79) if leg == "schwab" else (1.06, 1.07)
    observed = []

    def before_callback(bot, state):
        observed.append((bot.strategy._active_resting_trigger(state, leg=leg),
                         bot.strategy._active_resting_cap(state, leg=leg)))
        assert observed[-1] == expected

    bot, state, row = await restart(monkeypatch, local_book, leg, slot, startup_observer=before_callback)
    assert observed == [expected]
    record, = bot.strategy._legacy_resting_orders.values()
    assert record.phase == "working"
    assert (record.account, record.symbol, record.client_id, record.broker_id) == (
        row["account"], row["symbol"], row["client_order_id"], row["broker_order_id"])
    assert record.segment == int(row["payload"]["fanout_segment_id"])
    assert record.generation == row["payload"].get("rpg_resting_generation", row["client_order_id"])
    assert record.slot == slot
    assert record.quantity == int(Decimal(row["quantity"]))
    assert getattr(state, f"resting_{leg}_quantity") == record.quantity
    assert state.resting_is_broker_order and state.resting_active
    assert not bot.strategy._rpg_handoffs
    assert not bot.strategy.drain_pending_intents()
    assert not bot.strategy.drain_webull_direct_intents()


@pytest.mark.asyncio
@pytest.mark.parametrize("leg", ["schwab", "webull"])
@pytest.mark.parametrize("slot", ["first", "reclaim"])
async def test_discovered_unproven_leg_blocks_only_its_account(monkeypatch, local_book, leg, slot):
    bot, state, _ = await restart(monkeypatch, local_book, leg, slot, accepted_proof=False, status="submitted")
    assert len(assert_owned_blocked(bot, state, leg, slot)) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("leg", ["schwab", "webull"])
@pytest.mark.parametrize("slot", ["first", "reclaim"])
@pytest.mark.parametrize("evidence", ["partially_filled", "filled", "accounted_fill", "broker_partial"])
async def test_no_ticket_partial_or_filled_consumes_exact_slot_without_rebuy(monkeypatch, local_book, leg, slot, evidence):
    def seed(session, order, intent, md, row, clock):
        if evidence in {"partially_filled", "filled"}:
            order.status = evidence
        elif evidence == "accounted_fill":
            session.add(Fill(order_id=order.id, strategy_id=order.strategy_id,
                broker_account_id=order.broker_account_id, symbol=order.symbol, side="buy",
                quantity=Decimal("1"), price=Decimal("1"), filled_at=clock))
        else:
            session.add(BrokerOrderEvent(order_id=order.id, event_type="partially_filled", event_source="broker",
                event_at=clock, payload={"client_order_id": order.client_order_id,
                    "broker_order_id": order.broker_order_id, "filled_quantity": "1", "metadata": md}))

    bot, state, row = await restart(monkeypatch, local_book, leg, slot, mutate_book=seed)
    record, = bot.strategy._legacy_resting_orders.values()
    assert record.phase == "consumed" and record.slot == slot
    assert record.quantity == int(Decimal(row["quantity"]))
    assert not state.resting_active
    if leg == "schwab":
        assert state.cw_resting_taken if slot == "first" else state.cw_reclaim_taken
    else:
        assert state.fanout_webull_resting_taken
    assert_owned_blocked(bot, state, leg, slot)
    await bot._legacy_resting_refresh()
    assert_owned_blocked(bot, state, leg, slot)


@pytest.mark.asyncio
@pytest.mark.parametrize("leg", ["schwab", "webull"])
@pytest.mark.parametrize("slot", ["first", "reclaim"])
async def test_no_ticket_exact_broker_terminal_zero_proof_releases(monkeypatch, local_book, leg, slot):
    bot, state, _ = await restart(monkeypatch, local_book, leg, slot, mutate_book=terminal_report)
    record, = bot.strategy._legacy_resting_orders.values()
    assert record.phase == "clear"
    assert not state.resting_active
    bot.strategy._queue_resting_place(state, state.atr_trail, slot=slot)
    assert len(bot.strategy.drain_pending_intents()) == 1
    assert len(bot.strategy.drain_webull_direct_intents()) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("leg", ["schwab", "webull"])
@pytest.mark.parametrize("bad", ["row_only", "client", "unknown", "missing_zero", "wrong_client", "wrong_broker", "malformed_zero"])
async def test_no_ticket_terminal_without_exact_zero_proof_stays_blocked(monkeypatch, local_book, leg, bad):
    def seed(session, order, intent, md, row, clock):
        order.status = "cancelled"
        if bad == "row_only":
            return
        terminal_report(session, order, intent, md, row, clock,
            source=bad if bad in {"client", "unknown"} else "broker",
            filled=None if bad == "missing_zero" else "NaN" if bad == "malformed_zero" else "0",
            client="another-client" if bad == "wrong_client" else None,
            broker="another-broker" if bad == "wrong_broker" else None)

    bot, state, _ = await restart(monkeypatch, local_book, leg, "first", mutate_book=seed)
    assert_owned_blocked(bot, state, leg, "first")


@pytest.mark.asyncio
@pytest.mark.parametrize("leg", ["schwab", "webull"])
async def test_no_ticket_unreadable_refresh_preserves_wire_quantity_and_blocks_admission(monkeypatch, local_book, leg):
    bot, state, _ = await restart(monkeypatch, local_book, leg, "first")
    original = (getattr(state, f"resting_{leg}_quantity"), bot.strategy._active_resting_trigger(state, leg=leg),
                bot.strategy._active_resting_cap(state, leg=leg))

    def unreadable():
        raise RuntimeError("controlled unreadable book")

    monkeypatch.setattr(bot, "session_factory", unreadable)
    await bot._legacy_resting_refresh()
    assert original == (getattr(state, f"resting_{leg}_quantity"), bot.strategy._active_resting_trigger(state, leg=leg),
                        bot.strategy._active_resting_cap(state, leg=leg))
    assert_owned_blocked(bot, state, leg, "first")
    assert not bot.strategy.drain_pending_intents()
    assert not bot.strategy.drain_webull_direct_intents()


@pytest.mark.asyncio
@pytest.mark.parametrize("leg", ["schwab", "webull"])
async def test_default_off_no_ticket_recovery_never_reads_even_if_reader_fails(monkeypatch, local_book, leg):
    def forbidden_read(self):
        pytest.fail("default OFF performed a generic recovery read")

    monkeypatch.setattr(SchwabV2BotService, "_fetch_legacy_resting_orders", forbidden_read)
    bot, state, _ = await restart(monkeypatch, local_book, leg, "first", enabled=False)
    assert not hasattr(bot.strategy, "_legacy_resting_orders")
    assert bot.strategy._active_resting_trigger(state, leg=leg) == 0
    await bot._legacy_resting_refresh()
    bot.strategy._queue_resting_place(state, state.atr_trail)
    assert bot.strategy.drain_pending_intents()
    assert bot.strategy.drain_webull_direct_intents()


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["missing_event", "unknown_origin", "client_origin", "wire_missing", "wire_nan", "wire_conflict", "client_conflict", "quantity_conflict"])
async def test_webull_requires_exact_broker_accepted_wire_proof(monkeypatch, local_book, bad):
    def seed(session, order, intent, md, row, clock):
        session.flush()
        event = session.scalar(select(BrokerOrderEvent).where(BrokerOrderEvent.order_id == order.id))
        if bad == "missing_event":
            session.delete(event)
        elif bad in {"unknown_origin", "client_origin"}:
            event.event_source = bad.split("_")[0]
        else:
            payload = deepcopy(event.payload)
            if bad == "wire_missing":
                payload["metadata"].pop("webull_wire_limit_price")
            elif bad == "wire_nan":
                payload["metadata"]["webull_wire_stop_price"] = "NaN"
            elif bad == "quantity_conflict":
                payload["metadata"]["webull_wire_quantity"] = "279"
            elif bad == "client_conflict":
                payload["client_order_id"] = "wrong-client"
            else:
                payload["metadata"]["webull_wire_stop_price"] = "1.07"
                payload["metadata"]["webull_wire_limit_price"] = "1.08"
                session.add(BrokerOrderEvent(order_id=order.id, event_type="accepted", event_source="broker",
                    event_at=clock, payload=payload))
                return
            event.payload = payload

    bot, state, _ = await restart(monkeypatch, local_book, "webull", "first", mutate_book=seed)
    assert not state.webull_resting_active
    assert bot.strategy._active_resting_trigger(state, leg="webull") == 0
    assert len(assert_owned_blocked(bot, state, "webull", "first")) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("leg", ["schwab", "webull"])
@pytest.mark.parametrize("bad", ["segment", "slot_id", "generation", "attempt", "quantity", "intent_account", "intent_strategy", "slot", "payload"])
async def test_malformed_or_conflicting_durable_identity_fails_closed(monkeypatch, local_book, leg, bad):
    def seed(session, order, intent, md, row, clock):
        payload = deepcopy(order.payload)
        if bad == "intent_account":
            other = OmsStore().ensure_broker_account(session, "unrelated", provider=leg, environment="test")
            intent.broker_account_id = other.id
        elif bad == "intent_strategy":
            intent.strategy_id = OmsStore().ensure_strategy(session, "unrelated", name="unrelated").id
        elif bad == "quantity":
            order.quantity += Decimal("1")
        elif bad == "payload":
            order.payload = ["not metadata"]
        else:
            key = {"segment": "fanout_segment_id", "slot_id": "fanout_slot_id", "generation": "rpg_resting_generation",
                   "attempt": "fanout_attempt_id", "slot": "cw_entry_slot"}[bad]
            payload[key] = "wrong"
            order.payload = payload

    bot, state, _ = await restart(monkeypatch, local_book, leg, "first", mutate_book=seed)
    assert not state.resting_active
    assert_owned_blocked(bot, state, leg, "first")


@pytest.mark.asyncio
@pytest.mark.parametrize("leg", ["schwab", "webull"])
@pytest.mark.parametrize("status", ["accepted", "partially_filled"])
async def test_recorded_order_never_hydrates_or_consumes_a_different_segment(monkeypatch, local_book, leg, status):
    bot, state, _ = await restart(monkeypatch, local_book, leg, "first", status=status,
        state_hook=lambda state: setattr(state, "fanout_segment_id", state.fanout_segment_id + 1))
    record, = bot.strategy._legacy_resting_orders.values()
    assert state.fanout_segment_id != record.segment
    assert not state.resting_active
    assert not state.cw_resting_taken and not state.fanout_webull_resting_taken
    assert getattr(state, f"resting_{leg}_quantity") == 0
    if status == "accepted":
        assert_owned_blocked(bot, state, leg, "first")
    else:
        bot.strategy._queue_resting_place(state, state.atr_trail)
        assert bot.strategy.drain_pending_intents()
        assert bot.strategy.drain_webull_direct_intents()


@pytest.mark.asyncio
@pytest.mark.parametrize("leg", ["schwab", "webull"])
async def test_disappearing_order_never_releases_cached_ownership(monkeypatch, local_book, leg):
    bot, state, _ = await restart(monkeypatch, local_book, leg, "first")
    bot.strategy.apply_legacy_resting_book([], readable_accounts=set(bot.strategy.legacy_resting_accounts()))
    assert_owned_blocked(bot, state, leg, "first")


@pytest.mark.asyncio
@pytest.mark.parametrize("leg", ["schwab", "webull"])
async def test_unreadable_cold_start_with_no_cache_blocks_both_legs(monkeypatch, local_book, leg):
    monkeypatch.setattr(SchwabV2BotService, "_fetch_legacy_resting_orders", lambda self: ([], set()))
    bot, state, _ = await restart(monkeypatch, local_book, leg, "first")
    assert not bot.strategy._legacy_resting_orders
    bot.strategy._queue_resting_place(state, state.atr_trail)
    assert not bot.strategy.drain_pending_intents()
    assert not bot.strategy.drain_webull_direct_intents()


@pytest.mark.asyncio
@pytest.mark.parametrize("leg", ["schwab", "webull"])
async def test_cancel_of_hydrated_legacy_order_targets_exact_entry_ids(monkeypatch, local_book, leg):
    bot, state, row = await restart(monkeypatch, local_book, leg, "first")
    bot.strategy._queue_resting_cancel(state, reason="reprice")
    primary = bot.strategy.drain_pending_intents()
    mirror = bot.strategy.drain_webull_direct_intents()
    draft, = primary if leg == "schwab" else mirror
    assert draft.intent_type == "cancel"
    assert draft.metadata["target_client_order_id"] == row["client_order_id"]
    assert draft.metadata["broker_order_id"] == row["broker_order_id"]
    assert draft.quantity == Decimal(row["quantity"])
    assert not (mirror if leg == "schwab" else primary)
    assert_owned_blocked(bot, state, leg, "first")


def add_other_entry(session, order, md, clock, *, account_name=None, terminal=False):
    store = OmsStore()
    account = store.ensure_broker_account(session, account_name or "live:schwab_1m_v2", provider="schwab", environment="test")
    strategy = session.get(Strategy, order.strategy_id)
    metadata = {key: value for key, value in md.items() if key not in {"fanout_leg", "fanout_source", "webull_mirror_generation_id"}}
    metadata.update(fanout_attempt_id="controlled-primary-entry", stop_price="1.06", limit_price="1.07")
    event = TradeIntentEvent(source_service="controlled-second-entry", produced_at=clock,
        payload=TradeIntentPayload(strategy_code="schwab_1m_v2", broker_account_name=account.name,
            symbol=order.symbol, side="buy", intent_type="open", quantity=order.quantity,
            reason="ATR Flip", metadata=metadata))
    intent = store.create_trade_intent(session, strategy=strategy, broker_account=account, event=event)
    second = store.get_or_create_order(session, intent=intent, strategy_id=strategy.id, broker_account_id=account.id,
        client_order_id=metadata["fanout_attempt_id"], broker_order_id="controlled-primary-broker", symbol=order.symbol,
        side="buy", quantity=order.quantity, metadata=metadata, status="accepted", order_type="STOP_LIMIT")
    if terminal:
        terminal_report(session, second, intent, metadata, {}, clock)
    return second


@pytest.mark.asyncio
async def test_unproven_webull_does_not_block_exactly_proven_clear_schwab(monkeypatch, local_book):
    def seed(session, order, intent, md, row, clock):
        add_other_entry(session, order, md, clock, terminal=True)

    bot, state, _ = await restart(monkeypatch, local_book, "webull", "first", accepted_proof=False, mutate_book=seed)
    assert {record.phase for record in bot.strategy._legacy_resting_orders.values()} == {"clear", "unknown"}
    assert len(assert_owned_blocked(bot, state, "webull", "first")) == 1


@pytest.mark.asyncio
async def test_both_accounts_restore_their_own_wire_and_quantity(monkeypatch, local_book):
    def seed(session, order, intent, md, row, clock):
        add_other_entry(session, order, md, clock)

    bot, state, _ = await restart(monkeypatch, local_book, "webull", "first", mutate_book=seed)
    assert len(bot.strategy._legacy_resting_orders) == 2
    assert state.resting_schwab_quantity == state.resting_webull_quantity == 280
    assert (state.resting_schwab_wire_stop, state.resting_schwab_wire_limit) == (1.06, 1.07)
    assert (state.resting_webull_wire_stop, state.resting_webull_wire_limit) == (1.06, 1.07)
    assert not assert_owned_blocked(bot, state, "webull", "first")


@pytest.mark.asyncio
async def test_multiple_exact_candidates_fail_closed_without_latest_order_selection(monkeypatch, local_book):
    def seed(session, order, intent, md, row, clock):
        add_other_entry(session, order, md, clock, account_name="live:schwab_1m_v2")

    bot, state, _ = await restart(monkeypatch, local_book, "schwab", "first", mutate_book=seed)
    assert len(bot.strategy._legacy_resting_orders) == 2
    assert not state.resting_active
    assert_owned_blocked(bot, state, "schwab", "first")


@pytest.mark.asyncio
async def test_recovery_never_queries_on_quote_or_placement_hotpath(monkeypatch, local_book):
    bot, state, _ = await restart(monkeypatch, local_book, "webull", "first", accepted_proof=False)

    def forbidden():
        pytest.fail("hot path read the database")

    monkeypatch.setattr(bot, "session_factory", forbidden)
    assert len(assert_owned_blocked(bot, state, "webull", "first")) == 1


@pytest.mark.asyncio
async def test_consumption_survives_terminal_refresh_without_releasing_slot(monkeypatch, local_book):
    bot, state, _ = await restart(monkeypatch, local_book, "webull", "first", status="partially_filled")
    record, = bot.strategy._legacy_resting_orders.values()
    bot.strategy.apply_legacy_resting_book([replace(record, phase="clear")],
        readable_accounts=set(bot.strategy.legacy_resting_accounts()))
    assert_owned_blocked(bot, state, "webull", "first")
