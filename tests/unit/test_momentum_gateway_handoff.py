from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import time
from uuid import uuid4

import pytest

from project_mai_tai.momentum_gateway_handoff import (
    BoundedPaperHandoff,
    connect_consumer_socket,
    CrossProcessPaperConsumer,
    drain_handoff_to_socket,
    encode_trade_frame,
    parse_massive_trade_frame,
    ParsedTradeFrame,
    UnixDatagramPaperReceiver,
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


@pytest.mark.asyncio
async def test_production_receiver_preserves_a_raw_trade_frame(tmp_path: Path) -> None:
    del tmp_path
    receiver = UnixDatagramPaperReceiver(f"/tmp/mt-{uuid4().hex}.sock")
    receiver.open()
    producer = connect_consumer_socket(receiver.socket_path)
    try:
        raw = {
            "ev": "T",
            "sym": "AEMD",
            "p": 2.5,
            "s": 40,
            "t": 1_789_555_200_123,
            "y": 1_789_555_100_999,
            "c": [12, 14, 41],
            "i": "wire-id",
            "x": 11,
            "trfi": 501,
        }
        producer.send(
            encode_trade_frame(ParsedTradeFrame(received_ns=time.time_ns(), trades=(raw,)))
        )
        frame = await asyncio.wait_for(receiver.receive(), timeout=1)
        assert frame.trades == (raw,)
    finally:
        producer.close()
        receiver.close()


@pytest.mark.asyncio
async def test_handoff_crosses_into_a_separate_consumer_process(tmp_path: Path) -> None:
    consumer = CrossProcessPaperConsumer(
        mode="active",
        raw_samples_path=tmp_path / "active.jsonl",
    )
    producer_socket = None
    result = None
    try:
        consumer_pid = await asyncio.to_thread(consumer.start)
        producer_socket = connect_consumer_socket(consumer.socket_path)
        handoff = BoundedPaperHandoff(capacity=4)
        handoff.offer({"ev": "T", "sym": "AEMD"}, received_ns=time.time_ns())
        producer_done = asyncio.Event()
        producer_done.set()

        writer = await drain_handoff_to_socket(handoff, producer_socket, producer_done)
        result = await asyncio.to_thread(consumer.stop)

        assert consumer_pid != os.getpid()
        assert result.producer_pid == os.getpid()
        assert result.consumer_pid == consumer_pid
        assert result.consumed_frames == 1
        assert len(result.handoff_lags_ms) == 1
        assert writer.sent_frames == 1
        assert writer.would_block_drops == 0
    finally:
        if producer_socket is not None:
            producer_socket.close()
        consumer.close()


@pytest.mark.asyncio
async def test_dead_cross_process_consumer_raises_socket_drop_counter(tmp_path: Path) -> None:
    consumer = CrossProcessPaperConsumer(
        mode="dead",
        raw_samples_path=tmp_path / "dead.jsonl",
    )
    producer_socket = None
    try:
        await asyncio.to_thread(consumer.start)
        producer_socket = connect_consumer_socket(consumer.socket_path)
        handoff = BoundedPaperHandoff(capacity=2_000)
        blob = "x" * 4_000
        for sequence in range(2_000):
            handoff.offer(
                {"ev": "T", "sequence": sequence, "blob": blob},
                received_ns=time.time_ns(),
            )
        producer_done = asyncio.Event()
        producer_done.set()

        writer = await drain_handoff_to_socket(handoff, producer_socket, producer_done)
        result = await asyncio.to_thread(consumer.stop)

        assert result.consumed_frames == 0
        assert writer.sent_frames > 0
        assert writer.would_block_drops > 0
    finally:
        if producer_socket is not None:
            producer_socket.close()
        consumer.close()
