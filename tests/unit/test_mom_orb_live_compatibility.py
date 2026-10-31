"""New controlled reader/comparison guards, never historical trading proof."""
from copy import deepcopy
import json

import pytest

from tools.mom_orb_live_compatibility import (
    Capture, EvidenceError, NEW, OLD, census, compare_components, compare_replays, digest,
    number, request_fields, session, stamp,
)


def capture_fixture(tmp_path, *, order_changes=None, fill_changes=None, account_name='live:orb'):
    """Explicit CONTROLLED rows exercise the delivered source-chunk envelope."""
    order = dict(id='order-1', intent_id='intent-1', broker_account_id='account-1',
                 strategy_id='strategy-1', client_order_id='controlled-entry',
                 broker_order_id='controlled-native', symbol='CONTROL', side='buy',
                 quantity='10', order_type='limit', time_in_force='day', status='filled',
                 submitted_at='2026-10-13T13:31:00Z', payload={'limit_price': '2.00'})
    order.update(order_changes or {})
    fill = dict(id='fill-1', order_id='order-1', broker_account_id='account-1',
                strategy_id='strategy-1', broker_fill_id='controlled-fill', symbol='CONTROL',
                side='buy', quantity='10', price='2.00', filled_at='2026-10-13T13:31:01Z', payload={})
    fill.update(fill_changes or {})
    rows = {
        'broker_accounts': [dict(id='account-1', name=account_name, provider='webull', external_account_id=None)],
        'strategies': [dict(id='strategy-1', code='momentum_warn_live', execution_mode='live')],
        'broker_orders': [order], 'fills': [fill],
        'trade_intents': [dict(id='intent-1', broker_account_id='account-1', strategy_id='strategy-1',
                               symbol='CONTROL', side='buy', intent_type='open', reason='CONTROLLED')],
    }
    manifest = dict(version=2, snapshot_policy='ONE_READONLY_REPEATABLE_READ_FULL_PK_NO_OTHER_PREDICATES',
                    snapshot_id='CONTROLLED_NOT_NATIVE', asof_ms=1792281600000, tables={})
    for table, items in rows.items():
        body = dict(table=table, sequence=1, previous_sha256='0' * 64,
                    snapshot_id=manifest['snapshot_id'], asof_ms=manifest['asof_ms'], rows=items)
        raw = json.dumps(body).encode()
        reference = table + '.json'
        (tmp_path / reference).write_bytes(raw)
        end = dict(table=table, snapshot_id=manifest['snapshot_id'], asof_ms=manifest['asof_ms'],
                   rows=[], after_key=[items[-1]['id']])
        end_raw = json.dumps(end).encode()
        (tmp_path / (table + '-END.json')).write_bytes(end_raw)
        manifest['tables'][table] = dict(source_state='PRESENT_FULL_EXHAUSTED', exact_export_rows=len(items),
            exact_export_bytes=len(raw), chunks=[dict(reference=reference, bytes=len(raw),
                sha256=digest(raw), rows=len(items), first_key=[items[0]['id']], last_key=[items[-1]['id']])],
            end_probe=dict(reference=table + '-END.json', bytes=len(end_raw), sha256=digest(end_raw)))
    path = tmp_path / 'manifest.json'
    path.write_text(json.dumps(manifest))
    return path, manifest


def load(path):
    return Capture(path, path.parent, expected_sha=digest(path.read_bytes()))


def test_complete_controlled_capture_preserves_source_fields_without_native_claim(tmp_path):
    path, _ = capture_fixture(tmp_path)
    result = census(load(path))
    assert result['orders'] == result['fills'] == result['linked_fills'] == 1
    assert result['source_table_counts']['broker_orders'] == 1
    assert result['cases'][0]['order']['payload'] == {'limit_price': '2.00'}
    assert result['cases'][0]['source_fill_pointers']['fill-1']['row_ordinal_1based'] == 1
    assert result['cases'][0]['physical_account_equivalence'] == 'UNKNOWN'
    assert result['historical_replay_qualified'] is False


@pytest.mark.parametrize('fault', ['raw_bytes', 'previous', 'snapshot', 'asof', 'sequence', 'end_key', 'count'])
def test_capture_refuses_changed_chain_scope_and_membership(tmp_path, fault):
    path, manifest = capture_fixture(tmp_path)
    table = manifest['tables']['broker_orders']
    part = tmp_path / 'broker_orders.json'
    if fault == 'raw_bytes':
        part.write_bytes(part.read_bytes() + b' ')
    elif fault == 'count':
        table['exact_export_rows'] = 2
    else:
        spec = table['end_probe'] if fault == 'end_key' else table['chunks'][0]
        part = tmp_path / spec['reference']
        body = json.loads(part.read_bytes())
        field, value = {'previous': ('previous_sha256', 'f' * 64),
                        'snapshot': ('snapshot_id', 'OTHER'), 'asof': ('asof_ms', 1),
                        'sequence': ('sequence', 2), 'end_key': ('after_key', ['foreign'])}[fault]
        body[field] = value
        raw = json.dumps(body).encode()
        part.write_bytes(raw)
        spec.update(bytes=len(raw), sha256=digest(raw))
        if fault != 'end_key':
            table['exact_export_bytes'] = len(raw)
    path.write_text(json.dumps(manifest))
    with pytest.raises(EvidenceError):
        list(load(path).rows('broker_orders'))


@pytest.mark.parametrize('field,value', [('order_id', 'orphan'), ('side', 'sell'), ('symbol', 'FOREIGN')])
def test_mismatched_real_shape_fill_is_not_dropped_from_denominator(tmp_path, field, value):
    path, _ = capture_fixture(tmp_path, fill_changes={field: value})
    result = census(load(path))
    assert result['fills'] == 1 and result['linked_fills'] == 0
    assert len(result['unbound_fills']) == 1 and result['identity_issues']
    assert result['historical_replay_qualified'] is False


def test_pre_window_order_with_in_window_fill_keeps_parent(tmp_path):
    path, _ = capture_fixture(tmp_path, order_changes={'submitted_at': '2026-10-10T13:00:00Z'})
    result = census(load(path))
    assert result['orders'] == result['fills'] == 1
    assert result['cases'][0]['order']['submitted_at'] == '2026-10-10T13:00:00Z'


@pytest.mark.parametrize('at', [None, 'not-a-clock', '2026-11-01T14:00:00Z'])
def test_uncertain_order_time_never_proves_absence(tmp_path, at):
    path, _ = capture_fixture(tmp_path, order_changes={'submitted_at': at})
    result = census(load(path))
    assert result['orders'] == 1 and result['uncertain_membership']


def test_paper_account_does_not_become_live_merely_from_strategy_code(tmp_path):
    path, _ = capture_fixture(tmp_path, account_name='paper:orb')
    result = census(load(path))
    assert result['orders'] == result['fills'] == 0
    assert result['historical_replay_qualified'] is False


@pytest.mark.parametrize('at,expected', [
    ('2026-10-13T07:59:59Z', 'OUTSIDE_0400_2000'),
    ('2026-10-13T08:00:00Z', 'PRE_0400_0930'),
    ('2026-10-13T13:29:59Z', 'PRE_0400_0930'),
    ('2026-10-13T13:30:00Z', 'RTH_0930_1600'),
    ('2026-10-13T20:00:00Z', 'AH_1600_2000'),
    ('2026-10-14T00:00:00Z', 'OUTSIDE_0400_2000'),
])
def test_sessions_are_et_and_boundaries_are_not_rounded(at, expected):
    assert session(at) == expected


@pytest.mark.parametrize('value', [True, 2.1, 'NaN', 'Infinity'])
def test_money_requires_exact_finite_values(value):
    with pytest.raises(EvidenceError):
        number(value)


def components():
    data = dict(manifest_sha256='sealed', cases=[{'order': {'id': 'order-1'}}])
    row = dict(order_id='order-1', status='CAPTURED_REQUEST_FORMATTED_NOT_DISPATCHED',
               side='buy', quantity='10', wire={'limit': '2.00'}, fills_reproduced=False)
    old = dict(schema='MOM_ORB_FORMATTER_COMPONENT_V1', head=OLD,
               manifest_sha256='sealed', census_sha256='same-input', rows=[row])
    new = deepcopy(old)
    new['head'] = NEW
    return data, old, new


def test_same_adapter_copy_is_never_historical_fill_or_decision_pass():
    data, old, new = components()
    result = compare_components(data, old, new, 'same-input')
    assert result['changed_rows'] == result['unknown_rows'] == []
    assert result['historical_orders_replayed'] == result['historical_fills_reproduced'] == 0
    assert result['historical_replay_qualified'] is False


@pytest.mark.parametrize('field,value', [('quantity', '11'), ('side', 'sell'), ('wire', {'limit': '2.01'})])
def test_changed_fields_are_reported_with_both_exact_rows(field, value):
    data, old, new = components()
    new['rows'][0][field] = value
    result = compare_components(data, old, new, 'same-input')
    assert len(result['changed_rows']) == 1
    assert result['changed_rows'][0]['old'][field] != result['changed_rows'][0]['new'][field]


def test_unknown_same_on_both_sides_is_not_counted_as_proven_unchanged():
    data, old, new = components()
    old['rows'][0]['status'] = new['rows'][0]['status'] = 'UNKNOWN'
    result = compare_components(data, old, new, 'same-input')
    assert len(result['unknown_rows']) == 1 and result['historical_replay_qualified'] is False


@pytest.mark.parametrize('fault', ['head', 'census_sha', 'manifest_sha', 'missing', 'duplicate', 'census_duplicate'])
def test_comparison_requires_same_input_and_exact_complete_denominator(fault):
    data, old, new = components()
    if fault == 'head':
        new['head'] = OLD
    elif fault == 'census_sha':
        new['census_sha256'] = 'different'
    elif fault == 'manifest_sha':
        new['manifest_sha256'] = 'different'
    elif fault == 'missing':
        new['rows'] = []
    elif fault == 'duplicate':
        new['rows'] *= 2
    else:
        data['cases'] *= 2
    with pytest.raises(EvidenceError):
        compare_components(data, old, new, 'same-input')


def test_request_projection_uses_actual_parent_and_does_not_change_metadata(tmp_path):
    path, _ = capture_fixture(tmp_path)
    case = census(load(path))['cases'][0]
    before = deepcopy(case)
    request = request_fields(case)
    assert str(request['quantity']) == '10' and request['metadata'] == {'limit_price': '2.00'}
    assert case == before
    case['intent']['side'] = 'sell'
    with pytest.raises(EvidenceError):
        request_fields(case)


def replay_receipts():
    fill = dict(id='fill-1', order_id='order-1', side='buy', symbol='CONTROL',
                quantity='10', price='2.00', filled_at='2026-10-13T13:31:01Z')
    data = dict(cases=[dict(order={'id': 'order-1'}, fills=[fill], source_order_pointer={'part': 'CONTROLLED'})])
    row = dict(order_id='order-1', unknown_inputs=[], status='SENT', driver_trace_sha256='controlled',
        side='buy', quantity='10', price='2.00', client_order_id='controlled-entry',
        decision_at='2026-10-13T13:31:00Z', bar_seconds=60,
        bar_duration_provenance='RETAINED_STRATEGY_PROFILE',
        fill_reconstruction_basis='ORIGINAL_CALLBACKS_RETAINED_REPORTS', fills=[deepcopy(fill)])
    old = dict(schema='MOM_ORB_ORIGINAL_DRIVER_RECEIPT_V1', head=OLD,
        census_sha256='controlled-same', input_manifest_sha256='controlled-inputs',
        source_inventory_sha256='controlled-source', mode='RETAINED_HISTORY', rows=[row])
    new = deepcopy(old)
    new['head'] = NEW
    return data, old, new


def test_driver_receipt_equality_still_requires_main_input_trace_review():
    data, old, new = replay_receipts()
    result = compare_replays(data, old, new, 'controlled-same')
    assert result['matched_output_rows_pending_input_review'] == 1
    assert result['historical_replay_qualified'] is False


@pytest.mark.parametrize('field,value', [('side', 'sell'), ('quantity', '11'), ('price', '2.01')])
def test_replay_changed_order_fields_are_literal_counterexamples(field, value):
    data, old, new = replay_receipts()
    new['rows'][0][field] = value
    result = compare_replays(data, old, new, 'controlled-same')
    assert result['changed_rows'] and result['matched_output_rows_pending_input_review'] == 0


@pytest.mark.parametrize('seconds,changed', [(60, False), (61, True)])
def test_one_retained_bar_tolerance_is_exact_not_an_arbitrary_clock_waiver(seconds, changed):
    data, old, new = replay_receipts()
    new['rows'][0]['decision_at'] = '2026-10-13T13:32:' + ('00' if seconds == 60 else '01') + 'Z'
    result = compare_replays(data, old, new, 'controlled-same')
    assert bool(result['changed_rows']) is changed
    assert result['historical_replay_qualified'] is False


@pytest.mark.parametrize('fault', ['controlled_mode', 'unknown_owner', 'missing_fills', 'changed_fill'])
def test_recorded_fills_cannot_be_forced_or_old_new_absence_called_reproduction(fault):
    data, old, new = replay_receipts()
    if fault == 'controlled_mode':
        new['mode'] = 'TAPE_CONTROLLED_CLOCK'
    elif fault == 'unknown_owner':
        new['rows'][0]['unknown_inputs'] = ['historical_owner_asof_absent']
    elif fault == 'missing_fills':
        old['rows'][0]['fills'] = new['rows'][0]['fills'] = []
    else:
        new['rows'][0]['fills'][0]['quantity'] = '11'
    result = compare_replays(data, old, new, 'controlled-same')
    assert result['changed_rows'] or result['unknown_rows']
    assert result['matched_output_rows_pending_input_review'] == 0


def test_submicrosecond_clock_not_silently_truncated():
    with pytest.raises(EvidenceError):
        stamp('2026-10-13T13:31:00.000000001Z')
