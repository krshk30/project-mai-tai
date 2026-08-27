#!/usr/bin/env python3
"""Grade only the shared cross-venue fan-out identity introduced by Section 82.

This report is read-only.  Its denominator is queued Webull fan-out BUY/open
intents in the requested window.  A record is paired when a Schwab primary
intent for the same symbol carries the exact same deterministic segment/slot
identity.  It does not grade fills, duplicate exposure, or slot consumption.

Exit codes:
  PASS            0  every exercised Webull leg has one usable shared identity
  FAIL            1  an exercised Webull leg is missing/malformed/unpaired
  COULD_NOT_TELL  2  the window or database evidence is unreadable
  UNEXERCISED     3  no Webull fan-out leg exists in the window
"""

from __future__ import annotations

import argparse
import csv
from dataclasses import dataclass
from datetime import UTC, datetime
import io
import subprocess
from typing import Callable, Sequence

from project_mai_tai.fanout_identity import fanout_slot_id


PASS = 0
FAIL = 1
COULD_NOT_TELL = 2
UNEXERCISED = 3


@dataclass(frozen=True)
class PairRecord:
    intent_id: str
    symbol: str
    segment_id: str
    slot: str
    slot_id: str
    matching_primary_intents: int


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
WITH candidates AS (
    SELECT
        ti.id::text AS intent_id,
        upper(ti.symbol::text) AS symbol,
        lower(ba.provider::text) AS provider,
        coalesce(ti.payload::jsonb->'metadata'->>'fanout_leg', '') AS fanout_leg,
        coalesce(ti.payload::jsonb->'metadata'->>'fanout_segment_id', '') AS segment_id,
        coalesce(ti.payload::jsonb->'metadata'->>'fanout_slot', '') AS slot,
        coalesce(ti.payload::jsonb->'metadata'->>'fanout_slot_id', '') AS slot_id
    FROM trade_intents ti
    JOIN strategies s ON s.id = ti.strategy_id
    JOIN broker_accounts ba ON ba.id = ti.broker_account_id
    WHERE s.code = 'schwab_1m_v2'
      AND ti.side = 'buy'
      AND ti.intent_type = 'open'
      AND ti.created_at >= :'window_since'::timestamptz
      AND ti.created_at < :'window_until'::timestamptz
), webull AS (
    SELECT *
    FROM candidates
    WHERE provider = 'webull'
      AND fanout_leg = 'webull'
)
SELECT
    w.intent_id,
    w.symbol,
    w.segment_id,
    w.slot,
    w.slot_id,
    (
        SELECT count(*)
        FROM candidates p
        WHERE p.provider = 'schwab'
          AND p.fanout_leg = ''
          AND p.symbol = w.symbol
          AND p.segment_id <> ''
          AND p.segment_id = w.segment_id
          AND p.slot <> ''
          AND p.slot = w.slot
          AND p.slot_id <> ''
          AND p.slot_id = w.slot_id
    ) AS matching_primary_intents
FROM webull w
ORDER BY w.symbol, w.intent_id
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


def parse_rows(raw: str) -> list[PairRecord]:
    reader = csv.DictReader(io.StringIO(raw))
    required = {
        "intent_id",
        "symbol",
        "segment_id",
        "slot",
        "slot_id",
        "matching_primary_intents",
    }
    if reader.fieldnames is None or set(reader.fieldnames) != required:
        raise EvidenceFailure("database output header does not match the pairing schema")
    records: list[PairRecord] = []
    for row in reader:
        try:
            primary_count = int(row["matching_primary_intents"])
        except ValueError as exc:
            raise EvidenceFailure(
                f"matching_primary_intents is not an integer: {row['matching_primary_intents']!r}"
            ) from exc
        if primary_count < 0:
            raise EvidenceFailure("matching_primary_intents is negative")
        records.append(
            PairRecord(
                intent_id=row["intent_id"],
                symbol=row["symbol"].upper(),
                segment_id=row["segment_id"],
                slot=row["slot"],
                slot_id=row["slot_id"],
                matching_primary_intents=primary_count,
            )
        )
    return records


def query_database(since: datetime, until: datetime) -> list[PairRecord]:
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
            input=SQL,
            capture_output=True,
            text=True,
            timeout=45,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise EvidenceFailure(f"could not execute the read-only database query: {exc}") from exc
    if result.returncode != 0:
        raise EvidenceFailure(result.stderr.strip() or "psql exited non-zero without an error")
    if result.stderr.strip():
        raise EvidenceFailure(f"psql wrote unexpected stderr: {result.stderr.strip()}")
    return parse_rows(result.stdout)


def _identity_error(record: PairRecord) -> str | None:
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
    if record.matching_primary_intents == 0:
        return "no Schwab primary intent carries the same identity"
    return None


def evaluate(
    records: Sequence[PairRecord], *, since: datetime, until: datetime
) -> Report:
    lines = [
        "### CROSS-VENUE FANOUT IDENTITY ACCEPTANCE",
        f"    window=[{since.isoformat()}, {until.isoformat()})",
        "    trigger=queued Webull fan-out BUY/open intent",
        "    polarity=paired=N of M; PASS requires every exercised Webull leg to share "
        "one deterministic identity with a Schwab primary",
        "    scope=identity pairing only; no duplicate, slot-consumption, or fill verdict",
        "    historical_comparison=16 of 53 filled Webull legs had a usable arm join "
        "before this increment; comparison only, not a pass threshold",
    ]
    if since >= until:
        lines.append("    => COULD_NOT_TELL. window start is not before end")
        return Report(COULD_NOT_TELL, "COULD_NOT_TELL", tuple(lines))
    if not records:
        lines.append("    paired=0 of 0")
        lines.append("    => UNEXERCISED. denominator is 0; no fan-out opportunity was queued")
        return Report(UNEXERCISED, "UNEXERCISED", tuple(lines))

    errors = [
        f"intent {record.intent_id} ({record.symbol}): {error}"
        for record in records
        if (error := _identity_error(record)) is not None
    ]
    paired = len(records) - len(errors)
    lines.append(f"    paired={paired} of {len(records)}")
    if errors:
        lines.extend(f"    identity_error={error}" for error in errors[:12])
        if len(errors) > 12:
            lines.append(f"    identity_error=... {len(errors) - 12} more")
        lines.append("    => FAIL. at least one queued pair lacks the exact shared identity")
        return Report(FAIL, "FAIL", tuple(lines))

    lines.append(
        "    => PASS. [V2-CROSS-VENUE-IDENTITY-PAIRED] every exercised pair shares the key"
    )
    return Report(PASS, "PASS", tuple(lines))


def run_report(
    *,
    since: datetime,
    until: datetime,
    query: Callable[[datetime, datetime], list[PairRecord]] = query_database,
) -> Report:
    if since >= until:
        return evaluate([], since=since, until=until)
    try:
        records = query(since, until)
    except (EvidenceFailure, ValueError) as exc:
        return Report(
            COULD_NOT_TELL,
            "COULD_NOT_TELL",
            (
                "### CROSS-VENUE FANOUT IDENTITY ACCEPTANCE",
                f"    window=[{since.isoformat()}, {until.isoformat()})",
                f"    => COULD_NOT_TELL. read-only evidence failed: {exc}",
            ),
        )
    return evaluate(records, since=since, until=until)


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
