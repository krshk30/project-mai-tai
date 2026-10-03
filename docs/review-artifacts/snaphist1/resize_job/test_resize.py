"""Offline tests: no SSH, brokers, Redis writes, or systemd calls."""
import copy
import json
import sys
from datetime import datetime
from pathlib import Path

import pytest

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
import resize as r
import review_gate as gate


def replay():
    events = []
    sets = {name: [] for name in sorted(r.OWNER_NAMES)}
    sets['strategy-engine'] = ['TEST']
    for i, (name, symbols) in enumerate(sets.items(), 1):
        fields = {'data': json.dumps({'payload': {'consumer_name': name, 'mode': 'replace', 'symbols': symbols}})}
        events.append({'source_id': f'{i}-0', 'fields': fields, 'symbols': symbols})
    return {'sets': sets, 'events': events}


def test_replay_accepts_five_unchanged_source_ordered_replaces():
    data = replay()
    before = copy.deepcopy(data)
    r.validate_replay(data)
    assert data == before


@pytest.mark.parametrize('mutation', ['missing', 'duplicate', 'add', 'reordered', 'changed_symbols', 'paper_symbols'])
def test_replay_refuses_any_changed_or_missing_owner(mutation):
    data = replay()
    if mutation == 'missing':
        data['events'].pop()
    elif mutation == 'duplicate':
        data['events'][1]['fields'] = data['events'][0]['fields']
    elif mutation == 'reordered':
        data['events'].reverse()
    else:
        index = next(i for i,e in enumerate(data['events']) if
                     json.loads(e['fields']['data'])['payload']['consumer_name'] == 'momentum-paper')
        payload = json.loads(data['events'][index]['fields']['data'])
        if mutation == 'add':
            payload['payload']['mode'] = 'add'
        else:
            payload['payload']['symbols'] = ['TEST']
            if mutation == 'paper_symbols':
                data['sets']['momentum-paper'] = ['TEST']
        data['events'][index]['fields']['data'] = json.dumps(payload)
    with pytest.raises(RuntimeError, match='STOP:'):
        r.validate_replay(data)


class RedisSafety:
    def __init__(self, memory=1_000_000, evictions=0):
        self.memory, self.evictions = memory, evictions

    def info(self, kind):
        return {'used_memory': self.memory} if kind == 'memory' else {'evicted_keys': self.evictions}


@pytest.mark.parametrize('memory,evictions', [(1_600_000_001, 0), (1_000_000, 1)])
def test_redis_margin_or_eviction_stops(memory, evictions):
    with pytest.raises(RuntimeError, match='STOP: Redis'):
        r.safety(RedisSafety(memory, evictions), expected=0)


def test_redis_safe_read_prints_measured_values():
    assert r.safety(RedisSafety(), expected=0) == {'used_memory': 1_000_000, 'evicted_keys': 0}


def test_reply_bound_cannot_be_ignored():
    with pytest.raises(RuntimeError, match='bytes=5>4'):
        r.bounded({'x': '1234'}, 4, 'test')


def test_empty_owner_hash_is_not_ready():
    class Empty:
        def hkeys(self, _):
            return []
    with pytest.raises(RuntimeError, match='owner fields incomplete'):
        r.owners(Empty(), 'mai_tai')


def test_unknown_redis_key_is_a_stop():
    class Extra:
        def scan_iter(self, **kwargs):
            return iter(['mai_tai:symbol-block:current:live:orb:TEST'])
    with pytest.raises(RuntimeError, match='unknown Redis-only state'):
        r.inventory(Extra(), 'mai_tai')


@pytest.mark.parametrize('when', ['2026-10-03T10:00:00-04:00', '2026-10-05T03:40:00-04:00'])
def test_approval_cannot_execute_another_day(when):
    with pytest.raises(ValueError, match='Sunday-only'):
        gate.verify(now=datetime.fromisoformat(when))


def test_content_uses_post_start_microsecond_bound_and_absolute_180_seconds(monkeypatch):
    anchor = datetime.fromisoformat('2026-10-04T14:00:00.123456+00:00')
    monkeypatch.setattr(r, 'load', lambda name: {'content_after_utc': anchor.isoformat()} if 'bound' in name else {})
    monkeypatch.setattr(r, 'now', lambda: datetime.fromisoformat('2026-10-04T14:03:00.123457+00:00'))
    with pytest.raises(RuntimeError, match='deadline already expired'):
        r.content()


def test_rdb_archive_refuses_while_redis_running(monkeypatch):
    monkeypatch.setattr(r, 'identity', lambda _: {'ActiveState': 'active'})
    with pytest.raises(RuntimeError, match='Redis must be stopped'):
        r.archive_rdb()


def test_preboot_recovery_refuses(monkeypatch, tmp_path):
    monkeypatch.setattr(r, 'RUN', tmp_path)
    with pytest.raises(RuntimeError, match='not an incomplete postboot'):
        r.recovery_claim('project-mai-tai-oms.service')


@pytest.mark.parametrize('unit', ['redis-server.service', 'postgresql@16-main.service', 'unknown.service'])
def test_recovery_cannot_restart_infrastructure(monkeypatch, unit):
    with pytest.raises(RuntimeError, match='recovery excludes'):
        r.recovery_claim(unit)


def test_saving_evidence_never_overwrites(monkeypatch, tmp_path):
    monkeypatch.setattr(r, 'RUN', tmp_path)
    r.save('proof.json', {'first': 1})
    with pytest.raises(FileExistsError):
        r.save('proof.json', {'second': 2})
    assert json.loads((tmp_path/'proof.json').read_text()) == {'first': 1}


def test_snapshot_reader_is_single_entry_not_xinfo_or_eval():
    import ast
    tree = ast.parse((HERE/'resize.py').read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            assert node.func.attr not in {'xinfo_stream', 'eval', 'evalsha'}
            if node.func.attr in {'xrange', 'xrevrange'} and 'snapshot-batches' in ast.unparse(node):
                assert next(k.value.value for k in node.keywords if k.arg == 'count') == 1


def test_runner_never_reboots_or_restarts_gateway_as_retry():
    script = (HERE/'run_resize.sh').read_text()
    assert 'systemctl restart' not in script
    assert 'shutdown -' not in script
    assert 'systemctl reboot' not in script
    assert script.count('timeout 90s systemctl start "$UNIT"') == 1
    assert '--operator-override' not in script
    assert 'proof drain' in script
    assert script.index('proof drain') < script.index('systemctl stop redis-server.service')


def test_release_absent_refuses_before_execution(tmp_path):
    with pytest.raises(FileNotFoundError):
        gate.verify(root=tmp_path,now=datetime.fromisoformat('2026-10-04T09:30:00-04:00'))


def test_approval_requires_exact_reviewed_dispositions():
    import inspect
    source=inspect.getsource(gate.verify)
    assert "read(root/'approval.json') != expected" in source
    assert 'manual_stop_disposition' in source
    assert 'retained_intents' in source
    assert 'unknown_broker_orders' in source


def test_staging_does_not_write_approval_or_start_apps():
    source=(HERE/'stage_review.sh').read_text()
    assert 'test ! -e "$2/approval.json"' in source
    assert 'systemctl start' not in source
    assert 'systemctl restart' not in source
    assert 'systemctl enable --now project-mai-tai-resize-prepare-20261004.timer' in source


@pytest.mark.parametrize('broker',['Schwab','Webull'])
def test_known_order_blocks_but_unknown_orphan_does_not(broker):
    r.refuse_known_order(broker,{'orphan'},{'our-order'})
    with pytest.raises(RuntimeError,match='known order still live'):
        r.refuse_known_order(broker,{'our-order'},{'our-order'})


@pytest.mark.parametrize('changes',[False,True])
def test_archive_keeps_special_envelopes_without_inventing_ack(monkeypatch,changes):
    from types import SimpleNamespace
    class History:
        count_reads=0
        page_reads=0
        def __enter__(self): return self
        def __exit__(self,*args): pass
        def xlen(self,key):
            self.count_reads+=1
            return 1 if self.count_reads==1 else 1+int(changes)
        def xrange(self,key,**kwargs):
            assert kwargs['count']==25
            self.page_reads+=1
            return [('1-0',{'data':'{"confirmation_exit":true}'})] if self.page_reads==1 else []
    monkeypatch.setattr(r,'client',lambda:(History(),SimpleNamespace(redis_stream_prefix='mai_tai')))
    if changes:
        with pytest.raises(RuntimeError,match='archive changed'):
            r.archive_intents()
    else:
        result=r.archive_intents()
        assert result['count']==1 and result['stream_ids']==['1-0']
        assert result['entries'][0]['fields']['data']=='{"confirmation_exit":true}'
        assert 'never replay' in result['disposition']


def test_no_year_wide_history_or_complete_enumeration_assumption():
    import inspect
    source=inspect.getsource(r.direct_open_orders)
    assert 'timedelta(days=' not in source
    assert 'maxResults=500' in source
    assert 'list_open_orders_for_sync' in source
    assert 'not whole-history completeness' in source
