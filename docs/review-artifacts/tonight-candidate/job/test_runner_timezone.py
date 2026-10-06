"""Exercise runner functions only; no systemd, broker or deployment calls."""
import os
from pathlib import Path
import re
import subprocess

import pytest

SCRIPT = (Path(__file__).parent / "run.sh").read_text()


def test_runner_never_sets_process_timezone_even_with_new_york_parent(monkeypatch):
    monkeypatch.setenv("TZ", "America/New_York")
    assert not re.search(r"^\s*(?:export\s+)?TZ=", SCRIPT, re.MULTILINE)
    assert "export TZ" not in SCRIPT
    for line in SCRIPT.splitlines():
        if "TZ=" in line:
            assert "TZ=America/New_York date " in line


@pytest.mark.parametrize("native_tz", ("America/New_York", "UTC", None))
def test_et_display_does_not_leak_into_snapshot_or_identity_child(native_tz):
    function = SCRIPT[SCRIPT.index("run() {"):SCRIPT.index("abort() {")]
    environment = dict(os.environ)
    if native_tz is None:
        environment.pop("TZ", None)
    else:
        environment["TZ"] = native_tz
    command = function + """
STAGE=offline
date() { printf '%s' "${TZ:-native}"; }
probe() { printf 'CHILD=%s kind=%s\\n' "${TZ:-native}" "$1"; }
run probe snapshot
run probe service-identity
"""
    output = subprocess.run(["bash", "-c", command], env=environment,
                            text=True, capture_output=True, check=True).stdout
    expected = native_tz or "native"
    assert f"CHILD={expected} kind=snapshot" in output
    assert f"CHILD={expected} kind=service-identity" in output
    assert output.count("ET=America/New_York") == 2
    # NY-parent case proves no override, not UTC output from a NY caller.
    assert "export TZ" not in SCRIPT


@pytest.mark.parametrize("day,clock,first_stop,allowed", (
    ("2026-10-05", "200459", False, True),
    ("2026-10-05", "200459", True, False),
    ("2026-10-05", "200500", True, True),
    ("2026-10-05", "230000", True, True),
    ("2026-10-05", "230001", True, False),
    ("2026-10-06", "200500", True, False),
))
def test_literal_et_first_stop_boundaries(day, clock, first_stop, allowed):
    function = SCRIPT[SCRIPT.index("window_now() {"):SCRIPT.index("STAGE=initial-read-only-gates")]
    command = "set -Eeuo pipefail\n" + """
date() {
  [[ ${TZ:-} == America/New_York ]]
  case "$1" in
    +%%F) printf '%s' '%s';;
    +%%H%%M%%S) printf '%s' '%s';;
    *) return 99;;
  esac
}
""" % ("%s", day, "%s", clock) + function + "window_now " + ("first-stop" if first_stop else "")
    result = subprocess.run(["bash", "-c", command], env={**os.environ, "TZ": "UTC"},
                            text=True, capture_output=True)
    assert (result.returncode == 0) is allowed, result.stderr
