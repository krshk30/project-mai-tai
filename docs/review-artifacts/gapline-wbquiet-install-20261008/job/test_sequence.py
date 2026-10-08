"""Literal runner rehearsals in private files; systemd/brokers never invoked."""
import json
from pathlib import Path

import pytest

import runner
from test_retire_orb import receipt
from test_runner import catalogs, environment


@pytest.fixture
def rehearsal(tmp_path, monkeypatch):
    repo, job, attempt = (tmp_path / name for name in ('repo', 'job', 'attempt'))
    for path in (repo, job, attempt):
        path.mkdir()
    env, catalog = tmp_path / 'env', tmp_path / 'catalog'
    env.write_bytes(environment())
    original, approved = catalogs()
    catalog.write_bytes(runner.canonical(original))
    deploy = repo / 'ops/systemd/deploy_service.sh'
    deploy.parent.mkdir(parents=True)
    deploy.write_bytes(b'isolated deploy fixture')
    monkeypatch.setattr(runner, 'REPO', repo)
    monkeypatch.setattr(runner, 'ENV', env)
    monkeypatch.setattr(runner, 'CATALOG', catalog)
    monkeypatch.setattr(runner.time, 'sleep', lambda seconds: None)
    monkeypatch.setattr(runner, 'window', lambda now: 'READY')
    release = dict(approved_sha='a' * 40, box_sha='b' * 40, plan_commit='c' * 40, line_enabled=True,
                   release_branch='codex/install-2026-10-08-' + 'a' * 12, source_hashes={},
                   baseline_deploy_sha256=runner.digest(deploy.read_bytes()))
    release['baseline_identities'] = {name: dict(MainPID=100 + i, NRestarts=0, ActiveState='active', SubState='running',
        InvocationID='old-' + name, ExecMainStartTimestampMonotonic=100) for i, name in enumerate(runner.UNITS)}
    (job / 'release.json').write_bytes(runner.canonical(release))
    (job / 'completed-retirement.json').write_bytes(runner.canonical(receipt()))
    class Rehearsal(runner.Run):
        def __init__(self):
            super().__init__(job, release, attempt)
            self.calls, self.reads = [], []
            self.identity_reads = 0
            self.block_at = None

        def gate(self, label):
            self.stage = label
            self.reads.append(label)
            return 1 if label == self.block_at else 0

        def identities(self):
            self.identity_reads += 1
            deployed = any('MAI_TAI_RUN_MIGRATIONS=0' in call for call in self.calls)
            return {name: dict(MainPID=str(i + (200 if deployed else 100)),
                     NRestarts='0', ActiveState='active', SubState='running')
                    for i, name in enumerate(runner.UNITS)}

        def checked(self, command, **kwargs):
            self.calls.append(command)
            output = attempt / ('fixture-output-' + str(len(self.calls)))
            output.write_text('')
            if 'rev-parse' in command:
                output.write_text(release['box_sha'] if command[-1] == 'HEAD' else release['approved_sha'])
            elif 'show' in command:
                output.write_bytes(runner.canonical(approved))
            elif 'snapshot' in command:
                Path(command[-1]).write_bytes(runner.canonical(dict(schema_version=3,
                    captured_at_utc='2026-10-08T21:00:00+00:00', services={name: {} for name in runner.UNITS})))
            elif any(str(arg).endswith('proof_readonly.py') for arg in command) and 'baseline' in command:
                Path(command[-1]).write_bytes(runner.canonical(dict(services=release['baseline_identities'])))
            elif any(str(arg).endswith('proof_readonly.py') for arg in command) and 'after' in command:
                Path(command[-1]).write_bytes(runner.canonical(dict(assessment={'verdict': 'PASS', 'scope': 'fixture only'})))
            elif any(str(arg).endswith('retire_orb.py') for arg in command):
                Path(command[-1]).write_bytes(runner.canonical(receipt()))
            return output

        def command(self, command, **kwargs):
            self.calls.append(command)
            output, errors = attempt / 'flags.stdout', attempt / 'flags.stderr'
            output.write_text('PASS fixture-catalog\nUNKNOWN service=momentum-paper reason=inactive\n')
            errors.write_text('')
            return 2, output, errors

    return Rehearsal(), env, catalog


def test_literal_deploy_sequence_fresh_gate_before_each_restart_retirement_and_five_service_repin(rehearsal):
    run, env, catalog = rehearsal
    run.install()
    deploys = [call for call in run.calls if 'MAI_TAI_RUN_MIGRATIONS=0' in call]
    assert [call[-1] for call in deploys] == ['oms', 'schwab-1m-v2', 'orb-schwab', 'control']
    assert all('MAI_TAI_EXPECTED_SHA=' + 'a' * 40 in call for call in deploys)
    assert run.reads == ['final-before-first-write', 'before-deploy-oms', 'before-deploy-schwab-1m-v2',
                        'before-deploy-orb-schwab', 'before-deploy-control',
                        'post-install-trading-read']
    assert not any('disable' in call or 'publish' in call for call in run.calls)
    assert json.loads((run.attempt / 'orb-retirement.json').read_bytes()) == receipt()
    snapshot = next(call for call in run.calls if 'snapshot' in call)
    assert str(snapshot[1]).endswith('/official_v2_restart_evidence.py')
    record = json.loads((run.attempt / 'install-record.json').read_bytes())
    assert record['service_actions'] == {name: 'restarted' for name in runner.UNITS}
    assert 'orb' not in record['service_actions']
    repin = next(call for call in run.calls if str(call[1]).endswith('/repin_preopen.py'))
    assert repin[repin.index('--line-enabled') + 1] == 'true'
    assert '--retirement' in repin
    assert (run.job / 'COMPLETE.json').is_file()
    assert runner.LINE + '=true' in env.read_text() and runner.HANDOFF + '=false' in env.read_text()
    assert not any(row['name'] == 'orb_paper_enabled' for row in json.loads(catalog.read_bytes())['flags'])


@pytest.mark.parametrize('target', ['oms', 'schwab-1m-v2', 'orb-schwab', 'control'])
def test_measured_work_stops_before_target_never_skips_gate_or_recovers(rehearsal, target):
    run, _, _ = rehearsal
    run.block_at = 'before-deploy-' + target
    with pytest.raises(RuntimeError, match='fresh trading gate blocked'):
        run.install()
    assert not any(call[-1] == target and 'MAI_TAI_RUN_MIGRATIONS=0' in call for call in run.calls)
    assert not (run.job / 'COMPLETE.json').exists()
    assert not any('disable' in call for call in run.calls)


def test_changed_baseline_identity_refuses_before_env_catalog_or_deploy(rehearsal):
    run, env, catalog = rehearsal
    original = run.checked
    def drift(command, **kwargs):
        output = original(command, **kwargs)
        if 'baseline' in command:
            path = Path(command[-1])
            value = json.loads(path.read_bytes())
            value['services']['oms']['MainPID'] += 1
            path.write_bytes(runner.canonical(value))
        return output
    run.checked = drift
    old_env, old_catalog = env.read_bytes(), catalog.read_bytes()
    with pytest.raises(RuntimeError, match='baseline identity changed'):
        run.install()
    assert env.read_bytes() == old_env and catalog.read_bytes() == old_catalog
    assert not (run.job / 'write-started.json').exists()
