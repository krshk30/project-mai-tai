"""Versioned session-history admission for an off-callback line rebuild.

A successful database read is not coverage evidence: the provider must attest its full query
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
    prefix_complete: bool = False


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


@dataclass(frozen=True)
class SessionLineSnapshot:
    indicator: tuple[tuple[str, object], ...]
    confirmation: tuple[tuple[int, str], ...]
    signal: tuple[tuple[str, object], ...]
    reset_after_ms: int


def build_session_line(
    request: RebuildInput, period: int, factor: float, reset_after_ms: int = 0,
) -> SessionLineSnapshot:
    """Run production mathematics on private state, retaining sparse-bar safeguards."""
    from project_mai_tai.settings import Settings
    from project_mai_tai.strategy_core.schwab_1m_v2 import (
        OHLCVBar, SchwabV2Strategy, SymbolState,
    )

    engine = SchwabV2Strategy(Settings(
        strategy_schwab_1m_v2_atr_flip_period=period,
        strategy_schwab_1m_v2_atr_flip_factor=factor,
        strategy_schwab_1m_v2_atr_flip_probe_symbols="",
        strategy_schwab_1m_v2_line_chart_restoration_enabled=False,
    ))
    state = SymbolState(request.symbol)
    confirmation = []
    signal = None
    for bar in request.bars:
        if bar.timestamp_ms <= reset_after_ms:
            continue
        signal = engine._update_atr_state(
            state, OHLCVBar(bar.timestamp_ms, bar.open, bar.high, bar.low, bar.close, bar.volume),
            observation_phase="replay", state_only=True,
        )
        confirmation.append((bar.timestamp_ms, str(state.atr_state or "unknown")))
    snapshot = engine._atr_indicator_snapshot(state)
    snapshot["atr_hl"] = tuple(state.atr_hl)
    snapshot["atr_tr_seed"] = tuple(state.atr_tr_seed)
    previous = state.atr_prev_bar
    snapshot["atr_prev_bar"] = (
        HistoryBar(request.symbol, previous.open, previous.high, previous.low,
                   previous.close, previous.volume, previous.timestamp_ms)
        if previous is not None else None
    )
    return SessionLineSnapshot(
        tuple(snapshot.items()), tuple(confirmation), tuple((signal or {}).items()), reset_after_ms,
    )


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

    def invalidate_coverage(self) -> None:
        self._coverage = None
        self.revision += 1
        self.incomplete_reason = "coverage_unproven"

    def prepare(self) -> RebuildInput | None:
        proof = self._coverage
        if (proof is None or not proof.complete or proof.source != "schwab_rest_full_session"
                or proof.start_ms != self.anchor_ms
                or proof.end_ms < self.current_bar_ms + 60_000):
            self.incomplete_reason = "coverage_unproven"
            return None
        ids = proof.closed_ids
        if (not ids or ids != tuple(sorted(set(ids)))
                or ids[0] < self.anchor_ms or ids[-1] != self.current_bar_ms):
            self.incomplete_reason = "coverage_ids_unproven"
            return None
        if ids[0] != self.anchor_ms and not proof.prefix_complete:
            self.incomplete_reason = "session_prefix_unproven"
            return None
        if set(ids) != set(self._bars):
            self.incomplete_reason = "history_missing_or_conflicting"
            return None
        bars = tuple(self._bars[ts] for ts in ids)
        if proof.bars_sha256 != history_fingerprint(bars):
            self.incomplete_reason = "source_values_unproven"
            return None
        # Until the separate two-input Pause lane proves silence, an omitted
        # minute is unknown coverage, even in a successful provider response.
        if any(right.timestamp_ms - left.timestamp_ms > 90_000
               for left, right in zip(bars, bars[1:])):
            self.incomplete_reason = "interior_gap_unproven"
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
