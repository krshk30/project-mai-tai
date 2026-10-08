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
UNITS = ('oms', 'schwab-1m-v2', 'strategy', 'control')
SCOPE = 'oct8-p1-bagh-migration0023-four-services'
DAY = '2026-10-08'


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


def env_candidate(raw, line_enabled=True):
    text = raw.decode('utf-8')
    bindings = {}
    for index, line in enumerate(text.splitlines(keepends=True)):
        match = re.match(r'^\s*(?:export\s+)?([A-Za-z_][A-Za-z_0-9]*)\s*=(.*)', line)
        if match:
            bindings.setdefault(match[1].upper(), []).append((index, match[1], match[2].strip()))
    for key in (GAP, LINE, HANDOFF, FALSE_FLIP, *RETAINED):
        rows = bindings.get(key, [])
        need(len(rows) <= 1 and all(row[1] == key for row in rows), 'duplicate/aliased env key ' + key)
    for key in (LINE, HANDOFF):
        rows = bindings.get(key, [])
        need(len(rows) == 1 and rows[0][2] in ('false', 'true'), key + ' must remain explicit/readable')
    need(bindings[HANDOFF][0][2] == 'false', HANDOFF + ' must remain false')
    for key in RETAINED:
        need(len(bindings.get(key, [])) == 1 and bindings[key][0][2] == 'true', 'retained ON flag differs ' + key)
    lines = text.splitlines(keepends=True)
    lines[bindings[LINE][0][0]] = LINE + '=' + str(line_enabled).lower() + '\n'
    for key in (GAP, FALSE_FLIP):
        rows = bindings.get(key, [])
        if rows:
            need(rows[0][2] in ('false', 'true'), 'unreadable activation env value ' + key)
            lines[rows[0][0]] = key + '=true\n'
        else:
            if lines and not lines[-1].endswith('\n'):
                lines[-1] += '\n'
            lines.append(key + '=true\n')
    return ''.join(lines).encode()


def catalog_candidate(raw, approved, line_enabled=True):
    current = json.loads(raw)
    names = [row['name'] for row in current['flags']]
    need(len(names) == len(set(names)), 'duplicate catalog rows')
    gap_name = 'strategy_schwab_1m_v2_gap_line_carry_enabled'
    gap = [row for row in approved['flags'] if row['name'] == gap_name]
    need(len(gap) == 1 and gap[0]['expected'] is True
         and gap[0]['owning_service'] == 'schwab-1m-v2', 'approved GAP catalog row unreadable')
    false_flip = [row for row in approved['flags'] if row['name'] == 'strategy_schwab_1m_v2_false_flip_enabled']
    need(len(false_flip) == 1 and false_flip[0]['expected'] is True
         and false_flip[0]['owning_service'] == 'schwab-1m-v2'
         and 'oms' in false_flip[0].get('also_check_services', []), 'approved FALSE_FLIP catalog row unreadable')
    approved_names = [row['name'] for row in approved['flags']]
    need(len(approved_names) == len(set(approved_names)), 'duplicate approved catalog rows')
    # Retain actual box inventory/rulings. Only these two reviewed activations
    # may be added/replaced; retired-process policy is not this installation.
    for addition in (gap[0], false_flip[0]):
        existing = [row for row in current['flags'] if row['name'] == addition['name']]
        if existing:
            current['flags'][current['flags'].index(existing[0])] = addition
        else:
            current['flags'].append(addition)
    for row in current['flags']:
        if row['name'] == 'strategy_schwab_1m_v2_line_chart_restoration_enabled':
            row['expected'] = line_enabled
            row['ruling'] = ('Operator 2026-10-08: LINESRC2 reviewed and merged; LINE ON at after-close install'
                             if line_enabled else 'Operator pre-close veto: LINE stays OFF; no activation inferred')
        if row['name'] == 'strategy_schwab_1m_v2_atr_reprice_handoff_enabled':
            need(row['expected'] is False, 'box RPG expectation must already remain false')
    need(any(row['name'] == 'strategy_schwab_1m_v2_atr_reprice_handoff_enabled'
             and row['expected'] is False for row in current['flags']), 'box RPG catalog must remain false')
    return canonical(current)


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
    need({'runner.py', 'gate_readonly.py', 'proof_readonly.py', 'repin_preopen.py', 'retire_orb.py', 'migration0023.py',
          'approved-migrations.tar', 'health_view.py',
          'official_v2_restart_evidence.py', 'candidate-review.json', 'completed-retirement.json', 'run.sh'}
         <= set(release['artifacts']), 'incomplete runtime artifact set')
    candidate = json.loads((job / 'candidate-review.json').read_bytes())
    need(candidate['approved_sha'] == release['approved_sha'] and candidate['line_enabled'] == release['line_enabled']
         and candidate['landed_prs'] == release['landed_reviews'], 'candidate review binding differs')
    need(set(candidate['candidate_prs']) == set(candidate['landed_prs'])
         and {'1126', '1127', '1130', '1134', '1135'} <= set(candidate['candidate_prs'])
         and set(candidate['candidate_prs']) <= {'1126', '1127', '1130', '1134', '1135', '1136'},
         'candidate outside reviewed P1-only install capability')
    need(json.loads((job / 'completed-retirement.json').read_bytes()) == candidate['completed_retirement'],
         'completed retirement receipt binding differs')
    from retire_orb import validate_receipt
    validate_receipt(candidate['completed_retirement'])
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

    def native_rehearsal(self):
        for name in ('oms', 'v2'):
            self.stage = 'native-readonly-' + name
            command = ['bash', str(REPO / 'ops/preflight' / ('preflight_' + name + '_restart.sh'))]
            rc = retry_read(lambda number: self.command(command, timeout=180)[0],
                            record=lambda attempt, code: self.note(read_attempt=attempt, rc=code))
            if rc == 1:
                raise WaitWork('native read-only fence refused ' + name + '; preserve exact output, no clock override')
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
        env = env_candidate(ENV.read_bytes(), self.release['line_enabled'])
        orb_env = Path('/etc/project-mai-tai/orb-paper.env')
        orb_backup = self.attempt / 'orb-paper.env.before'
        if orb_env.exists():
            orb_backup.write_bytes(orb_env.read_bytes())
            orb_backup.chmod(0o600)
            self.note(bootstrap_derived_env=str(orb_env), backup=str(orb_backup),
                      before_sha256=digest(orb_backup.read_bytes()))
        blob = self.checked(['sudo', '-u', 'trader', 'git', '-C', str(REPO), 'show',
                             self.release['approved_sha'] + ':ops/health/expected_flags.json'])
        catalog = catalog_candidate(CATALOG.read_bytes(), json.loads(blob.read_bytes()), self.release['line_enabled'])
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
        self.stage = 'env-and-catalog'
        self.backup_write(ENV, env)
        self.backup_write(CATALOG, catalog)
        need(self.gate('before-migration0023') == 0, 'fresh trading gate blocked before migration')
        self.stage = 'migration0023'
        self.checked([str(PYTHON), str(self.job / 'migration0023.py'),
                      '--job', str(self.job), '--attempt', str(self.attempt)], timeout=600)
        for target in ('oms', 'schwab-1m-v2', 'control'):
            need(self.gate('before-deploy-' + target) == 0, 'fresh trading gate blocked before ' + target)
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
        # Parent reports retirement completed manually. Preserve its actual
        # receipt; this P1-only installer must not publish another replace.
        self.stage = 'completed-retirement-evidence'
        retirement = (self.job / 'completed-retirement.json').read_bytes()
        from retire_orb import validate_receipt
        validate_receipt(json.loads(retirement))
        save(self.attempt / 'orb-retirement.json', json.loads(retirement))
        self.stage = 'post-install'
        after = self.identities()
        for name, row in after.items():
            need(row['ActiveState'] == 'active' and row['SubState'] == 'running'
                 and row['MainPID'] != identities[name]['MainPID'] and int(row['MainPID']) > 0
                 and row['NRestarts'] == '0', 'failed start or wrong identity: ' + name)
        save(self.attempt / 'service-identities.json', after)
        if orb_backup.exists():
            self.note(bootstrap_derived_env=str(orb_env), after_sha256=digest(orb_env.read_bytes()),
                      no_orb_restart=True)
        need(self.gate('post-install-trading-read') == 0, 'post-install trading gate blocked')
        self.stage = 'ten-minute-observation'
        self.note(observation_seconds=600, restart_identities=after)
        time.sleep(600)
        self.checked([str(PYTHON), str(self.job / 'proof_readonly.py'), 'after',
                      '--baseline', str(self.attempt / 'proof-before.json'),
                      '--approved-sha', self.release['approved_sha'],
                      '--line-enabled', str(self.release['line_enabled']).lower(),
                      '--retirement', str(self.attempt / 'orb-retirement.json'),
                      '--output', str(self.attempt / 'post-install-proof.json')])
        self.stage = 'flag-audit'
        rc, flags, errors = self.command([str(PYTHON), str(REPO / 'ops/health/expected_flags_check.py'),
            '--catalog', str(CATALOG), '--numeric-catalog', '/home/trader/restart_evidence/expected_numeric.json'])
        save(self.attempt / 'flaggate.json', dict(rc=rc, stdout=str(flags), stderr=str(errors),
                                               lines=flags.read_text().splitlines()))
        # Inactive momentum-paper UNKNOWN remains honest; retired ORB rows are removed.
        failures = [line for line in flags.read_text().splitlines() if line.startswith('REAL FAILURE')]
        need(not failures, 'unexpected process-flag mismatch')
        unknown = [line for line in flags.read_text().splitlines() if line.startswith('UNKNOWN')]
        need(all('service=momentum-paper ' in line for line in unknown), 'unreadable non-paper process flags')
        self.stage = 'preopen-bookkeeping'
        snapshot = json.loads((self.attempt / 'before-restart.json').read_bytes())
        save(self.attempt / 'sealed-actions.json', dict(source_journal=str(self.attempt / 'runner.log'),
             source_journal_sha256=digest((self.attempt / 'runner.log').read_bytes()), actions=self.events))
        record = dict(schema_version=1, snapshot_captured_at_utc=snapshot['captured_at_utc'],
                      source_journal=str(self.attempt / 'sealed-actions.json'),
                      service_actions={name: 'restarted' if name in UNITS else 'deliberately_untouched'
                                       for name in snapshot['services']})
        save(self.attempt / 'install-record.json', record)
        self.checked([str(PYTHON), str(self.job / 'repin_preopen.py'),
                      '--approved-sha', self.release['approved_sha'],
                      '--snapshot', str(self.attempt / 'before-restart.json'),
                      '--install-record', str(self.attempt / 'install-record.json'),
                      '--line-enabled', str(self.release['line_enabled']).lower(),
                      '--retirement', str(self.attempt / 'orb-retirement.json'),
                      '--receipt', str(self.attempt / 'preopen-repin.json')])
        self.checked(['bash', '-n', '/home/trader/preopen.sh'])
        proof = json.loads((self.attempt / 'post-install-proof.json').read_bytes())
        self.stage = 'COMPLETE'
        self.note(approved_sha=self.release['approved_sha'], plan_commit=self.release['plan_commit'],
                  release_sha256=digest((self.job / 'release.json').read_bytes()),
                  flaggate_rc=rc, known_orb_mismatches=failures, paper_unknown=unknown,
                  post_install_assessment=proof['assessment'],
                  morning_scanner_acceptance='UNMEASURED')
        save(self.job / 'COMPLETE.json', dict(attempt=str(self.attempt), approved_sha=self.release['approved_sha'],
             at_utc=datetime.now(timezone.utc).isoformat(), runner_log_sha256=digest((self.attempt / 'runner.log').read_bytes())))


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
