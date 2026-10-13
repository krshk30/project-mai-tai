"""Corrective controls: retained readbacks, controlled lifecycle, no venue calls."""
from dataclasses import replace
from datetime import timedelta
from uuid import uuid5, NAMESPACE_URL

import pytest
from sqlalchemy import select

from project_mai_tai.db.models import BrokerOrder, BrokerOrderEvent, TradeIntent
from project_mai_tai.oms.atr_reprice_handoff import HandoffJournal
from project_mai_tai.services.schwab_1m_v2_bot import SchwabV2BotService
from project_mai_tai.strategy_core.schwab_1m_v2 import PRIMARY_RESTING_BOOK_MAX_AGE_MS, session_start_ts_ms
from tests.unit.test_eh2rth1_handover import setup, quote, prove, native_book as _native_book, NATIVE_AT
from tests.unit.test_rpg1_runtime import runtime, begin, feedback

native_book = _native_book


@pytest.mark.asyncio
@pytest.mark.parametrize('status,broker_status', [('cancelled', 'CANCELED'), ('rejected', 'REJECTED')])
async def test_existing_exact_terminal_feedback_clears_primary_for_one_normal_same_slot_rest(monkeypatch, status, broker_status):
    h = await runtime(monkeypatch, 'schwab')
    token, _ = await begin(h, 'schwab')
    await feedback(h)
    request, = h.adapter.opens
    await feedback(h)  # The next poll applies the committed placed feedback.
    assert h.state.resting_is_broker_order
    slot_id = request.metadata['fanout_slot_id']
    with h.factory() as session:
        order = session.scalar(select(BrokerOrder).where(BrokerOrder.client_order_id == request.client_order_id))
        order.status = status
        if status == 'rejected':
            code = 'CONTROLLED-VENUE-REFUSAL'
            intent = session.get(TradeIntent, order.intent_id)
            intent.status = 'rejected'
            intent.payload = {**intent.payload, 'refusal_origin': 'broker_reject', 'refusal_code': code}
            order.payload = {**order.payload, 'reject_reason': code}
            session.add(BrokerOrderEvent(order_id=order.id, event_type='rejected',
                event_source='broker', event_at=h.clock[0], payload={
                    'client_order_id': request.client_order_id, 'reason': code,
                    'filled_quantity': '0', 'metadata': dict(request.metadata)}))
        # Existing successor journal is identity-bearing terminal-zero feedback,
        # not permission inferred from a cancelled status or an empty order book.
        old = replace(request, intent_type='cancel', metadata={
            **request.metadata, 'broker_order_id': order.broker_order_id,
            'resting_entry_cancel': 'true'})
        successor = HandoffJournal(h.factory).prepare_local(old, slot='first',
            segment_id=h.state.fanout_segment_id, now=h.clock[0].timestamp(),
            phase='clear', reason='terminal_cancel_explicit_zero', session=session,
            original_order_id=str(order.id), cleared_at=h.clock[0].timestamp(),
            clear_recorded=True, broker_status=broker_status)
        assert successor == uuid5(NAMESPACE_URL, f'atr-reprice:{request.broker_account_name}:{request.client_order_id}')
        session.commit()
    h.strategy._rpg_handoffs.clear()
    h.strategy._rpg_feedback_applied.clear()
    h.state.atr_state = 'long'
    await feedback(h)
    await feedback(h)  # Apply successor expiry and reconciled predecessor.
    assert not h.state.resting_active and not h.state.resting_is_broker_order
    assert not h.state.resting_schwab_quantity and not h.state.resting_schwab_generation
    assert not h.state.cw_resting_taken
    assert len(h.adapter.opens) == 1
    h.state.atr_state = 'short'
    for _ in range(3):
        h.strategy._cw_v2_resting_track(h.state, None)
    primary, = h.strategy.drain_pending_intents()
    assert primary.intent_type == 'open' and primary.metadata['order_type'] == 'STOP_LIMIT'
    assert primary.metadata['cw_entry_slot'] == 'first'
    assert primary.metadata['fanout_slot_id'] == slot_id
    assert primary.metadata['rpg_resting_generation'] != request.metadata['rpg_resting_generation']
    assert h.state.resting_is_broker_order
    assert h.strategy.drain_pending_intents() == []
    assert HandoffJournal(h.factory).read(token)['phase'] == 'refused'


@pytest.mark.asyncio
@pytest.mark.parametrize('status', ['cancelled', 'rejected'])
async def test_terminal_status_without_exact_feedback_never_grants_normal_replacement(monkeypatch, status):
    h = await runtime(monkeypatch, 'schwab')
    token, _ = await begin(h, 'schwab')
    await feedback(h)
    await feedback(h)
    request, = h.adapter.opens
    with h.factory() as session:
        session.scalar(select(BrokerOrder).where(
            BrokerOrder.client_order_id == request.client_order_id)).status = status
        session.commit()
    await feedback(h)
    for _ in range(3):
        h.strategy._cw_v2_resting_track(h.state, None)
    assert h.strategy.drain_pending_intents() == []
    assert len(h.adapter.opens) == 1
    assert HandoffJournal(h.factory).read(token)['phase'] == 'placed'


@pytest.mark.parametrize('age,allowed', [(0, True), (10001, True), (15000, True), (15001, False), (-1, False)])
def test_three_poll_cache_boundary_is_functional_clock_only(age, allowed):
    strategy, state, clock = setup()
    strategy.on_quote(state.symbol, quote(state, clock))
    primary, = strategy.drain_pending_intents()
    prove(strategy, state, clock)
    clock[0] += age
    assert PRIMARY_RESTING_BOOK_MAX_AGE_MS == 3 * 5000
    strategy._fanout_rth_resting_cross(state, quote(state, clock, 1.925))
    assert bool(strategy.drain_webull_fanout_intents()) is allowed
    assert strategy.drain_pending_intents() == []
    assert primary.metadata['order_type'] == 'STOP_LIMIT'


@pytest.mark.parametrize('prior_terminal', [False, True])
def test_next_day_restoration_never_adopts_prior_primary_as_current_proof(native_book, prior_terminal):
    bot, factory, (order_id, _) = native_book
    bot.observed[0] = NATIVE_AT + timedelta(days=1)
    if prior_terminal:
        with factory() as session:
            session.get(BrokerOrder, order_id).status = 'cancelled'
            session.commit()
    old_book = SchwabV2BotService._fetch_primary_resting_book(bot)
    assert bool(old_book) is not prior_terminal
    strategy, state, clock = setup()
    old_anchor = session_start_ts_ms(clock[0])
    state.atr_session_anchor_ms = old_anchor
    clock[0] += 24 * 60 * 60 * 1000
    strategy._entries_held = True
    assert strategy.roll_stale_session_state(clock[0], is_protected=lambda *_: False) == [state.symbol]
    assert state.atr_session_anchor_ms > old_anchor
    assert not state.resting_active and not state.resting_is_broker_order
    state.bars.clear()
    from project_mai_tai.strategy_core.schwab_1m_v2 import OHLCVBar
    state.bars.append(OHLCVBar(clock[0]-60000, 1.89, 1.90, 1.88, 1.89, 25000))
    state.atr_state, state.atr_state_age, state.atr_trail = 'short', 10, 1.9048
    state.atr_short_flip_bar_ts = clock[0]-10*60000
    strategy.apply_primary_resting_book(observed_at_ms=clock[0], orders=old_book)
    strategy.on_quote(state.symbol, quote(state, clock))
    assert strategy.drain_pending_intents() == []  # No entry before boot clearance.
    assert strategy.drain_webull_fanout_intents() == []
    if not prior_terminal:
        return  # A prior working BUY is not assumed clear at boot.
    # Controlled completed restore: cancelled prior row, current bars, no position.
    # Native boot-release guards are tested separately; no live startup claim.
    strategy._entries_held = False
    strategy._cw_v2_resting_track(state, None)
    primary, = strategy.drain_pending_intents()
    assert primary.metadata['rpg_resting_generation'] != 'GEN'
    assert int(primary.metadata['fanout_segment_id']) >= session_start_ts_ms(clock[0])
    strategy._fanout_rth_resting_cross(state, quote(state, clock, 1.925))
    assert strategy.drain_webull_fanout_intents() == []  # Old book cannot prove new primary.
    prove(strategy, state, clock)
    strategy._fanout_rth_resting_cross(state, quote(state, clock, 1.925))
    assert len(strategy.drain_webull_fanout_intents()) == 1
