"""Exercise literal hotfix calls in private files; no systemd/SSH/DB invocation."""
from contextlib import nullcontext
import json
from pathlib import Path

import pytest

import bookkeeping
import flag_audit
import health_view
import runner
from test_hotfix_mechanics import catalogs


@pytest.fixture
def rehearsal(tmp_path, monkeypatch):
    repo, job, attempt = [tmp_path / n for n in ('repo', 'job', 'attempt')]
    for directory in (repo, job, attempt):
        directory.mkdir()
    env, catalog = tmp_path / 'env', tmp_path / 'catalog'
    env.write_text('UNCHANGED_ACTUAL_ENV=true\n')
    box, approved = catalogs()
    catalog.write_bytes(runner.canonical(box))
    numeric = tmp_path / 'numeric'
    numeric.write_text('retained ten numeric checks, retry budget zero')
    deploy = repo / 'ops/systemd/deploy_service.sh'
    deploy.parent.mkdir(parents=True)
    deploy.write_text('isolated fixture')
    monkeypatch.setattr(runner, 'REPO', repo)
    monkeypatch.setattr(runner, 'ENV', env)
    monkeypatch.setattr(runner, 'CATALOG', catalog)
    # Only map the exact numeric path used by the literal sequence.
    original_path = runner.Path
    monkeypatch.setattr(runner, 'Path', lambda value: numeric if str(value) == str(flag_audit.NUMERIC) else original_path(value))
    monkeypatch.setattr(runner, 'window', lambda now: 'READY')
    waits = []
    monkeypatch.setattr(runner.time, 'sleep', waits.append)
    monkeypatch.setattr(health_view, 'health_view', lambda report: nullcontext('http://127.0.0.1:12345/health'))
    monkeypatch.setattr(flag_audit, 'command', lambda python: [str(python), '/isolated/checker', '--catalog', str(catalog), '--numeric-catalog', str(numeric)])
    monkeypatch.setattr(flag_audit, 'receipt', lambda args, rc, out, err: dict(rc=rc, lines=out.read_text().splitlines()))
    roles = (*runner.UNITS, 'control', 'orb')
    states = {n: dict(MainPID=100+i, NRestarts=0, ActiveState='active', SubState='running',
                     InvocationID='old-'+n, ExecMainStartTimestampMonotonic=1000+i) for i, n in enumerate(roles)}
    prior = dict(snapshot=dict(schema_version=3, captured_at_utc='2026-10-08T20:22:00+00:00', services={n: {} for n in roles}),
                 service_identities={'control': states['control']}, hashes={'fixture': 'actual proof modeled in private file'},
                 original_verdict='ABORT; never COMPLETE')
    monkeypatch.setattr(bookkeeping, 'prior_install', lambda bindings: prior)
    release = dict(approved_sha='a'*40, box_sha=runner.BASELINE_SHA, plan_commit='b'*40, line_enabled=True,
                   release_branch='codex/install-2026-10-08-' + 'a'*12, source_hashes={}, prior_install={},
                   baseline_identities=states, baseline_deploy_sha256=runner.digest(deploy.read_bytes()))
    (job / 'release.json').write_bytes(runner.canonical(release))
    class Rehearsal(runner.Run):
        def __init__(self):
            super().__init__(job, release, attempt)
            self.calls, self.gates = [], []
            self.fail_proof = False
            self.after_restore_count = 0
        def gate(self, label):
            self.stage = label
            self.gates.append(label)
            return 0
        def native_rehearsal(self, names=('oms', 'v2'), **kwargs):
            self.gates.append('native-' + '-'.join(names))
        def identities(self):
            deployed = any('MAI_TAI_RUN_MIGRATIONS=0' in call for call in self.calls)
            return {n: {**{k: str(v) for k, v in states[n].items()}, 'MainPID': str(200+i if deployed else 100+i)}
                    for i, n in enumerate(runner.UNITS)}
        def checked(self, args, **kwargs):
            self.calls.append(args)
            out = attempt / ('output-' + str(len(self.calls)))
            out.write_text('')
            if 'rev-parse' in args:
                out.write_text(release['box_sha'] if args[-1] == 'HEAD' else release['approved_sha'])
            elif 'show' in args:
                out.write_bytes(runner.canonical(approved))
            elif 'snapshot' in args:
                Path(args[-1]).write_bytes(runner.canonical(prior['snapshot']))
            elif any(str(a).endswith('proof_readonly.py') for a in args):
                if 'baseline' in args:
                    Path(args[-1]).write_bytes(runner.canonical(dict(services=states)))
                else:
                    Path(args[-1]).write_bytes(runner.canonical(dict(assessment=dict(verdict='FAIL' if self.fail_proof else 'PASS'))))
                    if self.fail_proof:
                        raise RuntimeError('observed v2 ERROR lines; no waiver')
            elif any(str(a).endswith('removed_wait_readonly.py') for a in args):
                count = self.after_restore_count if str(args[-1]).endswith('removed-wait-after.json') else 0
                Path(args[-1]).write_bytes(runner.canonical(dict(revision=['20261008_0023'], restored_count=count,
                    direct_boot_count='UNMEASURED; no marker')))
            return out
        def command(self, args, **kwargs):
            self.calls.append(args)
            out, err = attempt / 'flag-out', attempt / 'flag-err'
            out.write_text('Final call: UNKNOWN; checked=153/155 mismatches=0 unknown=2\nUNKNOWN service=momentum-paper reason=inactive\nUNKNOWN service=momentum-paper reason=inactive numeric\n')
            err.write_text('')
            return 2, out, err
    return Rehearsal(), env, numeric, waits


def test_oms_v2_only_literal_sequence_preserves_env_numeric_and_records_prior_control(rehearsal):
    run, env, numeric, waits = rehearsal
    before_env, before_numeric = env.read_bytes(), numeric.read_bytes()
    run.install()
    deploy = [args for args in run.calls if 'MAI_TAI_RUN_MIGRATIONS=0' in args]
    assert [args[-1] for args in deploy] == ['oms', 'schwab-1m-v2']
    assert all('MAI_TAI_EXPECTED_SHA=' + 'a'*40 in args for args in deploy)
    assert env.read_bytes() == before_env and numeric.read_bytes() == before_numeric
    assert waits == [600]
    assert run.gates == ['native-oms-v2', 'final-before-first-write', 'before-deploy-oms',
                         'before-deploy-schwab-1m-v2', 'native-v2', 'post-install-trading-read']
    record = json.loads((run.attempt / 'install-record.json').read_bytes())
    assert record['service_actions']['control'] == 'restarted'  # original authorized start, not this deployment
    journal = json.loads((run.attempt / 'sealed-actions.json').read_bytes())
    assert journal['prior_install_verdict'] == 'ABORT; never COMPLETE'
    assert journal['hotfix_restarted_group'] == ['oms', 'schwab-1m-v2', 'strategy']
    repin = next(args for args in run.calls if len(args) > 1 and str(args[1]).endswith('repin_preopen.py'))
    assert 'bookkeeping-original-snapshot.json' in repin[repin.index('--snapshot') + 1]
    assert '--retirement' not in repin
    assert not any('upgrade' in args or 'restart' in args for args in run.calls)
    assert (run.job / 'COMPLETE.json').exists()
    complete = json.loads((run.job / 'COMPLETE.json').read_bytes())
    assert complete['removed_wait_before']['restored_count'] == complete['removed_wait_after']['restored_count'] == 0
    assert complete['direct_boot_count'].startswith('UNMEASURED')
    assert len(complete['removed_wait_receipts']) == 2


def test_real_error_receipt_prevents_complete_or_repin_without_stopping_running_services(rehearsal):
    run, _, _, _ = rehearsal
    run.fail_proof = True
    with pytest.raises(RuntimeError, match='no waiver'):
        run.install()
    assert not (run.job / 'COMPLETE.json').exists()
    assert not any(any(str(arg).endswith('repin_preopen.py') for arg in args) for args in run.calls)
    assert not any('stop' in args or 'restart' in args for args in run.calls)
    assert json.loads((run.attempt / 'post-install-proof.json').read_bytes())['assessment']['verdict'] == 'FAIL'


def test_recreated_removed_wait_rows_report_nonzero_and_prevent_complete_without_archiving(rehearsal):
    run, _, _, _ = rehearsal
    run.after_restore_count = 8
    with pytest.raises(RuntimeError, match='preserve rows and actual receipt'):
        run.install()
    assert json.loads((run.attempt / 'removed-wait-after.json').read_bytes())['restored_count'] == 8
    assert not (run.job / 'COMPLETE.json').exists()
    assert not any('archive' in arg or 'delete' in arg for args in run.calls for arg in args)
