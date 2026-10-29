"""Exact retained seven links; missing parents/books remain CONTROLLED.

This successor exercises the read-only switch. Original source/raw1PASS1FAIL
receipts stay untouched in the causal checkout. No native/whole-book claim.
"""
import pytest
from copy import deepcopy
from datetime import UTC, datetime
from decimal import Decimal
import hashlib
import json
from pathlib import Path
from uuid import UUID, uuid4

from project_mai_tai.broker_adapters.protocols import ExecutionReport
from project_mai_tai.brokerdecides1 import AdmissionMemory, AdmissionSnapshot, NativeAccountBook
from project_mai_tai.brokerdecides1_sync import local_evidence
from project_mai_tai.db.models import BrokerOrder, BrokerOrderEvent, DashboardSnapshot, TradeIntent
from project_mai_tai.oms.store import OmsStore
from project_mai_tai.settings import Settings
from project_mai_tai.strategy_core.schwab_1m_v2 import OHLCVBar, SchwabV2Strategy
from tests.unit.test_brokerdecides1_local_census import census

BASE = Path('/Users/velkris/.codex/task-queues/analysis/OCT29-C6-ORIGINAL-CASE-RECEIPTS')
ACCOUNTS = ('live:schwab_1m_v2', 'live:orb')
NOW = 1793296323156  # Retained PCLA later SELL clock, not current NOW.
PINS = {
    'P1-OWNER-BOUND/ncpl_pcla_orders_attempts.jsonl': ('23f35226a140b4f040900ab2737f8dc5f0721ab60e3159298e600d5d08924483',14),
    'P1-OWNER-BOUND/ncpl_pcla_order_events.jsonl': ('cd4e6b229a75c856efce7f60ffb2179e4a10f75dc66ff948c9fa03aa57b287bc',25),
    'P1-OWNER-BOUND/ncpl_pcla_attempts_v2_wait_dispatch.jsonl': ('79008d8883a75d739df3b6e334fcf4112c87a821455df6e5587db0db5b903cf2',14),
}


def inputs():
    values = {}
    for name, (digest,count) in PINS.items():
        raw = (BASE/name).read_bytes()
        assert hashlib.sha256(raw).hexdigest() == digest
        lines = raw.splitlines()
        assert len(lines) == count
        if name.endswith('ncpl_pcla_orders_attempts.jsonl'):
            assert lines[11].startswith(b'ERROR:  column "symbol" does not exist')
            lines = lines[:11]  # Preserve the original failed export, not pretend14JSON.
        values[name.rsplit('/',1)[1]] = [json.loads(line,parse_float=Decimal) for line in lines]
    orders = [r for r in values['ncpl_pcla_orders_attempts.jsonl']
              if r['status']=='cancelled' and '-ocoexit-' not in r['client_order_id']]
    events = values['ncpl_pcla_order_events.jsonl']
    attempts = [r for r in values['ncpl_pcla_attempts_v2_wait_dispatch.jsonl']
                if r['payload']['kind']=='attempt']
    links = []
    for row in orders:
        own = sorted([e for e in events if e['client_order_id']==row['client_order_id']],
                     key=lambda e:(e['event_at'],e['id']))
        original = [e for e in own if e['payload'].get('metadata',{}).get('clearwait_dispatch_token')]
        assert len(original)==1 and original[0]['event_type']=='accepted'
        first = original[0]
        md = first['payload']['metadata']
        matches = [r for r in attempts if r['payload']['attempt_token']==md['clearwait_dispatch_token']]
        assert len(matches)==1
        wait = matches[0]
        assert wait['payload']['symbol']==first['symbol']==row['symbol']
        assert wait['payload']['opportunity_id']==md['fanout_segment_id']==row['seg']
        assert wait['payload']['account_name']==md['atr_country_account']
        assert 'clearwait_dispatch_token' not in row['payload']
        assert all(e['order_id']==first['order_id'] for e in own)
        assert all('clearwait_dispatch_token' not in e['payload']['metadata'] for e in own if e is not first)
        links.append((row,own,wait))
    assert len(links)==7 and sum(r['symbol']=='PCLA' for r,_,_ in links)==5
    return links


def selected_census(census, monkeypatch, *, preserve=False, binding=False):
    import project_mai_tai.brokerdecides1_sync as sync
    session, accounts, strategy = census
    for account,name in zip(accounts,ACCOUNTS,strict=True): account.name=name
    by_name={a.name:a for a in accounts}
    links=inputs()
    for row,events,wait in links:
        first=next(e for e in events if e['payload']['metadata'].get('clearwait_dispatch_token'))
        md=first['payload']['metadata'];account=by_name[wait['payload']['account_name']]
        # Actual parent payload/IDs and some row columns were not exported.
        # CONTROLLED linked parent/qty/native-id only isolate token matching.
        parent=TradeIntent(id=uuid4(),strategy_id=strategy.id,broker_account_id=account.id,
            symbol=row['symbol'],side='buy',intent_type='open',quantity=Decimal(1),reason='CONTROLLED_PARENT',
            status='cancelled',payload={'metadata':deepcopy(md)},
            created_at=datetime.fromisoformat(row['submitted_at']),updated_at=datetime.fromisoformat(row['updated_at']))
        session.add(parent);session.flush()
        native=next((e['payload']['broker_order_id'] for e in events if e['payload'].get('broker_order_id')),
                    'CONTROLLED:' + first['order_id'])
        target=BrokerOrder(id=UUID(first['order_id']),intent_id=parent.id,strategy_id=strategy.id,
            broker_account_id=account.id,client_order_id=row['client_order_id'],broker_order_id=native,
            symbol=row['symbol'],side='buy',quantity=Decimal(1),order_type='STOP_LIMIT',time_in_force='day',
            status='accepted' if preserve else row['status'],
            payload=deepcopy(md if preserve else row['payload']),
            submitted_at=datetime.fromisoformat(row['submitted_at']),updated_at=datetime.fromisoformat(row['updated_at']))
        if preserve:
            for event in events:
                if event is first or event['event_type']=='rejected': continue
                report=ExecutionReport(event['event_type'],row['client_order_id'],
                    broker_order_id=event['payload'].get('broker_order_id'),symbol=row['symbol'],
                    side='buy',intent_type='cancel',quantity=Decimal(1),
                    metadata=deepcopy(event['payload']['metadata']),reported_at=datetime.fromisoformat(event['event_at']),
                    origin=event['event_source'])
                OmsStore().update_order_from_report(target,report=report,metadata=report.metadata)
            assert target.payload['clearwait_dispatch_token']==md['clearwait_dispatch_token']
        assert target.status=='cancelled'
        session.add(target)
        session.add(DashboardSnapshot(id=UUID(wait['id']),snapshot_type='v2_wait_dispatch',
            created_at=datetime.fromisoformat(wait['created_at']),payload=deepcopy(wait['payload'])))
        for event in events:
            session.add(BrokerOrderEvent(id=UUID(event['id']),order_id=target.id,event_type=event['event_type'],
                event_source=event['event_source'],event_at=datetime.fromisoformat(event['event_at']),
                payload=deepcopy(event['payload'])))
    session.flush()
    monkeypatch.setattr(sync,'now_ms',lambda:NOW)
    inflight,fills,terminals=local_evidence(session,ACCOUNTS,prior_session_attempts_final=True, same_day_event_binding=binding)
    return links,inflight,fills,terminals


def sell_after_census(symbol,inflight):
    strategy=SchwabV2Strategy(Settings(_env_file=None,
        strategy_schwab_1m_v2_flip_owned_first_entry_enabled=True,
        strategy_schwab_1m_v2_keepwatch1_enabled=True,
        strategy_schwab_1m_v2_brokerdecides1_enabled=True,
        strategy_schwab_1m_v2_account_name=ACCOUNTS[0],strategy_schwab_1m_v2_webull_account_name=ACCOUNTS[1]))
    strategy._now_ms=lambda:NOW
    strategy.configure_fanout_identity_persistence(lambda *a:None)
    strategy.configure_flip_entry_ownership(lambda *a:None,restore_readable=True)
    state=strategy.watchlist_state(symbol);old={'PCLA':1793286123309,'NCPL':1793291104243}[symbol]
    state.fanout_segment_id=state.flip_owner_opportunity_id=old;state.flip_owner_phase='bound'
    state.flip_owner_first_rest_placed=True;state.flip_owner_fill_accounts.add(ACCOUNTS[0])
    state.flip_owner_position_ids[ACCOUNTS[0]]='CONTROLLED_CLOSED_ROW'
    state.flip_owner_provisional_started_ms=NOW-120000;state.flip_owner_flip_bar_ts=old
    state.flip_owner_evidence_readable=True;state.flip_owner_evidence_at_ms=NOW
    state.cw_resting_taken=True;state.atr_short_flip_bar_ts=1793296260000;state.atr_state='short'
    state.bars=[OHLCVBar(1793296260000,2,2,2,2,100)]
    strategy._brokerdecides1_memory=AdmissionMemory()
    strategy._brokerdecides1_memory.apply(AdmissionSnapshot('CONTROLLED_NATIVE_NOT_ORIGINAL',1,NOW-200,NOW-10,
        tuple(NativeAccountBook(a,p,NOW-100,NOW-20,frozenset(),frozenset(),True,'CONTROLLED_EMPTY')
              for a,p in zip(ACCOUNTS,('schwab','webull'))),inflight,frozenset(),True,'CONTROLLED_SELECTED7'))
    strategy._end_flip_owner_on_sell(state)
    if state.flip_owner_phase=='unknown': strategy._recover_unknown_flip_owner(state,(),(),frozenset())
    return state.flip_owner_phase,state.fanout_segment_id



def test_exact_seven_event_links_switch_off_keeps_both_owners_held(census,monkeypatch):
    links,inflight,fills,terminal=selected_census(census,monkeypatch,binding=False)
    assert len(links)==7 and inflight==frozenset({'NCPL','PCLA'}) and not fills and not terminal


def test_exact_seven_event_links_clear_ncpl_only_pending_precise_webull_lane(census,monkeypatch,record_property):
    links,inflight,fills,terminal=selected_census(census,monkeypatch,binding=True)
    expected={(r['symbol'],w['payload']['attempt_token']) for r,_,w in links if r['symbol']=='NCPL'}
    assert terminal==frozenset(expected) and inflight==frozenset({'PCLA'}) and not fills
    assert sell_after_census('NCPL',inflight)==('idle',0)
    assert sell_after_census('PCLA',inflight)[0]=='bound'
    assert all('clearwait_dispatch_token' not in row.payload for row in census[0].query(BrokerOrder).all())
    record_property('literal_seven_bindings',json.dumps([w['payload']['attempt_token'] for _,_,w in links]))
    record_property('native_evidence_scope','CONTROLLED parents/no-fills/no-replacement/native-books; original event source remains literal')


def controlled_link(census,monkeypatch):
    from datetime import timedelta
    from tests.unit.test_brokerdecides1_local_census import attempt,order,ACCOUNTS as names
    import project_mai_tai.brokerdecides1_sync as sync
    session,accounts,strategy=census
    original=attempt(session);row=order(session,accounts,strategy)
    closed_at=row.updated_at
    md=deepcopy(row.payload);md['source']='schwab_1m_v2'
    parent=session.get(TradeIntent,row.intent_id);parent.payload={'metadata':deepcopy(md)}
    original.created_at=parent.created_at-timedelta(seconds=1)
    session.add(BrokerOrderEvent(order_id=row.id,event_type='accepted',event_source='unknown',
        event_at=parent.created_at+timedelta(seconds=1),payload={'client_order_id':row.client_order_id,
        'broker_order_id':row.broker_order_id,'metadata':deepcopy(md)}))
    native=session.query(BrokerOrderEvent).filter_by(order_id=row.id,event_type='cancelled').one()
    native.payload={**native.payload,'reason':repr({'status':'CANCELED','orderId':row.broker_order_id,
        'filledQuantity':0,'remainingQuantity':0,'fixture':'CONTROLLED_NATIVE_ANSWER_NOT_ORIGINAL'})}
    row.payload={}
    session.flush()
    # SQLAlchemy's real onupdate clock fires on the controlled payload edits.
    # Restore this fixture's explicit old terminal clock after those edits;
    # never relax the application's existing 15-second terminal-age guard.
    row.updated_at=parent.updated_at=closed_at
    session.flush()
    return session,accounts,strategy,original,row,names


def test_read_only_binding_no_db_mutation_and_exact_terminal_only(census,monkeypatch):
    from sqlalchemy import event
    from sqlalchemy.sql.elements import SavepointClause, ReleaseSavepointClause, RollbackToSavepointClause
    session,accounts,strategy,attempt,row,names=controlled_link(census,monkeypatch)
    writes=[]
    def observe(conn,cursor,statement,parameters,context,many):
        # Existing retirement proof is SELECT-with-CTE (starts WITH). Inspect
        # SQLAlchemy's actual statement kind; never whitelist arbitrary WITH SQL.
        compiled=getattr(context,'compiled',None)
        control=compiled is not None and isinstance(compiled.statement,
            (SavepointClause,ReleaseSavepointClause,RollbackToSavepointClause))
        if compiled is None or not (getattr(compiled.statement,'is_select',False) or control):
            writes.append(statement)
    event.listen(session.bind,'before_cursor_execute',observe)
    try:
        result=local_evidence(session,names,same_day_event_binding=True)
    finally:event.remove(session.bind,'before_cursor_execute',observe)
    assert result==(frozenset(),frozenset(),frozenset({('WORX','persisted-attempt')}))
    assert not writes and row.payload=={} and attempt.payload['kind']=='attempt'


@pytest.mark.parametrize('fault',['partial-fill','partial-event','open','missing-parent','parent-token',
    'foreign-strategy','foreign-account','segment','wrong-client','different-token','duplicate-token','no-native','open-replacement',
    'positive-terminal-quantity','future-event'])
def test_binding_conflicts_or_real_inflight_stay_unknown(census,monkeypatch,fault):
    from datetime import timedelta
    from project_mai_tai.db.models import Fill,Strategy
    session,accounts,strategy,attempt,row,names=controlled_link(census,monkeypatch)
    parent=session.get(TradeIntent,row.intent_id)
    closed_at=row.updated_at
    accepted=session.query(BrokerOrderEvent).filter_by(order_id=row.id,event_type='accepted').one()
    if fault=='partial-fill':
        session.add(Fill(order_id=row.id,strategy_id=strategy.id,broker_account_id=accounts[0].id,
            broker_fill_id='CONTROLLED_PARTIAL',symbol=row.symbol,side='buy',quantity=Decimal('.5'),
            price=Decimal(3),filled_at=row.updated_at))
    elif fault=='partial-event':
        session.add(BrokerOrderEvent(order_id=row.id,event_type='partially_filled',event_source='broker',
            event_at=row.updated_at,payload={'broker_fill_id':'CONTROLLED_PARTIAL'}))
    elif fault=='open':row.status=parent.status='accepted'
    elif fault=='missing-parent':row.intent_id=None
    elif fault=='parent-token':parent.payload={'metadata':{'clearwait_dispatch_token':'foreign','fanout_segment_id':'100'}}
    elif fault=='foreign-strategy':
        foreign=Strategy(code='foreign',name='foreign');session.add(foreign);session.flush();row.strategy_id=foreign.id
    elif fault=='foreign-account':row.broker_account_id=accounts[1].id
    elif fault=='segment':accepted.payload={**accepted.payload,'metadata':{**accepted.payload['metadata'],'fanout_segment_id':'101'}}
    elif fault=='wrong-client':accepted.payload={**accepted.payload,'client_order_id':'foreign'}
    elif fault=='different-token':row.payload={'clearwait_dispatch_token':'foreign'}
    elif fault=='duplicate-token':
        session.add(BrokerOrderEvent(order_id=row.id,event_type='accepted',event_source='unknown',
            event_at=accepted.event_at+timedelta(milliseconds=1),payload=deepcopy(accepted.payload)))
    elif fault=='open-replacement':
        session.add(DashboardSnapshot(snapshot_type='atr_reprice_handoff',payload={'phase':'cancel_pending',
            'old':{'client_order_id':row.client_order_id,'symbol':row.symbol,'side':'buy',
            'broker_account_name':names[0],'strategy_code':'schwab_1m_v2','quantity':'1'},
            'replacement':{'client_order_id':'CONTROLLED_STILL_OPEN','symbol':row.symbol,'side':'buy',
            'broker_account_name':names[0],'strategy_code':'schwab_1m_v2','quantity':'1'}}))
    elif fault=='positive-terminal-quantity':
        native=session.query(BrokerOrderEvent).filter_by(order_id=row.id,event_type='cancelled').one()
        native.payload={**native.payload,'filled_quantity':'0.5'}
    elif fault=='future-event':accepted.event_at=datetime.now(UTC)+timedelta(minutes=1)
    else:
        native=session.query(BrokerOrderEvent).filter_by(order_id=row.id,event_type='cancelled').one();native.event_source='client'
    session.flush()
    row.updated_at=parent.updated_at=closed_at
    session.flush()
    inflight,_,terminal=local_evidence(session,names,same_day_event_binding=True)
    assert 'WORX' in inflight and not terminal


def test_fresh_terminal_clock_still_holds_without_an_age_waiver(census,monkeypatch):
    session,accounts,strategy,attempt,row,names=controlled_link(census,monkeypatch)
    row.updated_at=datetime.now(UTC)
    session.flush()
    inflight,_,terminal=local_evidence(session,names,same_day_event_binding=True)
    assert 'WORX' in inflight and not terminal


def test_default_off_adds_no_event_lookup_and_background_is_only_caller(census,monkeypatch):
    from project_mai_tai import brokerdecides1_event_binding as binding
    session,accounts,strategy,attempt,row,names=controlled_link(census,monkeypatch)
    monkeypatch.setattr(binding,'accepted_event_bindings',lambda *a:pytest.fail('OFF event lookup'))
    assert 'WORX' in local_evidence(session,names)[0]
    assert Settings(_env_file=None).strategy_schwab_1m_v2_brokerdecides1_same_day_event_binding_enabled is False
    source=(Path(__file__).resolve().parents[2]/'src/project_mai_tai/brokerdecides1_sync.py').read_text()
    assert source.count('accepted_event_bindings(session,')==1
    assert 'lambda: service._run_db(read_local, commit=False)' in source


@pytest.mark.asyncio
async def test_enabled_background_pass_forwards_binding_once_and_publishes_exact_answer(census,monkeypatch):
    import project_mai_tai.brokerdecides1_sync as sync
    from project_mai_tai.brokerdecides1 import decode
    from tests.unit.test_brokerdecides1_sync import service
    session,accounts,strategy,attempt,row,names=controlled_link(census,monkeypatch)
    observed=int(datetime.now(UTC).timestamp()*1000)
    async def reader(account):
        return NativeAccountBook(account,'schwab' if account==names[0] else 'webull',
            observed,observed,frozenset(),frozenset(),True,'CONTROLLED_NATIVE_EMPTY')
    value=service(monkeypatch,reader)
    monkeypatch.setattr(sync,'now_ms',lambda:observed)
    value.settings.strategy_schwab_1m_v2_brokerdecides1_same_day_event_binding_enabled=True
    calls=[]
    async def read(function,**kwargs):
        assert kwargs=={'commit':False}
        calls.append(function.__name__)
        return function(session)
    value._run_db=read
    generation=await sync.begin_pass(value)
    assert calls==[]  # Begin, bar/tick and native publication add no local read.
    await sync.finish_pass(value,generation,sync_succeeded=True)
    answer=decode(value.redis.raw)
    assert calls==['read_local'] and answer.local_complete and not answer.inflight
    assert answer.terminal_reservations==frozenset({('WORX','persisted-attempt')})
    assert len(value.redis.writes)==2 and row.payload=={} and attempt.payload['kind']=='attempt'


@pytest.mark.parametrize('fault',['missing-filled','null-filled','boolean-filled','positive-filled',
    'nonfinite-filled','missing-remaining','wrong-native-id','duplicate-key','malformed'])
def test_broker_cancel_without_explicit_exact_native_zero_never_releases(census,monkeypatch,fault):
    session,accounts,strategy,attempt,row,names=controlled_link(census,monkeypatch)
    native=session.query(BrokerOrderEvent).filter_by(order_id=row.id,event_type='cancelled').one()
    payload={'status':'CANCELED','orderId':row.broker_order_id,'filledQuantity':0,'remainingQuantity':0}
    if fault=='missing-filled':payload.pop('filledQuantity')
    elif fault=='null-filled':payload['filledQuantity']=None
    elif fault=='boolean-filled':payload['filledQuantity']=False
    elif fault=='positive-filled':payload['filledQuantity']='0.5'
    elif fault=='nonfinite-filled':payload['filledQuantity']='NaN'
    elif fault=='missing-remaining':payload.pop('remainingQuantity')
    elif fault=='wrong-native-id':payload['orderId']='foreign'
    reason=repr(payload)
    if fault=='duplicate-key':reason="{'status':'CANCELED','orderId':"+repr(row.broker_order_id)+",'filledQuantity':1,'filledQuantity':0,'remainingQuantity':0}"
    elif fault=='malformed':reason='CANCELED filledQuantity=0'
    native.payload={**native.payload,'reason':reason}
    session.flush()
    inflight,_,terminal=local_evidence(session,names,same_day_event_binding=True)
    assert 'WORX' in inflight and not terminal


def test_binding_exception_keeps_exact_flag_off_census_result(census,monkeypatch,caplog):
    from project_mai_tai import brokerdecides1_event_binding as binding
    session,accounts,strategy,attempt,row,names=controlled_link(census,monkeypatch)
    baseline=local_evidence(session,names)
    def refuse(*args):raise ValueError('CONTROLLED_PRIVATE_OPERAND_MUST_NOT_BE_LOGGED')
    monkeypatch.setattr(binding,'accepted_event_bindings',refuse)
    assert local_evidence(session,names,same_day_event_binding=True)==baseline
    assert '[BROKERDECIDES1-EVENT-BINDING-REFUSED] reason=ValueError' in caplog.text
    assert 'CONTROLLED_PRIVATE_OPERAND_MUST_NOT_BE_LOGGED' not in caplog.text


def test_one_closure_exception_keeps_its_symbol_inflight(census,monkeypatch):
    from project_mai_tai import brokerdecides1_event_binding as binding
    session,accounts,strategy,attempt,row,names=controlled_link(census,monkeypatch)
    def refuse(*args):raise ValueError('CONTROLLED_CLOSURE_OVERFLOW')
    monkeypatch.setattr(binding,'zero_execution_closure',refuse)
    inflight,fills,terminal=local_evidence(session,names,same_day_event_binding=True)
    assert inflight==frozenset({'WORX'}) and not fills and not terminal


def test_accepted_lookup_uses_existing_event_time_index_in_controlled_explain(census,record_property):
    from sqlalchemy import text
    from project_mai_tai.brokerdecides1 import MAX_SYMBOLS
    from project_mai_tai.brokerdecides1_event_binding import accepted_events_query
    from project_mai_tai.fanout_segment_store import current_session_anchor
    session,accounts,strategy=census
    query=accepted_events_query(('CONTROLLED_PLAN_TOKEN',),current_session_anchor())
    statement=str(query.compile(dialect=session.bind.dialect,compile_kwargs={'literal_binds':True}))
    assert "broker_order_events.event_type = 'accepted'" in statement
    assert 'broker_order_events.event_at >=' in statement and f'LIMIT {MAX_SYMBOLS+1}' in statement
    plan=session.execute(text('EXPLAIN QUERY PLAN '+statement)).all()
    details='; '.join(str(row[-1]) for row in plan)
    # SQLite can choose either existing index; the actual PostgreSQL choice is
    # not inferred from this controlled empty-fixture plan. Both indexed
    # predicates above remain mandatory and a table scan is not accepted.
    assert any('SEARCH broker_order_events USING INDEX '+name in details for name in
        ('ix_broker_order_events_event_at','ix_broker_order_events_event_type'))
    assert 'SCAN broker_order_events' not in details
    record_property('CONTROLLED_SQLITE_EXPLAIN_NOT_PRODUCTION',details)


def test_recovery_query_error_rolls_back_only_savepoint_before_baseline_continues(census,monkeypatch):
    from sqlalchemy import event
    from sqlalchemy.exc import OperationalError
    session,accounts,strategy,attempt,row,names=controlled_link(census,monkeypatch)
    baseline=local_evidence(session,names)
    state={'injected':False,'aborted':False,'savepoint_rollback':False}
    def controlled_transaction_failure(conn,cursor,statement,parameters,context,many):
        if statement.startswith('ROLLBACK TO SAVEPOINT'):
            state.update(aborted=False,savepoint_rollback=True)
        elif state['aborted']:
            raise AssertionError('CONTROLLED_ABORTED_TRANSACTION_REUSED_WITHOUT_SAVEPOINT_ROLLBACK')
        elif not state['injected'] and 'JOIN broker_orders ON broker_orders.id = broker_order_events.order_id' in statement:
            state.update(injected=True,aborted=True)
            raise OperationalError('CONTROLLED_RECOVERY_QUERY',{},RuntimeError('CONTROLLED_QUERY_TIMEOUT'))
    event.listen(session.bind,'before_cursor_execute',controlled_transaction_failure)
    try:
        assert local_evidence(session,names,same_day_event_binding=True)==baseline
    finally:event.remove(session.bind,'before_cursor_execute',controlled_transaction_failure)
    assert state=={'injected':True,'aborted':False,'savepoint_rollback':True}
