"""Controlled snapshot boundary; no native, route, tape or financial proof."""
from copy import deepcopy
import pytest
from tests.unit.test_atrsimlive1_runtime import module, unavailable, NOW


def normalized_document():
    value = unavailable()
    row = dict(ticker="CONTROLLED", path="CONTROLLED", quantity="UNKNOWN", entry_time="UNKNOWN",
        entry_price="UNKNOWN", exit_time="UNKNOWN", exit_price="UNKNOWN", pnl=None,
        pnl_label="UNKNOWN", pnl_label_known=False, summary="CONTROLLED normalized price model",
        coverage_note="CONTROLLED coverage; actual native allocation UNKNOWN",
        pnl_pct="10", pnl_pct_label_known=True, pnl_pct_label="+10.00%",
        sizing_status="UNKNOWN_UNITS_NORMALIZED_ONE_UNIT_MODEL",
        percent_basis="normalized modeled price return; actual units unresolved")
    value['tables']['FIRSTBAR']['rows'] = [dict(identity=dict(trading_day='2026-10-28'),
                                                row=row, outcome_id='CONTROLLED')]
    return value


def test_normalized_percent_is_valid_without_claiming_known_units_or_usd():
    runtime = module('atrsimlive1_runtime')
    value = runtime.validate(normalized_document(), NOW)
    row = value['tables']['FIRSTBAR']['rows'][0]['row']
    assert row['pnl_pct_label_known'] and row['quantity'] == 'UNKNOWN'
    assert row['pnl'] is None and row['pnl_label_known'] is False


def test_prior_immutable_row_without_optional_fields_retains_old_contract():
    runtime = module('atrsimlive1_runtime')
    value = normalized_document()
    row = value['tables']['FIRSTBAR']['rows'][0]['row']
    for key in ('pnl_pct', 'pnl_pct_label_known', 'sizing_status', 'percent_basis'):
        row.pop(key)
    row['pnl_pct_label'] = 'UNKNOWN'
    assert runtime.validate(value, NOW) == value


@pytest.mark.parametrize('number', ['NaN', 'Infinity', '1e400', None, 'invalid'])
def test_nonfinite_or_unparseable_known_percentage_never_reaches_publication(number):
    runtime = module('atrsimlive1_runtime')
    value = normalized_document()
    value['tables']['FIRSTBAR']['rows'][0]['row']['pnl_pct'] = number
    with pytest.raises(ValueError, match='percent'):
        runtime.validate(value, NOW)


@pytest.mark.parametrize('change', ['flag_type', 'unknown_value', 'unknown_label',
                                   'label_mismatch', 'basis_control_character', 'claimed_units'])
def test_percent_knowledge_display_and_sizing_cannot_contradict_each_other(change):
    runtime = module('atrsimlive1_runtime')
    value = normalized_document()
    row = value['tables']['FIRSTBAR']['rows'][0]['row']
    if change == 'flag_type':
        row['pnl_pct_label_known'] = 1
    elif change == 'unknown_value':
        row.update(pnl_pct_label_known=False, pnl_pct_label='UNKNOWN')
    elif change == 'unknown_label':
        row.update(pnl_pct_label_known=False, pnl_pct=None)
    elif change == 'label_mismatch':
        row['pnl_pct_label'] = '+20.00%'
    elif change == 'basis_control_character':
        row['percent_basis'] += '\n'
    else:
        row['quantity'] = '28'
    with pytest.raises(ValueError):
        runtime.validate(value, NOW)
