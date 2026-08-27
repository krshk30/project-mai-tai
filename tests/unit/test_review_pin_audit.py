from __future__ import annotations

import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest


SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
sys.path.insert(0, str(SCRIPTS))

from audit_review_pins import MergedPull, audit, load_policy, render  # noqa: E402


GATE = SCRIPTS / "review_pin_gate.py"
CODEX_MARKER = "Co-Authored-By: OpenAI Codex <noreply@openai.com>"
CLAUDE_MARKER = "Co-Authored-By: Claude <noreply@anthropic.com>"
MERGED_AT = datetime(2026, 8, 27, 18, 0, tzinfo=UTC)


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
    _git(path, "config", "user.name", "Audit Fixture")
    _git(path, "config", "user.email", "fixture@example.com")
    return path


def _commit(repo: Path, name: str, marker: str) -> str:
    (repo / name).write_text(name + "\n", encoding="utf-8")
    _git(repo, "add", name)
    _git(repo, "commit", "-m", name, "-m", marker)
    return _git(repo, "rev-parse", "HEAD")


def _fixture(tmp_path: Path) -> tuple[Path, Path, str, str, MergedPull]:
    repo = _init_repo(tmp_path / "target")
    base = _commit(repo, "base.txt", CLAUDE_MARKER)
    head = _commit(repo, "product.txt", CODEX_MARKER)
    ledger = _init_repo(tmp_path / "ledger")
    _commit(ledger, "README.md", CLAUDE_MARKER)
    pull = MergedPull(820, MERGED_AT, base, head, "fixture", "https://example.test/820")
    return repo, ledger, base, head, pull


def _record(
    repo: Path,
    ledger: Path,
    base: str,
    head: str,
    *,
    reviewer: str = "claude-1",
    marker: str = CLAUDE_MARKER,
) -> None:
    result = subprocess.run(
        [
            sys.executable,
            str(GATE),
            "record",
            "--repo",
            str(repo),
            "--ledger",
            str(ledger),
            "--pr",
            "820",
            "--base",
            base,
            "--head",
            head,
            "--reviewer",
            reviewer,
            "--summary",
            "independent audit fixture",
            "--reviewed-at",
            "2026-08-27T17:55:00Z",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    path = Path(result.stdout.strip())
    _git(ledger, "add", path.relative_to(ledger).as_posix())
    _git(ledger, "commit", "-m", "Record review", "-m", marker)


def _no_fetch(_repo: Path, _pull: MergedPull) -> None:
    return None


def test_merged_pr_with_covering_pin_passes(tmp_path: Path) -> None:
    repo, ledger, base, head, pull = _fixture(tmp_path)
    _record(repo, ledger, base, head)

    result = audit(repo, ledger, [pull], head_fetcher=_no_fetch)

    assert result.exit_code == 0
    assert result.covered == 1
    assert result.rows[0].verdict == "COVERED"


def test_admin_merge_without_pin_is_listed_and_fails(tmp_path: Path) -> None:
    repo, ledger, _base, _head, pull = _fixture(tmp_path)

    result = audit(repo, ledger, [pull], head_fetcher=_no_fetch)
    output = render(result, datetime(2026, 8, 27, 17, 33, 34, tzinfo=UTC))

    assert result.exit_code == 1
    assert result.missing == 1
    assert result.rows[0].verdict == "MISSING_PIN"
    assert "#820" in output
    assert "evaluated=1 covered=0 missing=1 could_not_tell=0" in output


def test_self_review_pin_is_still_missing_coverage(tmp_path: Path) -> None:
    repo, ledger, base, head, pull = _fixture(tmp_path)
    relative = Path("records") / head / f"pr-820--{base}--codex-2.json"
    path = ledger / relative
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "pr_number": 820,
                "range_base": base,
                "range_head": head,
                "reviewed_at": "2026-08-27T17:55:00Z",
                "reviewer": "codex-2",
                "schema_version": 1,
                "summary": "self review",
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    _git(ledger, "add", relative.as_posix())
    _git(ledger, "commit", "-m", "Self review", "-m", CODEX_MARKER)

    result = audit(repo, ledger, [pull], head_fetcher=_no_fetch)

    assert result.exit_code == 1
    assert result.rows[0].verdict == "MISSING_PIN"
    assert "SELF-REVIEW REFUSED" in result.rows[0].detail


def test_malformed_pin_is_could_not_tell(tmp_path: Path) -> None:
    repo, ledger, base, head, pull = _fixture(tmp_path)
    relative = Path("records") / head / f"pr-820--{base}--claude-1.json"
    path = ledger / relative
    path.parent.mkdir(parents=True)
    path.write_text("not json\n", encoding="utf-8")
    _git(ledger, "add", relative.as_posix())
    _git(ledger, "commit", "-m", "Broken record", "-m", CLAUDE_MARKER)

    result = audit(repo, ledger, [pull], head_fetcher=_no_fetch)

    assert result.exit_code == 3
    assert result.could_not_tell == 1
    assert result.rows[0].verdict == "COULD_NOT_TELL"


def test_zero_merged_prs_is_named_unexercised(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "target")
    ledger = _init_repo(tmp_path / "ledger")
    result = audit(repo, ledger, [], head_fetcher=_no_fetch)

    output = render(result, datetime(2026, 8, 27, 17, 33, 34, tzinfo=UTC))

    assert result.exit_code == 0
    assert "state=UNEXERCISED" in output
    assert "evaluated=0" in output


def test_policy_requires_exact_schema_and_timezone(tmp_path: Path) -> None:
    policy = tmp_path / "policy.json"
    policy.write_text(
        '{"audit_merged_after":"2026-08-27T17:33:34Z","schema_version":1}\n',
        encoding="utf-8",
    )
    assert load_policy(policy) == datetime(2026, 8, 27, 17, 33, 34, tzinfo=UTC)

    policy.write_text(
        '{"audit_merged_after":"2026-08-27T17:33:34","schema_version":1}\n',
        encoding="utf-8",
    )
    with pytest.raises(Exception, match="has no timezone"):
        load_policy(policy)
