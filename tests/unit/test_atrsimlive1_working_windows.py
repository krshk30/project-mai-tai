"""CONTROLLED source packet. Main executes; no actual SQL/tape/native claim."""
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
import importlib

import pytest

from tests.unit.test_atrsimlive1_recomputed import LEFT, bars, data_fixture, route_packet


def units(monkeypatch):
    from pathlib import Path
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2] / 'tools'))
    return tuple(importlib.import_module('atrsimlive1_' + name) for name in (
        'working_windows', 'window_census', 'window_view', 'window_collect', 'window_coverage'))


def row(at, *, ident=1, size=100, conditions='', price='20', symbol='CONTROLLED'):
    return dict(id=ident, provider='massive', symbol=symbol, event_ts=at,
        received_at=LEFT + timedelta(minutes=18), price=price, size=size,
        conditions=conditions, exchange=1, cumulative_volume=None)


def source_policy():
    from project_mai_tai.atr_simulation.types import SourceRef, EvidenceKind
    from project_mai_tai.atr_simulation.execution_eligibility import policy_from_payload
    source = SourceRef('/CONTROLLED/input', 'a' * 64, '$', EvidenceKind.CONTROLLED, None)
    return source, policy_from_payload({'1': dict(name='CONTROLLED',
        updates_high_low=True, updates_open_close=True, updates_volume=True)}, source)


def test_plan_starts_at_buy_bar_not_touch_and_keeps_no_window_names(monkeypatch):
    planner, *_ = units(monkeypatch)
    before = bars()
    result = planner.plan(['CONTROLLED', 'NO_WINDOW'], before, start=LEFT,
                          end=LEFT + timedelta(minutes=18))
    assert result['symbols'] == ['CONTROLLED', 'NO_WINDOW']
    assert result['no_window_symbols'] == ['NO_WINDOW']
    window, = result['windows']
    assert window.start_ns == planner.nanos(LEFT + timedelta(minutes=10))
    assert window.boundary_ns == planner.nanos(LEFT + timedelta(minutes=15))
    assert window.terminal_pending is True and before == bars()


def test_open_window_runs_to_actual_asof_not_fake_20_exit(monkeypatch):
    planner, *_ = units(monkeypatch)
    end = LEFT + timedelta(minutes=14)
    result = planner.plan(['CONTROLLED'], bars()[:13], start=LEFT, end=end)
    descriptor, = planner.selection(result['windows'])
    assert descriptor['end_exclusive_ns'] == planner.nanos(end)
    assert descriptor['end_inclusive_timestamp_ns'] is None
    assert descriptor['status'] == 'ASOF_OPEN'


def test_actual_placement_anchor_keeps_its_minute_and_source(monkeypatch):
    planner, *_ = units(monkeypatch)
    at = LEFT + timedelta(minutes=2, seconds=43)
    result = planner.plan(['CONTROLLED'], bars(), start=LEFT, end=LEFT + timedelta(minutes=18),
        placements=[dict(symbol='CONTROLLED', at=at, source='fill:CONTROLLED')])
    placed = next(w for w in result['windows'] if w.origin.startswith('RECORDED_PLACEMENT'))
    assert placed.start_ns == planner.nanos(LEFT + timedelta(minutes=2))
    assert placed.boundary_ns == planner.nanos(LEFT + timedelta(minutes=10))


def test_unresolved_tail_never_seals_empty_success(monkeypatch):
    planner, *_ = units(monkeypatch)
    window, = planner.plan(['CONTROLLED'], bars(), start=LEFT, end=LEFT + timedelta(minutes=18))['windows']
    with pytest.raises(ValueError, match='terminal_search_or_asof_proof_required'):
        planner.selection([window])
    source, policy = source_policy()
    assert planner.terminal(window, [], policy=policy, source=source, complete=False).terminal_pending


def test_terminal_retains_unknown_oddlot_prefix_and_entire_timestamp_group(monkeypatch):
    planner, *_ = units(monkeypatch)
    window, = planner.plan(['CONTROLLED'], bars(), start=LEFT, end=LEFT + timedelta(minutes=18))['windows']
    source, policy = source_policy()
    at = LEFT + timedelta(minutes=15)
    rows = [row(at, ident=1, conditions=None), row(at, ident=2, size=99),
            row(at + timedelta(seconds=1), ident=3), row(at + timedelta(seconds=1), ident=4, price='1')]
    resolved = planner.terminal(window, rows, policy=policy, source=source, complete=True)
    selected, = planner.selection([resolved])
    assert selected['terminal_witness_key'] == (planner.nanos(at + timedelta(seconds=1)), 3)
    assert selected['end_inclusive_timestamp_ns'] == planner.nanos(at + timedelta(seconds=1))
    assert selected['start_ns'] < planner.nanos(at)


def test_no_eligible_terminal_requires_complete_asof_not_guessed_padding(monkeypatch):
    planner, *_ = units(monkeypatch)
    window, = planner.plan(['CONTROLLED'], bars(), start=LEFT, end=LEFT + timedelta(minutes=18))['windows']
    source, policy = source_policy()
    result = planner.terminal(window, [row(LEFT + timedelta(minutes=15), size=99)],
                              policy=policy, source=source, complete=True)
    selected, = planner.selection([result])
    assert selected['end_exclusive_ns'] == planner.nanos(LEFT + timedelta(minutes=18))
    assert selected['status'] == 'ASOF_NO_ELIGIBLE_TERMINATOR'


@pytest.mark.parametrize('mutation', ['foreign_symbol', 'provider', 'before_sell', 'asof', 'descending'])
def test_terminal_probe_refuses_wrong_scope_or_serialization(monkeypatch, mutation):
    planner, *_ = units(monkeypatch)
    window, = planner.plan(['CONTROLLED'], bars(), start=LEFT, end=LEFT + timedelta(minutes=18))['windows']
    source, policy = source_policy()
    rows = [row(LEFT + timedelta(minutes=15), ident=2, size=99)]
    if mutation == 'foreign_symbol':
        rows[0]['symbol'] = 'FOREIGN'
    elif mutation == 'provider':
        rows[0]['provider'] = 'FOREIGN'
    elif mutation == 'before_sell':
        rows[0]['event_ts'] -= timedelta(microseconds=1)
    elif mutation == 'asof':
        rows[0]['event_ts'] = LEFT + timedelta(minutes=18)
    else:
        rows.append(dict(rows[0], id=1))
    with pytest.raises(ValueError, match='exact_causal_terminal_probe_required'):
        planner.terminal(window, rows, policy=policy, source=source, complete=True)


def test_full_census_preserves_outside_window_fault_clocks_and_raw_population(monkeypatch):
    planner, census, *_ = units(monkeypatch)
    at = LEFT + timedelta(seconds=7)
    raw = [row(at, ident=1), row(at, ident=2), row(at + timedelta(seconds=2), ident=3, size=None)]
    value, = census.full_census(raw)
    assert value['row_count'] == 3 and value['same_sip_valid_groups'] == 1
    assert value['same_sip_ns'] == [planner.nanos(at)]
    assert value['invalid_size_ns'] == [planner.nanos(at + timedelta(seconds=2))]
    assert value['first_id'] == 1 and value['last_id'] == 3
    assert value['max_gap_ns'] == 2000000000


def test_weighted_conditions_preserve_full_scope_unknown_blank_and_oddlot_counts(monkeypatch):
    planner, census, *_ = units(monkeypatch)
    source, policy = source_policy()
    at = LEFT + timedelta(seconds=7)
    raw = [row(at, ident=1), row(at, ident=2), row(at, ident=3, size=99),
           row(at, ident=4, conditions=None), row(at, ident=5, size=None)]
    result = census.condition_totals(census.condition_groups(raw), start_ns=planner.nanos(LEFT),
        end_ns=planner.nanos(LEFT + timedelta(minutes=1)), policy=policy, source=source)
    assert result['total_prints'] == 4
    assert result['blank_or_null_prints'] == 3 and result['actual_blank_or_null_pct'] == Decimal('75')
    assert result['missing_condition_evidence_prints'] == 1 and result['unknown_eligibility_prints'] == 1


def test_window_file_selection_uses_latest_raw_quote_even_invalid(monkeypatch, tmp_path):
    _, _, view, *_ = units(monkeypatch)
    models, data, _ = data_fixture(monkeypatch, tmp_path)
    def quote(ident, minute, bid):
        return dict(id=ident, provider='massive', symbol='CONTROLLED', event_ts=LEFT + timedelta(minutes=minute),
            received_at=data['source_asof'], bid_price=bid, ask_price='22', bid_size=1000, ask_size=1000)
    data['rows']['quotes'] = [quote(1, 8, '20'), quote(2, 9, None), quote(3, 12, '21'), quote(4, 17, '21')]
    data['source_hashes']['quotes'] = models.digest(data['rows']['quotes'])
    data['source_counts']['quotes'] = 4
    source, policy = source_policy()
    selected = view.make_view(data, policy=policy, source=source,
                              artifact='/CONTROLLED/full-input', artifact_sha256='a' * 64)
    assert [r['id'] for r in selected['rows']['quotes']] == [2, 3]
    assert selected['rows']['quotes'][0]['bid_price'] is None
    assert selected['source_counts']['quotes'] == 4  # Actual full FILE, not SQL-full-read.
    assert selected['window_view']['selected_counts']['quotes'] == 2


@pytest.mark.parametrize('mutation', ['payload', 'census', 'coverage', 'parent', 'artifact'])
def test_file_view_rejects_changed_payload_or_metadata(monkeypatch, tmp_path, mutation):
    _, _, view, *_ = units(monkeypatch)
    _, data, _ = data_fixture(monkeypatch, tmp_path)
    source, policy = source_policy()
    selected = view.make_view(data, policy=policy, source=source,
                              artifact='/CONTROLLED/full-input', artifact_sha256='a' * 64)
    if mutation == 'payload':
        selected['rows']['trades'][0] = dict(selected['rows']['trades'][0], price='999')
    elif mutation == 'census':
        selected['window_view']['metadata']['census'][0]['row_count'] += 1
    elif mutation == 'coverage':
        selected['window_view']['metadata']['coverage_report']['issues'].clear()
        selected['window_view']['metadata']['coverage_report']['issues'].append('CHANGED')
    elif mutation == 'parent':
        selected['source_hashes'] = dict(selected['source_hashes'], bars='f' * 64)
    else:
        selected['window_view']['artifact_sha256'] = 'NOT_A_HASH'
    with pytest.raises(ValueError):
        view.validate_view(selected)


def test_default_policy_file_view_preserves_controlled_receipts_and_known_economics(monkeypatch, tmp_path):
    _, _, view, *_ = units(monkeypatch)
    models, data, path = data_fixture(monkeypatch, tmp_path)
    source, policy = source_policy()
    selected = view.make_view(data, policy=policy, source=source,
                              artifact='/CONTROLLED/full-input', artifact_sha256='a' * 64)
    original = models.produce(data, policy_path=path, producer={}, route_packet=route_packet(models))
    reduced = models.produce(selected, policy_path=path, producer={}, route_packet=route_packet(models))
    assert len(selected['rows']['trades']) < len(data['rows']['trades'])
    assert reduced['producer']['engine_receipts'] == original['producer']['engine_receipts']
    assert 'same_sip_policy' not in reduced['producer']
    known = [x['row'] for x in reduced['tables']['FIRSTBAR']['rows'] if x['row']['pnl_label_known']]
    assert len(known) == 1 and Decimal(known[0]['pnl']) == Decimal('29.68')


def test_payload_sql_keeps_symbol_provider_asof_cursor_and_exact_end_group(monkeypatch):
    *_, collector, _ = units(monkeypatch)
    from atrsimlive1_queries import CAPTURE_PAGES
    for original in CAPTURE_PAGES.values():
        sql = collector.payload_sql(original)
        assert "symbol = %(symbol)s" in sql and "provider = 'massive'" in sql
        assert 'received_at <= %(asof)s' in sql and 'event_ts <= w.last_ts' in sql
        assert '(event_ts, id) > (%(after_ts)s::timestamptz, %(after_id)s::bigint)' in sql
        assert 'LIMIT 5001' in sql and 'EXISTS' in sql
    with pytest.raises(ValueError, match='literal_capture_predicate_not_found'):
        collector.payload_sql('SELECT unrelated')


def test_census_coverage_preserves_literal_minute_statuses_without_raw_full_read_claim(monkeypatch, tmp_path):
    planner, census, _, _, coverage = units(monkeypatch)
    models, data, _ = data_fixture(monkeypatch, tmp_path)
    metadata = census.full_census(data['rows']['trades'])
    proof = dict(schema='ATRSIMLIVE1_WINDOW_CENSUS_RR_V1', symbol='CONTROLLED', truncated=False,
        provider='massive', full_read=False, bar_provider='schwab', bar_table='strategy_bar_history',
        bars_full_read=True, bars_truncated=False, bar_count=len(data['rows']['bars']),
        start=LEFT, end=data['end'], census_sha256=models.digest(metadata),
        bars_sha256=models.digest(data['rows']['bars']), row_count=len(data['rows']['trades']))
    windows, report = coverage.derive_census(symbol='CONTROLLED', day=data['trading_day'],
        bars=data['rows']['bars'], census=metadata, read_proof=proof,
        log=data['capture_log'], asof_ns=planner.nanos(data['source_asof']))
    assert len(windows) == 1
    assert report['minutes'][0]['status'] == 'COVERED_POSITIVE_BAR_WITH_PRINT'
    assert windows[0][4]['rows_sha256'] is None and windows[0][4]['full_read'] is False
    assert windows[0][4]['census_full_read'] is True
    proof['census_sha256'] = 'f' * 64
    rejected, bad = coverage.derive_census(symbol='CONTROLLED', day=data['trading_day'],
        bars=data['rows']['bars'], census=metadata, read_proof=proof,
        log=data['capture_log'], asof_ns=planner.nanos(data['source_asof']))
    assert rejected == [] and bad['issues'] == ['UNKNOWN_SAME_SNAPSHOT_FULL_READ_CERTIFICATE']


def test_sql_window_path_pages_complete_end_group_deduplicates_and_seals_actual_selection(monkeypatch, tmp_path):
    planner, census, view, collector, _ = units(monkeypatch)
    from atrsimlive1_queries import CAPTURE_PAGES
    import atrsimlive1_queries as queries
    models, original, _ = data_fixture(monkeypatch, tmp_path)
    source, policy = source_policy()
    at = LEFT + timedelta(minutes=15, seconds=5)
    original['rows']['trades'].append(row(at, ident=501, price='1'))
    raw = sorted(original['rows']['trades'], key=lambda r: (r['event_ts'], r['id']))
    universe = dict(census=census.full_census(raw), conditions=census.condition_groups(raw), quote_counts=[])
    params = dict(start=LEFT, end=original['end'], asof=original['source_asof'], symbols=['CONTROLLED'])
    scoped = {kind: deepcopy(rows) for kind, rows in original['rows'].items() if kind not in ('trades', 'quotes')}
    calls = []
    monkeypatch.setattr(queries, 'PAGE_SIZE', 2)
    def fetch(sql, bound, label):
        calls.append((label, bound))
        if label.startswith('QUOTE_CARRY:') or label.startswith('WINDOW:quotes:'):
            return []
        if label.startswith('TERMINAL:'):
            assert sql == CAPTURE_PAGES['trades']
            values = [r for r in raw if bound['start'] <= r['event_ts'] < bound['end']]
        else:
            assert label == 'WINDOW:trades:CONTROLLED' and 'EXISTS' in sql
            values = [r for r in raw if any(r['event_ts'] >= start and
                (r['event_ts'] < end if end is not None else r['event_ts'] <= inclusive)
                for start, end, inclusive in zip(bound['starts'], bound['ends'], bound['inclusive_ends']))]
        if bound['after_ts'] is not None:
            values = [r for r in values if (r['event_ts'], r['id']) > (bound['after_ts'], bound['after_id'])]
        return values[:queries.PAGE_SIZE + 1]
    certificate = collector.collect_symbol(symbol='CONTROLLED', scoped=scoped, params=params,
        snapshot=original['snapshot_id'], universe=universe, fetch=fetch,
        policy=policy, source=source, queries=queries)
    assert len(scoped['trades']) < len(raw)
    assert scoped['trades'][-2]['event_ts'] == at and scoped['trades'][-1]['id'] == 501
    assert len({r['id'] for r in scoped['trades']}) == len(scoped['trades'])
    packet = dict(original, rows=scoped,
        source_hashes={key: models.digest(value) for key, value in scoped.items()},
        source_counts={key: len(value) for key, value in scoped.items()})
    collector.seal(packet, certificate)
    assert view.validate_view(packet)['schema'] == 'ATRSIMLIVE1_SQL_WINDOW_VIEW_V1'
    assert packet['read_proof']['full_read'] is False and 'rows_sha256' not in packet['read_proof']
    assert certificate['full_counts']['trades'] == len(raw)
    assert packet['query_sha256'] == collector.query_binding(queries)
    assert any(label.startswith('TERMINAL:') for label, _ in calls)


@pytest.mark.parametrize('failure', ['metadata', 'byte_cap', 'deadline'])
def test_working_collection_failure_rolls_back_and_never_processes_partial_roster(monkeypatch, failure):
    units(monkeypatch)
    from tests.unit.test_atrsimlive1_collect_close import controlled, NOW
    import atrsimlive1_window_collect as worker
    collect, connection, _, logs, _, _ = controlled(monkeypatch)
    source, policy = source_policy()
    monkeypatch.setattr(worker, 'policy_input', lambda *args, **kwargs: (policy, source))
    def refuse(*args, **kwargs):
        raise ValueError('CONTROLLED_METADATA_REFUSAL')
    monkeypatch.setattr(worker, 'metadata', refuse)
    if failure == 'byte_cap':
        monkeypatch.setattr(collect, 'MAX_INPUT_BYTES', 1)
    elif failure == 'deadline':
        from types import SimpleNamespace
        clocks = iter([0, 181])
        monkeypatch.setattr(collect, 'time', SimpleNamespace(monotonic=lambda: next(clocks)))
    calls = []
    with pytest.raises((ValueError, TimeoutError)):
        collect.collect(dsn='CONTROLLED-NOT-A-DSN', processor=lambda item: calls.append(item),
                        capture_log_path='CONTROLLED-NO-FILE', now=NOW, working_policy_path='/CONTROLLED/policy')
    assert connection.closed and connection.commands[-1] == 'ROLLBACK'
    assert calls == logs == []


def test_pre_day_placement_preserves_original_clock_without_reading_previous_day(monkeypatch):
    planner, *_ = units(monkeypatch)
    prior = LEFT - timedelta(hours=2)
    result = planner.plan(['CONTROLLED'], bars(), start=LEFT, end=LEFT + timedelta(minutes=18),
        placements=[dict(symbol='CONTROLLED', at=prior, source='order:PRIOR_DAY')])
    placed = next(w for w in result['windows'] if 'PRIOR_DAY' in w.origin)
    assert placed.start_ns == planner.nanos(LEFT)
    assert 'original_ns=' + str(planner.nanos(prior)) in placed.origin


@pytest.mark.parametrize('had_sip_gap', [False, True])
def test_explicit_additive_input_flag_only_replaces_old_sip_gap_not_other_unknown(monkeypatch, tmp_path, had_sip_gap):
    # Requires Main's separately sealed additive kernel in the composed root.
    # Captures the actual boundary objects; grants no fake native permission.
    units(monkeypatch)
    from project_mai_tai.atr_simulation.same_sip import SAME_SIP_CONSERVATIVE_V1
    from project_mai_tai.atr_simulation.same_sip_firstbar_replay import FirstBarReplayEngine
    from project_mai_tai.atr_simulation.same_sip_engine import SimulationEngine
    models, data, path = data_fixture(monkeypatch, tmp_path)
    raw = data['rows']['trades']
    if had_sip_gap:
        raw.append(dict(raw[-2], id=1001))
    raw.append(row(LEFT + timedelta(minutes=12, seconds=7), ident=1002, size=None))
    data['source_hashes']['trades'] = models.digest(raw)
    data['read_proof'].update(rows_sha256=models.digest(raw), row_count=len(raw))
    data['source_counts']['trades'] = len(raw)
    captured = []
    def firstbar(engine, items, **kwargs):
        captured.extend(item.episode for item in items)
        engine.same_sip_reports = {}
        return ()
    def core(engine, items):
        captured.extend(items)
        engine.same_sip_reports = {}
        return ()
    monkeypatch.setattr(FirstBarReplayEngine, 'simulate_many', firstbar)
    monkeypatch.setattr(SimulationEngine, 'simulate_many', core)
    models.produce(data, policy_path=path, producer={}, route_packet=route_packet(models),
                   same_sip_policy=SAME_SIP_CONSERVATIVE_V1)
    assert len(captured) == 2
    for episode in captured:
        assert episode.same_sip_policy == (SAME_SIP_CONSERVATIVE_V1 if had_sip_gap else None)
        assert 'UNKNOWN_CAPTURE_PRINT_SIZE' in episode.tape_coverage.gaps
        assert 'UNKNOWN_SAME_SIP_NATIVE_SEQUENCE' not in episode.tape_coverage.gaps


def differential_fixture():
    original = dict(trading_day='2026-10-28', source_asof='CONTROLLED',
        producer=dict(snapshot_id='CONTROLLED', observed_symbols=['CONTROLLED'],
                      input_refs={'trades': 'a' * 64},
                      engine_receipts={'one': dict(issues=['UNKNOWN_SAME_SIP_NATIVE_SEQUENCE'])}),
        tables={'FIRSTBAR': dict(rows=[dict(outcome_id='one', identity={'symbol': 'CONTROLLED'},
                                          row=dict(pnl_label_known=False, pnl=None))])})
    return original, deepcopy(original)


def test_existing_file_differential_lists_tie_conversion_separately_never_as_equality(monkeypatch):
    _, _, view, *_ = units(monkeypatch)
    original, successor = differential_fixture()
    successor['producer']['engine_receipts']['one'] = dict(issues=[])
    successor['tables']['FIRSTBAR']['rows'][0]['row'].update(pnl_label_known=True, pnl='1.00')
    successor['producer']['same_sip_conversions'] = [dict(world='FIRSTBAR', outcome_id='one')]
    result = view.compare_existing_receipts(original, successor)
    assert result['exact'] == 0 and result['explicit_tie_differences'] == 1
    assert result['previously_known'] == 0
    assert result['status'] == 'REFERENCE_DIFFERENTIAL_RECORDED_NOT_QUALIFICATION'


def test_changed_known_percent_or_undeclared_difference_blocks_without_normalization(monkeypatch):
    _, _, view, *_ = units(monkeypatch)
    original, successor = differential_fixture()
    original['tables']['FIRSTBAR']['rows'][0]['row']['pnl_pct_label_known'] = True
    successor['producer']['same_sip_conversions'] = [dict(world='FIRSTBAR', outcome_id='one')]
    successor['producer']['engine_receipts']['one']['issues'] = []
    result = view.compare_existing_receipts(original, successor)
    assert result['status'] == 'BLOCKED' and result['previously_known'] == 1
    assert result['records'][0]['status'] == 'UNEXPLAINED_CHANGE'


@pytest.mark.parametrize('mutation', ['duplicate', 'missing', 'foreign_snapshot'])
def test_existing_file_differential_refuses_changed_population(monkeypatch, mutation):
    _, _, view, *_ = units(monkeypatch)
    original, successor = differential_fixture()
    if mutation == 'duplicate':
        successor['tables']['FIRSTBAR']['rows'] *= 2
    elif mutation == 'missing':
        successor['tables']['FIRSTBAR']['rows'] = []
    else:
        successor['producer']['snapshot_id'] = 'FOREIGN'
    with pytest.raises(ValueError):
        view.compare_existing_receipts(original, successor)


def test_leg_membership_slices_keep_shared_physical_object_and_latest_quote(monkeypatch):
    _, _, view, *_ = units(monkeypatch)
    trades = [object() for _ in range(8)]
    quotes = [object() for _ in range(4)]
    descriptors = [dict(origin='BUY:A', start_ns=2, end_exclusive_ns=5, end_inclusive_timestamp_ns=None),
                   dict(origin='BUY:B', start_ns=4, end_exclusive_ns=None, end_inclusive_timestamp_ns=6)]
    first, first_quotes = view.leg_slice(descriptors, 'BUY:A', trades, list(range(8)),
                                       quotes, [0, 1, 3, 7], left=0, right=8)
    second, _ = view.leg_slice(descriptors, 'BUY:B', trades, list(range(8)),
                               quotes, [0, 1, 3, 7], left=0, right=8)
    assert first == trades[2:5] and second == trades[4:7]
    assert first[-1] is second[0] is trades[4]
    assert first_quotes == quotes[1:3] and first_quotes[0] is quotes[1]


@pytest.mark.parametrize('duplicates', [False, True])
def test_leg_membership_missing_or_duplicate_anchor_refuses(monkeypatch, duplicates):
    _, _, view, *_ = units(monkeypatch)
    one = dict(origin='BUY:A', start_ns=2, end_exclusive_ns=5, end_inclusive_timestamp_ns=None)
    with pytest.raises(ValueError, match='unique_original_working_anchor_required'):
        view.leg_slice([one, one] if duplicates else [], 'BUY:A', [], [], [], [], left=0, right=8)


@pytest.mark.parametrize('report,expected', [
    (None, None),
    (dict(same_timestamp_groups=99, entry_groups_blocked=0, exit_stop_wins=0, limit_pricing_fills=0), False),
    (dict(same_timestamp_groups=99, entry_groups_blocked=1, exit_stop_wins=0, limit_pricing_fills=0), True),
])
def test_tie_label_requires_actual_actions_not_global_group_count(monkeypatch, report, expected):
    units(monkeypatch)
    from atrsimlive1_models import tie_report_touched
    assert tie_report_touched(report) is expected
