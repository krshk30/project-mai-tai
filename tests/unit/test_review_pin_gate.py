from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest


SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "review_pin_gate.py"
CODEX_MARKER = "Co-Authored-By: OpenAI Codex <noreply@openai.com>"
CLAUDE_MARKER = "Co-Authored-By: Claude <noreply@anthropic.com>"


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _init_repo(path: Path) -> Path:
    path.mkdir()
    subprocess.run(["git", "init", "-b", "main", str(path)], check=True, capture_output=True)
    _git(path, "config", "user.name", "Review Fixture")
    _git(path, "config", "user.email", "fixture@example.com")
    return path


def _commit(repo: Path, name: str, body: str, marker: str) -> str:
    (repo / name).write_text(body, encoding="utf-8")
    _git(repo, "add", name)
    _git(repo, "commit", "-m", name, "-m", marker)
    return _git(repo, "rev-parse", "HEAD")


def _fixture(tmp_path: Path) -> tuple[Path, Path, str, str]:
    repo = _init_repo(tmp_path / "target")
    base = _commit(repo, "base.txt", "base\n", CLAUDE_MARKER)
    head = _commit(repo, "product.txt", "product\n", CODEX_MARKER)
    ledger = _init_repo(tmp_path / "ledger")
    _commit(ledger, "README.md", "review ledger\n", CLAUDE_MARKER)
    return repo, ledger, base, head


def _record(
    repo: Path,
    ledger: Path,
    base: str,
    head: str,
    *,
    reviewer: str = "claude-1",
    marker: str = CLAUDE_MARKER,
) -> Path:
    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "record",
            "--repo",
            str(repo),
            "--ledger",
            str(ledger),
            "--pr",
            "817",
            "--base",
            base,
            "--head",
            head,
            "--reviewer",
            reviewer,
            "--summary",
            "read every changed line and ran the controls",
            "--reviewed-at",
            "2026-08-27T12:00:00Z",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    record = Path(result.stdout.strip())
    _git(ledger, "add", record.relative_to(ledger).as_posix())
    _git(ledger, "commit", "-m", "Record independent review", "-m", marker)
    return record


def _verify(repo: Path, ledger: Path, base: str, head: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "verify",
            "--repo",
            str(repo),
            "--ledger",
            str(ledger),
            "--pr",
            "817",
            "--base",
            base,
            "--head",
            head,
        ],
        check=False,
        capture_output=True,
        text=True,
    )


def test_covering_independent_pin_passes(tmp_path: Path) -> None:
    repo, ledger, base, head = _fixture(tmp_path)
    _record(repo, ledger, base, head)

    result = _verify(repo, ledger, base, head)

    assert result.returncode == 0, result.stderr
    assert "complete independent review coverage" in result.stdout


def test_missing_pin_refuses(tmp_path: Path) -> None:
    repo, ledger, base, head = _fixture(tmp_path)

    result = _verify(repo, ledger, base, head)

    assert result.returncode == 1
    assert "no committed review pin" in result.stderr


def test_same_agent_pin_refuses_the_817_case(tmp_path: Path) -> None:
    repo, ledger, base, head = _fixture(tmp_path)
    relative = Path("records") / head / f"pr-817--{base}--codex-2.json"
    path = ledger / relative
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "pr_number": 817,
                "range_base": base,
                "range_head": head,
                "reviewed_at": "2026-08-27T12:00:00Z",
                "reviewer": "codex-2",
                "schema_version": 1,
                "summary": "self-review fixture",
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    _git(ledger, "add", relative.as_posix())
    _git(ledger, "commit", "-m", "Self review", "-m", CODEX_MARKER)

    result = _verify(repo, ledger, base, head)

    assert result.returncode == 1
    assert "SELF-REVIEW REFUSED" in result.stderr


def test_declared_reviewer_must_match_ledger_commit_marker(tmp_path: Path) -> None:
    repo, ledger, base, head = _fixture(tmp_path)
    _record(repo, ledger, base, head, marker=CODEX_MARKER)

    result = _verify(repo, ledger, base, head)

    assert result.returncode == 3
    assert "declares claude-1 but its ledger commit is codex-2" in result.stderr


def test_new_commit_after_pin_is_uncovered(tmp_path: Path) -> None:
    repo, ledger, base, reviewed_head = _fixture(tmp_path)
    _record(repo, ledger, base, reviewed_head)
    moved_head = _commit(repo, "later.txt", "not reviewed\n", CODEX_MARKER)

    result = _verify(repo, ledger, base, moved_head)

    assert result.returncode == 1
    assert "INCOMPLETE REVIEW COVERAGE" in result.stderr


def test_narrow_independent_pins_cover_the_union(tmp_path: Path) -> None:
    repo, ledger, base, first_head = _fixture(tmp_path)
    _record(repo, ledger, base, first_head)
    second_head = _commit(repo, "second.txt", "second change\n", CLAUDE_MARKER)
    _record(
        repo,
        ledger,
        first_head,
        second_head,
        reviewer="codex-2",
        marker=CODEX_MARKER,
    )

    result = _verify(repo, ledger, base, second_head)

    assert result.returncode == 0, result.stderr
    assert "from 2 committed pin(s)" in result.stdout


def test_record_is_immutable(tmp_path: Path) -> None:
    repo, ledger, base, head = _fixture(tmp_path)
    record = _record(repo, ledger, base, head)
    record.write_text(record.read_text(encoding="utf-8").replace("controls", "all controls"), encoding="utf-8")
    _git(ledger, "add", record.relative_to(ledger).as_posix())
    _git(ledger, "commit", "-m", "Rewrite review", "-m", CLAUDE_MARKER)

    result = _verify(repo, ledger, base, head)

    assert result.returncode == 3
    assert "must be immutable" in result.stderr


@pytest.mark.parametrize("bad_marker", ["", "Co-Authored-By: Codex <noreply@openai.com>"])
def test_unrecognised_product_authorship_is_could_not_tell(
    tmp_path: Path, bad_marker: str
) -> None:
    repo = _init_repo(tmp_path / "target")
    base = _commit(repo, "base.txt", "base\n", CLAUDE_MARKER)
    marker = bad_marker or "Fixture body without marker"
    head = _commit(repo, "product.txt", "product\n", marker)
    ledger = _init_repo(tmp_path / "ledger")
    _commit(ledger, "README.md", "review ledger\n", CLAUDE_MARKER)
    relative = Path("records") / head / f"pr-817--{base}--claude-1.json"
    path = ledger / relative
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "pr_number": 817,
                "range_base": base,
                "range_head": head,
                "reviewed_at": "2026-08-27T12:00:00Z",
                "reviewer": "claude-1",
                "schema_version": 1,
                "summary": "authorship control",
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    _git(ledger, "add", relative.as_posix())
    _git(ledger, "commit", "-m", "Record", "-m", CLAUDE_MARKER)

    result = _verify(repo, ledger, base, head)

    assert result.returncode == 3
    assert "no single recognised agent marker" in result.stderr
