#!/usr/bin/env python3
"""Reconstruct live exit-rejection episodes without counting client aborts as venue refusals.

This is a read-only after-close instrument. It deliberately reports both raw reject rows and
logical position episodes. A row is bound to an episode only through positive evidence:

1. the latest managed-position UUID already open when the rejection occurred;
2. a durable fan-out slot id carried by the order; or
3. an explicit UNBOUND order fallback, which makes the report COULD_NOT_TELL.

The default cutover is PR #946's merge instant. It is a code-deployment boundary, not proof that
the merged behavior was already running; callers should pass the effective deploy time when that
is the question being measured.

Run only after the market closes:

    python scripts/reconstruct_live_exit_reject_episodes.py \
      --start 2026-09-04T04:00:00-04:00 --end 2026-09-11T20:00:00-04:00 \
      --cutover 2026-09-10T18:55:13-04:00
"""

from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime, time
import os
from typing import Iterable
from zoneinfo import ZoneInfo

import psycopg


ET = ZoneInfo("America/New_York")
DEFAULT_ACCOUNTS = ("live:orb", "live:schwab_1m_v2")
PR_946_MERGED_AT = datetime(2026, 9, 10, 22, 55, 13, tzinfo=UTC)


RAW_REJECT_SQL = """
SELECT
    event.id::text,
    event.order_id::text,
    event.event_at,
    account.name,
    account.provider,
    strategy.code,
    orders.symbol,
    lower(coalesce(nullif(event.event_source, ''), 'unknown')),
    coalesce(
        nullif(event.payload->>'reason', ''),
        nullif(orders.payload->>'reject_reason', ''),
        nullif(intent.reason, ''),
        '<missing reason>'
    ),
    orders.client_order_id,
    position.id::text,
    position.entry_time,
    coalesce(
        nullif(orders.payload->'metadata'->>'confirmation_fanout_slot_id', ''),
        nullif(orders.payload->'metadata'->>'fanout_slot_id', ''),
        nullif(intent.payload->'metadata'->>'confirmation_fanout_slot_id', ''),
        nullif(intent.payload->'metadata'->>'fanout_slot_id', ''),
        ''
    ) AS fanout_slot_id
FROM broker_order_events AS event
JOIN broker_orders AS orders ON orders.id = event.order_id
JOIN broker_accounts AS account ON account.id = orders.broker_account_id
JOIN strategies AS strategy ON strategy.id = orders.strategy_id
LEFT JOIN trade_intents AS intent ON intent.id = orders.intent_id
LEFT JOIN LATERAL (
    SELECT managed.id, managed.entry_time
    FROM oms_managed_positions AS managed
    WHERE managed.strategy_code = strategy.code
      AND managed.broker_account_name = account.name
      AND managed.symbol = orders.symbol
      AND managed.entry_time <= event.event_at
    ORDER BY managed.entry_time DESC, managed.created_at DESC, managed.id DESC
    LIMIT 1
) AS position ON true
WHERE event.event_type = 'rejected'
  AND event.event_at >= %(start)s
  AND event.event_at < %(end)s
  AND orders.side = 'sell'
  AND account.name = ANY(%(accounts)s)
ORDER BY event.event_at, event.id
"""


@dataclass(frozen=True)
class RejectRow:
    event_id: str
    order_id: str
    event_at: datetime
    account: str
    provider: str
    strategy: str
    symbol: str
    event_source: str
    reason: str
    client_order_id: str
    position_id: str
    position_entry_at: datetime | None
    fanout_slot_id: str


@dataclass
class RejectEpisode:
    window: str
    account: str
    provider: str
    strategy: str
    symbol: str
    identity: str
    identity_source: str
    first_at: datetime
    last_at: datetime
    event_ids: set[str] = field(default_factory=set)
    order_ids: set[str] = field(default_factory=set)
    event_sources: Counter[str] = field(default_factory=Counter)
    reasons: Counter[str] = field(default_factory=Counter)

    @property
    def bound(self) -> bool:
        return self.identity_source != "unbound_order"

    @property
    def provenance(self) -> str:
        sources = sorted(self.event_sources)
        if sources == ["broker"]:
            return "venue_refusal"
        if sources == ["client"]:
            return "client_abort"
        if sources == ["unknown"]:
            return "unknown"
        return "mixed:" + "+".join(sources)


def parse_instant(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise argparse.ArgumentTypeError("timestamp must include a UTC offset")
    return parsed.astimezone(UTC)


def refuse_regular_market_hours(now: datetime | None = None) -> None:
    current = (now or datetime.now(UTC)).astimezone(ET)
    if current.weekday() < 5 and time(9, 30) <= current.time() < time(16, 0):
        raise SystemExit(
            "refusing historical exit query during 09:30-16:00 ET; run after the close"
        )


def normalize_reason(value: str) -> str:
    return " ".join(str(value or "<missing reason>").split()) or "<missing reason>"


def _identity(row: RejectRow) -> tuple[str, str]:
    if row.position_id:
        return f"position:{row.position_id}", "managed_position"
    if row.fanout_slot_id:
        return f"slot:{row.fanout_slot_id}", "fanout_slot"
    # This prevents duplicate event rows for one broker order from inflating the episode count,
    # but it is not positive position ownership evidence and remains explicitly incomplete.
    fallback = row.order_id or row.client_order_id or row.event_id
    return f"unbound:{fallback}", "unbound_order"


def build_episodes(
    rows: Iterable[RejectRow], *, cutover: datetime
) -> list[RejectEpisode]:
    episodes: dict[tuple[str, str, str, str, str], RejectEpisode] = {}
    for row in rows:
        window = "pre_946" if row.event_at < cutover else "post_946"
        identity, identity_source = _identity(row)
        key = (window, row.account, row.strategy, row.symbol, identity)
        episode = episodes.get(key)
        if episode is None:
            episode = RejectEpisode(
                window=window,
                account=row.account,
                provider=row.provider,
                strategy=row.strategy,
                symbol=row.symbol,
                identity=identity,
                identity_source=identity_source,
                first_at=row.event_at,
                last_at=row.event_at,
            )
            episodes[key] = episode
        episode.first_at = min(episode.first_at, row.event_at)
        episode.last_at = max(episode.last_at, row.event_at)
        episode.event_ids.add(row.event_id)
        episode.order_ids.add(row.order_id)
        episode.event_sources[row.event_source or "unknown"] += 1
        episode.reasons[normalize_reason(row.reason)] += 1
    return sorted(
        episodes.values(),
        key=lambda item: (item.window, item.account, item.first_at, item.symbol, item.identity),
    )


def _print_account_summary(episodes: list[RejectEpisode]) -> None:
    grouped: dict[tuple[str, str], list[RejectEpisode]] = {}
    for episode in episodes:
        grouped.setdefault((episode.window, episode.account), []).append(episode)

    print("\nACCOUNT / WINDOW (logical episodes; raw rows remain separate)")
    print("window    account                  episodes bound unbound venue client unknown raw_events orders")
    for (window, account), items in sorted(grouped.items()):
        raw_events = sum(len(item.event_ids) for item in items)
        orders = len({order for item in items for order in item.order_ids})
        classes = Counter(item.provenance for item in items)
        mixed_or_unknown = sum(
            count for name, count in classes.items() if name == "unknown" or name.startswith("mixed:")
        )
        print(
            f"{window:9} {account:24} {len(items):8d} "
            f"{sum(item.bound for item in items):5d} {sum(not item.bound for item in items):7d} "
            f"{classes['venue_refusal']:5d} {classes['client_abort']:6d} "
            f"{mixed_or_unknown:7d} {raw_events:10d} {orders:6d}"
        )


def _print_reason_summary(episodes: list[RejectEpisode]) -> None:
    summary: Counter[tuple[str, str, str, str]] = Counter()
    raw: Counter[tuple[str, str, str, str]] = Counter()
    for episode in episodes:
        for reason, event_count in episode.reasons.items():
            key = (episode.window, episode.account, episode.provenance, reason)
            summary[key] += 1
            raw[key] += event_count

    print("\nREASON MEMBERSHIP (one episode can appear under multiple reasons)")
    print("window    account                  provenance       episodes raw_events reason")
    for key in sorted(summary):
        window, account, provenance, reason = key
        print(
            f"{window:9} {account:24} {provenance:16} "
            f"{summary[key]:8d} {raw[key]:10d} {reason}"
        )


def _print_episode_detail(episodes: list[RejectEpisode]) -> None:
    print("\nEPISODES (ET)")
    for episode in episodes:
        reasons = " | ".join(
            f"{reason} x{count}" for reason, count in sorted(episode.reasons.items())
        )
        print(
            f"{episode.window} {episode.account} {episode.symbol} "
            f"{episode.first_at.astimezone(ET):%Y-%m-%d %H:%M:%S %Z}.."
            f"{episode.last_at.astimezone(ET):%H:%M:%S %Z} "
            f"identity={episode.identity_source}:{episode.identity} "
            f"provenance={episode.provenance} raw_events={len(episode.event_ids)} "
            f"orders={len(episode.order_ids)} reasons={reasons}"
        )


def rows_from_query(records: Iterable[tuple[object, ...]]) -> list[RejectRow]:
    return [
        RejectRow(
            event_id=str(record[0] or ""),
            order_id=str(record[1] or ""),
            event_at=record[2],  # type: ignore[arg-type]
            account=str(record[3] or ""),
            provider=str(record[4] or ""),
            strategy=str(record[5] or ""),
            symbol=str(record[6] or "").upper(),
            event_source=str(record[7] or "unknown").lower(),
            reason=str(record[8] or "<missing reason>"),
            client_order_id=str(record[9] or ""),
            position_id=str(record[10] or ""),
            position_entry_at=record[11],  # type: ignore[arg-type]
            fanout_slot_id=str(record[12] or ""),
        )
        for record in records
    ]


def _dsn(value: str | None) -> str:
    raw = value or os.environ.get("MAI_TAI_DATABASE_URL", "")
    if not raw:
        raise SystemExit("no DSN: pass --dsn or set MAI_TAI_DATABASE_URL")
    return raw.replace("postgresql+psycopg://", "postgresql://")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", required=True, type=parse_instant)
    parser.add_argument("--end", required=True, type=parse_instant)
    parser.add_argument("--cutover", type=parse_instant, default=PR_946_MERGED_AT)
    parser.add_argument("--account", action="append", dest="accounts")
    parser.add_argument("--dsn")
    parser.add_argument(
        "--allow-incomplete",
        action="store_true",
        help="return zero despite unknown provenance or unbound episodes",
    )
    args = parser.parse_args()
    if args.start >= args.end:
        parser.error("--start must be before --end")
    refuse_regular_market_hours()

    accounts = tuple(args.accounts or DEFAULT_ACCOUNTS)
    with psycopg.connect(_dsn(args.dsn)) as connection, connection.cursor() as cursor:
        cursor.execute(
            RAW_REJECT_SQL,
            {
                "start": args.start,
                "end": args.end,
                "accounts": list(accounts),
            },
        )
        rows = rows_from_query(cursor.fetchall())

    episodes = build_episodes(rows, cutover=args.cutover)
    print(
        "LIVE EXIT REJECT RECONSTRUCTION "
        f"start={args.start.astimezone(ET).isoformat()} "
        f"end={args.end.astimezone(ET).isoformat()} "
        f"cutover={args.cutover.astimezone(ET).isoformat()} accounts={','.join(accounts)}"
    )
    print(
        f"DENOMINATORS raw_reject_events={len(rows)} "
        f"broker_orders={len({row.order_id for row in rows})} "
        f"logical_episodes={len(episodes)}"
    )
    _print_account_summary(episodes)
    _print_reason_summary(episodes)
    _print_episode_detail(episodes)

    incomplete = [episode for episode in episodes if not episode.bound]
    unknown_events = [row for row in rows if row.event_source not in {"broker", "client"}]
    status = "COULD_NOT_TELL" if incomplete or unknown_events else "COMPLETE"
    print(
        f"\nSTATUS={status} unbound_episodes={len(incomplete)}/{len(episodes)} "
        f"unknown_source_events={len(unknown_events)}/{len(rows)}"
    )
    if status != "COMPLETE" and not args.allow_incomplete:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
