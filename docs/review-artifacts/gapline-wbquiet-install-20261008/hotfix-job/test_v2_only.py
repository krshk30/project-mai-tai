"""V2-only followup mechanics; isolated files, no broker or service actions."""
from copy import deepcopy
from datetime import timedelta
import json
import re
import sys
import types

import pytest

from test_repin_preopen import APP, NOW, RECORD, case as case_fixture, orb_case, put, m
import v2_only as lane

case = case_fixture

NEW = 'c' * 40
DEST = '/home/trader/new-v2-attempt'


def followup_case(case):
    root, obs, _ = case
    m.apply(root, orb_case(case), NOW)
    for role in (m.DEFAULT_SERVICES | {'orb-schwab'}) - obs['states'].keys():
        obs['states'][role] = dict(MainPID='100', NRestarts='0', ActiveState='active', SubState='running',
                                  ExecMainStartTimestamp='Thu 2026-10-08 21:00:00 UTC')
    baseline = dict(captured_at_utc=(NOW + timedelta(minutes=1)).isoformat(), states=deepcopy(obs['states']))
    obs['states']['schwab-1m-v2'] = {**obs['states']['schwab-1m-v2'], 'MainPID': '22222',
                                    'ExecMainStartTimestamp': 'Thu 2026-10-08 22:12:00 UTC'}
    obs['head'] = NEW
    m.location(root, DEST).mkdir(parents=True)
    args = lane.bundle(root, NEW, baseline, obs, 0, NOW + timedelta(minutes=3), DEST,
                       [{'stage': 'deploy-v2-only', 'rc': 0}])
    return root, obs, baseline, args


def build_followup(root, obs, args):
    return m.plan(root, NEW, args['snapshot'], args['record'], obs, NOW + timedelta(minutes=3),
                  orb_restart=args['orb_restart'], retirement=args['retirement'], v2_followup=args['followup'])


def test_v2_only_repin_keeps_five_service_history_and_only_changes_v2_pin(case):
    root, obs, _, args = followup_case(case)
    old = m.location(root, m.GATE).read_text()
    changes = build_followup(root, obs, args)
    gate = changes[m.GATE].decode()
    for label in ('OMS_', 'STRATEGY_', 'CONTROL_', 'ORB_SCHWAB_'):
        for suffix in ('PID', 'START'):
            pattern = '^EXPECTED_' + label + suffix + '=.*$'
            assert re.findall(pattern, old, re.M) == re.findall(pattern, gate, re.M)
    assert 'EXPECTED_PID=22222' in gate and f'EXPECTED_SHA={NEW}' in gate
    assert set(re.findall(r'--restarted ([\w-]+)', gate)) == m.RESTARTED | {'orb-schwab'}
    assert 'check_identity orb "$ORB_UNIT"' not in gate
    assert 'daily.py paper' in gate and '$(TZ=America/New_York date +%F)' in gate
    ack = json.loads(changes[m.DAILY + '/upgrade-ack.json'])
    assert ack['active_for_current_install'] is False
    binding = json.loads(changes[m.DAILY + '/binding.json'])
    assert binding['current_install']['latest_service_actions'] == {'schwab-1m-v2': 'restarted'}
    assert binding['historical_binding']['approved_sha'] == APP
    assert json.loads(m.location(root, RECORD).read_bytes())['source_journal'] != args['record']


@pytest.mark.parametrize('defect', ['oms', 'strategy', 'control', 'orb-schwab', 'market-data', 'old_v2',
                                  'same_v2', 'before_start', 'failed_deploy', 'extra_action', 'old_app',
                                  'missing_fleet', 'ack_active', 'retirement_changed'])
def test_v2_only_followup_cannot_adopt_identity_or_historical_proof(case, defect):
    root, obs, _, args = followup_case(case)
    path = m.location(root, args['followup'])
    value = json.loads(path.read_bytes())
    if defect in ('oms', 'strategy', 'control', 'orb-schwab', 'market-data'):
        obs['states'][defect]['MainPID'] = '666'
    elif defect == 'old_v2':
        value['before']['schwab-1m-v2']['MainPID'] = '999'
    elif defect == 'same_v2':
        obs['states']['schwab-1m-v2'] = value['before']['schwab-1m-v2']
    elif defect == 'before_start':
        obs['states']['schwab-1m-v2']['ExecMainStartTimestamp'] = 'Thu 2026-10-08 22:10:00 UTC'
    elif defect == 'failed_deploy':
        value['deploy_rc'] = 1
    elif defect == 'extra_action':
        value['service_actions']['oms'] = 'restarted'
    elif defect == 'old_app':
        value['previous_sha'] = NEW
    elif defect == 'missing_fleet':
        del value['before']['market-data']
    elif defect == 'ack_active':
        ack_path = m.location(root, m.DAILY + '/upgrade-ack.json')
        ack = json.loads(ack_path.read_bytes())
        ack['active_for_current_install'] = True
        put(root, m.DAILY + '/upgrade-ack.json', m.canonical(ack))
        runtime = json.loads(m.location(root, m.DAILY + '/runtime.json').read_bytes())
        runtime['artifacts']['upgrade-ack.json'] = m.digest(ack_path.read_bytes())
        put(root, m.DAILY + '/runtime.json', m.canonical(runtime))
    else:
        put(root, args['retirement'], b'{}')
    put(root, args['followup'], m.canonical(value))
    with pytest.raises((ValueError, m.Refusal)):
        build_followup(root, obs, args)


def test_deploy_command_is_v2_only_no_migration_or_live_override():
    command = lane.deploy_command(NEW, 'codex/install-2026-10-08-' + NEW[:12], 'http://127.0.0.1:123/health')
    assert command[-1] == 'schwab-1m-v2'
    assert 'MAI_TAI_RUN_MIGRATIONS=0' in command and 'MAI_TAI_ALLOW_LIVE_RESTART=0' in command
    assert not {'oms', 'strategy', 'control', 'orb-schwab'} & set(command)


def test_proof_scope_only_restarts_v2_preserves_oms_flags_and_restores_module(monkeypatch):
    import proof_readonly as proof
    old = proof.RESTARTED
    monkeypatch.setattr(proof, 'collect_baseline', lambda: dict(services={'oms': {'MainPID': 12}},
                                                               process_flags={}, errors={}))
    monkeypatch.setattr(proof, 'process_flags', lambda pid: {'values': {'held': ['true']}})
    with lane.proof_scope() as scoped:
        assert scoped.RESTARTED == {'schwab-1m-v2'}
        assert scoped.collect_baseline()['process_flags']['oms']['values']['held'] == ['true']
    assert proof.RESTARTED == old


def test_new_daily_runtime_real_verifier_accepts_v2_followup(case, monkeypatch):
    root, obs, _, args = followup_case(case)
    m.apply(root, build_followup(root, obs, args), NOW + timedelta(minutes=3))
    policy = types.ModuleType('release_policy')
    policy.__file__ = str(m.location(root, m.DAILY + '/release_policy.py'))
    exec(compile(open(policy.__file__, 'rb').read(), policy.__file__, 'exec'), policy.__dict__)
    monkeypatch.setitem(sys.modules, 'release_policy', policy)
    daily = types.ModuleType('isolated_v2_daily')
    exec(compile(m.location(root, m.DAILY + '/daily.py').read_bytes(), 'daily.py', 'exec'), daily.__dict__)
    daily.ROOT, daily.GATE, daily.REPO = (m.location(root, path) for path in (m.DAILY, m.GATE, m.REPO))
    import os
    from pathlib import Path
    original_stat = Path.stat
    def root_owned(path, *a, **kw):
        data = list(original_stat(path, *a, **kw))
        if path != daily.GATE:
            data[4] = 0
        return os.stat_result(data)
    with monkeypatch.context() as mock:
        mock.setattr(Path, 'stat', root_owned)
        mock.setattr(daily.os, 'geteuid', lambda: 0)
        pin = json.loads((daily.ROOT / 'runtime.json').read_bytes())
        pin['evidence_inputs'] = {str(m.location(root, p)): h for p, h in pin['evidence_inputs'].items()}
        (daily.ROOT / 'runtime.json').write_bytes(m.canonical(pin))
        assert daily.verify_runtime()['approved_sha'] == NEW


@pytest.mark.parametrize('defect', [None, 'unbound', 'scope', 'ref', 'artifact', 'missing_dependency',
                                  'missing_seal', 'changed_seal'])
def test_exact_release_has_no_application_guess_and_preserves_failed_seals(tmp_path, defect):
    job = tmp_path / 'new-job'
    job.mkdir()
    artifacts = {}
    for name in lane.DEPS:
        raw = name.encode()
        (job / name).write_bytes(raw)
        artifacts[name] = m.digest(raw)
    seal = tmp_path / 'old-ABORT.json'
    seal.write_bytes(b'{"verdict":"ABORT"}')
    release = dict(scope=lane.SCOPE, approved_sha=NEW, box_sha=APP,
                   release_branch='codex/install-2026-10-08-' + NEW[:12], artifacts=artifacts,
                   immutable_receipts={str(seal): m.digest(seal.read_bytes())})
    if defect == 'unbound':
        release['approved_sha'] = None
    elif defect == 'scope':
        release['scope'] = 'oms-v2'
    elif defect == 'ref':
        release['release_branch'] = 'main'
    elif defect == 'artifact':
        (job / 'v2_only.py').write_bytes(b'changed')
    elif defect == 'missing_dependency':
        del artifacts['repin_preopen.py']
    elif defect == 'missing_seal':
        release['immutable_receipts'] = {}
    elif defect == 'changed_seal':
        seal.write_bytes(b'{"verdict":"COMPLETE"}')
    (job / 'release.json').write_bytes(m.canonical(release))
    if defect is None:
        assert lane.verify(job)['approved_sha'] == NEW
    else:
        with pytest.raises(RuntimeError):
            lane.verify(job)


def test_literal_driver_restarts_only_v2_and_records_failed_proof_without_waiver(case, monkeypatch, tmp_path):
    from contextlib import contextmanager
    from pathlib import Path
    root, obs, baseline, args = followup_case(case)
    initial = deepcopy(obs)
    initial['head'] = APP
    initial['states'] = baseline['states']
    population = iter([initial, deepcopy(initial), deepcopy(obs), deepcopy(obs)])
    monkeypatch.setattr(lane.repin, 'collect', lambda **kwargs: next(population))
    original_read = Path.read_bytes
    monkeypatch.setattr(Path, 'read_bytes', lambda p: b'unchanged-env' if str(p) ==
                        '/etc/project-mai-tai/project-mai-tai.env' else original_read(p))
    monkeypatch.setattr(lane, 'window', lambda now: 'READY')
    monkeypatch.setattr(lane.time, 'sleep', lambda seconds: None)
    @contextmanager
    def scoped():
        yield types.SimpleNamespace(collect_baseline=lambda: {'actual': 'baseline'},
            collect_after=lambda before, app: {'assessment': {'verdict': 'FAIL',
                'failures': ['post_start_traceback_or_error:schwab-1m-v2'], 'unknown': []}})
    monkeypatch.setattr(lane, 'proof_scope', scoped)
    monkeypatch.setattr(lane, 'bundle', lambda *a: args)
    import health_view
    @contextmanager
    def health(report):
        yield 'http://127.0.0.1:123/health'
    monkeypatch.setattr(health_view, 'health_view', health)
    import flag_audit
    monkeypatch.setattr(flag_audit, 'command', lambda python: ['isolated', 'flag-audit'])
    monkeypatch.setattr(flag_audit, 'receipt', lambda *a: {'rc': 2, 'unknown': 'momentum-paper'})
    class Fake:
        def __init__(self):
            self.job = tmp_path / 'package'
            self.attempt = self.job / 'attempt'
            self.attempt.mkdir(parents=True)
            self.release = dict(approved_sha=NEW, box_sha=APP,
                                release_branch='codex/install-2026-10-08-' + NEW[:12])
            self.events, self.commands, self.gates = [], [], []
        def note(self, **value):
            self.events.append(value)
            (self.attempt / 'runner.log').write_bytes(m.canonical(self.events))
        def gate(self, label):
            self.gates.append(label)
            return 0
        def native_rehearsal(self, names):
            assert names == ('v2',)
        def checked(self, command, **kwargs):
            self.commands.append(command)
            path = self.attempt / 'command-output'
            path.write_text(NEW + '\trefs/heads/' + self.release['release_branch'] + '\n')
            return path
        def command(self, command, **kwargs):
            self.commands.append(command)
            out, err = self.attempt / 'stdout', self.attempt / 'stderr'
            out.write_bytes(b'FAIL/UNKNOWN retained')
            err.write_bytes(b'')
            return 1, out, err
    run = Fake()
    lane.execute(run)
    deployments = [c for c in run.commands if str(lane.REPO / 'ops/systemd/deploy_service.sh') in c]
    assert len(deployments) == 1 and deployments[0][-1] == 'schwab-1m-v2'
    assert run.gates == ['fresh-before-v2', 'immediate-before-deploy-v2', 'post-install-trading-read']
    assert any('--v2-followup' in c for c in run.commands)
    assert any('daily.verify_runtime' in ' '.join(c) for c in run.commands)
    assert json.loads((run.job / 'FINISHED.json').read_bytes())['assessment']['verdict'] == 'FAIL'
    assert not (run.job / 'COMPLETE.json').exists()
    assert run.events[-1]['global_install_pass'] is False
