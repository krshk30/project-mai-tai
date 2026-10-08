import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tarfile

import pytest

import make_release
import stage


def review():
    return dict(approved_sha='a' * 40, line_enabled=True,
        candidate_prs=['1126', '1127', '1130', '1134', '1135'],
        box_baseline=dict(sha='c' * 40, captured_at_utc='2026-10-08T20:00:00+00:00',
            authorization_source='isolated authorized baseline fixture',
            services={role: dict(MainPID=100, NRestarts=0, ActiveState='active', SubState='running',
                                InvocationID='test-' + role, ExecMainStartTimestampMonotonic=100)
                      for role in make_release.BASELINE_ROLES}),
        landed_prs={pr: dict(base='d' * 40,
        reviewed_head='e' * 40, landing=landing * 40, pin_verdict='PASS', validate_verdict='PASS',
        pin_receipt='committed independent-review-pin receipt', validate_receipts=['hosted validate run'],
        source_prs=[pr])
        for pr, landing in [('1126', 'f'), ('1127', 'a'), ('1130', '2'), ('1134', '3'), ('1135', '4')]})


def fixture_release(monkeypatch):
    names = ('runner.py', 'run.sh', 'gate_readonly.py', 'proof_readonly.py', 'repin_preopen.py',
             'retire_orb.py', 'migration0023.py', 'health_view.py', 'project-mai-tai-gapline-wbquiet-20261008.service',
             'project-mai-tai-gapline-wbquiet-20261008.timer')
    blobs = {make_release.PREFIX + name: name.encode() for name in names}
    def git(*arguments):
        if arguments[0] == 'rev-parse':
            if arguments[1].endswith('^{tree}'):
                return b'identical-reviewed-tree\n'
            return arguments[1].split('^')[0].encode() + b'\n'
        if arguments[0] == 'ls-tree':
            return ('\n'.join(blobs) + '\n').encode()
        if arguments[0] == 'show':
            path = arguments[1].split(':', 1)[1]
            return blobs[path] if path in blobs else path.encode()
        if arguments[0] == 'merge-base':
            assert arguments[1] == '--is-ancestor'
            return b''
        if arguments[0] == 'diff':
            return b'reviewed changed lines including both WB and ORB\n'
        if arguments[0] == 'archive':
            return b'isolated approved migration archive fixture'
        raise AssertionError(arguments)
    monkeypatch.setattr(make_release, 'git', git)
    monkeypatch.setattr(make_release, 'verify_pin', lambda pr, row, ledger: dict(ledger_commit='9' * 40,
        stdout_sha256='0' * 64, stderr_sha256=make_release.digest(b'')))
    return (*names, 'candidate-review.json', 'approved-migrations.tar', 'official_v2_restart_evidence.py'), blobs


def test_release_from_committed_blobs_binds_plan_app_baseline_and_each_byte(monkeypatch):
    names, blobs = fixture_release(monkeypatch)
    raw, approval, values = make_release.release('b' * 40, 'a' * 40, 'c' * 40, review())
    manifest, approval = json.loads(raw), json.loads(approval)
    assert manifest['plan_commit'] == 'b' * 40
    assert manifest['approved_sha'] == 'a' * 40
    assert manifest['box_sha'] == 'c' * 40
    assert set(manifest['artifacts']) == set(names)
    for name in names:
        assert manifest['artifacts'][name] == hashlib.sha256(values[name]).hexdigest()
        if make_release.PREFIX + name in blobs:
            assert values[name] == blobs[make_release.PREFIX + name]
    assert approval['manifest_sha256'] == make_release.digest(raw)
    assert approval['authority'] == 'operator-standing-go'


def test_incomplete_committed_package_or_short_sha_cannot_be_sealed(monkeypatch):
    _, blobs = fixture_release(monkeypatch)
    del blobs[make_release.PREFIX + 'gate_readonly.py']
    with pytest.raises(ValueError, match='incomplete'):
        make_release.release('b' * 40, 'a' * 40, 'c' * 40, review())
    with pytest.raises(ValueError, match='full commit'):
        make_release.release('bbb', 'a' * 40, 'c' * 40, review())


def test_release_cannot_seal_gap_only_main_missing_linesrc_merge(monkeypatch):
    fixture_release(monkeypatch)
    original = make_release.git
    def refuse(*args):
        if args[:3] == ('merge-base', '--is-ancestor', review()['landed_prs']['1127']['landing']):
            raise subprocess.CalledProcessError(1, ['git', *args])
        return original(*args)
    monkeypatch.setattr(make_release, 'git', refuse)
    with pytest.raises(subprocess.CalledProcessError):
        make_release.release('b' * 40, 'a' * 40, 'c' * 40, review())


def test_tar_has_exact_manifest_artifact_population_and_no_links(monkeypatch):
    names, _ = fixture_release(monkeypatch)
    raw, manifest = stage.package('b' * 40, 'a' * 40, 'c' * 40, review())
    with tarfile.open(fileobj=io.BytesIO(raw)) as archive:
        assert set(archive.getnames()) == set(names) | {'release.json', 'approval.json'}
        assert all(row.isfile() and row.mode == 0o644 for row in archive.getmembers())
        assert archive.extractfile('release.json').read() == manifest


def execute_isolated_stager(tmp_path, raw, stamp='2026-10-08T20:00:00-04:00'):
    job = tmp_path / 'job'
    units = tmp_path / 'systemd'
    units.mkdir()
    # Substitute ONLY the filesystem root and systemd interface in this rehearsal.
    source = stage.REMOTE.replace('/home/trader/after-hours/2026-10-08/gapline-wbquiet/job', str(job))
    source = source.replace('/etc/systemd/system', str(units))
    source = source.replace("now = datetime.now(ZoneInfo('America/New_York'))", 'now = datetime.fromisoformat(' + repr(stamp) + ')')
    harness = ('import os, subprocess, types\n'
               'os.geteuid = lambda: 0\n'
               'calls=[]\n'
               'def command(args, **kwargs):\n'
               '    calls.append(args)\n'
               '    return types.SimpleNamespace(stdout="NextElapseUSecRealtime=Thu 2026-10-08 20:00:00 UTC\\n")\n'
               'subprocess.run=command\n')
    harness += source
    harness += '\nprint("UNIT_CALLS=" + json.dumps(calls))\n'
    result = subprocess.run([sys.executable, '-c', harness], input=raw,
                            capture_output=True, timeout=10, check=False)
    return result, job, units


def test_stager_rehearsal_verifies_bytes_copies_only_its_units_enables_timer_not_installer(tmp_path, monkeypatch):
    fixture_release(monkeypatch)
    raw, manifest = stage.package('b' * 40, 'a' * 40, 'c' * 40, review())
    result, job, units = execute_isolated_stager(tmp_path, raw)
    assert result.returncode == 0, result.stderr.decode()
    assert (job / 'release.json').read_bytes() == manifest
    assert (job / 'staging-receipt.json').exists()
    assert len(list(units.iterdir())) == 2
    calls = json.loads(result.stdout.decode().split('UNIT_CALLS=', 1)[1])
    assert calls[-1][0:2] == ['systemctl', 'show']
    assert ['systemctl', 'enable', '--now', 'project-mai-tai-gapline-wbquiet-20261008.timer'] in calls
    assert not any('start' in row or 'restart' in row for row in calls)


@pytest.mark.parametrize('name, value', [('runner.py', b'drift'), ('../escape', b'bad')])
def test_stager_rejects_hash_drift_or_path_escape_before_installing_any_unit(tmp_path, monkeypatch, name, value):
    fixture_release(monkeypatch)
    raw, _ = stage.package('b' * 40, 'a' * 40, 'c' * 40, review())
    output = io.BytesIO()
    with tarfile.open(fileobj=io.BytesIO(raw)) as old, tarfile.open(fileobj=output, mode='w') as new:
        for member in old.getmembers():
            data = value if member.name == name else old.extractfile(member).read()
            member.size = len(data)
            new.addfile(member, io.BytesIO(data))
        if name == '../escape':
            member = tarfile.TarInfo(name)
            member.size = len(value)
            new.addfile(member, io.BytesIO(value))
    result, job, units = execute_isolated_stager(tmp_path, output.getvalue())
    assert result.returncode != 0
    assert not job.exists()
    assert not list(units.iterdir())


def test_stager_unit_calendar_is_reviewed_local_et_not_global_export():
    path = Path(__file__).parent
    shell = (path / 'run.sh').read_text()
    assert 'unset TZ' in shell
    assert 'export TZ=' not in shell


@pytest.mark.parametrize('stamp', ['2026-10-08T15:59:59-04:00', '2026-10-09T16:00:00-04:00'])
def test_staging_clock_refuses_before_any_remote_directory_or_unit_write(tmp_path, monkeypatch, stamp):
    fixture_release(monkeypatch)
    raw, _ = stage.package('b' * 40, 'a' * 40, 'c' * 40, review())
    result, job, units = execute_isolated_stager(tmp_path, raw, stamp)
    assert result.returncode != 0 and b'NO STAGING' in result.stderr
    assert not job.exists() and not list(units.iterdir())


@pytest.mark.parametrize('pr', ['1126', '1127', '1130', '1134', '1135'])
def test_release_requires_every_reviewed_landing_not_original_cherrypick_ancestry(monkeypatch, pr):
    fixture_release(monkeypatch)
    value = review()
    del value['landed_prs'][pr]
    with pytest.raises(ValueError, match='landings required'):
        make_release.release('b' * 40, 'a' * 40, 'c' * 40, value)


def test_release_rejects_changed_union_tree_and_unreviewed_final_head(monkeypatch):
    fixture_release(monkeypatch)
    original = make_release.git
    def changed(*args):
        if args == ('rev-parse', 'a' * 40 + '^{tree}'):
            return b'not-the-reviewed-union'
        return original(*args)
    monkeypatch.setattr(make_release, 'git', changed)
    with pytest.raises(ValueError, match='landing tree differs'):
        make_release.release('b' * 40, 'a' * 40, 'c' * 40, review())
    value = review()
    value['approved_sha'] = '8' * 40
    with pytest.raises(ValueError, match='exact reviewed landing'):
        make_release.release('b' * 40, '8' * 40, 'c' * 40, value)


def test_release_cannot_claim_pass_when_committed_pin_verification_refuses(monkeypatch):
    fixture_release(monkeypatch)
    def refuse(*args):
        raise subprocess.CalledProcessError(1, ['review_pin_gate', 'verify'])
    monkeypatch.setattr(make_release, 'verify_pin', refuse)
    with pytest.raises(subprocess.CalledProcessError):
        make_release.release('b' * 40, 'a' * 40, 'c' * 40, review())


def test_release_rejects_future_wb_data_source_provenance(monkeypatch):
    fixture_release(monkeypatch)
    value = review()
    value['landed_prs']['1127']['source_prs'] = ['1127', '1124']
    with pytest.raises(ValueError, match='source provenance expands P1'):
        make_release.release('b' * 40, 'a' * 40, 'c' * 40, value)


@pytest.mark.parametrize('pr', ['1131', '1132', '1128', '1115', '1110', '1103', '1124', '1129', '1133'])
def test_release_refuses_non_p1_candidate_even_with_claimed_review(monkeypatch, pr):
    fixture_release(monkeypatch)
    value = review()
    value['landed_prs'][pr] = dict(value['landed_prs']['1127'], source_prs=[pr])
    value['candidate_prs'].append(pr)
    with pytest.raises(ValueError, match='non-P1'):
        make_release.release('b' * 40, 'a' * 40, 'c' * 40, value)


def test_h_requires_its_own_reviewed_landing(monkeypatch):
    fixture_release(monkeypatch)
    value = review()
    del value['landed_prs']['1135']
    with pytest.raises(ValueError, match='landings required'):
        make_release.release('b' * 40, 'a' * 40, 'c' * 40, value)


def test_release_requires_explicit_candidate_without_fictitious_retirement(monkeypatch):
    fixture_release(monkeypatch)
    value = review()
    value['candidate_prs'].append('1133')
    with pytest.raises(ValueError, match='explicit final P1'):
        make_release.release('b' * 40, 'a' * 40, 'c' * 40, value)
    value = review()
    _, _, blobs = make_release.release('b' * 40, 'a' * 40, 'c' * 40, value)
    assert 'completed-retirement.json' not in blobs


@pytest.mark.parametrize('defect', ['missing', 'other_box', 'missing_role', 'missing_start'])
def test_release_never_adopts_unacknowledged_or_incomplete_box_baseline(monkeypatch, defect):
    fixture_release(monkeypatch)
    value = review()
    if defect == 'missing':
        del value['box_baseline']
    elif defect == 'other_box':
        value['box_baseline']['sha'] = '9' * 40
    elif defect == 'missing_role':
        del value['box_baseline']['services']['orb-schwab']
    else:
        del value['box_baseline']['services']['orb-schwab']['ExecMainStartTimestampMonotonic']
    with pytest.raises(ValueError, match='baseline'):
        make_release.release('b' * 40, 'a' * 40, 'c' * 40, value)
