"""Private, bounded volatile cache contracts; no model/native permissions.

Explicit opt-in only. JSON carries typed stored scalars, never executable
objects. A cache hit requires current dependency proof, not elapsed time.
"""
from contextlib import contextmanager
from datetime import datetime
from decimal import Decimal
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
from uuid import UUID


INCREMENTAL_V1 = 'C1_20261028_200908_ATRSIM_INCREMENTAL_V1'
FEATURE_PROFILE = 'C1_20261029_CLOSED_COMPONENT_CACHE_V1'
FEATURE_NAMES = ('closed_components', 'raw_pool', 'since_last_run')
SCHEMA = 'ATRSIMLIVE1_PRIVATE_INCREMENTAL_CACHE_V1'
MAX_CACHE_BYTES = 512 * 1024 * 1024
TYPE = '__atrsim_incremental_native_v1__'
IDENTITY = ('trading_day', 'symbol', 'broker', 'account_id', 'strategy', 'flip_id', 'segment_id')
WORLDS = ('FIRSTBAR', 'FIRSTBAR_X3', 'AGG_ENTRY', 'TODAY:SUPPLIED',
          'VOLBODY:QUEUE_AWARE', 'VOLBODY:OPTIMISTIC')


class CacheRefusal(ValueError):
    """Literal internal codes only; caller may explicitly recompute fresh."""


def features(binding):
    """No new semantics in old namespaces; every opt-in changes their name."""
    value = binding.get('features')
    if 'features' not in binding:
        return {name: False for name in FEATURE_NAMES}
    if (not isinstance(value, dict) or set(value) != {'version', *FEATURE_NAMES}
            or value['version'] != FEATURE_PROFILE
            or any(type(value[name]) is not bool for name in FEATURE_NAMES)):
        raise CacheRefusal('exact_reviewed_cache_feature_profile_required')
    return {name: value[name] for name in FEATURE_NAMES}


def require_version(version):
    if version is not None and version != INCREMENTAL_V1:
        raise CacheRefusal('unsupported_incremental_version')
    return version


def _sha(value):
    if not isinstance(value, str) or re.fullmatch('[0-9a-f]{64}', value) is None:
        raise CacheRefusal('exact_sha256_required')
    return value


def _native(value):
    if isinstance(value, datetime):
        if value.utcoffset() is None:
            raise CacheRefusal('aware_cache_clock_required')
        return {TYPE: 'datetime', 'value': value.isoformat()}
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise CacheRefusal('finite_cache_decimal_required')
        return {TYPE: 'decimal', 'value': str(value)}
    if isinstance(value, UUID):
        return {TYPE: 'uuid', 'value': str(value)}
    raise CacheRefusal('unsupported_cache_scalar')


def _validate_values(value, depth=0):
    if depth > 96:
        raise CacheRefusal('cache_nesting_cap')
    if isinstance(value, dict):
        if TYPE in value or any(not isinstance(key, str) for key in value):
            raise CacheRefusal('reserved_or_nonstring_cache_key')
        for item in value.values():
            _validate_values(item, depth + 1)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _validate_values(item, depth + 1)


def chunks(value):
    return json.JSONEncoder(sort_keys=True, separators=(',', ':'), ensure_ascii=False,
                            allow_nan=False, default=_native).iterencode(value)


def fingerprint(value):
    _validate_values(value)
    result = hashlib.sha256()
    for chunk in chunks(value):
        result.update(chunk.encode('utf-8'))
    return result.hexdigest()


def _pairs(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise CacheRefusal('duplicate_cache_key')
        value[key] = item
    if TYPE not in value:
        return value
    if set(value) != {TYPE, 'value'} or not isinstance(value['value'], str):
        raise CacheRefusal('invalid_cache_scalar_tag')
    kind, raw = value[TYPE], value['value']
    if kind == 'datetime':
        result = datetime.fromisoformat(raw)
        if result.utcoffset() is None:
            raise CacheRefusal('aware_cache_clock_required')
        return result
    if kind == 'decimal':
        result = Decimal(raw)
        if not result.is_finite():
            raise CacheRefusal('finite_cache_decimal_required')
        return result
    if kind == 'uuid':
        return UUID(raw)
    raise CacheRefusal('unknown_cache_scalar_tag')


def _constant(_):
    raise CacheRefusal('nonfinite_cache_number')


def cache_key(*, identity, world, window_sha256, cohort_sha256, policies, source_seal):
    if (set(identity) != set(IDENTITY)
            or any(not isinstance(identity[key], str) or not identity[key] for key in IDENTITY)
            or world not in WORLDS):
        raise CacheRefusal('actual_episode_world_identity_required')
    if (not isinstance(policies, dict)
            or not {'execution_rules_sha256', 'null_policy', 'same_sip_policy'} <= set(policies)):
        raise CacheRefusal('complete_actual_policy_binding_required')
    _sha(policies['execution_rules_sha256'])
    return fingerprint(dict(version=INCREMENTAL_V1, identity=identity, world=world,
        window_sha256=_sha(window_sha256), cohort_sha256=_sha(cohort_sha256),
        policies=policies, source_seal=_sha(source_seal)))


def finished_known(receipt, canonical_row):
    """Conservative admission; unknown/zero-fill/mixed cases recompute fresh."""
    if (receipt.get('issues') or canonical_row.get('pnl_label_known') is not True
            or canonical_row.get('pnl_pct_label_known') is not True):
        return False
    fills, exits = receipt.get('fills', []), receipt.get('exits', [])
    if not fills or len(exits) != 1 or exits[0].get('reason') in (None, 'OPEN'):
        return False
    quantities = [item.get('quantity') for item in (*fills, *exits)]
    if any(type(qty) is not int or qty <= 0 for qty in quantities):
        return False
    if sum(item['quantity'] for item in fills) != exits[0]['quantity']:
        return False
    return receipt.get('gross_pnl', receipt.get('modeled_gross_pnl')) is not None


def choose(entry, *, expected_key, current_dependency_sha256, source_seal):
    """Never changes a receipt, suppresses UNKNOWN, or retags its original asof."""
    _sha(expected_key)
    _sha(current_dependency_sha256)
    _sha(source_seal)
    if entry is None:
        return dict(action='FRESH', reason='NO_ENTRY')
    if (entry.get('key') != expected_key or entry.get('source_seal') != source_seal
            or entry.get('dependency_sha256') != current_dependency_sha256):
        return dict(action='FRESH', reason='DEPENDENCY_CHANGED')
    if not finished_known(entry['receipt'], entry['row']):
        return dict(action='FRESH', reason='UNKNOWN_OPEN_OR_MIXED')
    if (entry.get('receipt_sha256') != fingerprint(entry['receipt'])
            or entry.get('row_sha256') != fingerprint(entry['row'])
            or not entry.get('original_provenance')):
        raise CacheRefusal('cached_receipt_integrity_or_provenance_refused')
    return dict(action='REUSE_FINISHED', reason='CURRENT_DEPENDENCIES_EXACT', entry=entry)


def _regular(info):
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
            or info.st_mode & 0o077 or info.st_nlink != 1):
        raise CacheRefusal('private_owned_regular_cache_required')


def _identity(info):
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


class CacheStore:
    def __init__(self, root, binding, *, retention_enabled=False):
        if type(retention_enabled) is not bool:
            raise CacheRefusal('explicit_retention_reader_boolean_required')
        root = Path(root)
        if (not root.is_absolute() or '..' in root.parts
                or any(part.is_symlink() for part in (root, *root.parents))):
            raise CacheRefusal('literal_private_cache_root_required')
        info = root.stat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
            raise CacheRefusal('owned_0700_cache_root_required')
        if (set(binding) not in ({'version', 'trading_day', 'source_seal', 'policies', 'input_scope'},
                                {'version', 'trading_day', 'source_seal', 'policies', 'input_scope', 'features'})
                or binding.get('input_scope') not in ('CURRENT_RR', 'RETAINED_FILE')
                or binding.get('version') != INCREMENTAL_V1 or not binding.get('trading_day')
                or not isinstance(binding.get('policies'), dict)
                or set(binding['policies']) != {'execution_rules_sha256', 'null_policy', 'same_sip_policy'}):
            raise CacheRefusal('external_cache_binding_required')
        selected_features = features(binding)
        if any(selected_features.values()) and binding['input_scope'] != 'CURRENT_RR':
            raise CacheRefusal('new_cache_features_require_actual_CURRENT_RR_not_FILE')
        _sha(binding.get('source_seal'))
        self.root, self.binding = root, binding
        self.root_identity = info.st_dev, info.st_ino
        self.retention_enabled = retention_enabled

    @contextmanager
    def directory(self):
        # Main C2 owns the optional module. OFF neither imports it nor opens a
        # parent lock. ON failures propagate: no silently unleased reader.
        with self._parent_reader_lease():
            with self._directory_fd() as fd:
                yield fd

    @contextmanager
    def _parent_reader_lease(self):
        if not self.retention_enabled:
            yield
            return
        from atrsimlive1_namespace_retention import cache_parent, shared_parent_lease

        with shared_parent_lease(cache_parent(self.root), enabled=True):
            yield

    @contextmanager
    def _directory_fd(self):
        fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            info = os.fstat(fd)
            if ((info.st_dev, info.st_ino) != self.root_identity or info.st_uid != os.geteuid()
                    or info.st_mode & 0o077):
                raise CacheRefusal('private_cache_root_changed')
            yield fd
        finally:
            os.close(fd)

    def load(self):
        from atrsimlive1_incremental_container import load
        body = load(self)
        if body is None:
            return None
        if features(self.binding)['raw_pool']:
            from atrsimlive1_cache_pool import POOL_V1, unpack_windows

            if body.get('raw_pool_schema') != POOL_V1 or 'raw_pools' not in body:
                raise CacheRefusal('explicit_pooled_namespace_format_required')
            return unpack_windows(dict(schema=POOL_V1,
                body={key: value for key, value in body.items() if key not in ('raw_pool_schema', 'raw_pools')},
                pools=body['raw_pools']), enabled=True)
        if 'raw_pool_schema' in body or 'raw_pools' in body:
            raise CacheRefusal('pooled_cache_requires_explicit_profile')
        return body

    def save(self, body):
        from atrsimlive1_incremental_container import save
        if features(self.binding)['raw_pool']:
            from atrsimlive1_cache_pool import pack_windows

            packed = pack_windows(body, enabled=True)
            body = dict(packed['body'], raw_pool_schema=packed['schema'], raw_pools=packed['pools'])
        return save(self, body)


@contextmanager
def locked_cache(root, binding, *, retention_enabled=False):
    store = CacheStore(root, binding, retention_enabled=retention_enabled)
    # Hold through lazy section use, model admission and save, not just open().
    with store._parent_reader_lease(), store.directory() as directory:
        fd = os.open('.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600, dir_fd=directory)
        try:
            _regular(os.fstat(fd))
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            yield store
        finally:
            os.close(fd)
