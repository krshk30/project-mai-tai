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
    spanning_pairs: tuple[tuple[int, int], ...] = ()


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
    """Span only the pairs authorized by an immutable full-session admission."""
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
    previous_ms = 0
    for bar in request.bars:
        if bar.timestamp_ms <= reset_after_ms:
            continue
        signal = engine._update_atr_state(
            state, OHLCVBar(bar.timestamp_ms, bar.open, bar.high, bar.low, bar.close, bar.volume),
            observation_phase="replay", state_only=True,
            span_gap=(previous_ms, bar.timestamp_ms) in request.spanning_pairs,
        )
        previous_ms = bar.timestamp_ms
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
        self._trade_minutes: set[int] = set()
        self._traded_pairs: set[tuple[int, int]] = set()
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

    def observe_trade(self, timestamp_ms: int) -> None:
        minute = timestamp_ms // 60_000 * 60_000
        if self.anchor_ms <= minute < self.anchor_ms + 16 * 3_600_000:
            if minute not in self._trade_minutes:
                self._trade_minutes.add(minute)
                if any(left + 60_000 <= minute < right for left, right in self.gap_pairs()):
                    self.revision += 1
                    self.incomplete_reason = "trade_evidence_changed"

    def mark_traded_pair(self, pair: tuple[int, int]) -> None:
        if pair not in self._traded_pairs:
            self._traded_pairs.add(pair)
            self.revision += 1
            self.incomplete_reason = "trade_evidence_changed"

    def gap_pairs(self) -> tuple[tuple[int, int], ...]:
        ids = sorted(self._bars)
        return tuple((left, right) for left, right in zip(ids, ids[1:])
                     if right - left > 90_000)

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
        pairs = self.gap_pairs()
        # R6 is a complete-provider sparse series, not permission to span a
        # known outage. Positive tape evidence requires the missing bars.
        if any(pair in self._traded_pairs or any(left + 60_000 <= minute < right
               for minute in self._trade_minutes) for pair in pairs for left, right in (pair,)):
            self.incomplete_reason = "traded_gap_unrecovered"
            return None
        return RebuildInput(
            self.symbol, self.epoch, self.revision, self.anchor_ms,
            self.current_bar_ms, bars, pairs,
        )

    async def rebuild(self, builder: Callable[[RebuildInput], Snapshot]) -> RebuildResult[Snapshot] | None:
        request = self.prepare()
        if request is None:
            return None
        try:
            snapshot = await asyncio.wait_for(asyncio.to_thread(builder, request), 3.0)
        except TimeoutError:
            self.incomplete_reason = "rebuild_timeout"
            return None
        return RebuildResult(request, snapshot)

    def admit(self, result: RebuildResult[Snapshot]) -> Snapshot | None:
        # Re-read every identity/coverage fence after the worker returns. No
        # trading state, consumed slot or historical flip is replayed here.
        if result.request != self.prepare():
            self.incomplete_reason = "stale_rebuild"
            return None
        self.incomplete_reason = ""
        return result.snapshot
