"""Clock/retry admission without a broker, service action or installed gate."""
import os
from pathlib import Path
import subprocess

import pytest

SOURCE = Path(__file__).with_name("runner_mechanics.sh")


def shell(body, *, day="2026-10-06", clock="160001"):
    # No filesystem fake executable is needed; commands remain shell functions.
    script = """set -u
date() {
  case "$1" in
    +%F) printf '%s' "$FAKE_DAY";;
    +%H%M%S) printf '%s' "$FAKE_CLOCK";;
    *) return 9;;
  esac
}
source "$SOURCE"
STAGE=test
""" + body
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True,
                          env={**os.environ, "SOURCE": str(SOURCE), "FAKE_DAY": day,
                               "FAKE_CLOCK": clock}, timeout=5)


@pytest.mark.parametrize("day,clock,allowed", (
    ("2026-10-06", "155959", False), ("2026-10-06", "160000", False),
    ("2026-10-06", "160001", True), ("2026-10-06", "180000", True),
    ("2026-10-06", "200000", True), ("2026-10-06", "200500", True),
    ("2026-10-06", "235959", True), ("2026-10-07", "160001", False),
))
def test_first_stop_after_close_without_rotation_wait(day, clock, allowed):
    result = shell("window_now", day=day, clock=clock)
    assert (result.returncode == 0) is allowed


def test_clock_only_override_before_18_never_names_armed_symbols():
    result = shell("""
REPO=/reviewed
/reviewed/ops/preflight/preflight_v2_restart.sh() { printf '%s\n' "$@"; return 1; }
v2_gate_now
""")
    assert result.returncode == 1  # a real gate block still propagates
    assert "--clock-override" in result.stdout and "--i-accept-clock" in result.stdout
    assert "--operator-override" not in result.stdout and "--i-accept-bug2" not in result.stdout


def test_18_or_later_uses_plain_unmodified_gate():
    result = shell("""
REPO=/reviewed
/reviewed/ops/preflight/preflight_v2_restart.sh() { [[ $# == 0 ]]; }
v2_gate_now
""", clock="180000")
    assert result.returncode == 0


@pytest.mark.parametrize("codes,calls,sleeps,rc", (("2 0", 2, 1, 0), ("2 2 2", 3, 2, 2), ("1 0", 1, 0, 1)))
def test_retry_only_unreadables_three_times(codes, calls, sleeps, rc):
    result = shell("""
run() { local code=${codes[call]}; call=$((call + 1)); return "$code"; }
sleep() { [[ $1 == 60 ]] || return 9; pauses=$((pauses + 1)); }
call=0; pauses=0; codes=(""" + codes + """)
read_only_retry ignored; rc=$?
printf 'COUNTS=%s,%s RC=%s\n' "$call" "$pauses" "$rc"
exit "$rc"
""")
    assert result.returncode == rc
    assert f"COUNTS={calls},{sleeps} RC={rc}" in result.stdout


def test_no_global_tz_no_migration_no_rotation_gate_in_mechanics():
    source = SOURCE.read_text()
    assert "export TZ" not in source
    assert "alembic" not in source and "logrotate" not in source
    assert "200500" not in source and "200000" not in source
