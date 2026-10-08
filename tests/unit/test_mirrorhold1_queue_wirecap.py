"""Queue/restart use actual recorded histories; new SDK acknowledgements are controlled."""
from datetime import UTC, datetime, timedelta
from decimal import Decimal
import json
from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy import event as sqlalchemy_event, select
from project_mai_tai.db.models import BrokerOrder, BrokerOrderEvent, DashboardSnapshot, Fill
from project_mai_tai.broker_adapters.protocols import ExecutionReport
from project_mai_tai.events import TradeIntentEvent, TradeIntentPayload
from project_mai_tai.oms.mirror_retained_hold import row_id
from project_mai_tai.fanout_segment_store import SNAPSHOT_TYPE as SEGMENT_SNAPSHOT
from tests.unit.test_mirrorhold1_retained_hold import event_for, quote, restart, state
from tests.unit.test_mirrorhold1_retained_hold import lane as lane
from tests.unit.test_mirrorhold1_session_duplicate_scan import (
    RECORDED as SESSION_RECORDED, dispatch, prepare, recorded_setup, seed_order,
)
from tests.unit.test_webull_adapter import _ServerException


D28 = json.loads((Path(__file__).parents[1] / 'fixtures/mirrorhold1_wirecap_1008_recorded.json').read_text())


def temporal_event(lane, row):
    event = TradeIntentEvent(event_id=UUID(row['payload']['event_id']),
        source_service=row['payload']['source_service'], produced_at=datetime.fromisoformat(row['created_at']),
        payload=TradeIntentPayload(strategy_code=row['strategy'], broker_account_name=row['account'],
            symbol=row['symbol'], side=row['side'], intent_type=row['intent_type'],
            quantity=Decimal(str(row['quantity'])), reason=row['reason'], metadata=dict(row['payload']['metadata'])))
    md = event.payload.metadata
    quote_at = datetime.fromisoformat(md['webull_shape_market_at_utc'])
    lane[3][0] = max(event.produced_at, quote_at)
    lane[0]._latest_quotes_by_symbol[event.payload.symbol] = {
        'ask': Decimal(md['webull_shape_market_price']), 'received_at': quote_at}
    return event


async def temporal_queue_place(lane, event, *, target=None):
    lane[0]._restore_mirrorhold()
    queued = lane[0]._mirrorhold_prepare_queue(event.payload.symbol, [row_id(event)])
    assert len(queued) == 1
    retry = queued[0][2]
    if target is not None:
        md = target.payload.metadata
        assert retry.payload.quantity == target.payload.quantity
        for key in ('stop_price', 'limit_price', 'rpg_resting_generation', 'webull_mirror_generation_id'):
            assert retry.payload.metadata[key] == md[key]
    assert lane[0]._mirrorhold_claim(retry)
    result = await lane[0].process_trade_intent(retry)
    assert result[-1].payload.status == 'accepted'
    if target is not None:
        placed = lane[1].last['place'].values
        assert Decimal(placed['stop_price']) == Decimal(md['stop_price'])
        assert Decimal(placed['limit_price']) == Decimal(md['limit_price'])
        assert Decimal(placed['qty']) == target.payload.quantity
    await lane[0].process_trade_intent(retry)
    assert lane[1].calls['place'] == 1


@pytest.mark.asyncio
async def test_temporal_aixi_held_back_in_band_places_once(lane):
    event, history = recorded_setup(lane, 'AIXI')
    assert len(history) == 48
    actual_quote_at = lane[0]._latest_quotes_by_symbol['AIXI']['received_at']
    actual_price = lane[0]._latest_quotes_by_symbol['AIXI']['ask']
    lane[3][0] = event.produced_at
    quote(lane, event, '2.10')  # CONTROLLED outside-band prestage, not an observed AIXI quote.
    await lane[0].process_trade_intent(event)
    assert state(lane, event)['phase'] == 'held'
    assert state(lane, event)['reason'] == 'outside_8pct'
    assert lane[1].calls.get('place', 0) == 0
    lane[3][0] = max(actual_quote_at, event.produced_at + timedelta(seconds=1))
    lane[0]._latest_quotes_by_symbol['AIXI'] = {'ask': actual_price, 'received_at': actual_quote_at}
    await temporal_queue_place(lane, event)
    assert state(lane, event)['wire_submissions'] == 1
    assert state(lane, event)['price_aggressive_refusals'] == 0


@pytest.mark.asyncio
async def test_temporal_flye_0938_to_0955_places_once(lane):
    first_id = 'bd074901-077c-4a3b-b551-ead6c82a6f5e'
    event, history = recorded_setup(lane, 'FLYE', intent_id=first_id)
    assert len(history) == 38
    rows = [r for r in SESSION_RECORDED['intents'] if r['symbol'] == 'FLYE']
    assert [r['created_at'][11:16] for r in rows] == ['13:38', '13:42', '13:44', '13:47', '13:55']
    for row in rows[:-1]:
        event = temporal_event(lane, row)
        await lane[0].process_trade_intent(event)
        assert state(lane, event)['phase'] == 'held'
        assert state(lane, event)['reason'] == 'outside_8pct'
        assert lane[1].calls.get('place', 0) == 0
    target = temporal_event(lane, rows[-1])
    # Actual 09:55 changes quote/token, not price authorization; use a fresh durable queue token.
    assert target.payload.quantity == event.payload.quantity
    for key in ('stop_price', 'limit_price', 'rpg_resting_generation', 'webull_mirror_generation_id'):
        assert target.payload.metadata[key] == event.payload.metadata[key]
    await temporal_queue_place(lane, event, target=target)
    assert state(lane, event)['wire_submissions'] == 1
    assert state(lane, event)['price_aggressive_refusals'] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize('outcome,places,refusals', [('accepted', 5, 0), ('price_aggressive', 4, 4)])
async def test_temporal_flye_five_reprices_and_inverted_refusal_cap(lane, outcome, places, refusals):
    rows = D28['intents']
    assert [r['created_at'][11:16] for r in rows] == ['15:20', '15:22', '15:24', '15:32', '15:38']
    event, history = recorded_setup(lane, 'FLYE', recorded=D28, intent_id=rows[0]['id'])
    assert len(history) == 39  # Do not seed any of the four later accepted wires.
    if outcome == 'price_aggressive':
        lane[1].raises['place'] = _ServerException('ORDER_RISK_RULE_PRICE_AGGRESSIVE', 'CONTROLLED inverse', 417)
    for index, row in enumerate(rows):
        event = temporal_event(lane, row)
        result = await lane[0].process_trade_intent(event)
        assert lane[1].calls['place'] == min(index + 1, places)
        if outcome == 'accepted':
            assert result[-1].payload.status == 'accepted'
            assert state(lane, event)['wire_submissions'] == index + 1
            placed = lane[1].last['place'].values
            assert Decimal(placed['stop_price']) == Decimal(row['payload']['metadata']['stop_price'])
            assert Decimal(placed['limit_price']) == Decimal(row['payload']['metadata']['limit_price'])
            assert Decimal(placed['qty']) == Decimal(str(row['quantity']))
            if index < 4:
                client = state(lane, event)['dispatch_client']
                recorded_order = next(r for r in D28['orders'] if r['client_order_id'] == client)
                audit = next(r for r in D28['audits'] if r['order_id'] == recorded_order['id']
                             and r['event_type'] == 'cancelled' and r['event_source'] == 'broker')
                lane[3][0] = datetime.fromisoformat(audit['event_at'])
                with lane[2]() as session:
                    order = session.scalar(select(BrokerOrder).where(BrokerOrder.client_order_id == client))
                    order.status = 'cancelled'
                    # Actual terminal evidence/timing, linked to the CONTROLLED newly placed order.
                    session.add(BrokerOrderEvent(id=UUID(audit['id']), order_id=order.id,
                        event_type='cancelled', event_source='broker', payload=audit['payload'],
                        event_at=lane[3][0]))
                    session.flush()
                    assert lane[0]._mirrorhold_clear_order(session, order)
                    session.commit()
        else:
            assert state(lane, event)['price_aggressive_refusals'] == min(index + 1, 4)
    assert state(lane, event)['wire_submissions'] == places
    assert state(lane, event)['price_aggressive_refusals'] == refusals
    assert state(lane, event)['phase'] == ('accepted' if outcome == 'accepted' else 'capped')


@pytest.mark.asyncio
@pytest.mark.parametrize('symbol,count', [('AIXI', 48), ('FLYE', 38)])
async def test_recorded_queue_restore_dispatch_replay(lane, symbol, count):
    event, history = recorded_setup(lane, symbol)
    assert len(history) == count
    lane[0]._restore_mirrorhold()
    assert state(lane, event)['phase'] == 'held'
    queued = lane[0]._mirrorhold_prepare_queue(symbol, [row_id(event)])
    assert len(queued) == 1
    retry = queued[0][2]
    assert lane[0]._mirrorhold_claim(retry)
    result = await lane[0].process_trade_intent(retry)
    assert result[-1].payload.status == 'accepted'
    assert lane[1].calls['place'] == 1
    assert state(lane, event)['wire_submissions'] == 1
    assert state(lane, event)['price_aggressive_refusals'] == 0


def test_current_pending_queue_restore_dispatch_replay(lane, caplog):
    event = prepare(lane)
    seed_order(lane, event, status='pending')
    lane[0]._restore_mirrorhold()
    assert state(lane, event)['phase'] == 'held'
    assert lane[0]._mirrorhold_prepare_queue('AIXI', [row_id(event)]) == []
    assert dispatch(lane, event) == 'mirrorhold_dispatch_uncertain'
    expected = '[OMS-MIRRORHOLD1] account=live:orb symbol=AIXI phase=refused reason=mirrorhold_dispatch_uncertain orders_considered=1'
    assert sum(r.levelname == 'WARNING' and r.message == expected for r in caplog.records) == 3
    assert lane[1].calls.get('place', 0) == 0


def test_restore_never_materializes_unrelated_old_terminal(lane):
    event = prepare(lane)
    old_id = seed_order(lane, event, status='rejected', submitted_at=datetime(2026, 8, 25, 15, tzinfo=UTC))
    loaded = []

    def load(session, instance):
        if isinstance(instance, BrokerOrder):
            loaded.append(instance.id)

    sqlalchemy_event.listen(lane[2], 'loaded_as_persistent', load)
    try:
        lane[0]._restore_mirrorhold()
    finally:
        sqlalchemy_event.remove(lane[2], 'loaded_as_persistent', load)
    assert old_id not in loaded
    assert state(lane, event)['phase'] == 'held'


def test_queue_restore_same_slot_fill_precedes_current_pending(lane):
    event = prepare(lane)
    seed_order(lane, event, status='pending')
    seed_order(lane, event, status='filled', submitted_at=datetime(2026, 8, 25, 15, tzinfo=UTC),
               metadata={'fanout_segment_id': event.payload.metadata['fanout_segment_id'],
                         'fanout_slot_id': event.payload.metadata['fanout_slot_id']})
    lane[0]._restore_mirrorhold()
    assert state(lane, event)['phase'] == 'retired'
    assert state(lane, event)['reason'] == 'mirror_filled'
    assert lane[0]._mirrorhold_prepare_queue('AIXI', [row_id(event)]) == []


def test_queue_restore_invalid_segment_metadata_warns_before_scan(lane, caplog):
    event = prepare(lane)
    seed_order(lane, event)
    with lane[2]() as session:
        snapshot = session.scalar(select(DashboardSnapshot).where(DashboardSnapshot.snapshot_type == SEGMENT_SNAPSHOT))
        snapshot.payload = {**snapshot.payload, 'active': None}
        session.commit()
    lane[0]._restore_mirrorhold()
    assert lane[0]._mirrorhold_prepare_queue('AIXI', [row_id(event)]) == []
    expected = '[OMS-MIRRORHOLD1] account=live:orb symbol=AIXI phase=refused reason=mirrorhold_segment_identity_unproven orders_considered=0'
    assert sum(r.levelname == 'WARNING' and r.message == expected for r in caplog.records) == 2


@pytest.mark.parametrize('slot', ['same', 'different', 'absent'])
@pytest.mark.parametrize('proof', ['filled', 'fill_row'])
def test_queue_restore_exact_slot_fill_control(lane, slot, proof):
    event = prepare(lane)
    md = {'fanout_segment_id': event.payload.metadata['fanout_segment_id']}
    if slot != 'absent':
        md['fanout_slot_id'] = event.payload.metadata['fanout_slot_id'] if slot == 'same' else 'CONTROLLED-other'
    order_id = seed_order(lane, event, status='filled' if proof == 'filled' else 'cancelled',
                          submitted_at=datetime(2026, 8, 25, 15, tzinfo=UTC), metadata=md)
    if proof == 'fill_row':
        with lane[2]() as session:
            order = session.get(BrokerOrder, order_id)
            session.add(Fill(order_id=order.id, broker_account_id=order.broker_account_id,
                strategy_id=order.strategy_id, symbol='AIXI', side='buy', quantity=Decimal('1'),
                price=Decimal('2.27'), filled_at=datetime(2026, 8, 25, 15, tzinfo=UTC)))
            session.commit()
    lane[0]._restore_mirrorhold()
    assert state(lane, event)['phase'] == ('retired' if slot == 'same' else 'held')
    queued = lane[0]._mirrorhold_prepare_queue('AIXI', [row_id(event)])
    assert len(queued) == (1 if slot == 'different' and proof == 'filled' else 0)
    if slot != 'same':
        assert state(lane, event)['phase'] != 'retired'


@pytest.mark.parametrize('account', ['live:orb', 'live:schwab_1m_v2'])
@pytest.mark.parametrize('now,old,current', [
    ('2026-10-08T13:35:05+00:00', '2026-10-08T07:59:59+00:00', '2026-10-08T08:00:00+00:00'),
    ('2026-10-08T07:59:59+00:00', '2026-10-07T07:59:59+00:00', '2026-10-07T08:00:00+00:00'),
])
def test_queue_restore_session_anchor_control(lane, monkeypatch, caplog, account, now, old, current):
    event = prepare(lane, account=account, now=now)
    # Scan-clock control only: real PM eh_resting bypasses MIRRORHOLD1.
    monkeypatch.setattr(lane[0], '_nfq_window_open', lambda event: True)
    seed_order(lane, event, status='rejected', submitted_at=datetime.fromisoformat(old))
    seed_order(lane, event, status='pending', submitted_at=datetime.fromisoformat(current))
    lane[0]._restore_mirrorhold()
    assert lane[0]._mirrorhold_prepare_queue('AIXI', [row_id(event)]) == []
    assert state(lane, event)['phase'] == 'held'
    assert sum(r.levelname == 'WARNING' and r.message.endswith('orders_considered=1') for r in caplog.records) == 2


@pytest.mark.parametrize('condition', ['working_gtc', 'null_timestamp', 'null_segment', 'zero_segment'])
def test_queue_restore_unknowns_and_prior_working_stay_conservative(lane, condition, caplog):
    event = prepare(lane)
    kwargs = {'submitted_at': datetime(2026, 8, 25, 15, tzinfo=UTC), 'status': 'rejected'}
    if condition == 'working_gtc':
        kwargs.update(status='accepted', tif='gtc')
    elif condition == 'null_timestamp':
        kwargs['submitted_at'] = None
    else:
        kwargs['metadata'] = {'fanout_segment_id': None if condition == 'null_segment' else 0}
    seed_order(lane, event, **kwargs)
    lane[0]._restore_mirrorhold()
    assert lane[0]._mirrorhold_prepare_queue('AIXI', [row_id(event)]) == []
    assert state(lane, event)['phase'] == 'held'
    assert sum(r.levelname == 'WARNING' and r.message.endswith('orders_considered=1') for r in caplog.records) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize('legacy', [False, True])
async def test_recorded_flye_four_accepted_reprices_do_not_cap_next(lane, legacy):
    assert D28['raw_capture']['complete'] is True
    assert D28['replay_projection']['counts'] == {
        'intents': 5, 'orders': 43, 'historical_intents': 43, 'audits': 52, 'fills': 1}
    assert len(D28['orders']) == 43 and len(D28['audits']) == 52 and len(D28['fills']) == 1
    assert all(row['strategy'] is not None for row in D28['historical_intents'])
    reprices = [o for o in D28['orders'] if o['submitted_at'] >= '2026-10-08T15:15:00Z']
    assert [o['submitted_at'][11:16] for o in reprices] == ['15:20', '15:22', '15:24', '15:32']
    target = next(i for i in D28['intents'] if i['created_at'].startswith('2026-10-08T15:38:03'))
    assert target['payload']['refusal_code'] == 'mirrorhold_actual_submission_cap'
    assert target['quantity'] == 135
    reprice_ids = {o['id'] for o in reprices}
    assert len({a['order_id'] for a in D28['audits'] if a['order_id'] in reprice_ids
                and a['event_type'] == 'accepted' and a['event_source'] == 'broker'}) == 4
    event, history = recorded_setup(lane, 'FLYE', recorded=D28, intent_id=target['id'])
    assert len(history) == 43
    assert D28['fills'][0]['filled_at'] == '2026-10-08T14:00:40.195+00:00'
    assert state(lane, event)['wire_submissions'] == 4
    assert state(lane, event)['price_aggressive_refusals'] == 0
    if legacy:
        with lane[2]() as session:
            owner = session.get(DashboardSnapshot, row_id(event))
            payload = dict(owner.payload)
            payload.pop('price_aggressive_clients')
            payload.pop('price_aggressive_refusals')
            owner.payload = {**payload, 'phase': 'capped', 'reason': 'actual_submission_cap'}
            session.commit()
        lane[0]._restore_mirrorhold()
        assert state(lane, event)['phase'] == 'blocked'
        assert state(lane, event)['price_aggressive_refusals'] == 0
    assert lane[0]._mirrorhold_admit(event)
    result = await lane[0].process_trade_intent(event)
    assert result[-1].payload.status == 'accepted'
    assert lane[1].calls['place'] == 1
    assert state(lane, event)['wire_submissions'] == 5
    assert state(lane, event)['price_aggressive_refusals'] == 0


def test_legacy_cap_without_retained_wire_proof_is_not_released(lane):
    event = prepare(lane)
    with lane[2]() as session:
        owner = session.get(DashboardSnapshot, row_id(event))
        payload = dict(owner.payload)
        payload.pop('price_aggressive_clients')
        payload.pop('price_aggressive_refusals')
        owner.payload = {**payload, 'wire_clients': ['CONTROLLED-unproven'], 'wire_submissions': 1,
                         'phase': 'capped', 'reason': 'actual_submission_cap'}
        session.commit()
    lane[0]._restore_mirrorhold()
    assert state(lane, event)['phase'] == 'uncertain'
    assert not lane[0]._mirrorhold_admit(event)
    assert lane[0]._mirrorhold_prepare_queue('AIXI', [row_id(event)]) == []
    assert lane[1].calls.get('place', 0) == 0


@pytest.mark.parametrize('phase', ['retired', 'filled'])
def test_budget_upgrade_cannot_reopen_terminal_owner(lane, phase):
    event = prepare(lane)
    with lane[2]() as session:
        owner = session.get(DashboardSnapshot, row_id(event))
        payload = dict(owner.payload)
        payload.pop('price_aggressive_clients')
        payload.pop('price_aggressive_refusals')
        owner.payload = {**payload, 'wire_clients': ['CONTROLLED-unproven'], 'wire_submissions': 1,
                         'phase': phase, 'reason': 'CONTROLLED-terminal'}
        session.commit()
    assert not lane[0]._mirrorhold_admit(event)
    assert state(lane, event)['phase'] == phase
    assert state(lane, event)['dispatch_unresolved'] is False


@pytest.mark.asyncio
async def test_legacy_price_aggressive_cap_is_rebuilt_and_survives_restart(lane):
    service, client, factory, clock = lane
    client.raises['place'] = _ServerException('ORDER_RISK_RULE_PRICE_AGGRESSIVE', 'CONTROLLED', 417)
    for number in range(4):
        event = event_for(lane, stop=str(Decimal('5.35') - Decimal(number) / 100))
        quote(lane, event, '5.1')
        await service.process_trade_intent(event)
    with factory() as session:
        owner = session.get(DashboardSnapshot, row_id(event))
        payload = dict(owner.payload)
        payload.pop('price_aggressive_clients')
        payload.pop('price_aggressive_refusals')
        owner.payload = payload
        session.commit()
    lane = restart(lane)
    assert state(lane, event)['phase'] == 'capped'
    assert state(lane, event)['price_aggressive_refusals'] == 4
    assert len(state(lane, event)['price_aggressive_clients']) == 4
    next_event = event_for(lane, stop='5.2')
    quote(lane, next_event, '5.1')
    await lane[0].process_trade_intent(next_event)
    assert client.calls['place'] == 4


@pytest.mark.asyncio
async def test_price_aggressive_reports_are_idempotent_and_legacy_report_preserves_history(lane):
    service, client, factory, clock = lane
    client.raises['place'] = _ServerException('ORDER_RISK_RULE_PRICE_AGGRESSIVE', 'CONTROLLED', 417)
    for number in range(2):
        event = event_for(lane, stop=str(Decimal('5.35') - Decimal(number) / 100))
        quote(lane, event, '5.1')
        await service.process_trade_intent(event)
    data = state(lane, event)
    report = ExecutionReport(event_type='rejected', client_order_id=data['dispatch_client'],
        symbol=event.payload.symbol, side='buy', intent_type='open', quantity=event.payload.quantity,
        origin='broker', metadata={**event.payload.metadata,
            'webull_error_code': 'ORDER_RISK_RULE_PRICE_AGGRESSIVE',
            'webull_wire_submitted_at_utc': clock[0].isoformat()})
    with factory() as session:
        owner = session.get(DashboardSnapshot, row_id(event))
        payload = dict(owner.payload)
        payload.pop('price_aggressive_clients')
        payload.pop('price_aggressive_refusals')
        owner.payload = payload
        session.commit()
    for _ in range(2):
        with factory() as session:
            service._mirrorhold_reports(session, event, [report])
            session.commit()
    assert state(lane, event)['wire_submissions'] == 2
    assert state(lane, event)['price_aggressive_refusals'] == 2
    assert client.calls['place'] == 2
