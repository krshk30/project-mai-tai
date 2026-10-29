"""Controlled source mechanics, never a known TODAY/native economic receipt."""

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys
import time

import pytest


ROOT = Path(__file__).resolve().parents[2]


def module(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "tools" / (name + ".py"))
    result = importlib.util.module_from_spec(spec)
    sys.modules[name] = result
    spec.loader.exec_module(result)
    return result


runtime = module("atrsimlive1_runtime")
NOW = datetime(2026, 10, 28, 18, 0, tzinfo=timezone.utc)


def unavailable():
    table = dict(updated_at=None, source_asof=None, read_status="UNAVAILABLE",
                 source_id="controlled:missing", issues=["UNKNOWN_INPUT"], rows=[],
                 known_pnl_label="UNKNOWN", skipped_count=None, unknown_count=None,
                 truncated=False)
    return dict(schema=runtime.SCHEMA, trading_day="2026-10-28", timezone="America/New_York",
                generated_at=NOW.isoformat(), source_asof=None,
                producer={"label": "CONTROLLED"}, native_execution_qualified=False,
                tables={key: deepcopy(table) for key in runtime.KEYS})


def test_missing_inputs_remain_explicit_unknown_not_empty_success():
    value = runtime.validate(unavailable(), NOW)
    assert all(t["unknown_count"] is None and t["known_pnl_label"] == "UNKNOWN"
               and t["read_status"] == "UNAVAILABLE" for t in value["tables"].values())
    bad = unavailable()
    bad["tables"]["FIRSTBAR"]["read_status"] = "AVAILABLE"
    with pytest.raises(ValueError, match="evidence"):
        runtime.validate(bad, NOW)


@pytest.mark.parametrize("change", ["day", "generation", "source"])
def test_wrong_day_and_future_watermark_refuse_instead_of_retiming(change):
    value = unavailable()
    if change == "day":
        value["trading_day"] = "2026-10-27"
    else:
        value["generated_at" if change == "generation" else "source_asof"] = "2026-10-29T00:00:00+00:00"
    with pytest.raises(ValueError):
        runtime.validate(value, NOW)


@pytest.mark.parametrize("raw", [b'{"x":1,"x":2}', b'{"x":NaN}', b'{"x":Infinity}'])
def test_duplicate_and_nonfinite_json_refuse(raw):
    with pytest.raises(ValueError):
        runtime.decode(raw)


def test_unknown_world_cannot_label_full_pnl_zero():
    value = unavailable()
    value["tables"]["FIRSTBAR"]["known_pnl_label"] = "$0.00"
    with pytest.raises(ValueError, match="full_economics"):
        runtime.validate(value, NOW)


def test_missing_coverage_note_does_not_gain_a_row_claim():
    value = unavailable()
    value["tables"]["FIRSTBAR"]["rows"] = [
        {"identity": {"trading_day": "2026-10-28"}, "row": {"ticker": "CONTROLLED"}}]
    with pytest.raises(ValueError, match="coverage_note"):
        runtime.validate(value, NOW)


def test_atomic_failure_preserves_previous_file_and_removes_temp(tmp_path, monkeypatch):
    tmp_path.chmod(0o700)
    path = tmp_path / "current.json"
    path.write_bytes(b"old retained bytes")
    monkeypatch.setattr(runtime.os, "replace", lambda *_: (_ for _ in ()).throw(OSError("controlled")))
    with pytest.raises(OSError):
        runtime.publish(unavailable(), path, NOW)
    assert path.read_bytes() == b"old retained bytes"
    assert not list(tmp_path.glob(".current-*.tmp"))


def test_atomic_success_retains_existing_reader_mode(tmp_path):
    tmp_path.chmod(0o700)
    path = tmp_path / "current.json"
    path.write_bytes(b"old")
    path.chmod(0o640)
    runtime.publish(unavailable(), path, NOW)
    assert path.stat().st_mode & 0o777 == 0o640
    assert runtime.decode(path.read_bytes())["trading_day"] == "2026-10-28"


def test_output_symlink_never_overwrites_target(tmp_path):
    tmp_path.chmod(0o700)
    target, path = tmp_path / "target", tmp_path / "current.json"
    target.write_bytes(b"unchanged")
    path.symlink_to(target)
    with pytest.raises(ValueError, match="regular"):
        runtime.publish(unavailable(), path, NOW)
    assert target.read_bytes() == b"unchanged"


def test_second_invocation_cannot_overlap_same_output(tmp_path):
    tmp_path.chmod(0o700)
    with runtime.exclusive_run(tmp_path):
        with pytest.raises(BlockingIOError):
            with runtime.exclusive_run(tmp_path):
                pytest.fail("second owner entered")


def test_deadline_joins_owned_child_before_return(monkeypatch):
    original, children = runtime.subprocess.Popen, []

    def record(*args, **kwargs):
        child = original(*args, **kwargs)
        children.append(child)
        return child

    monkeypatch.setattr(runtime.subprocess, "Popen", record)
    started = time.monotonic()
    with pytest.raises(TimeoutError):
        runtime.bounded_child([sys.executable, "-c", "import time; time.sleep(60)"], seconds=.1)
    assert time.monotonic() - started < 5
    assert len(children) == 1 and children[0].poll() is not None
    assert children[0].stdout.closed and children[0].stderr.closed


def test_null_condition_never_becomes_recorded_blank(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "tools"))
    models = module("atrsimlive1_models")
    from project_mai_tai.atr_simulation.types import (
        ConditionRuleEvidence, EventKey, EvidenceKind, ExecutionPolicy, SourceRef, TradePrint,
    )
    from decimal import Decimal

    source = SourceRef("CONTROLLED", "a" * 64, "$", EvidenceKind.CONTROLLED, None)
    policy = ExecutionPolicy("CONTROLLED", source, (ConditionRuleEvidence(1, "CONTROLLED", True, True, True),))
    trade = TradePrint(EventKey(1, 1, "CONTROLLED", source), "CONTROLLED", Decimal("2"), 100)
    missing = models.stored_print_execution(trade, None, policy)
    blank = models.stored_print_execution(trade, "", policy)
    assert missing.conditions_present is False and missing.eligible is None
    assert blank.conditions_present is True and blank.eligible is True


def test_late_stored_bar_is_unknown_for_aggregate_without_backdating(monkeypatch):
    from types import SimpleNamespace

    monkeypatch.syspath_prepend(str(ROOT / "tools"))
    models = module("atrsimlive1_models")
    exact = SimpleNamespace(bar_start_ns=0, available_ns=60000000000)
    late = SimpleNamespace(bar_start_ns=60000000000, available_ns=120000000001)
    assert models.aggregate_clock_issues([exact]) == ()
    assert models.aggregate_clock_issues([exact, late]) == (
        "UNKNOWN_AGGREGATE_STORED_BAR_AVAILABILITY_CONTRACT",)
    assert late.available_ns == 120000000001


@pytest.mark.parametrize("mutation", ["watch", "rows", "gap", "future"])
def test_envelope_refuses_missing_or_changed_actual_witness(tmp_path, monkeypatch, mutation):
    monkeypatch.syspath_prepend(str(ROOT / "tools"))
    coverage = module("atrsimlive1_coverage")
    models = module("atrsimlive1_models")
    collect = sys.modules["atrsimlive1_collect"]
    left = "2026-10-28T14:00:00+00:00"
    right = "2026-10-28T14:01:00+00:00"
    rows = [dict(id=1, event_ts=left, price="2", size=100),
            dict(id=2, event_ts="2026-10-28T14:00:03+00:00", price="2.01", size=100)]
    witnesses = {}
    for kind in ("watch", "subscription", "capture"):
        path = tmp_path / (kind + ".json")
        raw = json.dumps({"label": "CONTROLLED", "kind": kind}).encode()
        path.write_bytes(raw)
        witnesses[kind] = dict(artifact=str(path), pointer="$", sha256=hashlib.sha256(raw).hexdigest())
    window = dict(symbol="CONTROLLED", trading_day="2026-10-28", start=left, end=right,
                  native_market_complete=False, review_status="ACCEPTED_SUPPLIED_ENVELOPE",
                  continuity_witnesses=witnesses, first_print_id=1, last_print_id=2,
                  first_print_ns=models.nanos(left), last_print_ns=models.nanos(rows[1]["event_ts"]),
                  max_interprint_gap_ns=3000000000, row_count=2, rows_sha256=collect.digest(rows),
                  full_read=True, truncated=False)
    metrics = {key: window.pop(key) for key in (
        "first_print_id", "last_print_id", "first_print_ns", "last_print_ns",
        "max_interprint_gap_ns", "row_count", "rows_sha256", "full_read", "truncated")}
    window["expected_census"] = metrics
    proof = dict(provider="massive", table="market_capture_trades", symbol="CONTROLLED",
                 start=left, end=right, full_read=True, truncated=False,
                 row_count=len(rows), rows_sha256=collect.digest(rows))
    assert coverage.verify(window, symbol="CONTROLLED", day="2026-10-28", trades=rows,
                           asof_ns=models.nanos(right), nanos=models.nanos,
                           read_proof=proof)[0] == models.nanos(left)
    if mutation == "watch":
        window["continuity_witnesses"].pop("watch")
    elif mutation == "rows":
        rows[0]["price"] = "9"
    elif mutation == "gap":
        window["expected_census"]["max_interprint_gap_ns"] = 0
    else:
        window["end"] = "2026-10-29T14:00:00+00:00"
    with pytest.raises(ValueError):
        coverage.verify(window, symbol="CONTROLLED", day="2026-10-28", trades=rows,
                        asof_ns=models.nanos(right), nanos=models.nanos, read_proof=proof)
