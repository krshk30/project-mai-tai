"""UNRUN CP display controls, CONTROLLED data; not installed/native acceptance.

The literal 506/253/12+4 populations and supplied money labels exercise copying
and HTML visibility, not a recreated model, allocation or actual-copy witness.
"""

import asyncio
from copy import deepcopy
from datetime import UTC, date, datetime
from html.parser import HTMLParser
import re
from threading import get_ident
from types import SimpleNamespace

import httpx

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import sessionmaker

from project_mai_tai.atr_simulation import cp_sim_display as ui
from project_mai_tai.atr_simulation.aggregate_entry_read_model import AggregateReadBinding
from project_mai_tai.atr_simulation.realized_display import unknown_realized_view
from project_mai_tai.atr_simulation.ui_reader import unavailable_storage
from project_mai_tai.services import atr_aggregate_routes, atr_simulation_legacy_routes, control_plane as cp
from project_mai_tai.settings import Settings
from tests.unit.test_atrpnl1 import fill, page_data
from tests.unit.test_atr_simulation_aggregate_display_contract import view as aggregate_view
from tests.unit.test_atr_simulation_firstbar_display import _trade, _view
from tests.unit.test_atr_simulation_legacy_visibility import Reader, live_row, stored_page


def accepted_port():
    return SimpleNamespace(binding=SimpleNamespace(source_id=ui.AGGREGATE_SOURCE))


def contexts():
    first = _view()
    first["source"]["source_id"] = ui.FIRST_SOURCE
    first["outcomes"] = []
    for mode, total in (("FIRSTBAR", "$+196.87"), ("FIRSTBAR_X3", "$+186.59")):
        for index in range(253):
            trade = _trade(mode)
            trade["identity"]["flip_id"] = f"CONTROLLED-{index}"
            trade["trade_id"] = f"CONTROLLED-{index}"
            trade["outcome_id"] = f"CONTROLLED-{mode}-{index}"
            first["outcomes"].append(trade)
        first["totals"][mode].update(known_closed_trades=253, known_pnl_label=total)
    aggregate = aggregate_view()
    row = deepcopy(first["outcomes"][0]["row"])
    aggregate.update(read_status="AVAILABLE", source_id=ui.AGGREGATE_SOURCE,
        rows=[{**row, "ticker": f"CONTROLLED-{index}"} for index in range(253)],
        known_pnl_label="$-22.94")
    realized = unknown_realized_view()
    realized.update(status="BOUND_COMPLETE_SIGNAL_GROSS", receipt_sha256="a"*64,
        complete_signal_gross_pnl="28.94", matched_signals=147, unmatched_signals=26)
    legacy = []
    for day, count in zip(ui.LEGACY_DAYS, (3, 1)):
        rows = []
        for index in range(count):
            for mode, model in ui.LEGS:
                row = live_row()
                row["identity"].update(trading_day=day.isoformat(), flip_id=f"CONTROLLED-{day}-{index}")
                if mode != "LIVE":
                    row["fills"] = row.pop("entries")
                    # Deliberately unresolved SIM receipt: visible UNKNOWN, never an invented zero.
                    row["gross_pnl"] = None
                row.update(mode=mode, model=model)
                rows.append(row)
        legacy.append({"view": dict(source={"source_id": ui.LEGACY_SOURCE, "policy_version": "CONTROLLED"},
            read_status="AVAILABLE", coverage_status="UNKNOWN", truncated=False, rows=rows)})
    return ({"view": first}, {"view": aggregate, "realized": realized,
        "source_id": ui.AGGREGATE_SOURCE}, legacy)


def rendered(value=None):
    return ui.render_page(*(contexts() if value is None else value), render_row=cp._render_completed_position_row,
        scopes=ui.page_scopes(accepted_port()))


class TableParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.mode, self.section, self.table = None, None, None
        self.tables, self.captions, self.visible = {}, {}, []
        self.in_row, self.in_body, self.cells = False, False, 0

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "section":
            self.mode = attrs.get("data-sim-mode")
            self.captions[self.mode] = 0
        if tag == "div" and attrs.get("class") == "sub":
            self.captions[self.mode] += 1
        if tag == "table":
            self.table = self.tables.setdefault(self.mode, [])
        if tag == "tbody":
            self.in_body = True
        if tag == "tr" and self.in_body:
            self.in_row, self.cells = True, 0
        if tag == "td" and self.in_row:
            self.cells += 1

    def handle_endtag(self, tag):
        if tag == "tr" and self.in_row:
            self.table.append(self.cells)
            self.in_row = False
        if tag == "tbody":
            self.in_body = False
        if tag == "section":
            self.mode = None

    def handle_data(self, data):
        self.visible.append(data)


def test_queryless_defaults_are_exact_installed_runs_and_two_bounded_legacy_days():
    first, aggregate, legacy = ui.page_scopes(accepted_port())
    assert first == {"source_id": ui.FIRST_SOURCE, "day": date(2026, 9, 22), "days": 32}
    assert aggregate == {"source_id": ui.AGGREGATE_SOURCE, "day": date(2026, 9, 22), "days": 32}
    assert legacy == [{"source_id": ui.LEGACY_SOURCE, "day": day, "days": 1} for day in ui.LEGACY_DAYS]
    assert ui.LEGACY_DAYS == (date(2026, 10, 12), date(2026, 10, 21))


@pytest.mark.parametrize("port", [None, SimpleNamespace(),
    SimpleNamespace(binding=SimpleNamespace(source_id="aggregate-entry:" + "f"*64))])
def test_unconfigured_or_foreign_port_does_not_enable_accepted_defaults(port):
    first, aggregate, legacy = ui.page_scopes(port)
    assert first == aggregate == {} and legacy == [{}]


def test_overrides_are_preserved_detached_and_legacy_window_not_widened():
    first = dict(source_id="CONTROLLED-first", day=date(2026, 10, 1), days=2)
    aggregate = dict(source_id="aggregate-entry:" + "b"*64, day=date(2026, 10, 2), days=3)
    legacy = dict(source_id="CONTROLLED-legacy", day=date(2026, 10, 3), days=7)
    before = deepcopy((first, aggregate, legacy))
    selected = ui.page_scopes(accepted_port(), first, aggregate, legacy)
    assert selected == (first, aggregate, [legacy])
    selected[0]["days"] = 1
    assert (first, aggregate, legacy) == before


def test_default_legacy_day_override_is_one_window_not_two_silent_queries():
    _, _, legacy = ui.page_scopes(accepted_port(), legacy={"day": date(2026, 10, 21), "days": 1})
    assert legacy == [{"source_id": ui.LEGACY_SOURCE, "day": date(2026, 10, 21), "days": 1}]


def test_all_775_rows_remain_visible_in_seven_nine_column_mode_tables():
    html = rendered()
    parser = TableParser()
    parser.feed(html)
    assert ui.COLUMNS == ("Ticker", "Path", "Qty", "Entry Time", "Entry", "Exit Time", "Exit", "P&L", "Exit reason")
    assert {key: len(rows) for key, rows in parser.tables.items()} == {
        "FIRSTBAR": 253, "FIRSTBAR_X3": 253, "AGGREGATE_ENTRY": 253,
        "LIVE:ACTUAL": 4, "TODAY:SUPPLIED": 4, "VOLBODY:QUEUE_AWARE": 4, "VOLBODY:OPTIMISTIC": 4}
    assert sum(map(len, parser.tables.values())) == 775
    assert all(cells == 9 for rows in parser.tables.values() for cells in rows)
    assert set(parser.captions.values()) == {1}
    assert html.count('<div class="table-wrap">') == 7 and html.count(ui.HEADER) == 7


def test_full_supplied_totals_and_reconciliation_counts_are_small_tiles_not_recomputed():
    value = contexts()
    before = deepcopy(value)
    html = rendered(value)
    for label in ("+$196.87", "+$186.59", "-$22.94", "+$28.94", "147 complete / 26 unreconciled"):
        assert label in html
    for canonical in ("$+196.87", "$+186.59", "$-22.94"):
        assert 'data-pnl-label="' + canonical + '"' in html
    assert html.count('class="hero-card"') == 4
    assert value == before


def test_metadata_hashes_and_required_caveat_are_tooltips_not_body_paragraphs():
    html = rendered()
    parser = TableParser()
    parser.feed(html)
    visible = ''.join(parser.visible)
    assert '<p>' not in html and '<h2>' not in html
    # Six SIM tables carry the SIM badge; the one real recorded-trade table carries its own (operator 10-27).
    assert html.count("SIMULATION - NOT REAL TRADES") == 6
    assert html.count(ui.REAL_BADGE) == 1
    assert "in-sample replay; reference differences under review" in html
    assert "in-sample replay; reference differences under review" not in visible
    assert "a"*64 in html and "a"*64 not in visible
    assert ui.FIRST_SOURCE not in visible and ui.LEGACY_SOURCE not in visible
    for removed in ("Scope:", "Receipt issues", "Unmatched account-leg reason occurrences:", "Storage:"):
        assert removed not in visible


def test_small_mobile_tables_scroll_inside_cards_and_overrides_are_optional():
    html = rendered()
    assert '#atr-sim-compact .table-wrap{max-width:100%;overflow:auto}' in html
    assert '#atr-sim-compact{min-width:0;max-width:100%;grid-column:1/-1}' in html
    assert '@media(max-width:720px)' in html
    assert '<summary>Optional replay sources</summary>' in html
    assert ' required' not in html and 'onsubmit=' in html
    assert 'name="source_id"' in html and 'name="aggregate_source_id"' in html and 'name="legacy_source_id"' in html


def test_untrusted_row_and_source_text_are_escaped_without_modifying_economics():
    value = contexts()
    value[0]["view"]["source"]["population_label"] = "CONTROLLED"
    value[0]["view"]["outcomes"][0]["row"]["summary"] = '<script>BAD</script>'
    value[0]["view"]["source"]["source_id"] = '<script>BAD</script>'
    html = rendered(value)
    # Exactly one executable script: the trusted static day filter. Nothing from rows/sources executes.
    assert re.findall(r'<script\b', html, re.I) == ['<script']
    assert html.count(ui.DAY_FILTER_SCRIPT) == 1
    assert '<script' not in html.replace(ui.DAY_FILTER_SCRIPT, '', 1).lower()
    assert 'BAD' not in ui.DAY_FILTER_SCRIPT
    assert '<script>' not in html and '&lt;script&gt;BAD&lt;/script&gt;' in html
    assert "+$196.87" in html


@pytest.mark.parametrize("mutation", ["incomplete", "unknown_money", "duplicate"])
def test_firstbar_guards_are_not_removed_by_compact_output(mutation):
    value = contexts()
    first = value[0]["view"]
    if mutation == "incomplete":
        first["totals"]["FIRSTBAR"]["complete_requested_window"] = True
    elif mutation == "unknown_money":
        first["outcomes"][0]["row"].update(pnl_label_known=False, pnl="0", pnl_label="$0")
    else:
        first["outcomes"].append(deepcopy(first["outcomes"][0]))
    with pytest.raises(ValueError, match="firstbar_display_contract_required"):
        rendered(value)


@pytest.mark.parametrize("mutation", ["unavailable_zero", "native", "nonfinite"])
def test_aggregate_guards_are_not_removed_by_compact_output(mutation):
    value = contexts()
    aggregate = value[1]["view"]
    if mutation == "unavailable_zero":
        aggregate.update(read_status="UNAVAILABLE", rows=[], known_pnl_label="$0.00")
    elif mutation == "native":
        aggregate["global_pnl_qualified"] = True
    else:
        aggregate["rows"][0]["pnl"] = "NaN"
    with pytest.raises(ValueError):
        rendered(value)


def test_unknown_realized_remains_reconciliation_not_zero_or_subtotal():
    value = contexts()
    value[1]["realized"] = unknown_realized_view()
    html = rendered(value)
    assert 'Under reconciliation' in html and '+$28.94' not in html
    assert '147 complete' not in html and '+$196.87' in html


def test_legacy_incomplete_legs_remain_rows_with_unknown_not_zero():
    value = contexts()
    before = deepcopy(value[2])
    html = ui.legacy_panels(value[2], render_row=cp._render_completed_position_row)
    parser = TableParser()
    parser.feed(html)
    assert sum(map(len, parser.tables.values())) == 16
    assert 'UNKNOWN' in html and '$+0.00' not in html and value[2] == before


def test_overlapping_legacy_inputs_are_not_silently_deduplicated():
    value = contexts()[2]
    with pytest.raises(ValueError, match="duplicate_compact_legacy_identity"):
        ui.legacy_panels([value[0], value[0]], render_row=cp._render_completed_position_row)


@pytest.mark.parametrize("fact", [None, {"at": None}, {"at": {"event_ns": 2**64}}])
def test_unknown_legacy_facts_stay_unknown_without_inferred_quantity_time_or_pnl(fact):
    receipt = live_row()
    receipt.update(entries=[fact], exits=[fact], gross_pnl=None)
    row = ui._legacy_row(receipt, {"completed": False, "known_pnl": None})
    assert row["quantity"] == row["entry_time"] == row["exit_time"] == "UNKNOWN"
    assert row["pnl"] is None and row["pnl_label"] == "UNKNOWN" and not row["pnl_label_known"]


def test_non_sim_rendered_html_is_byte_equal_after_removing_only_sim_mount(monkeypatch):
    monkeypatch.setattr(cp, "utcnow", lambda: datetime(2026, 10, 23, 20, tzinfo=UTC))
    data = page_data([fill("buy", 10, 4, "CONTROLLED-entry"),
        fill("sell", 10, 5, "CONTROLLED-entry-ocoexit-native")])
    before = deepcopy(data)
    html = rendered()
    baseline = cp._render_bot_detail_page(data, "schwab_1m_v2")
    mounted = cp._render_bot_detail_page(data, "schwab_1m_v2", firstbar_simulation_html=html)
    assert mounted.replace(html, "", 1).encode() == baseline.encode()
    assert data == before


def test_compact_legacy_projection_keeps_guards_skips_api_encoding_and_is_detached(monkeypatch):
    page = stored_page()
    page["rows"] = [live_row()]
    before = deepcopy(page)
    def forbidden(*args, **kwargs):
        raise AssertionError("Compact CP must not encode an unused API response")
    monkeypatch.setattr(atr_simulation_legacy_routes, "_EncodedLegacyView", forbidden)
    value = asyncio.run(atr_simulation_legacy_routes.load_legacy_display(Reader(page),
        source_id="CONTROLLED-legacy", day=date(2026, 10, 23), render_html=None))
    assert value["html"] is None and len(value["view"]["rows"]) == 1
    assert page == before


def test_legacy_api_bytes_are_unchanged_and_compact_projection_drops_only_unused_display_metadata():
    page = stored_page()
    row = live_row()
    row["unused_controlled_metadata"] = {"kept_in_api": "CONTROLLED"}
    page["rows"] = [row]
    before, before_html = atr_simulation_legacy_routes._project(
        page, "CONTROLLED-legacy", date(2026, 10, 23), date(2026, 10, 23), render_html=False)
    compact, compact_html = atr_simulation_legacy_routes._project(
        page, "CONTROLLED-legacy", date(2026, 10, 23), date(2026, 10, 23), render_html=None)
    after, after_html = atr_simulation_legacy_routes._project(
        page, "CONTROLLED-legacy", date(2026, 10, 23), date(2026, 10, 23), render_html=False)
    assert before_html is compact_html is after_html is None
    assert before.body == after.body and before == after
    assert before["rows"][0]["unused_controlled_metadata"] == {"kept_in_api": "CONTROLLED"}
    assert "unused_controlled_metadata" not in compact["rows"][0]
    for key in ("identity", "entries", "exits", "gross_pnl", "coverage", "issues"):
        assert compact["rows"][0][key] == before["rows"][0][key]


@pytest.mark.parametrize("invalid", [float("inf"), "\ud800"])
def test_compact_legacy_still_refuses_invalid_unused_metadata(invalid):
    page = stored_page()
    page["rows"] = [{**live_row(), "unused_controlled_metadata": invalid}]
    view, html = atr_simulation_legacy_routes._project_or_unknown(
        page, "CONTROLLED-legacy", date(2026, 10, 23), date(2026, 10, 23), render_html=None)
    assert html is None and view["read_status"] == "UNAVAILABLE" and view["rows"] == []


def test_owned_legacy_routes_keep_bounded_scopes_offloop_while_cp_uses_sim2(monkeypatch):
    """CONTROLLED binding/empty projection: no actual775 DB or native-row claim."""
    from project_mai_tai.services import atr_firstbar_routes
    from project_mai_tai.atr_simulation import sim2_display
    calls, render_threads, captures = [], [], []
    class Recorder:
        def read(self, source, start, end, *, limit):
            calls.append((source, start, end, limit, get_ident()))
            return unavailable_storage("CONTROLLED-not-native")
    binding = AggregateReadBinding(ui.AGGREGATE_SOURCE, "0"*64, b"{}", "1"*64)
    port = atr_aggregate_routes.AggregateSimulationReadPort(Recorder(), binding)
    monkeypatch.setattr(atr_aggregate_routes, "aggregate_entry_display_view", lambda *args, **kwargs: aggregate_view())
    def observe(target, name):
        actual = getattr(target, name)
        def observed(*args, **kwargs):
            render_threads.append(get_ident())
            return actual(*args, **kwargs)
        monkeypatch.setattr(target, name, observed)
    observe(atr_firstbar_routes, "render_firstbar_page")
    observe(atr_aggregate_routes, "render_aggregate_page")
    observe(atr_simulation_legacy_routes, "_project_or_unknown")
    sim2_reads = []
    def sim2_read(factory, day):
        sim2_reads.append((day, get_ident()))
        return None  # CONTROLLED unavailable day, not historical zero trades.
    monkeypatch.setattr(sim2_display, "load_day", sim2_read)
    template = cp._render_bot_detail_page
    def capture(data, strategy, **kwargs):
        captures.append((deepcopy(data), strategy, kwargs["firstbar_simulation_html"]))
        return template(data, strategy, **kwargs)
    monkeypatch.setattr(cp, "_render_bot_detail_page", capture)
    monkeypatch.setattr(cp, "utcnow", lambda: datetime(2026, 10, 23, 20, tzinfo=UTC))
    data = page_data([])
    engine = sa.create_engine("sqlite:///:memory:")
    try:
        app = cp.build_app(Settings(_env_file=None, trade_coach_enabled=False, bot_control_enabled=False),
            session_factory=sessionmaker(engine), firstbar_read_port=Recorder(),
            legacy_simulation_read_port=Recorder(), aggregate_simulation_read_port=port)
        async def dashboard():
            return deepcopy(data)
        app.state.repository = SimpleNamespace(load_bot_dashboard_data=dashboard)
        async def request():
            loop_thread = get_ident()
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://controlled") as client:
                response = await client.get("/bot/1m-schwab-v2")
                assert calls == [] and "No simulation for 2026-10-23 yet" in response.text
                for path, source, day, days in (
                    ("/atr-aggregate", ui.AGGREGATE_SOURCE, "2026-09-22", 32),
                    ("/atr-firstbar", ui.FIRST_SOURCE, "2026-09-22", 32),
                    ("/atr-simulation", ui.LEGACY_SOURCE, "2026-10-12", 1),
                    ("/atr-simulation", ui.LEGACY_SOURCE, "2026-10-21", 1),
                ):
                    selected = await client.get(path, params={"source_id": source,
                        "day": day, "days": days})
                    assert selected.status_code == 200
            assert response.status_code == 200
            assert render_threads and all(thread != loop_thread for thread in render_threads)
            assert len(sim2_reads) == 1 and sim2_reads[0][0] == "2026-10-23"
            assert sim2_reads[0][1] != loop_thread
            assert all(call[-1] != loop_thread for call in calls)
            return response
        response = asyncio.run(request())
        assert [(source, start, end, limit) for source, start, end, limit, _ in calls] == [
            (ui.AGGREGATE_SOURCE, date(2026, 9, 22), date(2026, 10, 23), 10000),
            (ui.FIRST_SOURCE, date(2026, 9, 22), date(2026, 10, 23), 10000),
            (ui.LEGACY_SOURCE, date(2026, 10, 12), date(2026, 10, 12), 10000),
            (ui.LEGACY_SOURCE, date(2026, 10, 21), date(2026, 10, 21), 10000)]
        snapshot, strategy, panel = captures[0]
        assert response.text.replace(panel, "", 1) == template(snapshot, strategy)
    finally:
        engine.dispose()
