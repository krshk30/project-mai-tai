"""Stage one immutable approved package and its timer; never start the installer."""
import argparse
import io
import json
import subprocess
import tarfile

from make_release import digest, release

REMOTE = r'''
import hashlib, io, json, os, pathlib, subprocess, sys, tarfile
from datetime import datetime
from zoneinfo import ZoneInfo
now = datetime.now(ZoneInfo('America/New_York'))
assert now.date().isoformat() == '2026-10-08' and now.hour >= 16, 'NO STAGING BEFORE CLOSE / wrong date'
job = pathlib.Path('/home/trader/after-hours/2026-10-08/gapline-wbquiet/job')
raw = sys.stdin.buffer.read(4000001)
assert len(raw) <= 4000000 and os.geteuid() == 0
files = {}
with tarfile.open(fileobj=io.BytesIO(raw)) as archive:
    for member in archive.getmembers():
        assert member.isfile() and pathlib.Path(member.name).name == member.name
        assert member.name not in files and member.size <= 1000000
        files[member.name] = archive.extractfile(member).read()
manifest = json.loads(files['release.json'])
approval = json.loads(files['approval.json'])
h = lambda value: hashlib.sha256(value).hexdigest()
assert approval['manifest_sha256'] == h(files['release.json'])
assert approval['approved_sha'] == manifest['approved_sha'] and approval['decision'] == 'APPROVED'
assert approval['plan_commit'] == manifest['plan_commit'] and approval['authority'] == 'operator-standing-go'
assert manifest['date_et'] == '2026-10-08' and manifest['scope'] == 'oct8-p1-bagh-migration0023-four-services'
assert {'runner.py','run.sh','gate_readonly.py','proof_readonly.py','repin_preopen.py','retire_orb.py','migration0023.py','health_view.py','approved-migrations.tar','official_v2_restart_evidence.py','candidate-review.json'} <= set(manifest['artifacts'])
assert set(files) == set(manifest['artifacts']) | {'release.json', 'approval.json'}
assert all(h(files[name]) == expected for name, expected in manifest['artifacts'].items())
job.parent.mkdir(parents=True, exist_ok=True)
job.mkdir(mode=0o755, exist_ok=False)
for name, value in files.items():
    path = job / name
    with path.open('xb') as stream:
        stream.write(value)
    path.chmod(0o644)
assert all(h((job / name).read_bytes()) == h(value) for name, value in files.items())
unit = 'project-mai-tai-gapline-wbquiet-20261008'
backups = {}
for suffix in ('.service', '.timer'):
    path = pathlib.Path('/etc/systemd/system') / (unit + suffix)
    if path.exists():
        backup = job / (unit + suffix + '.before')
        backup.write_bytes(path.read_bytes())
        backups[str(path)] = {'backup': str(backup), 'sha256': h(backup.read_bytes())}
    path.write_bytes(files[unit + suffix])
    path.chmod(0o644)
subprocess.run(['systemd-analyze','verify',str(job / (unit + '.service')),str(job / (unit + '.timer'))], check=True)
subprocess.run(['systemctl','daemon-reload'], check=True)
subprocess.run(['systemctl','enable','--now',unit + '.timer'], check=True)
next_fire = subprocess.run(['systemctl','show',unit + '.timer','--property=NextElapseUSecRealtime,ActiveState,UnitFileState'], capture_output=True,text=True,check=True).stdout
receipt = {'approved_sha':manifest['approved_sha'], 'plan_commit':manifest['plan_commit'],
           'manifest_sha256':h(files['release.json']), 'approval_sha256':h(files['approval.json']),
           'staged_hashes':{name:h((job / name).read_bytes()) for name in sorted(files)},
           'unit_backups':backups, 'timer':next_fire, 'application_action':False}
(job / 'staging-receipt.json').write_text(json.dumps(receipt,sort_keys=True,indent=2)+'\n')
print(json.dumps(receipt,sort_keys=True,indent=2))
'''


def package(plan, approved, box, review, ledger=None):
    manifest, approval, blobs = release(plan, approved, box, review, ledger)
    values = {**blobs, 'release.json': manifest, 'approval.json': approval}
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode='w') as archive:
        for name, raw in sorted(values.items()):
            item = tarfile.TarInfo(name)
            item.size, item.mode, item.mtime = len(raw), 0o644, 0
            archive.addfile(item, io.BytesIO(raw))
    return buffer.getvalue(), manifest


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--plan', required=True)
    parser.add_argument('--approved-sha', required=True)
    parser.add_argument('--box-sha', required=True)
    parser.add_argument('--review-receipt', type=argparse.FileType('r'), required=True)
    parser.add_argument('--review-ledger', required=True)
    parser.add_argument('--host', default='mai-tai-vps')
    parser.add_argument('--check-only', action='store_true')
    args = parser.parse_args()
    raw, manifest = package(args.plan, args.approved_sha, args.box_sha, json.load(args.review_receipt), args.review_ledger)
    if args.check_only:
        print(json.dumps(dict(manifest_sha256=digest(manifest), package_sha256=digest(raw), bytes=len(raw))))
        return
    quoted = "'" + REMOTE.replace("'", "'\\''") + "'"
    result = subprocess.run(['ssh', args.host, 'sudo -n python3 -c ' + quoted], input=raw, check=True)
    return result.returncode


if __name__ == '__main__':
    main()
