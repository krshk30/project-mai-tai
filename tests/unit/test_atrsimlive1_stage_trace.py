"""Changed stage evidence only; typed fake DB, no tape/provider connection."""
import importlib
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "tools"), str(Path(__file__).parent)]
from test_atrsimlive1_collect_close import controlled, invoke


@pytest.fixture
def progress(monkeypatch):
    module = importlib.import_module("atrsimlive1_progress")
    monkeypatch.setattr(module, "_written", 0)
    monkeypatch.setattr(module.time, "time_ns", lambda: 1793226000123000000)
    return module


def test_compact_query_clock_and_actual_row_count(progress, capsys):
    progress.stage("Q12", "+")
    progress.stage("Q12", "-", 5001)
    assert capsys.readouterr().err == "1793226000123 Q12+\n1793226000123 Q12-:5001\n"


def test_trace_rejects_unsanitized_label_and_noninteger_rows(progress, capsys):
    for code, rows in (("dsn=secret", None), ("Q1", -1), ("Q1", True), ("Q1", 1.5)):
        with pytest.raises(ValueError, match="stage_literal_shape"):
            progress.stage(code, "+", rows)
    assert capsys.readouterr().err == ""


def test_trace_never_raises_existing_stderr_cap(progress, monkeypatch, capsys):
    monkeypatch.setattr(progress, "_written", 60000)
    with pytest.raises(ValueError, match="stage_stderr_budget"):
        progress.stage("Q1", "+")
    assert capsys.readouterr().err == ""


def test_parent_streams_fragmented_clock_but_never_raw_child_errors(progress, capsys):
    parent = progress.StageForwarder()
    parent.feed(b"password=CONTROLLED_SECRET\n1793226000123 Q1")
    parent.feed(b"+\nTraceback CONTROLLED_RAW_ERROR\n1793226000123 Q1-:5\n")
    assert capsys.readouterr().err == "1793226000123 Q1+\n1793226000123 Q1-:5\n"


def test_query_trace_precedes_closed_snapshot_then_models(progress, monkeypatch, capsys):
    collect, connection, _, _, _, _ = controlled(monkeypatch)
    seen = []
    def processor(data):
        assert connection.closed
        seen.append(data["symbols"])
        return data
    documents = invoke(collect, processor)
    lines = capsys.readouterr().err.splitlines()
    assert len(documents) == len(seen) == 3
    closed = next(i for i, line in enumerate(lines) if "DB_CLOSED-" in line)
    first_model = next(i for i, line in enumerate(lines) if "MODEL:" in line)
    assert closed < first_model
    assert any("Q1-:1" in line for line in lines)
    assert any("Q2-:3" in line for line in lines)
    assert all("CONTROLLED-NOT-A-DSN" not in line for line in lines)


def test_failed_query_keeps_raw_failure_and_rollback_without_model(progress, monkeypatch, capsys):
    collect, connection, _, _, _, _ = controlled(monkeypatch, fail_capture=True)
    with pytest.raises(RuntimeError, match="CONTROLLED source query failure"):
        invoke(collect, lambda data: pytest.fail("no partial models"))
    lines = capsys.readouterr().err
    assert connection.closed and connection.commands[-1] == "ROLLBACK"
    assert "ROLLBACK-" in lines and "MODEL:" not in lines
