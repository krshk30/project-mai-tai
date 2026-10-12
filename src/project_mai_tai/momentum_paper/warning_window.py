"""Pure MOMWARN1 components; no subscriptions, ledger selection or cold-low seeding."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Mapping

from project_mai_tai.momentum_paper.engine import MomentumPaperEngine
from project_mai_tai.momentum_paper.models import (
    MOMENTUM_WARNING_STRATEGIES,
    MomentumTapeRecord,
    TradePrint,
)


_WARNING = Decimal("1.10")
_WINDOWS = (30, 60)
_SECOND_NS = 1_000_000_000


@dataclass(frozen=True)
class AggregateObservation:
    symbol: str
    start_ms: int
    end_ms: int
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int
    received_utc_ns: int
    published_utc_ns: int
    gateway_instance_id: str
    generation: int
    sequence: int
    dropped_total: int
    provider_kind: str = "massive_ws_a"

    def __post_init__(self) -> None:
        prices = (self.open, self.high, self.low, self.close)
        if not all(value.is_finite() and value > 0 for value in prices):
            raise ValueError("invalid_aggregate_price")
        if not self.low <= min(self.open, self.close) <= max(self.open, self.close) <= self.high:
            raise ValueError("invalid_aggregate_ohlc")
        if (
            not self.symbol or self.symbol != self.symbol.upper()
            or not self.gateway_instance_id or self.start_ms < 1_000_000_000_000
            or self.end_ms - self.start_ms != 1000
            or self.volume < 0 or self.received_utc_ns <= 0
            or self.published_utc_ns < self.received_utc_ns
            or min(self.generation, self.sequence, self.dropped_total) < 0
            or self.provider_kind != "massive_ws_a"
        ):
            raise ValueError("invalid_aggregate_metadata")

    @property
    def epoch(self) -> tuple[str, int]:
        return self.gateway_instance_id, self.generation

    @classmethod
    def from_payload(cls, payload: Mapping[str, object]) -> AggregateObservation:
        if (
            not isinstance(payload, Mapping) or type(payload.get("version")) is not int
            or payload.get("version") != 1 or payload.get("event_type") != "aggregate"
            or payload.get("provider_kind") != "massive_ws_a"
        ):
            raise ValueError("unsupported_warning_contract")
        integer_fields = (
            "start_ms", "end_ms", "volume", "received_utc_ns", "published_utc_ns",
            "generation", "sequence", "dropped_total",
        )
        if any(type(payload.get(name)) is not int for name in integer_fields):
            raise ValueError("invalid_aggregate_integer")
        return cls(
            symbol=str(payload["symbol"]),
            **{name: payload[name] for name in integer_fields},
            **{name: Decimal(str(payload[name])) for name in ("open", "high", "low", "close")},
            gateway_instance_id=str(payload["gateway_instance_id"]),
            provider_kind=str(payload["provider_kind"]),
        )


@dataclass(frozen=True)
class CoverageEvidence:
    """Explicit caller evidence, NOT inferred from missing bars or a T request."""

    epoch: tuple[str, int]
    start_ms: int
    through_ms: int
    dropped_total: int
    continuous: bool
    lineage: str = "explicit"


class ObservedATransport:
    """Accepted A prefix only, never provider ACK, SIP completeness or raw coverage."""

    def __init__(self) -> None:
        self.epoch: tuple[str, int] | None = None
        self.sequence = -1
        self.drops = 0
        self._start_ns: int | None = None
        self._received_ns = 0
        self._read_ns = 0
        self._checkpoint: tuple[int, int] | None = None
        self._committed_sequence = -1
        self._evidence: CoverageEvidence | None = None
        self.status = "waiting_observed_transport"

    def invalidate(self, reason: str) -> None:
        self._start_ns = None
        self._evidence = None
        self.status = reason

    def observe(self, bar: AggregateObservation, *, observed_ns: int) -> None:
        if not 0 <= observed_ns - bar.received_utc_ns <= _SECOND_NS:
            self.invalidate("transport_event_stale_or_future")
            return
        if observed_ns < bar.published_utc_ns:
            self.invalidate("transport_clock_skew")
            return
        changed_epoch = bar.epoch != self.epoch
        if not changed_epoch and bar.sequence <= self.sequence:
            if bar.sequence < self.sequence:
                self.invalidate("transport_sequence_regressed")
            return
        if not changed_epoch and (
            bar.sequence != self.sequence + 1 or bar.dropped_total != self.drops
            or bar.received_utc_ns < self._received_ns
            or bar.received_utc_ns - self._received_ns > 2 * _SECOND_NS
        ):
            self.invalidate("transport_sequence_drop_or_clock_gap")
        if changed_epoch:
            self.invalidate("transport_epoch_changed")
        self.epoch = bar.epoch
        self.sequence = bar.sequence
        self.drops = bar.dropped_total
        self._received_ns = bar.received_utc_ns
        if self._start_ns is None:
            self._start_ns = bar.received_utc_ns
        self.status = "transport_prefix_pending_read_checkpoint"

    def checkpoint(self, message_id: str, *, observed_ns: int) -> None:
        if self.epoch is None or self._start_ns is None:
            return
        try:
            parts = message_id.split("-")
            if len(parts) != 2 or any(not part.isdigit() for part in parts):
                raise ValueError
            cursor = tuple(int(part) for part in parts)
        except (AttributeError, ValueError):
            self.invalidate("transport_invalid_read_checkpoint")
            return
        if (
            (self._checkpoint is not None and cursor < self._checkpoint)
            or observed_ns < max(self._read_ns, self._received_ns)
        ):
            self.invalidate("transport_read_checkpoint_regressed")
            return
        self._checkpoint = cursor
        if self._read_ns and observed_ns - self._read_ns > 2 * _SECOND_NS:
            self._start_ns = self._received_ns
        self._read_ns = observed_ns
        self._committed_sequence = self.sequence
        self._evidence = CoverageEvidence(
            self.epoch, (self._start_ns + 999_999) // 1_000_000,
            self._received_ns // 1_000_000, self.drops, True, "observed_transport",
        )
        self.status = "observed_transport"

    def evidence(self, *, observed_ns: int) -> CoverageEvidence | None:
        if (
            self._evidence is None or self._committed_sequence != self.sequence
            or not 0 <= observed_ns - self._read_ns <= 2 * _SECOND_NS
            or not 0 <= observed_ns - self._received_ns <= 2 * _SECOND_NS
        ):
            if self._evidence is not None and self._committed_sequence == self.sequence:
                self.status = "transport_progress_or_read_stale"
            return None
        return self._evidence


@dataclass(frozen=True)
class WindowRange:
    seconds: int
    low: Decimal | None
    high: Decimal | None
    fully_known: bool
    boundary_ambiguous: bool


@dataclass(frozen=True)
class WarningEvaluation:
    symbol: str
    observed_ms: int
    windows: tuple[WindowRange, ...]
    warning_windows: tuple[int, ...]
    proof_at_ms: int | None = None

    @property
    def fully_known_cool(self) -> bool:
        return len(self.windows) == 2 and all(
            window.fully_known and window.low is not None and window.high is not None
            and window.high < window.low * _WARNING
            for window in self.windows
        )


class WarningWindow:
    """One-symbol, bounded aggregate history; lows retain bucket bounds only."""

    def __init__(self, symbol: str) -> None:
        self.symbol = symbol.upper()
        self._bars: dict[int, AggregateObservation] = {}
        self._latest: AggregateObservation | None = None
        self._observed_ms = 0
        self._started_ms: int | None = None
        self._discarded_through_ms = 0

    @property
    def retained_buckets(self) -> int:
        return len(self._bars)

    def whole_buckets(
        self, *, observed_ms: int, seconds: int, after_ms: int = 0,
    ) -> tuple[AggregateObservation, ...]:
        return tuple(bar for bar in self._bars.values()
                     if observed_ms - seconds * 1000 <= bar.start_ms
                     and bar.start_ms > after_ms and bar.end_ms <= observed_ms)

    def observe(self, bar: AggregateObservation, *, observed_ns: int) -> bool:
        if bar.symbol != self.symbol:
            raise ValueError("wrong_warning_symbol")
        now_ms = observed_ns // 1_000_000
        if observed_ns < bar.published_utc_ns or now_ms < self._observed_ms:
            raise ValueError("warning_clock_order")
        previous = self._latest
        if previous is not None and bar.epoch == previous.epoch:
            if bar.sequence <= previous.sequence:
                return False
            if bar.dropped_total < previous.dropped_total:
                raise ValueError("warning_drop_counter_regressed")
        if previous is not None and (
            bar.epoch != previous.epoch or bar.dropped_total != previous.dropped_total
        ):
            self._bars.clear()
            self._started_ms = now_ms
            self._discarded_through_ms = 0
        if self._started_ms is None:
            self._started_ms = now_ms
        self._latest = bar
        self._observed_ms = now_ms
        self._bars = {
            start: item for start, item in self._bars.items()
            if item.end_ms > now_ms - 60_000
        }
        # A late correction is not a new current-window signal.
        if bar.end_ms > now_ms - 60_000 and bar.start_ms <= now_ms:
            self._bars[bar.start_ms] = bar
        while len(self._bars) > 64:
            removed = self._bars.pop(min(self._bars))
            self._discarded_through_ms = max(self._discarded_through_ms, removed.end_ms)
        return True

    def evaluate(
        self, *, observed_ms: int, coverage: CoverageEvidence | None = None,
        cooling: bool = False, window_at_ms: int | None = None,
    ) -> WarningEvaluation:
        if observed_ms < self._observed_ms:
            raise ValueError("warning_clock_order")
        latest = self._latest
        at_ms = observed_ms if window_at_ms is None else window_at_ms
        if at_ms > observed_ms:
            raise ValueError("warning_future_window")
        ranges: list[WindowRange] = []
        warnings: list[int] = []
        for seconds in _WINDOWS:
            cutoff = at_ms - seconds * 1000
            whole = [
                bar for bar in self._bars.values()
                if ((bar.start_ms < at_ms and bar.end_ms > cutoff) if cooling
                    else (cutoff <= bar.start_ms and bar.end_ms <= at_ms))
            ]
            partial = any(
                bar.start_ms < cutoff < bar.end_ms or bar.start_ms < at_ms < bar.end_ms
                for bar in self._bars.values()
            )
            low = min((bar.low for bar in whole), default=None)
            high = max((bar.high for bar in whole), default=None)
            known = bool(
                latest is not None and coverage is not None and coverage.continuous
                and coverage.epoch == latest.epoch
                and coverage.dropped_total == latest.dropped_total
                and coverage.start_ms <= cutoff and coverage.through_ms >= at_ms
                and (self._started_ms or 0) <= cutoff
                and self._discarded_through_ms <= cutoff
                and (cooling or not partial)
            )
            ranges.append(WindowRange(seconds, low, high, known, partial))
            if (
                not cooling and latest is not None and cutoff <= latest.start_ms
                and latest.end_ms <= at_ms <= latest.end_ms + 1000
                and low is not None and latest.high >= low * _WARNING
            ):
                warnings.append(seconds)
        return WarningEvaluation(self.symbol, observed_ms, tuple(ranges), tuple(warnings),
                                 at_ms if cooling else None)


@dataclass
class _Candidate:
    last_warning_ms: int
    quiet_since_ms: int | None = None
    last_quiet_sample_ms: int | None = None


@dataclass(frozen=True)
class CandidateOutcome:
    symbol: str
    status: str
    reason: str


class WarningCandidates:
    """Cap proposals against shared active paths, without owning subscriptions."""

    def __init__(self, *, cap: int = 16, retention_ms: int = 60_000,
                 cooldown_ms: int = 60_000, owner_cap: int | None = None) -> None:
        if not 1 <= cap <= 16 or retention_ms < 60_000 or cooldown_ms < 0:
            raise ValueError("invalid_warning_candidate_bounds")
        self.cap = cap
        self.owner_cap = cap if owner_cap is None else owner_cap
        if not 1 <= self.owner_cap <= cap:
            raise ValueError("invalid_warning_owner_cap")
        self.retention_ms = retention_ms
        self.cooldown_ms = cooldown_ms
        self._candidates: dict[str, _Candidate] = {}
        self._cooldowns: dict[str, int] = {}
        self._now_ms = 0

    def desired_symbols(self, active_path_symbols: set[str]) -> set[str]:
        desired = {symbol.upper() for symbol in active_path_symbols} | set(self._candidates)
        if len(desired) > self.cap:
            raise ValueError("external_active_path_cap_exceeded")
        return desired

    def invalidate_cooling(self) -> None:
        for candidate in self._candidates.values():
            candidate.quiet_since_ms = None
            candidate.last_quiet_sample_ms = None

    def observe(
        self, evaluation: WarningEvaluation, *, active_path_symbols: set[str],
        update_cooling: bool = True,
    ) -> CandidateOutcome:
        now = evaluation.observed_ms
        if now < self._now_ms:
            raise ValueError("candidate_clock_order")
        self._now_ms = now
        self._cooldowns = {key: end for key, end in self._cooldowns.items() if end > now}
        symbol = evaluation.symbol
        active = {item.upper() for item in active_path_symbols}
        desired = self.desired_symbols(active)
        candidate = self._candidates.get(symbol)
        if evaluation.warning_windows:
            if candidate is None:
                if symbol in self._cooldowns and symbol not in active:
                    return CandidateOutcome(symbol, "REFUSED", "drop_cooldown")
                if symbol not in desired and len(desired) >= self.cap:
                    return CandidateOutcome(symbol, "REFUSED", "warning_cap_full")
                if len(self._candidates) >= self.owner_cap:
                    return CandidateOutcome(symbol, "REFUSED", "warning_owner_cap_full")
                self._candidates[symbol] = _Candidate(now)
                return CandidateOutcome(symbol, "ADMITTED", "observed_warning")
            candidate.last_warning_ms = now
            candidate.quiet_since_ms = None
            candidate.last_quiet_sample_ms = None
            return CandidateOutcome(symbol, "RETAINED", "warning_refreshed")
        if candidate is None:
            return CandidateOutcome(symbol, "IGNORED", "no_warning_candidate")
        if not update_cooling:
            return CandidateOutcome(symbol, "RETAINED", "cooling_requires_read_checkpoint")
        proof_ms = now if evaluation.proof_at_ms is None else evaluation.proof_at_ms
        if evaluation.fully_known_cool:
            previous = candidate.last_quiet_sample_ms
            if previous is None or not 0 <= proof_ms - previous <= 1000:
                candidate.quiet_since_ms = proof_ms
            candidate.last_quiet_sample_ms = proof_ms
        else:
            candidate.quiet_since_ms = None
            candidate.last_quiet_sample_ms = None
        if symbol in active:
            return CandidateOutcome(symbol, "RETAINED", "active_paper_path")
        quiet = candidate.quiet_since_ms
        if (
            quiet is not None and proof_ms - quiet >= 60_000
            and now - candidate.last_warning_ms >= self.retention_ms
        ):
            self._candidates.pop(symbol)
            self._cooldowns[symbol] = now + self.cooldown_ms
            return CandidateOutcome(symbol, "DROPPED", "both_windows_cool_60s")
        reason = "quiet_or_retention_not_proved"
        if any(window.low is None or window.high is None for window in evaluation.windows):
            reason = "empty_window_bounds"
        elif any(not window.fully_known for window in evaluation.windows):
            reason = "cooling_coverage_unknown"
        elif not evaluation.fully_known_cool:
            reason = "cooling_window_hot"
        return CandidateOutcome(symbol, "RETAINED", reason)


@dataclass(frozen=True)
class RawPathOutcome:
    status: str
    reason: str
    observed_decision_ns: int
    signal_sip_ns: int | None
    records: tuple[MomentumTapeRecord, ...] = ()


class ObservedRawPaperPath:
    """In-memory native model only; caller must choose ledger integration later.

    No aggregate input exists here. Coverage is explicit continuous-feed evidence,
    not fabricated ACK or evidence inferred from the first received trade alone.
    """

    def __init__(self, *, symbol: str, prior_close: Decimal, condition_version: str) -> None:
        self.symbol = symbol.upper()
        self._prior_close = prior_close
        self._condition_version = condition_version
        self._engine: MomentumPaperEngine | None = None
        self._coverage: CoverageEvidence | None = None
        self._first_observed_sip_ms: int | None = None
        self._last_observed_ns = 0
        self._last_sip_ns = 0
        self._pending_decisions: dict[str, int] = {}

    @property
    def active_symbols(self) -> set[str]:
        return self._engine.active_symbols if self._engine is not None else set()

    def observe(
        self, trade: TradePrint, *, sip_timestamp_ns: int, observed_ns: int,
        coverage: CoverageEvidence | None,
    ) -> RawPathOutcome:
        if trade.symbol.upper() != self.symbol or sip_timestamp_ns // 1_000_000 != trade.sip_ts_ms:
            raise ValueError("raw_trade_identity_mismatch")
        if observed_ns < self._last_observed_ns:
            raise ValueError("raw_observation_clock_order")
        self._last_observed_ns = observed_ns
        if not 0 <= observed_ns - sip_timestamp_ns <= _SECOND_NS:
            return RawPathOutcome("REFUSED", "raw_future_or_stale", observed_ns, None)
        if sip_timestamp_ns <= self._last_sip_ns:
            return RawPathOutcome("REFUSED", "raw_duplicate_or_out_of_order", observed_ns, None)
        self._last_sip_ns = sip_timestamp_ns
        if self._engine is None:
            self._engine = MomentumPaperEngine(
                prior_closes={self.symbol: self._prior_close},
                condition_version=self._condition_version,
                coverage_started_ms=trade.sip_ts_ms,
                strategy_windows=MOMENTUM_WARNING_STRATEGIES,
            )
            self._first_observed_sip_ms = trade.sip_ts_ms
        previous = self._coverage
        gap = previous is not None and (
            coverage is None or not coverage.continuous or coverage.epoch != previous.epoch
            or coverage.dropped_total != previous.dropped_total
            or coverage.start_ms > previous.through_ms
        )
        records: tuple[MomentumTapeRecord, ...] = ()
        if gap:
            records = self._engine.mark_feed_gap(previous.through_ms, observed_ns // 1_000_000)
            self._first_observed_sip_ms = trade.sip_ts_ms
        self._coverage = coverage
        first = self._first_observed_sip_ms
        complete = bool(
            coverage is not None and coverage.continuous and first is not None
            and coverage.lineage != "observed_transport"
            and coverage.start_ms <= trade.sip_ts_ms - 60_000
            and first <= trade.sip_ts_ms - 60_000 and coverage.through_ms >= trade.sip_ts_ms
        )
        # A queued pre-decision print cannot become a historical next-print fill.
        if self._pending_decisions and trade.sip_ts_ms <= max(self._pending_decisions.values()):
            return RawPathOutcome("REFUSED", "raw_predecision_print", observed_ns, None, records)
        native = self._engine.ingest(trade, allow_detection=complete,
                                     observed_decision_ns=observed_ns)
        for record in native:
            if record.event_type == "DETECTED":
                self._pending_decisions[record.logical_id] = observed_ns // 1_000_000
            elif record.event_type in {"FILLED", "NO_FILL", "UNANSWERABLE", "FINAL"}:
                self._pending_decisions.pop(record.logical_id, None)
        detected = any(record.event_type == "DETECTED" for record in native)
        return RawPathOutcome(
            "DETECTED" if detected else "OBSERVED",
            "complete_observed_raw_window" if complete else "raw_coverage_incomplete",
            observed_ns, sip_timestamp_ns if detected else None, records + native,
        )
