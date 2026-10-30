"""Original row-copy characterization; not live/native or full boot evidence."""
import os
import json
from copy import deepcopy
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy import select

from project_mai_tai.db.models import AccountPosition, BrokerAccount, BrokerOrder, BrokerOrderEvent, Fill, OmsManagedPosition, SystemIncident, TradeIntent, VirtualPosition

from project_mai_tai.oms import wbexit2
from tests.unit.wbexit_stale1_episode_port import (
    WB2_ID, WB3_ID, seed_episode, snapshot, hydrate,
)


@pytest.fixture
def retained_episode(tmp_path):
    if not os.environ.get('WBSTALE1_EPISODE_SOURCE'):
        pytest.skip('requires the explicitly reviewed private C1 episode export')
    case = seed_episode(tmp_path)
    try:
        yield case
    finally:
        case.engine.dispose()


@pytest.mark.asyncio
async def test_original24_keeps_wb2_hold_despite_real_durable_child_copy(retained_episode, monkeypatch, record_property):
    case = retained_episode
    original = json.loads((Path(__file__).parents[1] / 'fixtures/wbexit_stale1_original24_finish.json').read_text())
    assert original['head'] == '24d80deb1301cd3bf4c68f4efda98cd368a6716c'
    namespace = dict(wbexit2.__dict__)
    namespace['_now'] = lambda: case.now
    exec(compile(original['finish_call'], '<pinned-original24-finish_call>', 'exec'), namespace)
    monkeypatch.setattr(wbexit2, 'finish_call', namespace['finish_call'])
    monkeypatch.setattr(wbexit2, '_now', lambda: case.now)
    before = snapshot(case.factory)
    assert before['system_incidents'][str(WB2_ID)]['payload']['phase'] == 'HOLD'
    assert before['system_incidents'][str(WB3_ID)]['payload']['phase'] == 'RESOLVED'
    assert before['system_incidents'][str(WB3_ID)]['status'] == 'closed'
    assert len(before['fills']) == 2 and len(before['broker_orders']) == 2
    child = next(row for row in before['fills'].values() if row['side'] == 'sell')
    assert child['quantity'] == Decimal(69) and child['price'] == Decimal('4.54')
    await hydrate(case)
    assert not case.service._wbexit3_pending
    scan_started = datetime.now(UTC)
    await wbexit2.refresh_pending(case.service)
    scan_ended = datetime.now(UTC)
    after = snapshot(case.factory)
    expected = deepcopy(before)
    expected_owner = expected['system_incidents'][str(WB2_ID)]
    actual_clock = after['system_incidents'][str(WB2_ID)]['payload']['updated_at']
    assert scan_started <= datetime.fromisoformat(actual_clock) <= scan_ended
    expected_owner['payload'].update(
        generation=expected_owner['payload']['generation'] + 1,
        updated_at=actual_clock,
        hold_reason='named_close_owner_pending_next_permitted_observation',
        scan_exception=True, original_attempt_outcome='FAIL')
    expected_owner['severity'] = 'critical'
    assert after == expected
    state = after['system_incidents'][str(WB2_ID)]['payload']
    assert state['phase'] == 'HOLD' and Decimal(state['remaining']) == 69
    assert case.service._wbexit2_pending[('live:orb', 'TNMG')]['owner_id'] == str(WB2_ID)
    assert case.attempts == []
    record_property('OLD24_original_copy', 'HOLD_remaining69_exact_expected_checkpoint_only_WB3_economic_rows_unchanged_providerRedis0')


@pytest.mark.asyncio
@pytest.mark.parametrize('session_clock', ['captured', 'premarket', 'after_hours'])
async def test_durable_real_child_copy_resolves_first_existing_scan(retained_episode, monkeypatch, record_property, session_clock):
    case = retained_episode
    if session_clock == 'premarket': case.now = datetime(2026, 10, 31, 8, tzinfo=UTC)
    elif session_clock == 'after_hours': case.now = datetime(2026, 10, 30, 23, tzinfo=UTC)
    monkeypatch.setattr(wbexit2, '_now', lambda: case.now)
    before = snapshot(case.factory)
    await hydrate(case)
    await wbexit2.refresh_pending(case.service)
    after = snapshot(case.factory)
    assert {table for table in before if before[table] != after[table]} == {'system_incidents'}
    assert after['system_incidents'][str(WB3_ID)] == before['system_incidents'][str(WB3_ID)]
    old, new = before['system_incidents'][str(WB2_ID)], after['system_incidents'][str(WB2_ID)]
    assert {key for key in old if old[key] != new[key]} == {'payload', 'status', 'closed_at'}
    assert new['status'] == 'closed' and new['closed_at'] is not None
    state = new['payload']
    assert state['phase'] == 'RESOLVED' and state['remaining'] == '0'
    assert state['generation'] == old['payload']['generation'] + 1
    assert {key for key in set(state) | set(old['payload']) if state.get(key) != old['payload'].get(key)} == {
        'remaining', 'child_fill_highwater', 'generation', 'phase', 'resolved_at',
        'updated_at', 'durable_accounting_retirement'}
    assert sorted(state['child_fill_highwater'].values()) == ['0', '69']
    assert state['original_attempt_outcome'] == 'FAIL'
    assert state['originals'] == old['payload']['originals']
    assert state['cancel_bases'] == old['payload']['cancel_bases']
    receipt = state['durable_accounting_retirement']
    assert receipt['wire_authority'] is receipt['new_native_observation'] is False
    assert receipt['fill_ids'] == ['45d583bc-410b-468a-a308-80c22fffcf5e']
    assert ('live:orb', 'TNMG') not in case.service._wbexit2_pending
    assert not case.service._wbexit3_pending and case.attempts == []
    await wbexit2.refresh_pending(case.service)
    assert snapshot(case.factory) == after
    record_property('real_episode_copy', 'NEW_first_existing_scan_RESOLVED_no_wire_WB3_economic_rows_unchanged')
    record_property('clock_scope', session_clock + '_CONTROLLED_not_native13:36_latency')


ADVERSE = ('missing_child_fill', 'partial_child_fill', 'foreign_child_fill', 'missing_buy_fill',
           'wrong_child_coid', 'wrong_child_parent', 'missing_position', 'positive_position',
           'preclose_position', 'future_position', 'missing_VP', 'positive_VP',
           'managed_open', 'managed_positive', 'missing_base_binding', 'foreign_native_account',
           'active_invocation', 'prior_software_without_fill', 'duplicate_child_execution')


@pytest.mark.asyncio
@pytest.mark.parametrize('case_name', ADVERSE)
async def test_durable_unknown_or_held_copy_retains_responsibility(retained_episode, monkeypatch, case_name):
    case = retained_episode
    monkeypatch.setattr(wbexit2, '_now', lambda: case.now)
    with case.factory.begin() as db:
        owner = db.get(SystemIncident, WB2_ID)
        payload = deepcopy(owner.payload)
        child_fill = db.scalar(select(Fill).where(Fill.side == 'sell'))
        child_order = db.get(BrokerOrder, child_fill.order_id)
        position = db.scalar(select(AccountPosition))
        vp = db.scalar(select(VirtualPosition))
        managed = db.scalar(select(OmsManagedPosition))
        if case_name == 'missing_child_fill': db.delete(child_fill)
        elif case_name == 'partial_child_fill': child_fill.quantity = Decimal(68)
        elif case_name == 'foreign_child_fill': child_fill.strategy_id = WB3_ID
        elif case_name == 'missing_buy_fill': db.delete(db.scalar(select(Fill).where(Fill.side == 'buy')))
        elif case_name == 'wrong_child_coid': child_order.client_order_id = 'CONTROLLED-foreign'
        elif case_name == 'wrong_child_parent': child_order.intent_id = UUID('47cf706e-592f-443c-8f37-a1ede53e8148')
        elif case_name == 'missing_position': db.delete(position)
        elif case_name == 'positive_position': position.quantity = Decimal(1)
        elif case_name == 'preclose_position': position.source_updated_at = datetime(2026, 10, 30, 13, 34, tzinfo=UTC)
        elif case_name == 'future_position': position.source_updated_at = datetime(2026, 10, 30, 16, tzinfo=UTC)
        elif case_name == 'missing_VP': db.delete(vp)
        elif case_name == 'positive_VP': vp.quantity = Decimal(1)
        elif case_name == 'managed_open': managed.status = 'open'
        elif case_name == 'managed_positive': managed.current_quantity = Decimal(1)
        elif case_name == 'missing_base_binding':
            protection = db.get(TradeIntent, UUID('47cf706e-592f-443c-8f37-a1ede53e8148'))
            changed = deepcopy(protection.payload)
            changed['metadata'].pop('webull_protect_base_client_order_id')
            protection.payload = changed
        elif case_name == 'foreign_native_account': payload['native_account_id'] = 'CONTROLLED-foreign-native'
        elif case_name == 'active_invocation': payload['invocation'] = 'CONTROLLED-foreign-live'
        elif case_name == 'prior_software_without_fill': payload['prior_software_filled'] = '1'
        elif case_name == 'duplicate_child_execution':
            db.add(Fill(id=UUID('00000000-0000-0000-0000-000000000069'), order_id=child_order.id,
                strategy_id=child_fill.strategy_id, broker_account_id=child_fill.broker_account_id,
                symbol=child_fill.symbol, side='sell', quantity=Decimal(69), price=Decimal('4.54'),
                broker_fill_id=child_order.broker_order_id + ':duplicate', filled_at=child_fill.filled_at,
                payload=deepcopy(child_fill.payload)))
        owner.payload = payload
    before = snapshot(case.factory)
    await hydrate(case)
    await wbexit2.refresh_pending(case.service)
    after = snapshot(case.factory)
    assert all(after[table] == before[table] for table in before if table != 'system_incidents')
    assert after['system_incidents'][str(WB3_ID)] == before['system_incidents'][str(WB3_ID)]
    state = after['system_incidents'][str(WB2_ID)]['payload']
    assert state['phase'] == 'HOLD' and state['remaining'] == '69'
    assert state['child_fill_highwater'] == before['system_incidents'][str(WB2_ID)]['payload']['child_fill_highwater']
    assert state['invocation'] == before['system_incidents'][str(WB2_ID)]['payload']['invocation']
    assert state['original_attempt_outcome'] == 'FAIL'
    assert 'durable_accounting_retirement' not in state
    assert case.service._wbexit2_pending[('live:orb', 'TNMG')]['owner_id'] == str(WB2_ID)
    assert case.attempts == []


@pytest.mark.asyncio
async def test_oco_fetch_exception_receipt_preserves_failure_and_cancellation(caplog):
    import asyncio
    import logging
    from types import SimpleNamespace
    from project_mai_tai.oms.service import OmsRiskService, _EXIT_FETCH_FAILED
    from project_mai_tai.settings import Settings
    service = OmsRiskService.__new__(OmsRiskService)
    service.settings = Settings(_env_file=None, oms_record_native_oco_exit_fills_enabled=True)
    service.logger = logging.getLogger('CONTROLLED-fetch-exception')
    attempted = []
    async def refused(*args, **kwargs):
        attempted.append((args, kwargs))
        raise RuntimeError('CONTROLLED-retained-read-refusal')
    service.broker_adapter = SimpleNamespace(fetch_oco_exit_fill=refused)
    with caplog.at_level(logging.WARNING, logger=service.logger.name):
        result = await service._fetch_oco_exit_detail('live:orb', 'TNMG', 'CONTROLLED-base')
    assert result is _EXIT_FETCH_FAILED and len(attempted) == 1
    assert 'exception_class=RuntimeError' in caplog.text
    assert 'exception_message=CONTROLLED-retained-read-refusal' in caplog.text
    async def cancelled(*args, **kwargs): raise asyncio.CancelledError()
    service.broker_adapter = SimpleNamespace(fetch_oco_exit_fill=cancelled)
    with pytest.raises(asyncio.CancelledError):
        await service._fetch_oco_exit_detail('live:orb', 'TNMG', 'CONTROLLED-base')


@pytest.mark.asyncio
async def test_nonrecord_finish_never_enters_accounting_only_path(retained_episode, monkeypatch):
    from project_mai_tai.oms import wbexit2_durable
    case = retained_episode
    monkeypatch.setattr(wbexit2, '_now', lambda: case.now)
    def forbidden(*args): raise AssertionError('accounting-only helper entered for nonrecord call')
    monkeypatch.setattr(wbexit2_durable, 'reconcile_closed_originals', forbidden)
    await hydrate(case)
    state = deepcopy(case.service._wbexit2_pending[('live:orb', 'TNMG')])
    call = wbexit2._RecordOnly('live:orb', 'TNMG', state['identity'], state, record_only=False)
    await wbexit2.finish_call(case.service, call)
    assert call.wb2_state['phase'] == 'HOLD' and call.wb2_state['remaining'] == '69'
    assert case.attempts == []


@pytest.mark.asyncio
async def test_foreign_Schwab_binding_cannot_enter_Webull_boot_or_accounting(retained_episode):
    case = retained_episode
    with case.factory.begin() as db:
        db.scalar(select(BrokerAccount)).provider = 'schwab'
    before = snapshot(case.factory)
    with pytest.raises(ValueError, match='wbexit2_actual_entry_binding_unknown'):
        await wbexit2.hydrate_at_boot(case.service)
    assert case.service._wbexit2_boot_complete is False
    assert snapshot(case.factory) == before and case.attempts == []


@pytest.mark.parametrize('operand,allowed', [
    (0, True), (69, True), (Decimal('69'), True),
    (True, False), (69.0, False), ('69', False), (None, False),
    (-1, False), (Decimal('0.5'), False), (Decimal('NaN'), False),
    (Decimal('Infinity'), False),
], ids=['ORM_int_zero', 'ORM_int_original', 'Numeric_Decimal', 'bool', 'float',
        'string', 'missing', 'negative', 'fractional', 'NaN', 'Infinity'])
def test_declared_ORM_quantity_types_and_strict_refusals(operand, allowed):
    from sqlalchemy import Integer, Numeric
    from project_mai_tai.oms.wbexit2_durable import _quantity
    assert isinstance(OmsManagedPosition.__table__.c.current_quantity.type, Integer)
    assert isinstance(OmsManagedPosition.__table__.c.original_quantity.type, Integer)
    assert isinstance(Fill.__table__.c.quantity.type, Numeric)
    if allowed:
        assert _quantity(operand) == Decimal(69 if operand else 0)
    else:
        with pytest.raises(ValueError): _quantity(operand)


@pytest.mark.asyncio
@pytest.mark.parametrize('accounting_case', ['prior_software', 'OUR_sell', 'double_count_refused'])
async def test_controlled_full_quantity_conservation_including_software(retained_episode, monkeypatch, accounting_case, record_property):
    """CONTROLLED split 60native+9software; original TNMG replay stays separate."""
    case = retained_episode
    monkeypatch.setattr(wbexit2, '_now', lambda: case.now)
    controlled_at = datetime(2026, 10, 30, 13, 34, 12, tzinfo=UTC)
    with case.factory.begin() as db:
        owner = db.get(SystemIncident, WB2_ID)
        state = deepcopy(owner.payload)
        entry = db.get(BrokerOrder, UUID(state['identity']['entry_id']))
        native_fill = db.scalar(select(Fill).where(Fill.side == 'sell'))
        native_order = db.get(BrokerOrder, native_fill.order_id)
        native_fill.quantity = native_order.quantity = Decimal(60)
        native_fill.broker_fill_id = native_order.broker_order_id + ':60'
        protection = db.get(TradeIntent, UUID('47cf706e-592f-443c-8f37-a1ede53e8148'))
        payload = deepcopy(protection.payload)
        proof = json.loads(payload['metadata']['wbattach1_terminal_bookkeeping'])
        next(c for c in proof['proof']['children'] if c['status'] == 'filled')['filled_quantity'] = '60'
        payload['metadata']['wbattach1_terminal_bookkeeping'] = json.dumps(proof)
        protection.payload = payload
        soft_intent_id = UUID('00000000-0000-0000-0000-000000000091')
        soft_order_id = UUID('00000000-0000-0000-0000-000000000092')
        coid = 'CONTROLLED-software-own-nine'
        metadata = {'entry_order_id': str(entry.id), 'oms_managed_row_id': state['identity']['managed_id']}
        db.add(TradeIntent(id=soft_intent_id, strategy_id=entry.strategy_id,
            broker_account_id=entry.broker_account_id, symbol='TNMG', side='sell',
            intent_type='close', quantity=Decimal(9), reason=state['reason'], status='filled',
            payload={'metadata': metadata}, created_at=controlled_at, updated_at=controlled_at))
        db.add(BrokerOrder(id=soft_order_id, intent_id=soft_intent_id, strategy_id=entry.strategy_id,
            broker_account_id=entry.broker_account_id, symbol='TNMG', side='sell', order_type='market',
            time_in_force='day', quantity=Decimal(9), status='filled', client_order_id=coid,
            broker_order_id='CONTROLLED-nine-native', payload=metadata,
            submitted_at=controlled_at, updated_at=controlled_at))
        db.add(Fill(id=UUID('00000000-0000-0000-0000-000000000093'), order_id=soft_order_id,
            strategy_id=entry.strategy_id, broker_account_id=entry.broker_account_id,
            symbol='TNMG', side='sell', quantity=Decimal(9), price=Decimal('4.54'),
            broker_fill_id='CONTROLLED-nine-native:9', filled_at=controlled_at, payload={}))
        db.add(BrokerOrderEvent(order_id=soft_order_id, event_type='filled', event_source='broker',
                                event_at=controlled_at, payload={}))
        if accounting_case == 'prior_software':
            state['prior_software_filled'] = '9'
            state['remaining'] = '60'
        else:
            state['sell'] = {'coid': coid, 'quantity': '9', 'filled_highwater': '9', 'ack': 'filled'}
            if accounting_case == 'double_count_refused': state['prior_software_filled'] = '9'
        owner.payload = state
    before = snapshot(case.factory)
    await hydrate(case)
    await wbexit2.refresh_pending(case.service)
    after = snapshot(case.factory)
    assert all(after[t] == before[t] for t in before if t != 'system_incidents')
    assert after['system_incidents'][str(WB3_ID)] == before['system_incidents'][str(WB3_ID)]
    incident = after['system_incidents'][str(WB2_ID)]
    state = incident['payload']
    if accounting_case == 'double_count_refused':
        assert incident['status'] == 'open' and state['phase'] != 'RESOLVED'
        assert Decimal(state['remaining']) > 0 and 'durable_accounting_retirement' not in state
        assert ('live:orb', 'TNMG') in case.service._wbexit2_pending
    else:
        assert incident['status'] == 'closed' and state['phase'] == 'RESOLVED' and state['remaining'] == '0'
        assert sorted(state['child_fill_highwater'].values()) == ['0', '60']
        assert state['durable_accounting_retirement']['wire_authority'] is False
        assert ('live:orb', 'TNMG') not in case.service._wbexit2_pending
    assert case.attempts == [] and state['original_attempt_outcome'] == 'FAIL'
    record_property('scope', 'CONTROLLED60native+9software_conservation_not_originalTNMGeconomics')
