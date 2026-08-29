from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
INSTALLER = ROOT / "ops" / "health" / "install_armed_segments_schedule.sh"
WRAPPER = ROOT / "ops" / "health" / "armed_segments_cron.sh"


def _bash_path() -> str | None:
    if os.name == "nt":
        git_bash = Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Git/bin/bash.exe"
        if git_bash.is_file():
            return str(git_bash)
    return shutil.which("bash")


BASH = _bash_path()

pytestmark = pytest.mark.skipif(BASH is None, reason="bash is required")


def _shell_path(path: Path) -> str:
    value = path.as_posix()
    if os.name == "nt" and len(value) >= 3 and value[1:3] == ":/":
        return f"/{value[0].lower()}{value[2:]}"
    return value


def _fake_crontab_script() -> bytes:
    return b"""#!/usr/bin/env bash
set -u
printf 'crontab %s\\n' "$*" >> "$FAKE_COMMAND_LOG"
if [[ "${1:-}" == "-l" ]]; then
  if [[ "${FAKE_FAIL_POST_WRITE_READ:-0}" == "1" \
        && -f "$FAKE_WRITE_SEEN" && ! -f "$FAKE_READ_FAILURE_CONSUMED" ]]; then
    : > "$FAKE_READ_FAILURE_CONSUMED"
    echo 'injected post-write read failure' >&2
    exit 18
  fi
  if [[ ! -f "$FAKE_CRONTAB_STATE" ]]; then
    echo 'no crontab for root' >&2
    exit 1
  fi
  cat "$FAKE_CRONTAB_STATE"
  exit 0
fi
if [[ "${1:-}" == "-r" ]]; then
  rm -f "$FAKE_CRONTAB_STATE"
  : > "$FAKE_WRITE_SEEN"
  exit 0
fi
cp "$1" "$FAKE_CRONTAB_STATE"
if [[ "${FAKE_DUPLICATE_AFTER_WRITE:-0}" == "1" \
      && ! -f "$FAKE_DUPLICATE_CONSUMED" ]]; then
  : > "$FAKE_DUPLICATE_CONSUMED"
  grep -F 'armed_segments_cron.sh' "$1" >> "$FAKE_CRONTAB_STATE"
fi
: > "$FAKE_WRITE_SEEN"
"""


def _install_fixture(tmp_path: Path, initial: bytes | None) -> tuple[dict[str, str], Path, Path]:
    test_root = tmp_path / "root"
    fake_bin = test_root / "fake-bin"
    fake_bin.mkdir(parents=True)
    crontab = fake_bin / "crontab"
    crontab.write_bytes(_fake_crontab_script())
    crontab.chmod(0o755)

    state = test_root / "root.crontab"
    if initial is not None:
        state.write_bytes(initial)
    command_log = test_root / "commands.log"
    env = os.environ.copy()
    env.update(
        {
            "MAI_TAI_ARMED_SEGMENTS_INSTALL_TEST_ROOT": _shell_path(test_root),
            "FAKE_CRONTAB_STATE": _shell_path(state),
            "FAKE_COMMAND_LOG": _shell_path(command_log),
            "FAKE_WRITE_SEEN": _shell_path(test_root / "write-seen"),
            "FAKE_READ_FAILURE_CONSUMED": _shell_path(test_root / "read-failure-consumed"),
            "FAKE_DUPLICATE_CONSUMED": _shell_path(test_root / "duplicate-consumed"),
        }
    )
    return env, state, test_root


def _run(env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(BASH), INSTALLER.as_posix()],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
        check=False,
    )


def _schedule() -> str:
    return f"*/5 10-21 * * 1-5 {_shell_path(WRAPPER)}"


def test_installer_reads_back_exactly_one_managed_schedule_and_preserves_preimage(
    tmp_path: Path,
) -> None:
    original = b"MAILTO=ops@example.invalid\n17 3 * * * /usr/local/bin/foreign job\n"
    env, state, test_root = _install_fixture(tmp_path, original)

    first = _run(env)

    assert first.returncode == 0, first.stderr
    installed = state.read_text(encoding="utf-8")
    assert installed.count(_schedule()) == 1
    assert installed.count("# BEGIN project-mai-tai armed-segments pager") == 1
    assert installed.count("# END project-mai-tai armed-segments pager") == 1
    assert "17 3 * * * /usr/local/bin/foreign job\n" in installed
    assert "managed_entries=1" in first.stdout
    backups = [
        path
        for path in (test_root / "backups").glob("root.crontab.pre-install.*")
        if not path.name.endswith(".present")
    ]
    assert len(backups) == 1
    assert backups[0].read_bytes() == original
    assert backups[0].with_suffix(backups[0].suffix + ".present").read_text().strip() == "1"

    second = _run(env)

    assert second.returncode == 0, second.stderr
    assert state.read_text(encoding="utf-8").count(_schedule()) == 1


def test_post_write_read_failure_rolls_back_byte_exact_preimage(tmp_path: Path) -> None:
    original = b"SHELL=/bin/bash\n5 2 * * * /usr/local/bin/keep  two-spaces\n"
    env, state, test_root = _install_fixture(tmp_path, original)
    env["FAKE_FAIL_POST_WRITE_READ"] = "1"

    result = _run(env)

    assert result.returncode != 0
    assert "prior crontab restored" in result.stderr
    assert state.read_bytes() == original
    backups = [
        path
        for path in (test_root / "backups").glob("root.crontab.pre-install.*")
        if not path.name.endswith(".present")
    ]
    assert len(backups) == 1
    assert backups[0].read_bytes() == original
    commands = (test_root / "commands.log").read_text(encoding="utf-8")
    writes = [line for line in commands.splitlines() if line != "crontab -l"]
    assert len(writes) == 2  # attempted install, then rollback from the preserved pre-image


def test_duplicate_schedule_on_readback_is_refused_and_rolled_back(tmp_path: Path) -> None:
    original = b"0 1 * * * /usr/local/bin/foreign\n"
    env, state, _test_root = _install_fixture(tmp_path, original)
    env["FAKE_DUPLICATE_AFTER_WRITE"] = "1"

    result = _run(env)

    assert result.returncode != 0
    assert "does not contain exactly one armed-segments schedule" in result.stderr
    assert "prior crontab restored" in result.stderr
    assert state.read_bytes() == original
