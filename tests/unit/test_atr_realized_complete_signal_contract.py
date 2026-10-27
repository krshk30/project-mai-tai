"""Actual4797 field contract, CONTROLLED only; all UNRUN."""

from copy import deepcopy

import pytest

from project_mai_tai.atr_simulation import realized_display as display
from tests.unit.test_atr_realized_display import controlled_receipt, prepare


PENDING = ('<section data-realized-reconciliation="true">'
           '<p>realized: under reconciliation</p></section>')


def future_result(*, unknown=True, all_unknown=False):
    result, binding = controlled_receipt(unknown=unknown, all_unknown=all_unknown)
    result.update(version="ATR_REALIZED_RECONCILIATION_V3_MANUAL_EXCLUSION_COMPLETE_SIGNALS",
        display_contract="COMPLETE_SIGNALS_ONLY_GROSS_FEES_NOT_INCLUDED_V1",
        complete_signal_count=result["matched_signals"], unreconciled_signal_count=result["unmatched_signals"],
        complete_signal_gross_pnl="0" if all_unknown else ("251.00" if unknown else "253.00"),
        known_partial_account_leg_gross_pnl="1.00" if unknown and not all_unknown else "0.00",
        known_partial_account_leg_count=1 if unknown and not all_unknown else 0)
    result["known_partial_account_legs"] = [leg for signal in result["signals"]
        if signal["status"] == "UNKNOWN" for leg in signal["legs"] if leg["status"] == "MATCHED"]
    return result, binding


def test_complete_signal_amount_is_not_the_known_leg_component():
    result, binding = future_result()
    before = deepcopy(result)
    view = prepare(result, binding).view()
    html = display.render_realized_display(view)
    assert result["matched_component_gross_pnl"] == "252.00"
    assert view["complete_signal_gross_pnl"] == "251.00"
    assert "Realized (fills): +$251.00 gross over 172 complete signals" in html
    assert "1 signals unreconciled" in html
    assert "252.00" not in html and "gross component" not in html
    assert "known_partial_account_leg_gross_pnl" not in view
    assert view["full_gross_pnl"] is None
    assert view["included_in_live_totals"] is view["native_execution_qualified"] is False
    assert "fees not included" in html and result == before


def test_all_complete_scope_has_explicit_signal_denominator():
    result, binding = future_result(unknown=False)
    view = prepare(result, binding).view()
    assert view["complete_signal_gross_pnl"] == view["full_gross_pnl"] == "253.00"
    html = display.render_realized_display(view)
    assert "Realized (fills): +$253.00 gross over 173 complete signals" in html
    assert "0 signals unreconciled" in html
    assert "fees not included" in html


def test_known_leg_without_any_complete_signal_stays_pending_not_zero():
    result, binding = future_result(all_unknown=True)
    # One explicit known leg in a signal whose companion leg is UNKNOWN.
    leg = result["account_legs"][0]
    leg.update(status="MATCHED", gross_pnl="1.00", issues=[],
               fill_ids=["CONTROLLED-entry", "CONTROLLED-exit"])
    result.update(matched_component_gross_pnl="1.00",
                  known_partial_account_leg_gross_pnl="1.00", known_partial_account_leg_count=1)
    result["known_partial_account_legs"] = [leg]
    view = prepare(result, binding).view()
    assert view["matched_signals"] == 0 and view["unmatched_signals"] == 173
    assert view["complete_signal_gross_pnl"] is view["full_gross_pnl"] is None
    assert display.render_realized_display(view) == PENDING


def test_old_v2_receipt_cannot_acquire_new_complete_signal_semantics():
    result, binding = future_result()
    result["version"] = "ATR_REALIZED_RECONCILIATION_V2_SCOPED_IDENTITIES"
    with pytest.raises(ValueError, match="completed_allocator_result_required"):
        prepare(result, binding)


@pytest.mark.parametrize("field", (
    "complete_signal_gross_pnl", "known_partial_account_leg_gross_pnl", "known_partial_account_leg_count"))
def test_missing_authoritative_field_never_falls_back_to_legacy_component(field):
    result, binding = future_result()
    del result[field]
    with pytest.raises(ValueError):
        prepare(result, binding)


@pytest.mark.parametrize("bad_count", (True, 0))
def test_known_partial_account_leg_count_is_exact_not_truthy_or_silent_zero(bad_count):
    result, binding = future_result()
    result["known_partial_account_leg_count"] = bad_count
    with pytest.raises(ValueError, match="partial_known_leg_count_mismatch"):
        prepare(result, binding)


def test_nonzero_partial_subtotal_with_no_partial_legs_refuses():
    result, binding = future_result(unknown=False)
    result["known_partial_account_leg_gross_pnl"] = "1.00"
    with pytest.raises(ValueError, match="no_partial_legs_requires_zero"):
        prepare(result, binding)


def test_unconfigured_future_consumer_remains_pending_without_opening_a_receipt(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("no artifact configuration")
    monkeypatch.setattr(display.os, "open", forbidden)
    assert display.configured_realized_display(None, None, aggregate_binding=None) is None
    assert display.render_realized_display(None) == PENDING


def test_legacy_display_status_cannot_relabel_an_amount_as_complete_signals():
    view = display.unknown_realized_view()
    view.update(status="BOUND_RECONCILED_COMPONENT", receipt_sha256="a"*64,
                component_gross_pnl="-777.125", complete_signal_gross_pnl="-999.875")
    assert display.render_realized_display(view) == PENDING


@pytest.mark.parametrize("field", ("complete_signal_count", "unreconciled_signal_count"))
def test_authoritative_count_alias_cannot_disagree_with_signal_membership(field):
    result, binding = future_result()
    result[field] = True
    with pytest.raises(ValueError, match="complete_signal_denominator_mismatch"):
        prepare(result, binding)


def test_partial_member_list_cannot_substitute_a_known_leg_from_a_complete_signal():
    result, binding = future_result()
    result["known_partial_account_legs"] = [result["account_legs"][2]]
    with pytest.raises(ValueError, match="partial_known_leg_membership_mismatch"):
        prepare(result, binding)


@pytest.mark.parametrize("field,reason", (
    ("complete_signal_gross_pnl", "complete_signal_gross_mismatch"),
    ("known_partial_account_leg_gross_pnl", "partial_known_leg_gross_mismatch"),
    ("matched_component_gross_pnl", "known_leg_audit_bridge_mismatch"),
))
def test_declared_gross_fields_must_agree_with_bound_row_amounts(field, reason):
    result, binding = future_result()
    result[field] = "999.00"
    with pytest.raises(ValueError, match=reason):
        prepare(result, binding)


def test_missing_display_contract_is_not_inferred_from_amount_fields():
    result, binding = future_result()
    del result["display_contract"]
    with pytest.raises(ValueError, match="completed_allocator_result_required"):
        prepare(result, binding)


def test_raw_zero_without_complete_signals_does_not_authorize_a_visible_total():
    result, binding = future_result(all_unknown=True)
    assert result["complete_signal_gross_pnl"] == "0"
    view = prepare(result, binding).view()
    assert view["complete_signal_gross_pnl"] is None
    assert display.render_realized_display(view) == PENDING
