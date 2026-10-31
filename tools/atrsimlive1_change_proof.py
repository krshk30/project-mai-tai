"""Since-last-observation candidate SQL; no absence/immutability permission.

An empty received_at suffix cannot detect UPDATE/DELETE/backdated arrival.
Current complete RR content fingerprints remain the required change proof.
"""
from atrsimlive1_incremental_cache import CacheRefusal, fingerprint, _sha
from atrsimlive1_incremental_reads import FIELDS, read_plan, projection, content_hash, FINGERPRINT_SCHEMA, query_pair
from atrsimlive1_working_windows import nanos


CHANGE_V1 = 'ATRSIMLIVE1_SINCE_OBSERVATION_CANDIDATES_V1'
TABLES = dict(trades='market_capture_trades', quotes='market_capture_quotes')


def candidate_query(kind, *, symbol, previous_asof, current_asof, start, end,
                    day_start, after_ts=None, after_id=None):
    if (kind not in TABLES or type(symbol) is not str or not symbol
            or not nanos(day_start) <= nanos(start) <= nanos(end) <= nanos(current_asof)
            or nanos(previous_asof) > nanos(current_asof)
            or (after_ts is None) != (after_id is None)
            or (after_id is not None and (type(after_id) is not int or after_id <= 0))):
        raise CacheRefusal('exact_since_observation_bounds_required')
    if after_ts is not None:
        nanos(after_ts)
    # Identifiers are from literal source lists, all data values are bound.
    # Quote discovery includes newly observed pre-window rows: one can change
    # the actual carry-in. The original full content query still decides that.
    sql = f"""SELECT r.*,encode(sha256(convert_to(to_jsonb(r)::text,'UTF8')),'hex') AS incremental_row_sha256
      FROM ({projection(kind)}) r
      WHERE received_at > %(previous_asof)s AND received_at <= %(asof)s
      AND (%(after_ts)s::timestamptz IS NULL
           OR (event_ts,id) > (%(after_ts)s::timestamptz,%(after_id)s::bigint))
      ORDER BY event_ts,id LIMIT 5001"""
    return dict(schema=CHANGE_V1, role='DISCOVERY_ONLY_NOT_UNCHANGED_OR_COMPLETE_PROOF',
        clock='stored received_at observation; native arrival UNKNOWN', sql=sql,
        query_sha256=fingerprint(sql), params=dict(symbol=symbol, previous_asof=previous_asof,
            asof=current_asof, start=start, end=end, day_start=day_start,
            after_ts=after_ts, after_id=after_id))


def plan_window(previous, current, *, current_asof, current_snapshot, open_window,
                enabled=False, discovery_row_count=None):
    if type(enabled) is not bool:
        raise CacheRefusal('explicit_change_proof_boolean_required')
    if not enabled:
        return dict(action='ORIGINAL_FULL_PREFIX_PROOF', discovery_enabled=False)
    if discovery_row_count is not None and (type(discovery_row_count) is not int or discovery_row_count < 0):
        raise CacheRefusal('actual_discovery_count_or_UNKNOWN_required')
    # Do NOT skip this for an empty discovery result. This is the actual strong
    # current-RR proof, covering corrected/deleted/backdated prefix rows too.
    action = read_plan(previous, current, current_asof=current_asof,
                       current_snapshot=current_snapshot, open_window=open_window)
    return dict(action, discovery_enabled=discovery_row_count is not None,
                discovery_status='UNEXERCISED' if discovery_row_count is None else 'ACTUAL_COUNT_SUPPLIED',
                discovery_row_count=discovery_row_count,
                authoritative_proof='CURRENT_RR_COMPLETE_CONTENT_FINGERPRINT',
                absence_from_suffix_is_not_proof=True)


def merge_since(previous, candidates, candidate_hashes, *, current_full, current_asof, current_snapshot):
    """A suffix is an optimization candidate; full RR content decides success.

    Current rows can replace a physical ID only from the actual newly observed
    rowset. An unobserved correction/delete cannot vanish: the exact full count
    and ordered SQL row-hash digest will fail, requiring the original full read.
    """
    old, cert = previous['certificate'], current_full
    if (cert.get('schema') != FINGERPRINT_SCHEMA or cert.get('complete') is not True
            or cert.get('snapshot_id') != current_snapshot or nanos(cert['asof']) != nanos(current_asof)
            or old.get('schema') != FINGERPRINT_SCHEMA or old.get('complete') is not True
            or any(old.get(key) != cert.get(key) for key in ('kind', 'symbol', 'start', 'day_start', 'source_timezone', 'query_sha256'))
            or nanos(old['end']) > nanos(cert['end']) or nanos(old['asof']) > nanos(current_asof)
            or cert['query_sha256'] != query_pair(cert['kind'])['query_sha256']):
        raise CacheRefusal('same_scope_actual_current_RR_since_merge_required')
    kind, symbol = cert['kind'], cert['symbol']
    prior_hashes = {(symbol, int(key)): _sha(value) for key, value in previous['hashes'].items()}
    if (len(previous['rows']) != old['row_count'] or len(prior_hashes) != len(previous['rows'])
            or content_hash(previous['rows'], prior_hashes) != old['content_sha256']):
        raise CacheRefusal('original_cached_raw_population_must_match_its_certificate')
    assembled, hashes, seen = {}, dict(prior_hashes), set()
    for later, rows in ((False, previous['rows']), (True, candidates)):
        for row in rows:
            event, received = nanos(row['event_ts']), nanos(row['received_at'])
            key = symbol, row['id']
            if (set(row) != set(FIELDS[kind]) or row['symbol'] != symbol or row['provider'] != 'massive'
                    or type(row['id']) is not int or row['id'] <= 0
                    or not nanos(cert['day_start']) <= event < nanos(cert['end'])
                    or kind == 'trades' and event < nanos(cert['start'])
                    or not later and event >= nanos(old['end'])
                    or received > nanos(current_asof) or not later and received > nanos(old['asof'])
                    or later and received <= nanos(old['asof']) or later and key in seen
                    or not later and key in assembled):
                raise CacheRefusal('exact_actual_since_row_scope_and_identity_required')
            assembled[key] = row
            if later:
                seen.add(key)
                hashes[key] = _sha(candidate_hashes[key])
            if len(assembled) > 1000000:
                return dict(action='FRESH_WINDOW', reason='SINCE_MERGE_POPULATION_REQUIRES_FULL_READ')
    if set(candidate_hashes) != seen:
        raise CacheRefusal('exact_since_SQL_row_hash_population_required')
    if kind == 'quotes':
        carry = [key for key, row in assembled.items() if nanos(row['event_ts']) < nanos(cert['start'])]
        keep = max(carry, key=lambda key: (nanos(assembled[key]['event_ts']), key[1]), default=None)
        for key in carry:
            if key != keep:
                del assembled[key], hashes[key]
    ordered = sorted(assembled.values(), key=lambda row: (nanos(row['event_ts']), row['id']))
    if (len(ordered) != cert['row_count'] or content_hash(ordered, hashes) != cert['content_sha256']):
        return dict(action='FRESH_WINDOW', reason='SINCE_MERGE_FULL_CERTIFICATE_MISMATCH')
    return dict(action='SINCE_MERGE_EXACT_CURRENT_FULL_CERTIFICATE', rows=ordered, hashes=hashes,
        since_rows=len(candidates), authoritative_proof='CURRENT_RR_COMPLETE_CONTENT_FINGERPRINT')
