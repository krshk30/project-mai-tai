"""C119:54:03 controlled TODAY successor; no provider/native/live claims."""
from copy import deepcopy
from dataclasses import asdict
from datetime import timedelta
from decimal import Decimal
import importlib
from pathlib import Path

import pytest

from tests.unit.test_atrsimlive1_recomputed import LEFT, data_fixture, route_packet
from atrsimlive1_collect import encoded
from tests.unit.test_atrsimlive1_working_windows import row


def operands(monkeypatch, *, size=100):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2] / 'tools'))
    nulls = importlib.import_module('atrsimlive1_null_policy')
    models = importlib.import_module('atrsimlive1_models')
    from project_mai_tai.atr_simulation.types import SourceRef, EvidenceKind, EventKey, TradePrint
    from project_mai_tai.atr_simulation.execution_eligibility import policy_from_payload
    source = SourceRef('/CONTROLLED/conditions', 'a' * 64, '$', EvidenceKind.CONTROLLED, None)
    # Code37 flags match retained oct5-condition-rules.json: no high/low or
    # open/close update; volume true. This is an explicit controlled snapshot.
    policy = policy_from_payload({
        '1': dict(name='CONTROLLED regular', updates_high_low=True, updates_open_close=True, updates_volume=True),
        '37': dict(name='Odd Lot Trade', updates_high_low=False, updates_open_close=False, updates_volume=True),
    }, source)
    trade = TradePrint(EventKey(models.nanos(LEFT), 1, 'CONTROLLED:1', source, models.nanos(LEFT)),
                       'CONTROLLED', Decimal('10'), size)
    return nulls, models, source, policy, trade


def test_present_null_roundlot_is_regular_only_under_named_opt_in(monkeypatch):
    nulls, models, _, policy, trade = operands(monkeypatch)
    raw = {'conditions': None}
    before = encoded(raw)
    off = nulls.classify_row_execution(trade, raw, policy)
    on = nulls.classify_row_execution(trade, raw, policy, null_policy=nulls.NULL_REGULAR_V1)
    assert off.eligible is None and off.conditions_present is False
    assert on.eligible is True and on.conditions_present is True and on.reason == 'no_conditions'
    assert on.raw_conditions is None and on.raw_id == trade.key.raw_id and on.raw_source == trade.key.source
    assert encoded(raw) == before and raw == {'conditions': None}


def test_absent_column_stays_unknown_and_is_not_injected_as_null(monkeypatch):
    nulls, _, _, policy, trade = operands(monkeypatch)
    raw = {}
    evidence = nulls.classify_row_execution(trade, raw, policy, null_policy=nulls.NULL_REGULAR_V1)
    assert evidence.eligible is None and evidence.conditions_present is False
    assert evidence.reason == 'UNKNOWN_CONDITIONS_COLUMN_MISSING' and raw == {}


@pytest.mark.parametrize('conditions,size,eligible,reason', [
    ('37', 100, False, 'not_consolidated_ohlc=37'),
    ('37', 37, False, 'ROUND_LOT_REQUIRED'),
    ('', 100, True, 'no_conditions'),
    ('bad', 100, None, 'UNKNOWN_MALFORMED_CONDITIONS'),
    ('999999', 100, False, 'unknown_conditions=999999'),
])
def test_non_null_codes_and_roundlot_rules_are_not_reinterpreted(monkeypatch, conditions, size, eligible, reason):
    nulls, _, _, policy, trade = operands(monkeypatch, size=size)
    evidence = nulls.classify_row_execution(trade, {'conditions': conditions}, policy,
                                           null_policy=nulls.NULL_REGULAR_V1)
    assert evidence.eligible is eligible and evidence.reason == reason


def test_present_null_small_print_and_missing_policy_cannot_become_executable(monkeypatch):
    nulls, _, _, policy, small = operands(monkeypatch, size=37)
    evidence = nulls.classify_row_execution(small, {'conditions': None}, policy,
                                           null_policy=nulls.NULL_REGULAR_V1)
    assert evidence.eligible is False and evidence.reason == 'ROUND_LOT_REQUIRED'
    _, _, _, _, large = operands(monkeypatch)
    missing = nulls.classify_row_execution(large, {'conditions': None}, None,
                                         null_policy=nulls.NULL_REGULAR_V1)
    assert missing.eligible is None and missing.reason == 'UNKNOWN_EXECUTION_POLICY'


@pytest.mark.parametrize('raw,expected', [(None, None), ('', True), ('37', False), ('bad', None)])
def test_default_none_matches_exact_old_classifier_boundary_and_literal_outcomes(monkeypatch, raw, expected):
    _, models, _, policy, trade = operands(monkeypatch)
    from project_mai_tai.atr_simulation.execution_eligibility import classify_execution
    old_boundary = classify_execution(trade, raw, policy, conditions_present=raw is not None)
    off = models.stored_print_execution(trade, raw, policy)
    explicit_off = models.stored_print_execution(trade, raw, policy, null_policy=None)
    assert off.eligible is expected
    assert encoded(asdict(off)) == encoded(asdict(old_boundary))
    assert explicit_off == off


@pytest.mark.parametrize('bad', ['NULL_IS_EMPTY', True, 1])
def test_unknown_policy_version_refuses_instead_of_enabling_by_truthiness(monkeypatch, bad):
    nulls, _, _, policy, trade = operands(monkeypatch)
    with pytest.raises(ValueError, match='explicit_supported_TODAY_null_policy_required'):
        nulls.classify_row_execution(trade, {'conditions': None}, policy, null_policy=bad)


def test_terminal_and_model_use_same_present_null_and_absence_rules(monkeypatch):
    nulls, models, source, policy, _ = operands(monkeypatch)
    windows = importlib.import_module('atrsimlive1_working_windows')
    at = LEFT + timedelta(minutes=15)
    raw = [row(at, ident=1), row(at + timedelta(seconds=1), ident=2, size=37, conditions=None),
           row(at + timedelta(seconds=2), ident=3, conditions=None),
           row(at + timedelta(seconds=3), ident=4, conditions='')]
    del raw[0]['conditions']
    before = encoded(raw)
    window = windows.WorkingWindow('CONTROLLED', models.nanos(LEFT), models.nanos(at),
                                  models.nanos(LEFT + timedelta(minutes=18)), 'CONTROLLED', True)
    off = windows.terminal(window, raw, policy=policy, source=source, complete=True)
    on = windows.terminal(window, raw, policy=policy, source=source, complete=True, null_policy=nulls.NULL_REGULAR_V1)
    assert off.end_key == (models.nanos(at + timedelta(seconds=3)), 4)
    assert on.end_key == (models.nanos(at + timedelta(seconds=2)), 3)
    assert encoded(raw) == before and 'conditions' not in raw[0]


def test_weighted_counts_keep_present_null_separate_from_absent_and_keep_all_body_rows(monkeypatch):
    nulls, models, source, policy, _ = operands(monkeypatch)
    census = importlib.import_module('atrsimlive1_window_census')
    raw = [row(LEFT, ident=1, conditions=None), row(LEFT, ident=2, conditions=None),
           row(LEFT, ident=3), row(LEFT, ident=4, conditions='37'),
           row(LEFT, ident=5, conditions=None, size=37)]
    del raw[2]['conditions']
    before = encoded(raw)
    grouped = census.condition_groups(raw)
    assert len(grouped) == 4 and sorted(item['count'] for item in grouped) == [1, 1, 1, 2]
    kwargs = dict(start_ns=models.nanos(LEFT), end_ns=models.nanos(LEFT + timedelta(minutes=1)),
                  policy=policy, source=source)
    off = census.condition_totals(grouped, **kwargs)
    on = census.condition_totals(grouped, **kwargs, null_policy=nulls.NULL_REGULAR_V1)
    assert off['total_prints'] == on['total_prints'] == 5
    assert off['missing_condition_evidence_prints'] == 4 and off['unknown_eligibility_prints'] == 3
    assert on['missing_condition_evidence_prints'] == 1 and on['unknown_eligibility_prints'] == 1
    assert on['blank_or_null_prints'] == 3 and on['actual_blank_or_null_pct'] == Decimal('60')
    assert encoded(raw) == before


@pytest.mark.parametrize('selected_on', [False, True])
def test_file_window_and_model_policy_mismatch_refuses(monkeypatch, tmp_path, selected_on):
    nulls, models, source, policy, _ = operands(monkeypatch)
    _, data, policy_path = data_fixture(monkeypatch, tmp_path)
    views = importlib.import_module('atrsimlive1_window_view')
    selected_policy = nulls.NULL_REGULAR_V1 if selected_on else None
    selected = views.make_view(data, policy=policy, source=source, artifact='/CONTROLLED/full',
        artifact_sha256='a' * 64, null_policy=selected_policy)
    with pytest.raises(ValueError, match='same_terminal_and_model_null_policy_required'):
        models.produce(selected, policy_path=policy_path, producer={},
            null_policy=None if selected_on else nulls.NULL_REGULAR_V1)


def test_default_file_view_omits_new_policy_metadata_and_explicit_none_is_identical(monkeypatch, tmp_path):
    nulls, models, source, policy, _ = operands(monkeypatch)
    _, data, _ = data_fixture(monkeypatch, tmp_path)
    views = importlib.import_module('atrsimlive1_window_view')
    kwargs = dict(policy=policy, source=source, artifact='/CONTROLLED/full', artifact_sha256='a' * 64)
    original = views.make_view(data, **kwargs)
    explicit_none = views.make_view(data, **kwargs, null_policy=None)
    assert encoded(original) == encoded(explicit_none)
    assert 'null_policy' not in original['window_view'] and 'null_policy' not in original['window_view']['metadata']
    on = views.make_view(data, **kwargs, null_policy=nulls.NULL_REGULAR_V1)
    assert views.validate_view(on)['null_policy'] == nulls.NULL_REGULAR_V1
    on['window_view']['null_policy'] = None
    with pytest.raises(ValueError, match='bound_window_null_policy_required'):
        views.validate_view(on)


def test_opt_in_model_records_version_only_no_new_row_label_or_raw_change(monkeypatch, tmp_path):
    nulls, models, _, _, _ = operands(monkeypatch)
    models, data, policy_path = data_fixture(monkeypatch, tmp_path)
    before = encoded(data['rows'])
    document = models.produce(data, policy_path=policy_path, producer={}, route_packet=route_packet(models),
                              null_policy=nulls.NULL_REGULAR_V1)
    assert document['producer']['stored_null_policy'] == nulls.NULL_REGULAR_V1
    assert encoded(data['rows']) == before
    for table in document['tables'].values():
        for item in table['rows']:
            assert 'null_policy_label' not in item['row'] and 'stored_null_policy' not in item['row']


def test_sql_window_collector_uses_same_null_policy_and_seals_it_without_raw_rewrite(monkeypatch, tmp_path):
    nulls, models, source, policy, _ = operands(monkeypatch)
    _, data, _ = data_fixture(monkeypatch, tmp_path)
    collector = importlib.import_module('atrsimlive1_window_collect')
    census = importlib.import_module('atrsimlive1_window_census')
    views = importlib.import_module('atrsimlive1_window_view')
    queries = importlib.import_module('atrsimlive1_queries')
    at = LEFT + timedelta(minutes=15)
    data['rows']['trades'].append(row(at, ident=888, conditions=None))
    raw = sorted(data['rows']['trades'], key=lambda r: (r['event_ts'], r['id']))
    before = encoded(raw)
    universe = dict(census=census.full_census(raw), conditions=census.condition_groups(raw), quote_counts=[])
    scoped = {kind: deepcopy(values) for kind, values in data['rows'].items() if kind not in ('trades', 'quotes')}
    params = dict(start=data['start'], end=data['end'], asof=data['source_asof'], symbols=['CONTROLLED'])
    def fetch(sql, bound, label):
        if 'quotes' in label.lower() or label.startswith('QUOTE_CARRY:'):
            return []
        if label.startswith('TERMINAL:'):
            return [r for r in raw if bound['start'] <= r['event_ts'] < bound['end']]
        assert label == 'WINDOW:trades:CONTROLLED'
        return [r for r in raw if any(r['event_ts'] >= start and
            (r['event_ts'] < end if end is not None else r['event_ts'] <= inclusive)
            for start, end, inclusive in zip(bound['starts'], bound['ends'], bound['inclusive_ends']))]
    certificate = collector.collect_symbol(symbol='CONTROLLED', scoped=scoped, params=params,
        snapshot=data['snapshot_id'], universe=universe, fetch=fetch, policy=policy, source=source,
        queries=queries, null_policy=nulls.NULL_REGULAR_V1)
    descriptor, = certificate['descriptors']
    assert descriptor['terminal_witness_key'] == (models.nanos(at), 888)
    assert scoped['trades'][-1]['id'] == 888 and scoped['trades'][-1]['conditions'] is None
    packet = dict(data, rows=scoped, source_hashes={key: models.digest(value) for key, value in scoped.items()},
                  source_counts={key: len(value) for key, value in scoped.items()})
    collector.seal(packet, certificate)
    assert views.validate_view(packet)['null_policy'] == nulls.NULL_REGULAR_V1
    assert packet['read_proof']['stored_null_policy'] == nulls.NULL_REGULAR_V1
    assert encoded(raw) == before
    packet['read_proof']['stored_null_policy'] = None
    with pytest.raises(ValueError, match='bound_SQL_null_policy_required'):
        views.validate_view(packet)
