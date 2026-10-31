"""New trader-only current/rotation route transport; no real logs or providers."""
from datetime import datetime,timezone
from pathlib import Path
from types import SimpleNamespace
import hashlib
import importlib
import importlib.util
import json
import sys

import pytest

ROOT=Path(__file__).resolve().parents[2]
NOW=datetime(2026,10,28,22,tzinfo=timezone.utc)
MIDNIGHT=datetime(2026,10,29,0,5,tzinfo=timezone.utc)
EPOCH={'original_log_clock':'UTC_DEFAULT_LOGGING_LOCALTIME_VERIFIED','CONTROLLED':True}


@pytest.fixture
def route():
    spec=importlib.util.spec_from_file_location('controlled_direct_route',ROOT/'tools/atrsimlive1_route_log.py')
    module=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def line(stamp='2026-10-28 13:02:00,000',broker='webull'):
    return(stamp+' [ATR-COUNTRY-SELECTED] symbol=ELPW segment=1793192520000 route='+broker+
           ' reason=sec_unknown status=INCOMPLETE as_of=2026-10-28T12:00:00+00:00 notional=300\n').encode()


def written(tmp_path,raw):
    path=tmp_path/'schwab-1m-v2.log'
    path.write_bytes(raw)
    path.chmod(0o600)
    return path


def test_current_snapshot_binds_original_bytes_and_only_today(route,tmp_path):
    raw=line('2026-10-27 13:02:00,000')+line()+line('2026-10-29 13:02:00,000')
    path=written(tmp_path,raw)
    packet,status=route.read_today_routes(base=path,now=NOW,epoch=EPOCH)
    assert status=='BOUND_DIRECT_TODAY_LOG_READSET' and len(packet['lines'])==1
    assert packet['lines'][0]['line_number']==2
    assert packet['source']['sha256']==hashlib.sha256(raw).hexdigest()
    assert packet['lines'][0]['source_artifact']==str(path) and path.read_bytes()==raw


def test_after_utc_midnight_reads_rotation_date_and_current_for_same_et_day(route,tmp_path):
    path=written(tmp_path,line('2026-10-29 00:02:00,000'))
    rotated=path.with_name(path.name+'-20261029')
    rotated.write_bytes(line())
    rotated.chmod(0o600)
    packet,status=route.read_today_routes(base=path,now=MIDNIGHT,epoch=EPOCH)
    assert packet['trading_day']=='2026-10-28' and len(packet['lines'])==2
    assert [x['artifact']for x in packet['source']['epoch']['readset_artifacts']]==[str(rotated),str(path)]
    assert status=='BOUND_DIRECT_TODAY_LOG_READSET'


def test_next_et_day_never_reuses_prior_day_selected_lines(route,tmp_path):
    path=written(tmp_path,line()+line('2026-10-29 13:02:00,000'))
    packet,_=route.read_today_routes(base=path,now=datetime(2026,10,29,14,tzinfo=timezone.utc),epoch=EPOCH)
    assert packet['trading_day']=='2026-10-29' and len(packet['lines'])==1
    assert packet['lines'][0]['recorded_at'].startswith('2026-10-29')


def test_each_invocation_rereads_without_persistent_projection(route,tmp_path):
    path=written(tmp_path,line())
    first,_=route.read_today_routes(base=path,now=NOW,epoch=EPOCH)
    path.write_bytes(b'ordinary line\n')
    second,_=route.read_today_routes(base=path,now=NOW,epoch=EPOCH)
    assert len(first['lines'])==1 and second['lines']==[]
    assert first['source']['sha256']!=second['source']['sha256']


def test_no_sources_grants_no_route(route,tmp_path):
    packet,status=route.read_today_routes(base=tmp_path/'absent.log',now=NOW,epoch=EPOCH)
    assert packet is None and status=='ROUTE_SOURCES_ABSENT'


def test_symlink_is_not_a_route_capability(route,tmp_path):
    path=written(tmp_path,line())
    link=tmp_path/'link'
    link.symlink_to(path)
    assert route.read_today_routes(base=link,now=NOW,epoch=EPOCH)[0]is None


def test_group_writable_log_refuses(route,tmp_path):
    path=written(tmp_path,line())
    path.chmod(0o620)
    assert route.read_today_routes(base=path,now=NOW,epoch=EPOCH)[0]is None


def test_unknown_clock_and_naive_asof_never_grant_route(route,tmp_path):
    path=written(tmp_path,line())
    assert route.read_today_routes(base=path,now=NOW,epoch={})[0]is None
    assert route.read_today_routes(base=path,now=NOW.replace(tzinfo=None),epoch=EPOCH)[0]is None


def test_malformed_or_partial_country_lines_refuse(route,tmp_path):
    path=written(tmp_path,b'[ATR-COUNTRY-SELECTED] symbol=ELPW\n')
    assert route.read_today_routes(base=path,now=NOW,epoch=EPOCH)[0]is None
    path.write_bytes(line().rstrip(b'\n'))
    assert route.read_today_routes(base=path,now=NOW,epoch=EPOCH)[0]is None


def test_total_read_byte_cap_is_not_raised_for_rotation(route,tmp_path,monkeypatch):
    path=written(tmp_path,line())
    rotated=path.with_name(path.name+'-20261029')
    rotated.write_bytes(line('2026-10-28 13:03:00,000'))
    rotated.chmod(0o600)
    monkeypatch.setattr(route,'MAX_BYTES',len(path.read_bytes())+5)
    assert route.read_today_routes(base=path,now=MIDNIGHT,epoch=EPOCH)[0]is None


def test_selected_count_and_packet_caps_refuse(route,tmp_path,monkeypatch):
    path=written(tmp_path,line()+line('2026-10-28 13:03:00,000'))
    monkeypatch.setattr(route,'MAX_LINES',1)
    assert route.read_today_routes(base=path,now=NOW,epoch=EPOCH)[0]is None
    monkeypatch.setattr(route,'MAX_LINES',30000)
    monkeypatch.setattr(route,'MAX_PACKET',10)
    assert route.read_today_routes(base=path,now=NOW,epoch=EPOCH)[0]is None


def test_unreadable_existing_rotation_is_not_silently_ignored(route,tmp_path,monkeypatch):
    path=written(tmp_path,line())
    rotated=path.with_name(path.name+'-20261029')
    rotated.write_bytes(line())
    original=route.os.open
    def denied(name,*args,**kwargs):
        if Path(name)==rotated:
            raise PermissionError('CONTROLLED_NO_REAL_LOG')
        return original(name,*args,**kwargs)
    monkeypatch.setattr(route.os,'open',denied)
    assert route.read_today_routes(base=path,now=MIDNIGHT,epoch=EPOCH)[0]is None


def test_copytruncate_during_read_refuses_without_retry(route,tmp_path,monkeypatch):
    path=written(tmp_path,line())
    original=route.os.fstat
    calls=[]
    def changed(fd):
        value=original(fd)
        calls.append(fd)
        return value if len(calls)==1 else SimpleNamespace(st_size=0,st_dev=value.st_dev,st_ino=value.st_ino)
    monkeypatch.setattr(route.os,'fstat',changed)
    assert route.read_today_routes(base=path,now=NOW,epoch=EPOCH)[0]is None
    assert len(calls)==2


def test_missing_optional_rotation_can_use_still_unrotated_current(route,tmp_path):
    path=written(tmp_path,line())
    packet,_=route.read_today_routes(base=path,now=MIDNIGHT,epoch=EPOCH)
    assert len(packet['lines'])==1 and len(packet['source']['epoch']['readset_artifacts'])==1


def test_copy_boundary_duplicate_is_reported_not_invented_twice(route,tmp_path):
    path=written(tmp_path,line())
    rotated=path.with_name(path.name+'-20261029')
    rotated.write_bytes(line())
    rotated.chmod(0o600)
    packet,_=route.read_today_routes(base=path,now=MIDNIGHT,epoch=EPOCH)
    assert len(packet['lines'])==1 and packet['source']['epoch']['duplicate_copy_records']==1


def controlled_cli(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT/'tools'))
    cli=importlib.import_module('atrsimlive1')
    seen=[]
    monkeypatch.setattr(cli,'source_binding',lambda path:{'head':'a'*40})
    def collect(*,dsn,processor,capture_log_path,working_policy_path,null_policy):
        assert dsn == 'CONTROLLED_NO_DB'
        assert working_policy_path is None and null_policy is None
        return[processor({'symbol':s})for s in('CHSN','ELPW')]
    def produce(data,*,policy_path,producer,route_packet,same_sip_policy,null_policy):
        assert policy_path is None and same_sip_policy is None and null_policy is None
        seen.append(route_packet)
        return data
    monkeypatch.setitem(sys.modules,'atrsimlive1_collect',SimpleNamespace(collect=collect,dsn_from_environment=lambda name:'CONTROLLED_NO_DB',encoded=lambda value:json.dumps(value).encode()))
    monkeypatch.setitem(sys.modules,'atrsimlive1_models',SimpleNamespace(produce=produce,combine=lambda rows:{'rows':rows}))
    return cli,seen


def test_normal_cli_reads_direct_once_for_all_symbols(monkeypatch,capsys):
    cli,seen=controlled_cli(monkeypatch)
    calls=[]
    packet={'CONTROLLED':True}
    monkeypatch.setattr(cli,'read_today_routes',lambda:(calls.append('read')or(packet,'BOUND_DIRECT_TODAY_LOG_READSET')))
    assert cli.main(['--source-manifest','CONTROLLED','--dsn-env','MAI_TAI_DATABASE_URL','--child'])==0
    assert calls==['read'] and seen==[packet,packet]
    assert len(json.loads(capsys.readouterr().out)['rows'])==2


def test_partial_explicit_binding_never_falls_back_to_live_log(monkeypatch,capsys):
    cli,seen=controlled_cli(monkeypatch)
    monkeypatch.setattr(cli,'read_today_routes',lambda:pytest.fail('no alternative source'))
    assert cli.main(['--source-manifest','CONTROLLED','--dsn-env','MAI_TAI_DATABASE_URL','--child','--country-route-sha256','a'*64])==0
    assert seen==[None,None]
    capsys.readouterr()


def test_timer_unit_is_trader_end_to_end_without_rejected_root_paths():
    unit=(ROOT/'ops/systemd/project-mai-tai-atr-sim-today.service').read_text()
    assert 'User=trader\nGroup=trader\n'in unit
    assert '/usr/local/libexec/'not in unit and 'route-runs'not in unit
    assert all(line in unit for line in('NoNewPrivileges=true','ProtectSystem=strict','ProtectHome=read-only','MemoryMax=2G','TimeoutStartSec=150','TimeoutStopSec=4','Nice=19','IOSchedulingClass=idle','Restart=no'))
    assert 'RuntimeMaxSec=260' not in unit
