"""18:2x exact native-row controls; no live broker/PG/performance claims."""
from datetime import timedelta

import pytest

from project_mai_tai.db.models import BrokerOrder, BrokerOrderEvent, TradeIntent
from project_mai_tai.fanout_identity import fanout_slot_id
from project_mai_tai.services.schwab_1m_v2_bot import SchwabV2BotService
from tests.unit.test_eh2rth1_handover import (
    NATIVE_AT, NATIVE_METADATA, native_book as _native_book, prove, quote, setup,
)

native_book = _native_book


def armed():
    strategy, state, clock = setup(strategy_schwab_1m_v2_atr_reprice_handoff_enabled=False)
    strategy.on_quote(state.symbol, quote(state, clock))
    primary, = strategy.drain_pending_intents()
    assert not strategy.settings.strategy_schwab_1m_v2_atr_reprice_handoff_enabled
    return strategy, state, clock, primary


def align(state, factory, order_id, event_id):
    state.resting_schwab_generation = 'GEN'
    metadata = {**NATIVE_METADATA, 'fanout_segment_id': str(state.fanout_segment_id),
                'fanout_slot_id': fanout_slot_id(strategy_code='schwab_1m_v2', symbol=state.symbol,
                    segment_id=state.fanout_segment_id, slot='resting')}
    with factory() as session:
        order = session.get(BrokerOrder, order_id)
        order.payload = metadata
        intent = session.get(TradeIntent, order.intent_id)
        intent.payload = {'metadata': metadata}
        event = session.get(BrokerOrderEvent, event_id)
        event.payload = {**event.payload, 'metadata': metadata}
        session.commit()


@pytest.mark.parametrize('status', ['cancelled', 'rejected', 'expired'])
def test_nonrpg_native_terminal_then_next_bar_replaces_once_same_slot(native_book, status):
    bot, factory, (order_id, event_id) = native_book
    strategy, state, clock, original = armed()
    align(state, factory, order_id, event_id)
    old_segment = state.fanout_segment_id
    with factory() as session:
        order = session.get(BrokerOrder, order_id)
        order.status = status
        event = session.get(BrokerOrderEvent, event_id)
        event.event_type = status  # Native event shape deliberately has no filled_quantity.
        session.commit()
    book = SchwabV2BotService._fetch_primary_resting_book(bot)
    assert book[(state.symbol, 'GEN')]['terminal_zero_proven']
    strategy.apply_primary_resting_book(observed_at_ms=clock[0], orders=book)
    assert not state.resting_is_broker_order and not state.resting_active
    assert strategy.drain_pending_intents() == []
    clock[0] += 60_000
    state.last_quote = quote(state, clock)
    state.bars[-1] = type(state.bars[-1])(
        clock[0]-60_000, 1.89, 1.90, 1.88, 1.89, 25_000)
    for _ in range(3):
        strategy._cw_v2_resting_track(state, None)
    drafts = strategy.drain_pending_intents()
    assert len(drafts) == 1, [(d.intent_type, d.metadata.get('reason'), d.metadata.get('rpg_resting_generation')) for d in drafts]
    replacement, = drafts
    assert replacement.metadata['order_type'] == 'STOP_LIMIT'
    assert replacement.metadata['rpg_resting_generation'] != 'GEN'
    assert replacement.metadata['cw_entry_slot'] == original.metadata['cw_entry_slot']
    assert replacement.metadata['fanout_slot_id'] == original.metadata['fanout_slot_id']
    assert state.fanout_segment_id == old_segment and state.resting_is_broker_order


def test_oms_positive_pre_wire_refusal_same_slot_replacement_without_order(native_book):
    bot, factory, (order_id, event_id) = native_book
    strategy, state, clock, original = armed()
    align(state, factory, order_id, event_id)
    with factory() as session:
        order = session.get(BrokerOrder, order_id)
        intent = session.get(TradeIntent, order.intent_id)
        for event in session.query(BrokerOrderEvent).filter_by(order_id=order_id):
            session.delete(event)
        session.delete(order)
        intent.status = 'rejected'
        intent.payload = {**intent.payload, 'refusal_origin': 'skipped_before_submit',
                         'refusal_code': 'controlled_before_wire'}
        session.commit()
    book = SchwabV2BotService._fetch_primary_resting_book(bot)
    assert book[(state.symbol, 'GEN')]['status'] == 'refused_before_wire'
    strategy.apply_primary_resting_book(observed_at_ms=clock[0], orders=book)
    strategy._cw_v2_resting_track(state, None)
    replacement, = strategy.drain_pending_intents()
    assert replacement.metadata['fanout_slot_id'] == original.metadata['fanout_slot_id']


@pytest.mark.parametrize('age,allowed', [(0, True), (14999, True), (15000, False), (15001, False)])
def test_accepted_idless_submit_ack_before_working_book_no_duplicate(native_book, age, allowed):
    bot, factory, (order_id, event_id) = native_book
    strategy, state, clock, _ = armed()
    align(state, factory, order_id, event_id)
    with factory() as session:
        order, event = session.get(BrokerOrder, order_id), session.get(BrokerOrderEvent, event_id)
        order.status, order.broker_order_id = 'pending', None
        event.event_type, event.event_source = 'accepted', 'unknown'
        event.event_at = NATIVE_AT-timedelta(milliseconds=age)
        session.commit()
    book = SchwabV2BotService._fetch_primary_resting_book(bot)
    strategy.apply_primary_resting_book(observed_at_ms=clock[0], orders=book)
    for _ in range(3):
        strategy._cw_v2_resting_track(state, None)
    assert strategy.drain_pending_intents() == [] and state.resting_is_broker_order
    strategy._fanout_rth_resting_cross(state, quote(state, clock, 1.925))
    assert bool(strategy.drain_webull_fanout_intents()) is allowed


@pytest.mark.parametrize('age,allowed', [(14999, True), (15000, True), (15001, False)])
def test_failed_book_retains_last_good_original_timestamp_only_to_ttl(age, allowed):
    strategy, state, clock, _ = armed()
    prove(strategy, state, clock)
    original = strategy._primary_resting_book
    clock[0] += age
    strategy.apply_primary_resting_book(observed_at_ms=clock[0], orders=None)
    assert strategy._primary_resting_book is original
    strategy._fanout_rth_resting_cross(state, quote(state, clock, 1.925))
    assert bool(strategy.drain_webull_fanout_intents()) is allowed
    assert strategy.drain_pending_intents() == []


@pytest.mark.parametrize('bad', ['status_only', 'client', 'partial', 'wrong_generation', 'future', 'filled_qty'])
def test_terminal_unknown_or_fill_never_clears_or_replaces(native_book, bad):
    bot, factory, (order_id, event_id) = native_book
    strategy, state, clock, _ = armed()
    align(state, factory, order_id, event_id)
    with factory() as session:
        order, event = session.get(BrokerOrder, order_id), session.get(BrokerOrderEvent, event_id)
        order.status = 'cancelled'
        if bad != 'status_only':
            event.event_type = 'cancelled'
        if bad == 'client':
            event.event_source = 'client'
        elif bad == 'partial':
            session.add(BrokerOrderEvent(order_id=order_id, event_type='partially_filled',
                event_source='broker', event_at=NATIVE_AT-timedelta(seconds=1)))
        elif bad == 'wrong_generation':
            event.payload = {**event.payload, 'metadata': {**event.payload['metadata'], 'rpg_resting_generation': 'OTHER'}}
        elif bad == 'future':
            event.event_at = NATIVE_AT+timedelta(seconds=1)
        elif bad == 'filled_qty':
            event.payload = {**event.payload, 'filled_quantity': '1'}
        session.commit()
    book = SchwabV2BotService._fetch_primary_resting_book(bot)
    strategy.apply_primary_resting_book(observed_at_ms=clock[0], orders=book)
    assert state.resting_is_broker_order
    for _ in range(3):
        strategy._cw_v2_resting_track(state, None)
    assert strategy.drain_pending_intents() == []
