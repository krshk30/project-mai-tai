"""Versioned session-history admission for an off-callback line rebuild.

This primitive is deliberately not wired to the live service yet. A successful
database read is not coverage evidence: the provider must attest its full query
window and candle IDs before a reconstruction can be admitted.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
from dataclasses import dataclass
from typing import Callable, Generic, TypeVar

from project_mai_tai.market_data.schwab_v2_rest_client import ChartBar

Snapshot = TypeVar("Snapshot")


@dataclass(frozen=True)
class SessionCoverage:
    source: str
    start_ms: int
    end_ms: int
    closed_ids: tuple[int, ...]
    complete: bool
    bars_sha256: str


@dataclass(frozen=True)
class HistoryBar:
    symbol: str
    open: float
    high: float
    low: float
    close: float
    volume: int
    timestamp_ms: int


def history_fingerprint(bars) -> str:
    """Attest candle values as well as IDs; the adapter must use its own response."""
    rows = [(bar.symbol.upper(), bar.timestamp_ms, bar.open, bar.high, bar.low,
             bar.close, bar.volume) for bar in bars]
    return hashlib.sha256(json.dumps(rows, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


@dataclass(frozen=True)
class RebuildInput:
    symbol: str
    epoch: int
    revision: int
    anchor_ms: int
    current_bar_ms: int
    bars: tuple[HistoryBar, ...]


@dataclass(frozen=True)
class RebuildResult(Generic[Snapshot]):
    request: RebuildInput
    snapshot: Snapshot


class SessionLineRestoration:
    """Only the event-loop owner may update/admit; workers receive frozen input."""

    def __init__(self, symbol: str, anchor_ms: int, epoch: int):
        self.symbol = symbol.upper()
        self.anchor_ms = anchor_ms
        self.epoch = epoch
        self.revision = 0
        self.current_bar_ms = 0
        self._bars: dict[int, HistoryBar] = {}
        self._coverage: SessionCoverage | None = None
        self.incomplete_reason = "coverage_unproven"

    def observe(self, bar: ChartBar) -> None:
        if bar.symbol.upper() != self.symbol:
            raise ValueError("foreign symbol in session rebuild")
        if bar.timestamp_ms < self.anchor_ms:
            return
        if (bar.timestamp_ms >= self.anchor_ms + 86_400_000 or bar.timestamp_ms % 60_000
                or not all(math.isfinite(v) and v > 0 for v in (bar.open, bar.high, bar.low, bar.close))
                or not bar.low <= min(bar.open, bar.close) <= max(bar.open, bar.close) <= bar.high
                or bar.volume < 0):
            raise ValueError("invalid or foreign-session bar in rebuild")
        frozen = HistoryBar(
            self.symbol, bar.open, bar.high, bar.low, bar.close, bar.volume, bar.timestamp_ms,
        )
        if self._bars.get(bar.timestamp_ms) != frozen:
            self._bars[bar.timestamp_ms] = frozen
            self.revision += 1
            self.incomplete_reason = "history_changed"
        self.current_bar_ms = max(self.current_bar_ms, bar.timestamp_ms)

    def attest(self, proof: SessionCoverage) -> None:
        if proof != self._coverage:
            self.revision += 1
            self._coverage = proof
            self.incomplete_reason = "coverage_changed"

    def prepare(self) -> RebuildInput | None:
        proof = self._coverage
        if (proof is None or not proof.complete or proof.source != "schwab_rest_full_session"
                or proof.start_ms > self.anchor_ms
                or proof.end_ms < self.current_bar_ms + 60_000):
            self.incomplete_reason = "coverage_unproven"
            return None
        ids = proof.closed_ids
        if (not ids or ids != tuple(sorted(set(ids)))
                or ids[0] < self.anchor_ms or ids[-1] != self.current_bar_ms):
            self.incomplete_reason = "coverage_ids_unproven"
            return None
        if set(ids) != set(self._bars):
            self.incomplete_reason = "history_missing_or_conflicting"
            return None
        bars = tuple(self._bars[ts] for ts in ids)
        if proof.bars_sha256 != history_fingerprint(bars):
            self.incomplete_reason = "source_values_unproven"
            return None
        return RebuildInput(
            self.symbol, self.epoch, self.revision, self.anchor_ms,
            self.current_bar_ms, bars,
        )

    async def rebuild(self, builder: Callable[[RebuildInput], Snapshot]) -> RebuildResult[Snapshot] | None:
        request = self.prepare()
        if request is None:
            return None
        return RebuildResult(request, await asyncio.to_thread(builder, request))

    def admit(self, result: RebuildResult[Snapshot]) -> Snapshot | None:
        # Re-read every identity/coverage fence after the worker returns. No
        # trading state, consumed slot or historical flip is replayed here.
        if result.request != self.prepare():
            self.incomplete_reason = "stale_rebuild"
            return None
        self.incomplete_reason = ""
        return result.snapshot
