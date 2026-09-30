from __future__ import annotations

import subprocess
from pathlib import Path

import pytest


ROUTER = Path(__file__).resolve().parents[2] / "ops" / "health" / "preopen_restart_evidence.sh"


@pytest.mark.parametrize(
    ("evidence_rc", "collector_call", "expected_rc", "expected_line"),
    [
        (0, "PASS", 0, "PASS: daily pre-open gate is green"),
        (0, "EXPECTED BY DESIGN", 0, "N/A remains N/A"),
        (1, "REAL FAILURE", 1, "BLOCKED: REAL FAILURE in 1 check group(s)"),
        (2, "UNKNOWN", 2, "BLOCKED: UNKNOWN in 1 check group(s)"),
        (2, "", 2, "BLOCKED: UNKNOWN in 1 check group(s)"),
        (7, "", 2, "UNKNOWN: restart evidence rc=7 disagrees"),
        (1, "", 2, "UNKNOWN: restart evidence rc=1 disagrees"),
        (1, "UNKNOWN", 2, "UNKNOWN: restart evidence rc=1 disagrees"),
        (0, "", 2, "UNKNOWN: restart evidence rc=0 disagrees"),
    ],
)
def test_preopen_routes_restart_evidence_without_collapsing_unknown(
    evidence_rc: int, collector_call: str, expected_rc: int, expected_line: str
) -> None:
    script = """
        source "$1"
        failures=0
        unknowns=0
        fail() { printf 'FAIL: %s\\n' "$*"; failures=$((failures + 1)); }
        pass() { printf 'PASS: %s\\n' "$*"; }
        preopen_record_restart_evidence "$2" /tmp/restart-evidence.md "$3"
        preopen_final_verdict
    """
    output = f"Final call: {collector_call}; measured evidence" if collector_call else "Traceback"
    result = subprocess.run(
        ["bash", "-c", script, "preopen-test", str(ROUTER), str(evidence_rc), output],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == expected_rc
    assert expected_line in result.stdout
    if expected_rc == 2:
        assert "failed" not in result.stdout.lower()


def test_real_failure_takes_precedence_without_erasing_unknown() -> None:
    script = """
        source "$1"
        failures=0
        unknowns=0
        fail() { failures=$((failures + 1)); }
        pass() { :; }
        preopen_record_restart_evidence 2 /tmp/restart-evidence.md 'Final call: UNKNOWN; missing bar'
        fail 'other check'
        preopen_final_verdict
    """
    result = subprocess.run(
        ["bash", "-c", script, "preopen-test", str(ROUTER)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1
    assert "REAL FAILURE in 1 check group(s); unknown groups=1" in result.stdout


def test_duplicate_final_calls_are_unknown() -> None:
    script = """
        source "$1"
        failures=0
        unknowns=0
        fail() { failures=$((failures + 1)); }
        pass() { :; }
        preopen_record_restart_evidence 1 /tmp/restart-evidence.md "$2"
        preopen_final_verdict
    """
    result = subprocess.run(
        [
            "bash", "-c", script, "preopen-test", str(ROUTER),
            "Final call: REAL FAILURE; first\nFinal call: REAL FAILURE; second",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 2
    assert "current final call=missing" in result.stdout


@pytest.mark.parametrize(
    ("collector_rc", "collector_call", "expected_rc", "expected_line"),
    [
        (0, "PASS", 0, "PASS: running flags match reviewed catalog"),
        (1, "REAL FAILURE", 1, "FAIL: running flag mismatch"),
        (2, "UNKNOWN", 2, "UNKNOWN: running flags could not be read"),
        (1, "UNKNOWN", 2, "UNKNOWN: flag check rc=1 disagrees"),
        (0, "", 2, "UNKNOWN: flag check rc=0 disagrees"),
    ],
)
def test_preopen_routes_flag_check_three_ways(
    collector_rc: int, collector_call: str, expected_rc: int, expected_line: str
) -> None:
    script = """
        source "$1"
        failures=0
        unknowns=0
        fail() { printf 'FAIL: %s\\n' "$*"; failures=$((failures + 1)); }
        pass() { printf 'PASS: %s\\n' "$*"; }
        preopen_record_expected_flags "$2" /tmp/expected_flags.json "$3"
        preopen_final_verdict
    """
    output = f"Final call: {collector_call}; checked=1/1" if collector_call else "Traceback"
    result = subprocess.run(
        ["bash", "-c", script, "flag-test", str(ROUTER), str(collector_rc), output],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == expected_rc
    assert expected_line in result.stdout


def test_duplicate_flag_final_calls_are_unknown() -> None:
    script = """
        source "$1"
        failures=0
        unknowns=0
        fail() { failures=$((failures + 1)); }
        pass() { :; }
        preopen_record_expected_flags 0 /tmp/expected_flags.json "$2"
        preopen_final_verdict
    """
    result = subprocess.run(
        [
            "bash", "-c", script, "flag-test", str(ROUTER),
            "Final call: PASS; first\nFinal call: malformed duplicate",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 2
    assert "current final call=missing" in result.stdout
    assert "running flags match reviewed catalog" not in result.stdout


@pytest.mark.parametrize(
    ("checker_rc", "checker_call", "expected_rc", "expected_line"),
    [
        (0, "PASS", 0, "PASS: running flags match reviewed catalog"),
        (1, "REAL FAILURE", 1, "FAIL: running flag mismatch"),
        (2, "UNKNOWN", 2, "UNKNOWN: running flags could not be read"),
        (1, "UNKNOWN", 2, "UNKNOWN: flag check rc=1 disagrees"),
    ],
)
def test_preopen_invokes_flag_checker_and_routes_result(
    tmp_path: Path, checker_rc: int, checker_call: str, expected_rc: int, expected_line: str
) -> None:
    checker = tmp_path / "checker.sh"
    checker.write_text(
        "#!/bin/sh\n"
        'test "$1" = "--catalog" && test "$2" = "/tmp/expected_flags.json" || exit 9\n'
        f"printf 'Final call: {checker_call}; checked=1/1\\n'\n"
        f"exit {checker_rc}\n"
    )
    script = """
        source "$1"
        failures=0
        unknowns=0
        fail() { printf 'FAIL: %s\\n' "$*"; failures=$((failures + 1)); }
        pass() { printf 'PASS: %s\\n' "$*"; }
        preopen_check_expected_flags /bin/sh "$2" /tmp/expected_flags.json
        preopen_final_verdict
    """
    result = subprocess.run(
        ["bash", "-c", script, "flag-test", str(ROUTER), str(checker)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == expected_rc
    assert "=== RUNNING FLAG CONTRACT ===" in result.stdout
    assert expected_line in result.stdout
