"""New inert presentation controls, not allocated-result or 173-case proof."""

from copy import deepcopy
from html import escape

import pytest

from project_mai_tai.atr_simulation.firstbar_display import (
    COLUMN_HEADER,
    COLUMNS,
    DISPLAY_CONTRACT,
    ROW_FIELDS,
    firstbar_tables,
    render_firstbar_tables,
)


def _identity(account="account-a"):
    return {"trading_day": "2026-10-23", "symbol": "EXAMPLE", "broker": "schwab",
            "account_id": account, "strategy": "schwab_1m_v2", "flip_id": "flip-a",
            "segment_id": "segment-a"}


def _trade(mode="FIRSTBAR", *, account="account-a"):
    return {"mode": mode, "model": "CONTROLLED-model", "identity": _identity(account), "outcome_id": "outcome-a",
            "kind": "TRADE", "trade_id": "controlled-trade-a", "accepted_quantity": 100,
            "allocation_refs": ["CONTROLLED:allocated-fill-a"], "status": "CLOSED",
            "row": {"ticker": "EXAMPLE", "path": "Schwab", "quantity": "100",
                    "entry_time": "2026-10-23 10:01:00 ET", "entry_price": "$3.00",
                    "exit_time": "2026-10-23 10:02:00 ET", "exit_price": "$3.15",
                    "pnl": "15.00", "pnl_label": "$+15.00", "pnl_pct_label": "+5.0%",
                    "pnl_label_known": True, "summary": "TARGET_5 / controlled raw reason"}}


def _view():
    return {"contract": DISPLAY_CONTRACT, "source": {"source_id": "CONTROLLED-source",
            "policy_version": "CONTROLLED-policy", "engine_dependency_head": "CONTROLLED-head",
            "population_label": "replay, in-sample; CONTROLLED, not 173-case acceptance"},
            "truncated": False, "coverage_status": "UNKNOWN", "outcomes": [_trade()],
            "totals": {"FIRSTBAR": {"known_pnl_label": "$+15.00", "known_closed_trades": 1,
                                    "complete_requested_window": False},
                       "FIRSTBAR_X3": {"known_pnl_label": "UNKNOWN", "known_closed_trades": 0,
                                       "complete_requested_window": False}}}


def test_columns_and_copied_trade_fields_match_live_port_without_economic_recalculation():
    view = _view()
    result = firstbar_tables(view)
    assert COLUMNS == ("Ticker", "Path", "Qty", "Entry Time", "Entry", "Exit Time", "Exit", "P&L", "Exit Summary")
    assert result[0]["rows"][0]["row"] == view["outcomes"][0]["row"]
    assert result[0]["totals"] == view["totals"]["FIRSTBAR"]
    assert result[1]["mode"] == "FIRSTBAR_X3"


def test_two_worlds_keep_same_trade_and_both_accounts_without_cross_account_dedup():
    view = _view()
    view["outcomes"] += [_trade(account="account-b"), _trade("FIRSTBAR_X3")]
    view["totals"]["FIRSTBAR_X3"] = deepcopy(view["totals"]["FIRSTBAR"])
    tables = firstbar_tables(view)
    assert [len(table["rows"]) for table in tables] == [2, 1]
    assert [row["identity"]["account_id"] for row in tables[0]["rows"]] == ["account-a", "account-b"]


def test_planner_candidate_is_not_an_allocated_trade_row():
    view = _view()
    view["outcomes"][0]["kind"] = "ENTRY_CANDIDATE"
    with pytest.raises(ValueError, match="firstbar_display_contract_required"):
        firstbar_tables(view)


def test_skip_footer_and_unknown_population_are_not_trade_rows_or_zero_pnl():
    view = _view()
    view["outcomes"] += [
        {"mode": "FIRSTBAR", "model": "CONTROLLED-model", "identity": _identity(), "outcome_id": "skip-a",
         "kind": "SKIP", "reason": "SKIPPED_FALSE_FLIP"},
        {"mode": "FIRSTBAR", "model": "CONTROLLED-model", "identity": _identity(), "outcome_id": "unknown-a", "kind": "UNKNOWN"}]
    table = firstbar_tables(view)[0]
    assert len(table["rows"]) == 1
    assert table["skip_counts"] == {"SKIPPED_FALSE_FLIP": 1}
    assert table["unknown_count"] == 1
    assert firstbar_tables(view)[1]["totals"]["known_pnl_label"] == "UNKNOWN"


@pytest.mark.parametrize("status", ["OPEN", "UNKNOWN"])
def test_filled_open_or_unknown_trade_remains_visible_with_unknown_economics(status):
    view = _view()
    trade = view["outcomes"][0]
    trade["status"] = status
    trade["row"].update(pnl=None, pnl_label="UNKNOWN", pnl_pct_label="UNKNOWN", pnl_label_known=False,
                        exit_time="UNKNOWN", exit_price="UNKNOWN", summary=status)
    view["totals"]["FIRSTBAR"].update(known_closed_trades=0, known_pnl_label="UNKNOWN")
    row = firstbar_tables(view)[0]["rows"][0]
    assert row["status"] == status and row["accepted_quantity"] == 100
    assert row["row"]["pnl_label"] == "UNKNOWN"


@pytest.mark.parametrize("mutation", ["truncated", "coverage", "open", "count"])
def test_incomplete_population_cannot_claim_full_window_totals(mutation):
    view = _view()
    view["coverage_status"] = "COMPLETE"
    view["totals"]["FIRSTBAR"]["complete_requested_window"] = True
    if mutation == "truncated":
        view["truncated"] = True
    elif mutation == "coverage":
        view["coverage_status"] = "UNKNOWN"
    elif mutation == "open":
        view["outcomes"][0]["status"] = "OPEN"
    else:
        view["totals"]["FIRSTBAR"].update(known_closed_trades=0, known_pnl_label="UNKNOWN")
    with pytest.raises(ValueError, match="firstbar_display_contract_required"):
        firstbar_tables(view)


def test_duplicate_trade_with_different_outcome_id_is_refused():
    view = _view()
    duplicate = deepcopy(view["outcomes"][0])
    duplicate["outcome_id"] = "different-wrapper-same-trade"
    view["outcomes"].append(duplicate)
    with pytest.raises(ValueError, match="firstbar_display_contract_required"):
        firstbar_tables(view)


@pytest.mark.parametrize("field,value", [("accepted_quantity", 0), ("accepted_quantity", True),
                                       ("allocation_refs", []), ("mode", "VOLBODY")])
def test_missing_allocation_or_wrong_world_cannot_be_projected_as_firstbar_trade(field, value):
    view = _view()
    view["outcomes"][0][field] = value
    with pytest.raises(ValueError, match="firstbar_display_contract_required"):
        firstbar_tables(view)


def test_renderer_reuses_detached_live_formatters_and_escapes_new_metadata():
    view = _view()
    view["source"]["source_id"] = "<untrusted-source>"
    view["outcomes"][0]["issues"] = ["RAW_<ISSUE>"]
    snapshot = deepcopy(view)
    rows, totals = [], []

    def render_row(row):
        rows.append(deepcopy(row))
        row["summary"] = "caller-mutated-detached-copy"
        return "<tr>" + "".join("<td>" + escape(row[key]) + "</td>" for key in ROW_FIELDS) + "</tr>"

    def render_totals(total):
        totals.append(deepcopy(total))
        total["known_closed_trades"] = 999
        return "<p>" + escape(total["known_pnl_label"]) + "</p>"

    html = render_firstbar_tables(view, render_row=render_row, render_totals=render_totals)
    assert view == snapshot
    assert rows == [snapshot["outcomes"][0]["row"]]
    assert len(totals) == 2
    assert html.count(COLUMN_HEADER) == 2
    assert "&lt;untrusted-source&gt;" in html and "<untrusted-source>" not in html
    assert "SIMULATION - NOT REAL TRADES" in html and "replay, in-sample" in html
    assert "First-bar-high + 2-min" in html and "not a global total" in html
    assert "RAW_&lt;ISSUE&gt;" in html


def test_display_limit_is_distinct_from_known_page_totals_or_source_truncation():
    view = _view()
    view["outcomes"] = []
    for index in range(27):
        trade = _trade()
        trade.update(outcome_id=f"outcome-{index}", trade_id=f"trade-{index}")
        view["outcomes"].append(trade)
    view["totals"]["FIRSTBAR"].update(known_closed_trades=27, known_pnl_label="CONTROLLED total")
    rendered, totals = [], []

    def render_row(row):
        rendered.append(row)
        return "<tr><td>controlled row</td></tr>"

    def render_totals(total):
        totals.append(total)
        return ""

    html = render_firstbar_tables(view, render_row=render_row, render_totals=render_totals)
    assert len(rendered) == 25
    assert totals[0]["known_closed_trades"] == 27
    assert "shown 25 of 27 trade rows in known page" in html
    assert "truncated false" in html
