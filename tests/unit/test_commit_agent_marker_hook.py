from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
HOOK = ROOT / ".githooks" / "commit-msg"
INSTALLER = ROOT / "scripts" / "install_commit_agent_marker_hook.sh"


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=check,
        capture_output=True,
        text=True,
    )


def _bash() -> str:
    git = shutil.which("git")
    if git is not None:
        git_root = Path(git).resolve().parent.parent
        for candidate in (git_root / "bin" / "bash.exe", git_root / "usr" / "bin" / "bash.exe"):
            if candidate.is_file():
                return str(candidate)
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("bash is required to exercise the tracked commit-msg hook")
    return bash


def _repo(tmp_path: Path, agent: str | None = "codex") -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.name", "Operator")
    _git(repo, "config", "user.email", "operator@example.invalid")
    hooks = repo / ".githooks"
    hooks.mkdir()
    shutil.copy2(HOOK, hooks / "commit-msg")
    os.chmod(hooks / "commit-msg", 0o755)
    _git(repo, "config", "extensions.worktreeConfig", "true")
    _git(repo, "config", "--worktree", "core.hooksPath", ".githooks")
    if agent is not None:
        _git(repo, "config", "--worktree", "mai-tai.agentMarker", agent)
    return repo


def _commit(repo: Path, subject: str, body: str | None = None) -> subprocess.CompletedProcess[str]:
    command = ["commit", "--allow-empty", "-m", subject]
    if body is not None:
        command.extend(["-m", body])
    return _git(repo, *command, check=False)


def _message(repo: Path) -> str:
    return _git(repo, "show", "-s", "--format=%B", "HEAD").stdout


def test_unmarked_commit_gets_exact_codex_marker(tmp_path: Path) -> None:
    repo = _repo(tmp_path)

    result = _commit(repo, "feature")

    assert result.returncode == 0, result.stderr
    message = _message(repo)
    assert message.count("Co-Authored-By: OpenAI Codex <noreply@openai.com>") == 1
    assert "Co-Authored-By: Codex <noreply@openai.com>" not in message


def test_manual_codex_label_is_normalized_by_adding_recognised_marker(tmp_path: Path) -> None:
    repo = _repo(tmp_path)

    result = _commit(
        repo,
        "feature",
        "Co-Authored-By: Codex <noreply@openai.com>",
    )

    assert result.returncode == 0, result.stderr
    message = _message(repo)
    assert "Co-Authored-By: Codex <noreply@openai.com>" in message
    assert message.count("Co-Authored-By: OpenAI Codex <noreply@openai.com>") == 1


def test_existing_exact_marker_is_not_duplicated(tmp_path: Path) -> None:
    repo = _repo(tmp_path)

    result = _commit(
        repo,
        "feature",
        "Co-Authored-By: OpenAI Codex <noreply@openai.com>",
    )

    assert result.returncode == 0, result.stderr
    assert _message(repo).count("Co-Authored-By: OpenAI Codex <noreply@openai.com>") == 1


def test_marker_for_different_agent_refuses_commit(tmp_path: Path) -> None:
    repo = _repo(tmp_path)

    result = _commit(
        repo,
        "feature",
        "Co-Authored-By: Claude <noreply@anthropic.com>",
    )

    assert result.returncode == 1
    assert "different agent" in result.stderr
    assert _git(repo, "rev-parse", "--verify", "HEAD", check=False).returncode != 0


def test_missing_worktree_identity_refuses_commit(tmp_path: Path) -> None:
    repo = _repo(tmp_path, agent=None)

    result = _commit(repo, "feature")

    assert result.returncode == 1
    assert "set this worktree's agent marker" in result.stderr


def test_installer_sets_hook_and_identity_per_worktree(tmp_path: Path) -> None:
    repo = _repo(tmp_path, agent=None)
    shutil.copy2(INSTALLER, repo / "install.sh")
    os.chmod(repo / "install.sh", 0o755)

    result = subprocess.run(
        [_bash(), str(repo / "install.sh"), "claude"],
        cwd=repo,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert _git(repo, "config", "--worktree", "--get", "core.hooksPath").stdout.strip() == ".githooks"
    assert _git(repo, "config", "--worktree", "--get", "mai-tai.agentMarker").stdout.strip() == "claude"
    commit = _commit(repo, "feature")
    assert commit.returncode == 0, commit.stderr
    assert "Co-Authored-By: Claude <noreply@anthropic.com>" in _message(repo)
