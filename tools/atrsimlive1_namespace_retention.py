"""Opt-in volatile-cache retention; never prune evidence or infer reader coverage.

The composer must put EVERY CacheStore.directory() use under shared_parent_lease
before opening its child FD. Legacy/unsealed parents and incomplete protection
catalogs HOLD. This module does not activate a job or grant an operator permit.
"""
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
from zoneinfo import ZoneInfo

DEFAULT_ENABLED = False
PROTOCOL = 'ATRSIM_NAMESPACE_READER_LEASE_V1'
SCHEMA = 'ATRSIM_NAMESPACE_RETENTION_V1'
ET = ZoneInfo('America/New_York')
NAME = re.compile(r'(\d{4}-\d{2}-\d{2})-([0-9a-f]{64})\Z')
MAX_NAMESPACES = 128
MAX_CACHE_BYTES = 512 * 1024 * 1024
MAX_TOTAL_HASH_BYTES = 2 * 1024**3


class RetentionRefusal(ValueError):
    pass


def _need(condition, code):
    if not condition:
        raise RetentionRefusal(code)


def _encode(value):
    return (json.dumps(value, sort_keys=True, separators=(',', ':'),
                       ensure_ascii=False, allow_nan=False) + '\n').encode()


def _digest(value):
    return hashlib.sha256(_encode(value)).hexdigest()


def approval_sha256(value):
    """Literal review seal; computing it is NOT permission to execute a plan."""
    return _digest(value)


def _pairs(items):
    out = {}
    for key, value in items:
        _need(key not in out, 'duplicate_metadata_key')
        out[key] = value
    return out


def _bad(_):
    raise RetentionRefusal('nonfinite_metadata')


def _sha(value):
    _need(type(value) is str and re.fullmatch('[0-9a-f]{64}', value), 'sha256_required')
    return value


def _clock(value):
    _need(type(value) is datetime and value.utcoffset() is not None, 'aware_clock_required')
    return value.astimezone(timezone.utc)


def _stamp(value):
    _need(type(value) is str, 'literal_manifest_clock_required')
    return _clock(datetime.fromisoformat(value))


@dataclass(frozen=True)
class TradingCalendar:
    first_year: int
    last_year: int
    holidays: frozenset[date]
    source_sha256: str

    def trading(self, day):
        _sha(self.source_sha256)
        _need(type(day) is date and self.first_year <= day.year <= self.last_year,
              'calendar_coverage_unknown')
        return day.weekday() < 5 and day not in self.holidays

    def last_seven_completed(self, now):
        """Seven completed extended sessions; early-close days still end20ET.

        The current/open date is additionally protected. Calendar gaps refuse;
        this never extrapolates an unlisted year or substitutes directory mtime.
        """
        local = _clock(now).astimezone(ET)
        cursor, days = local.date(), []
        for _ in range(40):
            if self.trading(cursor) and datetime.combine(cursor, time(20), ET) <= local:
                days.append(cursor)
                if len(days) == 7:
                    return tuple(reversed(days))
            cursor -= timedelta(days=1)
        raise RetentionRefusal('seven_complete_sessions_not_proven')


def _directory(path):
    path = Path(path)
    _need(path.is_absolute() and '..' not in path.parts
          and not any(p.is_symlink() for p in (path, *path.parents)), 'literal_private_parent_required')
    info = path.stat()
    _need(stat.S_ISDIR(info.st_mode) and info.st_uid == os.geteuid()
          and stat.S_IMODE(info.st_mode) == 0o700, 'owned_exact0700_directory_required')
    return path, (info.st_dev, info.st_ino)


def _regular(info):
    _need(stat.S_ISREG(info.st_mode) and info.st_uid == os.geteuid()
          and stat.S_IMODE(info.st_mode) == 0o600 and info.st_nlink == 1,
          'owned_single_link0600_file_required')


def _identity(info):
    return [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns]


def _read(directory, name, cap=1024 * 1024):
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory)
    with os.fdopen(fd, 'rb') as stream:
        before = os.fstat(stream.fileno()); _regular(before)
        _need(before.st_size <= cap, 'metadata_size_cap')
        raw = stream.read(cap + 1)
        after = os.fstat(stream.fileno())
    _need(len(raw) == before.st_size and _identity(before) == _identity(after)
          == _identity(os.stat(name, dir_fd=directory, follow_symlinks=False)), 'metadata_changed')
    return raw, _identity(after)


def _metadata(directory, name):
    raw, identity = _read(directory, name)
    value = json.loads(raw, object_pairs_hook=_pairs, parse_constant=_bad,
                       parse_float=lambda _: _bad(None))
    _need(type(value) is dict, 'metadata_object_required')
    return value, hashlib.sha256(raw).hexdigest(), identity


@contextmanager
def _lease(parent, exclusive):
    parent, identity = _directory(parent)
    directory = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    lock = None
    try:
        _need((os.fstat(directory).st_dev, os.fstat(directory).st_ino) == identity, 'parent_changed')
        lock = os.open('.retention.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW,
                       0o600, dir_fd=directory)
        _regular(os.fstat(lock))
        try:
            fcntl.flock(lock, (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RetentionRefusal('reader_or_writer_active_HOLD') from exc
        _need((parent.stat().st_dev, parent.stat().st_ino) == identity, 'parent_path_changed')
        yield directory, identity
    finally:
        if lock is not None:
            os.close(lock)
        os.close(directory)


@contextmanager
def shared_parent_lease(parent, *, enabled=DEFAULT_ENABLED):
    """A disabled hook does NO I/O. Enabled hook spans the entire reader use."""
    _need(type(enabled) is bool, 'literal_boolean_required')
    if not enabled:
        yield
        return
    with _lease(parent, False):
        yield


def cache_parent(root):
    root = Path(root)
    return root.parent if NAME.fullmatch(root.name) else root


def _controls(directory, parent_identity, expected_profile):
    role, _, _ = _metadata(directory, '.retention-parent.json')
    _need(role == {'schema': SCHEMA, 'kind': 'VOLATILE_CACHE_PARENT',
                   'device': parent_identity[0], 'inode': parent_identity[1]}, 'parent_role_unproven_HOLD')
    catalog, digest, identity = _metadata(directory, '.retention-protection.json')
    _need(set(catalog) == {'schema', 'reader_protocol', 'source_profile_sha256',
                          'coverage_complete', 'generation', 'current', 'last_good', 'active', 'preserved'},
          'complete_protection_schema_required')
    _need(catalog['schema'] == SCHEMA and catalog['reader_protocol'] == PROTOCOL
          and catalog['source_profile_sha256'] == _sha(expected_profile)
          and catalog['coverage_complete'] is True and type(catalog['generation']) is int
          and catalog['generation'] > 0, 'reader_reference_coverage_unknown_HOLD')
    protected = set()
    for key in ('current', 'last_good', 'active', 'preserved'):
        values = catalog[key]
        _need(type(values) is list and len(values) == len(set(values))
              and all(type(x) is str and NAME.fullmatch(x) for x in values), 'literal_protected_names_required')
        protected.update(values)
    _need(catalog['last_good'], 'last_good_required_HOLD')
    return protected, digest, identity


def _inspect(directory, name, now, calendar, oldest, protected, budget):
    result = {'name': name, 'action': 'HOLD', 'reason': 'UNKNOWN_METADATA'}
    if name in protected:
        return dict(result, reason='CURRENT_LAST_GOOD_ACTIVE_OR_PRESERVED')
    match = NAME.fullmatch(name)
    if match is None:
        return dict(result, reason='UNKNOWN_ENTRY_NO_RECURSION')
    fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
    try:
        info = os.fstat(fd)
        _need(stat.S_IMODE(info.st_mode) == 0o700 and info.st_uid == os.geteuid(), 'namespace_private_mode')
        result['directory_identity'] = [info.st_dev, info.st_ino]
        manifest, msha, mid = _metadata(fd, '.retention.json')
        _need(set(manifest) == {'schema', 'kind', 'name', 'binding', 'created_at', 'closed_at', 'cache_sha256'},
              'closed_manifest_schema_required')
        binding = manifest['binding']
        _need(type(binding) is dict and set(binding) == {
            'version', 'trading_day', 'source_seal', 'policies', 'input_scope'}
            and binding['version'] == 'C1_20261028_200908_ATRSIM_INCREMENTAL_V1'
            and binding['input_scope'] in ('CURRENT_RR', 'RETAINED_FILE')
            and type(binding['policies']) is dict and set(binding['policies']) == {
                'execution_rules_sha256', 'null_policy', 'same_sip_policy'}, 'literal_cache_binding_required')
        _sha(binding['source_seal']); _sha(binding['policies']['execution_rules_sha256'])
        # Identical plain-JSON binding fingerprint used by the existing namespace.
        bsha = hashlib.sha256(_encode(binding).rstrip(b'\n')).hexdigest()
        _need(manifest['schema'] == SCHEMA and manifest['kind'] == 'VOLATILE_CACHE_NAMESPACE'
              and manifest['name'] == name and binding['trading_day'] == match[1]
              and bsha == match[2], 'namespace_binding_mismatch')
        day = date.fromisoformat(match[1]); calendar.trading(day)
        created, closed = _stamp(manifest['created_at']), _stamp(manifest['closed_at'])
        _need(created <= closed <= now and closed.astimezone(ET).date() >= day, 'namespace_clock_unproven')
        if day >= oldest:
            return dict(result, reason='WITHIN_SEVEN_COMPLETED_SESSIONS_OR_CURRENT')
        # A freshly sealed namespace for an old source day gets seven FULL closes too.
        seven = calendar.last_seven_completed(now)
        if closed >= datetime.combine(seven[0], time(20), ET).astimezone(timezone.utc):
            return dict(result, reason='SEVEN_FULL_CLOSES_SINCE_SEAL_NOT_ELAPSED')
        _need(set(os.listdir(fd)) == {'.lock', 'cache.json', '.retention.json'}, 'unknown_or_partial_child_HOLD')
        lock = os.open('.lock', os.O_RDWR | os.O_NOFOLLOW, dir_fd=fd)
        try:
            _regular(os.fstat(lock))
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            cache = os.open('cache.json', os.O_RDONLY | os.O_NOFOLLOW, dir_fd=fd)
            with os.fdopen(cache, 'rb') as stream:
                before = os.fstat(stream.fileno()); _regular(before)
                _need(before.st_size <= MAX_CACHE_BYTES, 'cache_cap_HOLD')
                budget[0] += before.st_size
                _need(budget[0] <= MAX_TOTAL_HASH_BYTES, 'retention_hash_budget_HOLD')
                checksum = hashlib.sha256()
                while block := stream.read(1024 * 1024):
                    checksum.update(block)
                _need(_identity(os.fstat(stream.fileno())) == _identity(before), 'cache_changed')
            _need(checksum.hexdigest() == _sha(manifest['cache_sha256']), 'cache_hash_mismatch')
            lock_id = _identity(os.fstat(lock))
        finally:
            os.close(lock)
        result.update(action='PRUNE_CANDIDATE', reason='EXPIRED_UNREFERENCED_SEALED_VOLATILE_CACHE',
                      files={'.retention.json': {'sha256': msha, 'identity': mid},
                             'cache.json': {'sha256': checksum.hexdigest(), 'identity': _identity(before)},
                             '.lock': {'identity': lock_id}}, bytes=before.st_size)
        return result
    finally:
        os.close(fd)


def _scan(directory, parent_identity, now, calendar, profile):
    protected, catalog_sha, catalog_id = _controls(directory, parent_identity, profile)
    dates = calendar.last_seven_completed(now)
    names = sorted(n for n in os.listdir(directory) if n not in {
        '.retention.lock', '.retention-parent.json', '.retention-protection.json', '.retention-receipts'})
    _need(len(names) <= MAX_NAMESPACES and protected <= set(names), 'namespace_roster_or_reference_gap_HOLD')
    rows, budget = [], [0]
    for name in names:
        try:
            rows.append(_inspect(directory, name, now, calendar, dates[0], protected, budget))
        except (OSError, ValueError, KeyError, TypeError) as exc:
            rows.append({'name': name, 'action': 'HOLD', 'reason': str(exc) if isinstance(exc, RetentionRefusal)
                         else type(exc).__name__})
    return dict(schema=SCHEMA, status='DRY_RUN_ONLY', at=now.isoformat(),
                parent_identity=list(parent_identity), calendar_source_sha256=calendar.source_sha256,
                retained_completed_days=[d.isoformat() for d in dates], protection_sha256=catalog_sha,
                protection_identity=catalog_id, source_profile_sha256=profile, namespace_N=len(rows),
                rows=rows, hashed_bytes=budget[0])


def plan(parent, *, now, calendar, source_profile_sha256, enabled=DEFAULT_ENABLED):
    _need(type(enabled) is bool, 'literal_boolean_required')
    if not enabled:
        return {'schema': SCHEMA, 'status': 'OFF', 'rows': []}
    with _lease(parent, True) as (directory, identity):
        return _scan(directory, identity, _clock(now), calendar, source_profile_sha256)


def approved_prune(parent, approved, *, approved_sha256, now, calendar,
                   source_profile_sha256, enabled=DEFAULT_ENABLED):
    """Source-only executor for an explicit reviewed plan; NOT a scheduler.

    Complete reader-hook/protection coverage must be independently reviewed and
    bound by the installed source profile before marking its catalog complete.
    Never run this against a cited cache or without the actual after-close card.
    """
    _need(type(enabled) is bool, 'literal_boolean_required')
    if not enabled:
        return {'schema': SCHEMA, 'status': 'OFF', 'deleted': []}
    _need(_digest(approved) == _sha(approved_sha256), 'explicit_exact_plan_approval_required')
    current = _clock(now)
    _need(current >= _stamp(approved['at']), 'clock_regression_HOLD')
    local = current.astimezone(ET)
    _need(not calendar.trading(local.date()) or local.time() >= time(20),
          'no_retention_mutation_during_trading_session')
    with _lease(parent, True) as (directory, identity):
        actual = _scan(directory, identity, _stamp(approved['at']), calendar, source_profile_sha256)
        _need(actual == approved and list(calendar.last_seven_completed(current)) ==
              [date.fromisoformat(d) for d in approved['retained_completed_days']], 'plan_or_clock_changed_HOLD')
        if not any(row['action'] == 'PRUNE_CANDIDATE' for row in approved['rows']):
            return {'schema': SCHEMA, 'status': 'NO_ELIGIBLE_NAMESPACES', 'deleted': []}
        # The full scan matches approval before the FIRST unlink; each child
        # also has an immediate identity CAS. No recursive or receipt deletion.
        try:
            os.mkdir('.retention-receipts', 0o700, dir_fd=directory)
        except FileExistsError:
            pass
        receipts = os.open('.retention-receipts', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                           dir_fd=directory)
        deleted = []
        failure = None
        try:
            st = os.fstat(receipts)
            _need(st.st_uid == os.geteuid() and stat.S_IMODE(st.st_mode) == 0o700, 'receipt_directory_private')
            fd = os.open(approved_sha256 + '.plan.json', os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                         0o600, dir_fd=receipts)
            with os.fdopen(fd, 'wb') as stream:
                stream.write(_encode(approved)); stream.flush(); os.fsync(stream.fileno())
            for row in approved['rows']:
                if row['action'] != 'PRUNE_CANDIDATE':
                    continue
                child = os.open(row['name'], os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
                child_lock = None
                try:
                    child_lock = os.open('.lock', os.O_RDWR | os.O_NOFOLLOW, dir_fd=child)
                    _regular(os.fstat(child_lock))
                    fcntl.flock(child_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    st = os.fstat(child)
                    _need([st.st_dev, st.st_ino] == row['directory_identity'], 'namespace_CAS_changed_HOLD')
                    for name, expected in row['files'].items():
                        _need(_identity(os.stat(name, dir_fd=child, follow_symlinks=False)) == expected['identity'],
                              'file_CAS_changed_HOLD')
                    for name in ('cache.json', '.retention.json', '.lock'):
                        os.unlink(name, dir_fd=child)
                    os.fsync(child)
                finally:
                    if child_lock is not None:
                        os.close(child_lock)
                    os.close(child)
                os.rmdir(row['name'], dir_fd=directory); deleted.append(row['name'])
            os.fsync(directory)
            return {'schema': SCHEMA, 'status': 'PRUNED_EXACT_APPROVED_VOLATILE_NAMESPACES',
                    'deleted': deleted, 'preserved_receipt': approved_sha256 + '.plan.json'}
        except BaseException as exc:
            failure = type(exc).__name__
            raise
        finally:
            try:
                result = dict(schema=SCHEMA, approved_plan_sha256=approved_sha256,
                              observed_at=current.isoformat(), deleted=deleted,
                              status='STOP_NO_RETRY_OR_ROLLBACK' if failure else 'COMPLETE',
                              failure_type=failure)
                fd = os.open(approved_sha256 + '.result.json', os.O_WRONLY | os.O_CREAT | os.O_EXCL
                             | os.O_NOFOLLOW, 0o600, dir_fd=receipts)
                with os.fdopen(fd, 'wb') as stream:
                    stream.write(_encode(result)); stream.flush(); os.fsync(stream.fileno())
                os.fsync(receipts)
            finally:
                os.close(receipts)
