"""UNRUN retained FIRST display projection; no model/replay/native acceptance.

FIRST rows/outcome IDs/labels are copied from the actual saved API; non-display
identity/allocation refs are redacted. Other modes in the composite are controlled.
"""

from collections import Counter
from copy import deepcopy
from datetime import UTC, datetime
from html import unescape
import json
from pathlib import Path
import re
from types import SimpleNamespace

import pytest

from project_mai_tai.atr_simulation import cp_sim_display as ui
from project_mai_tai.services import control_plane as cp
from tests.unit.test_l14_ui_default_compact import contexts
from tests.unit.test_atrpnl1 import fill, page_data


FIXTURE = Path(__file__).parents[1] / "fixtures/atr_simulation/l14_ui/actual_firstbar_140_trade_113_skip.json"
SOURCE_SHA = "387b4513d5fd91d1668f8f5b0a7bb74796a64e9b8dc8556bb084ecc4cfee6174"


def retained():
    return json.loads(FIXTURE.read_text())


def render(value=None):
    value = retained()["view"] if value is None else value
    first, aggregate, legacy = contexts()
    first["view"] = value
    return ui.render_page(first, aggregate, legacy, render_row=cp._render_completed_position_row,
        scopes=ui.page_scopes(SimpleNamespace(binding=SimpleNamespace(source_id=ui.AGGREGATE_SOURCE))))


def sections(html):
    return {mode: (attrs, content) for mode, attrs, content in re.findall(
        r'<section\b[^>]*data-sim-mode="([^"]+)"([^>]*)>(.*?)</section>', html, re.S)}


def cells(row):
    return [" ".join(unescape(re.sub(r"<[^>]+>", "", value)).split())
        for value in re.findall(r"<td\b[^>]*>(.*?)</td>", row, re.S)]


def test_actual_fixture_retains_140_trade_113_skip_and_exact_reason_population():
    packet = retained()
    assert packet["provenance"]["source_sha256"] == SOURCE_SHA
    view = packet["view"]
    assert len(view["outcomes"]) == 506
    for mode, amount in (("FIRSTBAR", "$+196.87"), ("FIRSTBAR_X3", "$+186.59")):
        outcomes = [item for item in view["outcomes"] if item["mode"] == mode]
        assert len({item["outcome_id"] for item in outcomes}) == 253
        assert Counter(item["kind"] for item in outcomes) == {"TRADE": 140, "SKIP": 113}
        assert Counter(item["reason"] for item in outcomes if item["kind"] == "SKIP") == {
            "SKIPPED_FALSE_FLIP": 80, "SKIPPED_NO_BREAK_BEFORE_BOUNDARY": 29,
            "SKIPPED_ENTRY_WINDOW_ENDED": 4}
        assert view["totals"][mode]["known_closed_trades"] == 140
        assert view["totals"][mode]["known_pnl_label"] == amount
        assert all("row" not in item and "accepted_quantity" not in item and "allocation_refs" not in item
            for item in outcomes if item["kind"] == "SKIP")


def test_all_actual_outcome_ids_and_kinds_have_rows_without_changing_trade_bytes():
    from tests.unit.test_l14_ui_compact_times import assert_only_sim_time_cells_changed
    view = retained()["view"]
    html = render(view)
    panels = sections(html)
    assert sum(len(re.findall(r"<tbody>(.*?)</tbody>", content, re.S)[0].split("</tr>")) - 1
        for _, content in panels.values()) == 549
    for table in ui.firstbar_tables(view):
        mode = table["mode"]
        attrs, content = panels[mode]
        metadata = json.loads(unescape(re.search(r'data-firstbar-outcomes="([^"]*)"', attrs)[1]))
        assert metadata == [{"outcome_id": item["outcome_id"], "kind": item["kind"], "model": item["model"],
            "identity": item["identity"]}
            for item in view["outcomes"] if item["mode"] == mode]
        assert len(metadata) == 253
        body = re.search(r"<tbody>(.*?)</tbody>", content, re.S)[1]
        # C12039 permits only compact time text/full titles; all other row bytes stay canonical.
        actual_rows = re.findall(r"<tr>.*?</tr>", body, re.S)
        assert len(actual_rows) == len(table["rows"]) == 140
        for actual, item in zip(actual_rows, table["rows"]):
            assert_only_sim_time_cells_changed(cp._render_completed_position_row(item["row"]), actual)
        assert 'data-firstbar-kind="SKIP"' not in body
        assert '<summary>113 signals skipped (no trade)</summary>' in content
        assert all(len(cells(row)) == 9 for row in re.findall(r"<tr\b[^>]*>.*?</tr>", body, re.S))


def test_skip_audit_is_collapsed_with_real_identities_reasons_and_no_execution_rows():
    view = retained()["view"]
    panels = sections(render(view))
    for mode in ("FIRSTBAR", "FIRSTBAR_X3"):
        _, content = panels[mode]
        rows = re.findall(r'<li data-firstbar-kind="SKIP"[^>]*>.*?</li>', content, re.S)
        assert len(rows) == 113 and '<details class="sim-outcome-audit">' in content
        assert '<details class="sim-outcome-audit" open' not in content
        outcomes = [item for item in view["outcomes"] if item["mode"] == mode and item["kind"] == "SKIP"]
        for outcome, row in zip(outcomes, rows):
            assert outcome["outcome_id"] in row
            text = unescape(re.sub(r"<[^>]+>", "", row))
            assert " / ".join(outcome["identity"][key] for key in (
                "trading_day", "symbol", "broker", "account_id", "strategy", "flip_id", "segment_id")) in text
            assert "SKIP: " + outcome["reason"] in text
            assert '<td' not in row and '$' not in row and 'P&L' not in row


def test_unknown_outcome_is_accounted_as_unresolved_not_a_trade_or_skip():
    view = retained()["view"]
    outcome = next(item for item in view["outcomes"] if item["kind"] == "SKIP")
    outcome.update(kind="UNKNOWN", reason="UNRESOLVED_RETAINED")
    content = sections(render(view))[outcome["mode"]][1]
    row = re.search(r'<li data-firstbar-kind="UNKNOWN"[^>]*>.*?</li>', content, re.S)[0]
    assert "UNKNOWN: UNRESOLVED_RETAINED" in row and '<td' not in row
    assert '<summary>112 signals skipped (no trade); 1 unresolved (UNKNOWN)</summary>' in content
    assert len(re.findall(r"<tr\b", re.search(r"<tbody>(.*?)</tbody>", content, re.S)[1])) == 140


@pytest.mark.parametrize("kind", ["TRADE", "SKIP"])
def test_duplicate_mode_outcome_id_is_refused_even_for_different_identities(kind):
    view = retained()["view"]
    selected = [item for item in view["outcomes"] if item["mode"] == "FIRSTBAR" and item["kind"] == kind]
    selected[1]["outcome_id"] = selected[0]["outcome_id"]
    with pytest.raises(ValueError, match="duplicate_firstbar_mode_outcome_id"):
        render(view)


@pytest.mark.parametrize("field", ["row", "accepted_quantity"])
def test_nontrade_cannot_smuggle_an_allocation_or_trade_row(field):
    view = retained()["view"]
    next(item for item in view["outcomes"] if item["kind"] == "SKIP")[field] = 1
    with pytest.raises(ValueError, match="firstbar_display_contract_required"):
        render(view)


def test_actual_view_and_supplied_totals_are_not_mutated_or_recomputed():
    view = retained()["view"]
    before = deepcopy(view)
    html = render(view)
    assert view == before and "+$196.87" in html and "+$186.59" in html
    assert html.count('data-firstbar-kind="SKIP"') == 226


def test_actual_mixed_population_keeps_nonSIM_html_bytes_exact(monkeypatch):
    monkeypatch.setattr(cp, "utcnow", lambda: datetime(2026, 10, 23, 20, tzinfo=UTC))
    data = page_data([fill("buy", 10, 4, "CONTROLLED-entry"),
        fill("sell", 10, 5, "CONTROLLED-entry-ocoexit-native")])
    before = deepcopy(data)
    fragment = render()
    baseline = cp._render_bot_detail_page(data, "schwab_1m_v2")
    mounted = cp._render_bot_detail_page(data, "schwab_1m_v2", firstbar_simulation_html=fragment)
    assert mounted.replace(fragment, "", 1).encode() == baseline.encode()
    assert data == before and "$+10.00" in baseline
