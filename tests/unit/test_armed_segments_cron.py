from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
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


def _write_executable(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8", newline="\n")
    path.chmod(0o755)


def _run_at(tmp_path: Path, *, hour: str, minute: str) -> bool:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    marker = tmp_path / "python-invoked"
    _write_executable(
        fake_bin / "date",
        """#!/usr/bin/env bash
case "${1:-}" in
  '+%F %H:%M:%S %Z') printf '2026-08-31 %s:%s:00 EDT\\n' "$FAKE_ET_HOUR" "$FAKE_ET_MINUTE" ;;
  '+%F') printf '2026-08-31\\n' ;;
  '+%H') printf '%s\\n' "$FAKE_ET_HOUR" ;;
  '+%M') printf '%s\\n' "$FAKE_ET_MINUTE" ;;
  '+%u') printf '1\\n' ;;
  *) exit 91 ;;
esac
""",
    )
    _write_executable(fake_bin / "mkdir", "#!/usr/bin/env bash\nexit 0\n")
    _write_executable(
        fake_bin / "python3",
        "#!/usr/bin/env bash\nprintf 'invoked\\n' > \"$FAKE_PYTHON_MARKER\"\nexit 0\n",
    )
    env = os.environ.copy()
    env.update(
        {
            "FAKE_ET_HOUR": hour,
            "FAKE_ET_MINUTE": minute,
            "FAKE_PYTHON_MARKER": _shell_path(marker),
        }
    )

    result = subprocess.run(
        [
            str(BASH),
            "-c",
            'PATH="$1:$PATH"; exec "$2"',
            "armed-segments-boundary-test",
            _shell_path(fake_bin),
            _shell_path(WRAPPER),
        ],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=15,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    return marker.exists()


@pytest.mark.parametrize(
    ("hour", "minute", "should_proceed"),
    [("05", "59", False), ("06", "00", True)],
)
def test_wrapper_opens_exactly_at_0600_et(
    tmp_path: Path, hour: str, minute: str, should_proceed: bool
) -> None:
    assert _run_at(tmp_path, hour=hour, minute=minute) is should_proceed


def test_wrapper_header_names_the_effective_schedule_and_window() -> None:
    source = WRAPPER.read_text(encoding="utf-8")
    assert "WINDOW: page from 06:00-16:30 ET" in source
    assert "*/5 10-21 * * 1-5" in source
    assert 'if [ "$ETMIN" -lt 360 ] || [ "$ETMIN" -ge 990 ]' in source
