"""Presentation-only C1 review label, not actual seed/native qualification."""

from copy import deepcopy

import pytest

from project_mai_tai.atr_simulation.firstbar_read_model import firstbar_display_view
from project_mai_tai.atr_simulation.firstbar_ui_reader import SEED_BINDING, SEED_START
from project_mai_tai.services.atr_firstbar_routes import render_firstbar_page
from project_mai_tai.services.control_plane import _render_completed_position_row
from tests.unit.test_atr_simulation_firstbar_many_issues import _stored_with_issues


@pytest.mark.parametrize("bound", [True, False])
def test_review_banner_is_additive_for_seed_and_does_not_promote_economics(bound):
    stored, _ = _stored_with_issues(18)
    if bound:
        stored["run"]["coverage"]["reference_seed_binding"] = deepcopy(SEED_BINDING)
        stored["run"]["source_refs"] = [{"sha256": SEED_BINDING[field]} for field in
            ("csv_sha256", "split_sha256", "selector_sha256")]
    view = firstbar_display_view(stored, source_id="CONTROLLED-many-issues")
    before = deepcopy(view)
    html = render_firstbar_page({"view": view, "source_id": "CONTROLLED-many-issues",
        "day": SEED_START, "days": 32, "read_status": "AVAILABLE"},
        render_row=_render_completed_position_row)
    assert ('data-firstbar-reference-review="true"' in html) is bound
    assert ("in-sample replay; reference differences under review" in html) is bound
    assert view == before and view["coverage_status"] == "UNKNOWN"
    assert all(not total["complete_requested_window"] for total in view["totals"].values())
    if bound:
        assert "underlying capture completeness UNKNOWN" in html
