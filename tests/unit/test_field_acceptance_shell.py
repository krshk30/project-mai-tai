from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess

import pytest


REPO = Path(__file__).resolve().parents[2]
EVIDENCE = REPO / "ops" / "health" / "evidence.sh"
SINCE = "2026-08-24T21:28:00Z"
UNTIL = "2026-08-25T20:00:00Z"


def _bash() -> str:
    if os.name == "nt":
        git_bash = Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Git" / "bin" / "bash.exe"
        assert git_bash.is_file(), f"Git Bash is required for this shell integration test: {git_bash}"
        return str(git_bash)
    found = shutil.which("bash")
    assert found is not None, "bash is required for this shell integration test"
    return found


def _bash_path(path: Path) -> str:
    resolved = path.resolve()
    if os.name != "nt":
        return str(resolved)
    drive = resolved.drive.rstrip(":").lower()
    tail = resolved.as_posix()[2:]
    return f"/{drive}{tail}"


def _fake_repo(tmp_path: Path) -> tuple[Path, Path]:
    fake_python = tmp_path / ".venv" / "bin" / "python"
    fake_python.parent.mkdir(parents=True)
    fake_python.write_text(
        """#!/usr/bin/env bash
printf '%s\n' "$@" > "$FAKE_ARGS_FILE"
printf 'FAKE_FIELD_VERDICT rc=%s\n' "$FAKE_RC"
exit "$FAKE_RC"
""",
        encoding="utf-8",
        newline="\n",
    )
    fake_python.chmod(0o755)
    fake_tool = tmp_path / "ops" / "health" / "field_acceptance.py"
    fake_tool.parent.mkdir(parents=True)
    fake_tool.write_text("# readable production-tool placeholder\n", encoding="utf-8")
    return fake_python, fake_tool


def _run_shell(tmp_path: Path, exit_code: int, check: str = "broker-order-event-source"):
    _fake_repo(tmp_path)
    args_file = tmp_path / "received-args.txt"
    env = os.environ.copy()
    env.update(
        {
            "MAI_TAI_REPO_DIR": _bash_path(tmp_path),
            "MAI_TAI_EVIDENCE_OUT": _bash_path(tmp_path / "evidence-out"),
            "FAKE_ARGS_FILE": _bash_path(args_file),
            "FAKE_RC": str(exit_code),
        }
    )
    result = subprocess.run(
        [
            _bash(),
            _bash_path(EVIDENCE),
            "field-acceptance",
            "--check",
            check,
            "--since",
            SINCE,
            "--until",
            UNTIL,
        ],
        capture_output=True,
        text=True,
        env=env,
        timeout=20,
        check=False,
    )
    return result, args_file


@pytest.mark.parametrize("exit_code", [0, 1, 2, 3])
def test_evidence_shell_preserves_field_acceptance_args_and_exit_codes(
    tmp_path: Path, exit_code: int,
) -> None:
    result, args_file = _run_shell(tmp_path, exit_code)

    assert result.returncode == exit_code
    assert f"FAKE_FIELD_VERDICT rc={exit_code}" in result.stdout
    assert f"EXIT_STATUS={exit_code}" in result.stdout
    received = args_file.read_text(encoding="utf-8").splitlines()
    assert received[0].replace("\\", "/").endswith("/ops/health/field_acceptance.py")
    assert received[1:] == [
        "--check",
        "broker-order-event-source",
        "--since",
        SINCE,
        "--until",
        UNTIL,
    ]


def test_evidence_shell_refuses_unknown_check_before_invoking_python(tmp_path: Path) -> None:
    result, args_file = _run_shell(tmp_path, 0, check="select-anything")

    assert result.returncode == 2
    assert "VOID" in result.stdout
    assert "unknown field acceptance check 'select-anything'" in result.stderr
    assert "EXIT_STATUS=2" in result.stdout
    assert not args_file.exists(), "the Python tool ran despite the shell allowlist refusal"
