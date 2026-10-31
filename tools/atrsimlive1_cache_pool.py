"""Opt-in lossless raw-row pool; no execution or coverage permissions.

The caller must bind this format and opt-in to a NEW cache namespace. It is
not an alternate interpretation of an existing V2 cache artifact.
"""
from datetime import datetime
from decimal import Decimal
import math
import re
from uuid import UUID

from atrsimlive1_incremental_cache import CacheRefusal, MAX_CACHE_BYTES, _sha, chunks
from atrsimlive1_incremental_reads import FIELDS


POOL_V1 = 'ATRSIMLIVE1_LOSSLESS_RAW_POOL_V1'


def _flag(enabled):
    if type(enabled) is not bool:
        raise CacheRefusal('explicit_pool_boolean_required')


def _copy(value, depth=0):
    if depth > 96:
        raise CacheRefusal('cache_nesting_cap')
    if isinstance(value, dict):
        if any(type(key) is not str for key in value):
            raise CacheRefusal('raw_pool_string_keys_required')
        return {key: _copy(item, depth + 1) for key, item in value.items()}
    if isinstance(value, list):
        return [_copy(item, depth + 1) for item in value]
    if isinstance(value, tuple):
        return tuple(_copy(item, depth + 1) for item in value)
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise CacheRefusal('finite_cache_decimal_required')
    elif isinstance(value, datetime):
        if value.utcoffset() is None:
            raise CacheRefusal('aware_cache_clock_required')
    elif type(value) is float:
        if not math.isfinite(value):
            raise CacheRefusal('nonfinite_cache_number')
    elif value is not None and type(value) not in (str, int, bool, UUID):
        raise CacheRefusal('unsupported_raw_pool_value')
    return value


def _same(left, right):
    # Python numeric equality alone erases Decimal scale and bool/int type.
    # Container SectionMapping is a verified dict subclass, not a new raw
    # scalar type. Its public mapping methods verify/materialize before read.
    if isinstance(left, dict) and isinstance(right, dict):
        return left.keys() == right.keys() and all(_same(left[key], right[key]) for key in left)
    if type(left) is not type(right):
        return False
    if isinstance(left, Decimal):
        return left.as_tuple() == right.as_tuple()
    if isinstance(left, datetime):
        return left.isoformat() == right.isoformat()
    if isinstance(left, (list, tuple)):
        return len(left) == len(right) and all(_same(a, b) for a, b in zip(left, right))
    return left == right


def _partition(kind, symbol):
    if kind not in FIELDS or type(symbol) is not str or re.fullmatch(r'[A-Z][A-Z0-9.\-]{0,31}', symbol) is None:
        raise CacheRefusal('literal_raw_pool_partition_required')
    return kind + '|' + symbol


def pack_windows(body, *, enabled=False):
    """OFF returns the exact object; ON stores identical physical rows once.

    No input hash is replaced by a different SQL/Python hash, and duplicate
    identities WITHIN one original window remain refusals, not a dedup repair.
    """
    _flag(enabled)
    if not enabled:
        return body
    if not isinstance(body, dict) or not isinstance(body.get('windows'), dict):
        raise CacheRefusal('raw_pool_window_population_required')
    result = {key: value for key, value in body.items() if key != 'windows'}
    result['windows'] = {}
    pools, indexes = {}, {}
    for symbol in sorted(body['windows']):
        windows = body['windows'][symbol]
        target = result['windows'][symbol] = {}
        for origin in sorted(windows):
            window = windows[origin]
            target[origin] = {key: value for key, value in window.items() if key not in FIELDS}
            for kind in FIELDS:
                name = _partition(kind, symbol)
                pool = pools.setdefault(name, dict(rows=[], hashes={}))
                indexed = indexes.setdefault(name, {})
                value = window[kind]
                if set(value) != {'rows', 'hashes', 'certificate'}:
                    raise CacheRefusal('exact_original_raw_window_fields_required')
                references, seen = [], set()
                for row in value['rows']:
                    if (set(row) != set(FIELDS[kind]) or row['symbol'] != symbol
                            or row['provider'] != 'massive' or type(row['id']) is not int or row['id'] <= 0
                            or row['id'] in seen):
                        raise CacheRefusal('raw_pool_exact_physical_identity_required')
                    seen.add(row['id'])
                    checksum = _sha(value['hashes'][str(row['id'])])
                    prior = indexed.get(row['id'])
                    if prior is None:
                        if len(pool['rows']) >= 1000000:
                            raise CacheRefusal('today_symbol_capture_cap')
                        prior = indexed[row['id']] = len(pool['rows'])
                        pool['rows'].append(_copy(row))
                        pool['hashes'][str(row['id'])] = checksum
                    elif (pool['hashes'][str(row['id'])] != checksum
                          or not _same(pool['rows'][prior], row)):
                        raise CacheRefusal('raw_pool_conflicting_physical_row')
                    references.append(prior)
                if set(value['hashes']) != {str(key) for key in seen}:
                    raise CacheRefusal('raw_pool_exact_window_hash_population_required')
                target[origin][kind] = dict(pool=name, row_refs=references,
                                            certificate=_copy(value['certificate']))
    return dict(schema=POOL_V1, body=result, pools=pools)


def unpack_windows(document, *, enabled=False):
    _flag(enabled)
    if not enabled:
        if isinstance(document, dict) and document.get('schema') == POOL_V1:
            raise CacheRefusal('pooled_cache_requires_explicit_profile')
        return document
    if (not isinstance(document, dict) or set(document) != {'schema', 'body', 'pools'}
            or document['schema'] != POOL_V1):
        raise CacheRefusal('exact_raw_pool_format_required')
    source, pools = document['body'], document['pools']
    result = {key: value for key, value in source.items() if key != 'windows'}
    result['windows'], used, canonical, expected_names = {}, set(), {}, set()
    expanded_bytes, row_sizes = 0, {}
    for symbol in sorted(source['windows']):
        windows = source['windows'][symbol]
        target = result['windows'][symbol] = {}
        for origin in sorted(windows):
            window = windows[origin]
            target[origin] = {key: value for key, value in window.items() if key not in FIELDS}
            for kind in FIELDS:
                value = window[kind]
                if set(value) != {'pool', 'row_refs', 'certificate'} or value['pool'] != _partition(kind, symbol):
                    raise CacheRefusal('raw_pool_foreign_window_reference')
                pool = pools[value['pool']]
                expected_names.add(value['pool'])
                assigned = canonical.setdefault(value['pool'], {})
                if set(pool) != {'rows', 'hashes'}:
                    raise CacheRefusal('exact_raw_pool_partition_fields_required')
                refs = value['row_refs']
                if (not isinstance(refs, list)
                        or any(type(ref) is not int or not 0 <= ref < len(pool['rows']) for ref in refs)
                        or len(refs) != len(set(refs))):
                    raise CacheRefusal('raw_pool_reference_bounds_or_duplicate')
                rows, hashes = [], {}
                for ref in refs:
                    row = pool['rows'][ref]
                    if (set(row) != set(FIELDS[kind]) or row['symbol'] != symbol or row['provider'] != 'massive'
                            or type(row['id']) is not int or row['id'] <= 0 or str(row['id']) in hashes):
                        raise CacheRefusal('raw_pool_decoded_identity_refused')
                    if row['id'] not in assigned:
                        if ref != len(assigned):
                            raise CacheRefusal('raw_pool_noncanonical_or_conflicting_representation')
                        assigned[row['id']] = ref
                    elif assigned[row['id']] != ref:
                        raise CacheRefusal('raw_pool_noncanonical_or_conflicting_representation')
                    size_key = value['pool'], ref
                    if size_key not in row_sizes:
                        row_sizes[size_key] = sum(len(part.encode('utf-8')) for part in chunks(row))
                    # Existing whole-cache cap, also applied before duplicating
                    # pooled rows in memory. No compressed/reference bomb.
                    expanded_bytes += row_sizes[size_key] + len(str(row['id'])) + 70
                    if expanded_bytes > MAX_CACHE_BYTES:
                        raise CacheRefusal('raw_pool_expanded_cache_byte_cap')
                    rows.append(_copy(row))
                    hashes[str(row['id'])] = _sha(pool['hashes'][str(row['id'])])
                    used.add((value['pool'], ref))
                target[origin][kind] = dict(rows=rows, hashes=hashes, certificate=_copy(value['certificate']))
    expected = {(name, index) for name, pool in pools.items() for index in range(len(pool['rows']))}
    if used != expected:
        raise CacheRefusal('raw_pool_unreferenced_or_missing_rows')
    if set(pools) != expected_names:
        raise CacheRefusal('raw_pool_unreferenced_or_missing_rows')
    for name, pool in pools.items():
        if set(pool['hashes']) != {str(identity) for identity in canonical[name]}:
            raise CacheRefusal('raw_pool_exact_window_hash_population_required')
    # Canonical first-use references above establish the same row/identity
    # relation as pack_windows, without allocating a second full pool/copy.
    # This is not a measured RSS/CPU improvement or a native completeness fact.
    return result
