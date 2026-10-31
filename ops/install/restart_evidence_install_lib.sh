#!/usr/bin/env bash
# Sourced by five_service_install.sh only. No command runs merely by sourcing it.
# This is evidence and execution machinery, not an approval mechanism.
rei_python() {
  "$REI_PY" - "$@" <<'PY'
import hashlib, json, os, re, stat, sys, tempfile
from datetime import datetime, timezone
from pathlib import Path

def need(ok, reason):
    if not ok:
        raise ValueError(reason)

def pairs(rows):
    out = {}
    for key, value in rows:
        need(key not in out, 'duplicate JSON key')
        out[key] = value
    return out

def reject(_):
    raise ValueError('non-integer JSON number')

def load(raw):
    return json.loads(raw, object_pairs_hook=pairs, parse_float=reject, parse_constant=reject)

def digest(raw):
    return hashlib.sha256(raw).hexdigest()

def stamp(value):
    return tuple(getattr(value, key) for key in ('st_dev', 'st_ino', 'st_uid', 'st_gid', 'st_mode', 'st_size', 'st_mtime_ns', 'st_ctime_ns'))

def secure_read(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, 'rb') as stream:
        before = os.fstat(stream.fileno())
        need(stat.S_ISREG(before.st_mode) and before.st_uid == 0
             and stat.S_IMODE(before.st_mode) == 0o600, 'input must be root-owned regular0600')
        raw = stream.read(1_048_577)
        after = os.fstat(stream.fileno())
        need(len(raw) <= 1_048_576 and stamp(before) == stamp(after), 'input changed or overflow')
    return raw, before

def exclusive(path, raw):
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'wb') as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())

def encoded(value):
    return (json.dumps(value, sort_keys=True, indent=2) + '\n').encode()

def plan_payload(raw, target, services):
    value = load(raw)
    fields = {'version', 'target_sha', 'expected_current_sha', 'expected_env_sha256',
              'install_date_et', 'expected_alembic_head', 'evidence_directory',
              'updates', 'remove', 'expect_flags', 'clock_override_reason',
              'verification_hook', 'verification_hook_sha256'}
    need(type(value) is dict and set(value) == fields and type(value['version']) is int
         and value['version'] == 1, 'unsupported exact plan schema')
    need(re.fullmatch('[0-9a-f]{40}', target) and value['target_sha'] == target
         and re.fullmatch('[0-9a-f]{40}', value['expected_current_sha'])
         and re.fullmatch('[0-9a-f]{64}', value['expected_env_sha256']), 'unbound source/env preimage')
    need(re.fullmatch(r'\d{4}-\d\d-\d\d', value['install_date_et'])
         and re.fullmatch(r'[A-Za-z0-9_]+', value['expected_alembic_head']), 'unbound day/schema')
    directory = Path(value['evidence_directory'])
    need(str(directory).startswith('/home/trader/after-hours/') and directory.is_absolute()
         and '..' not in directory.parts and str(directory) == value['evidence_directory'],
         'evidence directory is not a canonical private installation namespace')
    updates, remove = value['updates'], value['remove']
    need(type(updates) is dict and type(remove) is list and len(remove) == len(set(remove))
         and set(updates).isdisjoint(remove), 'ambiguous environment edits')
    need(updates.get('MOMLIVE_SEC_APPROVED_HEAD') == target, 'MOMLIVE pin must name target')
    for key, item in updates.items():
        need(key == 'MOMLIVE_SEC_APPROVED_HEAD' or
             re.fullmatch(r'MAI_TAI_[A-Z0-9_]+_ENABLED', key) and item in ('true', 'false'),
             'only explicit boolean switches and MOMLIVE pin are supported')
    need(all(type(key) is str and re.fullmatch(r'MAI_TAI_[A-Z0-9_]+_ENABLED', key)
             for key in remove), 'removal outside boolean switch scope')
    allowed = {'oms', 'schwab-1m-v2', 'strategy', 'control', 'orb-schwab'}
    names = services.split(',')
    need(names and len(names) == len(set(names)) and set(names) <= allowed
         and 'schwab-1m-v2' in names, 'focused collector install requires v2 and only selected five services')
    flags = value['expect_flags']
    need(type(flags) is list and flags and len(flags) == len(set(flags)), 'explicit process flags required')
    for flag in flags:
        need(type(flag) is str and re.fullmatch(r'[a-z0-9-]+:[A-Z0-9_]+=(true|false|[0-9]+)', flag)
             and flag.split(':', 1)[0] in names, 'flag is malformed or names an untouched service')
    reason = value['clock_override_reason']
    need(reason is None or type(reason) is str and 0 < len(reason) <= 512
         and '\n' not in reason and '\r' not in reason, 'clock-only reason malformed')
    hook = Path(value['verification_hook'])
    need(hook.is_absolute() and '..' not in hook.parts and hook.suffix == '.py'
         and re.fullmatch('[0-9a-f]{64}', value['verification_hook_sha256']), 'unbound proof hook')
    return value

def environment_candidate(original, updates, remove):
    governed, seen, out = set(updates) | set(remove), set(), []
    for line in original.decode('utf-8').splitlines(keepends=True):
        found = re.match(r'^(?:export[ \t]+)?([A-Za-z_][A-Za-z0-9_]*)=', line)
        if found and found[1].upper() in governed:
            key = found[1]
            need(key in governed and key not in seen, 'environment duplicate/alias')
            seen.add(key)
            if key in updates:
                out.append(key + '=' + updates[key] + '\n')
        else:
            out.append(line)
    need(set(remove) <= seen, 'requested removal does not exist')
    append = sorted(set(updates) - seen)
    if append and out and not out[-1].endswith('\n'):
        out[-1] += '\n'
    out.extend(key + '=' + updates[key] + '\n' for key in append)
    return ''.join(out).encode()


def moment(raw):
    value = datetime.fromisoformat(raw)
    need(value.tzinfo is not None and value.utcoffset() is not None, 'journal clock has no timezone')
    return value


def record_payload(before, events, selected, journal_path):
    population = {'control', 'market-capture', 'market-data', 'oms', 'reconciler',
                  'schwab-1m-v2', 'strategy', 'tv-alerts', 'orb-schwab'}
    need(type(before) is dict and before.get('schema_version') == 3
         and type(before.get('services')) is dict
         and set(before['services']) == population, 'original complete9 collector snapshot missing')
    need(len(selected) == len(set(selected)) and set(selected) <= {'oms', 'schwab-1m-v2', 'strategy', 'control', 'orb-schwab'}
         and 'schwab-1m-v2' in selected, 'new/duplicate service cannot be relabelled restarted')
    expected = [(operation, name) for operation, names in (
        ('stop', ['orb-schwab', 'schwab-1m-v2', 'strategy', 'oms', 'control']),
        ('start', ['oms', 'schwab-1m-v2', 'strategy', 'control', 'orb-schwab']))
        for name in names if name in selected]
    actual = [(row.get('operation'), row.get('service')) for row in events
              if row.get('operation') in ('stop', 'start')]
    need(actual == expected, 'actual stop/start order or census differs')
    previous = moment(before['captured_at_utc'])
    for row in events:
        operation, name = row.get('operation'), row.get('service')
        need(name in selected and operation in ('stop-request', 'stop', 'start-request', 'start')
             and row.get('result') == ('entered' if operation.endswith('-request') else 'success'),
             'journal contains unknown/failed/foreign operation')
        at = moment(row['at_utc'])
        need(at >= previous, 'journal predates snapshot or regresses')
        previous = at
    # Every successful physical action must have precisely its preceding entered request.
    need([(row['operation'], row['service']) for row in events] ==
         [(op, name) for operation, name in expected
          for op in (operation + '-request', operation)], 'entered action journal incomplete/duplicated')
    return dict(schema_version=1, snapshot_captured_at_utc=before['captured_at_utc'],
        source_journal=journal_path, service_actions={name: 'restarted' if name in selected
            else 'deliberately_untouched' for name in before['services']})


mode, *args = sys.argv[1:]
try:
    if mode == 'validate':
        path, target, services = args
        raw, _ = secure_read(path)
        value = plan_payload(raw, target, services)
        directory = Path(value['evidence_directory'])
        need(not directory.exists() and str(directory) == str(directory.resolve(strict=False)),
             'evidence namespace consumed or resolves through a link')
        print(json.dumps(value, sort_keys=True))
    elif mode == 'env-check':
        payload, env_path = args
        original, _ = secure_read(env_path)
        need(digest(original) == load(payload)['expected_env_sha256'], 'environment pre-stop CAS mismatch')
    elif mode == 'hook-check':
        value = load(args[0])
        raw, _ = secure_read(value['verification_hook'])
        parent = Path(value['verification_hook']).parent.stat()
        need(parent.st_uid == 0 and stat.S_IMODE(parent.st_mode) == 0o700
             and digest(raw) == value['verification_hook_sha256'], 'proof hook source/private parent differs')
    elif mode == 'field':
        value = load(args[0])[args[1]]
        if type(value) is list:
            print('\n'.join(value))
        elif value is not None:
            print(value)
    elif mode == 'event':
        path, operation, service, result = args
        row = dict(at_utc=datetime.now(timezone.utc).isoformat(), operation=operation,
                   service=service, result=result)
        fd = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'ab') as stream:
            stream.write((json.dumps(row, sort_keys=True) + '\n').encode())
            stream.flush()
            os.fsync(stream.fileno())
    elif mode == 'env':
        payload, env_path, backup_path = args
        value = load(payload)
        original, before = secure_read(env_path)
        need(digest(original) == value['expected_env_sha256'], 'environment preimage CAS mismatch')
        candidate = environment_candidate(original, value['updates'], value['remove'])
        exclusive(backup_path, original)
        parent = Path(env_path).parent
        fd, temporary = tempfile.mkstemp(prefix='.five-service-env-', dir=parent)
        try:
            with os.fdopen(fd, 'wb') as stream:
                os.fchmod(stream.fileno(), 0o600)
                os.fchown(stream.fileno(), 0, 0)
                stream.write(candidate)
                stream.flush()
                os.fsync(stream.fileno())
            actual, current = secure_read(env_path)
            need(stamp(current) == stamp(before) and actual == original, 'environment changed before replacement')
            os.replace(temporary, env_path)
            directory = os.open(parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
            readback, _ = secure_read(env_path)
            need(readback == candidate, 'environment readback mismatch')
            print(json.dumps(dict(before_sha256=digest(original), after_sha256=digest(candidate))))
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
    elif mode == 'gate-args':
        payload, services, snapshot, record, output = args
        value = load(payload)
        exclusive(output, encoded(dict(snapshot=snapshot, install_record=record,
            restarted=services.split(','), expect_flags=value['expect_flags'],
            expected_alembic_head=value['expected_alembic_head'], no_schema_change=True,
            disposition='C1 reviewed future preopen CAS input only; NOT published or native preopen PASS')))
    elif mode == 'record':
        snapshot_path, journal_path, services, output = args
        before = load(Path(snapshot_path).read_bytes())
        events = [load(line) for line in Path(journal_path).read_text().splitlines()]
        selected = services.split(',')
        exclusive(output, encoded(record_payload(before, events, selected, journal_path)))
    else:
        raise ValueError('unknown evidence helper mode')
except Exception as exc:
    print('STOP: evidence helper ' + type(exc).__name__ + ': ' + str(exc), file=sys.stderr)
    raise SystemExit(2)
PY
}

rei_snapshot() {
  local rc
  if "$REI_PY" "$REI_COLLECTOR" snapshot --output "$REI_SNAPSHOT" \
      >"$REI_EVIDENCE/snapshot.stdout" 2>"$REI_EVIDENCE/snapshot.stderr"; then
    sha256sum "$REI_SNAPSHOT" >"$REI_EVIDENCE/snapshot.sha256"
  else
    rc=$?
    printf 'STOP: pre-stop collector rc=%s; no service action permitted\n' "$rc" >&2
    return "$rc"
  fi
}

rei_report() {
  sha256sum -c "$REI_EVIDENCE/snapshot.sha256" >/dev/null || return $?
  local flags flag rc head
  local -a args=(report --snapshot "$REI_SNAPSHOT" --install-record "$REI_RECORD")
  for flag in "${FSI_SERVICES[@]}"; do args+=(--restarted "$flag"); done
  flags=$(rei_python field "$FSI_PLAN_JSON" expect_flags) || return $?
  while IFS= read -r flag; do args+=(--expect-flag "$flag"); done <<<"$flags"
  head=$(rei_python field "$FSI_PLAN_JSON" expected_alembic_head) || return $?
  args+=(--expected-alembic-head "$head"
         --no-schema-change --output "$REI_EVIDENCE/official-restart-report.md")
  # One attended official report. The scheduled gate later uses its root-pinned wrapper.
  if "$REI_PY" "$REI_COLLECTOR" "${args[@]}" \
      >"$REI_EVIDENCE/report.stdout" 2>"$REI_EVIDENCE/report.stderr"; then
    printf '0\n' >"$REI_EVIDENCE/report.rc"
  else
    rc=$?
    printf '%s\n' "$rc" >"$REI_EVIDENCE/report.rc"
    printf 'STOP: post-start report rc=%s; retain all raw evidence, no retry\n' "$rc" >&2
    return "$rc"
  fi
}
