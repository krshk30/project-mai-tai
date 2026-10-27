"""CONTROLLED presentation/provenance controls, not an engine or DB witness."""

from copy import deepcopy

import pytest

from project_mai_tai.atr_simulation.aggregate_entry_contract import (
    ENTRY_ASSUMPTION, EXIT_POLICY, LABEL, MODE, MODEL, VERSION, require_provenance,
)
from project_mai_tai.atr_simulation.aggregate_entry_display import CONTRACT, render_aggregate_entry_table


def provenance():
    return dict(mode=MODE, model=MODEL, version=VERSION, history_label=LABEL,
        entry_assumption=ENTRY_ASSUMPTION, exit_policy_version=EXIT_POLICY,
        partial_fill_timing="UNKNOWN", native_execution_qualified=False,
        producer_head="a" * 40, source_pins={"CONTROLLED-source.py": "b" * 64})


def view():
    return dict(contract=CONTRACT, mode=MODE, model=MODEL, version=VERSION,
        history_label=LABEL, read_status="UNKNOWN", rows=[], unknown_count=0,
        source_id="unconfigured", known_pnl_label="UNKNOWN", truncated=False,
        global_pnl_qualified=False)


def render(value, render_row=lambda row: "ROW"):
    return render_aggregate_entry_table(value, render_row=render_row,
        column_header="<tr><th>Same LIVE formatter columns</th></tr>")


def test_explicit_separate_source_binding_does_not_make_aggregate_exits_actual():
    row = provenance()
    assert require_provenance(row, producer_head="a" * 40,
        source_pins={"CONTROLLED-source.py": "b" * 64}) is row
    assert row["mode"] != "LIVE" and row["model"] != "ACTUAL"


@pytest.mark.parametrize("field,value", [("mode", "LIVE"), ("model", "ACTUAL"),
    ("history_label", "Actual fills"), ("partial_fill_timing", "KNOWN"),
    ("native_execution_qualified", True), ("producer_head", "c" * 40),
    ("source_pins", {"CONTROLLED-source.py": "d" * 64})])
def test_stored_claim_cannot_upgrade_native_or_replace_reviewed_source(field, value):
    row = provenance()
    row[field] = value
    with pytest.raises(ValueError):
        require_provenance(row, producer_head="a" * 40,
            source_pins={"CONTROLLED-source.py": "b" * 64})


def test_unavailable_table_keeps_unknown_instead_of_zero_or_native_total():
    value = view()
    before = deepcopy(value)
    html = render(value)
    assert LABEL in html and "UNKNOWN" in html
    assert "$0" not in html and "not actual realized" in html
    assert value == before


@pytest.mark.parametrize("defect", ["zero", "native", "rows"])
def test_unavailable_or_global_claim_is_refused(defect):
    value = view()
    if defect == "zero":
        value["known_pnl_label"] = "$0.00"
    elif defect == "native":
        value["global_pnl_qualified"] = True
    else:
        value["rows"] = [{}]
    with pytest.raises(ValueError):
        render(value)


def test_inert_formatter_receives_detached_rows_and_escaped_source_text():
    value = view()
    value.update(read_status="AVAILABLE", source_id="<script>CONTROLLED</script>")
    row = dict(ticker="CONTROLLED", path="orb:CONTROLLED", quantity="1",
        entry_time="UNKNOWN", entry_price="UNKNOWN", exit_time="UNKNOWN",
        exit_price="UNKNOWN", pnl_label="UNKNOWN", pnl_pct_label="UNKNOWN",
        summary="CONTROLLED", pnl_label_known=False, pnl=None)
    value["rows"] = [row]
    before = deepcopy(value)
    def formatter(copy):
        copy["ticker"] = "MUTATED"
        return "<tr><td>CONTROLLED formatted row</td></tr>"
    html = render(value, formatter)
    assert value == before
    assert "&lt;script&gt;" in html and "<script>" not in html
    assert "CONTROLLED formatted row" in html


@pytest.mark.parametrize("amount", ["NaN", True, "not-a-number"])
def test_nonfinite_boolean_or_invalid_known_modeled_money_is_refused(amount):
    value = view()
    value.update(read_status="AVAILABLE", known_pnl_label="$+1.00")
    value["rows"] = [dict(ticker="CONTROLLED", path="orb:CONTROLLED", quantity="1",
        entry_time="CONTROLLED", entry_price="$1", exit_time="CONTROLLED",
        exit_price="$2", pnl_label="$+1.00", pnl_pct_label="+100%", summary="CONTROLLED",
        pnl_label_known=True, pnl=amount)]
    with pytest.raises(ValueError, match="finite_aggregate_modeled_pnl"):
        render(value)
