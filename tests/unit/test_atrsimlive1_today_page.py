"""CONTROLLED display/transport cases; not actual SIM/tape or hotinstall proof."""

import asyncio
from copy import deepcopy
from datetime import UTC, datetime, timedelta
import json
from threading import get_ident
from types import SimpleNamespace

import httpx
import pytest
import sqlalchemy as sa
from sqlalchemy.orm import sessionmaker

from project_mai_tai.atr_simulation import today_display, today_file
from project_mai_tai.services import control_plane as cp
from project_mai_tai.settings import Settings
from tests.unit.test_atrpnl1 import fill, page_data


NOW = datetime(2026, 10, 28, 18, tzinfo=UTC)
DAY = "2026-10-28"


def document():
    identity = dict(trading_day=DAY, symbol="ELPW", broker="webull", account_id="live:orb",
                    strategy="schwab_1m_v2", flip_id="CONTROLLED-flip", segment_id="CONTROLLED-segment")
    row = dict(ticker="ELPW", path="webull:live:orb", quantity="1",
        entry_time="2026-10-28T13:00:00-04:00", entry_price="$2.00",
        exit_time="UNKNOWN", exit_price="UNKNOWN", pnl_label="UNKNOWN",
        pnl_pct_label="UNKNOWN", summary="CONTROLLED OPEN", pnl=None, pnl_label_known=False,
        coverage_note="coverage: watched window 12:30–14:00 ET; outside-window and native/market completeness UNKNOWN")
    table = dict(updated_at=(NOW-timedelta(seconds=60)).isoformat(),
        source_asof=(NOW-timedelta(seconds=90)).isoformat(), read_status="AVAILABLE",
        source_id="CONTROLLED-source", issues=[], rows=[dict(identity=identity, row=row)],
        known_pnl_label="UNKNOWN", skipped_count=0, unknown_count=1, truncated=False)
    value = dict(schema=today_file.SCHEMA, trading_day=DAY, timezone="America/New_York",
        generated_at=(NOW-timedelta(seconds=30)).isoformat(),
        source_asof=(NOW-timedelta(seconds=90)).isoformat(), native_execution_qualified=False,
        producer=dict(head="a"*40, source_pins={"engine": "b"*64},
                      engine_versions={"existing": "CONTROLLED"}, input_references=["CONTROLLED"]),
        tables={key: deepcopy(table) for key, _, _ in today_file.TABLES})
    value["tables"]["LIVE:ACTUAL"]["rows"][0]["row"]["coverage_note"] = (
        "coverage: recorded-fill source CONTROLLED; attribution/native coverage UNKNOWN")
    return value


def context(value=None, *, now=NOW):
    return today_file.validate_today_document(document() if value is None else value, now=now)


@pytest.mark.parametrize("field,value", [
    ("schema", "foreign"), ("trading_day", "2026-10-23"),
    ("timezone", "UTC"), ("native_execution_qualified", True),
    ("native_execution_qualified", 0), ("producer", {}),
    ("generated_at", "2026-10-28T18:00:01+00:00"),
    ("generated_at", "2026-10-28T17:59:30"),
    ("source_asof", "2026-10-28T18:00:01+00:00"), ("html", "<script>x</script>"),
])
def test_invalid_or_historical_envelope_never_falls_back_to_history(field, value):
    data = document()
    data[field] = value
    result = context(data)
    assert result["file_status"] == "INVALID_TODAY_FILE"
    assert all(not item["rows"] and item["known_pnl_label"] == "UNKNOWN"
               for item in result["tables"].values())


@pytest.mark.parametrize("field,age,expected", [
    ("updated_at", 600, "AVAILABLE"), ("updated_at", 601, "STALE"),
    ("source_asof", 600, "AVAILABLE"), ("source_asof", 601, "STALE"),
])
def test_both_computation_and_input_freshness_use_strict_ten_minute_boundary(field, age, expected):
    data = document()
    # Root snapshot watermark is a ceiling, so use a fresh root watermark while
    # explicitly retaining the older per-world source watermark being tested.
    data["source_asof"] = (NOW-timedelta(seconds=30)).isoformat()
    table = data["tables"]["FIRSTBAR"]
    table[field] = (NOW-timedelta(seconds=age)).isoformat()
    if field == "updated_at":
        table["source_asof"] = table[field]
    result = context(data)
    assert result["tables"]["FIRSTBAR"]["display_status"] == expected
    assert len(result["tables"]["FIRSTBAR"]["rows"]) == 1


@pytest.mark.parametrize("field,value", [
    ("updated_at", None), ("source_asof", None), ("source_id", ""),
    ("read_status", "SUCCESS_ZERO"), ("issues", [None]),
    ("skipped_count", True), ("unknown_count", -1),
    ("truncated", "false"), ("html", "<table>untrusted</table>"),
])
def test_table_invalid_or_missing_proof_is_explicit_without_hiding_other_tables(field, value):
    data = document()
    data["tables"]["FIRSTBAR"][field] = value
    result = context(data)
    assert result["tables"]["FIRSTBAR"]["display_status"] != "AVAILABLE"
    assert result["tables"]["FIRSTBAR_X3"]["display_status"] == "AVAILABLE"


def test_wrong_day_or_duplicate_row_refuses_table_without_silent_filter_or_dedup():
    for mutation in ("day", "duplicate"):
        data = document()
        rows = data["tables"]["FIRSTBAR"]["rows"]
        if mutation == "day":
            rows[0]["identity"]["trading_day"] = "2026-10-23"
        else:
            rows.append(deepcopy(rows[0]))
        result = context(data)
        assert result["tables"]["FIRSTBAR"]["display_status"] == "INVALID_TABLE"
        assert not result["tables"]["FIRSTBAR"]["rows"]


def test_unknown_preserves_known_rows_and_null_counts_without_zero_economics():
    data = document()
    data["tables"]["AGG_ENTRY"]["rows"][0]["row"].update(
        exit_time="2026-10-28T13:30:00-04:00", exit_price="$2.10",
        pnl_label_known=True, pnl="0.10000000000000000000001",
        pnl_label="$+0.10", pnl_pct_label="+5.0%", pnl_pct="5.0", pnl_pct_label_known=True,
        sizing_status="CONTROLLED_SIZED", percent_basis="Controlled price return",
        summary="CONTROLLED TARGET_5")
    data["tables"]["AGG_ENTRY"].update(read_status="UNKNOWN", issues=["OUTSIDE_WATCH_UNKNOWN"],
                                    skipped_count=None, unknown_count=None)
    result = context(data)
    table = result["tables"]["AGG_ENTRY"]
    assert table["rows"][0] == data["tables"]["AGG_ENTRY"]["rows"][0]["row"]
    assert table["display_status"] == "UNKNOWN"
    assert table["known_pnl_label"] == "UNKNOWN" and table["skipped_count"] is None
    html = today_display.render_today_page(result, render_row=cp._render_completed_position_row)
    assert "Simulation coverage UNKNOWN: OUTSIDE_WATCH_UNKNOWN" in html
    assert "simulation not updated since" not in html


@pytest.mark.parametrize("field,value", [("issues", []), ("known_pnl_label", "$0.00")])
def test_missing_inputs_cannot_be_silently_empty_success(field, value):
    data = document()
    table = data["tables"]["FIRSTBAR"]
    table.update(read_status="UNAVAILABLE", issues=["MISSING_INPUT"], rows=[])
    table[field] = value
    assert context(data)["tables"]["FIRSTBAR"]["display_status"] == "INVALID_TABLE"


@pytest.mark.parametrize("amount", ["NaN", "Infinity", "1e400", True])
def test_known_pnl_requires_finite_original_value(amount):
    data = document()
    data["tables"]["FIRSTBAR"]["rows"][0]["row"].update(
        pnl_label_known=True, pnl=amount, pnl_label="$1.00")
    assert context(data)["tables"]["FIRSTBAR"]["display_status"] == "INVALID_TABLE"


@pytest.mark.parametrize("field", ["entry_time", "exit_time"])
def test_known_time_cell_cannot_disagree_with_today_identity(field):
    data = document()
    data["tables"]["FIRSTBAR"]["rows"][0]["row"][field] = "2026-10-27T13:00:00-04:00"
    result = context(data)
    assert result["tables"]["FIRSTBAR"]["display_status"] == "INVALID_TABLE"
    assert result["tables"]["FIRSTBAR"]["rows"] == []


def test_utc_time_cell_is_checked_in_et_without_retiming_value():
    data = document()
    row = data["tables"]["FIRSTBAR"]["rows"][0]["row"]
    row["entry_time"] = "2026-10-28T01:00:00+00:00"  # Prior ET date.
    assert context(data)["tables"]["FIRSTBAR"]["display_status"] == "INVALID_TABLE"
    row["entry_time"] = "2026-10-28T17:00:00+00:00"
    assert context(data)["tables"]["FIRSTBAR"]["rows"][0]["entry_time"] == row["entry_time"]


@pytest.mark.parametrize("label", ["UNKNOWN", "OPEN", "-"])
def test_unknown_open_time_labels_remain_unknown_not_invented_dates(label):
    data = document()
    row = data["tables"]["FIRSTBAR"]["rows"][0]["row"]
    row["exit_time"] = label
    assert context(data)["tables"]["FIRSTBAR"]["rows"][0]["exit_time"] == label


def test_json_overflow_literal_refused_while_finite_decimal_remains_exact(tmp_path):
    path = tmp_path / "current.json"
    ordinary = json.dumps(document())
    path.write_text(ordinary.replace('"producer":', '"overflow_probe": 1e400, "producer":', 1))
    assert today_file.read_today_file(path, now=NOW)["file_status"] == "TODAY_FILE_UNAVAILABLE"
    exact = today_file._finite_decimal("1.00000000000000000000001")
    assert str(exact) == "1.00000000000000000000001"


def test_read_and_projection_are_detached_and_keep_exact_display_fields(tmp_path):
    data = document()
    before = deepcopy(data)
    path = tmp_path / "current.json"
    path.write_text(json.dumps(data))
    result = today_file.read_today_file(path, now=NOW)
    assert data == before
    assert result["file_status"] == "AVAILABLE"
    assert result["tables"]["FIRSTBAR"]["rows"][0] == data["tables"]["FIRSTBAR"]["rows"][0]["row"]


@pytest.mark.parametrize("fault", ["missing", "malformed", "duplicate_key", "nonfinite", "symlink"])
def test_local_file_failures_explicit_unknown_no_history_or_queries(tmp_path, fault):
    path = tmp_path / "current.json"
    if fault == "malformed":
        path.write_text("{")
    elif fault == "duplicate_key":
        path.write_text('{"schema":1,"schema":2}')
    elif fault == "nonfinite":
        path.write_text('{"schema":NaN}')
    elif fault == "symlink":
        target = tmp_path / "other.json"
        target.write_text(json.dumps(document()))
        path.symlink_to(target)
    result = today_file.read_today_file(path, now=NOW)
    assert result["file_status"] == "TODAY_FILE_UNAVAILABLE"
    assert all(not table["rows"] for table in result["tables"].values())


def test_reader_caps_refuse_without_partial_rows(tmp_path, monkeypatch):
    assert today_file.MAX_BYTES == 16*1024*1024 and today_file.MAX_ROWS == 10000
    path = tmp_path / "current.json"
    path.write_bytes(b" "*257)
    monkeypatch.setattr(today_file, "MAX_BYTES", 256)
    assert today_file.read_today_file(path, now=NOW)["file_status"] == "TODAY_FILE_UNAVAILABLE"
    monkeypatch.setattr(today_file, "MAX_ROWS", 6)
    assert context()["file_status"] == "TODAY_ROW_LIMIT_EXCEEDED"


def test_seven_today_panels_keep_descriptions_real_badge_and_original_live_rows():
    result = context()
    html = today_display.render_today_page(result, render_row=cp._render_completed_position_row)
    assert html.count('data-sim-mode="') == 7
    assert html.count("REAL RECORDED TRADES") == 1
    assert html.count("SIMULATION - NOT REAL TRADES") == 6
    assert html.count("Last updated 13:59:00 ET") == 7
    assert "FIRSTBAR+2min" in html and "first in line" in html
    assert 'data-sim-day-filter' not in html and 'type="date"' not in html
    assert "All dates" not in html and "Optional replay sources" not in html
    data = page_data([fill("buy", 1, 2, "CONTROLLED"), fill("sell", 1, 3, "CONTROLLED-ocoexit-native")])
    before = deepcopy(data)
    baseline = cp._render_bot_detail_page(data, "schwab_1m_v2")
    mounted = cp._render_bot_detail_page(data, "schwab_1m_v2", firstbar_simulation_html=html)
    assert mounted.replace(html, "", 1).encode() == baseline.encode() and data == before


def test_missing_or_stale_each_panel_shows_actual_or_unknown_update_clock(tmp_path):
    missing = today_file.read_today_file(tmp_path / "missing", now=NOW)
    html = today_display.render_today_page(missing, render_row=cp._render_completed_position_row)
    assert html.count("simulation not updated since UNKNOWN ET") == 7
    old = document()
    old["tables"]["FIRSTBAR"]["source_asof"] = (NOW-timedelta(minutes=11)).isoformat()
    html = today_display.render_today_page(context(old), render_row=cp._render_completed_position_row)
    assert "simulation not updated since 13:49 ET" in html
    assert "inputs as of 13:49:00 ET" in html


def test_untrusted_row_text_is_escaped_not_raw_html():
    data = document()
    data["tables"]["FIRSTBAR"]["rows"][0]["row"]["summary"] = "<script>CONTROLLED</script>"
    html = today_display.render_today_page(context(data), render_row=cp._render_completed_position_row)
    assert "<script>CONTROLLED</script>" not in html and "&lt;script&gt;CONTROLLED&lt;/script&gt;" in html


@pytest.mark.parametrize("field", ["summary", "coverage_note", "issues", "source_id",
                                   "known_pnl_label", "identity"])
def test_json_escaped_surrogate_refuses_table_before_html_response(tmp_path, field):
    from starlette.responses import HTMLResponse

    data = document()
    table = data["tables"]["FIRSTBAR"]
    if field == "identity":
        table["rows"][0]["identity"]["account_id"] = "\ud800"
    elif field in {"summary", "coverage_note"}:
        table["rows"][0]["row"][field] = "\ud800"
    else:
        table[field] = ["\ud800"] if field == "issues" else "\ud800"
    path = tmp_path / "current.json"
    raw = json.dumps(data, ensure_ascii=True)
    assert "\\ud800" in raw
    path.write_text(raw, encoding="utf-8")
    result = today_file.read_today_file(path, now=NOW)
    assert result["tables"]["FIRSTBAR"]["display_status"] == "INVALID_TABLE"
    assert result["tables"]["FIRSTBAR"]["rows"] == []
    assert result["tables"]["FIRSTBAR"]["known_pnl_label"] == "UNKNOWN"
    assert result["tables"]["FIRSTBAR_X3"]["display_status"] == "AVAILABLE"
    response = HTMLResponse(today_display.render_today_page(
        result, render_row=cp._render_completed_position_row))
    assert response.status_code == 200
    assert "INVALID_TABLE" in response.body.decode("utf-8")


def test_valid_unicode_and_emoji_survive_json_reader_and_html_encoding(tmp_path):
    from starlette.responses import HTMLResponse

    data = document()
    table = data["tables"]["FIRSTBAR"]
    text = "Résumé 東京 🟢"
    table["rows"][0]["row"].update(summary=text, coverage_note=text)
    table.update(source_id=text, known_pnl_label=text, issues=[text])
    path = tmp_path / "current.json"
    path.write_text(json.dumps(data, ensure_ascii=True), encoding="utf-8")
    result = today_file.read_today_file(path, now=NOW)
    assert result["tables"]["FIRSTBAR"]["display_status"] == "AVAILABLE"
    response = HTMLResponse(today_display.render_today_page(
        result, render_row=cp._render_completed_position_row))
    assert response.status_code == 200 and text in response.body.decode("utf-8")


@pytest.mark.parametrize("note", [None, "", "x"*2049, "coverage\nnot-plain"])
def test_missing_or_invalid_coverage_note_cannot_become_implied_proof(note):
    data = document()
    data["tables"]["FIRSTBAR"]["rows"][0]["row"]["coverage_note"] = note
    table = context(data)["tables"]["FIRSTBAR"]
    assert table["display_status"] == "INVALID_TABLE" and table["rows"] == []


def test_coverage_visible_escaped_beside_original_exit_reason_and_nine_cells():
    data = document()
    note = "coverage: watched window 12:30–14:00 ET; outside/native UNKNOWN <b>literal</b>"
    data["tables"]["FIRSTBAR"]["rows"][0]["row"].update(coverage_note=note,
        summary="CONTROLLED ORIGINAL_EXIT_REASON")
    before = deepcopy(data)
    html = today_display.render_today_page(context(data), render_row=cp._render_completed_position_row)
    assert ('CONTROLLED ORIGINAL_EXIT_REASON; percent evidence UNKNOWN (legacy row)'
            '<br><small class="sim-row-coverage">') in html
    assert "&lt;b&gt;literal&lt;/b&gt;" in html and "<b>literal</b>" not in html
    assert html.count('class="sim-row-coverage"') == 7
    assert html.count("<td") == 7*9
    assert data == before


@pytest.mark.skip(reason="operator 10-29: ATR page now shows only FIRSTBAR / FIRSTBAR+2min from ATRSIM2 (test_atrsim2.py)")
def test_real_today_route_reads_and_renders_offloop_without_any_historical_port(monkeypatch):
    calls, render_threads = [], []

    class ForbiddenHistory:
        def read(self, *args, **kwargs):
            raise AssertionError("TODAY page must not query historical SIM storage")

    def today_reader(*, now):
        calls.append((get_ident(), now))
        return context(now=now)

    original = today_display.render_today_page

    def observed(*args, **kwargs):
        render_threads.append(get_ident())
        return original(*args, **kwargs)

    monkeypatch.setattr(today_display, "render_today_page", observed)
    monkeypatch.setattr(cp, "utcnow", lambda: NOW)
    engine = sa.create_engine("sqlite:///:memory:")
    try:
        app = cp.build_app(Settings(_env_file=None, trade_coach_enabled=False, bot_control_enabled=False),
            session_factory=sessionmaker(engine), firstbar_read_port=ForbiddenHistory(),
            aggregate_simulation_read_port=ForbiddenHistory(), legacy_simulation_read_port=ForbiddenHistory(),
            today_simulation_read_port=today_reader)

        async def dashboard():
            return page_data([])

        app.state.repository = SimpleNamespace(load_bot_dashboard_data=dashboard)

        async def request():
            loop_thread = get_ident()
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://controlled") as client:
                response = await client.get("/bot/1m-schwab-v2?day=2026-09-22")
            assert response.status_code == 200 and 'data-atrsimlive1="today-only"' in response.text
            assert 'type="date"' not in response.text
            assert calls and all(thread != loop_thread for thread, _ in calls)
            assert render_threads and all(thread != loop_thread for thread in render_threads)

        asyncio.run(request())
    finally:
        engine.dispose()
