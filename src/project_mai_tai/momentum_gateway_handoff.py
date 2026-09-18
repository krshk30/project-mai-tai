"""Standalone candidate hand-off for the Momentum gateway throughput study.

This module is intentionally not wired into the market-data gateway.  Step 2
replays raw Massive frames through the same parser and bounded queue that Step 3
would use, without changing a running service.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import json
from typing import Mapping


@dataclass(frozen=True)
class ParsedTradeFrame:
    received_ns: int
    trades: tuple[dict[str, object], ...]


@dataclass(frozen=True)
class HandoffCounters:
    input_frames: int
    forwarded_frames: int
    dropped_frames: int
    parse_failures: int


def parse_massive_trade_frame(
    raw: bytes | str | Mapping[str, object],
) -> tuple[dict[str, object], ...]:
    """Parse one Massive websocket frame and retain its `T.*` trade events."""

    decoded: object
    if isinstance(raw, Mapping):
        decoded = dict(raw)
    else:
        decoded = json.loads(raw)
    rows = decoded if isinstance(decoded, list) else [decoded]
    if not rows or any(not isinstance(row, Mapping) for row in rows):
        raise ValueError("Massive frame must contain an object or a non-empty object list")
    trades: list[dict[str, object]] = []
    for item in rows:
        row = dict(item)
        event = str(row.get("ev", ""))
        if event == "T" or event.startswith("T."):
            trades.append(row)
    return tuple(trades)


class BoundedPaperHandoff:
    """Non-awaiting producer side of a drop-oldest queue.

    ``offer`` contains no ``await`` and uses only ``asyncio.Queue`` non-blocking
    operations.  A dead paper consumer therefore sheds the oldest queued frame
    and increments a counter instead of applying backpressure to the producer.
    """

    def __init__(self, capacity: int) -> None:
        if capacity <= 0:
            raise ValueError("capacity must be positive")
        self._queue: asyncio.Queue[ParsedTradeFrame] = asyncio.Queue(maxsize=capacity)
        self._input_frames = 0
        self._forwarded_frames = 0
        self._dropped_frames = 0
        self._parse_failures = 0

    def offer(
        self,
        raw: bytes | str | Mapping[str, object],
        *,
        received_ns: int,
    ) -> bool:
        self._input_frames += 1
        try:
            trades = parse_massive_trade_frame(raw)
        except (TypeError, ValueError, json.JSONDecodeError):
            self._parse_failures += 1
            return False
        if not trades:
            return True
        frame = ParsedTradeFrame(received_ns=int(received_ns), trades=trades)
        if self._queue.full():
            self._queue.get_nowait()
            self._queue.task_done()
            self._dropped_frames += 1
        self._queue.put_nowait(frame)
        self._forwarded_frames += 1
        return True

    async def get(self) -> ParsedTradeFrame:
        return await self._queue.get()

    def get_nowait(self) -> ParsedTradeFrame:
        return self._queue.get_nowait()

    def task_done(self) -> None:
        self._queue.task_done()

    @property
    def size(self) -> int:
        return self._queue.qsize()

    @property
    def counters(self) -> HandoffCounters:
        return HandoffCounters(
            input_frames=self._input_frames,
            forwarded_frames=self._forwarded_frames,
            dropped_frames=self._dropped_frames,
            parse_failures=self._parse_failures,
        )
