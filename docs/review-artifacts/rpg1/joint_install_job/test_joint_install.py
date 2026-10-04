"""Explicit local simulators only; never connect to brokers/Redis/systemd."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import policy as p
import review_gate as gate
import proofs
import install


AT = datetime.fromisoformat('2026-10-03T22:00:00-04:00')


def row(pid='42', **overrides):
    return {'MainPID': pid, 'ExecMainStartTimestamp': 'Sat 2026-10-03 22:03:17 UTC',
            'ActiveState': 'active', 'SubState': 'running', 'NRestarts': '0',
            'InvocationID': 'a' * 32, 'ExecMainCode': '0', 'ExecMainStatus': '0',
            'Result': 'success', 'KillSignal': '15', **overrides}


def release():
    value = json.loads((HERE / 'release.json').read_text())
    value['ready'] = True
    value['application'].update(approved_sha='1' * 40, tree='2' * 40, coldstart_merge_sha='3' * 40,
        commits=[value['application']['rpg_merge_sha'], '3' * 40, '1' * 40],
        changed_paths=['src/project_mai_tai/settings.py'], source_sha256={'src/project_mai_tai/settings.py': '4' * 64})
    value['plan'].update(commit='5' * 40, sha256='6' * 64)
    value['reviews'] = [{'pr': pr, 'head': '7' * 40, 'base': '8' * 40, 'record_commit': '9' * 40,
        'pin_path': 'records/' + '7' * 40 + f'/pr-{pr}--' + '8' * 40 + '--claude-1.json',
        'pin_sha256': 'a' * 64, 'ci_urls': [f'https://github.com/a/b/actions/runs/{pr}1',
                                          f'https://github.com/a/b/actions/runs/{pr}2']} for pr in (1085, 1088)]
    value['host'].update(captured_at=AT.isoformat(), services={name: row(str(i + 100)) for i, name in enumerate(p.SERVICES)},
        tv_alerts=row('0', ActiveState='inactive', SubState='dead'),
        files={path: {'sha256': p.FLAT_HASH if path == str(p.FLAT) else 'b' * 64,
                      'uid': 0, 'gid': 0, 'mode': 0o600} for path in gate.FILE_PATHS},
        units={name: 'c' * 64 for name in gate.UNIT_NAMES},
        subscription_inputs={name: {'symbols': [], 'evidence': 'simulator: independent empty-input proof',
                                    'sha256': 'd' * 64} for name in p.OWNERS},
        status_contract=deepcopy(gate.STATUS_CONTRACT), pager_url='https://ntfy.sh/mai-tai-preopen-28806a5a97b7')
    value['artifacts'] = {name: 'e' * 64 for name in p.ARTIFACTS}
    return value


def test_template_cannot_execute():
    with pytest.raises(p.Refusal, match='NOT READY'):
        gate.validate_release(json.loads((HERE / 'release.json').read_text()))


def test_complete_release_schema_and_ledger_paths():
    gate.validate_release(release())


@pytest.mark.parametrize('change', [
    lambda r: r.update(ready=False),
    lambda r: r['application'].update(approved_sha=None),
    lambda r: r['application'].update(commits=[]),
    lambda r: r['application'].update(source_sha256={}),
    lambda r: r['application'].update(changed_paths=['../src/evil']),
    lambda r: r['denominators'].update(combined=136),
    lambda r: r['denominators'].update(boolean=128),
    lambda r: r['host']['services'].pop('control'),
    lambda r: r['host']['services']['oms'].update(NRestarts='1'),
    lambda r: r['host']['files'].pop(str(p.MONDAY)),
    lambda r: r['host']['status_contract']['nfq'].append('enqueue_uncertain'),
    lambda r: r['host']['subscription_inputs']['momentum-paper'].update(symbols=['AMOD']),
    lambda r: r['host'].update(intent_ids=['1-1', '1-1']),
    lambda r: r['host'].update(pager_url='http://bad.example/token'),
    lambda r: r['artifacts'].pop('install.py'),
    lambda r: r['reviews'][0].update(pin_path='docs/fake.json'),
    lambda r: r['reviews'][0].update(ci_urls=['https://github.com/a/b/actions/runs/1']),
])
def test_unbound_release_refuses(change):
    value = release()
    change(value)
    with pytest.raises((p.Refusal, ValueError)):
        gate.validate_release(value)


def receipt():
    return {'schema_version': 1, 'reviewer': 'claude-1', 'decision': 'APPROVED',
            'release_sha256': 'f' * 64, 'window': p.WINDOW, 'dispositions': deepcopy(gate.DISPOSITIONS)}


@pytest.mark.parametrize('key', ['broker_inventory', 'idle', 'reset_failed', 'denominator'])
def test_receipt_requires_each_explicit_disposition(key):
    value = receipt()
    value['dispositions'][key] = 'approved'
    with pytest.raises(p.Refusal):
        gate.validate_approval(value, 'f' * 64)


def test_receipt_exact_release_hash_and_no_extra_fields():
    gate.validate_approval(receipt(), 'f' * 64)
    with pytest.raises(p.Refusal):
        gate.validate_approval(receipt(), '0' * 64)
    value = receipt()
    value['allow_override'] = True
    with pytest.raises(p.Refusal):
        gate.validate_approval(value, 'f' * 64)


@pytest.mark.parametrize('raw', ['{"ready":true,"ready":false}', '{"count":NaN}', '{"count":Infinity}'])
def test_ambiguous_json_refused(raw):
    with pytest.raises((p.Refusal, ValueError)):
        gate.unique_json(raw)


@pytest.mark.parametrize('instant,prepare,ok', [
    ('2026-10-03T18:00:00-04:00', True, True),
    ('2026-10-03T17:59:59-04:00', True, False),
    ('2026-10-03T23:00:00-04:00', True, True),
    ('2026-10-03T23:00:00.000001-04:00', True, False),
    ('2026-10-03T23:58:59-04:00', False, True),
    ('2026-10-03T23:59:00-04:00', False, True),
    ('2026-10-03T23:59:00.000001-04:00', False, False),
    ('2026-10-04T00:00:00-04:00', False, False),
])
def test_exact_tonight_window(instant, prepare, ok):
    if ok:
        p.window(datetime.fromisoformat(instant), prepare=prepare)
    else:
        with pytest.raises(p.Refusal):
            p.window(datetime.fromisoformat(instant), prepare=prepare)


class FakeHost:
    """Controlled state-machine simulator, not recorded production evidence."""
    def __init__(self, fail=None):
        self.calls = []
        self.fail = fail

    def __getattr__(self, name):
        def call(*args):
            event = (name, *args)
            self.calls.append(event)
            if event == self.fail:
                raise p.Refusal('controlled failure')
        return call


def test_real_coordinator_exact_sequence_flat_guard_before_each_action():
    host = FakeHost()
    install.Coordinator(host).run()
    expected = [('stop', name) for name in p.STOP] + [('restart', 'oms')] + [('start', name) for name in p.START]
    assert [x[1:] for x in host.calls if x[0] == 'service'] == expected
    for i, call in enumerate(host.calls):
        if call[0] == 'service':
            assert host.calls[i - 1] == ('before', *call[1:])
    assert host.calls[0:2] == [('initial',), ('prepare',)]
    assert host.calls[-1] == ('finalize',)
    assert host.calls.index(('drained',)) < host.calls.index(('service', 'restart', 'oms'))


@pytest.mark.parametrize('failed', [('initial',), ('prepare',), ('drained',), ('finalize',)] + [
    (method, action, name) for action, names in [('stop', p.STOP), ('restart', ('oms',)), ('start', p.START)]
    for name in names for method in ('before', 'service')])
def test_no_continuation_after_any_guard_or_action_failure(failed):
    host = FakeHost(failed)
    with pytest.raises(p.Refusal):
        install.Coordinator(host).run()
    assert host.calls[-1] == failed


@pytest.mark.parametrize('action', ['start', 'stop', 'restart', 'reset-failed', 'enable', 'reload'])
@pytest.mark.parametrize('name', p.UNTOUCHED)
def test_untouched_service_actions_impossible(action, name):
    with pytest.raises(p.Refusal):
        p.check_service_action(action, name)


def test_env_only_two_flags_and_preserves_other_bytes():
    original = '# numeric settings untouched\nMAI_TAI_OTHER=600.00\nexport ' + next(iter(p.FLAGS)) + '=false\n'
    updated, changes = install.env_update(original)
    assert updated.startswith('# numeric settings untouched\nMAI_TAI_OTHER=600.00\n')
    assert set(changes) == set(p.FLAGS)
    assert all(updated.count(key + '=true\n') == 1 for key in p.FLAGS)
    assert install.env_update(updated)[0] == updated


@pytest.mark.parametrize('original', [
    next(iter(p.FLAGS)) + '=false\n' + next(iter(p.FLAGS)).lower() + '=true\n',
    next(iter(p.FLAGS)).lower() + '=false\n',
    next(iter(p.FLAGS)) + '=false\nexport ' + next(iter(p.FLAGS)) + '=true\n',
])
def test_duplicate_and_case_conflicting_env_refuses(original):
    with pytest.raises(p.Refusal):
        install.env_update(original)


FLAT = "FRESH_DIRECT_READ schwab_holding_rows=0 webull_holding_rows=0 ok\nopen_managed={'live:schwab_1m_v2': 0, 'live:orb': 0}\nnonzero_virtual=[]\nnet_bot_fills=[]\n"


def test_strict_flat_zero_parser():
    assert proofs.parse_flat(FLAT)['manual_override'] is False


@pytest.mark.parametrize('bad', [
    FLAT.replace('schwab_holding_rows=0', 'schwab_holding_rows=1'),
    FLAT.replace('webull_holding_rows=0', 'webull_holding_rows=1'),
    FLAT.replace('nonzero_virtual=[]', "nonzero_virtual=['NXL']"),
    FLAT.replace('net_bot_fills=[]', "net_bot_fills=['manual exception']"),
    FLAT.replace("'live:orb': 0", "'live:orb': 1"),
    FLAT.replace('schwab_holding_rows=0 ', ''),
    FLAT + 'schwab_holding_rows=1 manual_override=1\n',
])
def test_helper_rc0_manual_exception_still_refuses(bad):
    with pytest.raises(p.Refusal):
        proofs.parse_flat(bad)


@pytest.mark.parametrize('raw', [b'$1000000000\r\n', b'*999999999\r\n', b'$5\r\nabc', b'+OK', b'-ERR refused\r\n', b'$-2\r\n'])
def test_resp_bounds_before_allocation_and_malformed(raw):
    with pytest.raises((p.Refusal, ValueError)):
        proofs.RESP(io.BytesIO(raw), 1024).read()


def test_resp_small_nested_envelope():
    assert proofs.RESP(io.BytesIO(b'*2\r\n$3\r\none\r\n:2\r\n'), 100).read() == ['one', 2]


@pytest.mark.parametrize('args', [('XADD', 'p:heartbeats', '*', 'data', '{}'),
                                ('FLUSHALL',), ('CONFIG', 'SET', 'save', ''),
                                ('XREVRANGE', 'p:snapshot-batches', '+', '-', 'COUNT', 1),
                                ('XRANGE', 'p:market-data-subscriptions', '-', '+', 'COUNT', 25)])
def test_redis_forbidden_reads_and_all_writes_refuse_before_connection(args, monkeypatch):
    monkeypatch.setattr(proofs.socket, 'create_connection', lambda *a, **k: pytest.fail('must not connect'))
    reader = proofs.RedisRead(SimpleNamespace(redis_url='redis://localhost/0', redis_stream_prefix='p'))
    with pytest.raises(p.Refusal):
        reader.call(*args)


class FakeRedis:
    def __init__(self, memory=500_000_000, evicted=0, owner=None):
        self.memory, self.evicted = memory, evicted
        self.prefix = 'p'
        self.owner = {**{name: '[]' for name in p.OWNERS}, '_migration_complete': '1', '_last_applied_id': '1-0'}
        self.owner.update(owner or {})
        self.calls = []

    def call(self, *args, **kwargs):
        self.calls.append(args)
        if args[0] == 'INFO':
            return f'used_memory:{self.memory}\r\nevicted_keys:{self.evicted}\r\n'
        if args[0] == 'CONFIG':
            return ['save', '', 'appendonly', 'no', 'maxmemory', '2147483648', 'maxmemory-policy', 'allkeys-lru']
        if args[0] == 'HLEN':
            return len(self.owner)
        if args[0] == 'HSTRLEN':
            return len(self.owner.get(args[2], ''))
        if args[0] == 'HGETALL':
            return [item for pair in self.owner.items() for item in pair]
        if args[0] == 'TYPE':
            return 'stream'
        raise AssertionError(args)


def test_owner_hlen_and_seven_hstrlen_precede_hgetall():
    reader = FakeRedis()
    value = proofs.redis_state(reader, evicted=0)
    assert set(value['sets']) == p.OWNERS
    names = [args[0] for args in reader.calls]
    assert names.index('HLEN') < names.index('HGETALL')
    assert names[:names.index('HGETALL')].count('HSTRLEN') == 7


@pytest.mark.parametrize('kwargs', [{'memory': 1_600_000_001}, {'evicted': 1},
                                   {'owner': {'_migration_complete': '0'}}, {'owner': {'_last_applied_id': 'unknown'}},
                                   {'owner': {'rogue': '[]'}}, {'owner': {'momentum-paper': '["AMOD"]'}},
                                   {'owner': {'orb': '["AMOD","AMOD"]'}}, {'owner': {'orb': 'x' * 1_000_000}}])
def test_redis_unsafe_state_refuses(kwargs):
    with pytest.raises((p.Refusal, ValueError)):
        proofs.redis_state(FakeRedis(**kwargs), evicted=0)


def test_recording_paper_fake_only_empty_replace():
    import asyncio
    fake = proofs.RecordOnlyRedis()
    value = {'source_service': 'momentum-paper', 'payload': {'consumer_name': 'momentum-paper', 'mode': 'replace', 'symbols': []}}
    asyncio.run(fake.xadd('p:market-data-subscriptions', {'data': json.dumps(value)}))
    assert len(fake.calls) == 1
    value['payload']['symbols'] = ['AMOD']
    with pytest.raises(p.Refusal):
        asyncio.run(fake.xadd('p:market-data-subscriptions', {'data': json.dumps(value)}))
    with pytest.raises(p.Refusal):
        fake.publish('something')


CANCEL = 'Traceback (most recent call last):\n  File "/venv/bin/mai-tai-orb-schwab", line 8, in simulated_run\nasyncio.exceptions.CancelledError\n'
JOURNAL = '\n'.join(json.dumps({'INVOCATION_ID': 'a' * 32, 'UNIT': p.unit('orb-schwab'), 'MESSAGE': message})
                    for message in ('Stopping project-mai-tai-orb-schwab.service',
                                    'Main process exited, code=exited, status=1/FAILURE'))


@pytest.fixture
def simulated_known_cancel_stack(monkeypatch):
    # Deliberately NOT a recorded/approved production fingerprint.
    monkeypatch.setattr(install, 'KNOWN_CANCELLED_STACKS', ((('mai-tai-orb-schwab', 'simulated_run'),),))


def test_only_narrow_exit1_cancelled_stop_can_reset(simulated_known_cancel_stack):
    stopped = row('0', ActiveState='failed', SubState='failed', ExecMainCode='1', ExecMainStatus='1', Result='exit-code')
    install.narrow_cancelled_stop(row(), stopped, CANCEL, JOURNAL, stop_issued=True)


@pytest.mark.parametrize('change', [
    {'MainPID': '88'}, {'ExecMainStatus': '2'}, {'ExecMainCode': '2'}, {'Result': 'timeout'},
    {'InvocationID': 'new'}, {'NRestarts': '1'}, {'ActiveState': 'active'},
])
def test_reset_never_generalizes_to_other_failed_state(change, simulated_known_cancel_stack):
    stopped = row('0', ActiveState='failed', SubState='failed', ExecMainCode='1', ExecMainStatus='1', Result='exit-code')
    stopped.update(change)
    with pytest.raises(p.Refusal):
        install.narrow_cancelled_stop(row(), stopped, CANCEL, JOURNAL, stop_issued=True)


@pytest.mark.parametrize('tail,journal,issued', [(CANCEL, JOURNAL, False),
    (CANCEL.replace('CancelledError', 'RuntimeError'), JOURNAL, True), (CANCEL + CANCEL, JOURNAL, True),
    (CANCEL, JOURNAL + ' SIGKILL', True), (CANCEL, 'exit status1', True)])
def test_reset_requires_this_sigterm_and_exact_cancelled_trace(tail, journal, issued, simulated_known_cancel_stack):
    stopped = row('0', ActiveState='failed', SubState='failed', ExecMainCode='1', ExecMainStatus='1', Result='exit-code')
    with pytest.raises((p.Refusal, ValueError)):
        install.narrow_cancelled_stop(row(), stopped, tail, journal, stop_issued=issued)


def test_review_counterexample_unrelated_error_unknown_cancel_stack():
    tail = ('ERROR unexpected fatal database failure\nTraceback (most recent call last):\n'
            '  File "mai-tai-orb-schwab", line 123, in unrelated_path\nasyncio.exceptions.CancelledError\n')
    stopped = row('0', ActiveState='failed', SubState='failed', ExecMainCode='1', ExecMainStatus='1', Result='exit-code')
    with pytest.raises(p.Refusal, match='unrelated'):
        install.narrow_cancelled_stop(row(), stopped, tail,
            'Stopping service\nMain process exited, code=exited, status=1/FAILURE\n', stop_issued=True)


def test_reset_requires_matching_invocation_in_actual_journal_records(simulated_known_cancel_stack):
    stopped = row('0', ActiveState='failed', SubState='failed', ExecMainCode='1', ExecMainStatus='1', Result='exit-code')
    with pytest.raises(p.Refusal, match='old invocation'):
        install.narrow_cancelled_stop(row(), stopped, CANCEL, JOURNAL.replace('a' * 32, 'b' * 32), stop_issued=True)


def test_unpinned_shutdown_stack_never_resets():
    stopped = row('0', ActiveState='failed', SubState='failed', ExecMainCode='1', ExecMainStatus='1', Result='exit-code')
    with pytest.raises(p.Refusal, match='recorded row-47'):
        install.narrow_cancelled_stop(row(), stopped, CANCEL, JOURNAL, stop_issued=True)


def test_control_refresh_is_from_post_quiescence_file_log(monkeypatch):
    monkeypatch.setattr(install, 'now', lambda: AT + timedelta(minutes=28))
    expiry = AT + timedelta(minutes=60)
    text = '2026-10-04 02:27:00,123 INFO [SCHWAB-TOKEN-REFRESHED] access_token refreshed; expires_at=' + expiry.isoformat()
    assert install.control_refresh_after(text, AT, expiry)
    assert not install.control_refresh_after(text.replace('02:27', '01:59'), AT, expiry)
    assert not install.control_refresh_after(text, AT, expiry + timedelta(seconds=1))


def test_original_token_settings_must_refuse_refresh_before_override(monkeypatch):
    from project_mai_tai import settings as settings_module
    seen = []
    def construct(**kwargs):
        seen.append(kwargs)
        return SimpleNamespace(schwab_adapter_token_refresh_enabled=True)
    monkeypatch.setattr(settings_module, 'Settings', construct)
    with pytest.raises(p.Refusal, match='original adapter refresh'):
        proofs.settings()
    assert seen == [{'_env_file': str(p.ENV)}]


def test_v2_ownership_direct_read_does_not_swallow_database_error():
    def factory():
        raise RuntimeError('controlled DB unreadable')
    config = SimpleNamespace(strategy_schwab_1m_v2_account_name='live:schwab_1m_v2',
                             strategy_schwab_1m_v2_webull_account_name='live:orb')
    with pytest.raises(RuntimeError, match='DB unreadable'):
        proofs.v2_owned_symbols(factory, config)


def test_completion_proof_fsynced_before_journal(monkeypatch):
    calls = []
    monkeypatch.setattr(install, 'save', lambda name, value: calls.append(('save', name)))
    monkeypatch.setattr(install, 'journal', lambda result: calls.append(('journal', result)))
    monkeypatch.setattr(install, 'digest', lambda path: 'f' * 64)
    install.commit_completion({'simulator': True})
    assert calls == [('save', 'COMPLETE.json'), ('journal', 'INSTALL COMPLETE'), ('save', 'JOURNALED.json')]


def test_failed_proof_write_never_claims_journal_complete(monkeypatch):
    monkeypatch.setattr(install, 'save', lambda *args: (_ for _ in ()).throw(OSError('controlled disk error')))
    monkeypatch.setattr(install, 'journal', lambda *args: pytest.fail('must not journal COMPLETE'))
    with pytest.raises(OSError):
        install.commit_completion({})


def test_journal_failure_after_proof_is_not_silently_complete(tmp_path, monkeypatch):
    monkeypatch.setattr(install, 'RUN', tmp_path)
    monkeypatch.setattr(install, 'journal', lambda *args: (_ for _ in ()).throw(OSError('controlled journal error')))
    monkeypatch.setattr(install, 'identity', lambda name: row())
    monkeypatch.setattr(install.subprocess, 'run', lambda *a, **k: SimpleNamespace(returncode=0))
    with pytest.raises(OSError):
        install.commit_completion({})
    install.abort('controlled journal error')
    assert not (tmp_path / 'JOURNALED.json').exists()
    assert json.loads((tmp_path / 'ABORT.json').read_text())['proofs_complete'] is True
    assert (tmp_path / 'JOURNAL-FAILURE.json').exists()


def test_actual_application_idle_probes_on_controlled_empty_sqlite(tmp_path, monkeypatch):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session
    from project_mai_tai.db.models import Base, BrokerAccount, DashboardSnapshot
    from project_mai_tai.events import StrategyStateSnapshotEvent, StrategyStateSnapshotPayload
    from project_mai_tai.settings import Settings
    engine = create_engine('sqlite:///' + str(tmp_path / 'simulated-inputs.db'))
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        for name, provider in (('live:schwab_1m_v2', 'schwab'), ('live:orb', 'webull')):
            db.add(BrokerAccount(name=name, provider=provider, environment='live'))
        db.add(DashboardSnapshot(snapshot_type='scanner_confirmed_last_nonempty',
            payload={'scanner_session_start_utc': '2026-10-02T08:00:00+00:00',
                     'persisted_at': '2026-10-02T08:00:00+00:00', 'all_confirmed_candidates': []}))
        db.commit()
    monkeypatch.setattr(proofs, 'readonly_engine', lambda config: engine)
    monkeypatch.setattr(proofs, 'now', lambda: AT)
    event = StrategyStateSnapshotEvent(source_service='strategy-engine', produced_at=AT,
                                      payload=StrategyStateSnapshotPayload()).model_dump(mode='json')
    reader = SimpleNamespace(rows=lambda *a, **k: [('1-0', event)])
    config = Settings(_env_file=None, database_url='sqlite://', strategy_schwab_1m_v2_account_name='live:schwab_1m_v2',
                      strategy_schwab_1m_v2_webull_account_name='live:orb')
    result = proofs.input_proofs(config, reader)
    assert set(result) == p.OWNERS
    assert all(item['symbols'] == [] for item in result.values())


@pytest.mark.parametrize('line', ['Final call: PASS; checked=136/136 mismatches=0 unknown=0',
    'Final call: PASS; checked=139/140 mismatches=0 unknown=0',
    'Final call: UNKNOWN; checked=140/140 mismatches=0 unknown=1',
    'Final call: PASS; checked=140/140 mismatches=1 unknown=0',
    'Final call: PASS; checked=140/140 mismatches=0 unknown=0\nFinal call: PASS; checked=8/8 mismatches=0 unknown=0'])
def test_no_partial_or_old_denominator_pass(line):
    with pytest.raises(p.Refusal):
        install.parse_gate(line, 140)


def test_exact_counts_pass():
    install.parse_gate('Final call: PASS; checked=140/140 mismatches=0 unknown=0', 140)
    install.parse_gate('Final call: PASS; checked=8/8 mismatches=0 unknown=0', 8)


def test_proc_probe_uses_fresh_child_and_no_installer_application_imports(monkeypatch):
    host = install.Host.__new__(install.Host)
    seen = []
    def fake(args, **kwargs):
        seen.append((args, kwargs))
        return SimpleNamespace(returncode=0, stdout='{"fresh":true}', stderr='')
    monkeypatch.setattr(install.subprocess, 'run', fake)
    assert host.proof('loaded') == {'fresh': True}
    assert seen[0][0][:2] == [str(install.PY), '-s']
    assert seen[0][0][-1] == 'loaded'
    assert seen[0][1]['env']['PYTHONPATH'] == str(p.REPO / 'src')
    assert 'from project_mai_tai' not in (HERE / 'install.py').read_text()


def test_units_lock_attempt_and_no_approval_shipped():
    service = (HERE / (p.JOB + '.service')).read_text()
    timer = (HERE / (p.JOB + '.timer')).read_text()
    shell = (HERE / 'run_install.sh').read_text()
    assert 'Restart=no' in service and 'RemainAfterExit=yes' in service
    assert 'ConditionPathExists=!' + str(p.RUN) in service
    assert 'Persistent=false' in timer and '23:00:00 America/New_York' in timer
    assert shell.index('review_gate.py --prepare') < shell.index('flock -n 9') < shell.index('mkdir -m 0700')
    assert 'set -Eeuo pipefail' in shell and 'trap ' in shell
    assert not (HERE / 'approval.json').exists()


def test_source_compiles_and_shell_parses():
    for path in HERE.glob('*.py'):
        compile(path.read_text(), str(path), 'exec')
    subprocess.run(['bash', '-n', str(HERE / 'run_install.sh')], check=True)


def test_nonzero_orb_stop_only_routes_to_bound_cancelled_handler(monkeypatch):
    host = install.Host.__new__(install.Host)
    host.stage = 'stop-orb-schwab'
    host.gate = lambda *a, **k: None
    host.last_flat_at = p.now()
    host.record = lambda *args: None
    calls = []
    host.narrow_stop_evidence = lambda: calls.append('narrow-evidence')
    monkeypatch.setattr(install, 'window', lambda *a, **k: None)
    monkeypatch.setattr(install, 'append_event', lambda *a, **k: None)
    monkeypatch.setattr(install, 'identity', lambda name: row('0', ActiveState='failed'))
    monkeypatch.setattr(install.subprocess, 'run', lambda *a, **k: SimpleNamespace(returncode=1, stdout='', stderr='controlled'))
    host.service('stop', 'orb-schwab')
    assert calls == ['narrow-evidence']
    with pytest.raises(p.Refusal):
        host.service('stop', 'orb')
    with pytest.raises(p.Refusal):
        host.service('start', 'orb-schwab')


def test_report_options_preserve_existing_flags_quiet_and_schema():
    old = '''KEEP=true
  --expect-flag 'oms:MAI_TAI_RETAINED=true' \\
  --expect-flag "strategy:MAI_TAI_OLD=$KEEP" \\
  --expected-quiet-service market-capture \\
  --schema-column schema.table.column \\
  --schema-constraint schema.constraint \\
'''
    assert install.report_options(old) == [
        '--expect-flag', 'oms:MAI_TAI_RETAINED=true', '--expect-flag', 'strategy:MAI_TAI_OLD=true',
        '--expected-quiet-service', 'market-capture', '--schema-column', 'schema.table.column',
        '--schema-constraint', 'schema.constraint']


def test_report_options_never_add_quiet_service_waiver():
    assert install.report_options('--expect-flag oms:MAI_TAI_X=true') == ['--expect-flag', 'oms:MAI_TAI_X=true']
    with pytest.raises(p.Refusal):
        install.report_options('--expected-quiet-service "$UNKNOWN"')


def test_recorded_oct3_row47_stack_is_the_only_unpatched_reset_exception():
    # Frames from orb-schwab.log-20261004, read 10-03; journal invocation below
    # is the actual 20:59 UTC stop, not a hypothetical shutdown trace.
    trace = '''Traceback (most recent call last):
  File "/home/trader/project-mai-tai/.venv/bin/mai-tai-orb-schwab", line 6, in <module>
    sys.exit(run())
  File "/home/trader/project-mai-tai/src/project_mai_tai/services/orb_schwab_app.py", line 551, in run
  File "/usr/lib/python3.12/asyncio/runners.py", line 194, in run
    return runner.run(main)
  File "/usr/lib/python3.12/asyncio/runners.py", line 118, in run
    return self._loop.run_until_complete(task)
  File "/usr/lib/python3.12/asyncio/base_events.py", line 687, in run_until_complete
    return future.result()
  File "/home/trader/project-mai-tai/src/project_mai_tai/services/orb_schwab_app.py", line 547, in main
  File "/home/trader/project-mai-tai/src/project_mai_tai/services/orb_schwab_app.py", line 536, in run
  File "/usr/lib/python3.12/asyncio/tasks.py", line 665, in sleep
    return await future
asyncio.exceptions.CancelledError
'''
    invocation = 'a14bf297def14153a0554a1c1259793f'
    before = {'KillSignal': '15', 'InvocationID': invocation}
    after = {'MainPID': '0', 'ActiveState': 'failed', 'ExecMainCode': '1',
             'ExecMainStatus': '1', 'Result': 'exit-code', 'NRestarts': '0',
             'InvocationID': invocation}
    journal = '\n'.join(json.dumps({'INVOCATION_ID': invocation,
        'UNIT': p.unit('orb-schwab'), 'MESSAGE': message}) for message in (
        'Stopping project-mai-tai-orb-schwab.service - Project Mai Tai separate default-off ORB Schwab producer...',
        'project-mai-tai-orb-schwab.service: Main process exited, code=exited, status=1/FAILURE'))
    install.narrow_cancelled_stop(before, after, trace, journal, stop_issued=True)
    with pytest.raises(p.Refusal, match='recorded row-47 stack'):
        install.narrow_cancelled_stop(before, after, trace.replace('in sleep', 'in unrelated_path'),
                                      journal, stop_issued=True)
