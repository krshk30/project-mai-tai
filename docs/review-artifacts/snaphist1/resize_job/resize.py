"""Reviewed resize evidence/edits. Broker and Postgres access is read-only."""
from __future__ import annotations

import hashlib
import asyncio
import json
import math
import os
import re
import runpy
import shlex
import shutil
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from urllib.parse import quote
from pathlib import Path
from zoneinfo import ZoneInfo

from review_gate import BASE, PIN, ROOT, TREE, verify

REPO = Path('/home/trader/project-mai-tai')
RUN = Path('/home/trader/after-hours/2026-10-03/resize-run')
ENV = Path('/etc/project-mai-tai/project-mai-tai.env')
PREOPEN = Path('/home/trader/preopen.sh')
MONDAY = Path('/home/trader/after-hours/2026-10-05/guard-start-job/start-guard-20261005.sh')
JOURNAL = Path('/home/trader/fleet_health/deployments-20261003.md')
KEY = 'MAI_TAI_REDIS_SNAPSHOT_BATCH_STREAM_MAXLEN'
OWNER_NAMES = {'strategy-engine','schwab-1m-v2','orb','orb-schwab','momentum-paper'}
APPS = ('control','market-capture','market-data','oms','orb','orb-schwab',
        'reconciler','schwab-1m-v2','strategy','momentum-paper')
APP_UNITS = tuple(f'project-mai-tai-{x}.service' for x in APPS)
INFRA = ('redis-server.service','postgresql@16-main.service')
KNOWN_STREAMS = {'heartbeats','market-data','market-data-subscriptions','order-events',
                 'runtime-controls','snapshot-batches','strategy-intents','strategy-state','strategy-state-isolated'}
LIMIT = 1_600_000_000
PINS = {
    ENV: 'cbb03703e2722581ae50ed3bd286f7e3c369ac39fac2de30d2da98a1c427f718',
    PREOPEN: 'f460100dced83dd7ffac94dc640b19e3dff0bb79c95b1d96a467b81915a3baf1',
    MONDAY: '5600628f18891c3a7bbfc2938580209ef09d5d6bade00040e12eeb2ce6761dd9',
}
DETECTORS = {
    'scripts/option_a_treatment_1008_sampler.py':'22b2f4fdb5a0b5aabea962b8718652adf44e829c5222f51e001ea9b37b648aa5',
    'ops/health/fleet_health_check.py':'cc0a87158cc3df0da77ef9f6c0e37b2eff23309e554e13ca95cd8014878d03ac',
    'ops/health/fleet_health_cron.sh':'0fe0b71767af26d82ffda8c1fdf2530c41579bc7a9dda851dc0faf72e12a6d66',
}


def need(ok, reason):
    if not ok:
        raise RuntimeError('STOP: '+reason)


def cmd(*args, timeout=60):
    return subprocess.check_output(args, text=True, timeout=timeout).strip()


def git(*args):
    return cmd('sudo','-u','trader','git','-C',str(REPO),*args)


def now():
    return datetime.now(UTC)


def sha(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as f:
        for block in iter(lambda:f.read(1024*1024), b''):
            h.update(block)
    return h.hexdigest()


def load(name):
    return json.loads((RUN/name).read_text())


def save(name, data):
    with (RUN/name).open('x') as out:
        json.dump(data,out,sort_keys=True,indent=2)
        out.write('\n')
        out.flush()
        os.fsync(out.fileno())
    print('EVIDENCE',str(RUN/name),flush=True)


def boot_id():
    return Path('/proc/sys/kernel/random/boot_id').read_text().strip()


def atomic(path, data):
    st = path.stat()
    pending = path.with_name(path.name+'.resize.pending')
    with pending.open('xb') as out:
        out.write(data)
        out.flush()
        os.fsync(out.fileno())
    os.chown(pending,st.st_uid,st.st_gid)
    os.chmod(pending,st.st_mode & 0o777)
    os.replace(pending,path)


def identity(unit):
    raw = cmd('systemctl','show',unit,'-p','MainPID','-p','ExecMainStartTimestamp',
              '-p','ActiveState','-p','SubState','-p','NRestarts','-p','InvocationID')
    return dict(line.split('=',1) for line in raw.splitlines())


def active(row):
    return (row['ActiveState']=='active' and row['SubState']=='running'
            and int(row['MainPID'])>0 and row['NRestarts']=='0')


def client():
    from redis import Redis
    from project_mai_tai.settings import Settings
    s=Settings(_env_file=str(ENV))
    return Redis.from_url(s.redis_url,decode_responses=True,socket_timeout=5,socket_connect_timeout=5),s


def size(value):
    if isinstance(value,str):
        return len(value.encode())
    if isinstance(value,dict):
        return sum(size(k)+size(v) for k,v in value.items())
    if isinstance(value,(list,tuple)):
        return sum(size(v) for v in value)
    return len(str(value))


def bounded(value, limit, label):
    result=size(value)
    need(result<=limit,f'{label} bytes={result}>{limit}')
    return result


def safety(r, *, expected=None):
    stats,memory=r.info('stats'),r.info('memory')
    bounded([stats,memory],262144,'Redis INFO')
    result={'evicted_keys':int(stats['evicted_keys']),'used_memory':int(memory['used_memory'])}
    need(result['used_memory']<=LIMIT,f'Redis margin {result}')
    if expected is not None:
        need(result['evicted_keys']==expected,f'Redis eviction {result}, expected={expected}')
    return result


def persistence(r):
    data=r.config_get('save','appendonly','dir','dbfilename','maxmemory','maxmemory-policy')
    bounded(data,16384,'Redis config')
    need(data['save']=='' and data['appendonly']=='no','persistence drift; do not change it')
    need(data['dir']=='/var/lib/redis' and data['dbfilename']=='dump.rdb','RDB location drift')
    need(data['maxmemory']==str(2*1024**3) and data['maxmemory-policy']=='allkeys-lru','Redis memory policy drift')
    return data


def inventory(r,prefix):
    keys=[]
    for key in r.scan_iter(count=100):
        keys.append(key)
        need(len(keys)<=2000,'key inventory >2000')
    bounded(keys,262144,'Redis SCAN keys')
    allowed={prefix+':'+x for x in KNOWN_STREAMS}|{prefix+':market-data-subscription-owners'}
    need(set(keys)<=allowed,'unknown Redis-only state: '+str(sorted(set(keys)-allowed)))
    return {key:r.type(key) for key in sorted(keys)}


def owners(r,prefix):
    key=prefix+':market-data-subscription-owners'
    fields=r.hkeys(key)
    bounded(fields,16384,'owner HKEYS')
    need(set(fields)==OWNER_NAMES|{'_migration_complete','_last_applied_id'},'owner fields incomplete/unknown')
    need(sum(r.hstrlen(key,f) for f in fields)<=262144,'owner values oversized')
    raw=r.hgetall(key)
    bounded(raw,262144,'owner HGETALL')
    need(raw.get('_migration_complete')=='1','migration marker absent')
    sets={name:json.loads(raw[name]) for name in OWNER_NAMES}
    for name,values in sets.items():
        need(isinstance(values,list) and all(isinstance(x,str) and re.fullmatch(r'[A-Z0-9.\-]+',x) for x in values),
             f'invalid owner set {name}')
        need(len(values)==len(set(values)),f'duplicate owner {name}')
        sets[name]=sorted(values)
    need(len(sets['momentum-paper'])<=16,'paper cap')
    return raw,sets


def capture_owners():
    r,s=client()
    with r:
        before=safety(r)
        raw,sets=owners(r,s.redis_stream_prefix)
        latest={}
        cursor='-'
        stream=s.redis_stream_prefix+':market-data-subscriptions'
        for _ in range(40):
            rows=r.xrange(stream,min=cursor,count=25)
            bounded(rows,1024**2,'subscription page COUNT25')
            if not rows:
                break
            for entry_id,fields in rows:
                event=json.loads(fields['data'])
                p=event['payload']
                need(p['consumer_name'] in OWNER_NAMES and p['mode']=='replace','unreviewed owner or non-replace')
                latest[p['consumer_name']]={'source_id':entry_id,'fields':fields,'symbols':sorted(p['symbols'])}
            cursor='('+rows[-1][0]
        else:
            raise RuntimeError('STOP: subscription capture >1000 events')
        need({k:v['symbols'] for k,v in latest.items()}==sets,'stream and owner hash differ')
        tail=r.xrevrange(stream,count=1)
        bounded(tail,1024**2,'subscription tail COUNT1')
        need(tail and tail[0][0]==raw['_last_applied_id'] and owners(r,s.redis_stream_prefix)[0]==raw,'owner capture raced')
        need(sets['momentum-paper']==[],'paper owns symbols before Saturday reboot')
        events=sorted(latest.values(),key=lambda v:tuple(map(int,v['source_id'].split('-'))))
        return {'at_utc':now().isoformat(),'raw':raw,'sets':sets,'events':events,
                'static':s.market_data_static_symbol_list,'safety':safety(r,expected=before['evicted_keys'])}


def flat():
    path=Path('/home/trader/after-hours/2026-10-01/option-a-strict-flat-check.py')
    need(sha(path)=='831913206678c5bd2fa6718173c4c5d7b5887c62e44217ad10b6776851f0381e','flat helper hash')
    output=cmd(str(REPO/'.venv/bin/python'),str(path),timeout=90)
    print(output,flush=True)
    for value in ("open_managed={'live:schwab_1m_v2': 0, 'live:orb': 0}",'nonzero_virtual=[]',
                  'net_bot_fills=[]','schwab_holding_rows=0 ','webull_holding_rows=0 '):
        need(value in output,'strict flat proof missing '+value)
    from sqlalchemy import text
    from project_mai_tai.db.session import build_engine
    _,s=client()
    engine=build_engine(s.database_url,connect_timeout_s=5,statement_timeout_ms=5000)
    queries={
        'working_orders':"SELECT count(*) FROM broker_orders o JOIN broker_accounts a ON a.id=o.broker_account_id WHERE a.name IN ('live:orb','live:schwab_1m_v2') AND lower(o.status) IN ('pending','submitted','accepted','partially_filled')",
        'nfq_pending':"SELECT count(*) FROM dashboard_snapshots WHERE snapshot_type='oms_webull_mirror_price_hold' AND payload->>'phase' IN ('held','queued','dispatching','uncertain')",
        'confirmation_outbox':"SELECT count(*) FROM v2_confirmation_exit_evaluations WHERE should_exit AND published_at IS NULL",
    }
    with engine.connect() as conn:
        values={k:int(conn.execute(text(q)).scalar_one()) for k,q in queries.items()}
    engine.dispose()
    print('DURABLE_PENDING',json.dumps(values),flush=True)
    need(not any(values.values()),'unresolved orders/holds/outbox; no lossy reboot')
    order_proof=asyncio.run(direct_open_orders(s))
    print('OPEN_ORDER_READ',json.dumps(order_proof,sort_keys=True),flush=True)
    print('STRICT_FLAT_PASS both_accounts=1 manual_override_used=0',flush=True)
    return order_proof


async def direct_open_orders(settings):
    from sqlalchemy import select
    from sqlalchemy.orm import Session
    from project_mai_tai.db.models import BrokerOrder
    from project_mai_tai.db.session import build_engine
    from project_mai_tai.oms.store import OmsStore
    from project_mai_tai.broker_adapters.schwab import SchwabBrokerAdapter
    from project_mai_tai.broker_adapters.webull import WebullBrokerAdapter
    from webull.trade.request.get_open_orders_request import OpenOrdersListRequest
    broker=SchwabBrokerAdapter(settings)
    account=broker.accounts_by_name['live:schwab_1m_v2']
    # Exactly the store selector used by OMS sync, without calling its mutating
    # sync/persist routine. With no known orders, OMS makes zero detail calls.
    engine=build_engine(settings.database_url,connect_timeout_s=5,statement_timeout_ms=5000)
    known={}
    store=OmsStore()
    with Session(engine) as session:
        accounts=store.list_named_broker_accounts(session,['live:schwab_1m_v2','live:orb'])
        need({a.name for a in accounts}=={'live:schwab_1m_v2','live:orb'},'OMS sync accounts incomplete')
        sync_counts={}
        for a in accounts:
            sync_counts[a.name]=len(store.list_open_orders_for_sync(session,broker_account_ids=[a.id]))
            ids=session.execute(select(BrokerOrder.broker_order_id,BrokerOrder.client_order_id)
                                .where(BrokerOrder.broker_account_id==a.id)).all()
            known[a.name]={str(x) for row in ids for x in row if x}
    engine.dispose()
    need(not any(sync_counts.values()),'known OMS working order; wait, do not reboot')
    # Supplemental discovery uses the same bounded 12h account-list request as
    # the native-OCO sync reader. NOT a proof of all historical broker orders.
    current=now()
    frm=(current-timedelta(hours=12)).strftime('%Y-%m-%dT%H:%M:%S.000Z')
    to=(current+timedelta(hours=1)).strftime('%Y-%m-%dT%H:%M:%S.000Z')
    query=f'fromEnteredTime={frm}&toEnteredTime={to}&maxResults=500'
    exact_request=f'/trader/v1/accounts/{quote(account.account_hash,safe="")}/orders?{query}'
    status,headers,body=await broker._authorized_request_json('GET',exact_request)
    need(200<=status<300 and isinstance(body,list),'Schwab open-order discovery unreadable HTTP '+str(status))
    body_bytes=len(json.dumps(body,separators=(',',':'),ensure_ascii=False).encode())
    terminal={'FILLED','CANCELED','CANCELLED','REJECTED','EXPIRED','REPLACED'}
    nonterminal=[]
    def walk(row):
        need(isinstance(row,dict) and isinstance(row.get('status'),str),'Schwab order missing status')
        if row['status'].upper() not in terminal:
            nonterminal.append({'order_id':str(row.get('orderId','')),'status':row['status'],
                                'legs':row.get('orderLegCollection',[])})
        children=row.get('childOrderStrategies',[])
        need(isinstance(children,list),'Schwab malformed children')
        for child in children:
            walk(child)
    for row in body:
        walk(row)
    refuse_known_order('Schwab',{x['order_id'] for x in nonterminal},known['live:schwab_1m_v2'])
    webull=WebullBrokerAdapter(settings)
    cfg=webull.accounts_by_name['live:orb']
    request=OpenOrdersListRequest()
    request.set_account_id(cfg.account_id)
    request.set_page_size(100)
    response=await asyncio.to_thread(webull._get_client().get_response,request)
    need(200<=int(getattr(response,'status_code',0))<300,'Webull open-order HTTP error')
    orders=webull._body(response)
    need(isinstance(orders,dict) and isinstance(orders.get('orders'),list),'Webull open-order body unreadable')
    more=orders.get('has_next',orders.get('hasNext',False))
    need(isinstance(more,bool),'Webull page status unreadable')
    need(not more and len(orders['orders'])<100,'Webull open-order discovery truncated; report before proceeding')
    for row in orders['orders']:
        need(isinstance(row,dict),'Webull unreadable order')
        row_ids={str(row.get(k,'')) for k in ('order_id','orderId','client_order_id','clientOrderId')}- {''}
        need(row_ids,'Webull order identity unreadable')
        refuse_known_order('Webull',row_ids,known['live:orb'])
    raw_content=getattr(response,'content',None)
    result={'at_utc':now().isoformat(),'oms_sync_selector_counts':sync_counts,'oms_detail_requests':0,
        'schwab':{'request_method':'GET','request':'/trader/v1/accounts/<account_hash>/orders?'+query,
            'exact_request_root_only_evidence':exact_request,
            'exact_request_sha256':hashlib.sha256(exact_request.encode()).hexdigest(),'http_status':status,
            'response_decoded_json_bytes':body_bytes,'content_length_header':headers.get('Content-Length'),
            'returned_rows':len(body),'working_queued_accepted_and_other_nonterminal':len(nonterminal),
            'preexisting_orphans_nonblocking':nonterminal,'scope':'12h sync/OCO discovery, not whole-history completeness'},
        'webull':{'request_method':'GET','request':'/trade/orders/list-open','api_version':'v2',
            'query':{'account_id':cfg.account_id,'page_size':100},
            'http_status':int(response.status_code),'response_decoded_json_bytes':len(json.dumps(orders,separators=(',',':')).encode()),
            'response_content_bytes':len(raw_content) if isinstance(raw_content,bytes) else None,
            'returned_rows':len(orders['orders']),'has_next':more,'preexisting_orphans_nonblocking':orders['orders']}}
    return result


def refuse_known_order(broker, observed, known):
    need(not observed & known,broker+' known order still live; no reboot')


def drain():
    producers=('schwab-1m-v2','strategy','orb','orb-schwab','momentum-paper')
    need(all(identity(f'project-mai-tai-{x}.service')['MainPID']=='0' for x in producers),'producers not quiesced')
    r,s=client()
    with r:
        prior=r.xrevrange(s.redis_stream_prefix+':strategy-intents',count=1)
        bounded(prior,1024**2,'intent tail COUNT1')
        time.sleep(20)
        current=r.xrevrange(s.redis_stream_prefix+':strategy-intents',count=1)
        bounded(current,1024**2,'intent tail COUNT1')
        need(current==prior,'intent tail moved after producer stop; no blind interruption')
        safety(r,expected=load('before.json')['redis']['evicted_keys'])
    flat()
    idle_gates()
    archived=archive_intents()
    save('intents-final.json',archived)
    need(active(identity('project-mai-tai-oms.service')),'OMS unavailable during drain')
    save('drained.json',{'at_utc':now().isoformat(),'quiet_seconds':20,'intent_tail':current,
         'both_brokers_positions_and_known_orders':'zero; unknown orphan discoveries logged separately',
         'database_working_orders_holds_outbox':'zero',
         'retained_intents_archived':len(archived['entries'])})


def archive_intents():
    """Approved history disposition; do not replay old intents or invent ACKs."""
    r,s=client()
    entries=[]
    with r:
        stream=s.redis_stream_prefix+':strategy-intents'
        initial_count=r.xlen(stream)
        need(initial_count<=2000,'too many retained intents for bounded audit')
        cursor='-'
        for _ in range(81):
            page=r.xrange(stream,min=cursor,count=25)
            bounded(page,1024**2,'intent audit COUNT25')
            if not page:
                break
            for entry_id,fields in page:
                entries.append({'stream_id':entry_id,'fields':fields})
            cursor='('+page[-1][0]
        else:
            raise RuntimeError('STOP: intent audit exceeded 2000 entries')
        need(len(entries)==initial_count==r.xlen(stream),'intent archive changed during capture')
    return {'at_utc':now().isoformat(),'disposition':'archived history; OMS starts at $; never replay',
            'count':len(entries),'stream_ids':[x['stream_id'] for x in entries],'entries':entries}


def idle_gates():
    """Exercise current code with no publish route, using real read-only DB input."""
    from sqlalchemy import select
    from sqlalchemy.orm import sessionmaker
    from project_mai_tai.db.models import DashboardSnapshot
    from project_mai_tai.db.session import build_engine
    from project_mai_tai.services.schwab_1m_v2_bot import SchwabV2BotService
    from project_mai_tai.services.orb_schwab_app import OrbSchwabService
    from project_mai_tai.services.momentum_paper_app import MomentumPaperService
    _,settings=client()
    engine=build_engine(settings.database_url,connect_timeout_s=5,statement_timeout_ms=5000)
    factory=sessionmaker(bind=engine)
    with factory() as session:
        snapshot=session.scalar(select(DashboardSnapshot).where(
            DashboardSnapshot.snapshot_type=='scanner_confirmed_last_nonempty'))
        need(snapshot is not None and isinstance(snapshot.payload,dict),'scanner snapshot unreadable')
        source_session=snapshot.payload.get('scanner_session_start_utc')
        need(isinstance(source_session,str),'scanner source session unreadable')
    class NoIO:
        def __getattr__(self,name):
            raise RuntimeError('idle probe attempted external I/O: '+name)
    results=[]
    times=(now(),datetime(2026,10,3,17,0,tzinfo=ZoneInfo('America/New_York')))
    for instant in times:
        need(instant.astimezone(ZoneInfo('America/New_York')).weekday()>=5,'not a weekend idle proof')
        v2=SchwabV2BotService.__new__(SchwabV2BotService)
        v2.settings=settings
        session_kind=v2._market_session(instant)
        entry=v2._within_entry_window(instant)
        need(session_kind=='closed' and not entry,'v2 weekend entry gate did not close')
        orb=OrbSchwabService(settings=settings,redis_client=NoIO(),session_factory=factory)
        universe=orb._pre_open_universe(now=instant)
        need(universe==[] and orb._closed_bars==[] and orb._opening_orders=={},'ORB cold-start entry state not idle')
        paper=MomentumPaperService(settings=settings,redis_client=NoIO(),clock=lambda:instant)
        asyncio.run(paper._tick())
        need(paper._engine is None and paper._gateway_task is None and not paper._subscribed_symbols,
             'paper prepared or streamed on weekend')
        results.append({'probe_at_utc':instant.astimezone(UTC).isoformat(),
            'v2':{'market_session':session_kind,'within_entry_window':entry},
            'orb_schwab':{'scanner_source_session':source_session,'universe':universe,
                          'cold_start_closed_bars':0,'cold_start_opening_orders':0},
            'paper':{'engine':None,'gateway_task':None,'subscribed_symbols':[],
                     'external_io_calls':0,'broker_route':'none (paper service code)'},
            'scope':'read-only code probes, not inspection of another process memory'})
    engine.dispose()
    print('IDLE_GATES',json.dumps(results,sort_keys=True),flush=True)
    print('MANUAL_STOP_DISPOSITION operator: none; no hand orders to preserve; 2026-10-03 15:40 ET',flush=True)
    return results


def review_evidence(path):
    import contextlib
    import io
    r,_=client()
    with r:
        before=safety(r)
    output=io.StringIO()
    with contextlib.redirect_stdout(output):
        orders=flat()
        gates_result=idle_gates()
        intents=archive_intents()
    r,_=client()
    with r:
        after=safety(r,expected=before['evicted_keys'])
    data={'at_utc':now().isoformat(),'application':git('rev-parse','HEAD'),'orders':orders,
          'idle_gates':gates_result,'archived_intents':intents,'redis_before':before,'redis_after':after,
          'manual_stops':'operator: none; no hand orders to preserve; 2026-10-03 15:40 ET',
          'stdout':output.getvalue()}
    with path.open('x') as out:
        json.dump(data,out,sort_keys=True,indent=2)
        out.write('\n')
    path.chmod(0o600)
    print('REVIEW_EVIDENCE',path,'sha256='+sha(path))
    for account,detail in (('schwab',orders['schwab']),('webull',orders['webull'])):
        print('OPEN_ORDERS',account,'http='+str(detail['http_status']),
              'rows='+str(detail['returned_rows']),'json_bytes='+str(detail['response_decoded_json_bytes']))
    print('OMS_SYNC',orders['oms_sync_selector_counts'],'detail_requests=0')
    print('IDLE_GATES',json.dumps(gates_result,sort_keys=True))
    print('HISTORY_ARCHIVED count='+str(intents['count'])+' manual_stops=operator:none')
    print('REDIS',before,'->',after)


def before():
    et=now().astimezone(ZoneInfo('America/New_York'))
    need(et.date().isoformat()=='2026-10-03' and 1640<=int(et.strftime('%H%M'))<2100,'prepare window')
    need(git('rev-parse','HEAD')==BASE and not git('status','--porcelain'),'box base/clean drift')
    need(git('ls-remote','origin','refs/heads/main').split()[0]==BASE,'main moved before merge')
    for path,want in PINS.items():
        need(sha(path)==want,'file drift '+str(path))
    state={u:identity(u) for u in APP_UNITS+INFRA}
    need(all(active(v) for v in state.values()),'unhealthy app/infrastructure before resize')
    need(cmd('systemctl','show','project-mai-tai.target','-p','WantedBy','--value')=='','boot topology changed')
    need(cmd('systemctl','show','project-mai-tai-orb-schwab.service','-p','WantedBy','--value')=='','orb boot topology changed')
    need(not any(u in cmd('systemctl','list-jobs','--no-pager') for u in APP_UNITS),'concurrent service job')
    processes=cmd('ps','-eo','args')
    need(not any('option_a_treatment_guard.py' in x or 'option_a_treatment_1008_sampler.py' in x or
                 re.search(r'/(?:deploy_main|deploy_service)\.sh(?:\s|$)',x) for x in processes.splitlines()),
         'concurrent deploy/guard/sampler')
    r,s=client()
    with r:
        data={'boot_id':boot_id(),'services':state,'redis':safety(r),'persistence':persistence(r),
              'keys':inventory(r,s.redis_stream_prefix),'hardware':cmd('nproc')+'\n'+cmd('free','-m')}
    need(s.market_data_snapshot_interval_seconds==5,'scan interval changed')
    save('before.json',data)
    save('owners-before.json',capture_owners())


def backups():
    paths=list(PINS)+[Path('/var/lib/redis/dump.rdb'),Path('/etc/redis/redis.conf')]
    paths += [REPO/p for p in DETECTORS]
    paths += list(Path('/etc/systemd/system').glob('project-mai-tai*'))
    paths += [Path('/home/trader/restart_evidence')/n for n in
              ('expected_flags_check.py','expected_flags.json','expected_numeric.json','v2_restart_evidence.py')]
    index={}
    for n,path in enumerate(paths):
        if not path.is_file():
            continue
        dest=RUN/f'backup-{n}-{path.name}'
        need(not dest.exists(),'backup collision')
        shutil.copy2(path,dest)
        dest.chmod(0o600)
        need(sha(dest)==sha(path),'backup hash mismatch')
        index[str(path)]={'backup':str(dest),'sha256':sha(dest),'mode':path.stat().st_mode & 0o777,
                          'uid':path.stat().st_uid,'gid':path.stat().st_gid}
    save('backups.json',index)
    save('unit-definitions.json',{u:cmd('systemctl','cat',u) for u in APP_UNITS+INFRA})


def ready():
    release=verify()
    save('merge-ready.json',{k:release[k] for k in ('plan_commit','base_sha','pinned_head','pinned_tree')}|
         {'at_utc':now().isoformat()})


def receipt():
    release=verify()
    need((RUN/'merge-ready.json').exists(),'not waiting for merge')
    data=json.load(sys.stdin)
    need(data['plan_commit']==release['plan_commit'] and re.fullmatch('[0-9a-f]{40}',data['sha']),'merge receipt binding')
    save('merge.json',data)


def target():
    data=load('merge.json')
    app=data['sha']
    need(data['plan_commit']==json.loads((ROOT/'release.json').read_text())['plan_commit'],'plan receipt drift')
    need(git('rev-parse','origin/main')==app and git('rev-parse',app+'^{tree}')==TREE,'merged head/tree differs')
    git('merge-base','--is-ancestor',BASE,app)
    need(git('rev-list','--count',BASE+'..'+app)=='1','unexpected application merge range')
    return app


def detectors_env():
    for name,want in DETECTORS.items():
        path=REPO/name
        data=subprocess.check_output(['sudo','-u','trader','git','-C',str(REPO),'show',BASE+':'+name])
        need(hashlib.sha256(data).hexdigest()==want,'detector source')
        atomic(path,data)
        need(sha(path)==want,'detector installed hash')
    sampler=runpy.run_path(str(REPO/'scripts/option_a_treatment_1008_sampler.py'))
    fleet=runpy.run_path(str(REPO/'ops/health/fleet_health_check.py'))
    need(sampler['MARKER']==fleet['_MARKET_DATA_POLICY_NEEDLE']==b'1008 (policy violation)','bare 1008 detector')
    for line,expected in [(b'count=1008; published snapshot batch with 11008 records',0),
                          (b'received 1008 (policy violation); then sent 1008 (policy violation)',2)]:
        need(line.count(sampler['MARKER'])==expected,'detector signature test')
    cron=cmd('crontab','-l')
    need('*/5 * * * * /home/trader/project-mai-tai/ops/health/fleet_health_cron.sh' in cron,'pager invocation drift')
    need(not re.search(r'FLEET_HEALTH_CHECK\s*=',cron),'alternate pager reader')
    watch=Path('/home/trader/unexercised_watch/watch.py')
    need(sha(watch)=='45df60c60fe55af89406d20c29de277a44634d6bc6308586a4e0b62e718b8272'
         and '1008' not in watch.read_text(),'INC1 reader inventory changed')
    original=ENV.read_text()
    need(not re.search(r'^\s*(?:export\s+)?'+KEY+r'\s*=',original,re.M|re.I),'duplicate retention key')
    updated=original+('' if original.endswith('\n') else '\n')+KEY+'=120\n'
    atomic(ENV,updated.encode())
    save('detectors-env.json',{'detectors':DETECTORS,'env_before':PINS[ENV],'env_after':sha(ENV),KEY:'120'})


def boot_setup():
    created=[]
    for unit in APP_UNITS+INFRA:
        directory=Path('/etc/systemd/system')/(unit+'.d')
        directory.mkdir(exist_ok=True)
        path=directory/'90-resize-20261003-no-auto-retry.conf'
        with path.open('x') as out:
            out.write('[Service]\nRestart=no\n')
        created.append({'path':str(path),'sha256':sha(path)})
    cmd('systemctl','daemon-reload')
    cmd('systemctl','enable','project-mai-tai-resize-postboot-20261003.service')
    save('boot-setup.json',{'dropins':created,'postboot_enabled':True})
    need({u:identity(u) for u in APP_UNITS+INFRA}==load('before.json')['services'],'identity moved before quiesce')


def quiesce_ready():
    need(int(now().astimezone(ZoneInfo('America/New_York')).strftime('%H%M'))<2115,'too late for 21:15 quiesce window; no stop')
    need(git('rev-parse','HEAD')==target() and not git('status','--porcelain'),'checkout drift')
    r,_=client()
    with r:
        print('PRE_STOP_REDIS',safety(r,expected=load('before.json')['redis']['evicted_keys']))
    for unit in APP_UNITS+INFRA:
        need(cmd('systemctl','show',unit,'-p','Restart','--value')=='no','automatic retry still enabled '+unit)


def stopped():
    states={u:identity(u) for u in APP_UNITS}
    need(all(v['MainPID']=='0' and v['ActiveState']=='inactive' for v in states.values()),'apps not stopped cleanly')
    save('stopped.json',states)
    logs={}
    for name in APPS:
        path=Path('/var/log/project-mai-tai')/(name+'.log')
        if path.exists():
            st=path.stat()
            logs[str(path)]={'inode':st.st_ino,'offset':st.st_size}
    save('logs-before-boot.json',logs)


def archive_rdb():
    need(identity('redis-server.service')['ActiveState']=='inactive','Redis must be stopped before archival')
    path=Path('/var/lib/redis/dump.rdb')
    old=load('backups.json')[str(path)]
    need(sha(path)==old['sha256'],'offline RDB changed after backup; stop')
    destination=path.with_name('dump.rdb.pre-resize-20261003.offline')
    need(not destination.exists(),'RDB archive exists')
    os.rename(path,destination)
    need(not path.exists() and sha(destination)==old['sha256'],'RDB archival proof')
    save('PREPARED.json',{'boot_id':boot_id(),'application':target(),'at_utc':now().isoformat(),
         'offline_rdb':str(destination),'sha256':sha(destination),'persistence':'OFF'})


def new_boot():
    before=load('PREPARED.json')
    need(boot_id()!=before['boot_id'],'no reboot observed')
    et=now().astimezone(ZoneInfo('America/New_York'))
    need(et.date().isoformat()=='2026-10-03' and 1640<=int(et.strftime('%H%M'))<2359,'postboot window')
    need(git('rev-parse','HEAD')==before['application'] and not git('status','--porcelain'),'booted checkout drift')
    need(int(cmd('nproc'))==8,'resize did not yield 8 cores')
    mem_kib=int(re.search(r'^MemTotal:\s+(\d+)',Path('/proc/meminfo').read_text(),re.M)[1])
    need(mem_kib>=15_000_000,'resize did not yield approximately 16 GB')
    need(not Path('/var/lib/redis/dump.rdb').exists(),'stale RDB would have loaded')
    need(all(identity(u)['MainPID']=='0' for u in APP_UNITS),'apps started before owner bootstrap')
    r,s=client()
    with r:
        need(r.dbsize()==0,'Redis NOT empty; no flush or overwrite authorized')
        state=safety(r,expected=0)
        config=persistence(r)
    save('postboot-claim.json',{'boot_id':boot_id(),'at_utc':now().isoformat(),'redis':state,
         'persistence':config,'nproc':8,'free_m':cmd('free','-m'),'mem_kib':mem_kib})


def replay_owners():
    saved=load('owners-final.json')
    validate_replay(saved)
    r,s=client()
    with r:
        need(r.dbsize()==0,'Redis moved before bootstrap')
        pipe=r.pipeline()
        pipe.watch(s.redis_stream_prefix+':market-data-subscriptions',s.redis_stream_prefix+':market-data-subscription-owners')
        need(not pipe.exists(s.redis_stream_prefix+':market-data-subscriptions') and
             not pipe.exists(s.redis_stream_prefix+':market-data-subscription-owners'),'owner bootstrap would overwrite')
        pipe.multi()
        for event in saved['events']:
            bounded(event['fields'],1024**2,'preserved replace')
            body=json.loads(event['fields']['data'])['payload']
            need(body['mode']=='replace' and body['consumer_name'] in OWNER_NAMES,'bad preserved action')
            pipe.xadd(s.redis_stream_prefix+':market-data-subscriptions',event['fields'])
        ids=pipe.execute()
        state=safety(r,expected=0)
    save('owner-replay.json',{'source_ids':[e['source_id'] for e in saved['events']],
         'new_ids':ids,'raw_fields_unchanged':True,'redis':state})


def validate_replay(saved):
    need(set(saved['sets'])==OWNER_NAMES and len(saved['events'])==5,'five raw replace events required')
    seen=set()
    last=(0,0)
    for event in saved['events']:
        bounded(event['fields'],1024**2,'preserved replace')
        p=json.loads(event['fields']['data'])['payload']
        name=p['consumer_name']
        need(name in OWNER_NAMES and name not in seen and p['mode']=='replace','duplicate/unknown/non-replace owner')
        need(sorted(p['symbols'])==saved['sets'][name],'replay changed subscription set')
        eid=tuple(map(int,event['source_id'].split('-')))
        need(eid>last,'replay not in source-ID order')
        last=eid
        seen.add(name)
    need(seen==OWNER_NAMES and saved['sets']['momentum-paper']==[],'paper must stay unsubscribed Saturday')


def fleet():
    rows={u:identity(u) for u in APP_UNITS+INFRA}
    need(all(active(v) for v in rows.values()),'fleet not active/running, NRestarts=0: '+json.dumps(rows))
    before=load('before.json')['services']
    need(all(rows[u]['InvocationID']!=before[u]['InvocationID'] for u in rows),'preboot invocation survived?')
    need(identity('project-mai-tai-tv-alerts.service')['MainPID']=='0','tv-alerts intentionally inactive')
    for path,old in load('logs-before-boot.json').items():
        log=Path(path)
        need(log.stat().st_ino==old['inode'] and log.stat().st_size>=old['offset'],'log rotated during boot '+path)
        with log.open('rb') as f:
            f.seek(old['offset'])
            data=f.read(20*1024**2+1)
        bounded(data.decode(errors='replace'),20*1024**2,'postboot log tail')
        need(b'Traceback (most recent call last):' not in data,'new traceback '+path)
    return rows


def content():
    anchor=load('recovery-proof-bound.json') if (RUN/'recovery-proof-bound.json').exists() else load('gateway-start-bound.json')
    lower=datetime.fromisoformat(anchor['content_after_utc'])
    saved=load('owners-final.json')
    remaining=180-(now()-lower).total_seconds()
    need(0<remaining<=180,'content proof deadline already expired')
    deadline=time.monotonic()+remaining
    cursor=f'{int(lower.timestamp()*1000)}-18446744073709551615'
    intervals=[]
    millis=[]
    max_bytes=0
    r,s=client()
    with r:
        while time.monotonic()<deadline:
            safety(r,expected=0)
            # Cold boot may still be in reference-data initialization. Missing
            # fields are pending, never PASS; an eviction is checked first.
            fields=r.hkeys(s.redis_stream_prefix+':market-data-subscription-owners')
            bounded(fields,16384,'owner readiness HKEYS')
            need(set(fields)<=OWNER_NAMES|{'_migration_complete','_last_applied_id'},'unknown owner field')
            if set(fields)!=OWNER_NAMES|{'_migration_complete','_last_applied_id'}:
                print('CONTENT owners=PENDING cold_restore',flush=True)
                time.sleep(1)
                continue
            raw,sets=owners(r,s.redis_stream_prefix)
            need(sets==saved['sets'],'postboot owner sets differ; no repair loop')
            union=set(saved['static']).union(*(set(x) for x in sets.values()))
            hb_rows=r.xrevrange(s.redis_stream_prefix+':heartbeats',count=25)
            bounded(hb_rows,1024**2,'heartbeat COUNT25')
            hb=None
            for _,fields in hb_rows:
                event=json.loads(fields['data'])
                if event.get('source_service')=='market-data-gateway':
                    hb=event
                    break
            stamp=datetime.fromisoformat(hb['produced_at'].replace('Z','+00:00')) if hb else None
            healthy=bool(hb and stamp>lower and 0<=(now()-stamp).total_seconds()<30 and
                         hb['payload']['status']=='healthy' and int(hb['payload']['details']['active_symbols'])==len(union))
            for _ in range(3):
                batch=r.xrange(s.redis_stream_prefix+':snapshot-batches',min='('+cursor,count=1)
                max_bytes=max(max_bytes,bounded(batch,20*1024**2,'snapshot COUNT1'))
                if not batch:
                    break
                eid=batch[0][0]
                need(tuple(map(int,eid.split('-')))>tuple(map(int,cursor.split('-'))),'snapshot ID backwards')
                cursor=eid
                ms=int(eid.split('-')[0])
                del batch
                if millis:
                    intervals.append((ms-millis[-1])/1000)
                millis.append(ms)
            else:
                raise RuntimeError('STOP: snapshot backlog >3 reads/second')
            redis_state=safety(r,expected=0)
            p95=sorted(intervals)[math.ceil(len(intervals)*.95)-1] if intervals else None
            print('CONTENT',json.dumps({'healthy':healthy,'union':sorted(union),'intervals':len(intervals),
                  'p95':p95,'owners':sets,'redis':redis_state,'max_snapshot_bytes':max_bytes}),flush=True)
            if healthy and len(intervals)>=20 and p95<=10:
                rows=fleet()
                save('content.json',{'heartbeat':hb,'union':sorted(union),'owners':raw,'intervals':intervals,
                     'p95':p95,'redis':redis_state,'services':rows,'max_snapshot_bytes':max_bytes,
                     'ticks':'UNEXERCISED weekend; Monday 03:40/04:00/07:10'})
                return
            time.sleep(1)
    raise RuntimeError('STOP: content proof not established within 180s; no automatic restart')


def gates():
    app=target()
    hashes={}
    for name in ('expected_flags_check.py','expected_flags.json','expected_numeric.json','v2_restart_evidence.py'):
        path=Path('/home/trader/restart_evidence')/name
        data=subprocess.check_output(['sudo','-u','trader','git','-C',str(REPO),'show',app+':ops/health/'+name])
        atomic(path,data)
        need(path.read_bytes()==data,'isolated gate hash')
        hashes[name]=sha(path)
    checker=Path('/home/trader/restart_evidence/expected_flags_check.py')
    full=cmd(str(REPO/'.venv/bin/python'),str(checker),'--catalog',str(checker.with_name('expected_flags.json')),
             '--numeric-catalog',str(checker.with_name('expected_numeric.json')),timeout=180)
    print(full,flush=True)
    need('Final call: PASS; checked=134/134' in full,'FLAGGATE denominator/verdict')
    ns=runpy.run_path(str(checker))
    rc,lines=ns['audit'](ns['load_numeric_catalog'](checker.with_name('expected_numeric.json')))
    numeric='\n'.join(lines)
    print(numeric,flush=True)
    need(rc==0 and 'Final call: PASS; checked=8/8' in numeric,'numeric gate')
    save('gates.json',{'hashes':hashes,'full':full,'numeric':numeric})


def repin():
    rows=fleet()
    app=target()
    need(sha(PREOPEN)==PINS[PREOPEN] and sha(MONDAY)==PINS[MONDAY],'pin files changed; no silent overwrite')
    snapshot=load('before-reboot.json')
    actions={name:('deliberately_untouched' if name=='tv-alerts' else 'restarted') for name in snapshot['services']}
    save('install-record.json',{'schema_version':1,'snapshot_captured_at_utc':snapshot['captured_at_utc'],
         'source_journal':str(JOURNAL),'service_actions':actions})
    text=PREOPEN.read_text()
    values={'EXPECTED_SHA':app,'EXPECTED_DATE':'2026-10-05',
            'SNAPSHOT':str(RUN/'before-reboot.json'),'INSTALL_RECORD':str(RUN/'install-record.json')}
    for name,prefix in [('schwab-1m-v2','EXPECTED'),('oms','EXPECTED_OMS'),('strategy','EXPECTED_STRATEGY'),
                         ('orb','EXPECTED_ORB'),('orb-schwab','EXPECTED_ORB_SCHWAB'),('market-data','EXPECTED_MARKET_DATA')]:
        row=rows[f'project-mai-tai-{name}.service']
        values[prefix+'_PID']=row['MainPID']
        values[prefix+'_START']=row['ExecMainStartTimestamp']
    for key,value in values.items():
        text,n=re.subn('^'+key+r'=.*$',key+'='+shlex.quote(value),text,flags=re.M)
        need(n==1,'preopen pin replacement '+key)
    old_args=re.findall(r'^  --(?:restarted|new-service) [^\n]+\\\n',text,re.M)
    need(len(old_args)==6,'unexpected preopen classification block')
    new_args=''.join(f'  --restarted {name} \\\n' for name,action in sorted(actions.items()) if action=='restarted')
    for item in old_args:
        text=text.replace(item,new_args if item==old_args[0] else '',1)
    monday=MONDAY.read_text()
    gateway=rows['project-mai-tai-market-data.service']
    paper=rows['project-mai-tai-momentum-paper.service']
    replacements={f"APPROVED_SHA='{BASE}'":f"APPROVED_SHA='{app}'",
        "PAPER_PID='2477935'":f"PAPER_PID='{paper['MainPID']}'",
        "PAPER_START='Fri 2026-10-02 11:20:32 UTC'":f"PAPER_START='{paper['ExecMainStartTimestamp']}'",
        "value['MainPID'] == '2346625'":f"value['MainPID'] == '{gateway['MainPID']}'",
        "value['ExecMainStartTimestamp'] == 'Thu 2026-10-01 21:01:04 UTC'":
            f"value['ExecMainStartTimestamp'] == '{gateway['ExecMainStartTimestamp']}'",
        "Path('/proc/2346625/environ')":f"Path('/proc/{gateway['MainPID']}/environ')"}
    for old,new in replacements.items():
        need(monday.count(old)==1,'Monday pin replacement '+old)
        monday=monday.replace(old,new)
    for name,data in [('preopen',text),('monday',monday)]:
        with (RUN/(name+'.candidate')).open('x') as out:
            out.write(data)
        cmd('bash','-n',str(RUN/(name+'.candidate')))
    atomic(PREOPEN,text.encode())
    atomic(MONDAY,monday.encode())
    # New reviewed checker runs as root; its raw evidence files may remain root-only.
    cmd('systemctl','enable','project-mai-tai-option-a-guard-start-20261005.timer')
    save('repin.json',{'date':'2026-10-05','sha':app,'preopen_sha256':sha(PREOPEN),
         'monday_sha256':sha(MONDAY),'services':rows,
         'timer_next':cmd('systemctl','show','project-mai-tai-option-a-guard-start-20261005.timer','-p','NextElapseUSecRealtime','--value')})
    print(f'PREOPEN_REPIN date=2026-10-05 sha={app} all_six_service_PID/start_pairs=new; Monday_gateway_and_paper=new')


def closeout():
    rows=fleet()
    r,s=client()
    with r:
        memory=safety(r,expected=0)
        persistence(r)
        need(owners(r,s.redis_stream_prefix)[1]==load('owners-final.json')['sets'],'owner drift at closeout')
    # Restore normal retry policies only AFTER all first-boot evidence is good.
    for record in load('boot-setup.json')['dropins']:
        path=Path(record['path'])
        need(sha(path)==record['sha256'],'temporary drop-in changed')
        path.unlink()
    cmd('systemctl','daemon-reload')
    # Durable normal-boot linkage, missing on the pre-resize box; no restart.
    cmd('systemctl','add-wants','project-mai-tai.target','project-mai-tai-orb-schwab.service')
    cmd('systemctl','add-wants','multi-user.target','project-mai-tai.target')
    cmd('systemctl','disable','project-mai-tai-resize-postboot-20261003.service')
    need(fleet()==rows,'service changed during closeout')
    save('COMPLETE.json',{'at_utc':now().isoformat(),'boot_id':boot_id(),'services':rows,'redis':memory,
         'application':target(),'nproc':cmd('nproc'),'free_m':cmd('free','-m'),
         'delivery':'UNEXERCISED weekend; Monday assigned proof window'})


def recovery_claim(unit):
    need(unit in APP_UNITS,'recovery excludes Redis/Postgres/unknown units')
    need((RUN/'postboot-claim.json').exists() and not (RUN/'COMPLETE.json').exists(),'not an incomplete postboot')
    rows={u:identity(u) for u in APP_UNITS}
    need(not active(rows[unit]) and rows[unit]['MainPID']=='0','not a stopped/failed unit')
    need(all(active(v) for u,v in rows.items() if u!=unit),'more than one app failed; no multi-unit retry')
    r,_=client()
    with r:
        memory=safety(r,expected=0)
    save('recovery-claim.json',{'unit':unit,'before':rows,'at_utc':now().isoformat(),'redis':memory})


def recovery_result(unit):
    need(load('recovery-claim.json')['unit']==unit,'recovery unit mismatch')
    row=identity(unit)
    need(active(row),'one recovery start failed; page, no retry')
    save('recovery-result.json',{'unit':unit,'after':row,'at_utc':now().isoformat()})
    save('recovery-proof-bound.json',{'content_after_utc':now().isoformat(),'recovered_unit':unit})


def journal(result):
    with JOURNAL.open('a') as out:
        out.write(f'\n## Saturday resize {now().isoformat()} {result}\n')
        out.write(f'Plan {json.loads((ROOT/"release.json").read_text())["plan_commit"]}; raw `{RUN}`.\n')
        for path in sorted(RUN.glob('*.json')):
            out.write(f'- {path.name}: sha256={sha(path)} bytes={path.stat().st_size}\n')
        if (RUN/'COMPLETE.json').exists():
            out.write('\n```json\n'+(RUN/'COMPLETE.json').read_text()+'\n```\n')
        out.flush()
        os.fsync(out.fileno())


if __name__=='__main__':
    actions={'before':before,'flat':flat,'backups':backups,'ready':ready,'receipt':receipt,
             'target':lambda:print(target()),'detectors-env':detectors_env,'boot-setup':boot_setup,
             'quiesce-ready':quiesce_ready,'stopped':stopped,'drain':drain,
             'idle-gates':idle_gates,
             'archive-intents':lambda:save(sys.argv[2],archive_intents()),
             'review-evidence':lambda:review_evidence(Path(sys.argv[2])),
             'preserve-final-owners':lambda:save('owners-final.json',capture_owners()),
             'archive-rdb':archive_rdb,'new-boot':new_boot,'replay-owners':replay_owners,
             'gateway-start-bound':lambda:save('gateway-start-bound.json',{'content_after_utc':now().isoformat()}),
             'fleet':lambda:print(json.dumps(fleet(),sort_keys=True)),'content':content,'gates':gates,
             'repin':repin,'closeout':closeout,'recovery-claim':lambda:recovery_claim(sys.argv[2]),
             'recovery-result':lambda:recovery_result(sys.argv[2]),
             'recovered':lambda:need((RUN/'recovery-result.json').exists(),'no successful recovery'),
             'journal':lambda:journal(sys.argv[2])}
    try:
        if sys.argv[1] not in {'flat','idle-gates','review-evidence','fleet'}:
            verify()
        actions[sys.argv[1]]()
    except Exception as exc:
        print(type(exc).__name__,str(exc),flush=True)
        raise SystemExit(2)
