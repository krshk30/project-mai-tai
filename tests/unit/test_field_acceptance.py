from __future__ import annotations

from datetime import UTC, datetime
import importlib.util
from pathlib import Path
import subprocess
import sys


SCRIPT = Path(__file__).resolve().parents[2] / "ops" / "health" / "field_acceptance.py"
SPEC = importlib.util.spec_from_file_location("field_acceptance", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
tool = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = tool
SPEC.loader.exec_module(tool)

CHECK = tool.BROKER_ORDER_EVENT_SOURCE
SINCE = datetime(2026, 8, 24, 21, 28, tzinfo=UTC)
UNTIL = datetime(2026, 8, 25, 20, 0, tzinfo=UTC)


def _run(counts):
    return tool.run_check(CHECK, SINCE, UNTIL, query=lambda _spec, _since, _until: counts)


def test_known_positive_at_least_one_rejection_carries_a_classified_source() -> None:
    code, output = _run(tool.Counts(CHECK.result_key, 4, 3, 1, 0, 0))

    assert code == tool.PASS
    assert "=> PASS" in output
    assert "matched=3 of 4" in output
    assert "table=broker_order_events  field=event_source" in output
    assert "denominator=event_type = 'rejected'" in output


def test_known_negative_unknown_source_is_fail_not_pass() -> None:
    code, output = _run(tool.Counts(CHECK.result_key, 4, 0, 4, 0, 0))

    assert code == tool.FAIL
    assert "=> FAIL" in output
    assert "predicate matched 0 time(s), below minimum 1" in output


def test_zero_of_zero_is_unexercised_not_pass() -> None:
    code, output = _run(tool.Counts(CHECK.result_key, 0, 0, 0, 0, 0))

    assert code == tool.UNEXERCISED
    assert "=> UNEXERCISED" in output
    assert "PASS" not in output


def test_null_field_is_void_could_not_tell() -> None:
    code, output = _run(tool.Counts(CHECK.result_key, 2, 1, 0, 1, 0))

    assert code == tool.VOID
    assert "=> VOID_COULD_NOT_TELL" in output
    assert "NULL event_source" in output


def test_unknown_field_vocabulary_is_void_could_not_tell() -> None:
    code, output = _run(tool.Counts(CHECK.result_key, 2, 1, 0, 0, 1))

    assert code == tool.VOID
    assert "unrecognised event_source value" in output


def test_mismatched_population_is_void_could_not_tell() -> None:
    code, output = _run(tool.Counts(CHECK.result_key, 3, 2, 0, 0, 0))

    assert code == tool.VOID
    assert "field buckets total 2, but denominator is 3" in output


def test_query_failure_is_void_could_not_tell(monkeypatch) -> None:
    def failed_run(*_args, **_kwargs):
        return subprocess.CompletedProcess([], 1, stdout="", stderr="permission denied")

    monkeypatch.setattr(tool.subprocess, "run", failed_run)
    code, output = tool.run_check(CHECK, SINCE, UNTIL)

    assert code == tool.VOID
    assert "=> VOID_COULD_NOT_TELL. read-only query failed: permission denied" in output
    assert "table=broker_order_events  field=event_source" in output
    assert "window=event_at" in output


def test_wrong_result_key_is_void_even_when_counts_would_otherwise_pass() -> None:
    code, output = _run(tool.Counts("different_population", 2, 2, 0, 0, 0))

    assert code == tool.VOID
    assert "result key does not match" in output


def test_window_requires_timezone_and_positive_width() -> None:
    try:
        tool.parse_instant("2026-08-24T21:28:00", "--since")
    except ValueError as exc:
        assert "explicit UTC offset" in str(exc)
    else:
        raise AssertionError("a timezone-free window was accepted")

    code, output = tool.run_check(CHECK, UNTIL, SINCE)
    assert code == tool.VOID
    assert "start must be earlier" in output


def test_query_is_immutable_read_only_and_uses_psql_variables(monkeypatch) -> None:
    captured: list[str] = []

    def successful_run(command, **_kwargs):
        captured.extend(command)
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=f"{CHECK.result_key}|1|1|0|0|0\n",
            stderr="",
        )

    monkeypatch.setattr(tool.subprocess, "run", successful_run)
    counts = tool.query_counts(CHECK, SINCE, UNTIL)

    assert counts.matched == 1
    sql = captured[captured.index("-c") + 1]
    assert "BEGIN READ ONLY" in sql
    assert "FROM broker_order_events" in sql
    assert ":'window_since'" in sql and ":'window_until'" in sql
    assert "project_mai_tai" in captured
    assert any(item.startswith("window_since=") for item in captured)
    assert any(item.startswith("window_until=") for item in captured)


def test_cli_refuses_unknown_check_without_exposing_sql(capsys) -> None:
    code = tool.main(
        [
            "--check",
            "select-anything",
            "--since",
            "2026-08-24T21:28:00Z",
            "--until",
            "2026-08-25T20:00:00Z",
        ]
    )

    assert code == tool.VOID
    output = capsys.readouterr().out
    assert "VOID_COULD_NOT_TELL" in output
    assert "broker-order-event-source" in output
