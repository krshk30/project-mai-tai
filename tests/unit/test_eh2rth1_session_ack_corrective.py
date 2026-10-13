"""R-only controlled database/stream proofs; no provider or performance claims."""
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest

from project_mai_tai.db.models import BrokerOrder, TradeIntent
from project_mai_tai.events import OrderEventEvent, OrderEventPayload, TradeIntentEvent
from project_mai_tai.fanout_identity import fanout_slot_id
from project_mai_tai.services.schwab_1m_v2_bot import SchwabV2BotService
from project_mai_tai.strategy_core.schwab_1m_v2 import SchwabV2IntentEmitter
from tests.unit.test_eh2rth1_handover import (
    NATIVE_AT, PgCompileSQLiteReadControl, native_book as _native_book, native_expected,
    prove, quote, setup,
)

native_book = _native_book


@pytest.mark.parametrize("noise", ["history", "other_segment", "other_slot", "other_generation", "other_symbol"])
def test_over_one_thousand_unrelated_rows_do_not_consume_current_book_cap(native_book, noise):
    bot, factory, (order_id, _) = native_book
    with factory() as session:
        original = session.get(BrokerOrder, order_id)
        intent = session.get(TradeIntent, original.intent_id)
        for _ in range(1002):
            md = dict(original.payload)
            created = NATIVE_AT
            symbol = original.symbol
            if noise == "history":
                created -= timedelta(days=1)
            elif noise == "other_segment":
                md["fanout_segment_id"] = str(int(md["fanout_segment_id"])-1)
            elif noise == "other_slot":
                md["fanout_slot_id"] = "OTHER"
            elif noise == "other_generation":
                md["rpg_resting_generation"] = "OTHER"
            else:
                symbol = "OTHER"
            extra = TradeIntent(strategy_id=intent.strategy_id, broker_account_id=intent.broker_account_id,
                symbol=symbol, side="buy", intent_type="open", quantity=Decimal(1), reason="controlled",
                status="rejected", created_at=created, payload={"metadata": md,
                    "refusal_origin": "skipped_before_submit", "refusal_code": "controlled"})
            session.add(extra)
            session.flush()
            session.add(BrokerOrder(intent_id=extra.id, strategy_id=intent.strategy_id,
                broker_account_id=intent.broker_account_id, client_order_id=str(uuid4()),
                symbol=symbol, side="buy", order_type="STOP_LIMIT", time_in_force="DAY",
                quantity=Decimal(1), status="cancelled", payload=md))
        session.commit()
    assert SchwabV2BotService._fetch_primary_resting_book(bot) == {("TCRT", "GEN"): native_expected()}


def test_real_exact_current_overcap_has_no_permission_or_cap_waiver(native_book, caplog):
    bot, factory, (order_id, _) = native_book
    with factory() as session:
        original = session.get(BrokerOrder, order_id)
        session.add_all(BrokerOrder(intent_id=original.intent_id, strategy_id=original.strategy_id,
            broker_account_id=original.broker_account_id, client_order_id=str(uuid4()),
            symbol=original.symbol, side="buy", order_type="STOP_LIMIT", time_in_force="DAY",
            quantity=Decimal(1), status="working", payload=original.payload) for _ in range(1000))
        session.commit()
    assert SchwabV2BotService._fetch_primary_resting_book(bot) is None
    assert "count=1001 cap=1000" in caplog.text


def test_cap_remains_unknown_and_logs_bounded_count_warning(native_book, caplog):
    bot, factory, _ = native_book
    control = PgCompileSQLiteReadControl(factory, overflow=True)
    bot.session_factory = lambda: control
    assert SchwabV2BotService._fetch_primary_resting_book(bot) is None
    assert "count=1001 cap=1000" in caplog.text
    assert any(row.levelname == "WARNING" for row in caplog.records)
    sql = control.commands[-1]
    assert "trade_intents.created_at >=" in sql
    assert "fanout_segment_id" in sql and "fanout_slot_id" in sql


@pytest.mark.parametrize("hour,day", [(3, 12), (4, 13)])
def test_four_et_session_anchor_not_calendar_midnight(native_book, hour, day):
    bot, factory, (order_id, _) = native_book
    bot.observed[0] = datetime(2026, 10, 13, hour+4, tzinfo=UTC)
    segment = int(datetime(2026, 10, day, 8, tzinfo=UTC).timestamp()*1000)
    with factory() as session:
        order = session.get(BrokerOrder, order_id)
        session.get(TradeIntent, order.intent_id).created_at = datetime(2026, 10, day, 7, 59, tzinfo=UTC)
        session.commit()
    bot.strategy.primary_resting_scope = lambda: (("TCRT", "GEN", segment, "CONTROLLED", "first"),)
    control = PgCompileSQLiteReadControl(factory)
    bot.session_factory = lambda: control
    assert SchwabV2BotService._fetch_primary_resting_book(bot) == {}
    assert f"2026-10-{day} 08:00:00" in control.commands[-1]


def submitted():
    strategy, state, clock = setup(strategy_schwab_1m_v2_atr_reprice_handoff_enabled=False,
        strategy_schwab_1m_v2_account_name="live:schwab_1m_v2")
    strategy.on_quote(state.symbol, quote(state, clock))
    draft, = strategy.drain_pending_intents()
    event_id = uuid4()
    strategy.note_primary_resting_submission(draft, event_id)
    event = OrderEventEvent(source_service="oms-risk", produced_at=datetime.fromtimestamp(clock[0]/1000, UTC),
        payload=OrderEventPayload(strategy_code="schwab_1m_v2", broker_account_name="live:schwab_1m_v2",
            intent_event_id=event_id, order_db_id=uuid4(), client_order_id="CONTROLLED-PRIMARY",
            symbol=state.symbol, side="buy", intent_type="open", status="accepted",
            quantity=draft.quantity, reason="controlled", metadata=draft.metadata))
    return strategy, state, clock, draft, event


@pytest.mark.parametrize("age,allowed", [(0, True), (4999, True), (14999, True), (15000, False), (-1, False)])
def test_actual_accepted_ack_between_submit_and_first_book_poll(age, allowed):
    strategy, state, clock, _, event = submitted()
    strategy.apply_primary_resting_event(event)
    clock[0] += age
    strategy._fanout_rth_resting_cross(state, quote(state, clock, 1.925))
    assert bool(strategy.drain_webull_fanout_intents()) is allowed
    assert strategy.drain_pending_intents() == []


@pytest.mark.parametrize("bad", ["no_emit", "no_ack", "account", "strategy", "source", "intent_id",
    "no_order", "client_id", "quantity", "filled", "generation", "segment", "slot", "entry_slot",
    "terminal", "future", "newer_empty_book"])
def test_provisional_ack_negative_controls(bad):
    strategy, state, clock, _, event = submitted()
    order = event.payload
    if bad == "no_emit":
        strategy._primary_resting_submissions.clear()
    elif bad == "no_ack":
        event = None
    elif bad == "account":
        order.broker_account_name = "live:orb"
    elif bad == "strategy":
        order.strategy_code = "orb"
    elif bad == "source":
        event.source_service = "controlled-client"
    elif bad == "intent_id":
        order.intent_event_id = uuid4()
    elif bad == "no_order":
        order.order_db_id = None
    elif bad == "client_id":
        order.client_order_id = ""
    elif bad == "quantity":
        order.quantity += 1
    elif bad == "filled":
        order.filled_quantity = 1
    elif bad == "terminal":
        order.status = "rejected"
    elif bad == "future":
        event.produced_at += timedelta(milliseconds=1)
    elif bad != "newer_empty_book":
        key = {"generation": "rpg_resting_generation", "segment": "fanout_segment_id",
               "slot": "fanout_slot_id", "entry_slot": "cw_entry_slot"}[bad]
        order.metadata = {**order.metadata, key: "OTHER"}
    if event is not None:
        strategy.apply_primary_resting_event(event)
    if bad == "newer_empty_book":
        clock[0] += 1
        strategy.apply_primary_resting_book(observed_at_ms=clock[0], orders={})
    strategy._fanout_rth_resting_cross(state, quote(state, clock, 1.925))
    assert strategy.drain_webull_fanout_intents() == []
    assert strategy.drain_pending_intents() == []


def test_terminal_stream_receipt_vetoes_cached_book_and_delayed_ack():
    strategy, state, clock, _, event = submitted()
    strategy.apply_primary_resting_event(event)
    prove(strategy, state, clock)
    terminal = event.model_copy(deep=True)
    terminal.payload.status = "cancelled"
    strategy.apply_primary_resting_event(terminal)
    strategy.apply_primary_resting_event(event)
    strategy._fanout_rth_resting_cross(state, quote(state, clock, 1.925))
    assert strategy.drain_webull_fanout_intents() == []


@pytest.mark.asyncio
@pytest.mark.parametrize("fail", [False, True])
async def test_real_emitter_only_successful_xadd_records_exact_local_submission(fail):
    strategy, state, clock, draft, _ = submitted()
    strategy._primary_resting_submissions.clear()
    class Redis:
        async def xadd(self, stream, data, **kwargs):
            self.event = TradeIntentEvent.model_validate_json(data["data"])
            if fail:
                raise RuntimeError("controlled transport failure")
            return "1-0"
    redis = Redis()
    emitter = SchwabV2IntentEmitter(strategy.settings, redis, "live:schwab_1m_v2",
        on_submitted=strategy.note_primary_resting_submission)
    if fail:
        with pytest.raises(RuntimeError):
            await emitter.emit(draft)
        assert strategy._primary_resting_submissions == {}
    else:
        assert await emitter.emit(draft) == redis.event.event_id
        assert strategy._primary_resting_submissions[state.symbol][1] == redis.event.event_id


@pytest.mark.asyncio
async def test_existing_oms_stream_handler_delivers_ack_without_position_poll():
    strategy, state, clock, _, event = submitted()
    class Redis:
        async def xread(self, **kwargs):
            assert kwargs["count"] == 100
            return [("order-events", [("2-0", {"data": event.model_dump_json()})])]
    bot = SimpleNamespace(redis=Redis(), settings=strategy.settings, strategy=strategy,
                          _primary_order_event_cursor="1-0")
    await SchwabV2BotService._primary_order_events_pass(bot)
    assert bot._primary_order_event_cursor == "2-0"
    strategy._fanout_rth_resting_cross(state, quote(state, clock, 1.925))
    assert len(strategy.drain_webull_fanout_intents()) == 1


def test_ack_can_arrive_during_xadd_but_permission_waits_for_successful_emit():
    strategy, state, clock, draft, event = submitted()
    strategy._primary_resting_submissions.clear()
    strategy.apply_primary_resting_event(event)
    assert not strategy._primary_resting_proven(state)
    strategy.note_primary_resting_submission(draft, event.payload.intent_event_id)
    assert strategy._primary_resting_proven(state)


def test_scope_is_exact_primary_only_and_immutable_snapshot():
    strategy, state, clock, _, _ = submitted()
    scope = strategy.primary_resting_scope()
    assert scope == ((state.symbol, state.resting_schwab_generation, state.fanout_segment_id,
        fanout_slot_id(strategy_code="schwab_1m_v2", symbol=state.symbol,
            segment_id=state.fanout_segment_id, slot="resting"), state.last_resting_placed_slot),)
    state.resting_is_broker_order = False
    assert strategy.primary_resting_scope() == ()
    assert len(scope) == 1


def test_no_active_primary_does_not_query_history(native_book):
    bot, factory, _ = native_book
    bot.strategy.primary_resting_scope = lambda: ()
    bot.session_factory = lambda: (_ for _ in ()).throw(AssertionError("No SQL for empty active scope"))
    assert SchwabV2BotService._fetch_primary_resting_book(bot) == {}


@pytest.mark.asyncio
async def test_malformed_stream_entry_advances_without_granting_or_poisoning_following_ack(caplog):
    strategy, state, clock, _, event = submitted()
    class Redis:
        async def xread(self, **kwargs):
            return [("order-events", [("2-0", {"data": "malformed"}),
                                     ("3-0", {"data": event.model_dump_json()})])]
    bot = SimpleNamespace(redis=Redis(), settings=strategy.settings, strategy=strategy,
                          _primary_order_event_cursor="1-0")
    await SchwabV2BotService._primary_order_events_pass(bot)
    assert bot._primary_order_event_cursor == "3-0"
    assert strategy._primary_resting_proven(state)
    assert "malformed_event=1 provisional_allowed=0" in caplog.text
