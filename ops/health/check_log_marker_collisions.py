#!/usr/bin/env python3
"""Refuse ambiguous log-marker counts and cross-marker emitted messages.

B32 is a consumer rule. Marker prefix families are intentional: for example,
``[V2-DB-SEED-GAP]`` and ``[V2-DB-SEED-GAP-CENSUS]`` may both exist. A count of
the bare prefix is unsafe unless it anchors the closing ``]``; a count naming
the longer sibling is already specific and is safe.

The secondary emitted-message rule catches a different spelling of the same
failure: one event's format string must not quote another complete marker.
That is how a LATCHED denominator line once inflated SUPPRESSED counts.

Comments and non-count strings are not consumers, so this parser does not
match its own warnings or test fixtures. The marker grammar is the one used by
``ops/health/evidence.sh``.

Exit codes:
  0  every count is token-specific and every emitted format has one marker
  1  at least one ambiguous count or cross-marker emitted format exists
  3  the marker population or source files could not be inspected
"""

from __future__ import annotations

import argparse
import ast
from dataclasses import dataclass
from pathlib import Path
import re
import sys
from typing import Iterable, Sequence


MARKER_RE = re.compile(r"\[([A-Z][A-Z0-9-]{3,})\]")
LOG_METHODS = frozenset(
    {"debug", "info", "warning", "warn", "error", "exception", "critical"}
)


@dataclass(frozen=True)
class EmissionFinding:
    path: Path
    line: int
    markers: tuple[str, ...]


@dataclass(frozen=True)
class Consumer:
    path: Path
    line: int
    command: str
    pattern: str
    fixed_string: bool


@dataclass(frozen=True)
class ConsumerFinding:
    consumer: Consumer
    bare_marker: str
    longer_markers: tuple[str, ...]


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


def inspect_python_file(path: Path) -> tuple[list[EmissionFinding], int, set[str]]:
    try:
        source = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise PopulationUnknown(f"cannot read {path}: {exc}") from exc
    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError as exc:
        raise PopulationUnknown(f"cannot parse {path}:{exc.lineno}: {exc.msg}") from exc

    findings: list[EmissionFinding] = []
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
            findings.append(EmissionFinding(path=path, line=node.lineno, markers=markers))
    return findings, calls, population


def inspect_python_roots(
    roots: Iterable[Path],
) -> tuple[list[EmissionFinding], int, set[str]]:
    paths: list[Path] = []
    for root in roots:
        if not root.is_dir():
            raise PopulationUnknown(f"source root does not exist: {root}")
        paths.extend(root.rglob("*.py"))
    paths = sorted(set(paths))
    if not paths:
        raise PopulationUnknown("source roots contain no Python files")

    findings: list[EmissionFinding] = []
    calls = 0
    population: set[str] = set()
    for path in paths:
        file_findings, file_calls, file_population = inspect_python_file(path)
        findings.extend(file_findings)
        calls += file_calls
        population.update(file_population)
    if calls == 0 or not population:
        raise PopulationUnknown("source roots contain no emitted-marker population")
    return findings, calls, population


def _strip_shell_comment(line: str) -> str:
    """Remove a real shell comment, preserving # characters inside quotes."""

    quote = ""
    escaped = False
    for index, char in enumerate(line):
        if escaped:
            escaped = False
            continue
        if char == "\\" and quote != "'":
            escaped = True
            continue
        if quote:
            if char == quote:
                quote = ""
            continue
        if char in {"'", '"'}:
            quote = char
        elif char == "#":
            return line[:index]
    return line


def _shell_arg(text: str, start: int) -> tuple[str, int, bool] | None:
    while start < len(text) and text[start].isspace():
        start += 1
    if start >= len(text) or text[start] in ";|&)":
        return None
    quote = text[start] if text[start] in {"'", '"'} else ""
    if quote:
        index = start + 1
        value: list[str] = []
        while index < len(text):
            char = text[index]
            if char == quote:
                return "".join(value), index + 1, True
            if char == "\\" and quote == '"' and index + 1 < len(text):
                value.extend((char, text[index + 1]))
                index += 2
                continue
            value.append(char)
            index += 1
        return None
    index = start
    while index < len(text) and not text[index].isspace() and text[index] not in ";|&)":
        index += 1
    return text[start:index], index, False


def _literal_helper_consumers(path: Path, line: int, code: str) -> list[Consumer]:
    consumers: list[Consumer] = []
    for match in re.finditer(r"(?<![A-Za-z0-9_])(cnt|logcount)\s+", code):
        command = match.group(1)
        first = _shell_arg(code, match.end())
        if first is None:
            continue
        pattern_arg = first
        if command == "logcount":
            second = _shell_arg(code, first[1])
            if second is None:
                continue
            pattern_arg = second
        pattern, _, quoted = pattern_arg
        if quoted:
            consumers.append(
                Consumer(
                    path=path,
                    line=line,
                    command=command,
                    pattern=pattern,
                    fixed_string=command == "logcount",
                )
            )
    return consumers


def _grep_consumers(path: Path, line: int, code: str) -> list[Consumer]:
    consumers: list[Consumer] = []
    for match in re.finditer(r"(?<![A-Za-z0-9_])grep\s+", code):
        cursor = match.end()
        options: list[str] = []
        pattern_arg: tuple[str, int, bool] | None = None
        while True:
            arg = _shell_arg(code, cursor)
            if arg is None:
                break
            value, cursor, quoted = arg
            if value == "--":
                pattern_arg = _shell_arg(code, cursor)
                break
            if not quoted and value.startswith("-"):
                options.append(value)
                continue
            pattern_arg = arg
            break
        if pattern_arg is None or not any("c" in option.lstrip("-") for option in options):
            continue
        pattern, _, quoted = pattern_arg
        if quoted:
            consumers.append(
                Consumer(
                    path=path,
                    line=line,
                    command="grep -c",
                    pattern=pattern,
                    fixed_string=any("F" in option.lstrip("-") for option in options),
                )
            )
    return consumers


def _evidence_consumers(path: Path, line: int, code: str) -> list[Consumer]:
    """Extract evidence.sh's fixed-string marker and denominator counts."""

    consumers: list[Consumer] = []
    for match in re.finditer(r"--(marker|denominator)\s+", code):
        arg = _shell_arg(code, match.end())
        if arg is None:
            continue
        pattern, _, quoted = arg
        if quoted:
            consumers.append(
                Consumer(
                    path=path,
                    line=line,
                    command=f"--{match.group(1)}",
                    pattern=pattern,
                    fixed_string=True,
                )
            )
    return consumers


def inspect_shell_file(path: Path) -> list[Consumer]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise PopulationUnknown(f"cannot read {path}: {exc}") from exc
    consumers: list[Consumer] = []
    for line_number, raw in enumerate(lines, 1):
        code = _strip_shell_comment(raw)
        consumers.extend(_literal_helper_consumers(path, line_number, code))
        consumers.extend(_grep_consumers(path, line_number, code))
        consumers.extend(_evidence_consumers(path, line_number, code))
    return consumers


def inspect_shell_roots(roots: Iterable[Path]) -> list[Consumer]:
    paths: list[Path] = []
    for root in roots:
        if not root.is_dir():
            raise PopulationUnknown(f"consumer root does not exist: {root}")
        paths.extend(root.rglob("*.sh"))
    paths = sorted(set(paths))
    if not paths:
        raise PopulationUnknown("consumer roots contain no shell scripts")
    consumers: list[Consumer] = []
    for path in paths:
        consumers.extend(inspect_shell_file(path))
    if not consumers:
        raise PopulationUnknown("consumer roots contain no literal count consumers")
    return consumers


def find_ambiguous_consumers(
    consumers: Iterable[Consumer], population: set[str],
) -> list[ConsumerFinding]:
    substring_families = {
        marker: tuple(sorted(other for other in population if marker != other and marker in other))
        for marker in population
    }
    substring_families = {
        marker: family for marker, family in substring_families.items() if family
    }
    findings: list[ConsumerFinding] = []
    for consumer in consumers:
        for marker, longer in substring_families.items():
            for match in re.finditer(re.escape(marker), consumer.pattern):
                before = consumer.pattern[match.start() - 1 : match.start()]
                after = consumer.pattern[match.end() :]
                if before and (before.isalnum() or before == "-"):
                    continue
                if after.startswith("-"):
                    continue
                if after.startswith(r"\]") or (consumer.fixed_string and after.startswith("]")):
                    continue
                findings.append(
                    ConsumerFinding(
                        consumer=consumer,
                        bare_marker=marker,
                        longer_markers=longer,
                    )
                )
                break
    return findings


def _default_roots() -> tuple[list[Path], list[Path]]:
    repo = Path(__file__).resolve().parents[2]
    return [repo / "src" / "project_mai_tai", repo / "ops"], [repo / "ops"]


def run(python_roots: Sequence[Path], shell_roots: Sequence[Path]) -> int:
    try:
        emission_findings, calls, population = inspect_python_roots(python_roots)
        consumers = inspect_shell_roots(shell_roots)
        consumer_findings = find_ambiguous_consumers(consumers, population)
    except PopulationUnknown as exc:
        print(f"COULD_NOT_TELL: {exc}", file=sys.stderr)
        return 3

    if consumer_findings or emission_findings:
        if consumer_findings:
            print("FAIL: ambiguous substring marker counts", file=sys.stderr)
            for finding in consumer_findings:
                consumer = finding.consumer
                siblings = ", ".join(finding.longer_markers)
                print(
                    f"  {consumer.path}:{consumer.line}: {consumer.command} "
                    f"{consumer.pattern!r} counts {finding.bare_marker} and sibling(s): {siblings}",
                    file=sys.stderr,
                )
        if emission_findings:
            print("FAIL: a log event contains another event's complete marker", file=sys.stderr)
            for finding in emission_findings:
                joined = ", ".join(finding.markers)
                print(f"  {finding.path}:{finding.line}: {joined}", file=sys.stderr)
        return 1

    print(
        f"PASS: {len(consumers)} literal count consumers are token-specific across "
        f"{len(population)} emitted markers; {calls} logging formats are cross-marker clean"
    )
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--python-root", action="append", type=Path, default=[])
    parser.add_argument("--shell-root", action="append", type=Path, default=[])
    args = parser.parse_args(argv)
    default_python, default_shell = _default_roots()
    return run(args.python_root or default_python, args.shell_root or default_shell)


if __name__ == "__main__":
    raise SystemExit(main())
