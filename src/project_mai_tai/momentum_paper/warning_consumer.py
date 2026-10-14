"""Bounded warning proposals and separate paper evaluation; no provider actions."""

from __future__ import annotations
from project_mai_tai.momwarn_validation_window import MomentumValidationWindow

from collections import OrderedDict
from datetime import UTC, datetime, time
from decimal import Decimal
from zoneinfo import ZoneInfo

from project_mai_tai.momentum_paper.engine import (
    MomentumPaperEngine,
    symbol_is_eligible,
)
from project_mai_tai.momentum_paper.models import (
    MOMENTUM_WARNING_STRATEGIES,
    MomentumTapeRecord,
    TradePrint,
)
from project_mai_tai.momentum_paper.warning_window import (
    AggregateObservation,
    CandidateOutcome,
    CoverageEvidence,
    ObservedATransport,
    WarningCandidates,
    WarningWindow,
)
from project_mai_tai.momentum_paper.warning_source_age import (
    AggregateSourceAgeStats,
    aggregate_source_age_reason,
)


class MomentumWarningConsumer:
    def __init__(self, *, prior_closes: dict[str, Decimal], condition_version: str,
                 coverage_started_ms: int, max_window_symbols: int = 20_000,
                 owner_cap: int = 16,
                 validation_window: MomentumValidationWindow | None = None) -> None:
        if max_window_symbols < 16:
            raise ValueError("warning_window_symbol_bound")
        self.engine = MomentumPaperEngine(
            prior_closes=prior_closes, condition_version=condition_version,
            coverage_started_ms=coverage_started_ms,
            strategy_windows=MOMENTUM_WARNING_STRATEGIES,
            validation_window=validation_window,
        )
        self.candidates = WarningCandidates(owner_cap=owner_cap)
        self.windows: OrderedDict[str, WarningWindow] = OrderedDict()
        self.max_window_symbols = max_window_symbols
        self.coverage: CoverageEvidence | None = None
        self.transport = ObservedATransport()
        self._latest: dict[str, AggregateObservation] = {}
        self._raw_first_ms: dict[str, int] = {}
        self._last_raw_ns: dict[str, int] = {}
        self._raw_coverage: dict[str, CoverageEvidence] = {}
        self._stream_epoch: tuple[str, int] | None = None
        self._sequence = -1
        self._drops = 0
        self.last_status = "waiting_aggregate"
        self.last_warning_ns: dict[str, int] = {}
        self.cooling_status: dict[str, str] = {}
        self.source_age_stats = AggregateSourceAgeStats()

    def desired_symbols(self, legacy_active: set[str]) -> set[str]:
        return self.candidates.desired_symbols(legacy_active | self.engine.active_symbols)

    def set_coverage(self, evidence: CoverageEvidence | None) -> None:
        # Silence/absence of a bar is not a coverage claim.
        self.coverage = evidence
        if evidence is None:
            self.transport.invalidate("coverage_invalidated")
            self.candidates.invalidate_cooling()

    def invalidate_transport(self, reason: str) -> None:
        self.coverage = None
        self.transport.invalidate(reason)
        self.candidates.invalidate_cooling()

    def complete_read(self, message_id: str, *, observed_ns: int) -> None:
        self.transport.checkpoint(message_id, observed_ns=observed_ns)
        self.coverage = self.transport.evidence(observed_ns=observed_ns)
        if self.coverage is None:
            self.candidates.invalidate_cooling()

    @property
    def epoch(self) -> tuple[str, int] | None:
        return self._stream_epoch

    def expire_quiet(self, *, observed_ms: int, legacy_active: set[str]) -> tuple[CandidateOutcome, ...]:
        if self.coverage is None or self.coverage.lineage == "observed_transport":
            self.coverage = self.transport.evidence(observed_ns=observed_ms * 1_000_000)
        if self.coverage is None:
            self.candidates.invalidate_cooling()
        at_ms = (
            min(observed_ms, self.coverage.through_ms) // 1000 * 1000
            if self.coverage is not None else observed_ms // 1000 * 1000
        )
        active = legacy_active | self.engine.active_symbols
        outcomes = list(self.candidates.expire_no_data(
            observed_ms=observed_ms, active_path_symbols=active,
        ))
        for outcome in outcomes:
            self._raw_first_ms.pop(outcome.symbol, None)
            self._raw_coverage.pop(outcome.symbol, None)
        if outcomes:
            self.last_status = "expired_no_data"
        for symbol in sorted(self.desired_symbols(legacy_active)):
            window = self.windows.get(symbol)
            if window is not None:
                outcomes.append(self.candidates.observe(
                    window.evaluate(observed_ms=observed_ms, coverage=self.coverage,
                                    cooling=True, window_at_ms=at_ms),
                    active_path_symbols=active,
                ))
        self.cooling_status = {row.symbol: row.reason for row in outcomes}
        return tuple(outcomes)

    def observe_aggregate(
        self, bar: AggregateObservation, *, observed_ns: int, legacy_active: set[str],
    ) -> tuple[CandidateOutcome, tuple[MomentumTapeRecord, ...]]:
        now_ms = observed_ns // 1_000_000
        active = legacy_active | self.engine.active_symbols
        clock = datetime.fromtimestamp(now_ms / 1000, tz=UTC).astimezone(
            ZoneInfo("America/New_York")
        )
        transport_bad = observed_ns < bar.published_utc_ns or observed_ns - bar.received_utc_ns > 1_000_000_000
        age_reason = aggregate_source_age_reason(end_ms=bar.end_ms, observed_ms=now_ms)
        self.source_age_stats.observe(end_ms=bar.end_ms, observed_ns=observed_ns,
            refusal_reason="aggregate_clock_or_stale" if transport_bad else age_reason)
        if transport_bad:
            self.invalidate_transport("aggregate_clock_or_stale")
            return CandidateOutcome(bar.symbol, "REFUSED", "aggregate_clock_or_stale"), ()
        if bar.epoch == self._stream_epoch and bar.sequence <= self._sequence:
            return CandidateOutcome(bar.symbol, "IGNORED", "duplicate_or_reordered_aggregate"), ()
        if bar.epoch == self._stream_epoch and bar.dropped_total < self._drops:
            self.invalidate_transport("warning_drop_counter_regressed")
            raise ValueError("warning_drop_counter_regressed")
        records: tuple[MomentumTapeRecord, ...] = ()
        if self._stream_epoch is not None and (
            bar.epoch != self._stream_epoch or bar.sequence != self._sequence + 1
            or bar.dropped_total != self._drops
        ):
            records = self.engine.mark_feed_gap(self.engine.coverage_started_ms, now_ms)
            self.windows.clear()
            self._latest.clear()
            self.invalidate_transport("aggregate_coverage_gap")
            self._raw_first_ms.clear()
            self._raw_coverage.clear()
            self.last_warning_ns.clear()
            self.last_status = "aggregate_coverage_gap"
        self._stream_epoch = bar.epoch
        self._sequence = bar.sequence
        self._drops = bar.dropped_total
        self.transport.observe(bar, observed_ns=observed_ns)
        if age_reason is not None:
            window = self.windows.get(bar.symbol)
            latest_start = window.newest_bucket_start_ms if window is not None else None
            if age_reason == "aggregate_source_end_stale" and latest_start is not None and latest_start >= bar.start_ms:
                self.source_age_stats.late_revisions += 1
                self.last_status = "aggregate_late_revision"
                return CandidateOutcome(bar.symbol, "IGNORED", "aggregate_late_revision"), records
            # An in-order late bucket is a symbol hole, not a transport gap.
            self.windows.pop(bar.symbol, None)
            self._latest.pop(bar.symbol, None)
            self.last_warning_ns.pop(bar.symbol, None)
            self.last_status = age_reason
            return CandidateOutcome(bar.symbol, "REFUSED", age_reason), records
        if (
            not symbol_is_eligible(bar.symbol)
            or self.engine.prior_closes.get(bar.symbol, Decimal("0")) < 1
            or clock.weekday() >= 5
            or not time(4) <= clock.time() < self.engine.validation_window.gateway_end_for(clock.date())
        ):
            return CandidateOutcome(bar.symbol, "IGNORED", "warning_symbol_or_time_filter"), records
        window = self.windows.get(bar.symbol)
        if window is None:
            if len(self.windows) >= self.max_window_symbols:
                # Bound memory without evicting candidate/active-path evidence.
                protected = self.desired_symbols(legacy_active)
                victim = next((symbol for symbol in self.windows if symbol not in protected), None)
                if victim is None:
                    return CandidateOutcome(bar.symbol, "REFUSED", "window_capacity"), records
                self.windows.pop(victim)
                self._latest.pop(victim, None)
            window = WarningWindow(bar.symbol)
            self.windows[bar.symbol] = window
        window.observe(bar, observed_ns=observed_ns)
        self.windows.move_to_end(bar.symbol)
        self._latest[bar.symbol] = bar
        evaluation = window.evaluate(observed_ms=now_ms, coverage=self.coverage)
        outcome = self.candidates.observe(evaluation, active_path_symbols=active,
                                           update_cooling=False)
        if outcome.status in {"ADMITTED", "RETAINED"} and evaluation.warning_windows:
            self.last_warning_ns[bar.symbol] = observed_ns
        if bar.symbol in self.desired_symbols(legacy_active):
            records += self._bucket_signals(bar.symbol, observed_ns=observed_ns)
        self.last_status = outcome.reason
        return outcome, records

    def _bucket_signals(
        self, symbol: str, *, observed_ns: int, trade: TradePrint | None = None,
    ) -> tuple[MomentumTapeRecord, ...]:
        window = self.windows.get(symbol)
        latest = self._latest.get(symbol)
        if window is None or latest is None:
            return ()
        now_ms = trade.sip_ts_ms if trade is not None else observed_ns // 1_000_000
        records: tuple[MomentumTapeRecord, ...] = ()
        for code, seconds in MOMENTUM_WARNING_STRATEGIES.items():
            # Keep actual bucket bounds; no fake reference timestamp enters raw history.
            eligible = window.whole_buckets(
                observed_ms=now_ms, seconds=seconds,
                after_ms=self.engine.reference_boundary(code, symbol),
            )
            if trade is None:
                eligible = tuple(item for item in eligible if (
                    item.end_ms <= latest.start_ms
                    or (item.start_ms, item.end_ms) == (latest.start_ms, latest.end_ms)
                ))
            if not eligible:
                continue
            reference = min(eligible, key=lambda bar: (bar.low, bar.start_ms))
            records += self.engine.detect_from_bucket(
                strategy_code=code, symbol=symbol, reference_price=reference.low,
                reference_start_ms=reference.start_ms, reference_end_ms=reference.end_ms,
                signal_price=trade.price if trade else latest.close,
                signal_start_ms=trade.sip_ts_ms if trade else latest.start_ms,
                signal_end_ms=trade.sip_ts_ms if trade else latest.end_ms,
                observed_decision_ns=observed_ns, signal_trade=trade,
                aggregate_received_utc_ns=latest.received_utc_ns,
                aggregate_published_utc_ns=latest.published_utc_ns,
            )
        return records

    def observe_trade(
        self, trade: TradePrint, *, sip_timestamp_ns: int, observed_ns: int,
        raw_coverage: CoverageEvidence | None = None,
    ) -> tuple[MomentumTapeRecord, ...]:
        symbol = trade.symbol
        if not symbol_is_eligible(symbol):
            self.last_status = "raw_symbol_ineligible"
            return ()
        if symbol not in self.candidates.desired_symbols(self.engine.active_symbols):
            self.last_status = "raw_symbol_not_warning_owned"
            return ()
        if sip_timestamp_ns // 1_000_000 != trade.sip_ts_ms:
            raise ValueError("raw_timestamp_mismatch")
        if not 0 <= observed_ns - sip_timestamp_ns <= 1_000_000_000:
            self.last_status = "raw_future_or_stale"
            # An unusable print compromises only its symbol's forward-path evidence.
            return self.engine.mark_feed_gap(
                trade.sip_ts_ms, observed_ns // 1_000_000, symbol=symbol,
            )
        if sip_timestamp_ns <= self._last_raw_ns.get(symbol, 0):
            self.last_status = "raw_duplicate_or_out_of_order"
            return ()
        self._last_raw_ns[symbol] = sip_timestamp_ns
        if trade.eligible:
            self.candidates.observe_eligible_trade(symbol, observed_ms=observed_ns // 1_000_000)
        self._raw_first_ms.setdefault(symbol, trade.sip_ts_ms)
        previous = self._raw_coverage.get(symbol)
        records: tuple[MomentumTapeRecord, ...] = ()
        if previous is not None and (
            raw_coverage is None or not raw_coverage.continuous
            or raw_coverage.epoch != previous.epoch
            or raw_coverage.dropped_total != previous.dropped_total
            or raw_coverage.start_ms > previous.through_ms
        ):
            self._raw_first_ms[symbol] = trade.sip_ts_ms
            records = self.engine.mark_feed_gap(previous.through_ms, observed_ns // 1_000_000)
        if raw_coverage is None:
            self._raw_coverage.pop(symbol, None)
        else:
            self._raw_coverage[symbol] = raw_coverage
        complete = bool(
            raw_coverage is not None and raw_coverage.continuous
            and raw_coverage.lineage != "observed_transport"
            and raw_coverage.start_ms <= trade.sip_ts_ms - 60_000
            and self._raw_first_ms[symbol] <= trade.sip_ts_ms - 60_000
            and raw_coverage.through_ms >= trade.sip_ts_ms
        )
        # Advance native paths first; a detection print can never fill its own event.
        native = self.engine.ingest(trade, allow_detection=complete,
                                    observed_decision_ns=observed_ns)
        if trade.eligible and not complete:
            native += self._bucket_signals(symbol, observed_ns=observed_ns, trade=trade)
        self.last_status = "observed_raw_complete" if complete else "whole_bucket_cold_reference"
        return records + native
