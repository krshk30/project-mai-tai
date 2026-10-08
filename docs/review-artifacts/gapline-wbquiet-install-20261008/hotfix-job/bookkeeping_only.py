"""Separate, evidence-bound preopen closure; never reruns the failed installer."""
import argparse
import ast
from datetime import datetime, timezone
import fcntl
import hashlib
import json
from pathlib import Path
import subprocess
import time

import bookkeeping as book
import repin_preopen as repin

APP = '06b5e388affb5f33e4edf80c6e635cf72dbc3f78'
JOB = Path('/home/trader/after-hours/2026-10-08/falseflip-pg-hotfix-r3/job')
ATTEMPT = JOB / 'attempt-20261008T211500954696Z'
DEST = Path('/home/trader/restart_evidence/hotfix-bookkeeping-06b5e388')
SEALED = {'runner.log': '50e015024767114d591e209ed55a2c88d328b9eec4588382ef9205a7d254cddc',
          'post-install-proof.json': 'ffe6b97ee86e927556e28deba5c77e99680a526409825adfd0350fd4e860b6ad',
          'ABORT.json': '6628baf0e018b0b2fc9a7a71cb440df32327b9a80867f8eed88d3837e1cb2b3d'}
PIDS = {'oms': '2094823', 'strategy': '2094834', 'schwab-1m-v2': '2096131', 'control': '2075090'}


def raw(path):
    repin.need(not path.is_symlink(), 'symlink evidence')
    result = path.read_bytes()
    repin.need(len(result) <= 4_000_000, 'oversized evidence')
    return result


def sealed():
    for name, expected in SEALED.items():
        path = JOB / name if name == 'ABORT.json' else ATTEMPT / name
        repin.need(hashlib.sha256(raw(path)).hexdigest() == expected, 'sealed receipt differs: ' + name)


def records():
    sealed()
    release = json.loads(raw(JOB / 'release.json'))
    repin.need(release['approved_sha'] == APP, 'foreign hotfix release')
    prior = book.prior_install(release['prior_install'])
    events = [json.loads(line) for line in raw(ATTEMPT / 'runner.log').decode().splitlines()]
    record, journal = book.combined_record(prior, json.loads(raw(ATTEMPT / 'proof-before.json')),
                                         json.loads(raw(ATTEMPT / 'service-identities.json')), events)
    journal.update(hotfix_verdict='FAIL: four historical v2 ERRORs; ABORT preserved; never COMPLETE',
                   hotfix_abort_sha256=SEALED['ABORT.json'],
                   hotfix_failed_proof_sha256=SEALED['post-install-proof.json'])
    record['source_journal'] = str(DEST / 'sealed-actions.json')
    return {str(DEST / 'original-snapshot.json'): repin.canonical(prior['snapshot']),
            str(DEST / 'install-record.json'): repin.canonical(record),
            str(DEST / 'sealed-actions.json'): repin.canonical(journal)}


def exact_identities(observed):
    for role, pid in PIDS.items():
        repin.need(observed['states'][role]['MainPID'] == pid, 'authorized installed PID differs: ' + role)
    before = json.loads(raw(ATTEMPT / 'proof-before.json'))['services']
    from proof_readonly import service_identity
    for role in before:
        if role in PIDS:
            continue
        current = service_identity(role)
        keys = ('MainPID', 'NRestarts', 'ActiveState', 'SubState', 'InvocationID', 'ExecMainStartTimestampMonotonic')
        repin.need(all(current[key] == before[role][key] for key in keys), 'untouched identity changed: ' + role)


def gate(run=subprocess.run, sleep=time.sleep):
    attempts = []
    for number in range(1, 4):
        result = run(['/home/trader/project-mai-tai/.venv/bin/python', str(JOB / 'gate_readonly.py')],
                     capture_output=True, text=True, timeout=240)
        attempts.append(dict(attempt=number, rc=result.returncode, stdout=result.stdout, stderr=result.stderr))
        if result.returncode != 2 or number == 3:
            break
        sleep(60)
    return attempts


def preview(files, observations, now):
    class Memory:
        def __init__(self, content):
            self.content = content
        def read_bytes(self):
            return self.content
    original = repin.location
    try:
        repin.location = lambda root, name: Memory(files[name]) if name in files else original(root, name)
        return repin.plan(Path('/'), APP, str(DEST / 'original-snapshot.json'),
                          str(DEST / 'install-record.json'), observations, now, line_enabled=True)
    finally:
        repin.location = original


def write_new(path, content):
    with path.open('xb') as stream:
        import os
        os.fchmod(stream.fileno(), 0o600)
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    with Path('/run/lock/project-mai-tai-deploy.lock').open('a') as install_lock:
        fcntl.flock(install_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with Path(repin.DAILY + '/run.lock').open('r+') as daily_lock:
            fcntl.flock(daily_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            attempts = gate()
            print(json.dumps(dict(trading_gate_attempts=attempts)), flush=True)
            repin.need(attempts[-1]['rc'] == 0, 'fresh trading gate blocked or unreadable')
            files = records()
            observations = repin.collect()
            exact_identities(observations)
            now = datetime.now(timezone.utc)
            changes = preview(files, observations, now)
            for name, content in changes.items():
                if name.endswith('.py'):
                    ast.parse(content)
            subprocess.run(['bash', '-n'], input=changes[repin.GATE], check=True, capture_output=True, timeout=5)
            result = dict(verdict='PREVIEW_CHECKS_NOT_RUN', approved_sha=APP, measured_at_utc=now.isoformat(),
                          candidate_hashes={name: repin.digest(content) for name, content in changes.items()},
                          schema_transition=json.loads(files[str(DEST / 'install-record.json')])['schema_transition'],
                          states=observations['states'], sealed_hashes=SEALED, writes_performed=False)
            if args.apply:
                repin.need(not DEST.exists() and not DEST.is_symlink(), 'bookkeeping directory already exists')
                DEST.mkdir(mode=0o700)
                for name, content in files.items():
                    write_new(Path(name), content)
                write_new(DEST / 'fresh-gate.json', repin.canonical(attempts))
                # Existing helper backs up all six targets and publishes runtime last.
                result.update(repin.apply(Path('/'), changes, now), writes_performed=True)
                write_new(DEST / 'preopen-repin.json', repin.canonical(result))
            sealed()
            print(json.dumps(result, sort_keys=True), flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
