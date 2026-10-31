"""CONTROLLED SQL-row-hash ports, never actual SQL/native proof. Author UNRUN."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import importlib
from pathlib import Path

import pytest


LEFT = datetime(2026, 10, 28, 8, tzinfo=timezone.utc)


def modules(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2] / 'tools'))
    return (importlib.import_module('atrsimlive1_change_proof'),
            importlib.import_module('atrsimlive1_incremental_reads'),
            importlib.import_module('atrsimlive1_incremental_cache'))


def row(number, event, received=None, *, kind='trades'):
    value = dict(id=number, provider='massive', symbol='CONTROLLED',
        event_ts=LEFT + timedelta(seconds=event), received_at=LEFT + timedelta(seconds=event if received is None else received))
    if kind == 'trades':
        value.update(price=Decimal('1.5300'), size=100, exchange=1, conditions=None, cumulative_volume=100)
    else:
        value.update(bid_price=Decimal('1.52'), ask_price=Decimal('1.53'), bid_size=100, ask_size=None)
    return value


def certificate(reads, cache, rows, *, kind='trades', end=120, asof=180, snapshot='OLD', start=0):
    # CONTROLLED stand-in for the ORIGINAL SQL's projected row hash, not a
    # claim that Python JSON equals PostgreSQL to_jsonb serialization.
    hashes = {(item['symbol'], item['id']): cache.fingerprint(item) for item in rows}
    result = reads.certify(dict(row_count=len(rows), content_sha256=reads.content_hash(rows, hashes), source_timezone='UTC'),
        kind=kind, symbol='CONTROLLED', start=LEFT + timedelta(seconds=start), end=LEFT + timedelta(seconds=end),
        day_start=LEFT, asof=LEFT + timedelta(seconds=asof), snapshot_id=snapshot,
        query_sha256=reads.query_pair(kind)['query_sha256'], read_only='on', isolation='repeatable read', complete=True)
    return result, hashes


def previous(reads, cache, rows, **kwargs):
    cert, hashes = certificate(reads, cache, rows, **kwargs)
    return dict(rows=rows, hashes={str(key[1]): value for key, value in hashes.items()}, certificate=cert)


def test_since_actual_arrivals_and_observed_update_match_exact_full_certificate(monkeypatch):
    change, reads, cache = modules(monkeypatch)
    old_rows = [row(1, 10), row(2, 20)]
    old = previous(reads, cache, old_rows)
    original = deepcopy(old)
    updated = dict(row(1, 10, 240), price=Decimal('1.5400'))
    late, appended = row(3, 15, 245), row(4, 180, 250)
    full, _ = certificate(reads, cache, [updated, late, old_rows[1], appended], end=240, asof=300, snapshot='NEW')
    candidates = [updated, late, appended]
    hashes = {(item['symbol'], item['id']): cache.fingerprint(item) for item in candidates}
    value = change.merge_since(old, candidates, hashes, current_full=full, current_asof=full['asof'], current_snapshot='NEW')
    assert value['action'] == 'SINCE_MERGE_EXACT_CURRENT_FULL_CERTIFICATE'
    assert value['rows'] == [updated, late, old_rows[1], appended]
    assert old == original and value['rows'][0]['price'] == Decimal('1.5400')


@pytest.mark.parametrize('fault', ['unobserved-correction', 'delete', 'backdated-receipt'])
def test_empty_suffix_never_hides_full_certificate_changes(monkeypatch, fault):
    change, reads, cache = modules(monkeypatch)
    old_rows = [row(1, 10), row(2, 20)]
    old = previous(reads, cache, old_rows)
    if fault == 'delete':
        current = old_rows[1:]
    elif fault == 'unobserved-correction':
        current = [dict(old_rows[0], price=Decimal('9.00')), old_rows[1]]
    else:
        current = [*old_rows, row(3, 15, 30)]
    full, _ = certificate(reads, cache, current, end=240, asof=300, snapshot='NEW')
    result = change.merge_since(old, [], {}, current_full=full, current_asof=full['asof'], current_snapshot='NEW')
    assert result == dict(action='FRESH_WINDOW', reason='SINCE_MERGE_FULL_CERTIFICATE_MISMATCH')
    assert 'rows' not in result


def test_since_quote_carry_replacement_preserves_exact_original_carry_rule(monkeypatch):
    change, reads, cache = modules(monkeypatch)
    old_quote, latest = row(1, 10, kind='quotes'), row(2, 50, 250, kind='quotes')
    old = previous(reads, cache, [old_quote], kind='quotes', start=60)
    full, hashes = certificate(reads, cache, [latest], kind='quotes', start=60, end=240, asof=300, snapshot='NEW')
    result = change.merge_since(old, [latest], hashes, current_full=full, current_asof=full['asof'], current_snapshot='NEW')
    assert result['rows'] == [latest] and result['rows'][0]['ask_size'] is None
    assert old['rows'] == [old_quote]


@pytest.mark.parametrize('fault', ['foreign-snapshot', 'future-old', 'stale-arrival', 'duplicate'])
def test_since_invalid_scope_or_candidate_cannot_become_a_merge(monkeypatch, fault):
    change, reads, cache = modules(monkeypatch)
    old = previous(reads, cache, [row(1, 10)])
    candidate = row(2, 180, 250)
    full, _ = certificate(reads, cache, [old['rows'][0], candidate], end=240, asof=300, snapshot='NEW')
    candidates = [candidate]
    if fault == 'foreign-snapshot':
        full['snapshot_id'] = 'FOREIGN'
    elif fault == 'future-old':
        old['certificate']['asof'] = LEFT + timedelta(seconds=400)
    elif fault == 'stale-arrival':
        candidates = [row(2, 180, 170)]
    else:
        candidates = [candidate, deepcopy(candidate)]
    hashes = {(item['symbol'], item['id']): cache.fingerprint(item) for item in candidates}
    with pytest.raises(cache.CacheRefusal):
        change.merge_since(old, candidates, hashes, current_full=full, current_asof=full['asof'], current_snapshot='NEW')


@pytest.mark.parametrize('case', ['append', 'unchanged-closed', 'unobserved-delete'])
def test_connected_collector_uses_suffix_only_when_useful_and_falls_back_exactly(monkeypatch, tmp_path, case):
    _, reads, cache = modules(monkeypatch)
    collect = importlib.import_module('atrsimlive1_incremental_collect')
    root = (tmp_path / 'cache').resolve()
    root.mkdir(mode=0o700)
    binding = dict(version=cache.INCREMENTAL_V1, trading_day='2026-10-28', source_seal='a' * 64,
        input_scope='CURRENT_RR', policies=dict(execution_rules_sha256='b' * 64, null_policy=None, same_sip_policy=None),
        features=dict(version=cache.FEATURE_PROFILE, closed_components=False, raw_pool=False, since_last_run=True))
    run = collect.IncrementalRun(cache.CacheStore(root, binding))
    run.bind_snapshot(dict(read_only='on', isolation='repeatable read', snapshot_id='NEW'),
        dict(day=LEFT.date(), start=LEFT, end=LEFT + timedelta(seconds=300), asof=LEFT + timedelta(seconds=300)), ('CONTROLLED',))
    old_rows = [row(1, 10), row(2, 20)]
    old = previous(reads, cache, old_rows)
    if case == 'append':
        current, suffix, end = [*old_rows, row(3, 180, 250)], [row(3, 180, 250)], 240
    elif case == 'unchanged-closed':
        current, suffix, end = old_rows, [], 120
    else:
        current, suffix, end = old_rows[1:], [], 240
    full, _ = certificate(reads, cache, current, end=end, asof=300, snapshot='NEW')
    calls = []
    def fetch(sql, bound, label):
        calls.append(label)
        if label.startswith('INCREMENTAL_FINGERPRINT:'):
            assert bound['end'] == LEFT + timedelta(seconds=end)
            return [{name: full[name] for name in ('row_count', 'content_sha256', 'source_timezone')}]
        if label.startswith('INCREMENTAL_SINCE_ROWS:'):
            assert case != 'unchanged-closed'
            assert 'received_at > %(previous_asof)s' in sql and 'incremental_row_sha256' in sql
            assert bound['previous_asof'] == old['certificate']['asof']
            values = suffix
        else:
            assert label.startswith('INCREMENTAL_ROWS:') and case == 'unobserved-delete'
            values = current
        return [dict(item, incremental_row_sha256=cache.fingerprint(item)) for item in values]
    result = run._window_rows('trades', 'CONTROLLED', collect.nanos(LEFT), collect.nanos(LEFT + timedelta(seconds=end)),
        {'trades': old}, fetch)
    assert result['rows'] == current and result['certificate'] == full
    assert len(calls) == {'append': 2, 'unchanged-closed': 1, 'unobserved-delete': 3}[case]
    assert run.audit[-1]['action'] == {'append': 'SINCE_MERGE_EXACT_CURRENT_FULL_CERTIFICATE',
        'unchanged-closed': 'RETAIN_CLOSED_INPUT', 'unobserved-delete': 'FRESH_WINDOW'}[case]
