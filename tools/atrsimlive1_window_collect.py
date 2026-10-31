"""Bounded SAME-RR window collection, called only inside the existing collector.

No connection, transaction, provider or output writer of its own. All queries
use the original fetch/budget owner; all metadata counts cover the full roster.
"""
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path

from atrsimlive1_collect import digest
from atrsimlive1_working_windows import (plan, terminal, selection, nanos,
    TRADE_WINDOW_PREDICATE)
from atrsimlive1_window_census import (MINUTE_CENSUS_SQL, CONDITION_GROUPS_SQL,
    QUOTE_COUNTS_SQL, normalize_census, normalize_conditions)


SCHEMA = 'ATRSIMLIVE1_SQL_WINDOW_VIEW_V1'
POLICY_SHA = 'e42e5075bd296144b2a632438de01be1c7f1afe8ebaed34cd528d58f34e39cf3'
EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
CARRY_SQL = """SELECT id,provider,symbol,event_ts,received_at,
 bid_price,ask_price,bid_size,ask_size FROM market_capture_quotes
 WHERE provider='massive' AND symbol=%(symbol)s AND received_at<=%(asof)s
 AND event_ts>=%(start)s AND event_ts<%(window_start)s
 ORDER BY event_ts DESC,id DESC LIMIT 1"""


def payload_sql(original):
    sql = original.replace("provider = 'massive'\n      AND symbol = %(symbol)s AND event_ts >= %(start)s AND event_ts < %(end)s\n      AND received_at <= %(asof)s", TRADE_WINDOW_PREDICATE)
    if sql == original:
        raise ValueError('literal_capture_predicate_not_found')
    return sql


def query_binding(queries):
    return digest(dict(headers=queries.SOURCES, census=MINUTE_CENSUS_SQL,
        conditions=CONDITION_GROUPS_SQL, quote_counts=QUOTE_COUNTS_SQL,
        terminal=queries.CAPTURE_PAGES['trades'], carry=CARRY_SQL,
        windows={key: payload_sql(value) for key, value in queries.CAPTURE_PAGES.items()}))


def clock(ns):
    if ns % 1000:
        raise ValueError('stored_SQL_microsecond_clock_required')
    return EPOCH + timedelta(microseconds=ns // 1000)


def policy_input(path, *, snapshot, asof):
    from project_mai_tai.atr_simulation.types import SourceRef, EvidenceKind
    from project_mai_tai.atr_simulation.execution_eligibility import policy_from_payload

    raw = Path(path).read_bytes()
    if len(raw) > 2 * 1024 * 1024 or hashlib.sha256(raw).hexdigest() != POLICY_SHA:
        raise ValueError('retained_execution_policy_hash_mismatch')
    policy = policy_from_payload(json.loads(raw), SourceRef(str(path), POLICY_SHA, '$', EvidenceKind.RETAINED, None))
    source = SourceRef('postgres:readonly-snapshot:' + snapshot, POLICY_SHA,
                       'TERMINAL_SELECTION_NOT_EXECUTION', EvidenceKind.RETAINED, nanos(asof))
    return policy, source


def metadata(fetch, params):
    census = normalize_census(fetch(MINUTE_CENSUS_SQL, params, 'WINDOW_CENSUS'))
    conditions = normalize_conditions(fetch(CONDITION_GROUPS_SQL, params, 'CONDITION_CENSUS'))
    quotes = fetch(QUOTE_COUNTS_SQL, params, 'QUOTE_CENSUS')
    if len(census) > 100000 or len(conditions) > 100000 or len(quotes) > 256:
        raise ValueError('window_metadata_population_cap')
    known = set(params['symbols'])
    if any(row['symbol'] not in known for row in (*census, *conditions, *quotes)):
        raise ValueError('window_metadata_foreign_symbol')
    for symbol in known:
        scoped = [row for row in census if row['symbol'] == symbol]
        count = sum(row['row_count'] for row in scoped)
        if count > 1000000 or sum(row['row_count'] for row in quotes if row['symbol'] == symbol) > 1000000:
            raise ValueError('today_symbol_capture_cap:full_census')
        if any(row['invalid_price_count'] for row in scoped):
            raise ValueError('positive_source_number_required')
        valid = count - sum(row['invalid_size_count'] for row in scoped)
        if valid != sum(row['count'] for row in conditions if row['symbol'] == symbol):
            raise ValueError('condition_census_population_mismatch')
    return dict(census=census, conditions=conditions, quote_counts=quotes)


def collect_symbol(*, symbol, scoped, params, snapshot, universe, fetch, policy, source, queries, null_policy=None):
    """Retain all oracle bars/scanner/fills, cut only raw tape/quote payload."""
    from atrsimlive1_null_policy import require_null_policy

    require_null_policy(null_policy)
    anchors = []
    for row in scoped['fills']:
        if row['side'] == 'buy':
            anchors.append(dict(symbol=symbol, at=row['filled_at'], source='fill:' + row['fill_id']))
            if row['submitted_at'] is not None:
                anchors.append(dict(symbol=symbol, at=row['submitted_at'], source='order:' + row['order_id']))
    planned = plan([symbol], scoped['bars'], start=params['start'], end=params['end'], placements=anchors)
    resolved = []
    for window in planned['windows']:
        if not window.terminal_pending:
            resolved.append(window)
            continue
        after, after_id = None, None
        while window.terminal_pending:
            bound = dict(params, symbol=symbol, start=clock(window.boundary_ns),
                         after_ts=after, after_id=after_id)
            found = fetch(queries.CAPTURE_PAGES['trades'], bound, 'TERMINAL:' + symbol)
            page = found[:queries.PAGE_SIZE]
            window = terminal(window, page, policy=policy, source=source,
                              complete=len(found) <= queries.PAGE_SIZE, null_policy=null_policy)
            if window.terminal_pending:
                key = page[-1]['event_ts'], page[-1]['id']
                if after is not None and key <= (after, after_id):
                    raise ValueError('today_capture_cursor_not_advancing')
                after, after_id = key
        resolved.append(window)
    descriptors = selection(resolved)
    bound = dict(params, symbol=symbol, starts=[clock(w['start_ns']) for w in descriptors],
        ends=[None if w['end_exclusive_ns'] is None else clock(w['end_exclusive_ns']) for w in descriptors],
        inclusive_ends=[None if w['end_inclusive_timestamp_ns'] is None
                        else clock(w['end_inclusive_timestamp_ns']) for w in descriptors])
    for kind, original in queries.CAPTURE_PAGES.items():
        # Literal identifiers/order/page cap are unchanged; only the bound
        # original-day predicate gains the union of actual working windows.
        sql = payload_sql(original)
        found_rows, after, after_id = {}, None, None
        while descriptors:
            found = fetch(sql, dict(bound, after_ts=after, after_id=after_id), 'WINDOW:' + kind + ':' + symbol)
            page = found[:queries.PAGE_SIZE]
            for row in page:
                prior = found_rows.get(row['id'])
                if prior is not None and prior != row:
                    raise ValueError('physical_row_identity_conflict')
                found_rows[row['id']] = row
            if len(found_rows) > queries.MAX_SYMBOL_CAPTURE_ROWS:
                raise ValueError('today_symbol_capture_cap:' + kind)
            if len(found) <= queries.PAGE_SIZE:
                break
            key = page[-1]['event_ts'], page[-1]['id']
            if after is not None and key <= (after, after_id):
                raise ValueError('today_capture_cursor_not_advancing')
            after, after_id = key
        if kind == 'quotes':
            # Latest prior raw quote is intentionally not latest VALID quote.
            for start in sorted(set(bound['starts'])):
                carry = fetch(CARRY_SQL,
                    dict(params, symbol=symbol, window_start=start), 'QUOTE_CARRY:' + symbol)
                for row in carry:
                    prior = found_rows.get(row['id'])
                    if prior is not None and prior != row:
                        raise ValueError('physical_row_identity_conflict')
                    found_rows[row['id']] = row
        scoped[kind] = sorted(found_rows.values(), key=lambda row: (row['event_ts'], row['id']))
    census = [row for row in universe['census'] if row['symbol'] == symbol]
    conditions = [row for row in universe['conditions'] if row['symbol'] == symbol]
    certificate = dict(schema=SCHEMA, purpose='SAME_RR_SQL_WINDOWS_PLUS_FULL_CENSUS', snapshot_id=snapshot,
        descriptors=descriptors, metadata=dict(census=census, conditions=conditions),
        full_counts=dict(trades=sum(row['row_count'] for row in census),
            quotes=sum(row['row_count'] for row in universe['quote_counts'] if row['symbol'] == symbol)))
    if null_policy is not None:
        certificate['null_policy'] = null_policy
        certificate['metadata']['null_policy'] = null_policy
    return certificate


def seal(data, certificate):
    """After DB CLOSE, seal actual selected payload; never claim raw full-read."""
    from atrsimlive1_window_coverage import derive_census

    value = dict(certificate)
    value['selected_counts'] = {kind: len(data['rows'][kind]) for kind in ('trades', 'quotes')}
    if any(value['selected_counts'][kind] > value['full_counts'][kind] for kind in ('trades', 'quotes')):
        raise ValueError('window_selected_population_exceeds_census')
    value['selected_hashes'] = {kind: data['source_hashes'][kind] for kind in ('trades', 'quotes')}
    proof = dict(schema='ATRSIMLIVE1_WINDOW_CENSUS_RR_V1', symbol=data['symbols'][0],
        provider='massive', snapshot_id=data['snapshot_id'], start=data['start'], end=data['end'],
        source_asof=data['source_asof'],
        truncated=False, full_read=False, row_count=value['full_counts']['trades'],
        census_sha256=digest(value['metadata']['census']), bars_sha256=data['source_hashes']['bars'],
        full_counts_sha256=digest(value['full_counts']),
        selected_hashes=value['selected_hashes'], bars_full_read=True, bars_truncated=False,
        bar_provider='schwab', bar_table='strategy_bar_history', bar_count=len(data['rows']['bars']))
    if value.get('null_policy') is not None:
        proof['stored_null_policy'] = value['null_policy']
    windows, report = derive_census(symbol=data['symbols'][0], day=data['trading_day'],
        bars=data['rows']['bars'], census=value['metadata']['census'], read_proof=proof,
        log=data['capture_log'], asof_ns=nanos(data['source_asof']))
    value['metadata'].update(coverage_windows=windows, coverage_report=report)
    value['metadata_sha256'] = digest(value['metadata'])
    value['parent_source_hashes'] = dict(data['source_hashes'])
    data['read_proof'] = proof
    data['window_view'] = value
    from atrsimlive1_collect import queries

    data['query_sha256'] = query_binding(queries)
    from atrsimlive1_window_view import envelope_sha256

    value['envelope_sha256'] = envelope_sha256(data)
    return data
