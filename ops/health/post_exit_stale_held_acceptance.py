#!/usr/bin/env python3
"""Read-only C3 census for Webull post-exit stale-held sell refusals.

This report grades the durable refusal population; it does not decide whether a sell is safe and
does not call a broker. A zero with zero post-exit episodes is UNEXERCISED, never "fixed".

Exit codes:
  0  MEASURED       at least one refusal exists and all count buckets reconcile
  2  COULD_NOT_TELL query output or count buckets are unreadable
  3  UNEXERCISED    zero refused sells across zero episodes
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import UTC, datetime
import subprocess


MEASURED = 0
COULD_NOT_TELL = 2
UNEXERCISED = 3

# Baseline requested by the release brief. These are comparison points, not thresholds: a thin-day
# zero with no episode population carries no evidence about the fix.
BASELINE = "2026-08-27=2 2026-08-26=49 2026-08-25=25 2026-08-24=37"


@dataclass(frozen=True)
class DayCounts:
    session_date_et: str
    refused_sells: int
    classified_post_exit: int
    post_exit_episodes: int
    no_preceding_sell_fill: int


@dataclass(frozen=True)
class Verdict:
    exit_code: int
    name: str
    lines: tuple[str, ...]


class EvidenceFailure(RuntimeError):
    """The immutable read-only query did not produce trustworthy counts."""


SQL = r"""
BEGIN READ ONLY;
WITH refused AS (
    SELECT
        e.id,
        e.event_at,
        bo.broker_account_id,
        bo.symbol,
        (e.event_at AT TIME ZONE 'America/New_York')::date AS session_date_et
    FROM broker_order_events e
    JOIN broker_orders bo ON bo.id = e.order_id
    JOIN broker_accounts ba ON ba.id = bo.broker_account_id
    WHERE ba.provider = 'webull'
      AND bo.side = 'sell'
      AND e.event_type = 'rejected'
      AND upper(coalesce(e.payload::jsonb->>'reason', '')) LIKE
          '%NEW_NO_POSITION%CAN_NOT_SELL_SHORT%'
      AND e.event_at >= :'window_since'::timestamptz
      AND e.event_at < :'window_until'::timestamptz
), classified AS (
    SELECT
        r.*,
        prior_fill.id AS exit_fill_id
    FROM refused r
    LEFT JOIN LATERAL (
        SELECT f.id
        FROM fills f
        WHERE f.broker_account_id = r.broker_account_id
          AND f.symbol = r.symbol
          AND f.side = 'sell'
          AND f.filled_at <= r.event_at
          AND (f.filled_at AT TIME ZONE 'America/New_York')::date = r.session_date_et
        ORDER BY f.filled_at DESC, f.id DESC
        LIMIT 1
    ) prior_fill ON TRUE
)
SELECT concat_ws('|',
    session_date_et::text,
    count(*),
    count(*) FILTER (WHERE exit_fill_id IS NOT NULL),
    count(DISTINCT exit_fill_id) FILTER (WHERE exit_fill_id IS NOT NULL),
    count(*) FILTER (WHERE exit_fill_id IS NULL)
)
FROM classified
GROUP BY session_date_et
ORDER BY session_date_et;
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


def parse_rows(raw: str) -> tuple[DayCounts, ...]:
    rows: list[DayCounts] = []
    for line in (item.strip() for item in raw.splitlines() if item.strip()):
        fields = line.split("|")
        if len(fields) != 5:
            raise EvidenceFailure(f"expected 5 fields, received {len(fields)}")
        try:
            counts = [int(value) for value in fields[1:]]
        except ValueError as exc:
            raise EvidenceFailure("one or more count fields are not integers") from exc
        if any(value < 0 for value in counts):
            raise EvidenceFailure("count fields must not be negative")
        rows.append(DayCounts(fields[0], *counts))
    return tuple(rows)


def query_database(since: datetime, until: datetime) -> tuple[DayCounts, ...]:
    command = [
        "sudo", "-n", "-u", "postgres", "psql", "-X", "-qAt",
        "-v", "ON_ERROR_STOP=1",
        "-v", f"window_since={since.isoformat()}",
        "-v", f"window_until={until.isoformat()}",
        "-d", "project_mai_tai", "-f", "-",
    ]
    try:
        result = subprocess.run(
            command,
            input=SQL,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise EvidenceFailure(f"could not execute the read-only query: {exc}") from exc
    if result.returncode != 0:
        raise EvidenceFailure(result.stderr.strip() or "psql exited non-zero without an error")
    if result.stderr.strip():
        raise EvidenceFailure(f"psql wrote unexpected stderr: {result.stderr.strip()}")
    return parse_rows(result.stdout)


def evaluate(rows: tuple[DayCounts, ...]) -> Verdict:
    for row in rows:
        if row.classified_post_exit + row.no_preceding_sell_fill != row.refused_sells:
            return Verdict(
                COULD_NOT_TELL,
                "COULD_NOT_TELL",
                (
                    f"date={row.session_date_et} refusal buckets do not equal denominator "
                    f"{row.refused_sells}",
                ),
            )
        if row.post_exit_episodes > row.classified_post_exit:
            return Verdict(
                COULD_NOT_TELL,
                "COULD_NOT_TELL",
                (f"date={row.session_date_et} episodes exceed classified refusals",),
            )

    refused_total = sum(row.refused_sells for row in rows)
    episode_total = sum(row.post_exit_episodes for row in rows)
    header = (
        "[OMS-C3-POST-EXIT-ACCEPTANCE] "
        f"baseline_refused_sells_per_day=({BASELINE}) "
        "trigger=Webull NEW_NO_POSITION...CAN_NOT_SELL_SHORT sell refusal; "
        "polarity=classified_post_exit requires a preceding confirmed SELL fill on the same "
        "account/symbol/day"
    )
    if refused_total == 0 and episode_total == 0:
        return Verdict(
            UNEXERCISED,
            "UNEXERCISED",
            (
                header,
                "refused_sells=0 post_exit_episodes=0 — zero against zero episodes is not proof",
            ),
        )
    lines = [header]
    lines.extend(
        " ".join(
            (
                f"date={row.session_date_et}",
                f"refused_sells={row.refused_sells}",
                f"classified_post_exit={row.classified_post_exit}",
                f"post_exit_episodes={row.post_exit_episodes}",
                f"no_preceding_sell_fill={row.no_preceding_sell_fill}",
            )
        )
        for row in rows
    )
    lines.append(
        f"verdict=MEASURED refused_sells={refused_total} post_exit_episodes={episode_total} — "
        "compare exercised days; a thin-day zero remains UNEXERCISED"
    )
    return Verdict(MEASURED, "MEASURED", tuple(lines))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--since", required=True, help="inclusive ISO instant")
    parser.add_argument("--until", required=True, help="exclusive ISO instant")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        since = parse_instant(args.since, "--since")
        until = parse_instant(args.until, "--until")
        if until <= since:
            raise ValueError("--until must be after --since")
        verdict = evaluate(query_database(since, until))
    except (ValueError, EvidenceFailure) as exc:
        print(f"COULD_NOT_TELL: {exc}")
        return COULD_NOT_TELL
    for line in verdict.lines:
        print(line)
    print(f"verdict={verdict.name}")
    return verdict.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
