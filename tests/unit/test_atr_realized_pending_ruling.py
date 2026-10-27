"""C1 17:59 display controls, CONTROLLED receipts only; all UNRUN."""

from copy import deepcopy
import hashlib
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from project_mai_tai.atr_simulation import realized_display as display
from project_mai_tai.services import atr_aggregate_routes as routes
from project_mai_tai.settings import Settings
from tests.unit.test_atr_realized_display import controlled_receipt, encoded, prepare


PENDING_HTML = ('<section data-realized-reconciliation="true">'
                '<p>realized: under reconciliation</p></section>')


@pytest.mark.parametrize("state", ("none", "unknown_with_amounts", "missing_receipt"))
def test_unreviewed_state_cannot_display_a_component_full_total_or_zero(state):
    value = None if state == "none" else display.unknown_realized_view()
    if value is not None:
        value.update(component_gross_pnl="-777.125", complete_signal_gross_pnl="-888.500",
                     full_gross_pnl="-999.875",
                     matched_signals=173, unmatched_signals=0)
        if state == "missing_receipt":
            value["status"] = "BOUND_COMPLETE_SIGNAL_GROSS"
    original = deepcopy(value)
    assert display.render_realized_display(value) == PENDING_HTML
    assert value == original


@pytest.mark.parametrize("unknown", (True, False), ids=("partial", "full"))
@pytest.mark.parametrize("recorded_fees", (False, True), ids=("no_fee_fields", "fee_semantics_unknown"))
def test_future_explicit_external_receipt_copies_reviewed_gross_with_fees_not_included(
        tmp_path, unknown, recorded_fees):
    result, binding = controlled_receipt(unknown=unknown)
    if recorded_fees:
        result.update(fee_key_census={"$.commission": 2},
                      fees_status="RECORDED_FIELDS_REQUIRE_SEMANTIC_REVIEW")
    original = deepcopy(result)
    raw = encoded(result)
    path = tmp_path / "CONTROLLED-future-reviewed-reconciliation.json"
    path.write_bytes(raw)
    path.chmod(0o600)
    # Main's independent approved digest is the configuration operand. This
    # fixture is NOT the actual a507 receipt and conveys no financial approval.
    bound = display.configured_realized_display(str(path), hashlib.sha256(raw).hexdigest(),
                                               aggregate_binding=binding)
    before_view = encoded(bound.view())
    html = display.render_realized_display(bound.view())
    assert ("Realized (fills): +$251.00 gross over 172 complete signals" if unknown
            else "Realized (fills): +$253.00 gross over 173 complete signals") in html
    assert "fees not included" in html
    assert result["fees_status"] in html
    assert "never added to LIVE daily totals" in html
    assert "realized: under reconciliation" not in html
    assert encoded(bound.view()) == before_view
    assert result == original and path.read_bytes() == raw


def test_unconfigured_get_routes_keep_api_null_and_html_pending_without_storage_read():
    _, binding = controlled_receipt()
    reads = []
    def forbidden(*args, **kwargs):
        reads.append((args, kwargs))
        raise AssertionError("unselected storage must not read")
    port = routes.AggregateSimulationReadPort(SimpleNamespace(read=forbidden), binding)
    app = FastAPI()
    app.include_router(routes.build_aggregate_router(port, render_row=forbidden))
    settings = Settings(_env_file=None)
    assert settings.atr_simulation_realized_receipt_path is None
    assert settings.atr_simulation_realized_receipt_sha256 is None
    with TestClient(app) as client:
        api = client.get("/atr-aggregate/api")
        page = client.get("/atr-aggregate")
    assert api.status_code == page.status_code == 200
    assert api.headers["cache-control"] == page.headers["cache-control"] == "no-store"
    value = api.json()["realized"]
    assert value["receipt_sha256"] is None
    assert value["complete_signal_gross_pnl"] is value["full_gross_pnl"] is None
    assert value["included_in_live_totals"] is value["native_execution_qualified"] is False
    assert PENDING_HTML in page.text and "gross component:" not in page.text
    assert reads == []


def test_pending_render_does_not_mutate_selected_modeled_context_or_live_facts():
    context = dict(view=None, read_status="UNAVAILABLE", source_id=None,
                   realized=display.unknown_realized_view())
    live = dict(realized_pnl="CONTROLLED_ACTUAL_10.00", rows=[dict(kind="LIVE")])
    before, live_bytes = deepcopy(context), encoded(live)
    def no_row(*args, **kwargs):
        raise AssertionError("no modeled rows selected")
    html = routes.render_aggregate_page(context, render_row=no_row)
    assert PENDING_HTML in html
    assert "partial timing UNKNOWN" in html and "no reviewed baseline source selected" in html
    assert "gross component:" not in html and "selected-ledger gross:" not in html
    assert context == before and encoded(live) == live_bytes


def test_all_unknown_configured_receipt_still_cannot_render_a_gross_number():
    result, binding = controlled_receipt(all_unknown=True)
    view = prepare(result, binding).view()
    assert view["complete_signal_gross_pnl"] is view["full_gross_pnl"] is None
    assert display.render_realized_display(view) == PENDING_HTML
