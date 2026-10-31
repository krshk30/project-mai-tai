"""SOURCE-only controls for Main; no native/tape qualification or PAGE edits.

The external PAGE hook is byte-pinned to Main's reviewed composed PAGE bytes.
The original v3 identities remain explicit; no loose-hash or missing-file skip.
"""

from copy import deepcopy
import hashlib
from html import escape
import importlib.util
import os
from pathlib import Path
import sys

import pytest

from tests.unit.test_atrsimlive1_runtime import NOW, ROOT, module, runtime, unavailable


ORIGINAL_PAGE_V3_PINS = {  # Immutable 0e133e4f source-era identities.
    "today_file": "fecae0b80b48210b3b60764a38cf1e5f15d6ea701cc9fdd43928fc37766cb7f3",
    "today_display": "cd4d751779da61a2653bd511cafbe234ce7b0904de0bf04dc911a2df1cd2000f",
    "cp_sim_display": "68ffd4c2aafe6d3995bb992e1715d954f7c887ec2fe70a1804b910dafa96414c",
}
PAGE_PINS = {
    **ORIGINAL_PAGE_V3_PINS,
    # Reviewed compact-envelope validation/run-summary extensions. The legacy
    # V1 roundtrip and all original field/escaping/UNKNOWN assertions below stay.
    "today_file": "bc350e4655550d77491ef08127424dda7f12260ddf18e5a60a5883e6644c3763",
    "today_display": "d5bb6d3f04a626ad5594716e1135c88e52b4b4eb4a8db3380920ec08e78bf5fd",
}


def controlled_producer_row(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "tools"))
    models = module("atrsimlive1_models")
    identity = dict(trading_day="2026-10-28", symbol="CONTROLLED", broker="webull",
                    account_id="CONTROLLED-account", strategy="schwab_1m_v2",
                    flip_id="CONTROLLED-flip", segment_id="CONTROLLED-segment")
    receipt = dict(mode="FIRSTBAR", identity=identity,
        fills=[dict(at=dict(event_ns=1793206800000000000), price="2.00", quantity=100)],
        exits=[dict(at=dict(event_ns=1793206860000000000), price="2.10", quantity=100,
                    reason="TARGET_5 <CONTROLLED>")])
    before = deepcopy(receipt)
    note = "coverage: watched window 12:30–14:00 ET; outside/native UNKNOWN <CONTROLLED>"
    row = models.canonical_row(receipt, known=True, amount="10.00", coverage_note=note)
    assert receipt == before
    assert row["summary"] == "TARGET_5 <CONTROLLED>"
    document = unavailable()
    document["source_asof"] = NOW.isoformat()
    document["tables"]["FIRSTBAR"].update(read_status="UNKNOWN", updated_at=NOW.isoformat(),
        source_asof=NOW.isoformat(), rows=[dict(identity=identity, row=row, outcome_id="CONTROLLED")])
    return document


def external_page_module(name, monkeypatch):
    root = Path(os.environ.get("ATRSIMLIVE1_PAGE_SOURCE_ROOT", str(ROOT)))
    path = root / "src/project_mai_tai/atr_simulation" / (name + ".py")
    assert hashlib.sha256(path.read_bytes()).hexdigest() == PAGE_PINS[name], (
        "Main must supply the reviewed exact PAGE v3 bytes; no foreign/absent skip")
    qualified = "project_mai_tai.atr_simulation." + name
    spec = importlib.util.spec_from_file_location(qualified, path)
    result = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, qualified, result)
    spec.loader.exec_module(result)
    return result


@pytest.mark.parametrize("raw", [b'{"value":1e400}', b'{"value":-1e400}',
                                b'{"value":' + b'9' * 309 + b'}'])
def test_overflow_numeric_json_refuses_before_publication(raw):
    with pytest.raises(ValueError, match="numeric_overflow"):
        runtime.decode(raw)


@pytest.mark.parametrize("raw", [b'{"value":"\\ud800"}', b'\xff',
                                b'\xff\xfe{\x00}\x00'])
def test_json_requires_strict_utf8_without_surrogate_transport(raw):
    with pytest.raises((UnicodeError, ValueError)):
        runtime.decode(raw)


@pytest.mark.parametrize("mutation", ["empty", "long", "surrogate", "coverage_control"])
def test_canonical_rendered_text_refuses_invalid_unicode_and_page_bounds(monkeypatch, mutation):
    document = controlled_producer_row(monkeypatch)
    row = document["tables"]["FIRSTBAR"]["rows"][0]["row"]
    if mutation == "coverage_control":
        row["coverage_note"] = "not plain\nCONTROLLED"
    else:
        row["summary"] = {"empty": "", "long": "x" * 2049,
                          "surrogate": "\ud800"}[mutation]
    with pytest.raises((UnicodeError, ValueError)):
        runtime.validate(document, NOW)


def test_known_amount_overflow_refuses_before_publication(monkeypatch):
    document = controlled_producer_row(monkeypatch)
    document["tables"]["FIRSTBAR"]["rows"][0]["row"]["pnl"] = "1e400"
    with pytest.raises(ValueError, match="finite_known_row"):
        runtime.validate(document, NOW)


def test_wrapper_note_cannot_substitute_for_page_inner_note(monkeypatch):
    document = controlled_producer_row(monkeypatch)
    wrapper = document["tables"]["FIRSTBAR"]["rows"][0]
    wrapper["coverage_note"] = wrapper["row"].pop("coverage_note")
    with pytest.raises(ValueError, match="coverage_note"):
        runtime.validate(document, NOW)


def test_producer_atomic_file_roundtrips_exact_v3_reader_and_render(monkeypatch, tmp_path):
    document = controlled_producer_row(monkeypatch)
    row = document["tables"]["FIRSTBAR"]["rows"][0]["row"]
    tmp_path.chmod(0o700)
    path = tmp_path / "current.json"
    runtime.publish(document, path, NOW)
    reader = external_page_module("today_file", monkeypatch)
    external_page_module("cp_sim_display", monkeypatch)
    display = external_page_module("today_display", monkeypatch)
    context = reader.read_today_file(path, now=NOW)
    table = context["tables"]["FIRSTBAR"]
    assert context["file_status"] == "AVAILABLE"
    assert table["display_status"] == "UNKNOWN"
    assert table["rows"][0] == row
    assert table["rows"][0]["pnl"] == "10.00"
    assert table["rows"][0]["summary"] == "TARGET_5 <CONTROLLED>"
    fields = ("ticker", "path", "quantity", "entry_time", "entry_price", "exit_time",
              "exit_price", "pnl_label", "summary")

    def controlled_nine_cell_renderer(item):
        return "<tr>" + "".join("<td>" + escape(item[field]) + "</td>"
                                for field in fields) + "</tr>"

    html = display.render_today_page(context, render_row=controlled_nine_cell_renderer)
    assert 'class="sim-row-coverage"' in html
    assert escape(row["coverage_note"]) in html
    assert "TARGET_5 &lt;CONTROLLED&gt;" in html
    assert "TARGET_5 <CONTROLLED>" not in html
    assert "Simulation coverage UNKNOWN" in html
