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


def fixture_release(monkeypatch):
    names = ('runner.py', 'run.sh', 'gate_readonly.py', 'proof_readonly.py', 'repin_preopen.py',
             'project-mai-tai-gapline-wbquiet-20261008.service',
             'project-mai-tai-gapline-wbquiet-20261008.timer')
    blobs = {make_release.PREFIX + name: name.encode() for name in names}
    def git(*arguments):
        if arguments[0] == 'rev-parse':
            return arguments[1].split('^')[0].encode() + b'\n'
        if arguments[0] == 'ls-tree':
            return ('\n'.join(blobs) + '\n').encode()
        if arguments[0] == 'show':
            path = arguments[1].split(':', 1)[1]
            return blobs[path] if path in blobs else path.encode()
        if arguments[0] == 'merge-base':
            assert arguments[1] == '--is-ancestor'
            assert arguments[2] in make_release.PINNED.values()
            return b''
        raise AssertionError(arguments)
    monkeypatch.setattr(make_release, 'git', git)
    return names, blobs


def test_release_from_committed_blobs_binds_plan_app_baseline_and_each_byte(monkeypatch):
    names, blobs = fixture_release(monkeypatch)
    raw, approval, values = make_release.release('b' * 40, 'a' * 40, 'c' * 40)
    manifest, approval = json.loads(raw), json.loads(approval)
    assert manifest['plan_commit'] == 'b' * 40
    assert manifest['approved_sha'] == 'a' * 40
    assert manifest['box_sha'] == 'c' * 40
    assert set(manifest['artifacts']) == set(names)
    for name in names:
        assert manifest['artifacts'][name] == hashlib.sha256(blobs[make_release.PREFIX + name]).hexdigest()
        assert values[name] == blobs[make_release.PREFIX + name]
    assert approval['manifest_sha256'] == make_release.digest(raw)
    assert approval['authority'] == 'operator-standing-go'


def test_incomplete_committed_package_or_short_sha_cannot_be_sealed(monkeypatch):
    _, blobs = fixture_release(monkeypatch)
    del blobs[make_release.PREFIX + 'gate_readonly.py']
    with pytest.raises(ValueError, match='incomplete'):
        make_release.release('b' * 40, 'a' * 40, 'c' * 40)
    with pytest.raises(ValueError, match='full commit'):
        make_release.release('bbb', 'a' * 40, 'c' * 40)


def test_release_cannot_seal_gap_only_main_missing_wb_merge(monkeypatch):
    fixture_release(monkeypatch)
    original = make_release.git
    def refuse(*args):
        if args[:3] == ('merge-base', '--is-ancestor', make_release.PINNED['1124']):
            raise subprocess.CalledProcessError(1, ['git', *args])
        return original(*args)
    monkeypatch.setattr(make_release, 'git', refuse)
    with pytest.raises(subprocess.CalledProcessError):
        make_release.release('b' * 40, 'a' * 40, 'c' * 40)


def test_tar_has_exact_manifest_artifact_population_and_no_links(monkeypatch):
    names, _ = fixture_release(monkeypatch)
    raw, manifest = stage.package('b' * 40, 'a' * 40, 'c' * 40)
    with tarfile.open(fileobj=io.BytesIO(raw)) as archive:
        assert set(archive.getnames()) == set(names) | {'release.json', 'approval.json'}
        assert all(row.isfile() and row.mode == 0o644 for row in archive.getmembers())
        assert archive.extractfile('release.json').read() == manifest


def execute_isolated_stager(tmp_path, raw):
    job = tmp_path / 'job'
    units = tmp_path / 'systemd'
    units.mkdir()
    # Substitute ONLY the filesystem root and systemd interface in this rehearsal.
    source = stage.REMOTE.replace('/home/trader/after-hours/2026-10-08/gapline-wbquiet/job', str(job))
    source = source.replace('/etc/systemd/system', str(units))
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
    raw, manifest = stage.package('b' * 40, 'a' * 40, 'c' * 40)
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
    raw, _ = stage.package('b' * 40, 'a' * 40, 'c' * 40)
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
