#!/usr/bin/env python3
"""Report rejected intent classes from their durable refusal provenance.

The stable intent status remains plain ``rejected``. Ownership is read only from
``trade_intents.payload.refusal_origin``; missing historical provenance is
``could_not_tell`` and is never inferred from reason text or broker events.
"""
from __future__ import annotations

import argparse
from collections.abc import Iterable
from datetime import date, datetime, timedelta
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from zoneinfo import ZoneInfo


ET = ZoneInfo("America/New_York")
ENV = "/etc/project-mai-tai/project-mai-tai.env"
STATE = Path("/home/trader/reject_watch/state.json")
REAL = ("live:schwab_1m_v2", "live:orb")
REFUSAL_ORIGINS = frozenset(
    {"client_abort", "broker_reject", "skipped_before_submit", "could_not_tell"}
)

QUERY = """
    SELECT ba.name,
           COALESCE(ti.payload::jsonb->>'refusal_origin', 'could_not_tell') AS origin,
           COALESCE(
               ti.payload::jsonb->>'refusal_code',
               '(unlabelled historical refusal)'
           ) AS refusal_code,
           ti.intent_type,
           (ti.created_at AT TIME ZONE 'America/New_York')::date AS d,
           count(*)
    FROM trade_intents ti
    JOIN broker_accounts ba ON ba.id = ti.broker_account_id
    WHERE ti.status = 'rejected'
      AND (ti.created_at AT TIME ZONE 'America/New_York')::date >= %s
    GROUP BY 1,2,3,4,5
"""


def dsn() -> tuple[str, str]:
    result = subprocess.run(
        ["grep", "-E", "^MAI_TAI_DATABASE_URL=", ENV],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    url = result.split("=", 1)[1]
    match = re.match(r"^[^:]+://([^:]+):([^@]+)@", url)
    if match is None:
        raise RuntimeError("database URL does not contain local credentials")
    return f"dbname=project_mai_tai user={match.group(1)} host=localhost", match.group(2)


def query(since: date) -> list[tuple[object, ...]]:
    import psycopg

    connection_string, password = dsn()
    with (
        psycopg.connect(connection_string, password=password) as connection,
        connection.cursor() as cursor,
    ):
        cursor.execute(QUERY, (since,))
        return cursor.fetchall()


def klass(reason: str) -> str:
    """Collapse volatile identifiers while retaining the raw durable refusal code."""

    return re.sub(r"[0-9]+", "N", (reason or "(null)").strip())[:70]


def normalized_origin(origin: object) -> str:
    value = str(origin or "").strip().lower()
    return value if value in REFUSAL_ORIGINS else "could_not_tell"


def aggregate(rows: Iterable[tuple[object, ...]]) -> dict[tuple[str, str, str, str], dict]:
    result: dict[tuple[str, str, str, str], dict] = {}
    for account, origin, reason, kind, session_day, count in rows:
        key = (
            str(account),
            normalized_origin(origin),
            klass(str(reason)),
            str(kind or "?"),
        )
        item = result.setdefault(key, {"n": 0, "days": set()})
        item["n"] += int(count)
        item["days"].add(session_day)
    return result


def _streak(days: list[date], today: date) -> int:
    if not days or days[-1] != today:
        return 0
    streak = 1
    for index in range(len(days) - 1, 0, -1):
        if (days[index] - days[index - 1]).days != 1:
            break
        streak += 1
    return streak


def render(
    rows: Iterable[tuple[object, ...]],
    *,
    today: date,
    days: int,
    known: set[str],
) -> tuple[str, list[str], set[str]]:
    grouped = aggregate(rows)
    output = [
        "=" * 78,
        f"A7 INTENT-REFUSAL ALARM | {today} ET | window {days} days",
        "Counts: trade_intents.status=rejected; origin: trade_intents.payload.refusal_origin.",
        "Historical missing origin is could_not_tell; no reason-text inference and no backfill.",
        "=" * 78,
    ]
    pages: list[str] = []
    next_known = set(known)

    for scope, accounts, paged in (("REAL MONEY", REAL, True), ("PAPER / SIM", None, False)):
        selected = {
            key: value
            for key, value in grouped.items()
            if ((key[0] in accounts) if accounts else (key[0] not in REAL))
        }
        output.append(f"\n--- {scope} {'(PAGES)' if paged else '(reported, never pages)'} ---")
        if not selected:
            output.append("   no refused intents in the window")
            continue
        for key, item in sorted(selected.items(), key=lambda pair: -pair[1]["n"]):
            account, origin, refusal_class, kind = key
            observed_days = sorted(item["days"])
            consecutive = _streak(observed_days, today)
            current_key = "|".join(key)
            legacy_key = "|".join((account, refusal_class, kind))
            is_new = current_key not in known and legacy_key not in known
            flags = []
            if paged and is_new:
                flags.append("NEW-CLASS")
            if paged and consecutive >= 2:
                flags.append(f"{consecutive}d-STREAK")
            flag_text = " ".join(flags)
            output.append(
                f"   [{origin}] {account:<18} {kind:<6} n={item['n']:<5} "
                f"days={len(observed_days):<3} last={observed_days[-1]} {flag_text}".rstrip()
            )
            output.append(f"        {refusal_class}")
            if flags:
                pages.append(
                    f"{origin} {account} {kind} n={item['n']} "
                    f"({','.join(flags)}) - {refusal_class[:60]}"
                )
            next_known.add(current_key)

    output.extend(
        (
            "\n--- WHAT THIS CANNOT SEE ---",
            "   - historical rejected intents without refusal_origin (reported could_not_tell)",
            "   - a refusal that never produced or updated a trade_intents row",
            "   - whether a refusal cost a fill, position, or later successful retry",
            "   - accounts not represented in broker_accounts",
            f"\nVERDICT reject_alarm pages={len(pages)} classes={len(grouped)}",
        )
    )
    output.extend(f"PAGE {page}" for page in pages)
    return "\n".join(output), pages, next_known


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=10)
    parser.add_argument("--selftest", action="store_true")
    parser.add_argument("--state", type=Path, default=STATE)
    args = parser.parse_args()
    if args.days <= 0:
        parser.error("--days must be positive")

    today = datetime.now(ET).date()
    try:
        state = json.loads(args.state.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        state = {"known": []}
    report, pages, next_known = render(
        query(today - timedelta(days=args.days)),
        today=today,
        days=args.days,
        known=set(state.get("known", [])),
    )
    if args.selftest:
        pages.append("SELFTEST forced page")
        report += "\nPAGE SELFTEST forced page"
    print(report)
    os.makedirs(args.state.parent, exist_ok=True)
    args.state.write_text(json.dumps({"known": sorted(next_known)}), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
