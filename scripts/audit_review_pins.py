#!/usr/bin/env python3
"""Audit merged pull requests for repository-contained independent-review pins.

This is the detection layer for administrator merges and any other path that
bypasses the required ``independent-review-pin`` check.  It deliberately uses
the same verifier as the preventive gate.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Callable, NoReturn

from review_pin_gate import EXIT_COULD_NOT_TELL, EXIT_REFUSED, GateError, verify


@dataclass(frozen=True)
class MergedPull:
    number: int
    merged_at: datetime
    base_sha: str
    head_sha: str
    title: str
    url: str


@dataclass(frozen=True)
class AuditRow:
    pull: MergedPull
    verdict: str
    detail: str


@dataclass(frozen=True)
class AuditResult:
    rows: tuple[AuditRow, ...]
    evaluated: int
    covered: int
    missing: int
    could_not_tell: int

    @property
    def exit_code(self) -> int:
        if self.could_not_tell:
            return EXIT_COULD_NOT_TELL
        if self.missing:
            return EXIT_REFUSED
        return 0


def _parse_time(value: str, label: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise GateError(f"COULD_NOT_TELL: invalid {label} timestamp {value!r}", 3) from exc
    if parsed.tzinfo is None:
        raise GateError(f"COULD_NOT_TELL: {label} timestamp has no timezone", 3)
    return parsed.astimezone(UTC)


def load_policy(path: Path) -> datetime:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise GateError(f"COULD_NOT_TELL: cannot read audit policy {path}: {exc}", 3) from exc
    expected = {"schema_version", "audit_merged_after"}
    if set(raw) != expected or raw["schema_version"] != 1:
        raise GateError(f"COULD_NOT_TELL: audit policy {path} does not match schema 1", 3)
    return _parse_time(str(raw["audit_merged_after"]), "policy")


class GitHubClient:
    def __init__(self, repository: str, token: str, api_url: str = "https://api.github.com") -> None:
        if "/" not in repository:
            raise GateError("repository must be OWNER/NAME", 2)
        if not token:
            raise GateError("GITHUB_TOKEN is required", 2)
        self.repository = repository
        self.token = token
        self.api_url = api_url.rstrip("/")

    def _get(self, url: str) -> dict[str, object]:
        request = urllib.request.Request(
            url,
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {self.token}",
                "User-Agent": "project-mai-tai-review-pin-audit",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                payload = json.load(response)
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise GateError(f"COULD_NOT_TELL: GitHub API read failed for {url}: {exc}", 3) from exc
        if not isinstance(payload, dict):
            raise GateError(f"COULD_NOT_TELL: GitHub API returned non-object data for {url}", 3)
        return payload

    def merged_pulls(self, merged_after: datetime) -> list[MergedPull]:
        # Search uses a date floor to bound the population; the exact timestamp
        # comparison below is the authority.
        query = f"repo:{self.repository} is:pr is:merged merged:>={merged_after.date().isoformat()}"
        page = 1
        pulls: list[MergedPull] = []
        seen: set[int] = set()
        while True:
            params = urllib.parse.urlencode({"q": query, "per_page": 100, "page": page})
            result = self._get(f"{self.api_url}/search/issues?{params}")
            items = result.get("items")
            if not isinstance(items, list):
                raise GateError("COULD_NOT_TELL: GitHub search response has no items list", 3)
            for item in items:
                if not isinstance(item, dict) or not isinstance(item.get("pull_request"), dict):
                    raise GateError("COULD_NOT_TELL: malformed pull-request search item", 3)
                number = int(item["number"])
                if number in seen:
                    continue
                seen.add(number)
                detail = self._get(str(item["pull_request"]["url"]))
                merged_at_raw = detail.get("merged_at")
                if not isinstance(merged_at_raw, str):
                    continue
                merged_at = _parse_time(merged_at_raw, f"PR #{number} merged_at")
                if merged_at <= merged_after:
                    continue
                base = detail.get("base")
                head = detail.get("head")
                if not isinstance(base, dict) or not isinstance(head, dict):
                    raise GateError(f"COULD_NOT_TELL: PR #{number} has malformed base/head", 3)
                pulls.append(
                    MergedPull(
                        number=number,
                        merged_at=merged_at,
                        base_sha=str(base.get("sha", "")),
                        head_sha=str(head.get("sha", "")),
                        title=str(detail.get("title", "")),
                        url=str(detail.get("html_url", "")),
                    )
                )
            total = int(result.get("total_count", 0))
            if page * 100 >= total or not items:
                break
            page += 1
            if page > 10:
                raise GateError("COULD_NOT_TELL: audit search exceeded GitHub's 1,000-result cap", 3)
        return sorted(pulls, key=lambda pull: (pull.merged_at, pull.number))


def fetch_pull_head(repo: Path, pull: MergedPull) -> None:
    ref = f"refs/review-pin/audit/pr-{pull.number}"
    proc = subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "fetch",
            "--no-tags",
            "--force",
            "origin",
            f"pull/{pull.number}/head:{ref}",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        detail = proc.stderr.strip() or proc.stdout.strip() or "fetch failed"
        raise GateError(f"COULD_NOT_TELL: cannot fetch PR #{pull.number} head: {detail}", 3)
    resolved = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", ref],
        check=False,
        capture_output=True,
        text=True,
    )
    if resolved.returncode != 0:
        raise GateError(f"COULD_NOT_TELL: fetched ref for PR #{pull.number} does not resolve", 3)
    actual = resolved.stdout.strip()
    if actual != pull.head_sha:
        raise GateError(
            f"COULD_NOT_TELL: PR #{pull.number} API head {pull.head_sha} != fetched {actual}",
            3,
        )


def audit(
    repo: Path,
    ledger: Path,
    pulls: list[MergedPull],
    *,
    head_fetcher: Callable[[Path, MergedPull], None] = fetch_pull_head,
) -> AuditResult:
    rows: list[AuditRow] = []
    covered = 0
    missing = 0
    could_not_tell = 0
    for pull in pulls:
        try:
            head_fetcher(repo, pull)
            records = verify(
                repo,
                ledger,
                pull.number,
                pull.base_sha,
                pull.head_sha,
            )
        except GateError as error:
            if error.exit_code == EXIT_REFUSED:
                missing += 1
                rows.append(AuditRow(pull, "MISSING_PIN", str(error)))
            else:
                could_not_tell += 1
                rows.append(AuditRow(pull, "COULD_NOT_TELL", str(error)))
            continue
        covered += 1
        rows.append(AuditRow(pull, "COVERED", f"{len(records)} committed pin(s)"))
    return AuditResult(tuple(rows), len(pulls), covered, missing, could_not_tell)


def render(result: AuditResult, policy_time: datetime) -> str:
    lines = ["PR | merged_at | head | verdict | detail", "---|---|---|---|---"]
    for row in result.rows:
        detail = row.detail.replace("|", "/").replace("\n", " ")
        lines.append(
            f"#{row.pull.number} | {row.pull.merged_at.isoformat()} | "
            f"{row.pull.head_sha[:9]} | {row.verdict} | {detail}"
        )
    state = "UNEXERCISED" if result.evaluated == 0 else "MEASURED"
    lines.append("")
    lines.append(
        f"SUMMARY state={state} audit_merged_after={policy_time.isoformat()} "
        f"evaluated={result.evaluated} covered={result.covered} "
        f"missing={result.missing} could_not_tell={result.could_not_tell}"
    )
    return "\n".join(lines)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, required=True, help="target repository checkout")
    parser.add_argument("--ledger", type=Path, required=True, help="review-pins checkout")
    parser.add_argument("--repository", required=True, help="GitHub OWNER/NAME")
    parser.add_argument("--policy", type=Path, required=True)
    parser.add_argument("--api-url", default="https://api.github.com")
    return parser


def _fail(error: GateError) -> NoReturn:
    print(f"ERROR: {error}", file=sys.stderr)
    raise SystemExit(error.exit_code)


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        policy_time = load_policy(args.policy)
        client = GitHubClient(
            args.repository,
            os.environ.get("GITHUB_TOKEN", ""),
            args.api_url,
        )
        pulls = client.merged_pulls(policy_time)
        result = audit(args.repo.resolve(), args.ledger.resolve(), pulls)
        print(render(result, policy_time))
        return result.exit_code
    except GateError as error:
        _fail(error)


if __name__ == "__main__":
    raise SystemExit(main())
