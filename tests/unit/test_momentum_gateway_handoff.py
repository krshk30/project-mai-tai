from __future__ import annotations

import asyncio
import json

import pytest

from project_mai_tai.momentum_gateway_handoff import (
    BoundedPaperHandoff,
    parse_massive_trade_frame,
)


def test_parser_keeps_only_massive_trade_events() -> None:
    rows = parse_massive_trade_frame(
        json.dumps(
            [
                {"ev": "status", "message": "connected"},
                {"ev": "T", "sym": "AEMD", "t": 1},
                {"ev": "T.AEMD", "sym": "AEMD", "t": 2},
            ]
        )
    )

    assert [row["t"] for row in rows] == [1, 2]


def test_malformed_frame_is_counted_and_not_forwarded() -> None:
    handoff = BoundedPaperHandoff(capacity=2)

    assert handoff.offer("not-json", received_ns=1) is False

    assert handoff.size == 0
    assert handoff.counters.input_frames == 1
    assert handoff.counters.forwarded_frames == 0
    assert handoff.counters.parse_failures == 1


def test_non_trade_frame_is_ignored_without_becoming_a_parse_failure() -> None:
    handoff = BoundedPaperHandoff(capacity=2)

    assert handoff.offer({"ev": "status", "message": "connected"}, received_ns=1)

    assert handoff.size == 0
    assert handoff.counters.input_frames == 1
    assert handoff.counters.forwarded_frames == 0
    assert handoff.counters.parse_failures == 0


def test_dead_consumer_drops_oldest_without_awaiting() -> None:
    handoff = BoundedPaperHandoff(capacity=2)
    for sequence in (1, 2, 3):
        assert handoff.offer({"ev": "T", "sequence": sequence}, received_ns=sequence)

    assert handoff.size == 2
    assert handoff.counters.dropped_frames == 1
    assert handoff.get_nowait().trades[0]["sequence"] == 2
    assert handoff.get_nowait().trades[0]["sequence"] == 3


@pytest.mark.asyncio
async def test_active_consumer_receives_frame() -> None:
    handoff = BoundedPaperHandoff(capacity=1)
    handoff.offer({"ev": "T", "sym": "AEMD"}, received_ns=10)

    frame = await asyncio.wait_for(handoff.get(), timeout=0.1)

    assert frame.received_ns == 10
    assert frame.trades == ({"ev": "T", "sym": "AEMD"},)
