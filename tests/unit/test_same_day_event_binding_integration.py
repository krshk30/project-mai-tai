"""A/B selected-cohort integration, not historical full-book qualification.

Original seven order/event/token links stay literal. Parent rows, missing BO
quantity/native-account corroboration, no other rows and native books remain
explicit CONTROLLED operands. No production DML/native read is performed.
"""
from copy import deepcopy
from datetime import timedelta
from decimal import Decimal
import json
from uuid import UUID,uuid4

import pytest

from project_mai_tai.brokerdecides1_sync import local_evidence
from project_mai_tai.cancelack1 import _closed_with_replacements
from project_mai_tai.db.models import BrokerAccount,BrokerOrder,BrokerOrderEvent,DashboardSnapshot,Fill,TradeIntent
from tests.unit.test_brokerdecides1_local_census import census as census
from tests.unit.test_brokerdecides1_event_binding import ACCOUNTS,NOW,selected_census,sell_after_census

POSITIVE_ORDER=UUID('b9225b77-9c8f-4519-9be4-4b868496d3fc')
POSITIVE_EVENT=UUID('67733a2f-4a9c-4e28-b753-8920fbb734f6')
POSITIVE_TOKEN='05627c3e-20fc-413b-979d-a6930ebb96e7'


def selected(census,monkeypatch,*,typed=True):
    session,accounts,strategy=census
    links,_,_,_=selected_census(census,monkeypatch,binding=False)
    order=session.get(BrokerOrder,POSITIVE_ORDER)
    event=session.get(BrokerOrderEvent,POSITIVE_EVENT)
    parent=session.get(TradeIntent,order.intent_id)
    account=session.get(BrokerAccount,order.broker_account_id)
    order_at,parent_at=order.updated_at,parent.updated_at
    if typed:
        proof=json.loads(event.payload['metadata']['_webull_terminal_proof'])
        # CONTROLLED typed DB corroboration FROM the literal proof's inputs;
        # not a second independent original account/quantity receipt.
        account.external_account_id=proof['account_id']
        order.quantity=parent.quantity=Decimal(proof['body']['items'][0]['qty'])
        session.flush()
        order.updated_at,parent.updated_at=order_at,parent_at
        session.flush()
    return session,links,order,event,parent,account,order_at,parent_at


def answer(session,enabled):
    return local_evidence(session,ACCOUNTS,prior_session_attempts_final=True,
                          same_day_event_binding=enabled)


def test_combined_off_preserves_original_seven_missing_token_holds(census,monkeypatch):
    session,links,order,event,parent,account,_,_=selected(census,monkeypatch)
    inflight,fills,terminal=answer(session,False)
    assert inflight==frozenset({'NCPL','PCLA'}) and not fills and not terminal


def test_combined_missing_independent_typed_native_account_keeps_pcla_unknown(census,monkeypatch):
    session,links,order,event,parent,account,_,_=selected(census,monkeypatch,typed=False)
    assert account.external_account_id is None
    inflight,fills,terminal=answer(session,True)
    assert inflight==frozenset({'PCLA'}) and not fills
    assert len(terminal)==2 and all(symbol=='NCPL' for symbol,_ in terminal)


def test_combined_exact_seven_recover_only_two_ncpl_and_one_proven_pcla(census,monkeypatch,record_property):
    session,links,order,event,parent,account,_,_=selected(census,monkeypatch)
    before={row.id:deepcopy(row.payload) for row in session.query(BrokerOrder).all()}
    raw_events={row.id:deepcopy(row.payload) for row in session.query(BrokerOrderEvent).all()}
    inflight,fills,terminal=answer(session,True)
    assert len(links)==7 and len(terminal)==3 and ('PCLA',POSITIVE_TOKEN) in terminal
    assert sum(symbol=='NCPL' for symbol,_ in terminal)==2
    assert inflight==frozenset({'PCLA'}) and not fills
    assert sell_after_census('NCPL',inflight)==('idle',0)
    assert sell_after_census('PCLA',inflight)[0]=='bound'  # Four unproven original sends remain.
    assert {row.id:row.payload for row in session.query(BrokerOrder).all()}==before
    assert {row.id:row.payload for row in session.query(BrokerOrderEvent).all()}==raw_events
    record_property('acceptance_scope','Exact7 links; CONTROLLED missing typed DB operands; only3 positive terminals; NOT all7/wholeowner/nativebookPASS')


def test_cancelack_default_false_unchanged_and_exact_flagged_witness_only(census,monkeypatch):
    session,_,order,event,parent,account,_,_=selected(census,monkeypatch)
    assert event.payload['metadata']['cancel_outcome']=='requested'
    assert not _closed_with_replacements(session,order,NOW)
    assert _closed_with_replacements(session,order,NOW,same_day_event_binding_enabled=True)


@pytest.mark.parametrize('fault',['native-account','quantity','local-fill','open-replacement',
    'later-working','later-audit-account','tied-terminal'])
def test_combined_partial_replacement_or_conflicting_native_evidence_stays_held(census,monkeypatch,fault):
    session,_,order,event,parent,account,order_at,parent_at=selected(census,monkeypatch)
    if fault=='native-account':
        account.external_account_id='CONTROLLED_FOREIGN'
    elif fault=='quantity':
        order.quantity=parent.quantity=Decimal('36')
    elif fault=='local-fill':
        session.add(Fill(order_id=order.id,strategy_id=order.strategy_id,broker_account_id=order.broker_account_id,
            broker_fill_id='CONTROLLED_PARTIAL',symbol='PCLA',side='buy',quantity=Decimal('.5'),price=Decimal(8),
            filled_at=event.event_at-timedelta(seconds=1)))
    elif fault=='open-replacement':
        session.add(DashboardSnapshot(snapshot_type='atr_reprice_handoff',payload={'phase':'cancel_pending',
            'old':{'client_order_id':order.client_order_id,'symbol':'PCLA','side':'buy',
                'broker_account_name':account.name,'strategy_code':'schwab_1m_v2','quantity':'35'},
            'replacement':{'client_order_id':'CONTROLLED_OPEN','symbol':'PCLA','side':'buy',
                'broker_account_name':account.name,'strategy_code':'schwab_1m_v2','quantity':'35'}}))
    else:
        payload=deepcopy(event.payload)
        if fault=='tied-terminal':
            at,kind=event.event_at,'cancelled'
        else:
            at,kind=event.event_at+timedelta(seconds=1),'accepted'
            payload['metadata'].pop('_webull_terminal_proof')
            if fault=='later-audit-account':
                kind='rejected'
                payload['internal']='CANCEL_VERIFY_RESUBMIT'
                payload['broker_order_id']=None
                payload['metadata'].update(webull_error_code='ORDER_CAN_NOT_BE_CANCEL',
                    cancel_outcome='confirmed_after_accepted_request',atr_country_account='CONTROLLED_FOREIGN')
        session.add(BrokerOrderEvent(id=uuid4(),order_id=order.id,event_type=kind,event_source='broker',
            event_at=at,payload=payload))
    session.flush()
    order.updated_at,parent.updated_at=order_at,parent_at
    session.flush()
    inflight,_,terminal=answer(session,True)
    assert 'PCLA' in inflight and ('PCLA',POSITIVE_TOKEN) not in terminal
    assert sum(symbol=='NCPL' for symbol,_ in terminal)==2


def test_combined_exact_later_cancel_rejection_audit_does_not_replace_target_state(census,monkeypatch):
    session,_,order,event,parent,account,_,_=selected(census,monkeypatch)
    payload=deepcopy(event.payload)
    payload['internal']='CANCEL_VERIFY_RESUBMIT'
    payload['broker_order_id']=None
    payload['metadata'].pop('_webull_terminal_proof')
    payload['metadata'].update(webull_error_code='ORDER_CAN_NOT_BE_CANCEL',cancel_outcome='confirmed_after_accepted_request')
    audit=BrokerOrderEvent(id=uuid4(),order_id=order.id,event_type='rejected',event_source='broker',
        event_at=event.event_at+timedelta(seconds=1),payload=payload)
    session.add(audit)
    session.flush()
    assert ('PCLA',POSITIVE_TOKEN) in answer(session,True)[2]
    assert audit.event_type=='rejected' and order.status=='cancelled'


@pytest.mark.asyncio
async def test_combined_flag_on_native_unknown_never_reads_local_or_claims_complete(monkeypatch):
    from tests.unit.test_brokerdecides1_sync import service,native
    from project_mai_tai.brokerdecides1 import decode
    from project_mai_tai.brokerdecides1_sync import begin_pass,finish_pass
    async def reader(account):
        if 'webull' in account:
            raise ValueError('CONTROLLED_NATIVE_UNKNOWN')
        return native(account)
    value=service(monkeypatch,reader)
    value.settings.strategy_schwab_1m_v2_brokerdecides1_same_day_event_binding_enabled=True
    async def forbidden(*a,**k):pytest.fail('No local recovery without both actual complete books')
    value._run_db=forbidden
    generation=await begin_pass(value)
    await finish_pass(value,generation,sync_succeeded=True)
    result=decode(value.redis.raw)
    assert not result.local_complete and not result.terminal_reservations
