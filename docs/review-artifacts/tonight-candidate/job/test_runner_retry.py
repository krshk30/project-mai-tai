"""Exercise the literal Bash retry function offline; no deployment calls."""
import importlib.util
from datetime import UTC, datetime
from pathlib import Path
import subprocess

import pytest

ROOT = Path(__file__).parent


def retry_turn(codes):
    script = (ROOT / "run.sh").read_text()
    function = script[script.index("read_only_retry() {"):script.index("flat_now() {")]
    command = """set -Eeuo pipefail
STAGE=test
run() { "$@"; }
sleep() { printf 'SLEEP %%s\\n' "$1"; }
calls=0
codes=(%s)
probe() { local code=${codes[calls]}; calls=$((calls+1)); return "$code"; }
%s
if read_only_retry probe; then result=0; else result=$?; fi
printf 'FINAL rc=%%s calls=%%s\\n' "$result" "$calls"
""" % (" ".join(map(str, codes)), function)
    return subprocess.run(["bash", "-c", command], capture_output=True, text=True, check=True).stdout


def test_unreadable_then_ok_retries_once_and_proceeds():
    output = retry_turn([2, 0])
    assert "FINAL rc=0 calls=2" in output
    assert output.count("SLEEP 60") == 1
    assert "READ_ONLY_RESULT attempt=1/3 rc=2" in output
    assert "READ_ONLY_RESULT attempt=2/3 rc=0" in output


def test_three_unreadables_stop_after_exactly_three_attempts():
    output = retry_turn([2, 2, 2])
    assert "FINAL rc=2 calls=3" in output
    assert output.count("SLEEP 60") == 2
    assert output.count("READ_ONLY_RESULT") == 3


@pytest.mark.parametrize("codes", ([1], [2, 1], [3]))
def test_measured_blocker_or_unrecognized_code_never_retries(codes):
    output = retry_turn(codes)
    assert f"FINAL rc={codes[-1]} calls={len(codes)}" in output
    assert output.count("SLEEP 60") == len(codes) - 1


def test_every_flat_and_census_call_uses_retry_but_policy_is_not_changed():
    script = (ROOT / "run.sh").read_text()
    assert 'flat_now() { read_only_retry nice -n 19 "$PY" "$JOB/strict_flat_readonly.py"; }' in script
    assert 'census_now() { read_only_retry "$PY" "$JOB/census_readonly.py" --require-reviewed; }' in script
    assert 'run "$PY" "$JOB/census_readonly.py"' not in script
    assert 'run nice -n 19 "$PY" "$JOB/strict_flat_readonly.py"' not in script
    assert "rc != 2 || attempt == 3" in script and "sleep 60" in script


def test_clock_gate_only_before_first_stop_never_between_stop_and_start():
    script = (ROOT / "run.sh").read_text()
    stop = script.index("run systemctl stop project-mai-tai-schwab-1m-v2.service")
    assert "window_now" not in script[stop:]
    before = script[:stop]
    assert before.rstrip().endswith("window_now")
    assert "< 1915" in before


@pytest.mark.parametrize("clock", (datetime(2026, 10, 6, 0, 5, tzinfo=UTC),
                                  datetime(2026, 10, 6, 4, 5, tzinfo=UTC)))
def test_completion_proofs_do_not_abort_on_clock_after_first_stop(monkeypatch, clock):
    # Use UTC00:05 Oct6 =20:05 ET Oct5, and04:05 =00:05 ET Oct6.
    spec = importlib.util.spec_from_file_location("clock_proof", ROOT / "proof.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock.astimezone(tz) if tz else clock.replace(tzinfo=None)

    monkeypatch.setattr(module, "datetime", Clock)
    assert module.wall() == clock.isoformat()
    with pytest.raises(module.Blocked, match="first-stop"):
        module.wall(action=True)


def test_d2_no_new_approval_field_and_inactive_identity_failure_is_retained():
    actions = (ROOT / "actions.py").read_text()
    assert "preopen_inactive_paper_disposition" not in actions
    assert "paper PID0 is real, existing active check retained" in actions
    proof = (ROOT / "proof.py").read_text()
    assert "candidate PID0 stays REAL FAILURE, no routing bypass" in proof


def test_d2_report_annotation_preserves_identity_check_and_verdict(tmp_path):
    spec = importlib.util.spec_from_file_location("paper_proof", ROOT / "proof.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    original = ('#!/bin/bash\ncheck_identity momentum-paper "$PAPER_UNIT" "$EXPECTED_PAPER_PID" "$EXPECTED_PAPER_START"\n'
                'preopen_record_restart_evidence "$evidence_rc" "$REPORT" "$evidence_output"\n'
                'if (( failures )); then exit 1; fi\n')
    candidate = module.annotate_paper_expectation(original)
    for line in original.splitlines():
        assert line in candidate.splitlines()
    assert "EXPECTED D2: momentum-paper inactive/PID0" in candidate
    assert 'sudo -n tee -a "$REPORT"' in candidate
    assert 'fail "cannot record expected inactive-paper disposition in report"' in candidate
    path = tmp_path / "candidate.sh"
    path.write_text(candidate)
    subprocess.run(["bash", "-n", str(path)], check=True)
