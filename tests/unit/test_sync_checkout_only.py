from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import shutil
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "ops" / "systemd" / "sync_checkout_only.sh"
DECLARATIONS = ROOT / "ops" / "systemd" / "sync_only_unloaded_files.tsv"
WORKFLOW = ROOT / ".github" / "workflows" / "deploy-service.yml"


@dataclass(frozen=True)
class FakeSystemctl:
    path: Path
    call_log: Path
    counter: Path
    current_crontab: Path
    root_crontab: Path
    systemd_unit: Path


def _run(
    *args: str | Path,
    cwd: Path,
    env: dict[str, str] | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(arg) for arg in args],
        cwd=cwd,
        env=env,
        check=check,
        capture_output=True,
        text=True,
    )


def _git(cwd: Path, *args: str) -> str:
    return _run("git", *args, cwd=cwd).stdout.strip()


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _commit(repo: Path, message: str) -> str:
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


def _fixture(tmp_path: Path) -> tuple[Path, Path, Path, str]:
    origin = tmp_path / "origin.git"
    author = tmp_path / "author"
    box = tmp_path / "box"
    _run("git", "init", "--bare", origin, cwd=tmp_path)
    _run("git", "init", "-b", "main", author, cwd=tmp_path)
    _git(author, "config", "user.email", "sync-test@example.invalid")
    _git(author, "config", "user.name", "Sync Test")
    _write(author / "src" / "demo" / "runtime.py", "VALUE = 1\n")
    _write(author / "docs" / "base.md", "base\n")
    _write(author / "ops" / "health" / "fanout_identity_acceptance.py", "VALUE = 1\n")
    _write(author / "ops" / "health" / "field_acceptance.py", "VALUE = 1\n")
    _write(
        author / "ops" / "systemd" / "sync_only_unloaded_files.tsv",
        DECLARATIONS.read_text(encoding="utf-8"),
    )
    base = _commit(author, "base")
    _git(author, "remote", "add", "origin", str(origin))
    _git(author, "push", "-u", "origin", "main")
    _git(origin, "symbolic-ref", "HEAD", "refs/heads/main")
    _run("git", "clone", origin, box, cwd=tmp_path)
    _git(box, "checkout", "main")
    return origin, author, box, base


def _stable_systemctl(tmp_path: Path) -> FakeSystemctl:
    fake = tmp_path / "systemctl"
    fake_crontab = tmp_path / "crontab"
    fake_sudo = tmp_path / "sudo"
    counter = tmp_path / "systemctl-count"
    log = tmp_path / "systemctl-calls"
    current_crontab = tmp_path / "current-crontab"
    root_crontab = tmp_path / "root-crontab"
    unit = tmp_path / "systemd" / "project-mai-tai-test.service"
    counter.write_text("0\n", encoding="utf-8")
    current_crontab.write_text("# no candidate references\n", encoding="utf-8")
    root_crontab.write_text("# no candidate references\n", encoding="utf-8")
    _write(unit, "[Service]\nExecStart=/usr/bin/true\n")
    _write(
        fake,
        """#!/usr/bin/env bash
set -eu
echo "$*" >> "$SYNC_TEST_CALL_LOG"
[ "$1" = show ] || { echo "mutation attempted: $*" >&2; exit 97; }
count=$(cat "$SYNC_TEST_COUNTER")
count=$((count + 1))
printf '%s\n' "$count" > "$SYNC_TEST_COUNTER"
prop=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    --property) prop="$2"; shift 2 ;;
    *) shift ;;
  esac
done
case "$prop" in
  MainPID)
    if [ "${SYNC_TEST_MOVE_AFTER:-0}" = 1 ] && [ "$count" -gt 30 ]; then
      echo 101
    else
      echo 100
    fi
    ;;
  ExecMainStartTimestamp) echo 'Wed 2026-08-26 22:25:05 UTC' ;;
  ActiveState) echo active ;;
  SubState) echo running ;;
  NRestarts) echo 0 ;;
  *) exit 98 ;;
esac
""",
    )
    fake.chmod(0o755)
    _write(
        fake_crontab,
        """#!/usr/bin/env bash
set -eu
if [ "$*" = "-l" ]; then
  cat "$SYNC_TEST_CURRENT_CRONTAB"
elif [ "$*" = "-u root -l" ]; then
  cat "$SYNC_TEST_ROOT_CRONTAB"
else
  echo "unexpected crontab arguments: $*" >&2
  exit 96
fi
""",
    )
    fake_crontab.chmod(0o755)
    _write(
        fake_sudo,
        """#!/usr/bin/env bash
set -eu
[ "${1:-}" = "-n" ] && shift
exec "$@"
""",
    )
    fake_sudo.chmod(0o755)
    return FakeSystemctl(
        path=fake,
        call_log=log,
        counter=counter,
        current_crontab=current_crontab,
        root_crontab=root_crontab,
        systemd_unit=unit,
    )


def _env(fake: FakeSystemctl, *, move_after_snapshot: bool = False) -> dict[str, str]:
    env = os.environ.copy()
    env.update(
        {
            "SYSTEMCTL_BIN": str(fake.path),
            "CRONTAB_BIN": str(fake.current_crontab.parent / "crontab"),
            "SUDO_BIN": str(fake.current_crontab.parent / "sudo"),
            "SYSTEMD_UNIT_DIRS": str(fake.systemd_unit.parent),
            "SYNC_TEST_CALL_LOG": str(fake.call_log),
            "SYNC_TEST_COUNTER": str(fake.counter),
            "SYNC_TEST_MOVE_AFTER": "1" if move_after_snapshot else "0",
            "SYNC_TEST_CURRENT_CRONTAB": str(fake.current_crontab),
            "SYNC_TEST_ROOT_CRONTAB": str(fake.root_crontab),
        }
    )
    return env


def _sync_args(box: Path, target: str, declarations: Path = DECLARATIONS) -> tuple[object, ...]:
    return "bash", SCRIPT, box, "main", target, declarations


@pytest.mark.skipif(os.name == "nt", reason="exercises the production bash path in Linux CI")
def test_sync_only_fast_forwards_safe_delta_without_mutating_services(tmp_path: Path) -> None:
    _origin, author, box, _base = _fixture(tmp_path)
    _write(author / "docs" / "note.md", "reviewed note\n")
    _write(author / "tests" / "unit" / "test_note.py", "def test_note():\n    assert True\n")
    _write(author / "src" / "demo" / "runtime.py", "# comment only\nVALUE = 1\n")
    _write(author / ".github" / "workflows" / "deploy-service.yml", "name: test\n")
    _write(author / "ops" / "systemd" / "sync_checkout_only.sh", "# reviewed control\n")
    target = _commit(author, "safe target")
    _git(author, "push", "origin", "main")
    fake = _stable_systemctl(tmp_path)

    result = _run(*_sync_args(box, target), cwd=box, env=_env(fake))

    assert _git(box, "rev-parse", "HEAD") == target
    assert "[SYNC-ONLY-OK]" in result.stdout
    assert "python_ast_equal=1" in result.stdout
    assert "restarted_units=0 migrations=0 runtime_install=0" in result.stdout
    calls = fake.call_log.read_text(encoding="utf-8")
    assert calls.count("show ") == 60
    assert "restart" not in calls
    assert " stop " not in f" {calls} "
    assert " start " not in f" {calls} "


@pytest.mark.skipif(os.name == "nt", reason="exercises the production bash path in Linux CI")
def test_sync_only_refuses_behavioral_python_change_before_checkout_moves(tmp_path: Path) -> None:
    _origin, author, box, base = _fixture(tmp_path)
    _write(author / "src" / "demo" / "runtime.py", "VALUE = 2\n")
    target = _commit(author, "behavior change")
    _git(author, "push", "origin", "main")
    fake = _stable_systemctl(tmp_path)

    result = _run(
        *_sync_args(box, target), cwd=box, env=_env(fake), check=False
    )

    assert result.returncode == 1
    assert "behavior-bearing Python change" in result.stderr
    assert _git(box, "rev-parse", "HEAD") == base


@pytest.mark.skipif(os.name == "nt", reason="exercises the production bash path in Linux CI")
def test_sync_only_refuses_unlisted_runtime_path_before_checkout_moves(tmp_path: Path) -> None:
    _origin, author, box, base = _fixture(tmp_path)
    _write(author / "ops" / "health" / "scheduled.py", "VALUE = 1\n")
    target = _commit(author, "scheduled runtime change")
    _git(author, "push", "origin", "main")
    fake = _stable_systemctl(tmp_path)

    result = _run(
        *_sync_args(box, target), cwd=box, env=_env(fake), check=False
    )

    assert result.returncode == 1
    assert "path is not sync-only-safe" in result.stderr
    assert _git(box, "rev-parse", "HEAD") == base


@pytest.mark.skipif(os.name == "nt", reason="exercises the production bash path in Linux CI")
@pytest.mark.parametrize(
    "path",
    [
        "ops/health/fanout_identity_acceptance.py",
        "ops/health/field_acceptance.py",
    ],
)
def test_sync_only_accepts_each_declared_file_when_live_refuter_is_quiet(
    tmp_path: Path, path: str
) -> None:
    _origin, author, box, _base = _fixture(tmp_path)
    _write(author / path, "VALUE = 2\n")
    target = _commit(author, "declared unloaded report change")
    _git(author, "push", "origin", "main")
    fake = _stable_systemctl(tmp_path)

    result = _run(*_sync_args(box, target), cwd=box, env=_env(fake))

    assert _git(box, "rev-parse", "HEAD") == target
    assert "declared_unloaded=1" in result.stdout
    assert f"ALLOW declared-unloaded file={path}" in result.stderr
    assert "authority=reviewed-declaration refuter=quiet" in result.stderr


@pytest.mark.skipif(os.name == "nt", reason="exercises the production bash path in Linux CI")
@pytest.mark.parametrize("referrer", ["current-crontab", "root-crontab", "systemd"])
def test_live_reference_vetoes_a_declared_file_before_checkout_moves(
    tmp_path: Path, referrer: str
) -> None:
    _origin, author, box, base = _fixture(tmp_path)
    path = "ops/health/fanout_identity_acceptance.py"
    _write(author / path, "VALUE = 2\n")
    target = _commit(author, "referenced report change")
    _git(author, "push", "origin", "main")
    fake = _stable_systemctl(tmp_path)
    reference = "/home/trader/project-mai-tai/ops/health/fanout_identity_acceptance.py\n"
    if referrer == "current-crontab":
        fake.current_crontab.write_text(reference, encoding="utf-8")
    elif referrer == "root-crontab":
        fake.root_crontab.write_text(reference, encoding="utf-8")
    else:
        fake.systemd_unit.write_text(f"[Service]\nExecStart={reference}", encoding="utf-8")

    result = _run(
        *_sync_args(box, target), cwd=box, env=_env(fake), check=False
    )

    assert result.returncode == 1
    assert "live cron/systemd reference" in result.stderr
    assert "vetoes ops/health/fanout_identity_acceptance.py" in result.stderr
    assert _git(box, "rev-parse", "HEAD") == base


@pytest.mark.skipif(os.name == "nt", reason="exercises the production bash path in Linux CI")
def test_unpinned_declaration_copy_cannot_grant_a_new_file(tmp_path: Path) -> None:
    _origin, author, box, base = _fixture(tmp_path)
    path = "ops/health/unreviewed_report.py"
    _write(author / path, "VALUE = 1\n")
    target = _commit(author, "unreviewed report")
    _git(author, "push", "origin", "main")
    fake = _stable_systemctl(tmp_path)
    forged = tmp_path / "forged.tsv"
    forged.write_text(
        DECLARATIONS.read_text(encoding="utf-8")
        + f"{path}\tforged local permission that is absent from the target commit\n",
        encoding="utf-8",
    )

    result = _run(
        *_sync_args(box, target, forged), cwd=box, env=_env(fake), check=False
    )

    assert result.returncode == 3
    assert "provided declarations do not match the target commit" in result.stderr
    assert _git(box, "rev-parse", "HEAD") == base


@pytest.mark.skipif(os.name == "nt", reason="exercises the production bash path in Linux CI")
def test_sync_only_refuses_if_origin_moves_past_pinned_target(tmp_path: Path) -> None:
    _origin, author, box, base = _fixture(tmp_path)
    _write(author / "docs" / "first.md", "first\n")
    expected = _commit(author, "expected")
    _git(author, "push", "origin", "main")
    _write(author / "docs" / "second.md", "second\n")
    _commit(author, "moved")
    _git(author, "push", "origin", "main")
    fake = _stable_systemctl(tmp_path)

    result = _run(
        *_sync_args(box, expected), cwd=box, env=_env(fake), check=False
    )

    assert result.returncode == 3
    assert "origin/main moved" in result.stderr
    assert _git(box, "rev-parse", "HEAD") == base


@pytest.mark.skipif(os.name == "nt", reason="exercises the production bash path in Linux CI")
def test_sync_only_refuses_a_checkout_on_the_wrong_branch(tmp_path: Path) -> None:
    _origin, author, box, base = _fixture(tmp_path)
    _write(author / "docs" / "note.md", "safe delta\n")
    target = _commit(author, "safe target")
    _git(author, "push", "origin", "main")
    _git(box, "checkout", "-b", "stale-operator-branch")
    fake = _stable_systemctl(tmp_path)

    result = _run(
        *_sync_args(box, target), cwd=box, env=_env(fake), check=False
    )

    assert result.returncode == 1
    assert "production checkout is on stale-operator-branch" in result.stderr
    assert _git(box, "rev-parse", "HEAD") == base


@pytest.mark.skipif(os.name == "nt", reason="exercises the production bash path in Linux CI")
def test_sync_only_refuses_success_if_any_process_identity_moves(tmp_path: Path) -> None:
    _origin, author, box, _base = _fixture(tmp_path)
    _write(author / "docs" / "note.md", "safe delta\n")
    target = _commit(author, "safe target")
    _git(author, "push", "origin", "main")
    fake = _stable_systemctl(tmp_path)

    result = _run(
        *_sync_args(box, target),
        cwd=box,
        env=_env(fake, move_after_snapshot=True),
        check=False,
    )

    assert result.returncode == 1
    assert "managed process identity changed" in result.stderr
    assert "[SYNC-ONLY-OK]" not in result.stdout


def test_workflow_routes_sync_only_away_from_deploy_script() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    checkout_step = text.split("- name: Check out sync-only verifier", maxsplit=1)[1].split(
        "- name: Sync checkout without restart", maxsplit=1
    )[0]
    sync_step = text.split("- name: Sync checkout without restart", maxsplit=1)[1].split(
        "- name: Deploy selected service to VPS", maxsplit=1
    )[0]

    assert "- sync-only" in text
    assert "group: project-mai-tai-production-deploy" in text
    assert "ref: ${{ github.sha }}" in checkout_step
    assert "persist-credentials: false" in checkout_step
    assert "EXPECTED_SHA: ${{ github.sha }}" in sync_step
    assert "sync_checkout_only.sh" in sync_step
    assert "sync_only_unloaded_files.tsv" in sync_step
    assert "REMOTE_DECLARATIONS" in sync_step
    assert "deploy_service.sh" not in sync_step
    assert "08_install_runtime.sh" not in sync_step
    assert "systemctl" not in sync_step
    assert "alembic" not in sync_step
    assert "pip install" not in sync_step
    assert "if: ${{ inputs.service != 'sync-only' }}" in text


def test_sync_script_has_no_service_mutation_or_runtime_install_path() -> None:
    text = SCRIPT.read_text(encoding="utf-8")

    forbidden = (
        "systemctl restart",
        "systemctl stop",
        "systemctl start",
        "08_install_runtime.sh",
        "alembic",
        "pip install",
    )
    assert [needle for needle in forbidden if needle in text] == []
    assert "runtime_ast_changed=0" in text
    assert "restarted_units=0 migrations=0 runtime_install=0" in text
    assert 'REMOTE_SHA" != "$EXPECTED_SHA' in text
    assert 'AFTER_UNITS" != "$BEFORE_UNITS' in text
    assert "authority=reviewed-declaration refuter=quiet" in text
    assert "live cron/systemd reference" in text


def test_sync_only_rejects_contradictory_workflow_inputs() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    validation = text.split("- name: Validate workflow inputs", maxsplit=1)[1].split(
        "- name: Check deploy secrets", maxsplit=1
    )[0]

    assert '[ "$SERVICE" = "sync-only" ]' in validation
    assert '[ "$ALLOW_LIVE_RESTART" = "true" ]' in validation
    assert '[ "$RUN_MIGRATIONS" = "true" ]' in validation
    assert '[ "$HOLD_STRATEGY" = "true" ]' in validation


def test_dynamic_controls_are_not_silently_skipped_in_linux_ci() -> None:
    if os.name != "nt":
        assert shutil.which("bash"), "Linux CI must exercise the production bash controls"
