#!/usr/bin/env python3
"""C1-attended install proofs. No work on import; never starts/stops a service.

Plan-pinned, root600 source in a root700 staging directory. All failures stop the
installer. Waiting for a first publish is bounded observation, not an operation
retry. The post-start watch is one numeric600s window, not a polling daemon.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import time
from datetime import datetime, timezone

UTC = timezone.utc
REPO = Path('/home/trader/project-mai-tai')
PYTHON = REPO / '.venv/bin/python'
FLEET = ('oms', 'schwab-1m-v2', 'strategy', 'control', 'orb-schwab',
         'market-data', 'reconciler', 'momentum-paper', 'option-a-daily-guard',
         'snapshot-worker', 'trade-coach')
SELECTABLE = frozenset(FLEET[:5])
FIELDS = ('Id', 'MainPID', 'ActiveState', 'SubState', 'NRestarts',
          'ExecMainStartTimestamp', 'InvocationID', 'Result')
HEARTBEATS = {'oms': 'oms-risk', 'schwab-1m-v2': 'schwab-1m-v2',
              'strategy': 'strategy-engine', 'control': 'control-plane',
              'orb-schwab': 'orb-schwab'}
MAX_LOG_BYTES = 16 * 1024 * 1024
ERROR = re.compile(rb'(?:^|\s)(?:ERROR|CRITICAL)(?:\s|:)|Traceback \(most recent call last\)')
PUBLISH = re.compile(rb'\[BROKERDECIDES1-PUBLISH\].*\bcomplete=True\b.*\baccounts=2\b')


def need(ok, reason):
    if not ok:
        raise ValueError(reason)


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def pairs(rows):
    value = {}
    for key, item in rows:
        need(key not in value, 'duplicate JSON key')
        value[key] = item
    return value


def number(raw):
    value = float(raw)
    need(math.isfinite(value), 'nonfinite JSON number')
    return value


def load(raw):
    def reject(value):
        raise ValueError('nonfinite JSON constant: ' + value)
    return json.loads(raw, object_pairs_hook=pairs, parse_float=number, parse_constant=reject)


def private(path):
    need(not path.is_symlink(), 'private input symlink')
    info = path.stat()
    need(stat.S_ISREG(info.st_mode) and info.st_uid == 0
         and stat.S_IMODE(info.st_mode) == 0o600, 'private input is not root600')
    return path.read_bytes()


def write(path, value, *, replace=False):
    raw = (json.dumps(value, sort_keys=True, indent=2) + '\n').encode()
    target = path.with_name(path.name + '.next') if replace else path
    fd = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'wb') as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    if replace:
        os.replace(target, path)
    return sha(raw)


def command(args, *, timeout=15, maximum=524288):
    result = subprocess.run([str(x) for x in args], capture_output=True, timeout=timeout, check=False)
    need(len(result.stdout) + len(result.stderr) <= maximum, 'proof command output overflow')
    need(result.returncode == 0, 'proof command failed rc=' + str(result.returncode))
    return result.stdout


def parse_state(raw):
    rows = [line.split('=', 1) for line in raw.decode('utf-8').splitlines() if line]
    need(len(rows) == len(FIELDS) and all(len(row) == 2 for row in rows), 'unit field census incomplete')
    value = pairs(rows)
    need(set(value) == set(FIELDS), 'unit field keys differ')
    return value


def state(name):
    return parse_state(command(['systemctl', 'show', 'project-mai-tai-' + name + '.service',
                                *['--property=' + key for key in FIELDS]]))


def fleet():
    return {name: state(name) for name in FLEET}


def started(value):
    stamp = value['ExecMainStartTimestamp']
    need(re.fullmatch(r'[A-Za-z]{3} \d{4}-\d\d-\d\d \d\d:\d\d:\d\d UTC', stamp),
         'service start time is not an explicit UTC systemd value')
    return datetime.strptime(stamp, '%a %Y-%m-%d %H:%M:%S UTC').replace(tzinfo=UTC)


def running(name, value):
    need(value['Id'] == 'project-mai-tai-' + name + '.service'
         and re.fullmatch(r'[1-9][0-9]*', value['MainPID'])
         and value['ActiveState'] == 'active' and value['SubState'] == 'running'
         and value['NRestarts'] == '0' and value['Result'] == 'success'
         and re.fullmatch(r'[0-9a-f]{32}', value['InvocationID']), 'owner not active/running/NR0')
    need(started(value) <= datetime.now(UTC), 'service start is future')


def log_position(name):
    path = Path('/var/log/project-mai-tai') / (name + '.log')
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, 'rb') as stream:
        value = os.fstat(stream.fileno())
        need(stat.S_ISREG(value.st_mode), 'startup log is not a regular file')
        return dict(path=str(path), device=value.st_dev, inode=value.st_ino,
                    offset=value.st_size, bytes=0, tail='')


def consume(root, name, cursor):
    fd = os.open(cursor['path'], os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, 'rb') as stream:
        value = os.fstat(stream.fileno())
        need((value.st_dev, value.st_ino) == (cursor['device'], cursor['inode'])
             and value.st_size >= cursor['offset'], 'log rotation/truncation coverage UNKNOWN')
        stream.seek(cursor['offset'])
        raw = stream.read(MAX_LOG_BYTES - cursor['bytes'] + 1)
        need(cursor['bytes'] + len(raw) <= MAX_LOG_BYTES, 'startup/watch log exceeds bound')
    if raw:
        fd = os.open(root / (name + '.startup-watch.log'),
                     os.O_CREAT | os.O_APPEND | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'ab') as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
    combined = cursor['tail'].encode('utf-8') + raw
    decoded = combined.decode('utf-8')
    need(not ERROR.search(combined), 'startup/watch error or traceback')
    # A bounded partial line bridges read boundaries; no dropped split error/publish token.
    tail = decoded.rsplit('\n', 1)[-1]
    need(len(tail.encode()) <= 65536, 'log line exceeds bound')
    cursor.update(offset=cursor['offset'] + len(raw), bytes=cursor['bytes'] + len(raw), tail=tail)
    return combined


def verify_fleet(before, current, selected, *, changed):
    need(set(before) == set(current) == set(FLEET), 'fleet census incomplete')
    for name in FLEET:
        if name in changed:
            running(name, current[name])
            need(current[name]['MainPID'] != before[name]['MainPID']
                 and current[name]['InvocationID'] != before[name]['InvocationID'], 'new owner instance not proven')
        elif name not in selected:
            need(current[name] == before[name], 'untouched owner changed: ' + name)


def oms_ready(root, before, selected, cursors):
    need('oms' in selected, 'OMS phase without an OMS restart')
    original = state('oms')
    running('oms', original)
    need(original['MainPID'] != before['oms']['MainPID']
         and original['InvocationID'] != before['oms']['InvocationID'], 'OMS did not acquire a new instance')
    stop_at = time.monotonic() + max(0, 60 - (datetime.now(UTC) - started(original)).total_seconds())
    while True:
        need(state('oms') == original, 'OMS instance changed while awaiting publish')
        raw = consume(root, 'oms', cursors['oms'])
        for line in raw.split(b'\n')[:-1]:
            if PUBLISH.search(line):
                match = re.match(rb'(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d,\d{3})', line)
                need(match is not None, 'publish timestamp UNKNOWN')
                at = datetime.strptime(match[1].decode(), '%Y-%m-%d %H:%M:%S,%f').replace(tzinfo=UTC)
                need(started(original) <= at <= datetime.now(UTC)
                     and (at - started(original)).total_seconds() <= 60
                     and (datetime.now(UTC) - at).total_seconds() <= 30, 'publish is not current startup evidence')
                write(root / 'oms-ready.json', dict(owner=original, source_line=line.decode(),
                    complete=True, accounts=2, observed_at_utc=datetime.now(UTC).isoformat()))
                return
        need(time.monotonic() < stop_at, 'no fresh complete=True/accounts=2 startup publication within60s')
        time.sleep(min(1, max(0, stop_at - time.monotonic())))


def native_census_evidence(raw, line, accounts, owner, now):
    """Read the OMS's EXISTING published native census; never fetch a provider."""
    need(type(raw) is bytes and 0 < len(raw) <= 512 * 1024, 'native census absent/overflow')
    value = load(raw)
    need(type(value) is dict and type(value.get('schema')) is int and value['schema'] == 3
         and value.get('local_complete') is True
         and value.get('reason') == 'complete_native_and_local_census', 'native/local census incomplete')
    need(type(value.get('epoch')) is str and 0 < len(value['epoch']) <= 64
         and type(value.get('generation')) is int and value['generation'] > 0, 'native publisher identity UNKNOWN')
    start, end = value.get('started_ms'), value.get('completed_ms')
    now_ms = int(now.timestamp() * 1000)
    need(type(start) is int and type(end) is int
         and int(started(owner).timestamp() * 1000) <= start <= end <= now_ms
         and now_ms - start < 60000, 'native census old/future/wrong process')
    need(type(value.get('inflight')) is list and not value['inflight'], 'local unresolved in-flight obligation')
    books = value.get('books')
    need(type(books) is list and len(books) == 2 and all(type(book) is dict for book in books)
         and len(set(accounts)) == 2
         and set(book.get('account') for book in books) == set(accounts)
         and {book.get('provider') for book in books} == {'schwab', 'webull'}, 'native account census differs')
    for book in books:
        a, b = book.get('wire_started_ms'), book.get('wire_completed_ms')
        need(book.get('complete') is True and type(a) is int and type(b) is int
             and start <= a <= b <= end, 'native wire completeness/interval UNKNOWN')
        need(book.get('working') == [] and book.get('positions') == [],
             'native working order or position exists; manual exposure also holds')
        need(book.get('scope_policy') in ('all_native_working_including_previous_sessions',
             'schwab_reviewed_window_20261022_145124'), 'native universe UNKNOWN')
        if book['scope_policy'] == 'schwab_reviewed_window_20261022_145124':
            need(book['provider'] == 'schwab' and type(book.get('window_days')) is int
                 and 1 <= book['window_days'] <= 181, 'reviewed Schwab window UNKNOWN')
        else:
            need(book.get('window_days') is None, 'native all-working window unexpectedly narrowed')
    need(PUBLISH.search(line) and b'reason=complete_native_and_local_census' in line,
         'latest actual OMS publication is not complete two-account census')
    generation = re.search(rb'\bgeneration=([0-9]+)\b', line)
    stamp = re.match(rb'(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d,\d{3})', line)
    need(generation is not None and int(generation[1]) == value['generation'] and stamp is not None,
         'native packet/publication generation mismatch or clock UNKNOWN')
    at = datetime.strptime(stamp[1].decode(), '%Y-%m-%d %H:%M:%S,%f').replace(tzinfo=UTC)
    need(start <= int(at.timestamp() * 1000) <= now_ms and (now-at).total_seconds() < 60,
         'native publication is stale/future')
    return dict(epoch=value['epoch'], generation=value['generation'], accounts=list(accounts),
        native_scopes={book['account']: dict(policy=book.get('scope_policy'),
            window_days=book.get('window_days')) for book in books},
        all_native_working_symbol_counts={book['account']: len(book['working']) for book in books},
        all_native_position_symbol_counts={book['account']: len(book['positions']) for book in books},
        source_sha256=sha(raw), publication=line.decode(), checked_at_utc=now.isoformat())


def pre_first_stop(root, before):
    current = fleet()
    need(current == before, 'fleet changed before first stop')
    owner = current['oms']; running('oms', owner)
    # Select only transport namespace and account aliases. Never persist process env or secrets.
    names = {'MAI_TAI_REDIS_URL', 'MAI_TAI_REDIS_STREAM_PREFIX',
             'MAI_TAI_STRATEGY_SCHWAB_1M_V2_ACCOUNT_NAME',
             'MAI_TAI_STRATEGY_SCHWAB_1M_V2_WEBULL_ACCOUNT_NAME'}
    with open('/proc/' + owner['MainPID'] + '/environ', 'rb') as stream:
        environ = stream.read(1048577)
    need(len(environ) <= 1048576, 'OMS environment overflow')
    selected = {}
    for entry in environ.split(b'\0'):
        key, separator, item = entry.partition(b'=')
        key = key.decode('utf-8')
        if key.upper() in names:
            need(separator and key in names and key not in selected, 'OMS transport/account env alias')
            selected[key] = item.decode('utf-8')
    accounts = tuple(selected.get(name, '') for name in (
        'MAI_TAI_STRATEGY_SCHWAB_1M_V2_ACCOUNT_NAME',
        'MAI_TAI_STRATEGY_SCHWAB_1M_V2_WEBULL_ACCOUNT_NAME'))
    need(all(name.startswith('live:') for name in accounts), 'actual OMS account aliases UNKNOWN')
    prefix = selected.get('MAI_TAI_REDIS_STREAM_PREFIX', 'mai_tai')
    need(re.fullmatch(r'[A-Za-z0-9_-]{1,64}', prefix), 'OMS census namespace UNKNOWN')
    try:
        import redis
        from redis.retry import Retry
        from redis.backoff import NoBackoff
        client = redis.Redis.from_url(selected.get('MAI_TAI_REDIS_URL', 'redis://localhost:6379/0'),
            socket_connect_timeout=2, socket_timeout=2, retry_on_timeout=False,
            retry=Retry(NoBackoff(), 0))
        try:
            packet = client.get(prefix + ':brokerdecides1-native-book')
        finally:
            client.close()
    except Exception:
        raise ValueError('existing OMS native census read UNKNOWN') from None
    with open('/var/log/project-mai-tai/oms.log', 'rb') as stream:
        stream.seek(0, os.SEEK_END); end = stream.tell()
        stream.seek(max(0, end - MAX_LOG_BYTES))
        tail = stream.read(MAX_LOG_BYTES + 1)
    need(len(tail) <= MAX_LOG_BYTES, 'OMS publication tail overflow')
    lines = tail.split(b'\n')[:-1]
    candidates = [line for line in lines if b'[BROKERDECIDES1-PUBLISH]' in line]
    need(candidates, 'no current OMS publication in bounded log tail')
    proof = native_census_evidence(packet, candidates[-1], accounts, owner, datetime.now(UTC))
    need(fleet() == before, 'owner changed during pre-stop native census proof')
    need(not Path('/var/lib/project-mai-tai/option-a/safety-stop.json').exists()
         and not Path('/var/lib/project-mai-tai/option-a/safety-stop.json').is_symlink(), 'safety STOP exists')
    write(root / 'pre-first-stop-native-census.json', proof)
    fd = os.open(root / 'pre-first-stop-native-census-packet.json',
        os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'wb') as stream:
        stream.write(packet); stream.flush(); os.fsync(stream.fileno())
    # Fleet sampling can take time: age is checked again immediately before returning to stop.
    native_census_evidence(packet, candidates[-1], accounts, owner, datetime.now(UTC))


def health_ready(root, name, before, selected):
    need(name in selected, 'health phase for an untouched service')
    owner = state(name)
    running(name, owner)
    need(owner['MainPID'] != before[name]['MainPID']
         and owner['InvocationID'] != before[name]['InvocationID'], 'health owner is not new')
    raw = command([PYTHON, '-m', 'project_mai_tai.post_restart_health_gate',
        '--health-url', 'http://127.0.0.1:8100/health',
        '--service-name', HEARTBEATS[name], '--process-start-epoch',
        str(started(owner).timestamp()), '--pid', owner['MainPID'],
        '--expected-sha', load(private(root / 'reviewed-plan.json'))['target_sha'],
        '--sla-seconds', '240' if name == 'strategy' else '60'], timeout=250)
    need(state(name) == owner, 'health owner changed during first-health proof')
    path = root / 'first-health.json'
    health = load(private(path)) if path.exists() else {}
    need(name not in health, 'first-health proof repeated')
    health[name] = dict(owner=owner, output=raw.decode('utf-8'),
                        observed_at_utc=datetime.now(UTC).isoformat())
    write(path, health, replace=path.exists())


def post_start(root, plan, before, selected, cursors):
    baseline = fleet()
    verify_fleet(before, baseline, selected, changed=selected)
    catalog = REPO / 'ops/health/expected_flags.json'
    numeric = REPO / 'ops/health/expected_numeric.json'
    flags = command([PYTHON, REPO / 'ops/health/expected_flags_check.py',
                     '--catalog', catalog, '--numeric-catalog', numeric], timeout=60)
    fd = os.open(root / 'process-flags.txt', os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'wb') as stream:
        stream.write(flags)
    health = load(private(root / 'first-health.json'))
    need(set(health) == selected, 'first-health service census incomplete')
    for name in selected:
        need(health[name]['owner'] == baseline[name], 'first-health owner differs from final owner')
    start, start_utc, samples = time.monotonic(), datetime.now(UTC), []
    while True:
        now = time.monotonic()
        need(fleet() == baseline, 'fleet instance/state changed during numeric600watch')
        need(not Path('/var/lib/project-mai-tai/option-a/safety-stop.json').exists()
             and not Path('/var/lib/project-mai-tai/option-a/safety-stop.json').is_symlink(), 'safety STOP appeared')
        for name in selected:
            consume(root, name, cursors[name])
        samples.append(dict(at_utc=datetime.now(UTC).isoformat(), elapsed_seconds=time.monotonic() - start))
        if now - start >= 600:
            break
        time.sleep(min(20, max(0, 600 - (time.monotonic() - start))))
    elapsed = time.monotonic() - start
    need(elapsed >= 600 and len(samples) >= 2, 'numeric600watch incomplete')
    write(root / 'post-start-proof.json', dict(started_at_utc=start_utc.isoformat(),
        completed_at_utc=datetime.now(UTC).isoformat(), elapsed_seconds=elapsed, samples=samples,
        fleet=baseline, flags_sha256=sha(flags), catalog_sha256=sha(catalog.read_bytes()),
        numeric_sha256=sha(numeric.read_bytes()), log_cursors=cursors,
        scope='bounded startup/flags/health/identity/watch; not future trading or native economics'))


def main(argv=None):
    phase, directory = sys.argv[1:] if argv is None else argv
    root = Path(directory)
    need(os.geteuid() == 0 and not root.is_symlink(), 'proof requires C1 root/private namespace')
    info = root.stat()
    need(info.st_uid == 0 and stat.S_IMODE(info.st_mode) == 0o700, 'proof directory is not root700')
    plan = load(private(root / 'reviewed-plan.json'))
    selected = set(private(root / 'selected-services.txt').decode().splitlines())
    need(selected and selected <= SELECTABLE and 'schwab-1m-v2' in selected, 'unsupported selected service scope')
    if phase == 'before-stop':
        current = fleet()
        for name in selected:
            running(name, current[name])
            log_position(name)  # Fail BEFORE stopping if a required log is unreadable.
        write(root / 'before-fleet.json', current)
        write(root / 'log-cursors.json', {})
        return 0
    before = load(private(root / 'before-fleet.json'))
    cursors = load(private(root / 'log-cursors.json'))
    if phase == 'pre-first-stop':
        pre_first_stop(root, before)
    elif phase.startswith('before-start:'):
        name = phase.split(':', 1)[1]
        need(name in selected and name not in cursors, 'startup log baseline repeated/foreign')
        cursors[name] = log_position(name)  # Old process is now stopped; cover ALL new startup bytes.
    elif phase.startswith('health-ready:'):
        health_ready(root, phase.split(':', 1)[1], before, selected)
    elif phase == 'oms-ready':
        oms_ready(root, before, selected, cursors)
    elif phase == 'post-start':
        need(set(cursors) == selected, 'startup log population incomplete')
        post_start(root, plan, before, selected, cursors)
    else:
        raise ValueError('unknown proof phase')
    write(root / 'log-cursors.json', cursors, replace=True)
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception as exc:
        print('STOP: install proof ' + type(exc).__name__ + ': ' + str(exc), file=sys.stderr)
        raise SystemExit(2)
