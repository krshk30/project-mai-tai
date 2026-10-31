"""CONTROLLED PAGE v3 evidence/display contracts; no actual economic proof."""

from copy import deepcopy
import json

import pytest
from starlette.responses import HTMLResponse

from project_mai_tai.atr_simulation import today_display, today_file
from project_mai_tai.services import control_plane as cp
from tests.unit.test_atrsimlive1_today_page import document, NOW


def percent_document(*, sized=False):
    data = document()
    for key, table in data["tables"].items():
        if key == "LIVE:ACTUAL":
            continue
        table["rows"][0]["row"].update(
            quantity="150" if sized else "UNKNOWN",
            pnl="15.000000000000000001" if sized else None,
            pnl_label_known=sized, pnl_label="$+15.00" if sized else "UNKNOWN",
            pnl_pct="5.00000000000000000001", pnl_pct_label_known=True,
            pnl_pct_label="+5.0%", sizing_status="ROUTE_PROVEN_SIZED" if sized else "UNKNOWN_ROUTE_UNITS",
            percent_basis="Sized price return" if sized else "Normalized price return; units unresolved")
    return data


def read(data, tmp_path):
    path = tmp_path / "current.json"
    path.write_text(json.dumps(data, ensure_ascii=True), encoding="utf-8")
    return today_file.read_today_file(path, now=NOW)


def render(context):
    return HTMLResponse(today_display.render_today_page(
        context, render_row=cp._render_completed_position_row)).body.decode("utf-8")


@pytest.mark.parametrize("sized", [False, True])
def test_percent_is_primary_and_dollars_secondary_without_mutating_canonical_rows(tmp_path, sized):
    data = percent_document(sized=sized)
    before = deepcopy(data)
    context = read(data, tmp_path)
    snapshot = deepcopy(context)
    html = render(context)
    assert context["tables"]["FIRSTBAR"]["display_status"] == "AVAILABLE"
    assert "+5.0% (USD " + ("$+15.00" if sized else "UNKNOWN") + ")" in html
    assert context["tables"]["FIRSTBAR"]["rows"][0]["pnl_pct"] == "5.00000000000000000001"
    assert context["tables"]["FIRSTBAR"]["rows"][0]["pnl"] == before["tables"]["FIRSTBAR"]["rows"][0]["row"]["pnl"]
    if sized:
        assert "ROUTE_PROVEN_SIZED" in html
    else:
        assert "Normalized price return; units unresolved" in html
    assert context == snapshot and data == before
    assert html.count('data-sim-mode="') == 7 and html.count("<td") == 63


@pytest.mark.parametrize("change", [
    {"pnl_pct": None}, {"pnl_pct": "NaN"}, {"pnl_pct": "Infinity"},
    {"pnl_pct": "1e400"}, {"pnl_pct": True}, {"pnl_pct": 5},
    {"pnl_pct_label_known": 1}, {"pnl_pct_label_known": False},
    {"pnl_pct_label": "UNKNOWN"}, {"percent_basis": ""},
    {"percent_basis": "\ud800"}, {"sizing_status": "x"*2049},
    {"sizing_status": "UNKNOWN\nROUTE"}, {"quantity": "1.5"},
    {"quantity": "١٥٠"}, {"pnl_label_known": True, "pnl": "0", "pnl_label": "$0.00"},
])
def test_invalid_percent_or_unknown_units_with_dollars_refuses_only_affected_table(tmp_path, change):
    data = percent_document()
    data["tables"]["FIRSTBAR"]["rows"][0]["row"].update(change)
    context = read(data, tmp_path)
    assert context["tables"]["FIRSTBAR"]["display_status"] == "INVALID_TABLE"
    assert context["tables"]["FIRSTBAR"]["rows"] == []
    assert context["tables"]["FIRSTBAR_X3"]["display_status"] == "AVAILABLE"
    assert "INVALID_TABLE" in render(context)


@pytest.mark.parametrize("field", ["pnl_pct", "pnl_pct_label_known", "sizing_status", "percent_basis"])
def test_new_known_percent_requires_complete_explicit_contract(tmp_path, field):
    data = percent_document()
    del data["tables"]["FIRSTBAR"]["rows"][0]["row"][field]
    assert read(data, tmp_path)["tables"]["FIRSTBAR"]["display_status"] == "INVALID_TABLE"


def test_percent_label_alone_is_refused_not_inferred_from_dollars(tmp_path):
    data = document()
    data["tables"]["FIRSTBAR"]["rows"][0]["row"].update(
        pnl="15", pnl_label_known=True, pnl_label="$+15.00", pnl_pct_label="+5.0%")
    assert read(data, tmp_path)["tables"]["FIRSTBAR"]["display_status"] == "INVALID_TABLE"


def test_legacy_unknown_percent_and_explicit_unknown_new_percent_remain_unknown(tmp_path):
    data = percent_document()
    data["tables"]["FIRSTBAR"]["rows"][0]["row"].update(
        pnl_pct=None, pnl_pct_label_known=False, pnl_pct_label="UNKNOWN")
    data["tables"]["FIRSTBAR_X3"] = document()["tables"]["FIRSTBAR_X3"]
    context = read(data, tmp_path)
    assert context["tables"]["FIRSTBAR"]["display_status"] == "AVAILABLE"
    assert context["tables"]["FIRSTBAR_X3"]["display_status"] == "AVAILABLE"
    assert "UNKNOWN (USD UNKNOWN)" in render(context)


@pytest.mark.parametrize("note", ["x"*513, "bad\ud800", "bad\nline"])
def test_v3_coverage_note_is_bounded_plain_utf8(tmp_path, note):
    data = percent_document()
    data["tables"]["FIRSTBAR"]["rows"][0]["row"]["coverage_note"] = note
    assert read(data, tmp_path)["tables"]["FIRSTBAR"]["display_status"] == "INVALID_TABLE"


def test_real_panel_projection_and_actual_row_values_are_byte_identical(tmp_path):
    data = percent_document(sized=True)
    actual = data["tables"]["LIVE:ACTUAL"]["rows"][0]["row"]
    actual.update(quantity="7", pnl="0.7", pnl_label_known=True, pnl_label="$+0.70")
    context = read(data, tmp_path)
    seen = []

    def row_renderer(row):
        seen.append(deepcopy(row))
        return cp._render_completed_position_row(row)

    html = today_display.render_today_page(context, render_row=row_renderer)
    expected = {key: actual[key] for key in (*today_file.ROW_FIELDS, "pnl", "pnl_label_known", "coverage_note")}
    assert seen[3] == expected
    assert cp._render_completed_position_row(seen[3]).encode() == cp._render_completed_position_row(expected).encode()
    assert "$+0.70 (UNKNOWN)" in html and html.count("REAL RECORDED TRADES") == 1


def test_all_six_c1_comparisons_and_mismatches_visible_even_available(tmp_path):
    data = percent_document()
    refs = ["ELPW 09:02", "POM 10:51", "POM 11:44", "CHSN 12:20", "CHSN 13:31", "FTFT 15:32"]
    issues = ["C1-reported BUY " + ref + (" MISMATCH <literal>" if i == 5 else " within ±1 bar")
              for i, ref in enumerate(refs)]
    data["tables"]["FIRSTBAR"]["issues"] = issues
    context = read(data, tmp_path)
    html = render(context)
    visible = html.split('role="status">', 1)[1].split("</div>", 1)[0]
    assert all(ref in visible for ref in refs)
    assert "MISMATCH &lt;literal&gt;" in visible and "<literal>" not in html
    assert "line recomputed from stored bars" in html
    assert "actual live flip UNKNOWN unless matched by actual v2 log evidence" in html
    assert "C1-reported BUY flips, not native verification" in html
    assert 'type="date"' not in html and context["tables"]["FIRSTBAR"]["issues"] == issues


def test_sizing_basis_unicode_visible_and_markup_escaped(tmp_path):
    data = percent_document()
    data["tables"]["FIRSTBAR"]["rows"][0]["row"].update(
        sizing_status="UNKNOWN_ROUTE 🟢", percent_basis="Normalized <script>not HTML</script>")
    html = render(read(data, tmp_path))
    assert "UNKNOWN_ROUTE 🟢" in html and "&lt;script&gt;not HTML&lt;/script&gt;" in html
    assert "<script>not HTML</script>" not in html


@pytest.mark.parametrize("amount,label", [("0", "+0.0%"), ("-5.00000000001", "-5.0%")])
def test_explicit_zero_and_negative_percent_are_known_without_known_dollars(tmp_path, amount, label):
    data = percent_document()
    data["tables"]["FIRSTBAR"]["rows"][0]["row"].update(pnl_pct=amount, pnl_pct_label=label)
    context = read(data, tmp_path)
    assert context["tables"]["FIRSTBAR"]["display_status"] == "AVAILABLE"
    assert label + " (USD UNKNOWN)" in render(context)


def test_unused_nested_model_metadata_is_not_rendered_or_recursively_copied(tmp_path):
    data = percent_document()
    data["tables"]["FIRSTBAR"]["rows"][0]["row"]["unused_model"] = {"opaque": "\ud800"}
    context = read(data, tmp_path)
    assert context["tables"]["FIRSTBAR"]["display_status"] == "AVAILABLE"
    assert "unused_model" not in render(context)


def test_legacy_real_known_usd_and_percent_label_preserve_exact_row_and_html(tmp_path):
    data = percent_document()
    actual = data["tables"]["LIVE:ACTUAL"]["rows"][0]["row"]
    actual.update(quantity="7", pnl="0.70000000000000000001", pnl_label_known=True,
                  pnl_label="$+0.70", pnl_pct_label="+5.0%")
    original = deepcopy(actual)
    context = read(data, tmp_path)
    table = context["tables"]["LIVE:ACTUAL"]
    assert table["display_status"] == "AVAILABLE" and table["rows"] == [original]
    seen = []

    def row_renderer(row):
        seen.append(deepcopy(row))
        return cp._render_completed_position_row(row)

    html = today_display.render_today_page(context, render_row=row_renderer)
    assert seen[3] == original
    assert cp._render_completed_position_row(seen[3]).encode() == cp._render_completed_position_row(original).encode()
    assert "$+0.70 (+5.0%)" in html and actual == original
    assert "pnl_pct" not in table["rows"][0] and "pnl_pct_label_known" not in table["rows"][0]


@pytest.mark.parametrize("new_fields", [False, True])
def test_real_unknown_dollars_cannot_gain_known_percent_from_sim_contract(tmp_path, new_fields):
    data = percent_document()
    actual = data["tables"]["LIVE:ACTUAL"]["rows"][0]["row"]
    actual["pnl_pct_label"] = "+5.0%"
    if new_fields:
        actual.update(pnl_pct="5", pnl_pct_label_known=True,
                      sizing_status="UNKNOWN_ROUTE_UNITS", percent_basis="Normalized")
    context = read(data, tmp_path)
    assert context["tables"]["LIVE:ACTUAL"]["display_status"] == "INVALID_TABLE"
    assert context["tables"]["LIVE:ACTUAL"]["rows"] == []
    assert context["tables"]["FIRSTBAR"]["display_status"] == "AVAILABLE"
    assert "INVALID_TABLE" in render(context)


def test_legacy_real_600_character_plain_coverage_remains_byte_preserved(tmp_path):
    data = percent_document()
    actual = data["tables"]["LIVE:ACTUAL"]["rows"][0]["row"]
    actual["coverage_note"] = "R"*600
    original = deepcopy(actual)
    context = read(data, tmp_path)
    assert context["tables"]["LIVE:ACTUAL"]["display_status"] == "AVAILABLE"
    assert context["tables"]["LIVE:ACTUAL"]["rows"] == [original]
    assert "R"*600 in render(context) and actual == original


def test_sim_600_character_coverage_refused_without_changing_real_bound(tmp_path):
    data = percent_document()
    data["tables"]["FIRSTBAR"]["rows"][0]["row"]["coverage_note"] = "S"*600
    data["tables"]["LIVE:ACTUAL"]["rows"][0]["row"]["coverage_note"] = "R"*600
    context = read(data, tmp_path)
    assert context["tables"]["FIRSTBAR"]["display_status"] == "INVALID_TABLE"
    assert context["tables"]["FIRSTBAR"]["rows"] == []
    assert context["tables"]["LIVE:ACTUAL"]["display_status"] == "AVAILABLE"
    assert "R"*600 in render(context)
