#!/usr/bin/env python3
"""Read-only acceptance checks for evidence stored in allowlisted DB fields.

This is deliberately not a SQL runner. The CLI accepts only a named check and
an explicit time window; table, field, predicates, denominator, database, and
SQL are immutable code. The first check grades Q1/#758's durable
``broker_order_events.event_source`` evidence.

Verdicts and exit codes:
  PASS                 0  the accepted field predicate matched its minimum
  FAIL                 1  opportunities exist but the predicate never matched
  VOID_COULD_NOT_TELL  2  query/schema/NULL/vocabulary/population is unreadable
  UNEXERCISED          3  denominator is zero (0 of 0 is never a pass)
"""

from __future__ import annotations

import argparse
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
import subprocess


PASS = 0
FAIL = 1
VOID = 2
UNEXERCISED = 3


@dataclass(frozen=True)
class CheckSpec:
    name: str
    result_key: str
    table: str
    field: str
    window_field: str
    denominator: str
    predicate: str
    minimum: int
    sql: str


@dataclass(frozen=True)
class Counts:
    result_key: str
    denominator: int
    matched: int
    unknown: int
    nulls: int
    invalid: int


@dataclass(frozen=True)
class Verdict:
    exit_code: int
    name: str
    detail: str


BROKER_ORDER_EVENT_SOURCE = CheckSpec(
    name="broker-order-event-source",
    result_key="broker_order_event_source_v1",
    table="broker_order_events",
    field="event_source",
    window_field="event_at",
    denominator="event_type = 'rejected'",
    predicate="event_source IN ('broker', 'client')",
    # `unknown` is an intentional honest result for transport/SDK failures with no venue
    # evidence. Q1 acceptance is therefore existence (did the new field ever carry a real
    # classification?), not 100% coverage (which would contradict the field's contract).
    minimum=1,
    sql="""
BEGIN READ ONLY;
SELECT concat_ws('|',
    'broker_order_event_source_v1',
    count(*),
    count(*) FILTER (WHERE event_source IN ('broker', 'client')),
    count(*) FILTER (WHERE event_source = 'unknown'),
    count(*) FILTER (WHERE event_source IS NULL),
    count(*) FILTER (
        WHERE event_source IS NOT NULL
          AND event_source NOT IN ('broker', 'client', 'unknown')
    )
)
FROM broker_order_events
WHERE event_type = 'rejected'
  AND event_at >= :'window_since'::timestamptz
  AND event_at < :'window_until'::timestamptz;
COMMIT;
""".strip(),
)

CHECKS = {BROKER_ORDER_EVENT_SOURCE.name: BROKER_ORDER_EVENT_SOURCE}


class QueryFailure(RuntimeError):
    """The read-only query did not produce one trustworthy result row."""


def parse_instant(raw: str, label: str) -> datetime:
    try:
        value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{label} is not an ISO instant: {raw!r}") from exc
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{label} must carry Z or an explicit UTC offset")
    return value.astimezone(UTC)


def parse_counts(raw: str) -> Counts:
    lines = [line.strip() for line in raw.splitlines() if line.strip()]
    if len(lines) != 1:
        raise QueryFailure(f"expected exactly one result row, received {len(lines)}")
    fields = lines[0].split("|")
    if len(fields) != 6:
        raise QueryFailure(f"expected 6 result fields, received {len(fields)}")
    try:
        values = [int(value) for value in fields[1:]]
    except ValueError as exc:
        raise QueryFailure("one or more count fields are not integers") from exc
    if any(value < 0 for value in values):
        raise QueryFailure("count fields must not be negative")
    return Counts(fields[0], *values)


def query_counts(spec: CheckSpec, since: datetime, until: datetime) -> Counts:
    """Execute one immutable query in a read-only PostgreSQL transaction."""

    # psql performs :variable interpolation for files/stdin, not for a -c string.
    command = [
        "sudo",
        "-n",
        "-u",
        "postgres",
        "psql",
        "-X",
        "-qAt",
        "-v",
        "ON_ERROR_STOP=1",
        "-v",
        f"window_since={since.isoformat()}",
        "-v",
        f"window_until={until.isoformat()}",
        "-d",
        "project_mai_tai",
        "-f",
        "-",
    ]
    try:
        result = subprocess.run(
            command,
            input=spec.sql,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise QueryFailure(f"could not execute the read-only psql query: {exc}") from exc
    if result.returncode != 0:
        error = result.stderr.strip() or "psql exited non-zero without an error message"
        raise QueryFailure(error)
    if result.stderr.strip():
        raise QueryFailure(f"psql wrote unexpected stderr: {result.stderr.strip()}")
    return parse_counts(result.stdout)


def evaluate(spec: CheckSpec, counts: Counts) -> Verdict:
    if counts.result_key != spec.result_key:
        return Verdict(VOID, "VOID_COULD_NOT_TELL", "result key does not match the requested check")
    classified = counts.matched + counts.unknown + counts.nulls + counts.invalid
    if classified != counts.denominator:
        return Verdict(
            VOID,
            "VOID_COULD_NOT_TELL",
            f"field buckets total {classified}, but denominator is {counts.denominator}",
        )
    if counts.nulls:
        return Verdict(
            VOID,
            "VOID_COULD_NOT_TELL",
            f"{counts.nulls} opportunity row(s) have NULL {spec.field}",
        )
    if counts.invalid:
        return Verdict(
            VOID,
            "VOID_COULD_NOT_TELL",
            f"{counts.invalid} row(s) use an unrecognised {spec.field} value",
        )
    if counts.denominator == 0:
        return Verdict(
            UNEXERCISED,
            "UNEXERCISED",
            "denominator is 0; no rejected order event could exercise the field",
        )
    if counts.matched < spec.minimum:
        return Verdict(
            FAIL,
            "FAIL",
            f"predicate matched {counts.matched} time(s), below minimum {spec.minimum}",
        )
    return Verdict(PASS, "PASS", "a real rejection stored broker-or-client provenance")


def context_lines(spec: CheckSpec, since: datetime, until: datetime) -> tuple[str, ...]:
    return (
        f"### FIELD ACCEPTANCE  check={spec.name}",
        f"    table={spec.table}  field={spec.field}",
        f"    window={spec.window_field} in [{since.isoformat()}, {until.isoformat()})",
        f"    denominator={spec.denominator}",
        f"    predicate={spec.predicate}",
    )


def render(spec: CheckSpec, since: datetime, until: datetime, counts: Counts, verdict: Verdict) -> str:
    return "\n".join(
        context_lines(spec, since, until)
        + (
            f"    matched={counts.matched} of {counts.denominator}  unknown={counts.unknown} "
            f"null={counts.nulls} invalid={counts.invalid}  minimum={spec.minimum}",
            f"    => {verdict.name}. {verdict.detail}",
        )
    )


def run_check(
    spec: CheckSpec,
    since: datetime,
    until: datetime,
    query: Callable[[CheckSpec, datetime, datetime], Counts] = query_counts,
) -> tuple[int, str]:
    if since >= until:
        output = "\n".join(
            context_lines(spec, since, until)
            + ("    => VOID_COULD_NOT_TELL. window start must be earlier than window end",)
        )
        return VOID, output
    try:
        counts = query(spec, since, until)
    except QueryFailure as exc:
        output = "\n".join(
            context_lines(spec, since, until)
            + (f"    => VOID_COULD_NOT_TELL. read-only query failed: {exc}",)
        )
        return VOID, output
    verdict = evaluate(spec, counts)
    return verdict.exit_code, render(spec, since, until, counts, verdict)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check")
    parser.add_argument("--since", help="inclusive ISO instant with Z/offset")
    parser.add_argument("--until", help="exclusive ISO instant with Z/offset")
    args = parser.parse_args(argv)
    if args.check not in CHECKS:
        print(
            f"VOID_COULD_NOT_TELL: unknown --check {args.check!r}; "
            f"allowed: {', '.join(sorted(CHECKS))}"
        )
        return VOID
    if not args.since or not args.until:
        print("VOID_COULD_NOT_TELL: --since and --until are both required")
        return VOID
    try:
        since = parse_instant(args.since, "--since")
        until = parse_instant(args.until, "--until")
    except ValueError as exc:
        print(f"VOID_COULD_NOT_TELL: {exc}")
        return VOID
    exit_code, output = run_check(CHECKS[args.check], since, until)
    print(output)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
