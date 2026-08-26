#!/usr/bin/env python3
"""Read-only acceptance report for Section 82 fan-out identity increment 1.

The report reads immutable database fields and v2 process-start markers. It
does not call a broker or mutate application state.

Verdicts and exit codes:
  PASS                 0  identity is complete and the duplicate grade is clean
  FAIL                 1  exercised records violate identity/chain/duplicate rules
  COULD_NOT_TELL       2  evidence is unreadable or a lifecycle spans a restart
  UNEXERCISED          3  no queued Webull fan-out intent exists in the window
"""

from __future__ import annotations

import argparse
import csv
from dataclasses import dataclass
from datetime import UTC, datetime
import gzip
import io
from pathlib import Path
import re
import subprocess
from typing import Callable, Sequence

from project_mai_tai.fanout_identity import fanout_slot_id


PASS = 0
FAIL = 1
COULD_NOT_TELL = 2
UNEXERCISED = 3

TERMINAL_STATUSES = {"filled", "cancelled", "rejected"}
START_MARKER = "schwab_1m_v2 bot starting pid="
START_RE = re.compile(
    r"^(?P<at>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}(?:[,.]\d+)?)"
    r".*schwab_1m_v2 bot starting pid=(?P<pid>\d+)"
)


@dataclass(frozen=True)
class IntentRecord:
    record_id: str
    symbol: str
    account: str
    at: datetime
    last_at: datetime
    segment_id: str
    slot: str
    slot_id: str
    attempt_id: str
    source: str


@dataclass(frozen=True)
class AttemptRecord:
    record_id: str
    intent_id: str
    client_order_id: str
    symbol: str
    account: str
    at: datetime
    last_at: datetime
    status: str
    segment_id: str
    slot: str
    slot_id: str
    attempt_id: str
    predecessor_id: str
    source: str
    event_total: int
    event_identity: int
    fill_total: int
    fill_identity: int


@dataclass(frozen=True)
class ProcessStart:
    at: datetime
    pid: int


@dataclass(frozen=True)
class Report:
    exit_code: int
    verdict: str
    lines: tuple[str, ...]


class EvidenceFailure(RuntimeError):
    """The read-only evidence source could not be interpreted safely."""


SQL = r"""
BEGIN READ ONLY;
COPY (
WITH intent_rows AS (
    SELECT
        'intent'::text AS kind,
        ti.id::text AS record_id,
        ''::text AS intent_id,
        ''::text AS client_order_id,
        ti.symbol::text AS symbol,
        ba.name::text AS account,
        ti.created_at AS at,
        ti.updated_at AS last_at,
        ti.status::text AS status,
        coalesce(ti.payload::jsonb->'metadata'->>'fanout_segment_id', '') AS segment_id,
        coalesce(ti.payload::jsonb->'metadata'->>'fanout_slot', '') AS slot,
        coalesce(ti.payload::jsonb->'metadata'->>'fanout_slot_id', '') AS slot_id,
        coalesce(ti.payload::jsonb->'metadata'->>'fanout_attempt_id', '') AS attempt_id,
        coalesce(ti.payload::jsonb->'metadata'->>'fanout_predecessor_attempt_id', '') AS predecessor_id,
        coalesce(ti.payload::jsonb->'metadata'->>'fanout_source', '') AS source,
        0::bigint AS event_total,
        0::bigint AS event_identity,
        0::bigint AS fill_total,
        0::bigint AS fill_identity
    FROM trade_intents ti
    JOIN strategies s ON s.id = ti.strategy_id
    JOIN broker_accounts ba ON ba.id = ti.broker_account_id
    WHERE s.code = 'schwab_1m_v2'
      AND ti.side = 'buy'
      AND ti.intent_type = 'open'
      AND ti.payload::jsonb->'metadata' ? 'fanout_source'
      AND ti.created_at >= :'window_since'::timestamptz
      AND ti.created_at < :'window_until'::timestamptz
), order_rows AS (
    SELECT
        'order'::text AS kind,
        bo.id::text AS record_id,
        coalesce(bo.intent_id::text, '') AS intent_id,
        bo.client_order_id::text AS client_order_id,
        bo.symbol::text AS symbol,
        ba.name::text AS account,
        bo.submitted_at AS at,
        greatest(
            bo.updated_at,
            coalesce((SELECT max(e.event_at) FROM broker_order_events e WHERE e.order_id = bo.id), bo.updated_at),
            coalesce((SELECT max(f.filled_at) FROM fills f WHERE f.order_id = bo.id), bo.updated_at)
        ) AS last_at,
        bo.status::text AS status,
        coalesce(bo.payload::jsonb->>'fanout_segment_id', '') AS segment_id,
        coalesce(bo.payload::jsonb->>'fanout_slot', '') AS slot,
        coalesce(bo.payload::jsonb->>'fanout_slot_id', '') AS slot_id,
        coalesce(bo.payload::jsonb->>'fanout_attempt_id', '') AS attempt_id,
        coalesce(bo.payload::jsonb->>'fanout_predecessor_attempt_id', '') AS predecessor_id,
        coalesce(bo.payload::jsonb->>'fanout_source', '') AS source,
        (SELECT count(*) FROM broker_order_events e WHERE e.order_id = bo.id) AS event_total,
        (SELECT count(*) FROM broker_order_events e
          WHERE e.order_id = bo.id
            AND coalesce(e.payload::jsonb->'metadata'->>'fanout_segment_id', '') = coalesce(bo.payload::jsonb->>'fanout_segment_id', '')
            AND coalesce(e.payload::jsonb->'metadata'->>'fanout_slot_id', '') = coalesce(bo.payload::jsonb->>'fanout_slot_id', '')
            AND coalesce(e.payload::jsonb->'metadata'->>'fanout_attempt_id', '') = coalesce(bo.payload::jsonb->>'fanout_attempt_id', '')
            AND coalesce(e.payload::jsonb->'metadata'->>'fanout_predecessor_attempt_id', '') = coalesce(bo.payload::jsonb->>'fanout_predecessor_attempt_id', '')
        ) AS event_identity,
        (SELECT count(*) FROM fills f WHERE f.order_id = bo.id) AS fill_total,
        (SELECT count(*) FROM fills f
          WHERE f.order_id = bo.id
            AND coalesce(f.payload::jsonb->'metadata'->>'fanout_segment_id', '') = coalesce(bo.payload::jsonb->>'fanout_segment_id', '')
            AND coalesce(f.payload::jsonb->'metadata'->>'fanout_slot_id', '') = coalesce(bo.payload::jsonb->>'fanout_slot_id', '')
            AND coalesce(f.payload::jsonb->'metadata'->>'fanout_attempt_id', '') = coalesce(bo.payload::jsonb->>'fanout_attempt_id', '')
            AND coalesce(f.payload::jsonb->'metadata'->>'fanout_predecessor_attempt_id', '') = coalesce(bo.payload::jsonb->>'fanout_predecessor_attempt_id', '')
        ) AS fill_identity
    FROM broker_orders bo
    JOIN strategies s ON s.id = bo.strategy_id
    JOIN broker_accounts ba ON ba.id = bo.broker_account_id
    JOIN trade_intents ti ON ti.id = bo.intent_id
    WHERE s.code = 'schwab_1m_v2'
      AND bo.side = 'buy'
      AND bo.payload::jsonb ? 'fanout_source'
      AND ti.created_at >= :'window_since'::timestamptz
      AND ti.created_at < :'window_until'::timestamptz
      AND bo.submitted_at < :'window_until'::timestamptz
)
SELECT * FROM intent_rows
UNION ALL
SELECT * FROM order_rows
ORDER BY at, kind, record_id
) TO STDOUT WITH (FORMAT CSV, HEADER TRUE);
COMMIT;
""".strip()


def parse_instant(raw: str, label: str) -> datetime:
    try:
        value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{label} is not an ISO instant: {raw!r}") from exc
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{label} must carry Z or an explicit UTC offset")
    return value.astimezone(UTC)


def _integer(raw: str, field: str) -> int:
    try:
        value = int(raw)
    except ValueError as exc:
        raise EvidenceFailure(f"{field} is not an integer: {raw!r}") from exc
    if value < 0:
        raise EvidenceFailure(f"{field} is negative: {value}")
    return value


def parse_database_rows(raw: str) -> tuple[list[IntentRecord], list[AttemptRecord]]:
    reader = csv.DictReader(io.StringIO(raw))
    required = {
        "kind", "record_id", "intent_id", "client_order_id", "symbol", "account",
        "at", "last_at", "status", "segment_id", "slot", "slot_id", "attempt_id",
        "predecessor_id", "source", "event_total", "event_identity", "fill_total",
        "fill_identity",
    }
    if reader.fieldnames is None or set(reader.fieldnames) != required:
        raise EvidenceFailure("database output header does not match the fixed report schema")
    intents: list[IntentRecord] = []
    attempts: list[AttemptRecord] = []
    for row in reader:
        at = parse_instant(row["at"], "database timestamp")
        last_at = parse_instant(row["last_at"], "database last timestamp")
        if last_at < at:
            raise EvidenceFailure(
                f"database record {row['record_id']} ends before it starts"
            )
        common = {
            "record_id": row["record_id"],
            "symbol": row["symbol"].upper(),
            "account": row["account"],
            "at": at,
            "last_at": last_at,
            "segment_id": row["segment_id"],
            "slot": row["slot"],
            "slot_id": row["slot_id"],
            "attempt_id": row["attempt_id"],
            "source": row["source"],
        }
        if row["kind"] == "intent":
            intents.append(IntentRecord(**common))
        elif row["kind"] == "order":
            attempts.append(
                AttemptRecord(
                    **common,
                    intent_id=row["intent_id"],
                    client_order_id=row["client_order_id"],
                    status=row["status"],
                    predecessor_id=row["predecessor_id"],
                    event_total=_integer(row["event_total"], "event_total"),
                    event_identity=_integer(row["event_identity"], "event_identity"),
                    fill_total=_integer(row["fill_total"], "fill_total"),
                    fill_identity=_integer(row["fill_identity"], "fill_identity"),
                )
            )
        else:
            raise EvidenceFailure(f"unknown database row kind: {row['kind']!r}")
    return intents, attempts


def query_database(since: datetime, until: datetime) -> tuple[list[IntentRecord], list[AttemptRecord]]:
    command = [
        "sudo", "-n", "-u", "postgres", "psql", "-X", "-qAt",
        "-v", "ON_ERROR_STOP=1",
        "-v", f"window_since={since.isoformat()}",
        "-v", f"window_until={until.isoformat()}",
        "-d", "project_mai_tai", "-c", SQL,
    ]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=45, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        raise EvidenceFailure(f"could not execute the read-only database query: {exc}") from exc
    if result.returncode != 0:
        raise EvidenceFailure(result.stderr.strip() or "psql exited non-zero without an error")
    if result.stderr.strip():
        raise EvidenceFailure(f"psql wrote unexpected stderr: {result.stderr.strip()}")
    return parse_database_rows(result.stdout)


def parse_process_starts(lines: Sequence[str]) -> list[ProcessStart]:
    starts: dict[tuple[datetime, int], ProcessStart] = {}
    for line in lines:
        if START_MARKER not in line:
            continue
        match = START_RE.search(line)
        if match is None:
            raise EvidenceFailure("a v2 start marker exists but its timestamp/PID is unreadable")
        raw_at = match.group("at").replace(",", ".")
        at = datetime.fromisoformat(raw_at).replace(tzinfo=UTC)
        start = ProcessStart(at=at, pid=int(match.group("pid")))
        starts[(start.at, start.pid)] = start
    return sorted(starts.values(), key=lambda item: (item.at, item.pid))


def read_process_starts(log_dir: Path = Path("/var/log/project-mai-tai")) -> list[ProcessStart]:
    try:
        found = subprocess.run(
            [
                "sudo", "-n", "find", str(log_dir), "-maxdepth", "1", "-type", "f",
                "-name", "schwab-1m-v2.log*", "-print0",
            ],
            capture_output=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise EvidenceFailure(f"could not enumerate v2 logs: {exc}") from exc
    if found.returncode != 0:
        raise EvidenceFailure(found.stderr.decode(errors="replace").strip() or "log discovery failed")
    paths = [item.decode() for item in found.stdout.split(b"\0") if item]
    if not paths:
        raise EvidenceFailure("no schwab-1m-v2 log files were found")
    lines: list[str] = []
    for raw_path in paths:
        path = Path(raw_path)
        try:
            data = subprocess.run(
                ["sudo", "-n", "cat", "--", str(path)],
                capture_output=True,
                timeout=20,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise EvidenceFailure(f"could not read {path}: {exc}") from exc
        if data.returncode != 0:
            raise EvidenceFailure(
                data.stderr.decode(errors="replace").strip() or f"could not read {path}"
            )
        payload = data.stdout
        if path.suffix == ".gz":
            try:
                payload = gzip.decompress(payload)
            except (OSError, EOFError) as exc:
                raise EvidenceFailure(f"could not decompress {path}: {exc}") from exc
        lines.extend(payload.decode(errors="replace").splitlines())
    return parse_process_starts(lines)


def _identity_error(record: IntentRecord | AttemptRecord) -> str | None:
    try:
        expected = fanout_slot_id(
            strategy_code="schwab_1m_v2",
            symbol=record.symbol,
            segment_id=record.segment_id,
            slot=record.slot,
        )
    except ValueError as exc:
        return str(exc)
    if record.slot_id != expected:
        return "slot id does not match (strategy, symbol, segment, slot)"
    if not record.attempt_id:
        return "attempt id is missing"
    if isinstance(record, AttemptRecord) and record.attempt_id != record.client_order_id:
        return "attempt id does not match the existing client order id"
    return None


def _chain_metrics(attempts: Sequence[AttemptRecord]) -> tuple[int, int, list[str]]:
    by_slot: dict[tuple[str, str], list[AttemptRecord]] = {}
    for attempt in attempts:
        by_slot.setdefault((attempt.account, attempt.slot_id), []).append(attempt)
    max_depth = 0
    roots = 0
    errors: list[str] = []
    for (account, slot_id), members in by_slot.items():
        label = f"{account}/{slot_id}"
        nodes = {item.attempt_id: item for item in members}
        if len(nodes) != len(members):
            errors.append(f"slot {label} repeats an attempt id")
            continue
        slot_roots = 0
        for member in members:
            if not member.predecessor_id:
                roots += 1
                slot_roots += 1
            elif member.predecessor_id not in nodes:
                errors.append(
                    f"attempt {member.attempt_id} names a predecessor outside slot {label}"
                )
        if len(members) > 1 and slot_roots != 1:
            errors.append(
                f"slot {label} has {slot_roots} roots across {len(members)} attempts"
            )
        for member in members:
            seen: set[str] = set()
            cursor = member
            depth = 1
            while cursor.predecessor_id:
                if cursor.attempt_id in seen:
                    errors.append(f"slot {label} contains a predecessor cycle")
                    break
                seen.add(cursor.attempt_id)
                prior = nodes.get(cursor.predecessor_id)
                if prior is None:
                    break
                if prior.at > cursor.at:
                    errors.append(
                        f"attempt {cursor.attempt_id} names a later attempt as predecessor"
                    )
                    break
                cursor = prior
                depth += 1
            max_depth = max(max_depth, depth)
    return roots, max_depth, errors


def evaluate(
    *,
    intents: Sequence[IntentRecord],
    attempts: Sequence[AttemptRecord],
    starts: Sequence[ProcessStart],
    since: datetime,
    until: datetime,
) -> Report:
    if since >= until:
        return Report(COULD_NOT_TELL, "COULD_NOT_TELL", ("window start is not before end",))
    lines = [
        "### FANOUT IDENTITY ACCEPTANCE",
        f"    window=[{since.isoformat()}, {until.isoformat()})",
        "    trigger=queued Webull buy/open intent carrying fanout_source",
        "    polarity=complete identity and zero same-venue duplicate slots are required",
    ]
    window_starts = [item for item in starts if since <= item.at < until]
    prior_starts = [item for item in starts if item.at < since]
    overlapping_starts = ([max(prior_starts, key=lambda item: item.at)] if prior_starts else [])
    overlapping_starts.extend(window_starts)
    lines.append(
        "    process_starts="
        + (
            ", ".join(f"pid={item.pid}@{item.at.isoformat()}" for item in overlapping_starts)
            or "0"
        )
    )
    if not intents:
        lines.append("    queued=0 submitted=0 terminal=0 filled=0")
        lines.append("    => UNEXERCISED. denominator is 0; no fan-out intent exercised the increment")
        return Report(UNEXERCISED, "UNEXERCISED", tuple(lines))

    intent_identity_errors = [
        f"intent {record.record_id}: {error}"
        for record in intents
        if (error := _identity_error(record)) is not None
    ]
    order_identity_errors = [
        f"order {record.client_order_id}: {error}"
        for record in attempts
        if (error := _identity_error(record)) is not None
    ]
    identity_errors = [*intent_identity_errors, *order_identity_errors]
    event_total = sum(item.event_total for item in attempts)
    event_identity = sum(item.event_identity for item in attempts)
    fill_total = sum(item.fill_total for item in attempts)
    fill_identity = sum(item.fill_identity for item in attempts)
    if event_identity != event_total:
        identity_errors.append(
            f"order-event identity coverage is {event_identity} of {event_total}"
        )
    if fill_identity != fill_total:
        identity_errors.append(f"fill identity coverage is {fill_identity} of {fill_total}")

    intents_by_id = {item.record_id: item for item in intents}
    attempts_by_intent: dict[str, list[AttemptRecord]] = {}
    for attempt in attempts:
        attempts_by_intent.setdefault(attempt.intent_id, []).append(attempt)
        if attempt.intent_id not in intents_by_id:
            identity_errors.append(
                f"order {attempt.client_order_id} has no queued intent in the report population"
            )
    for intent in intents:
        members = attempts_by_intent.get(intent.record_id, [])
        if members and intent.attempt_id not in {item.attempt_id for item in members}:
            identity_errors.append(
                f"intent {intent.record_id} root attempt {intent.attempt_id} is absent from its order chain"
            )

    roots, max_depth, chain_errors = _chain_metrics(attempts)
    identity_errors.extend(chain_errors)
    terminal = sum(item.status in TERMINAL_STATUSES for item in attempts)
    filled_attempts = [item for item in attempts if item.fill_total > 0]
    duplicate_groups: dict[tuple[str, str, str, str], int] = {}
    for item in filled_attempts:
        key = (item.account, item.symbol, item.segment_id, item.slot)
        duplicate_groups[key] = duplicate_groups.get(key, 0) + 1
    duplicates = {key: count for key, count in duplicate_groups.items() if count > 1}

    spanning: set[str] = set()
    all_records = [*intents, *attempts]
    for start in window_starts:
        symbols_before = {item.symbol for item in all_records if item.at < start.at}
        symbols_after = {item.symbol for item in all_records if item.at >= start.at}
        spanning.update(symbols_before & symbols_after)
        spanning.update(
            item.symbol for item in all_records if item.at < start.at <= item.last_at
        )

    if not overlapping_starts:
        lines.append("    => COULD_NOT_TELL. no PID/process start overlaps the report window")
        return Report(COULD_NOT_TELL, "COULD_NOT_TELL", tuple(lines))
    if window_starts and not prior_starts:
        earliest = min(window_starts, key=lambda item: item.at)
        if any(item.at < earliest.at for item in all_records):
            lines.append(
                "    => COULD_NOT_TELL. records predate the earliest readable PID/process start"
            )
            return Report(COULD_NOT_TELL, "COULD_NOT_TELL", tuple(lines))

    lines.extend(
        (
            f"    queued={len(intents)} submitted={len(attempts)} terminal={terminal} "
            f"filled_attempts={len(filled_attempts)}",
            f"    intent_identity={len(intents) - len(intent_identity_errors)} "
            f"of {len(intents)}  order_events={event_identity} of {event_total}  "
            f"fills={fill_identity} of {fill_total}",
            f"    venue_scoped_slots={len({(item.account, item.slot_id) for item in attempts if item.slot_id})} "
            f"roots={roots} "
            f"max_chain_depth={max_depth}",
            f"    duplicate_grade_denominator={len(duplicate_groups)} filled venue-scoped slots "
            f"duplicates={len(duplicates)}",
        )
    )
    if spanning:
        lines.append(
            "    => COULD_NOT_TELL. restart-spanning symbols=" + ",".join(sorted(spanning))
        )
        return Report(COULD_NOT_TELL, "COULD_NOT_TELL", tuple(lines))
    if identity_errors:
        lines.extend(f"    identity_error={item}" for item in identity_errors[:12])
        if len(identity_errors) > 12:
            lines.append(f"    identity_error=... {len(identity_errors) - 12} more")
        lines.append("    => FAIL. duplicate grade refused because identity coverage is incomplete")
        return Report(FAIL, "FAIL", tuple(lines))
    if duplicates:
        for key, count in sorted(duplicates.items()):
            lines.append(f"    duplicate={key} filled_attempts={count}")
        lines.append("    => FAIL. more than one filled attempt exists in a venue-scoped slot")
        return Report(FAIL, "FAIL", tuple(lines))
    lines.append(
        "    => PASS. [V2-FANOUT-IDENTITY-ACCEPTED] complete identity; "
        "depth-1 fills are valid and deep chains remain readable"
    )
    return Report(PASS, "PASS", tuple(lines))


def run_report(
    *,
    since: datetime,
    until: datetime,
    query: Callable[[datetime, datetime], tuple[list[IntentRecord], list[AttemptRecord]]] = query_database,
    process_reader: Callable[[], list[ProcessStart]] = read_process_starts,
) -> Report:
    try:
        intents, attempts = query(since, until)
        starts = process_reader()
    except (EvidenceFailure, ValueError) as exc:
        return Report(
            COULD_NOT_TELL,
            "COULD_NOT_TELL",
            (
                "### FANOUT IDENTITY ACCEPTANCE",
                f"    window=[{since.isoformat()}, {until.isoformat()})",
                f"    => COULD_NOT_TELL. read-only evidence failed: {exc}",
            ),
        )
    return evaluate(intents=intents, attempts=attempts, starts=starts, since=since, until=until)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--since", required=True, help="inclusive ISO instant with Z/offset")
    parser.add_argument("--until", required=True, help="exclusive ISO instant with Z/offset")
    args = parser.parse_args(argv)
    try:
        since = parse_instant(args.since, "--since")
        until = parse_instant(args.until, "--until")
    except ValueError as exc:
        print(f"COULD_NOT_TELL: {exc}")
        return COULD_NOT_TELL
    report = run_report(since=since, until=until)
    print("\n".join(report.lines))
    return report.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
