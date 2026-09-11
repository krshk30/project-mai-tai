from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
import sys

import pytest


SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
sys.path.insert(0, str(SCRIPTS))

from reconstruct_live_exit_reject_episodes import (  # noqa: E402
    PR_946_MERGED_AT,
    RAW_REJECT_SQL,
    RejectRow,
    build_episodes,
    normalize_reason,
    parse_instant,
    refuse_regular_market_hours,
)


def _row(
    *,
    event_id: str,
    at: datetime,
    source: str = "broker",
    order_id: str = "order-1",
    position_id: str = "position-1",
    fanout_slot_id: str = "",
    reason: str = " venue refused ",
    account: str = "live:orb",
) -> RejectRow:
    return RejectRow(
        event_id=event_id,
        order_id=order_id,
        event_at=at,
        account=account,
        provider="webull" if account == "live:orb" else "schwab",
        strategy="schwab_1m_v2",
        symbol="DBGI",
        event_source=source,
        reason=reason,
        client_order_id=f"coid-{order_id}",
        position_id=position_id,
        position_entry_at=at - timedelta(minutes=5) if position_id else None,
        fanout_slot_id=fanout_slot_id,
    )


def test_multiple_reject_rows_for_one_position_are_one_episode() -> None:
    rows = [
        _row(event_id="e1", at=PR_946_MERGED_AT - timedelta(minutes=2)),
        _row(
            event_id="e2",
            at=PR_946_MERGED_AT - timedelta(minutes=1),
            order_id="order-2",
            reason="venue refused",
        ),
    ]

    episodes = build_episodes(rows, cutover=PR_946_MERGED_AT)

    assert len(episodes) == 1
    assert episodes[0].identity_source == "managed_position"
    assert episodes[0].provenance == "venue_refusal"
    assert len(episodes[0].event_ids) == 2
    assert len(episodes[0].order_ids) == 2


def test_client_abort_never_counts_as_a_venue_refusal() -> None:
    episode = build_episodes(
        [
            _row(
                event_id="e1",
                at=PR_946_MERGED_AT,
                source="client",
            )
        ],
        cutover=PR_946_MERGED_AT,
    )[0]

    assert episode.provenance == "client_abort"
    assert episode.provenance != "venue_refusal"


def test_unknown_provenance_stays_unknown() -> None:
    episode = build_episodes(
        [_row(event_id="e1", at=PR_946_MERGED_AT, source="unknown")],
        cutover=PR_946_MERGED_AT,
    )[0]

    assert episode.provenance == "unknown"


def test_fanout_slot_is_a_bound_fallback_when_managed_row_is_absent() -> None:
    episode = build_episodes(
        [
            _row(
                event_id="e1",
                at=PR_946_MERGED_AT,
                position_id="",
                fanout_slot_id="slot-123",
            )
        ],
        cutover=PR_946_MERGED_AT,
    )[0]

    assert episode.bound is True
    assert episode.identity_source == "fanout_slot"
    assert episode.identity == "slot:slot-123"


def test_missing_position_and_slot_is_explicitly_unbound() -> None:
    episode = build_episodes(
        [
            _row(
                event_id="e1",
                at=PR_946_MERGED_AT,
                position_id="",
                fanout_slot_id="",
            )
        ],
        cutover=PR_946_MERGED_AT,
    )[0]

    assert episode.bound is False
    assert episode.identity_source == "unbound_order"


def test_cutover_splits_the_same_position_into_pre_and_post_windows() -> None:
    rows = [
        _row(event_id="e1", at=PR_946_MERGED_AT - timedelta(microseconds=1)),
        _row(event_id="e2", at=PR_946_MERGED_AT),
    ]

    episodes = build_episodes(rows, cutover=PR_946_MERGED_AT)

    assert [episode.window for episode in episodes] == ["post_946", "pre_946"]


def test_account_is_part_of_episode_identity() -> None:
    rows = [
        _row(event_id="e1", at=PR_946_MERGED_AT, account="live:orb"),
        _row(event_id="e2", at=PR_946_MERGED_AT, account="live:schwab_1m_v2"),
    ]

    episodes = build_episodes(rows, cutover=PR_946_MERGED_AT)

    assert len(episodes) == 2
    assert {episode.account for episode in episodes} == {
        "live:orb",
        "live:schwab_1m_v2",
    }


def test_reason_is_normalized_without_destroying_the_text() -> None:
    assert normalize_reason(" rejected\n  because   shares reserved ") == (
        "rejected because shares reserved"
    )


def test_query_is_read_only_and_carries_every_required_split() -> None:
    lowered = RAW_REJECT_SQL.lower()
    assert "select" in lowered
    assert not any(word in lowered for word in ("insert ", "update ", "delete ", "truncate "))
    assert "event.event_source" in RAW_REJECT_SQL
    assert "orders.side = 'sell'" in RAW_REJECT_SQL
    assert "account.name = ANY" in RAW_REJECT_SQL
    assert "oms_managed_positions" in RAW_REJECT_SQL
    assert "confirmation_fanout_slot_id" in RAW_REJECT_SQL


def test_timestamp_requires_an_explicit_offset() -> None:
    with pytest.raises(Exception):
        parse_instant("2026-09-10T18:55:13")
    assert parse_instant("2026-09-10T18:55:13-04:00") == PR_946_MERGED_AT


def test_market_hours_guard_is_non_optional() -> None:
    with pytest.raises(SystemExit, match="refusing historical exit query"):
        refuse_regular_market_hours(datetime(2026, 9, 11, 14, 0, tzinfo=UTC))
    refuse_regular_market_hours(datetime(2026, 9, 11, 20, 1, tzinfo=UTC))
