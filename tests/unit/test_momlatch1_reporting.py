"""Reporting controls only. Policy outputs and clocks are controlled, not live injections."""
import asyncio
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import sys
import threading
from types import ModuleType

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from project_mai_tai.db.models import SystemIncident
from project_mai_tai.momwarn_pause_incidents import (
    MomwarnPauseIncidentReporter, PauseIncidentStore, readable_pause_snapshot,
    approximate_pause_utc_ns,
)
from project_mai_tai.services.control_plane import (
    _read_momentum_warning_gateway_metrics, _render_momentum_warning_gateway_metrics,
)

NOW = int(datetime(2026,10,13,20,20,tzinfo=UTC).timestamp()*1e9)


def metric(*, alerts=None, **updates):
    state={'state':'paused','reason':'monitor_unreadable','reasonclass':'monitor',
        'clean_reads':0,'required_clean_reads':3,'pause_count':7,'wire_pause_count':7,
        'recovery_interruptions':0,'reason_counts':{'monitor_unreadable':7},
        'history':[{'reason':'monitor_unreadable','start_ns':1000,'end_ns':None,'duration_seconds':601}],
        'current_pause':{'start_monotonic_ns':1000,'end_monotonic_ns':None,
            'duration_seconds':601},'alert_reasons':alerts or []}
    return {'version':1,'source':'market-data-gateway','event_type':'gateway_sample',
        'gateway_instance_id':'CONTROLLED-GUID','generation':1,'observed_ns':NOW,
        'observed_monotonic_ns':1_000_000_000_000,'pause_state':state,**updates}


class RedisReadOnly:
    def __init__(self,*samples):
        self.samples=samples
    async def xrevrange(self,_stream,*,count):
        assert count==64
        return [(str(i),{'data':json.dumps(sample)}) for i,sample in enumerate(self.samples)]


@pytest.fixture
def factory(tmp_path):
    engine=create_engine('sqlite:///'+str(tmp_path/'incidents.sqlite'),connect_args={'check_same_thread':False})
    SystemIncident.__table__.create(engine)
    yield sessionmaker(engine)
    engine.dispose()


@pytest.mark.asyncio
async def test_policy_alerts_normal_warning_paper_context_offloop_and_deduplicated(factory):
    store=PauseIncidentStore(factory)
    record=store.record
    threads=[]
    def traced(sample):
        threads.append(threading.get_ident())
        return record(sample)
    store.record=traced
    reporter=MomwarnPauseIncidentReporter(store)
    sample=metric(alerts=['pause_rate_gt_6_hour','pause_over_10min'])
    assert await reporter.observe(sample,now_ns=NOW)=='QUEUED'
    await reporter._task
    assert await reporter.observe(sample,now_ns=NOW)=='QUEUED'
    await reporter._task
    assert threads and all(t!=threading.get_ident() for t in threads)
    with factory() as session:
        rows=session.scalars(select(SystemIncident)).all()
        assert len(rows)==2
        for row in rows:
            assert row.severity=='warning' and row.service_name=='market-data-gateway'
            assert row.payload['account_name']=='paper:momentum'
            assert row.payload['strategy_codes']==['momentum_30s_warn','momentum_60s_warn']
            assert row.payload['times_scope']=='POLICY_MONOTONIC_ONLY_NOT_UTC'
            assert 'symbol' not in row.payload and 'position' not in row.payload
            assert 'quantity' not in row.payload and 'broker_account_name' not in row.payload
            assert row.payload['pause_state']==sample['pause_state']
    assert await reporter.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('count,duration,alerts,expected',[
    (6,600,[], 'NO_ALERT'),(7,600,['rate'], 'QUEUED'),
    (1,601,['duration'],'QUEUED'),(100,999,[], 'NO_ALERT'),
])
async def test_reporter_consumes_policy_alerts_not_a_second_threshold_policy(factory,count,duration,alerts,expected):
    reporter=MomwarnPauseIncidentReporter(PauseIncidentStore(factory))
    sample=metric(alerts=alerts)
    sample['pause_state']['pause_count']=count
    sample['pause_state']['current_pause']['duration_seconds']=duration
    assert await reporter.observe(sample,now_ns=NOW)==expected
    if reporter._task:
        await reporter._task
    assert await reporter.close()


@pytest.mark.asyncio
async def test_warning_incident_operator_closure_is_respected(factory):
    reporter=MomwarnPauseIncidentReporter(PauseIncidentStore(factory))
    sample=metric(alerts=['rate'])
    await reporter.observe(sample,now_ns=NOW)
    await reporter._task
    with factory.begin() as session:
        row=session.scalar(select(SystemIncident))
        row.status='closed'
    await reporter.observe(sample,now_ns=NOW)
    await reporter._task
    with factory() as session:
        row,=session.scalars(select(SystemIncident)).all()
        assert row.status=='closed'
    assert await reporter.close()


@pytest.mark.parametrize('change',[
    {'pause_state':None},{'pause_state':{}},{'observed_ns':NOW-20_000_000_001},
    {'observed_ns':NOW+1},{'generation':True},{'version':True},
    {'source':'oms-risk'},{'gateway_instance_id':''},
    {'pause_state':{'number':float('nan')}},{'pause_state':{'large':'x'*9000}},
])
def test_missing_stale_or_invalid_evidence_unknown_never_flat_or_zero(change):
    assert readable_pause_snapshot(metric(**change),now_ns=NOW) is None


@pytest.mark.asyncio
async def test_page_reads_existing_metrics_and_escapes_raw_policy_times_without_fake_UTC():
    sample=metric()
    sample['pause_state']['reason']='<script>NO</script>'
    result=await _read_momentum_warning_gateway_metrics(RedisReadOnly(sample),'test',now_ns=NOW)
    assert result['status']=='READABLE' and result['pause_state']==sample['pause_state']
    html=_render_momentum_warning_gateway_metrics(result)
    assert '&lt;script&gt;' in html and '<script>' not in html
    assert 'pause_count' in html and 'duration_seconds' in html and 'start_monotonic_ns' in html
    result=await _read_momentum_warning_gateway_metrics(
        RedisReadOnly(metric(observed_ns=NOW-20_000_000_001)),'test',now_ns=NOW)
    assert result=={'status':'UNKNOWN'}
    assert 'UNKNOWN' in _render_momentum_warning_gateway_metrics(result)


@pytest.mark.asyncio
async def test_page_conflicting_GUID_is_unknown_and_missing_pause_does_not_become_zero():
    result=await _read_momentum_warning_gateway_metrics(
        RedisReadOnly(metric(),metric(gateway_instance_id='OTHER')),'test',now_ns=NOW)
    assert result=={'status':'UNKNOWN'}
    sample=metric()
    sample.pop('pause_state')
    result=await _read_momentum_warning_gateway_metrics(RedisReadOnly(sample),'test',now_ns=NOW)
    assert result['pause_state']=='UNKNOWN'


@pytest.mark.asyncio
async def test_coalesces_latest_only_with_one_worker_no_loop_DB():
    started,release=threading.Event(),threading.Event()
    calls=[]
    class Store:
        def record(self,sample):
            calls.append(sample)
            started.set()
            assert release.wait(2), 'controlled worker barrier not released'
    reporter=MomwarnPauseIncidentReporter(Store())
    await reporter.observe(metric(alerts=['rate']),now_ns=NOW)
    assert await asyncio.to_thread(started.wait,1)
    for i in range(1,11):
        await reporter.observe(metric(alerts=['rate'],observed_ns=NOW+i),now_ns=NOW+i)
    assert len(calls)==1 and reporter._pending['observed_ns']==NOW+10
    release.set()
    await reporter._task
    assert len(calls)==2 and calls[-1]['observed_ns']==NOW+10
    assert await reporter.close()


@pytest.mark.asyncio
async def test_new_instance_or_regressed_epoch_refused_with_no_SQL(factory):
    reporter=MomwarnPauseIncidentReporter(PauseIncidentStore(factory))
    assert await reporter.observe(metric(),now_ns=NOW)=='NO_ALERT'
    assert await reporter.observe(metric(alerts=['rate'],gateway_instance_id='OTHER'),now_ns=NOW)=='UNKNOWN'
    assert await reporter.observe(metric(alerts=['rate'],generation=0),now_ns=NOW)=='UNKNOWN'
    assert await reporter.observe(metric(alerts=['rate'],observed_ns=NOW-1),now_ns=NOW)=='UNKNOWN'
    with factory() as session:
        assert session.scalars(select(SystemIncident)).all()==[]
    assert await reporter.close()


def test_paired_clock_projection_is_explicit_estimate_not_invented_UTC():
    sample=metric()
    assert approximate_pause_utc_ns(1_000_000_000_000-2_000_000_000,sample)==NOW-2_000_000_000
    assert approximate_pause_utc_ns(None,sample) is None
    assert approximate_pause_utc_ns(1_000_000_000_001,sample) is None
    sample.pop('observed_monotonic_ns')
    assert approximate_pause_utc_ns(1000,sample) is None


@pytest.mark.asyncio
async def test_store_failure_cannot_kill_metrics_loop_and_can_retry():
    calls=[]
    class Store:
        def record(self,sample):
            calls.append(sample)
            if len(calls)==1:
                raise RuntimeError('CONTROLLED DB failure')
    reporter=MomwarnPauseIncidentReporter(Store())
    assert await reporter.observe(metric(alerts=['rate']),now_ns=NOW)=='QUEUED'
    await reporter._task
    assert reporter.failures==1
    assert await reporter.observe(metric(alerts=['rate']),now_ns=NOW)=='QUEUED'
    await reporter._task
    assert len(calls)==2 and reporter.failures==1
    assert await reporter.close()


@pytest.mark.asyncio
async def test_wait_timeout_keeps_same_worker_not_unbounded_orphan_threads(monkeypatch):
    started,release=threading.Event(),threading.Event()
    calls=[]
    class Store:
        def record(self,sample):
            calls.append(sample)
            started.set()
            assert release.wait(2)
    original=asyncio.wait_for
    async def controlled_timeout(awaitable,timeout):
        if reporter.timeouts==0:
            raise TimeoutError('CONTROLLED await budget; no latency measurement')
        return await original(awaitable,timeout)
    reporter=MomwarnPauseIncidentReporter(Store())
    monkeypatch.setattr(asyncio,'wait_for',controlled_timeout)
    await reporter.observe(metric(alerts=['rate']),now_ns=NOW)
    assert await asyncio.to_thread(started.wait,1)
    assert reporter.timeouts==1
    await reporter.observe(metric(alerts=['rate'],observed_ns=NOW+1),now_ns=NOW+1)
    assert len(calls)==1
    release.set()
    await reporter._task
    assert len(calls)==2 and reporter.timeouts==1
    assert await reporter.close()


@pytest.fixture
def policy_module():
    selected=os.environ.get('MOMLATCH_POLICY_SOURCE')
    if not selected:
        pytest.skip('authoritative policy not selected; no forked policy fixture')
    module=ModuleType('_momlatch_reporting_external_policy')
    sys.modules[module.__name__]=module
    exec(compile(Path(selected).read_bytes(),selected,'exec'),module.__dict__)
    return module


@pytest.mark.asyncio
@pytest.mark.parametrize('duration_ns,alerted',[(600_000_000_000,False),(600_000_000_001,True)])
async def test_actual_policy_long_pause_threshold_drives_normal_incident(factory,policy_module,duration_ns,alerted):
    clock=[0]
    policy=policy_module.WarningPausePolicy(clock_ns=lambda:clock[0])
    policy.observe('monitor_unreadable')
    clock[0]=duration_ns
    state=policy.metrics()
    sample=metric(pause_state=state,observed_monotonic_ns=clock[0])
    reporter=MomwarnPauseIncidentReporter(PauseIncidentStore(factory))
    assert await reporter.observe(sample,now_ns=NOW)==('QUEUED' if alerted else 'NO_ALERT')
    if reporter._task:
        await reporter._task
    with factory() as session:
        rows=session.scalars(select(SystemIncident)).all()
        assert len(rows)==int(alerted)
        if rows:
            assert rows[0].payload['alert_kind']=='pause_over_10min'
    assert await reporter.close()


@pytest.mark.asyncio
async def test_actual_policy_flapping_metrics_page_and_warning_context(factory,policy_module):
    clock=[1]
    policy=policy_module.WarningPausePolicy(clock_ns=lambda:clock[0])
    def read(reason=None):
        clock[0]+=1
        return policy.observe(reason,ready=reason is None)
    for _ in range(3):
        read()
    for _ in range(7):
        read('monitor_unreadable')
        for _ in range(3):
            read()
    state=policy.metrics()
    assert 'pause_flapping' in state['alert_reasons']
    state['history']=state['history'][-8:]
    sample=metric(pause_state=state,observed_monotonic_ns=clock[0])
    reporter=MomwarnPauseIncidentReporter(PauseIncidentStore(factory))
    await reporter.observe(sample,now_ns=NOW)
    await reporter._task
    result=await _read_momentum_warning_gateway_metrics(RedisReadOnly(sample),'test',now_ns=NOW)
    html=_render_momentum_warning_gateway_metrics(result)
    assert 'pause_flapping' in html and 'observed-offset estimate' in html
    assert result['pause_state']==state
    with factory() as session:
        row,=session.scalars(select(SystemIncident)).all()
        assert row.severity=='warning' and row.payload['account_name']=='paper:momentum'
    assert await reporter.close()
