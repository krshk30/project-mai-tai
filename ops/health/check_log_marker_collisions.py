#!/usr/bin/env python3
"""Refuse log formats in which one bracketed marker can match another event.

Evidence readers use the complete bracketed token (for example,
``[V2-FANOUT-REACTIVE-SUPPRESSED]``) as a fixed string.  A different log event
must therefore never repeat that complete token in its message.  That happened
when the LATCHED denominator line named the SUPPRESSED marker in prose: a count
of suppressions also counted every successful latch.

This check parses actual Python logging calls instead of grepping repository
text.  Consequently marker regexes in evidence scripts, guard fixtures,
comments, and this file itself are not mistaken for emitted events.  The
marker grammar matches ``ops/health/evidence.sh``.

Exit codes:
  0  every statically readable logging format carries at most one marker
  1  a logging format carries two or more distinct markers
  3  the emitted-marker population could not be determined
"""

from __future__ import annotations

import argparse
import ast
from dataclasses import dataclass
from pathlib import Path
import re
import sys
from typing import Iterable, Sequence


MARKER_RE = re.compile(r"\[[A-Z][A-Z0-9-]{3,}\]")
LOG_METHODS = frozenset(
    {"debug", "info", "warning", "warn", "error", "exception", "critical"}
)


@dataclass(frozen=True)
class Finding:
    path: Path
    line: int
    markers: tuple[str, ...]


class PopulationUnknown(RuntimeError):
    """The source did not permit a complete, fail-closed extraction."""


def _receiver_is_logger(node: ast.expr) -> bool:
    rendered = ast.unparse(node)
    return (
        rendered in {"logger", "log"}
        or rendered.endswith(".logger")
        or rendered.endswith("._logger")
        or rendered.startswith("logging.getLogger(")
    )


def _is_logging_call(node: ast.Call) -> bool:
    return (
        isinstance(node.func, ast.Attribute)
        and node.func.attr in LOG_METHODS
        and _receiver_is_logger(node.func.value)
    )


def _static_text(node: ast.AST) -> str:
    """Return every statically knowable character in a logging format.

    Unknown interpolation values become a neutral placeholder.  A variable or
    helper-produced *format* is different: it may itself contain a marker, so
    accepting it would make the marker population incomplete.
    """

    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        parts: list[str] = []
        for value in node.values:
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                parts.append(value.value)
            elif isinstance(value, ast.FormattedValue):
                if isinstance(value.value, ast.Constant) and isinstance(value.value.value, str):
                    parts.append(value.value.value)
                else:
                    parts.append("{value}")
            else:  # pragma: no cover - future AST node shape; fail closed
                raise PopulationUnknown(f"unsupported f-string node {type(value).__name__}")
        return "".join(parts)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return _static_text(node.left) + _static_text(node.right)
    raise PopulationUnknown(
        f"dynamic logging format `{ast.unparse(node)}` may conceal an emitted marker"
    )


def inspect_file(path: Path) -> tuple[list[Finding], int, set[str]]:
    try:
        source = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise PopulationUnknown(f"cannot read {path}: {exc}") from exc
    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError as exc:
        raise PopulationUnknown(f"cannot parse {path}:{exc.lineno}: {exc.msg}") from exc

    findings: list[Finding] = []
    calls = 0
    population: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not _is_logging_call(node):
            continue
        calls += 1
        if not node.args:
            raise PopulationUnknown(f"logging call has no format at {path}:{node.lineno}")
        text = _static_text(node.args[0])
        markers = tuple(sorted(set(MARKER_RE.findall(text))))
        population.update(markers)
        if len(markers) > 1:
            findings.append(Finding(path=path, line=node.lineno, markers=markers))
    return findings, calls, population


def inspect_roots(roots: Iterable[Path]) -> tuple[list[Finding], int, set[str]]:
    paths: list[Path] = []
    for root in roots:
        if not root.is_dir():
            raise PopulationUnknown(f"source root does not exist: {root}")
        paths.extend(root.rglob("*.py"))
    paths = sorted(set(paths))
    if not paths:
        raise PopulationUnknown("source roots contain no Python files")

    findings: list[Finding] = []
    calls = 0
    population: set[str] = set()
    for path in paths:
        file_findings, file_calls, file_population = inspect_file(path)
        findings.extend(file_findings)
        calls += file_calls
        population.update(file_population)
    if calls == 0:
        raise PopulationUnknown("source roots contain no statically inspectable logging calls")
    return findings, calls, population


def _default_roots() -> list[Path]:
    repo = Path(__file__).resolve().parents[2]
    # `ops` is included deliberately: an operational Python service that starts
    # emitting markers must join the same population. Shell evidence consumers
    # are not emitters and therefore cannot make this guard match itself.
    return [repo / "src" / "project_mai_tai", repo / "ops"]


def run(roots: Sequence[Path]) -> int:
    try:
        findings, calls, population = inspect_roots(roots)
    except PopulationUnknown as exc:
        print(f"COULD_NOT_TELL: {exc}", file=sys.stderr)
        return 3

    if findings:
        print("FAIL: a log event contains another event's complete marker", file=sys.stderr)
        for finding in findings:
            joined = ", ".join(finding.markers)
            print(f"  {finding.path}:{finding.line}: {joined}", file=sys.stderr)
        return 1

    print(
        f"PASS: {len(population)} emitted markers in {calls} logging calls; "
        "no logging format contains another marker"
    )
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "roots",
        nargs="*",
        type=Path,
        help="Python source roots (defaults to src/project_mai_tai and ops)",
    )
    args = parser.parse_args(argv)
    return run(args.roots or _default_roots())


if __name__ == "__main__":
    raise SystemExit(main())
