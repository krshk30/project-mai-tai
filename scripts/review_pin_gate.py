#!/usr/bin/env python3
"""Create and verify repository-contained independent-review pins.

Review records live on the repository's dedicated ``review-pins`` branch.  The
reviewed pull-request head therefore stays immutable: committing the pin does
not move the object that was reviewed.

Exit codes are deliberately stable:
  0  complete independent coverage
  1  refused (missing coverage or self-review)
  2  invalid invocation
  3  could not tell (malformed or unverifiable evidence)
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import NoReturn


EXIT_REFUSED = 1
EXIT_USAGE = 2
EXIT_COULD_NOT_TELL = 3
SCHEMA_VERSION = 1
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
REVIEWERS = {"claude-1", "codex-2"}


class GateError(RuntimeError):
    def __init__(self, message: str, exit_code: int) -> None:
        super().__init__(message)
        self.exit_code = exit_code


@dataclass(frozen=True)
class ReviewRecord:
    path: Path
    pr_number: int
    range_base: str
    range_head: str
    reviewer: str
    reviewed_at: str
    summary: str


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=False,
        capture_output=True,
        text=True,
    )
    if check and proc.returncode != 0:
        detail = proc.stderr.strip() or proc.stdout.strip() or "git command failed"
        raise GateError(detail, EXIT_COULD_NOT_TELL)
    return proc


def _require_sha(value: str, label: str) -> str:
    value = value.lower()
    if not SHA_RE.fullmatch(value):
        raise GateError(f"COULD_NOT_TELL: {label} is not a full SHA: {value!r}", EXIT_COULD_NOT_TELL)
    return value


def _require_commit(repo: Path, sha: str, label: str) -> None:
    proc = _git(repo, "cat-file", "-e", f"{sha}^{{commit}}", check=False)
    if proc.returncode != 0:
        raise GateError(f"COULD_NOT_TELL: {label} {sha} is not present as a commit", EXIT_COULD_NOT_TELL)


def _is_ancestor(repo: Path, older: str, newer: str) -> bool:
    return _git(repo, "merge-base", "--is-ancestor", older, newer, check=False).returncode == 0


def _rev_list(repo: Path, range_spec: str) -> list[str]:
    out = _git(repo, "rev-list", "--reverse", range_spec).stdout
    return [line.strip() for line in out.splitlines() if line.strip()]


def commit_agent(repo: Path, sha: str) -> str:
    """Return the one recognised agent trailer on *sha* or refuse uncertainty."""

    message = _git(repo, "show", "-s", "--format=%B", sha).stdout
    claude = 0
    codex = 0
    for line in message.splitlines():
        lowered = line.strip().lower()
        if not lowered.startswith("co-authored-by:"):
            continue
        if "claude" in lowered and "<noreply@anthropic.com>" in lowered:
            claude += 1
        if "openai codex" in lowered and "<noreply@openai.com>" in lowered:
            codex += 1
    if claude == 1 and codex == 0:
        return "claude-1"
    if codex == 1 and claude == 0:
        return "codex-2"
    raise GateError(
        f"COULD_NOT_TELL: commit {sha[:9]} has no single recognised agent marker",
        EXIT_COULD_NOT_TELL,
    )


def _record_relative_path(pr_number: int, base: str, head: str, reviewer: str) -> Path:
    return Path("records") / head / f"pr-{pr_number}--{base}--{reviewer}.json"


def _load_record(path: Path, ledger: Path, pr_number: int) -> ReviewRecord:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise GateError(f"COULD_NOT_TELL: cannot parse {path}: {exc}", EXIT_COULD_NOT_TELL) from exc

    required = {
        "schema_version",
        "pr_number",
        "range_base",
        "range_head",
        "reviewer",
        "reviewed_at",
        "summary",
    }
    if set(raw) != required:
        raise GateError(
            f"COULD_NOT_TELL: {path} fields differ from schema (expected {sorted(required)})",
            EXIT_COULD_NOT_TELL,
        )
    if raw["schema_version"] != SCHEMA_VERSION or raw["pr_number"] != pr_number:
        raise GateError(f"COULD_NOT_TELL: {path} schema or PR binding is wrong", EXIT_COULD_NOT_TELL)
    reviewer = str(raw["reviewer"])
    if reviewer not in REVIEWERS:
        raise GateError(f"COULD_NOT_TELL: {path} has unknown reviewer {reviewer!r}", EXIT_COULD_NOT_TELL)
    base = _require_sha(str(raw["range_base"]), "record range_base")
    head = _require_sha(str(raw["range_head"]), "record range_head")
    summary = str(raw["summary"]).strip()
    reviewed_at = str(raw["reviewed_at"]).strip()
    if not summary or not reviewed_at:
        raise GateError(f"COULD_NOT_TELL: {path} has an empty summary or timestamp", EXIT_COULD_NOT_TELL)

    relative = path.relative_to(ledger)
    expected = _record_relative_path(pr_number, base, head, reviewer)
    if relative != expected:
        raise GateError(
            f"COULD_NOT_TELL: record path {relative} is not bound to its exact range ({expected})",
            EXIT_COULD_NOT_TELL,
        )
    return ReviewRecord(path, pr_number, base, head, reviewer, reviewed_at, summary)


def _verify_record_commit(record: ReviewRecord, ledger: Path) -> None:
    relative = record.path.relative_to(ledger).as_posix()
    touches = [
        line.strip()
        for line in _git(ledger, "log", "--format=%H", "--", relative).stdout.splitlines()
        if line.strip()
    ]
    if len(touches) != 1:
        raise GateError(
            f"COULD_NOT_TELL: {relative} must be immutable; found {len(touches)} commits touching it",
            EXIT_COULD_NOT_TELL,
        )
    ledger_agent = commit_agent(ledger, touches[0])
    if ledger_agent != record.reviewer:
        raise GateError(
            f"COULD_NOT_TELL: {relative} declares {record.reviewer} but its ledger commit is {ledger_agent}",
            EXIT_COULD_NOT_TELL,
        )
    changed = [
        line.strip()
        for line in _git(
            ledger, "diff-tree", "--no-commit-id", "--name-only", "-r", touches[0]
        ).stdout.splitlines()
        if line.strip()
    ]
    if not changed or any(not name.startswith("records/") for name in changed):
        raise GateError(
            f"COULD_NOT_TELL: pin commit {touches[0][:9]} contains non-record changes",
            EXIT_COULD_NOT_TELL,
        )


def _validate_independent_range(repo: Path, record: ReviewRecord) -> list[str]:
    if not _is_ancestor(repo, record.range_base, record.range_head):
        raise GateError(
            f"COULD_NOT_TELL: {record.range_base[:9]} is not an ancestor of {record.range_head[:9]}",
            EXIT_COULD_NOT_TELL,
        )
    commits = _rev_list(repo, f"{record.range_base}..{record.range_head}")
    if not commits:
        raise GateError("COULD_NOT_TELL: review range contains no commits", EXIT_COULD_NOT_TELL)
    for commit in commits:
        owner = commit_agent(repo, commit)
        if owner == record.reviewer:
            raise GateError(
                f"SELF-REVIEW REFUSED: {record.reviewer} authored commit {commit[:9]}",
                EXIT_REFUSED,
            )
    return commits


def verify(repo: Path, ledger: Path, pr_number: int, base: str, head: str) -> list[ReviewRecord]:
    repo = repo.resolve()
    ledger = ledger.resolve()
    base = _require_sha(base, "PR base")
    head = _require_sha(head, "PR head")
    _require_commit(repo, base, "PR base")
    _require_commit(repo, head, "PR head")
    if not _is_ancestor(repo, base, head):
        raise GateError(
            f"COULD_NOT_TELL: current PR base {base[:9]} is not an ancestor of head {head[:9]}",
            EXIT_COULD_NOT_TELL,
        )
    commits = _rev_list(repo, f"{base}..{head}")
    if not commits:
        raise GateError("COULD_NOT_TELL: PR range contains no commits", EXIT_COULD_NOT_TELL)

    pattern = f"records/*/pr-{pr_number}--*.json"
    candidate_paths = sorted(ledger.glob(pattern))
    if not candidate_paths:
        raise GateError(
            f"REFUSED: PR #{pr_number} head {head[:9]} has no committed review pin",
            EXIT_REFUSED,
        )

    valid: list[ReviewRecord] = []
    for path in candidate_paths:
        record = _load_record(path, ledger, pr_number)
        _require_commit(repo, record.range_base, "record base")
        _require_commit(repo, record.range_head, "record head")
        if not _is_ancestor(repo, base, record.range_base):
            print(
                f"WARNING: ignoring {path.name}; range starts outside current PR base",
                file=sys.stderr,
            )
            continue
        if not _is_ancestor(repo, record.range_head, head):
            print(
                f"WARNING: ignoring {path.name}; reviewed head is not on current PR history",
                file=sys.stderr,
            )
            continue
        _verify_record_commit(record, ledger)
        _validate_independent_range(repo, record)
        valid.append(record)

    if not valid:
        raise GateError(
            f"REFUSED: PR #{pr_number} has no authorising review pins for its current range",
            EXIT_REFUSED,
        )

    missing: list[str] = []
    for commit in commits:
        covered = any(
            _is_ancestor(repo, commit, record.range_head)
            and not _is_ancestor(repo, commit, record.range_base)
            for record in valid
        )
        if not covered:
            missing.append(commit[:9])
    if missing:
        raise GateError(
            "INCOMPLETE REVIEW COVERAGE: unreviewed commit(s): " + " ".join(missing),
            EXIT_REFUSED,
        )
    return valid


def write_record(
    repo: Path,
    ledger: Path,
    pr_number: int,
    base: str,
    head: str,
    reviewer: str,
    summary: str,
    reviewed_at: str | None,
) -> Path:
    if reviewer not in REVIEWERS:
        raise GateError(f"unknown reviewer {reviewer!r}", EXIT_USAGE)
    base = _require_sha(base, "range base")
    head = _require_sha(head, "range head")
    _require_commit(repo, base, "range base")
    _require_commit(repo, head, "range head")
    summary = summary.strip()
    if not summary:
        raise GateError("review summary must say what was checked", EXIT_USAGE)
    record = ReviewRecord(Path(), pr_number, base, head, reviewer, reviewed_at or "", summary)
    _validate_independent_range(repo, record)
    timestamp = reviewed_at or datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    relative = _record_relative_path(pr_number, base, head, reviewer)
    destination = ledger / relative
    if destination.exists():
        raise GateError(f"REFUSED: immutable review record already exists: {relative}", EXIT_REFUSED)
    destination.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": SCHEMA_VERSION,
        "pr_number": pr_number,
        "range_base": base,
        "range_head": head,
        "reviewer": reviewer,
        "reviewed_at": timestamp,
        "summary": summary,
    }
    destination.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return destination


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    verify_parser = subparsers.add_parser("verify", help="verify complete independent coverage")
    verify_parser.add_argument("--repo", type=Path, required=True)
    verify_parser.add_argument("--ledger", type=Path, required=True)
    verify_parser.add_argument("--pr", type=int, required=True)
    verify_parser.add_argument("--base", required=True)
    verify_parser.add_argument("--head", required=True)

    record_parser = subparsers.add_parser("record", help="write an immutable ledger record")
    record_parser.add_argument("--repo", type=Path, required=True)
    record_parser.add_argument("--ledger", type=Path, required=True)
    record_parser.add_argument("--pr", type=int, required=True)
    record_parser.add_argument("--base", required=True)
    record_parser.add_argument("--head", required=True)
    record_parser.add_argument("--reviewer", choices=sorted(REVIEWERS), required=True)
    record_parser.add_argument("--summary", required=True)
    record_parser.add_argument("--reviewed-at")
    return parser


def _fail(error: GateError) -> NoReturn:
    print(f"ERROR: {error}", file=sys.stderr)
    raise SystemExit(error.exit_code)


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "verify":
            records = verify(args.repo, args.ledger, args.pr, args.base, args.head)
            print(
                f"PASS: PR #{args.pr} head {args.head[:9]} has complete independent review "
                f"coverage from {len(records)} committed pin(s)"
            )
            return 0
        destination = write_record(
            args.repo,
            args.ledger,
            args.pr,
            args.base,
            args.head,
            args.reviewer,
            args.summary,
            args.reviewed_at,
        )
        print(destination)
        return 0
    except GateError as error:
        _fail(error)


if __name__ == "__main__":
    raise SystemExit(main())
