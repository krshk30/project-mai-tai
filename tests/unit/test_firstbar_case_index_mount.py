"""Controlled same-read footer integration, not actual PG/page qualification."""

import asyncio
from collections import UserDict
from copy import deepcopy
from threading import get_ident

from project_mai_tai.services import atr_firstbar_routes as routes
from project_mai_tai.services.control_plane import _render_completed_position_row
from tests.unit.test_atr_simulation_firstbar_case_index import SOURCE, _stored


class RetainedIndex:
    def __init__(self):
        self.stored = _stored()
        self.calls = 0

    def read(self, *args, **kwargs):
        self.calls += 1
        return self.stored


def test_invalid_trade_projection_preserves173_index_but_not_page_or_economics():
    reader = RetainedIndex()
    before = deepcopy(reader.stored)
    context = asyncio.run(routes.load_firstbar_display(reader, source_id=SOURCE))
    assert reader.calls == 1 and reader.stored == before
    assert context["view"]["outcomes"] == []  # Metadata fixture has no canonical fills.
    assert context["historical_index_bound"] is True
    footer = context["historical_reference_footer"]
    assert "Selected seed signals: 173" in footer
    assert "CANONICAL_RESULT_PAGE_UNKNOWN_INDEX_PRESERVED" in footer
    assert "UNKNOWN observed" in footer and "$0" not in footer
    html = routes.render_firstbar_page(context, render_row=_render_completed_position_row)
    assert "in-sample replay; reference differences under review" in html
    assert html.count('data-firstbar-reference-index="true"') == 1


def test_unselected_source_has_unknown_denominator_and_no_read():
    reader = RetainedIndex()
    context = asyncio.run(routes.load_firstbar_display(reader))
    assert reader.calls == 0 and not context["historical_index_bound"]
    assert "Signal denominator UNKNOWN" in context["historical_reference_footer"]
    assert "Selected seed signals: 173" not in context["historical_reference_footer"]


def test_footer_projection_and_render_run_off_the_page_loop(monkeypatch):
    original = routes._reference_footer
    calls = []

    def tracked(*args):
        calls.append(get_ident())
        return original(*args)

    monkeypatch.setattr(routes, "_reference_footer", tracked)

    async def scenario():
        loop_thread = get_ident()
        await routes.load_firstbar_display(RetainedIndex(), source_id=SOURCE)
        assert calls and all(thread != loop_thread for thread in calls)

    asyncio.run(scenario())


def test_missing_index_is_not_backfilled_from_the_known_csv_population():
    reader = RetainedIndex()
    reader.stored["run"]["coverage"].pop("historical_study")
    context = asyncio.run(routes.load_firstbar_display(reader, source_id=SOURCE))
    assert not context["historical_index_bound"] and reader.calls == 1
    assert "Signal denominator UNKNOWN" in context["historical_reference_footer"]


def test_invalid_mapping_page_preserves_index_but_cannot_keep_exact_page_claim():
    reader = RetainedIndex()
    reader.stored = UserDict(reader.stored)
    context = asyncio.run(routes.load_firstbar_display(reader, source_id=SOURCE))
    assert reader.calls == 1 and reader.stored["read_status"] == "AVAILABLE"
    assert context["historical_index_bound"] is True and context["view"]["outcomes"] == []
    footer = context["historical_reference_footer"]
    assert "Selected seed signals: 173" in footer
    assert "CANONICAL_RESULT_PAGE_UNKNOWN_INDEX_PRESERVED" in footer
    assert "EXACT_DECLARED_RESULT_ROWS_ONLY" not in footer
