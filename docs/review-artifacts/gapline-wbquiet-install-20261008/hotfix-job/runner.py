"""Exact-byte Oct 8 after-close deployment; no broker or ledger mutation."""
import argparse
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import time
from zoneinfo import ZoneInfo

REPO = Path('/home/trader/project-mai-tai')
ENV = Path('/etc/project-mai-tai/project-mai-tai.env')
CATALOG = Path('/home/trader/restart_evidence/expected_flags.json')
DAILY = Path('/home/trader/preopen-daily')
PYTHON = REPO / '.venv/bin/python'
GAP = 'MAI_TAI_STRATEGY_SCHWAB_1M_V2_GAP_LINE_CARRY_ENABLED'
LINE = 'MAI_TAI_STRATEGY_SCHWAB_1M_V2_LINE_CHART_RESTORATION_ENABLED'
HANDOFF = 'MAI_TAI_STRATEGY_SCHWAB_1M_V2_ATR_REPRICE_HANDOFF_ENABLED'
FALSE_FLIP = 'MAI_TAI_STRATEGY_SCHWAB_1M_V2_FALSE_FLIP_ENABLED'
RETAINED = ('MAI_TAI_OMS_V2_WEBULL_MIRROR_RETAINED_HOLD_ENABLED',
            'MAI_TAI_STRATEGY_SCHWAB_1M_V2_SLOTCLEAR_FRESH_FLIP_ENABLED',
            'MAI_TAI_STRATEGY_SCHWAB_1M_V2_SLOTCLEAR_FRESH_SELL_ENABLED')
UNITS = ('oms', 'schwab-1m-v2', 'strategy')
SCOPE = 'oct8-falseflip-pg-oms-v2-no-migration'
BASELINE_SHA = 'eced4599d05e72adab77551f18df8050a94028e2'
DAY = '2026-10-08'
CLOCK_REASON = ('Operator 2026-10-08 after-close GO: waive clock proxy only; '
                'native fresh zero-armed, managed-row and broker-flat gates remain mandatory')


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def canonical(value):
    return (json.dumps(value, sort_keys=True, indent=2) + '\n').encode()


def need(condition, message):
    if not condition:
        raise RuntimeError(message)


class WaitWork(RuntimeError):
    """Measured work appearing before the first write waits for a later tick."""


def window(now):
    local = now.astimezone(ZoneInfo('America/New_York'))
    if local.date().isoformat() != DAY:
        return 'EXPIRED'
    return 'READY' if local.hour >= 16 else 'BEFORE_CLOSE'


def native_v2_command(now, *, continuation=False):
    command = ['bash', str(REPO / 'ops/preflight/preflight_v2_restart.sh')]
    local = now.astimezone(ZoneInfo('America/New_York'))
    authorized = local.date().isoformat() == DAY and local.hour >= 16
    if local.hour < 18 and (authorized or continuation):
        command += ['--clock-override', CLOCK_REASON, '--i-accept-clock']
    return command




def verify_package(job):
    raw = (job / 'release.json').read_bytes()
    release = json.loads(raw)
    approval = json.loads((job / 'approval.json').read_bytes())
    need(approval['decision'] == 'APPROVED' and approval['authority'] == 'operator-standing-go'
         and approval['manifest_sha256'] == digest(raw)
         and approval['approved_sha'] == release['approved_sha']
         and approval['plan_commit'] == release['plan_commit'], 'approval/manifest binding differs')
    for key in ('approved_sha', 'plan_commit', 'box_sha'):
        need(re.fullmatch('[0-9a-f]{40}', release[key]) is not None, 'incomplete SHA ' + key)
    need(release['date_et'] == DAY and release['scope'] == SCOPE, 'foreign release scope/date')
    need(release['line_enabled'] is True, 'current operator GO requires LINE ON')
    need(re.fullmatch(r'codex/install-2026-10-08-[0-9a-f]{12}', release['release_branch']) is not None,
         'release must use the immutable exact-main-head branch')
    for name, expected in release['artifacts'].items():
        need(Path(name).name == name and name not in ('release.json', 'approval.json'), 'unsafe artifact name')
        path = job / name
        need(path.is_file() and not path.is_symlink() and digest(path.read_bytes()) == expected,
             'staged artifact hash differs: ' + name)
    need(release['box_sha'] == BASELINE_SHA and release['approved_sha'] != BASELINE_SHA,
         'PG hotfix must bind the installed eced baseline and a new reviewed landing')
    need({'runner.py', 'gate_readonly.py', 'proof_readonly.py', 'repin_preopen.py',
          'flag_audit.py', 'catalog_inventory.py', 'bookkeeping.py', 'removed_wait_readonly.py', 'health_view.py',
          'official_v2_restart_evidence.py', 'candidate-review.json', 'run.sh'}
         <= set(release['artifacts']), 'incomplete runtime artifact set')
    need(not {'migration0023.py', 'approved-migrations.tar', 'retire_orb.py'} & set(release['artifacts']),
         'PG hotfix package cannot contain migration or retirement actions')
    candidate = json.loads((job / 'candidate-review.json').read_bytes())
    need(candidate['approved_sha'] == release['approved_sha'] and candidate['line_enabled'] == release['line_enabled']
         and candidate['landed_prs'] == release['landed_reviews'], 'candidate review binding differs')
    need(set(candidate['candidate_prs']) == set(candidate['landed_prs'])
         and set(candidate['candidate_prs']) == {'1137'},
         'candidate outside the independently reviewed PG-only hotfix')
    return release


def retry_read(call, sleep=time.sleep, record=lambda attempt, rc: None):
    for attempt in range(1, 4):
        rc = call(attempt)
        record(attempt, rc)
        if rc != 2 or attempt == 3:
            return rc
        sleep(60)
    raise AssertionError('unreachable')


def save(path, value):
    path.write_bytes(canonical(value))
    path.chmod(0o600)


class Run:
    def __init__(self, job, release, attempt):
        self.job, self.release, self.attempt = job, release, attempt
        self.stage = 'initial-read-only'
        self.sequence = 0
        self.events = []

    def note(self, **event):
        event.update(at_utc=datetime.now(timezone.utc).isoformat(), stage=self.stage)
        self.events.append(event)
        with (self.attempt / 'runner.log').open('a') as stream:
            stream.write(json.dumps(event, sort_keys=True) + '\n')
        with (self.job / 'deployments-20261008.md').open('a') as stream:
            stream.write(event['at_utc'] + ' ' + self.stage + ' ' + json.dumps(event, sort_keys=True) + '\n')
        print(json.dumps(event, sort_keys=True), flush=True)

    def command(self, command, *, timeout=1200):
        self.sequence += 1
        stem = f'{self.sequence:03d}-{self.stage}'
        out, err = self.attempt / (stem + '.stdout'), self.attempt / (stem + '.stderr')
        environment = {key: value for key, value in os.environ.items()
                       if key != 'TZ' and not key.startswith('MAI_TAI_')}
        environment.update(PYTHONDONTWRITEBYTECODE='1', PYTHONPATH=str(REPO / 'src'))
        self.note(command=command, stdout=str(out), stderr=str(err))
        with out.open('wb') as stdout, err.open('wb') as stderr:
            try:
                result = subprocess.run(command, env=environment, stdout=stdout,
                                        stderr=stderr, timeout=timeout, check=False)
                rc = result.returncode
            except subprocess.TimeoutExpired:
                rc = 124
        self.note(rc=rc, stdout_sha256=digest(out.read_bytes()), stderr_sha256=digest(err.read_bytes()))
        return rc, out, err

    def checked(self, command, *, timeout=1200):
        rc, out, err = self.command(command, timeout=timeout)
        need(rc == 0, f'command failed rc={rc} stdout={out} stderr={err}')
        return out

    def gate(self, label):
        self.stage = label
        def read(number):
            rc, _, _ = self.command([str(PYTHON), str(self.job / 'gate_readonly.py')], timeout=180)
            return rc
        return retry_read(read, record=lambda attempt, rc: self.note(read_attempt=attempt, rc=rc))

    def native_rehearsal(self, names=('oms', 'v2'), *, continuation=False):
        for name in names:
            self.stage = 'native-readonly-' + name
            command = (native_v2_command(datetime.now(timezone.utc), continuation=continuation) if name == 'v2'
                       else ['bash', str(REPO / 'ops/preflight/preflight_oms_restart.sh')])
            rc = retry_read(lambda number: self.command(command, timeout=180)[0],
                            record=lambda attempt, code: self.note(read_attempt=attempt, rc=code))
            if rc == 1:
                message = 'native read-only fence refused ' + name + '; no armed or trading-gate override'
                if continuation:
                    raise RuntimeError(message)
                raise WaitWork(message)
            need(rc == 0, 'native read-only fence unreadable ' + name)

    def backup_write(self, path, raw):
        before = path.read_bytes()
        backup = self.attempt / (path.name + '.before')
        need(not backup.exists() and not path.is_symlink(), 'backup collision/unsafe destination')
        backup.write_bytes(before)
        backup.chmod(0o600)
        stat = path.stat()
        temporary = path.with_name(path.name + '.oct8-install-new')
        with temporary.open('xb') as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, stat.st_mode & 0o777)
        os.chown(temporary, stat.st_uid, stat.st_gid)
        os.replace(temporary, path)
        self.note(path=str(path), backup=str(backup), before_sha256=digest(before), after_sha256=digest(raw))

    def identities(self):
        rows = {}
        for name in UNITS:
            out = self.checked(['systemctl', 'show', f'project-mai-tai-{name}.service',
                '--property=MainPID,ActiveState,SubState,NRestarts,ExecMainStartTimestamp'])
            rows[name] = dict(line.split('=', 1) for line in out.read_text().splitlines())
        return rows

    def install(self):
        self.stage = 'source-precheck'
        status = self.checked(['sudo', '-u', 'trader', 'git', '-C', str(REPO), 'status', '--porcelain'])
        need(not status.read_bytes().strip(), 'checkout has local changes; do not overwrite')
        head = self.checked(['sudo', '-u', 'trader', 'git', '-C', str(REPO), 'rev-parse', 'HEAD'])
        need(head.read_text().strip() == self.release['box_sha'], 'box baseline advanced; recut release without app action')
        self.checked(['sudo', '-u', 'trader', 'git', '-C', str(REPO), 'fetch', 'origin', self.release['release_branch']])
        resolved = self.checked(['sudo', '-u', 'trader', 'git', '-C', str(REPO), 'rev-parse', 'FETCH_HEAD'])
        need(resolved.read_text().strip() == self.release['approved_sha'], 'immutable application ref differs')
        self.checked(['sudo', '-u', 'trader', 'git', '-C', str(REPO), 'merge-base', '--is-ancestor',
                      self.release['box_sha'], self.release['approved_sha']])
        for relative, expected in self.release['source_hashes'].items():
            blob = self.checked(['sudo', '-u', 'trader', 'git', '-C', str(REPO), 'show',
                                 self.release['approved_sha'] + ':' + relative])
            need(digest(blob.read_bytes()) == expected, 'approved source blob differs: ' + relative)
        need(digest((REPO / 'ops/systemd/deploy_service.sh').read_bytes())
             == self.release['baseline_deploy_sha256'], 'baseline deploy script differs')
        self.stage = 'baseline-proof'
        self.checked([str(PYTHON), str(self.job / 'proof_readonly.py'), 'baseline',
                      '--output', str(self.attempt / 'proof-before.json')])
        captured = json.loads((self.attempt / 'proof-before.json').read_bytes())
        need(set(captured.get('services', {})) == set(self.release['baseline_identities']),
             'baseline fleet unreadable; no arbitrary identity adoption')
        for role, expected in self.release['baseline_identities'].items():
            need(all(captured['services'][role].get(key) == expected[key] for key in
                 ('MainPID', 'NRestarts', 'ActiveState', 'SubState', 'InvocationID', 'ExecMainStartTimestampMonotonic')),
                 'baseline identity changed: ' + role)
        self.checked([str(PYTHON), str(self.job / 'official_v2_restart_evidence.py'), 'snapshot',
                      '--output', str(self.attempt / 'before-restart.json')])
        identities = self.identities()
        for role, current in identities.items():
            need(all(current[key] == str(captured['services'][role][key]) for key in
                     ('MainPID', 'NRestarts', 'ActiveState', 'SubState')),
                 'identity changed after baseline snapshot: ' + role)
        env_hash = digest(ENV.read_bytes())
        numeric = Path('/home/trader/restart_evidence/expected_numeric.json')
        numeric_hash = digest(numeric.read_bytes())
        self.stage = 'installed-schema-readonly'
        from bookkeeping import prior_install
        prior = prior_install(self.release['prior_install'])
        from flag_audit import command as flag_command
        # Validate the already-reviewed audit source before any application write.
        flag_command(PYTHON)
        self.checked([str(PYTHON), str(self.job / 'removed_wait_readonly.py'),
                      '--output', str(self.attempt / 'removed-wait-before.json')])
        before_restore = json.loads((self.attempt / 'removed-wait-before.json').read_bytes())
        self.note(removed_wait_before=before_restore, existing_rows_action='none; do not archive or force zero')
        blob = self.checked(['sudo', '-u', 'trader', 'git', '-C', str(REPO), 'show',
                             self.release['approved_sha'] + ':ops/health/expected_flags.json'])
        from catalog_inventory import candidate
        catalog, inventory = candidate(CATALOG.read_bytes(), blob.read_bytes())
        self.native_rehearsal()
        rc = self.gate('final-before-first-write')
        if rc == 1:
            raise WaitWork('measured work appeared before first write')
        need(rc == 0, 'trading gate UNKNOWN immediately before write')
        need(self.identities() == identities, 'restart identity changed during final gate; no adoption')
        need(window(datetime.now(timezone.utc)) == 'READY', 'first write outside Oct8 after-close window')
        claim = self.job / 'write-started.json'
        with claim.open('xb') as stream:
            stream.write(canonical(dict(attempt=str(self.attempt), approved_sha=self.release['approved_sha'],
                                        at_utc=datetime.now(timezone.utc).isoformat())))
        self.stage = 'authorized-catalog-namespace'
        self.backup_write(CATALOG, catalog)
        save(self.attempt / 'catalog-namespace.json', inventory)
        for target in ('oms', 'schwab-1m-v2'):
            need(self.gate('before-deploy-' + target) == 0, 'fresh trading gate blocked before ' + target)
            if target == 'schwab-1m-v2':
                self.native_rehearsal(('v2',), continuation=True)
            self.stage = 'deploy-' + target
            from health_view import health_view
            with health_view(lambda event: self.note(**event)) as health_url:
                self.checked(['sudo', '-u', 'trader', 'env',
                              'MAI_TAI_EXPECTED_SHA=' + self.release['approved_sha'],
                              'MAI_TAI_RUN_MIGRATIONS=0', 'MAI_TAI_ALLOW_LIVE_RESTART=0',
                              'APP_HEALTH_URL=' + health_url,
                              'bash', str(REPO / 'ops/systemd/deploy_service.sh'), str(REPO),
                              self.release['release_branch'], target], timeout=1800)
            self.note(deploy_finished=target, identities=self.identities())
        self.stage = 'post-install'
        after = self.identities()
        for name, row in after.items():
            need(row['ActiveState'] == 'active' and row['SubState'] == 'running'
                 and row['MainPID'] != identities[name]['MainPID'] and int(row['MainPID']) > 0
                 and row['NRestarts'] == '0', 'failed start or wrong identity: ' + name)
        save(self.attempt / 'service-identities.json', after)
        need(digest(ENV.read_bytes()) == env_hash, 'hotfix changed environment; no waiver or recovery authorized')
        need(digest(numeric.read_bytes()) == numeric_hash, 'hotfix changed numeric policy')
        self.checked([str(PYTHON), str(self.job / 'removed_wait_readonly.py'),
                      '--output', str(self.attempt / 'removed-wait-after.json')])
        after_restore = json.loads((self.attempt / 'removed-wait-after.json').read_bytes())
        self.note(removed_wait_after=after_restore, direct_boot_count='UNMEASURED; no source count marker')
        need(after_restore['restored_count'] == 0,
             'current removed-wait restore count is not zero; preserve rows and actual receipt')
        need(self.gate('post-install-trading-read') == 0, 'post-install trading gate blocked')
        self.stage = 'ten-minute-observation'
        self.note(observation_seconds=600, restart_identities=after)
        time.sleep(600)
        self.checked([str(PYTHON), str(self.job / 'proof_readonly.py'), 'after',
                      '--baseline', str(self.attempt / 'proof-before.json'),
                      '--approved-sha', self.release['approved_sha'],
                      '--line-enabled', str(self.release['line_enabled']).lower(),
                      '--output', str(self.attempt / 'post-install-proof.json')])
        self.stage = 'flag-audit'
        from flag_audit import receipt as flag_receipt, readable_outcome
        args = flag_command(PYTHON)
        rc, flags, errors = self.command(args)
        audit = flag_receipt(args, rc, flags, errors)
        save(self.attempt / 'flaggate.json', audit)
        need(readable_outcome(audit), 'current flag audit mismatch or non-paper UNKNOWN; raw receipt preserved')
        unknown = [line for line in audit['lines'] if line.startswith('UNKNOWN')]
        self.stage = 'preopen-bookkeeping'
        from bookkeeping import combined_record
        # Re-pin control's earlier authorized restart, without restarting it again.
        record, journal = combined_record(prior, captured, after, self.events)
        save(self.attempt / 'sealed-actions.json', journal)
        record['source_journal'] = str(self.attempt / 'sealed-actions.json')
        save(self.attempt / 'install-record.json', record)
        save(self.attempt / 'bookkeeping-original-snapshot.json', prior['snapshot'])
        self.checked([str(PYTHON), str(self.job / 'repin_preopen.py'),
                      '--approved-sha', self.release['approved_sha'],
                      '--snapshot', str(self.attempt / 'bookkeeping-original-snapshot.json'),
                      '--install-record', str(self.attempt / 'install-record.json'),
                      '--line-enabled', str(self.release['line_enabled']).lower(),
                      '--receipt', str(self.attempt / 'preopen-repin.json')])
        self.checked(['bash', '-n', '/home/trader/preopen.sh'])
        proof = json.loads((self.attempt / 'post-install-proof.json').read_bytes())
        self.stage = 'COMPLETE'
        self.note(approved_sha=self.release['approved_sha'], plan_commit=self.release['plan_commit'],
                  release_sha256=digest((self.job / 'release.json').read_bytes()),
                  flaggate_rc=rc, paper_unknown=unknown,
                  post_install_assessment=proof['assessment'],
                  morning_scanner_acceptance='UNMEASURED')
        save(self.job / 'COMPLETE.json', dict(attempt=str(self.attempt), approved_sha=self.release['approved_sha'],
             at_utc=datetime.now(timezone.utc).isoformat(), runner_log_sha256=digest((self.attempt / 'runner.log').read_bytes()),
             removed_wait_before=before_restore, removed_wait_after=after_restore,
             removed_wait_receipts=[str(self.attempt / 'removed-wait-before.json'), str(self.attempt / 'removed-wait-after.json')],
             direct_boot_count='UNMEASURED; source has no count marker'))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--job', type=Path, default=Path(__file__).resolve().parent)
    args = parser.parse_args()
    need(os.geteuid() == 0, 'root required')
    release = verify_package(args.job)
    if any((args.job / name).exists() for name in ('COMPLETE.json', 'ABORT.json', 'write-started.json')):
        print('SEALED: no duplicate installer or implicit recovery')
        return 0
    if window(datetime.now(timezone.utc)) != 'READY':
        print('PENDING/EXPIRED: no application write outside Oct8 after close')
        return 0
    with Path('/run/lock/project-mai-tai-deploy.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print('PENDING: exclusive deployment lock busy')
            return 0
        attempt = args.job / ('attempt-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ'))
        attempt.mkdir(mode=0o700)
        run = Run(args.job, release, attempt)
        def interrupted(number, frame):
            raise RuntimeError('signal ' + str(number))
        signal.signal(signal.SIGTERM, interrupted)
        signal.signal(signal.SIGINT, interrupted)
        try:
            rc = run.gate('initial-trading-read')
            if rc == 1:
                run.note(verdict='WAIT_MEASURED_TRADING_CONDITION', no_application_write=True)
                return 0
            need(rc == 0, 'broker/source UNKNOWN after three read-only attempts')
            run.install()
            return 0
        except WaitWork as exc:
            need(not (args.job / 'write-started.json').exists(), 'WAIT is only allowed before write claim')
            run.note(verdict='WAIT_MEASURED_TRADING_CONDITION', reason=str(exc), no_application_write=True)
            return 0
        except Exception as exc:
            run.note(verdict='STOP', reason=str(exc), application_write_started=(args.job / 'write-started.json').exists())
            states = {}
            for name in UNITS:
                result = subprocess.run(['systemctl', 'show', 'project-mai-tai-' + name + '.service',
                    '--property=MainPID,ActiveState,SubState,Result'], capture_output=True, text=True, timeout=10)
                states[name] = dict(rc=result.returncode, stdout=result.stdout, stderr=result.stderr)
            save(args.job / 'ABORT.json', dict(stage=run.stage, reason=str(exc), states=states, attempt=str(attempt),
                 at_utc=datetime.now(timezone.utc).isoformat(), starts_on_abort=False, recovery_authorized=False))
            subprocess.run(['/home/trader/preopen_alert.sh', 'ERROR', 'Oct8 install STOP stage=' + run.stage
                            + ' receipt=' + str(args.job / 'ABORT.json'), str(args.job / 'ABORT.json')],
                           check=False, timeout=30)
            return 1


if __name__ == '__main__':
    raise SystemExit(main())
