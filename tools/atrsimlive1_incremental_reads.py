"""Exact input invalidation/read planning. No query, connection or model call.

These are CONTENT fingerprints, not SQL folding of volume/body/trading rules.
The executor must verify core sha256(bytea) exists; never create an extension.
An unavailable fingerprint path requires an explicit fresh read, not a hit.
"""
import hashlib

from atrsimlive1_incremental_cache import CacheRefusal, fingerprint, _sha
from atrsimlive1_working_windows import nanos


FINGERPRINT_SCHEMA = 'ATRSIMLIVE1_CURRENT_RR_CONTENT_FINGERPRINT_V1'
FIELDS = {
    'trades': ('id', 'provider', 'symbol', 'event_ts', 'received_at', 'price', 'size',
               'exchange', 'conditions', 'cumulative_volume'),
    'quotes': ('id', 'provider', 'symbol', 'event_ts', 'received_at', 'bid_price',
               'ask_price', 'bid_size', 'ask_size'),
}


def projection(kind):
    """Original literal rowset, shared without changing its SQL text/hash."""
    if kind == 'trades':
        table = 'market_capture_trades'
    elif kind == 'quotes':
        table = 'market_capture_quotes'
    else:
        raise CacheRefusal('literal_capture_kind_required')
    columns = ','.join(FIELDS[kind])
    projection = f"SELECT {columns} FROM {table} WHERE provider='massive' AND symbol=%(symbol)s AND event_ts >= %(start)s AND event_ts < %(end)s AND received_at <= %(asof)s"
    if kind == 'quotes':
        projection = f"""SELECT {columns} FROM market_capture_quotes
        WHERE provider='massive' AND symbol=%(symbol)s AND received_at<=%(asof)s
        AND event_ts>=%(day_start)s AND event_ts<%(end)s
        AND (event_ts>=%(start)s OR id=(SELECT id FROM market_capture_quotes
          WHERE provider='massive' AND symbol=%(symbol)s AND received_at<=%(asof)s
          AND event_ts>=%(day_start)s AND event_ts<%(start)s
          ORDER BY event_ts DESC,id DESC LIMIT 1))"""
    return projection


def query_pair(kind):
    """Identifiers are from literal source-owned sets; all values stay bound."""
    selected = projection(kind)
    # to_jsonb(r) includes EVERY original projected field, NULLs included.
    # The exact SQL/session timezone is part of the certificate contract.
    rows = f"""SELECT r.*,encode(sha256(convert_to(to_jsonb(r)::text,'UTF8')),'hex') AS incremental_row_sha256
      FROM ({selected}) r
      WHERE (%(after_ts)s::timestamptz IS NULL OR (event_ts,id) > (%(after_ts)s::timestamptz,%(after_id)s::bigint))
      ORDER BY event_ts,id LIMIT 5001"""
    content = f"""WITH raw AS ({selected}), hashed AS (
      SELECT id,event_ts,encode(sha256(convert_to(to_jsonb(raw)::text,'UTF8')),'hex') AS row_sha FROM raw)
      SELECT count(*)::bigint AS row_count,
      encode(sha256(convert_to(coalesce(string_agg(id::text||':'||row_sha||E'\\n','' ORDER BY event_ts,id),''),'UTF8')),'hex') AS content_sha256,
      current_setting('TimeZone') AS source_timezone FROM hashed"""
    return dict(rows=rows, content=content, fields=FIELDS[kind],
                query_sha256=fingerprint(dict(rows=rows, content=content)))


def certify(result, *, kind, symbol, start, end, asof, snapshot_id, query_sha256,
            read_only, isolation, complete, day_start, expected_timezone=None):
    if (kind not in FIELDS or read_only != 'on' or isolation != 'repeatable read'
            or complete is not True or not isinstance(snapshot_id, str) or not snapshot_id
            or type(result.get('row_count')) is not int or not 0 <= result['row_count'] <= 1000000
            or not isinstance(result.get('source_timezone'), str)
            or not result['source_timezone'] or not nanos(day_start) <= nanos(start) <= nanos(end) <= nanos(asof)):
        raise CacheRefusal('actual_complete_RR_fingerprint_required')
    expected = query_pair(kind)['query_sha256']
    if query_sha256 != expected:
        raise CacheRefusal('fingerprint_query_changed')
    if expected_timezone is not None and result['source_timezone'] != expected_timezone:
        raise CacheRefusal('fingerprint_session_timezone_changed')
    return dict(schema=FINGERPRINT_SCHEMA, kind=kind, symbol=symbol, start=start, end=end, day_start=day_start,
        asof=asof, snapshot_id=snapshot_id, row_count=result['row_count'],
        content_sha256=_sha(result.get('content_sha256')), source_timezone=result['source_timezone'],
        query_sha256=expected, complete=True)


def _same_scope(old, current):
    return all(old.get(key) == current.get(key) for key in (
        'schema', 'kind', 'symbol', 'start', 'end', 'day_start', 'source_timezone', 'query_sha256'))


def read_plan(old_prefix, current_prefix, *, current_asof, current_snapshot, open_window):
    if (current_prefix.get('schema') != FINGERPRINT_SCHEMA or current_prefix.get('complete') is not True
            or current_prefix.get('snapshot_id') != current_snapshot
            or nanos(current_prefix.get('asof')) != nanos(current_asof)):
        raise CacheRefusal('current_snapshot_fingerprint_required')
    _sha(current_prefix.get('content_sha256'))
    if (old_prefix is None or not _same_scope(old_prefix, current_prefix)
            or old_prefix.get('complete') is not True
            or old_prefix.get('row_count') != current_prefix['row_count']
            or old_prefix.get('content_sha256') != current_prefix['content_sha256']):
        return dict(action='FRESH_WINDOW', reason='PREFIX_NEW_LATE_CORRECTED_OR_REMOVED')
    if nanos(old_prefix['asof']) > nanos(current_asof):
        raise CacheRefusal('future_cache_prefix_refused')
    if open_window:
        # Original interval was < end; new query is >= end, so the boundary
        # timestamp belongs wholly to delta. Earlier late arrivals invalidate
        # the prefix above; nothing is repaired or backdated.
        return dict(action='DELTA', start=current_prefix['end'], end=current_asof,
                    reason='EXACT_PREFIX_REVERIFIED_CURRENT_RR')
    return dict(action='RETAIN_CLOSED_INPUT', reason='EXACT_PREFIX_REVERIFIED_CURRENT_RR')


def rows_with_fingerprints(rows, *, kind):
    raw, hashes = [], {}
    for item in rows:
        if set(item) != set(FIELDS[kind]) | {'incremental_row_sha256'}:
            raise CacheRefusal('exact_capture_projection_required')
        row = {field: item[field] for field in FIELDS[kind]}
        key = row['symbol'], row['id']
        if key in hashes:
            raise CacheRefusal('duplicate_physical_capture_row')
        hashes[key] = _sha(item['incremental_row_sha256'])
        raw.append(row)
    return raw, hashes


def content_hash(rows, hashes):
    value = hashlib.sha256()
    for row in sorted(rows, key=lambda r: (nanos(r['event_ts']), r['id'])):
        value.update((str(row['id']) + ':' + _sha(hashes[row['symbol'], row['id']]) + '\n').encode('ascii'))
    return value.hexdigest()


def merge_open(*, prefix_rows, prefix_hashes, delta_rows, delta_hashes, cutoff,
               current_full_certificate, current_asof, current_snapshot):
    """Caller supplies a certified unchanged prefix and complete current delta.

    All actual row fields remain unchanged. A correction/conflicting duplicate
    returns refusal so the executor can explicitly perform a fresh window read.
    """
    cert = current_full_certificate
    if (cert.get('schema') != FINGERPRINT_SCHEMA or cert.get('complete') is not True
            or cert.get('snapshot_id') != current_snapshot or nanos(cert['asof']) != nanos(current_asof)):
        raise CacheRefusal('current_full_window_fingerprint_required')
    kind, symbol = cert['kind'], cert['symbol']
    known, hashes = {}, {}
    for old, rows, row_hashes in ((True, prefix_rows, prefix_hashes), (False, delta_rows, delta_hashes)):
        for row in rows:
            at = nanos(row['event_ts'])
            prior_quote = kind == 'quotes' and at < nanos(cutoff)
            if (set(row) != set(FIELDS[kind]) or row['symbol'] != symbol or row['provider'] != 'massive'
                    or nanos(row['received_at']) > nanos(current_asof)
                    or at >= nanos(cert['end']) or at < nanos(cert['day_start'])
                    or kind == 'trades' and at < nanos(cert['start'])
                    or old and at >= nanos(cutoff) or not old and at < nanos(cutoff) and not prior_quote):
                raise CacheRefusal('exact_prefix_delta_scope_required')
            key = symbol, row['id']
            checksum = _sha(row_hashes[key])
            if key in known and (known[key] != row or hashes[key] != checksum):
                raise CacheRefusal('conflicting_physical_capture_row')
            known[key], hashes[key] = row, checksum
    merged = sorted(known.values(), key=lambda r: (nanos(r['event_ts']), r['id']))
    if kind == 'quotes' and sum(nanos(row['event_ts']) < nanos(cert['start']) for row in merged) > 1:
        raise CacheRefusal('single_actual_quote_carry_required')
    if len(merged) != cert['row_count'] or content_hash(merged, hashes) != cert['content_sha256']:
        raise CacheRefusal('merged_current_window_content_mismatch')
    return merged, hashes


def proof_bindings(identities, *, windows, cohort_dependencies, policies, source_seal,
                   current_asof, current_snapshot):
    """Stable proof hashes after current-RR validation; current witness separate."""
    _sha(source_seal)
    if len(identities) != len(windows) or not identities:
        raise CacheRefusal('complete_ordered_cohort_required')
    current_certificates, window_hashes = [], []
    for identity, window in zip(identities, windows):
        certs = window['certificates']
        if (set(certs) != {'trades', 'quotes'} or window.get('identity') != identity
                or not {'bars', 'capture_bars', 'scanner', 'fills', 'route', 'coverage', 'oracle', 'membership'} <= set(window['header_hashes'])):
            raise CacheRefusal('complete_actual_window_dependencies_required')
        stable = {}
        for kind, cert in certs.items():
            if (cert.get('schema') != FINGERPRINT_SCHEMA or cert.get('complete') is not True
                    or cert.get('snapshot_id') != current_snapshot or nanos(cert['asof']) != nanos(current_asof)
                    or cert.get('kind') != kind or cert.get('symbol') != identity['symbol']):
                raise CacheRefusal('cohort_current_RR_certificate_required')
            _sha(cert['content_sha256'])
            if cert.get('query_sha256') != query_pair(kind)['query_sha256']:
                raise CacheRefusal('cohort_fingerprint_query_changed')
            current_certificates.append(cert)
            stable[kind] = {key: cert[key] for key in ('kind', 'symbol', 'start', 'end', 'day_start',
                'row_count', 'content_sha256', 'source_timezone', 'query_sha256')}
        for checksum in window['header_hashes'].values():
            _sha(checksum)
        window_hashes.append(fingerprint(dict(certificates=stable, headers=window['header_hashes'])))
    cohort = fingerprint(dict(identities=identities, window_hashes=window_hashes,
                              dependencies=cohort_dependencies, policies=policies, source_seal=source_seal))
    bindings = tuple(fingerprint(dict(identity=identity, window_sha256=_sha(window),
        cohort_sha256=cohort, policies=policies, source_seal=source_seal))
        for identity, window in zip(identities, window_hashes))
    return dict(bindings=bindings, cohort_sha256=cohort, window_hashes=tuple(window_hashes),
                current_verification=dict(snapshot_id=current_snapshot, asof=current_asof,
                    certificates_sha256=fingerprint(current_certificates)))
