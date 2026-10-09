"""Parameterized v2-only followup. No candidate or approval is invented here."""
import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import re
import subprocess
import signal
import time

import repin_preopen as repin
from runner import Run, REPO, PYTHON, canonical, digest, need, save, window

SCOPE = 'oct8-quote-guard-v2-only-no-migration-no-env'
DEPS = {'v2_only.py', 'repin_preopen.py', 'orb_receipts.py', 'removed_wait_readonly.py',
        'runner.py', 'gate_readonly.py', 'proof_readonly.py', 'health_view.py', 'flag_audit.py'}


def verify(job):
    release = json.loads((job / 'release.json').read_bytes())
    need(release.get('scope') == SCOPE and repin.sha(release.get('approved_sha'))
         and repin.sha(release.get('box_sha')), 'unbound/foreign v2-only release')
    need(re.fullmatch(r'codex/install-2026-10-08-[0-9a-f]{12}', release.get('release_branch', '')),
         'immutable release ref missing')
    need(release['release_branch'].endswith(release['approved_sha'][:12]), 'release ref/SHA differs')
    need(DEPS <= set(release['artifacts']), 'v2-only dependency omitted')
    for name, expected in release['artifacts'].items():
        need(Path(name).name == name and not (job / name).is_symlink()
             and digest((job / name).read_bytes()) == expected, 'artifact differs: ' + name)
    need(release.get('immutable_receipts'), 'historical failed seals omitted')
    for name, expected in release['immutable_receipts'].items():
        need(digest(Path(name).read_bytes()) == expected, 'historical receipt changed: ' + name)
    return release


def deploy_command(app, ref, health_url):
    return ['sudo', '-u', 'trader', 'env', 'MAI_TAI_EXPECTED_SHA=' + app,
            'MAI_TAI_RUN_MIGRATIONS=0', 'MAI_TAI_ALLOW_LIVE_RESTART=0',
            'APP_HEALTH_URL=' + health_url, 'bash', str(REPO / 'ops/systemd/deploy_service.sh'),
            str(REPO), ref, 'schwab-1m-v2']


@contextmanager
def proof_scope():
    import proof_readonly as proof
    restarted, original = proof.RESTARTED, proof.collect_baseline
    def capture():
        value = original()
        try:
            value['process_flags']['oms'] = proof.process_flags(value['services']['oms']['MainPID'])
        except Exception as exc:
            value['errors']['proc:oms'] = type(exc).__name__
        return value
    proof.RESTARTED = {'schwab-1m-v2'}
    proof.collect_baseline = capture
    try:
        yield proof
    finally:
        proof.RESTARTED, proof.collect_baseline = restarted, original


def bundle(root, app, baseline, after, deploy_rc, now, directory, events):
    binding_raw = repin.location(root, repin.DAILY + '/binding.json').read_bytes()
    binding = json.loads(binding_raw)
    old = binding['current_install']
    for path, expected in old['hashes'].items():
        need(digest(repin.location(root, path).read_bytes()) == expected, 'prior evidence changed: ' + path)
    record = json.loads(repin.location(root, old['install_record']).read_bytes())
    snapshot = repin.location(root, old['snapshot']).read_bytes()
    followup = dict(approved_sha=app, previous_sha=binding['approved_sha'], deploy_rc=deploy_rc,
                    captured_at_utc=baseline['captured_at_utc'], before=baseline['states'],
                    service_actions={'schwab-1m-v2': 'restarted'})
    text = repin.location(root, repin.GATE).read_text()
    ack = json.loads(repin.location(root, repin.DAILY + '/upgrade-ack.json').read_bytes())
    followup_path = directory + '/v2-followup.json'
    # Validate before publishing any re-pin; the receipt never claims a clean proof.
    target = repin.location(root, followup_path)
    with target.open('xb') as stream:
        stream.write(canonical(followup))
        os.fchmod(stream.fileno(), 0o600)
    repin.validate_v2_followup(root, followup_path, app, binding, ack, after, now, text)
    journal_path = directory + '/cumulative-actions.json'
    record['source_journal'] = journal_path
    journal = dict(prior_binding=binding, prior_binding_sha256=digest(binding_raw),
                   prior_verdict='FAIL/ABORT retained; no historical waiver', latest_events=events,
                   latest_service_actions=followup['service_actions'])
    for name, raw in (('original-snapshot.json', snapshot), ('install-record.json', canonical(record)),
                      ('cumulative-actions.json', canonical(journal))):
        with repin.location(root, directory + '/' + name).open('xb') as stream:
            stream.write(raw)
            os.fchmod(stream.fileno(), 0o600)
    return dict(snapshot=directory + '/original-snapshot.json', record=directory + '/install-record.json',
                followup=followup_path, orb_restart=old['orb_restart'], retirement=old['retirement'])


def execute(run):
    release = run.release
    need(run.gate('fresh-before-v2') == 0, 'fresh trading gate blocked')
    run.native_rehearsal(('v2',))
    initial = repin.collect(orb_completed=True)
    need(initial['head'] == release['box_sha'] and initial['clean'], 'baseline source differs')
    raw_env = Path('/etc/project-mai-tai/project-mai-tai.env').read_bytes()
    run.stage = 'exact-ref-read'
    output = run.checked(['sudo', '-u', 'trader', 'git', '-C', str(REPO), 'ls-remote', 'origin',
                          'refs/heads/' + release['release_branch']]).read_text().split()
    need(output == [release['approved_sha'], 'refs/heads/' + release['release_branch']], 'remote exact ref absent/moved')
    initial['captured_at_utc'] = datetime.now(timezone.utc).isoformat()
    with proof_scope() as proof:
        before = proof.collect_baseline()
        save(run.attempt / 'proof-before.json', before)
        need(run.gate('immediate-before-deploy-v2') == 0, 'fresh trading gate blocked')
        run.native_rehearsal(('v2',))
        live = repin.collect(orb_completed=True)
        need(live['states'] == initial['states'] and live['environments'] == initial['environments'],
             'baseline identities/flags changed before deploy')
        need(window(datetime.now(timezone.utc)) == 'READY', 'outside authorized after-close day')
        save(run.job / 'write-started.json', dict(at_utc=datetime.now(timezone.utc).isoformat(),
                                                approved_sha=release['approved_sha']))
        run.stage = 'deploy-v2-only'
        from health_view import health_view
        with health_view(lambda event: run.note(**event)) as health_url:
            run.checked(deploy_command(release['approved_sha'], release['release_branch'], health_url), timeout=1800)
        after = repin.collect(orb_completed=True)
        run.note(deploy_finished='schwab-1m-v2', identities=after['states'])
        need(after['environments'] == initial['environments']
             and Path('/etc/project-mai-tai/project-mai-tai.env').read_bytes() == raw_env,
             'v2-only followup changed environment')
        run.stage = 'post-install-trading-read'
        need(run.gate('post-install-trading-read') == 0, 'post-install trading gate blocked')
        run.stage = 'ten-minute-observation'
        time.sleep(600)
        result = proof.collect_after(before, release['approved_sha'])
        save(run.attempt / 'post-install-proof.json', result)
    run.stage = 'preopen-bookkeeping'
    args = bundle(Path('/'), release['approved_sha'], initial, repin.collect(orb_completed=True), 0,
                  datetime.now(timezone.utc), str(run.attempt), run.events)
    run.checked([str(PYTHON), str(run.job / 'repin_preopen.py'), '--approved-sha', release['approved_sha'],
                 '--snapshot', args['snapshot'], '--install-record', args['record'], '--v2-followup', args['followup'],
                 '--orb-restart', args['orb_restart'], '--retirement', args['retirement'], '--line-enabled', 'true',
                 '--receipt', str(run.attempt / 'preopen-repin.json')])
    run.stage = 'daily-runtime'
    run.command([str(PYTHON), '-c', "import sys; sys.path.insert(0, '/home/trader/preopen-daily'); "
                 "import daily; print(daily.verify_runtime()['approved_sha'])"])
    run.stage = 'official-checks-only'
    run.command(['bash', '/home/trader/preopen.sh'], timeout=360)
    from flag_audit import command as flags_command, receipt as flags_receipt
    command = flags_command(PYTHON)
    rc, stdout, stderr = run.command(command)
    save(run.attempt / 'flaggate.json', flags_receipt(command, rc, stdout, stderr))
    run.note(verdict=result['assessment']['verdict'], global_install_pass=False,
             note='official evening checks/previous failed evidence retained without waiver')
    save(run.job / 'FINISHED.json', dict(attempt=str(run.attempt), assessment=result['assessment'],
                                       runner_log_sha256=digest((run.attempt / 'runner.log').read_bytes())))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--job', type=Path, required=True)
    args = parser.parse_args()
    need(os.geteuid() == 0, 'root required')
    release = verify(args.job)
    need(not any((args.job / p).exists() for p in ('ABORT.json', 'FINISHED.json', 'write-started.json')), 'sealed package')
    with Path('/run/lock/project-mai-tai-deploy.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        attempt = args.job / ('attempt-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ'))
        attempt.mkdir(mode=0o700)
        run = Run(args.job, release, attempt)
        def interrupted(number, frame):
            raise RuntimeError('signal ' + str(number))
        signal.signal(signal.SIGTERM, interrupted)
        signal.signal(signal.SIGINT, interrupted)
        try:
            execute(run)
            return 0
        except Exception as exc:
            run.note(verdict='ABORT', reason=str(exc), no_recovery=True)
            states = subprocess.run(['systemctl', 'show', *['project-mai-tai-' + role + '.service'
                                     for role in ('oms', 'schwab-1m-v2', 'strategy', 'control', 'orb-schwab')],
                                     '--property=MainPID,ActiveState,SubState,Result'],
                                    capture_output=True, text=True, timeout=10)
            save(args.job / 'ABORT.json', dict(stage=run.stage, reason=str(exc), attempt=str(attempt),
                                             states=states.stdout, starts_on_abort=False))
            subprocess.run(['/home/trader/preopen_alert.sh', 'ERROR', 'v2-only STOP stage=' + run.stage,
                            str(args.job / 'ABORT.json')], check=False, timeout=30)
            return 1


if __name__ == '__main__':
    raise SystemExit(main())
