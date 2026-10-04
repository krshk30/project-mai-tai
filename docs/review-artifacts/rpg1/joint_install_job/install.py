"""Literal single-attempt joint install. Importing this file performs no actions."""
from __future__ import annotations

from datetime import timedelta
import difflib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import signal
import stat
import subprocess
import sys
import time
from types import SimpleNamespace

from policy import *
from proofs import (RedisRead, archive_intents, command, identity, redis_state)
from review_gate import verify

PY = REPO / '.venv/bin/python'
CHECKERS = Path('/home/trader/restart_evidence')


def save(name, value):
    need(re.fullmatch(r'[A-Za-z0-9_.-]+', name), 'unsafe evidence name')
    with (RUN / name).open('xb') as output:
        output.write((json.dumps(value, indent=2, sort_keys=True) + '\n').encode())
        output.flush()
        os.fsync(output.fileno())


def append_event(kind, **details):
    with (RUN / 'events.jsonl').open('a') as output:
        output.write(json.dumps({'at': now().isoformat(), 'event': kind, **details}, sort_keys=True) + '\n')
        output.flush()
        os.fsync(output.fileno())


def atomic(path, data):
    need(not path.is_symlink() and path.is_file(), 'atomic target not regular')
    info = path.stat()
    candidate = path.with_name(path.name + '.rpg1-coldstart1.pending')
    with candidate.open('xb') as output:
        output.write(data)
        output.flush()
        os.fsync(output.fileno())
    os.chown(candidate, info.st_uid, info.st_gid)
    os.chmod(candidate, stat.S_IMODE(info.st_mode))
    os.replace(candidate, path)
    descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def env_update(original):
    lines = original.splitlines(keepends=True)
    changes = {}
    for key, wanted in FLAGS.items():
        matches = [(index, re.match(r'^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=(.*?)(?:\r?\n)?$', line))
                   for index, line in enumerate(lines)]
        matches = [(i, m) for i, m in matches if m and m[1].upper() == key]
        need(len(matches) <= 1, 'duplicate/case-conflicting env flag: ' + key)
        if matches:
            index, match = matches[0]
            need(match[1] == key, 'case-conflicting env flag: ' + key)
            changes[key] = {'before': match[2].strip(), 'after': wanted}
            lines[index] = key + '=' + wanted + '\n'
        else:
            if lines and not lines[-1].endswith('\n'):
                lines[-1] += '\n'
            lines.append(key + '=' + wanted + '\n')
            changes[key] = {'before': '<absent>', 'after': wanted}
    return ''.join(lines), changes


def log_mark(name):
    path = Path('/var/log/project-mai-tai') / (name + '.log')
    need(not path.is_symlink() and path.is_file(), 'service log unavailable: ' + name)
    info = path.stat()
    return {'path': str(path), 'device': info.st_dev, 'inode': info.st_ino, 'offset': info.st_size}


def log_tail(mark):
    path = Path(mark['path'])
    info = path.stat()
    need((info.st_dev, info.st_ino) == (mark['device'], mark['inode']), 'log rotated: ' + str(path))
    length = info.st_size - mark['offset']
    need(0 <= length <= 20_000_000, 'log truncated/oversized: ' + str(path))
    with path.open('rb') as stream:
        stream.seek(mark['offset'])
        content = stream.read(length)
    after = path.stat()
    need((after.st_dev, after.st_ino) == (info.st_dev, info.st_ino) and after.st_size >= info.st_size,
         'log raced: ' + str(path))
    return content.decode(errors='replace')


def narrow_cancelled_stop(before, after, tail, journal, *, stop_issued):
    need(stop_issued and before['KillSignal'] in {'15', 'SIGTERM'}, 'no reviewed SIGTERM stop')
    need(after['MainPID'] == '0' and after['ActiveState'] == 'failed'
         and after['ExecMainCode'] == '1' and after['ExecMainStatus'] == '1'
         and after['Result'] == 'exit-code' and after['NRestarts'] == '0', 'not narrow exit1/PID0 stop')
    need(not re.search(r'\b(?:ERROR|CRITICAL|FATAL)\b|(?:^|\n)[A-Za-z_.]*(?:Error|Exception):', tail),
         'unrelated shutdown error; no reset')
    frames = tuple((path.replace('\\', '/').rsplit('/', 1)[-1], function.strip()) for path, function in
                   re.findall(r'File "([^"]+)", line \d+, in ([^\n]+)', tail))
    need(frames in KNOWN_CANCELLED_STACKS, 'shutdown stack is not the recorded row-47 stack')
    records = [json.loads(line) for line in journal.splitlines() if line.strip()]
    messages = []
    for record in records:
        message = record.get('MESSAGE', '')
        if 'Stopping ' in message or 'status=1/FAILURE' in message:
            invocation = record.get('INVOCATION_ID', record.get('OBJECT_SYSTEMD_INVOCATION_ID',
                                    record.get('_SYSTEMD_INVOCATION_ID')))
            need(invocation == before['InvocationID'], 'journal is not bound to old invocation')
            need(record.get('UNIT', record.get('_SYSTEMD_UNIT', record.get('OBJECT_SYSTEMD_UNIT'))) == unit('orb-schwab'),
                 'journal unit mismatch')
            messages.append(message)
    text = '\n'.join(messages)
    need(tail.count('Traceback (most recent call last):') == 1
         and tail.rstrip().endswith('asyncio.exceptions.CancelledError')
         and 'mai-tai-orb-schwab' in tail and 'SIGKILL' not in journal
         and 'Stopping ' in text and 'status=1/FAILURE' in text,
         'same-stop CancelledError/SIGTERM evidence missing')
    need(before['InvocationID'] == after['InvocationID'], 'stop invocation changed')


def control_refresh_after(text, began, expiry):
    for line in text.splitlines():
        match = re.match(r'^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3}).*'
                         r'\[SCHWAB-TOKEN-REFRESHED\] access_token refreshed; expires_at=(\S+)', line)
        if match:
            observed = datetime.strptime(match[1], '%Y-%m-%d %H:%M:%S,%f').replace(tzinfo=UTC)
            if began <= observed <= now() and stamp(match[2]) == expiry:
                return True
    return False


def parse_gate(output, total):
    lines = [line for line in output.splitlines() if line.startswith('Final call:')]
    need(len(lines) == 1 and re.fullmatch(
        rf'Final call: PASS; checked={total}/{total} mismatches=0 unknown=0', lines[0]),
        'FLAGGATE denominator/result: ' + repr(lines))


def replace_assignments(text, values):
    for key, value in values.items():
        text, count = re.subn(r'^' + re.escape(key) + r'=.*$',
                             lambda _: key + '=' + shlex.quote(str(value)), text, flags=re.M)
        need(count == 1, 're-pin key absent/duplicated: ' + key)
    return text


def report_options(preopen):
    """Preserve literal reviewed flags/quiet/schema options without executing Bash."""
    assignments = {}
    for key, raw in re.findall(r'^([A-Z_][A-Z_0-9]*)=(.*)$', preopen, re.M):
        tokens = shlex.split(raw, comments=True)
        if len(tokens) == 1 and '$' not in tokens[0] and '`' not in tokens[0]:
            assignments[key] = tokens[0]
    args = []
    pattern = r'--(expect-flag|expected-quiet-service|schema-column|schema-constraint)\s+(\S+|"[^"]*"|\x27[^\x27]*\x27)'
    lines = '\n'.join(line for line in preopen.splitlines() if not line.lstrip().startswith('#'))
    for option, raw in re.findall(pattern, lines):
        tokens = shlex.split(raw)
        need(len(tokens) == 1, 'ambiguous preopen report argument')
        def expand(match):
            key = match[1] or match[2]
            need(key in assignments, 'unresolved preopen report variable: ' + key)
            return assignments[key]
        value = re.sub(r'\$([A-Z_][A-Z_0-9]*)|\$\{([A-Z_][A-Z_0-9]*)\}', expand, tokens[0])
        need('$' not in value and '`' not in value and '\n' not in value, 'dynamic preopen report argument')
        args.extend(('--' + option, value))
    return args


def repin_texts(preopen, monday, rows, application, snapshot, record):
    values = {'EXPECTED_SHA': application, 'EXPECTED_DATE': '2026-10-05',
              'SNAPSHOT': str(snapshot), 'INSTALL_RECORD': str(record)}
    for name, prefix in (('schwab-1m-v2', 'EXPECTED'), ('oms', 'EXPECTED_OMS'),
                         ('strategy', 'EXPECTED_STRATEGY'), ('orb', 'EXPECTED_ORB'),
                         ('orb-schwab', 'EXPECTED_ORB_SCHWAB'), ('market-data', 'EXPECTED_MARKET_DATA')):
        values[prefix + '_PID'] = rows[name]['MainPID']
        values[prefix + '_START'] = rows[name]['ExecMainStartTimestamp']
    preopen = replace_assignments(preopen, values)
    pattern = r'^  --(?:restarted|new-service) [^\n]+\\\n'
    previous = re.findall(pattern, preopen, re.M)
    need(previous, 'preopen classification arguments absent')
    replacement = ''.join('  --restarted ' + name + ' \\\n' for name in sorted(set(RESTARTED) - {'momentum-paper'}))
    first = True
    def change(_):
        nonlocal first
        value = replacement if first else ''
        first = False
        return value
    preopen = re.sub(pattern, change, preopen, flags=re.M)
    monday = replace_assignments(monday, {'APPROVED_SHA': application,
        'PAPER_PID': rows['momentum-paper']['MainPID'],
        'PAPER_START': rows['momentum-paper']['ExecMainStartTimestamp']})
    for pattern, new in (
        (r"value\['MainPID'\] == '[0-9]+'", "value['MainPID'] == '" + rows['market-data']['MainPID'] + "'"),
        (r"value\['ExecMainStartTimestamp'\] == '[^']+'", "value['ExecMainStartTimestamp'] == '" + rows['market-data']['ExecMainStartTimestamp'] + "'"),
        (r"Path\('/proc/[0-9]+/environ'\)", "Path('/proc/" + rows['market-data']['MainPID'] + "/environ')")):
        monday, count = re.subn(pattern, lambda _: new, monday)
        need(count == 1, 'Monday gateway pin pattern changed')
    return preopen, monday


class Coordinator:
    """Small injectable state machine: tests exercise the real ordered entry point."""
    def __init__(self, host):
        self.host = host

    def run(self):
        h = self.host
        h.initial()
        h.prepare()
        for name in STOP:
            h.before('stop', name)
            h.service('stop', name)
            h.stopped(name)
        h.drained()
        h.before('restart', 'oms')
        h.service('restart', 'oms')
        h.started('oms')
        for name in START:
            h.before('start', name)
            h.service('start', name)
            h.started(name)
        h.finalize()


class Host:
    def __init__(self):
        self.release = verify(prepare=True)
        self.host = self.release['host']
        self.expected = dict(self.host['services'])
        self.logs, self.stop_before, self.start_cursors = {}, {}, {}
        self.stop_times, self.new, self.announcements = {}, {}, {}
        self.latest_announcements = {}
        self.counter, self.events_read, self.peak = 0, 0, 0
        self.quiesced_at = None
        self.finished = False
        self.stage = 'claimed'
        self.context = self.proof('context')
        self.reader = RedisRead(SimpleNamespace(**self.context))

    def record(self, label, value):
        self.counter += 1
        save(f'{self.counter:04d}-{label}.json', value)
        return value

    def proof(self, operation, **request):
        # Every invocation imports the on-disk source fresh, even across checkout.
        result = subprocess.run([str(PY), '-s', str(ROOT / 'proofs.py'), operation], input=json.dumps(request),
                                text=True, capture_output=True, timeout=150,
                                env={**os.environ, 'PYTHONDONTWRITEBYTECODE': '1', 'PYTHONPATH': str(REPO / 'src')})
        need(len(result.stdout) + len(result.stderr) < 8_000_000, 'proof output oversized')
        if result.returncode:
            self.record('proof-refusal', {'operation': operation, 'rc': result.returncode,
                                         'stderr': result.stderr[-10000:]})
            raise Refusal('read-only proof failed: ' + operation)
        return json.loads(result.stdout)

    def gate(self, stage, *, prepare=False):
        current = verify(prepare=prepare)
        need(current == self.release, 'approved release changed during attempt')
        self.stage = stage
        append_event('stage', stage=stage)

    def git(self, *args):
        return command('sudo', '-u', 'trader', 'git', '-C', str(REPO), *args, timeout=60)

    def blob(self, commit, path):
        result = subprocess.run(['sudo', '-u', 'trader', 'git', '-C', str(REPO), 'show', commit + ':' + path],
                                capture_output=True, timeout=30)
        need(result.returncode == 0 and len(result.stdout) < 8_000_000, 'Git blob unreadable: ' + path)
        return result.stdout

    def census(self):
        rows = {name: identity(name) for name in SERVICES}
        for name, wanted in self.expected.items():
            need(rows[name] == wanted, 'unexpected service identity/state: ' + name)
        need(identity('tv-alerts') == self.host['tv_alerts'], 'tv-alerts changed')
        return rows

    def file_pin(self, path):
        wanted = self.host['files'][str(path)]
        need(not path.is_symlink() and path.is_file(), 'pinned file not regular')
        info = path.stat()
        need({'sha256': digest(path), 'uid': info.st_uid, 'gid': info.st_gid,
              'mode': stat.S_IMODE(info.st_mode)} == wanted, 'host file drift: ' + str(path))

    def safety(self):
        current = redis_state(self.reader, evicted=self.redis_before['evicted_keys'])
        for name, kind in self.redis_before['streams'].items():
            need(kind == 'none' or current['streams'][name] == kind, 'preexisting stream disappeared: ' + name)
        self.peak = max(self.peak, current['used_memory'])
        tail = self.reader.rows('strategy-intents', reverse=True)
        wanted_tail = self.host['intent_ids'][-1:]
        need([entry[0] for entry in tail] == wanted_tail
             and self.reader.call('XLEN', self.reader.prefix + ':strategy-intents') == len(self.host['intent_ids']),
             'new/trimmed intent after reviewed idle archive')
        self.census()
        return current

    def flat(self, label):
        self.census()
        value = self.proof('flat', accepted=self.host['accepted_unexplained_orders'])
        if self.quiesced_at and (now() - self.quiesced_at).total_seconds() > 25 * 60:
            expiry = stamp(value['token']['expires_at'])
            need(expiry > now() + timedelta(minutes=25), 'quiescence >25m; normal control refresh not yet sufficient')
            control = log_tail(self.control_log)
            need(control_refresh_after(control, self.quiesced_at, expiry),
                 'no successful matching control-file refresh since quiescence')
            self.record('control-refresh', {'expires_at': value['token']['expires_at'],
                                          'identity': identity('control'), 'proof': 'matching successful control refresh log'})
        self.census()
        self.last_flat_at = stamp(value['at'])
        return self.record(label + '-flat', value)

    def v2_gate(self):
        raw = command('bash', str(REPO / 'ops/preflight/preflight_v2_restart.sh'), timeout=90)
        need('OVERRIDE' not in raw and 'overridden' not in raw.lower(), 'v2 gate override forbidden tonight')
        self.record('v2-preflight', {'rc': 0, 'raw': raw, 'clock_override': False, 'arm_override': False})

    def release_source(self):
        app = self.release['application']
        need(self.git('rev-parse', app['approved_sha'] + '^{tree}') == app['tree'], 'approved tree mismatch')
        need(self.git('rev-list', '--reverse', app['box_sha'] + '..' + app['approved_sha']).splitlines() == app['commits'], 'reviewed commit range mismatch')
        need(sorted(self.git('diff', '--name-only', app['box_sha'], app['approved_sha']).splitlines()) == app['changed_paths'], 'reviewed path diff mismatch')
        self.gate('fetch-reviewed-main', prepare=True)
        self.git('fetch', '--no-tags', 'origin', 'main')
        fetched = self.git('rev-parse', 'FETCH_HEAD')
        remote = self.git('ls-remote', 'origin', 'refs/heads/main').split()
        need(len(remote) == 2 and remote[1] == 'refs/heads/main', 'remote main unreadable')
        need(remote[0] == fetched, 'main moved during fetch; no moving-target retry')
        self.git('merge-base', '--is-ancestor', app['approved_sha'], remote[0])
        need(all(path.startswith('docs/') for path in self.git('diff', '--name-only', app['approved_sha'], remote[0]).splitlines()),
             'main ahead by unreviewed application changes')
        plan = self.release['plan']
        need(hashlib.sha256(self.blob(plan['commit'], plan['path'])).hexdigest() == plan['sha256'], 'plan Git hash mismatch')
        for review in self.release['reviews']:
            need(hashlib.sha256(self.blob(review['record_commit'], review['pin_path'])).hexdigest() == review['pin_sha256'], 'review ledger blob changed')
        paths = self.git('ls-tree', '-r', '--name-only', app['approved_sha'], '--', 'src').splitlines()
        need(set(paths) == set(app['source_sha256']), 'source manifest omits/adds files')
        for path, sha in app['source_sha256'].items():
            need(hashlib.sha256(self.blob(app['approved_sha'], path)).hexdigest() == sha, 'source Git hash mismatch: ' + path)

    def initial(self):
        self.gate('preflight', prepare=True)
        self.census()
        need(self.git('rev-parse', 'HEAD') == self.release['application']['box_sha']
             and not self.git('status', '--porcelain'), 'box not clean exact BOX_SHA')
        jobs = command('systemctl', 'list-jobs', '--no-pager', '--no-legend')
        need(not any(unit(name) in jobs for name in SERVICES), 'competing service job')
        ps = command('ps', '-eo', 'pid,args')
        need(not any(re.search(r'(?:^|/)(?:deploy_main|deploy_service)\.sh(?:\s|$)', line)
                     or 'option_a_treatment_guard.py' in line or 'option_a_treatment_1008_sampler.py' in line
                     for line in ps.splitlines()), 'competing deployment/expired guard')
        for name in self.host['files']:
            self.file_pin(Path(name))
        for name, expected in self.host['units'].items():
            need(hashlib.sha256(command('systemctl', 'cat', name).encode()).hexdigest() == expected, 'unit drift: ' + name)
        self.redis_before = redis_state(self.reader)
        self.peak = self.redis_before['used_memory']
        self.record('redis-before', self.redis_before)
        self.flat('preflight')
        self.release_source()
        self.v2_gate()
        self.inputs = self.proof('inputs')
        need(self.inputs == self.host['subscription_inputs'], 'reviewed restored/current inputs changed')
        self.record('input-proofs', self.inputs)
        intents = archive_intents(self.reader)
        need([row[0] for row in intents] == self.host['intent_ids'], 'retained intent inventory changed')
        self.record('retained-intents-before', {'entries': intents, 'disposition': 'archive, not an OMS ACK; no replay'})
        self.control_log = log_mark('control')
        self.wait_gateway(after=now() - timedelta(seconds=30), timeout=30)
        # Exact snapshot-relative census used by the existing independent checker.
        command(str(PY), str(CHECKERS / 'v2_restart_evidence.py'), 'snapshot', '--output', str(RUN / 'before-restart.json'), timeout=90)
        backups = {}
        for index, name in enumerate(sorted(self.host['files'])):
            path = Path(name)
            self.file_pin(path)
            destination = RUN / f'backup-{index:02d}'
            with destination.open('xb') as output:
                output.write(path.read_bytes())
                output.flush()
                os.fsync(output.fileno())
            backups[name] = {'backup': str(destination), **self.host['files'][name]}
        self.record('backups', backups)

    def prepare(self):
        self.flat('before-env')
        self.gate('env', prepare=True)
        self.file_pin(ENV)
        updated, changes = env_update(ENV.read_text())
        atomic(ENV, updated.encode())
        self.record('env-change', {'keys': changes, 'sha256': digest(ENV), 'other_keys': 'byte-preserved'})
        self.flat('before-checkout')
        self.gate('checkout', prepare=True)
        app = self.release['application']['approved_sha']
        need(self.git('rev-parse', 'HEAD') == self.release['application']['box_sha']
             and not self.git('status', '--porcelain'), 'checkout drift before mutation')
        self.git('switch', '--detach', app)
        self.flat('before-editable-metadata')
        self.gate('editable-metadata', prepare=True)
        command('sudo', '-u', 'trader', str(PY), '-m', 'pip', 'install', '--no-deps',
                '--disable-pip-version-check', '-e', str(REPO), timeout=120)
        need(self.git('rev-parse', 'HEAD') == app and not self.git('status', '--porcelain'), 'installed checkout not clean pinned SHA')
        for path, expected in self.release['application']['source_sha256'].items():
            need(digest(REPO / path) == expected, 'installed source hash mismatch: ' + path)
        imported = command('sudo', '-u', 'trader', str(PY), '-c',
            'import pathlib,project_mai_tai;print(pathlib.Path(project_mai_tai.__file__).resolve())')
        need(imported == str(REPO / 'src/project_mai_tai/__init__.py'), 'editable import path wrong')
        self.record('checkout', {'sha': app, 'import': imported, 'source_files_verified': len(self.release['application']['source_sha256'])})
        self.context = self.proof('context')
        self.reader = RedisRead(SimpleNamespace(**self.context))
        self.logs['oms'] = log_mark('oms')

    def before(self, action, name):
        check_service_action(action, name)
        if action == 'start':
            need(self.proof('inputs') == self.inputs, 'restored/current inputs changed before start')
        self.flat('before-' + action + '-' + name)
        if action == 'stop' and name == 'schwab-1m-v2':
            self.v2_gate()
        self.safety()
        self.gate(action + '-' + name)
        if action == 'stop':
            self.stop_before[name] = {'identity': identity(name), 'log': log_mark(name)}
            self.stop_times[name] = now()
            if self.quiesced_at is None:
                self.quiesced_at = now()
                self.control_log = log_mark('control')
        if action == 'start':
            need(identity(name)['MainPID'] == '0', 'start target not stopped')
            tail = self.reader.rows('market-data-subscriptions', reverse=True)
            need(len(tail) == 1, 'subscription post-stop cursor absent')
            self.start_cursors[name] = tail[0][0]
            self.cursor = tail[0][0]
            self.expected_sets = self.safety()['sets']
            self.record('post-stop-cursor-' + name, {'cursor': self.cursor, 'at': now().isoformat(),
                                                    'stopped_identity': identity(name), 'input': self.inputs[CONSUMERS[name]]})

    def service(self, action, name):
        check_service_action(action, name)
        self.gate(action + '-' + name)
        need(0 <= (now() - self.last_flat_at).total_seconds() <= 30, 'flat proof too old for service action')
        append_event('service-action', action=action, service=name, stage=self.stage)
        # No shell, wildcard, multi-unit action, rescue path, or second start.
        result = subprocess.run(['systemctl', action, unit(name)], capture_output=True, text=True, timeout=90)
        self.record('service-' + action + '-' + name, {'rc': result.returncode, 'stdout': result.stdout,
                                                       'stderr': result.stderr, 'state': identity(name)})
        if result.returncode != 0:
            need((action, name) == ('stop', 'orb-schwab'), 'systemctl action failed: ' + name)
            self.narrow_stop_evidence()

    def narrow_stop_evidence(self):
        prior = self.stop_before['orb-schwab']
        row = identity('orb-schwab')
        tail = log_tail(prior['log'])
        journal = command('journalctl', '-u', unit('orb-schwab'), '--since', self.stop_times['orb-schwab'].isoformat(),
                          '--no-pager', '-o', 'json', '-n', '200')
        narrow_cancelled_stop(prior['identity'], row, tail, journal, stop_issued=True)
        return {'before': prior['identity'], 'after': row, 'shutdown_log': tail, 'journal': journal,
                'signal': 'configured KillSignal=SIGTERM; this attempt stop'}

    def stopped(self, name):
        row = identity(name)
        if name == 'orb-schwab' and row['ActiveState'] == 'failed':
            self.record('orb-schwab-reviewed-stop', self.narrow_stop_evidence())
            self.expected[name] = row
            self.flat('before-narrow-reset')
            self.gate('orb-schwab-narrow-reset')
            self.service('reset-failed', name)
            row = identity(name)
        need(row['MainPID'] == '0' and row['ActiveState'] == 'inactive' and row['SubState'] == 'dead'
             and row['NRestarts'] == '0', 'stop not clean/PID0: ' + name)
        self.expected[name] = row
        self.logs[name] = log_mark(name)
        self.record('stopped-' + name, {'identity': row, 'post_exit_log_mark': self.logs[name]})
        self.census()

    def drained(self):
        self.gate('all-producers-quiesced')
        need(all(identity(name)['MainPID'] == '0' for name in STOP), 'producer still running')
        prior = archive_intents(self.reader)
        need([row[0] for row in prior] == self.host['intent_ids'], 'new intent before quiescence')
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            self.safety()
            time.sleep(1)
        current = archive_intents(self.reader)
        need(current == prior, 'intent arrived after quiescence')
        self.record('idle-disposition', {'entries': current, 'quiet_seconds': 20,
                    'scope': 'approved Saturday-idle disposition; NOT proof of OMS ACK/drain'})
        need(self.proof('inputs') == self.inputs, 'idle inputs changed')
        self.flat('quiesced')

    def heartbeat(self, source, after):
        for eid, event in self.reader.rows('heartbeats', reverse=True, count=25):
            if event.get('source_service') == source:
                when = stamp(event['produced_at'])
                if when >= after and 0 <= (now() - when).total_seconds() < 30 and event['payload']['status'] == 'healthy':
                    return {'id': eid, 'event': event}
                return None
        return None

    def wait_gateway(self, *, after, timeout=30):
        until = time.monotonic() + timeout
        while time.monotonic() <= until:
            current = self.safety()
            union = set(self.context['static_symbols']).union(*(set(v) for v in current['sets'].values()))
            hb = self.heartbeat('market-data-gateway', after)
            if hb and int(hb['event']['payload']['details']['active_symbols']) == len(union):
                return self.record('gateway-health', {'heartbeat': hb, 'union': sorted(union), 'identity': identity('market-data')})
            window(now())
            time.sleep(1)
        raise Refusal('gateway healthy/current union heartbeat unproven')

    def consume_replaces(self, name, row):
        # Cursor was read after this old consumer reached PID0, immediately before start.
        key = self.reader.prefix + ':market-data-subscriptions'
        need(self.reader.call('XRANGE', key, self.cursor, self.cursor, 'COUNT', 1), 'subscription cursor trimmed')
        for _ in range(1001 - self.events_read):
            rows = self.reader.rows('market-data-subscriptions', cursor='(' + self.cursor)
            if not rows:
                break
            self.events_read += 1
            need(self.events_read <= 1000, 'subscription event budget exceeded')
            eid, event = rows[0]
            payload = event.get('payload', {})
            consumer = payload.get('consumer_name')
            need(consumer in OWNERS and payload.get('mode') == 'replace'
                 and event.get('source_service') == consumer, 'unknown owner/non-replace/source')
            value = symbols(payload['symbols'])
            need(value == self.inputs[consumer]['symbols'], 'startup set differs from independent input')
            before_sets = dict(self.expected_sets)
            self.expected_sets[consumer] = value
            self.cursor = eid
            need(stamp(event['produced_at']) <= now(), 'future subscription timestamp')
            self.latest_announcements[consumer] = {'id': eid, 'event': event}
            self.record('subscription-event', {'id': eid, 'event': event, 'other_four_before': before_sets})
            if consumer == CONSUMERS[name]:
                delta = (stamp(event['produced_at']) - service_start(row)).total_seconds()
                need(0 <= delta <= 60 and stream_id(eid) > stream_id(self.start_cursors[name]), 'startup announcement stale/late')
                self.announcements[name] = {'id': eid, 'event': event, 'start_to_announce_seconds_upper_bound': delta,
                                             'systemd_start_resolution_seconds': 1, 'post_stop_cursor': self.start_cursors[name]}
        else:
            raise Refusal('subscription budget exhausted, including unseen backlog')
        state = self.safety()
        if stream_id(state['checkpoint']) < stream_id(self.cursor):
            return False
        need(state['checkpoint'] == self.cursor and state['sets'] == self.expected_sets,
             'owner application race/other consumer set drift')
        return name in self.announcements

    def started(self, name):
        row = identity(name)
        before = self.host['services'][name]
        need(active(row) and row['MainPID'] != before['MainPID']
             and row['ExecMainStartTimestamp'] != before['ExecMainStartTimestamp']
             and row['InvocationID'] != before['InvocationID'], 'new healthy process identity unproven: ' + name)
        self.expected[name], self.new[name] = row, row
        deadline = time.monotonic() + 180
        source = {'oms': 'oms-risk', **CONSUMERS}[name]
        special = name in {'orb-schwab', 'momentum-paper'}
        while time.monotonic() <= deadline:
            window(now())
            self.safety()
            log = log_tail(self.logs[name])
            need('Traceback (most recent call last):' not in log, 'new-process traceback: ' + name)
            if name == 'strategy':
                need(not re.search(r'\bERROR\b', log), 'new strategy ERROR')
            replacement = name == 'oms' or self.consume_replaces(name, row)
            if name != 'oms' and name not in self.announcements:
                need((now() - service_start(row)).total_seconds() <= 60, 'no startup replace within actual-start 60s')
            hb = None if special else self.heartbeat(source, service_start(row))
            proof = bool(log.strip()) and (special or hb is not None) and replacement
            if name == 'orb-schwab':
                proof = proof and '[ORB-SCHWAB] mode=LIVE' in log
            if proof:
                self.record('started-' + name, {'identity': row, 'heartbeat': hb,
                    'heartbeat_disposition': 'not emitted in this phase by service code' if special else 'post-start healthy',
                    'startup_replace': self.announcements.get(name), 'post_exit_log': log,
                    'paper': 'read-only _tick disconnected; actual empty replace, no treatment launch' if name == 'momentum-paper' else None})
                return
            time.sleep(1)
        raise Refusal('new process health unproven within180s: ' + name)

    def install_catalogs(self):
        self.flat('before-catalogs')
        for name in CATALOGS:
            self.gate('catalog-' + name)
            path = CHECKERS / name
            self.file_pin(path)
            raw = self.blob(self.release['application']['approved_sha'], 'ops/health/' + name)
            atomic(path, raw)
            self.record('catalog-' + name, {'sha256': digest(path), 'git_blob_sha256': hashlib.sha256(raw).hexdigest()})
        full = command(str(PY), str(CHECKERS / 'expected_flags_check.py'), '--catalog', str(CHECKERS / 'expected_flags.json'),
                       '--numeric-catalog', str(CHECKERS / 'expected_numeric.json'), timeout=180)
        parse_gate(full, COUNTS['combined'])
        numeric = command(str(PY), '-c',
            "import runpy,pathlib; n=runpy.run_path('/home/trader/restart_evidence/expected_flags_check.py'); "
            "rc,lines=n['audit'](n['load_numeric_catalog'](pathlib.Path('/home/trader/restart_evidence/expected_numeric.json'))); "
            "print('\\n'.join(lines));raise SystemExit(rc)", timeout=180)
        parse_gate(numeric, COUNTS['numeric'])
        self.record('flag-gates', {'full_rc': 0, 'full': full, 'numeric_rc': 0, 'numeric': numeric})

    def repin(self):
        rows = self.census()
        snapshot = json.loads((RUN / 'before-restart.json').read_text())
        known = set(snapshot['services'])
        expected_names = {'control', 'market-capture', 'market-data', 'oms', 'orb', 'orb-schwab',
                          'reconciler', 'schwab-1m-v2', 'strategy', 'tv-alerts'}
        need(known == expected_names, 'restart checker monitored service inventory changed')
        actions = {name: 'restarted' if name in RESTARTED else 'deliberately_untouched' for name in known}
        save('install-record.json', {'schema_version': 1, 'snapshot_captured_at_utc': snapshot['captured_at_utc'],
                                   'source_journal': str(JOURNAL), 'service_actions': actions})
        self.file_pin(PREOPEN)
        self.file_pin(MONDAY)
        originals = (PREOPEN.read_text(), MONDAY.read_text())
        candidates = repin_texts(*originals, rows, self.release['application']['approved_sha'],
                                 RUN / 'before-restart.json', RUN / 'install-record.json')
        for label, content in zip(('preopen', 'monday'), candidates):
            path = RUN / (label + '.candidate')
            with path.open('x') as output:
                output.write(content)
            command('bash', '-n', str(path))
        self.restart_report(snapshot, originals[0])
        self.flat('before-repins')
        for label, path, old, content in zip(('preopen', 'monday'), (PREOPEN, MONDAY), originals, candidates):
            self.gate('repin-' + label)
            self.file_pin(path)
            atomic(path, content.encode())
            self.record('repin-' + label, {'sha256': digest(path), 'diff': ''.join(difflib.unified_diff(
                old.splitlines(keepends=True), content.splitlines(keepends=True), fromfile='before', tofile='after'))})
        for name in (MONDAY_TIMER, MONDAY_TIMER.replace('.timer', '.service')):
            need(hashlib.sha256(command('systemctl', 'cat', name).encode()).hexdigest() == self.host['units'][name], 'Monday unit/timer drift')
        timers = command('systemctl', 'list-timers', '--all', '--no-pager')
        need(sum(MONDAY_TIMER in line for line in timers.splitlines()) == 1, 'Monday timer not unique')
        next_time = command('systemctl', 'show', MONDAY_TIMER, '-p', 'NextElapseUSecRealtime', '--value')
        need('2026-10-05 07:40:00' in next_time, 'Monday guard NEXT not03:40ET')
        self.record('monday-timer', {'listing': timers, 'next': next_time, 'action': 'read-only; not enabled/restarted'})

    def restart_report(self, snapshot, preopen):
        flags = [('schwab-1m-v2', key, value) for key, value in FLAGS.items()]
        args = [str(PY), str(CHECKERS / 'v2_restart_evidence.py'), 'report', '--snapshot', str(RUN / 'before-restart.json'),
                '--install-record', str(RUN / 'install-record.json'), '--expected-alembic-head', str(snapshot['alembic_version']), '--no-schema-change']
        args.extend(report_options(preopen))
        for name in sorted(set(RESTARTED) - {'momentum-paper'}):
            args.extend(('--restarted', name))
        for name, key, value in flags:
            args.extend(('--expect-flag', f'{name}:{key}={value}'))
        result = subprocess.run(args, capture_output=True, text=True, timeout=180)
        need(len(result.stdout) + len(result.stderr) < 8_000_000, 'restart report oversized')
        self.record('restart-report-raw', {'rc': result.returncode, 'stdout': result.stdout, 'stderr': result.stderr})
        if result.returncode:
            # This checker demands a live nonempty restoration; preserve its raw red
            # result and accept ONLY the two expected empty-population failures.
            expected = {'no post-restart restoration_complete=1 line',
                        'no literal post-restart BOOT-HOLD release with restoration_complete=1'}
            failures = set(re.findall(r'^- (.+)$', result.stdout, re.M))
            need(result.returncode == 1 and failures == expected and 'Unknowns:' not in result.stdout
                 and self.inputs['schwab-1m-v2']['symbols'] == [], 'restart evidence has unexpected failure/unknown')
            self.record('restart-report-disposition', {'raw_rc': 1, 'disposition': 'held, expected by design, population=0; NOT released/PASS',
                                                       'accepted_only': sorted(expected)})

    def finalize(self):
        self.gate('final-proofs')
        self.flat('final')
        need(set(self.new) == set(RESTARTED) and set(self.announcements) == set(START), 'six new/five startup proofs incomplete')
        need(self.proof('inputs') == self.inputs, 'final read-only inputs changed')
        need(self.consume_replaces('momentum-paper', self.new['momentum-paper']), 'final owner checkpoint not applied')
        final = self.safety()
        need(final['sets'] == {CONSUMERS[name]: symbols(self.announcements[name]['event']['payload']['symbols']) for name in START},
             'final owners differ from latest five announcements')
        latest = max(stamp(x['event']['produced_at']) for x in self.announcements.values())
        self.wait_gateway(after=latest, timeout=30)
        self.record('loaded-values', self.proof('loaded'))
        scanner = self.proof('scanner')
        strategy_log = log_tail(self.logs['strategy'])
        warm = re.findall(r'prefilled momentum alert history from (\d+) snapshot batches', strategy_log)
        need(warm and int(warm[-1]) >= scanner['squeeze_10min_needs'], 'scanner warmup short/unproven; do not excuse running-gateway history')
        scanner['prefilled_batches'] = int(warm[-1])
        self.record('scanner-warmup', scanner)
        v2_log = log_tail(self.logs['schwab-1m-v2'])
        markers = [line for line in v2_log.splitlines() if '[V2-BOOT-HOLD]' in line or '[V2-BOOT-RESTORE]' in line]
        need(markers, 'literal v2 boot-hold reason absent')
        self.record('v2-hold', {'markers': markers, 'population': 0,
                              'disposition': 'held, expected by design, population=0; live release remains UNEXERCISED'})
        for name in RESTARTED:
            text = log_tail(self.logs[name])
            need('Traceback (most recent call last):' not in text, 'closeout traceback: ' + name)
            if name == 'strategy':
                need(not re.search(r'\bERROR\b', text), 'closeout strategy ERROR')
        self.install_catalogs()
        self.repin()
        self.v2_gate()
        self.flat('complete')
        self.gate('complete')
        final = self.safety()
        complete = {'at': now().isoformat(), 'application': self.release['application'],
            'release_sha256': digest(ROOT / 'release.json'), 'approval_sha256': digest(ROOT / 'approval.json'),
            'services': self.census(), 'new_services': list(RESTARTED), 'unchanged_services': list(UNTOUCHED),
            'announcements': self.announcements, 'redis_before': self.redis_before,
            'redis_peak_bytes': self.peak, 'redis_after': final, 'denominators': COUNTS,
            'unexercised': ['empty-Redis live cold boot', 'Monday scanner windows/delivery/bar continuity',
                            'real broker cancel-to-place latency', 'native partial child scaling'],
            'latest_owner_announcements': self.latest_announcements,
            'inventory_limit': 'reviewed bounded discovery only; no complete broker inventory claim'}
        commit_completion(complete)
        self.finished = True


def stream_id(value):
    need(re.fullmatch(r'\d+-\d+', value), 'malformed stream ID')
    return tuple(map(int, value.split('-')))


def journal(result):
    with JOURNAL.open('a') as output:
        output.write('\n## RPG1 + COLDSTART1 ' + now().isoformat() + ': ' + result + '\n')
        output.write('Raw evidence: `' + str(RUN) + '`. No gateway/control/capture/reconciler/Redis/Postgres actions.\n')
        for path in sorted(RUN.iterdir()):
            if path.is_file():
                output.write(f'- {path.name}: sha256={digest(path)} bytes={path.stat().st_size}\n')
        output.flush()
        os.fsync(output.fileno())


def commit_completion(complete):
    # Proofs are durable before a COMPLETE journal entry is possible. The second
    # receipt distinguishes proof completion from successful journal completion.
    save('COMPLETE.json', {**complete, 'state': 'PROOFS_COMPLETE_JOURNAL_PENDING'})
    journal('INSTALL COMPLETE')
    save('JOURNALED.json', {'at': now().isoformat(), 'state': 'INSTALL COMPLETE',
                          'complete_sha256': digest(RUN / 'COMPLETE.json'), 'journal': str(JOURNAL)})


def abort(reason):
    if not RUN.is_dir() or (RUN / 'ABORT.json').exists() or (RUN / 'JOURNALED.json').exists():
        return
    proofs_complete = (RUN / 'COMPLETE.json').exists()
    if proofs_complete:
        reason = 'proofs complete but completion journal/receipt failed: ' + reason
    rows = {}
    for name in SERVICES:
        try:
            rows[name] = identity(name)
        except Exception as exc:
            rows[name] = {'UNKNOWN': str(exc)}
    message = f'RPG1+COLDSTART1 INCOMPLETE: {reason}. No automatic continuation. Evidence {RUN}'
    # Fixed known operations topic, no secret-containing stdout or broker bodies.
    try:
        page = subprocess.run(['curl', '-sS', '--fail-with-body', '--connect-timeout', '3', '--max-time', '8',
            '-H', 'Title: RPG1+COLDSTART1 install INCOMPLETE', '-H', 'Priority: urgent',
            '-d', message[:1000], 'https://ntfy.sh/mai-tai-preopen-28806a5a97b7'],
            capture_output=True, timeout=10)
        page_result = {'rc': page.returncode, 'delivered': page.returncode == 0}
    except Exception as exc:
        page_result = {'delivered': False, 'UNKNOWN': str(exc)}
    save('ABORT.json', {'at': now().isoformat(), 'reason': reason, 'services': rows,
                        'page': page_result, 'proofs_complete': proofs_complete,
                        'result': 'INCOMPLETE; operator continuation required'})
    try:
        journal('INCOMPLETE')
    except Exception as exc:
        save('JOURNAL-FAILURE.json', {'at': now().isoformat(), 'error': str(exc),
                                    'proofs_complete': proofs_complete, 'page': page_result})


def main():
    need(os.geteuid() == 0 and RUN.is_dir() and not RUN.is_symlink(), 'exclusive root attempt directory required')
    need(sys.argv[1:2] in (['run'], ['abort']), 'supported operations: run/abort')
    if sys.argv[1] == 'abort':
        abort('shell exit ' + ' '.join(sys.argv[2:]))
        return
    verify(prepare=True)
    info = RUN.stat()
    need(info.st_uid == 0 and info.st_gid == 0 and stat.S_IMODE(info.st_mode) == 0o700,
         'attempt directory not root:root0700')
    # The lock descriptor is inherited from the literal shell; direct invocation
    # must not bypass its exclusive deployment lock or execution claim.
    need(os.fstat(9).st_ino == Path('/run/lock/project-mai-tai-deploy.lock').stat().st_ino, 'deploy lock descriptor missing')
    fcntl.flock(9, fcntl.LOCK_EX | fcntl.LOCK_NB)
    save('execution-claim.json', {'pid': os.getpid(), 'at': now().isoformat()})
    def interrupted(number, frame):
        raise Refusal('runner interrupted by signal ' + str(number))
    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGINT, interrupted)
    try:
        Coordinator(Host()).run()
    except BaseException as exc:
        abort(type(exc).__name__ + ': ' + str(exc))
        raise


if __name__ == '__main__':
    main()
