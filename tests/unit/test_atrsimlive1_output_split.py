"""NEW controlled output/PAGE grammar. No actual full23 measurement claim."""

from copy import deepcopy
from datetime import timedelta
from decimal import Decimal
import gzip
import hashlib
import json
import os
from pathlib import Path
import stat
import sys

import pytest
from starlette.responses import HTMLResponse

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))
import atrsimlive1_output as output
from project_mai_tai.atr_simulation import today_file, today_display
from project_mai_tai.services import control_plane as cp
from tests.unit.test_atrsimlive1_today_page import document, NOW


def original(symbol="ELPW"):
    value = document()
    value["producer"].update(observed_symbols=[symbol], snapshot_id="PRIVATE_SNAPSHOT_ID",
        input_refs={"bars": "a"*64, "trades": "b"*64, "quotes": "c"*64},
        engine_receipts={}, c1_buy_parity=[], raw_private_path="/private/tmp/retained-input")
    for key, table in value["tables"].items():
        table["rows"][0]["identity"].update(symbol=symbol, account_id="PRIVATE_ACCOUNT_ID",
            flip_id="PRIVATE_FLIP_ID", segment_id="PRIVATE_SEGMENT_ID")
        table["rows"][0]["row"]["ticker"] = symbol
        table["rows"][0]["outcome_id"] = "PRIVATE_OUTCOME_ID:" + key
        value["producer"]["engine_receipts"]["PRIVATE_OUTCOME_ID:" + key] = {
            "issues": ["UNKNOWN_NATIVE"], "raw_print_ids": ["PRIVATE_PRINT_1", "PRIVATE_PRINT_2"]}
    return value


def directory(tmp_path, name="run"):
    path = tmp_path / name
    path.mkdir(mode=0o700)
    return path


def assembled(tmp_path, docs=None):
    docs = [original()] if docs is None else docs
    path = directory(tmp_path)
    components = []
    with output.EvidenceWriter(path) as writer:
        for doc in docs:
            symbol = doc["producer"]["observed_symbols"][0]
            writer.append("input", {"full_original_bars": [{"id": "RAW_BAR", "close": Decimal("1.230000000000001")}],
                                    "full_original_trades": [{"id": "RAW_PRINT", "conditions": None}]}, symbol=symbol)
            writer.append("model", doc, symbol=symbol)
            components.append(output.project_component(doc))
        seal = writer.seal()
    compact = output.compact_document(components, evidence=seal, timings={"collect_seconds": 49.017})
    return path, compact, seal


def html(value, *, now=NOW):
    checked = today_file.validate_today_document(value, now=now)
    return checked, HTMLResponse(today_display.render_today_page(
        checked, render_row=cp._render_completed_position_row)).body.decode("utf-8")


def test_one_streamed_gzip_preserves_all_original_fields_and_readback_hash(tmp_path):
    path, compact, seal = assembled(tmp_path)
    raw = (path / output.NAME).read_bytes()
    assert hashlib.sha256(raw).hexdigest() == seal["sha256"]
    expanded = gzip.decompress(raw)
    assert hashlib.sha256(expanded).hexdigest() == seal["uncompressed_sha256"]
    assert len(expanded) == seal["uncompressed_bytes"] and len(raw) == seal["bytes"]
    records = [json.loads(line) for line in expanded.splitlines()]
    assert [r["index"] for r in records] == [0, 1]
    assert records[0]["payload"]["full_original_bars"][0]["close"] == "1.230000000000001"
    assert records[0]["payload"]["full_original_trades"][0]["conditions"] is None
    assert records[1]["payload"] == original()
    assert seal["records_by_symbol"] == {"ELPW": {"input": 1, "model": 1}}
    assert list(path.iterdir()) == [path / output.NAME]
    assert stat.S_IMODE((path / output.NAME).stat().st_mode) == 0o600


def test_compact_projection_has_no_raw_ids_private_paths_witnesses_or_model_trees(tmp_path):
    path, compact, seal = assembled(tmp_path)
    raw = output.encode_compact(compact)
    for marker in (b"PRIVATE_", b"raw_print_ids", b"full_original_bars", b"engine_receipts", b"/private/"):
        assert marker not in raw
    assert set(compact["tables"]["FIRSTBAR"]["rows"][0]["identity"]) == {"trading_day", "symbol", "display_key"}
    assert compact["run_receipt"]["unknown_counts_by_cause"] == {"UNKNOWN_NATIVE": 7}
    assert compact["run_receipt"]["counts"]["rows_by_table"] == {key: 1 for key in output.KEYS}


def test_controlled_all23_large_evidence_keeps_every_row_and_stdout_under_one_mib(tmp_path):
    docs = [original("S" + str(index)) for index in range(23)]
    for doc in docs:
        doc["producer"]["large_full_receipt"] = "FULL_EVIDENCE_ONLY" + "x"*800000
    path, compact, seal = assembled(tmp_path, docs)
    raw = output.encode_compact(compact)
    assert seal["uncompressed_bytes"] > 16*1024*1024
    assert len(raw) < 1024*1024 and compact["run_receipt"]["counts"]["symbols"] == 23
    assert all(len(table["rows"]) == 23 for table in compact["tables"].values())
    assert seal["records"] == 46 and len(list(path.glob("*.gz"))) == 1


def test_pure_projector_does_not_mutate_inputs_or_write_files(tmp_path, monkeypatch):
    doc = original()
    before = deepcopy(doc)
    monkeypatch.setattr(output.os, "open", lambda *a, **k: pytest.fail("pure projection must not open files"))
    projected = output.project_component(doc)
    assert doc == before and len(projected["tables"]) == 7 and list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("kind", ["input", "model"])
def test_missing_original_input_or_model_refuses_evidence_seal(tmp_path, kind):
    path = directory(tmp_path)
    with output.EvidenceWriter(path) as writer:
        writer.append(kind, {"original": True}, symbol="ELPW")
        with pytest.raises(ValueError, match="complete_nonempty"):
            writer.seal()
    assert not (path / output.NAME).exists()
    assert (path / ("." + output.NAME + ".partial")).exists()


def test_repeated_writer_never_overwrites_or_creates_second_gzip(tmp_path):
    path, compact, seal = assembled(tmp_path)
    before = (path / output.NAME).read_bytes()
    with pytest.raises(ValueError, match="already_exists"):
        output.EvidenceWriter(path)
    assert (path / output.NAME).read_bytes() == before


def test_failed_record_refuses_compaction_no_retry_and_retains_partial(tmp_path):
    path = directory(tmp_path)
    with output.EvidenceWriter(path) as writer:
        with pytest.raises(TypeError, match="unsupported_evidence"):
            writer.append("input", {"unsupported": object()}, symbol="ELPW")
        with pytest.raises(ValueError):
            writer.append("model", {}, symbol="ELPW")
        with pytest.raises(ValueError):
            writer.seal()
    with pytest.raises(FileExistsError):
        output.EvidenceWriter(path)
    assert not (path / output.NAME).exists()


def test_explicit_evidence_cap_refuses_instead_of_dropping_rows(tmp_path, monkeypatch):
    monkeypatch.setattr(output, "MAX_EVIDENCE_BYTES", 10)
    path = directory(tmp_path)
    with output.EvidenceWriter(path) as writer:
        with pytest.raises(ValueError, match="no_truncation"):
            writer.append("input", {"original": "whole fact"}, symbol="ELPW")
        with pytest.raises(ValueError):
            writer.seal()
    assert not (path / output.NAME).exists()


def test_fsync_failure_never_returns_sealed_evidence(tmp_path, monkeypatch):
    path = directory(tmp_path)
    with output.EvidenceWriter(path) as writer:
        writer.append("input", {}, symbol="ELPW")
        writer.append("model", {}, symbol="ELPW")
        monkeypatch.setattr(output.os, "fsync", lambda *a: (_ for _ in ()).throw(OSError("controlled")))
        with pytest.raises(OSError):
            writer.seal()
    assert not (path / output.NAME).exists()


def test_atomic_final_race_cannot_overwrite_existing_evidence(tmp_path):
    path = directory(tmp_path)
    with output.EvidenceWriter(path) as writer:
        writer.append("input", {}, symbol="ELPW")
        writer.append("model", {}, symbol="ELPW")
        (path / output.NAME).write_bytes(b"retained foreign artifact")
        with pytest.raises(FileExistsError):
            writer.seal()
    assert (path / output.NAME).read_bytes() == b"retained foreign artifact"


def test_symbol_population_requires_exact_matching_evidence(tmp_path):
    path, compact, seal = assembled(tmp_path)
    with pytest.raises(ValueError, match="population_mismatch"):
        output.compact_document([output.project_component(original("FTFT"))], evidence=seal, timings={})
    with pytest.raises(ValueError, match="sealed_evidence"):
        output.compact_document([output.project_component(original())], evidence=dict(seal), timings={})


def test_compact_cap_is_failclosed_without_mutating_population(tmp_path, monkeypatch):
    path, compact, seal = assembled(tmp_path)
    before = deepcopy(compact)
    monkeypatch.setattr(output, "MAX_STDOUT", 100)
    with pytest.raises(ValueError, match="no_population_truncation"):
        output.encode_compact(compact)
    assert compact == before and sum(len(t["rows"]) for t in compact["tables"].values()) == 7


@pytest.mark.parametrize("mode", ["symlink", "public"])
def test_evidence_directory_security_refuses_symlink_or_nonprivate(tmp_path, mode):
    path = directory(tmp_path)
    if mode == "public":
        path.chmod(0o755)
    else:
        link = tmp_path / "link"
        link.symlink_to(path)
        path = link
    with pytest.raises(ValueError):
        output.EvidenceWriter(path)


def test_page_reads_compact_only_never_opens_gzip_and_preserves_real_rows(tmp_path, monkeypatch):
    path, compact, seal = assembled(tmp_path)
    raw = output.encode_compact(compact)
    current = path / "current.json"
    current.write_bytes(raw)
    monkeypatch.setattr(gzip, "open", lambda *a, **k: pytest.fail("HTTP cannot decompress evidence"))
    (path / output.NAME).unlink()  # Reader needs no local evidence access/permissions.
    checked = today_file.read_today_file(current, now=NOW)
    page = HTMLResponse(today_display.render_today_page(checked, render_row=cp._render_completed_position_row))
    assert page.status_code == 200 and checked["file_status"] == "AVAILABLE"
    assert page.body.count(b'data-sim-mode="') == 7 and b'PRIVATE_' not in page.body
    assert checked["tables"]["LIVE:ACTUAL"]["rows"][0]["quantity"] == "1"
    assert b"signal UNKNOWN; status AVAILABLE; SIM" in page.body


@pytest.mark.parametrize("fault", ["raw_field", "private_path", "surrogate", "future_signal", "duplicate", "bad_count", "evidence_path", "receipt_raw"])
def test_compact_adversaries_unknown_not_http_500_or_silent_row_loss(tmp_path, fault):
    path, compact, seal = assembled(tmp_path)
    row = compact["tables"]["FIRSTBAR"]["rows"][0]
    if fault == "raw_field":
        row["row"]["raw_print_ids"] = ["forbidden"]
    elif fault == "private_path":
        row["row"]["summary"] = "evidence:/private/tmp/raw.json"
    elif fault == "surrogate":
        row["row"]["label"] = "\ud800"
    elif fault == "future_signal":
        row["row"]["signal_time"] = "2026-10-29T09:30:00-04:00"
    elif fault == "duplicate":
        compact["tables"]["FIRSTBAR"]["rows"].append(deepcopy(row))
        compact["run_receipt"]["counts"]["rows_by_table"]["FIRSTBAR"] = 2
    elif fault == "bad_count":
        compact["run_receipt"]["counts"]["rows_by_table"]["FIRSTBAR"] = 0
    elif fault == "evidence_path":
        compact["run_receipt"]["evidence"]["file"] = "/private/tmp/raw.gz"
    else:
        compact["producer"]["engine_receipts"] = {"private": True}
    current = path / "current.json"
    current.write_text(json.dumps(compact, ensure_ascii=True), encoding="utf-8")
    checked = today_file.read_today_file(current, now=NOW)
    page = HTMLResponse(today_display.render_today_page(checked, render_row=cp._render_completed_position_row))
    assert page.status_code == 200
    assert checked["file_status"] != "AVAILABLE" or checked["tables"]["FIRSTBAR"]["display_status"] == "INVALID_TABLE"
    assert checked["tables"]["FIRSTBAR"]["rows"] == []


def test_compact_keeps_today_freshness_and_legacy_reader_contract(tmp_path):
    path, compact, seal = assembled(tmp_path)
    checked, page = html(compact, now=NOW + timedelta(minutes=11))
    assert checked["tables"]["FIRSTBAR"]["display_status"] == "STALE"
    assert "simulation not updated since" in page
    legacy, legacy_page = html(document())
    assert legacy["file_status"] == "AVAILABLE" and "run_receipt" not in legacy
    assert legacy_page.count('data-sim-mode="') == 7


def test_unknown_economics_and_real_legacy_percentage_remain_original(tmp_path):
    doc = original()
    real = doc["tables"]["LIVE:ACTUAL"]["rows"][0]["row"]
    real.update(quantity="7", pnl="0.70000000000001", pnl_label_known=True,
                pnl_label="$+0.70", pnl_pct_label="+5.00%", coverage_note="R"*600)
    path, compact, seal = assembled(tmp_path, [doc])
    checked, page = html(compact)
    assert "$+0.70 (+5.00%)" in page and "R"*600 in page
    assert checked["tables"]["FIRSTBAR"]["rows"][0]["pnl"] is None
    assert "UNKNOWN (USD UNKNOWN)" in page


def test_input_model_coverage_never_invented_when_source_display_invalid(tmp_path):
    doc = original()
    doc["tables"]["FIRSTBAR"]["rows"][0]["row"]["summary"] = "\ud800"
    with pytest.raises(ValueError, match="no_row_drop"):
        output.project_component(doc)


def test_original_signal_status_label_and_parity_disagreements_visible(tmp_path):
    doc = original()
    doc["tables"]["FIRSTBAR"]["rows"][0]["row"].update(
        signal_time="2026-10-28T09:02:00-04:00", status="UNKNOWN_QUEUE", same_sip_policy_label="tie → conservative")
    doc["producer"]["c1_buy_parity"] = [{"comparisons": [dict(symbol="FTFT", reference="15:32",
        reference_direction="BUY", status="MISMATCH", raw_bar_ids=["RAW_BAR"])]}]
    path, compact, seal = assembled(tmp_path, [doc])
    checked, page = html(compact)
    assert "signal 2026-10-28T09:02:00-04:00; status UNKNOWN_QUEUE; tie → conservative" in page
    assert "C1-reported FTFT 15:32 BUY: MISMATCH" in page
    assert "raw_bar_ids" not in output.encode_compact(compact).decode()


def test_readback_hash_failure_never_returns_compact_publication_token(tmp_path, monkeypatch):
    path = directory(tmp_path)
    actual = output._file_hash
    calls = []

    def checked(fd):
        result = actual(fd)
        calls.append(result)
        return result if len(calls) == 1 else ("0"*64, result[1])

    monkeypatch.setattr(output, "_file_hash", checked)
    with output.EvidenceWriter(path) as writer:
        writer.append("input", {}, symbol="ELPW")
        writer.append("model", {}, symbol="ELPW")
        with pytest.raises(ValueError, match="readback_mismatch"):
            writer.seal()
    assert len(calls) == 2 and (path / output.NAME).exists()  # Failed artifact retained.


def test_repeated_physical_witness_rows_are_all_retained_not_deduplicated(tmp_path):
    path = directory(tmp_path)
    with output.EvidenceWriter(path) as writer:
        writer.append("input", {}, symbol="ELPW")
        writer.append("witness", {"physical_id": "same", "sequence": 1}, symbol="ELPW")
        writer.append("witness", {"physical_id": "same", "sequence": 1}, symbol="ELPW")
        writer.append("model", {}, symbol="ELPW")
        seal = writer.seal()
    records = [json.loads(line) for line in gzip.decompress((path / output.NAME).read_bytes()).splitlines()]
    assert seal["records"] == 4 and records[1]["payload"] == records[2]["payload"]
    assert records[1]["index"] != records[2]["index"]


@pytest.mark.parametrize("fault", ["duplicate_symbol", "different_snapshot"])
def test_component_join_never_aliases_population_or_mixes_snapshot(tmp_path, fault):
    path, compact, seal = assembled(tmp_path)
    first = output.project_component(original())
    second = deepcopy(first)
    if fault == "different_snapshot":
        second["component"]["snapshot_hash"] = "f"*64
    with pytest.raises(ValueError, match="duplicate_symbol|same_day_snapshot"):
        output.compact_document([first, second], evidence=seal, timings={})


@pytest.mark.parametrize("seconds", [float("nan"), float("inf"), -1, True])
def test_compact_timings_cannot_claim_nonfinite_negative_or_boolean_values(tmp_path, seconds):
    path, compact, seal = assembled(tmp_path)
    with pytest.raises(ValueError, match="finite_timing"):
        output.compact_document([output.project_component(original())], evidence=seal, timings={"model": seconds})


def test_known_percent_and_exact_usd_are_projected_not_recalculated(tmp_path):
    doc = original()
    row = doc["tables"]["FIRSTBAR"]["rows"][0]["row"]
    row.update(pnl="0.100000000000000000001", pnl_label_known=True, pnl_label="$+0.10",
               pnl_pct="5.000000000000001", pnl_pct_label_known=True, pnl_pct_label="+5.00%",
               sizing_status="RECORDED_ROUTE_SIZED", percent_basis="sized price return")
    path, compact, seal = assembled(tmp_path, [doc])
    projected = compact["tables"]["FIRSTBAR"]["rows"][0]["row"]
    assert projected["pnl"] == row["pnl"] and projected["pnl_pct"] == row["pnl_pct"]
    assert projected["quantity"] == row["quantity"]
    checked, page = html(compact)
    assert "+5.00% (USD $+0.10)" in page


def test_actual_receipt_decision_is_not_replaced_with_a_success_status(tmp_path):
    doc = original()
    doc["producer"]["engine_receipts"]["PRIVATE_OUTCOME_ID:FIRSTBAR"]["decision"] = "SKIP"
    projected = output.project_component(doc)
    assert projected["tables"]["FIRSTBAR"]["rows"][0]["row"]["status"] == "SKIP"


def test_legacy_unused_signal_metadata_does_not_enter_new_compact_display_path(tmp_path):
    doc = document()
    baseline, original_page = html(doc)
    doc["tables"]["FIRSTBAR"]["rows"][0]["row"].update(
        signal_time={"opaque": "\ud800"}, status=None, label=["unused"])
    checked, page = html(doc)
    assert checked["file_status"] == "AVAILABLE" and page == original_page


def test_gzip_write_failure_retains_failure_and_cannot_seal(tmp_path):
    path = directory(tmp_path)
    with output.EvidenceWriter(path) as writer:
        actual = writer.gz

        class FailedWrite:
            def write(self, raw):
                raise OSError("controlled write failure")

            def close(self):
                actual.close()

        writer.gz = FailedWrite()
        with pytest.raises(OSError):
            writer.append("input", {"original": "not silently dropped"}, symbol="ELPW")
        with pytest.raises(ValueError):
            writer.seal()
    assert not (path / output.NAME).exists()


def test_absent_parity_facts_are_unknown_not_zero_of_zero_pass(tmp_path):
    path, compact, seal = assembled(tmp_path)
    checked, page = html(compact)
    assert "Parity facts UNKNOWN: no supplied comparisons; not a 0/0 PASS." in page
