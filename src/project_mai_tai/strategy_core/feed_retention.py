from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class FeedRetentionConfig:
    structure_bars: int = 10
    no_activity_minutes: int = 20
    cooldown_volume_ratio: float = 0.4
    cooldown_max_5m_range_pct: float = 1.5
    resume_hold_bars: int = 3
    resume_min_5m_range_pct: float = 2.5
    resume_min_5m_volume_ratio: float = 1.5
    resume_min_5m_volume_abs: float = 150_000.0
    drop_cooldown_minutes: int = 30
    drop_max_5m_range_pct: float = 1.0
    drop_max_5m_volume_abs: float = 75_000.0


@dataclass(frozen=True)
class FeedRetentionMetrics:
    price: float | None = None
    vwap: float | None = None
    ema20: float | None = None
    rolling_5m_volume: float | None = None
    rolling_5m_range_pct: float | None = None
    bar_timestamp: float | None = None


@dataclass
class RetainedSymbolState:
    symbol: str
    state: str
    promoted_at: datetime
    state_changed_at: datetime
    last_confirmed_at: datetime
    last_activity_at: datetime
    active_reference_5m_volume: float
    cooldown_started_at: datetime | None = None
    below_structure_bars: int = 0
    above_structure_bars: int = 0
    last_bar_timestamp: float | None = None

    def blocks_entries(self) -> bool:
        return self.state in {"cooldown", "resume_probe"}

    def keeps_feed(self) -> bool:
        return self.state != "dropped"


class FeedRetentionPolicy:
    def __init__(self, config: FeedRetentionConfig | None = None) -> None:
        self.config = config or FeedRetentionConfig()

    def promote(self, symbol: str, now: datetime, metrics: FeedRetentionMetrics | None) -> RetainedSymbolState:
        baseline_volume = float(metrics.rolling_5m_volume or 0) if metrics is not None else 0.0
        return RetainedSymbolState(
            symbol=symbol.upper(),
            state="active",
            promoted_at=now,
            state_changed_at=now,
            last_confirmed_at=now,
            last_activity_at=now,
            active_reference_5m_volume=baseline_volume,
            last_bar_timestamp=metrics.bar_timestamp if metrics is not None else None,
        )

    def evaluate(
        self,
        state: RetainedSymbolState | None,
        *,
        symbol: str,
        now: datetime,
        is_confirmed: bool,
        metrics: FeedRetentionMetrics | None,
    ) -> RetainedSymbolState | None:
        normalized_symbol = symbol.upper()
        if state is None:
            if not is_confirmed:
                return None
            return self.promote(normalized_symbol, now, metrics)

        state.symbol = normalized_symbol
        if is_confirmed:
            self._transition(state, "active", now)
            state.last_confirmed_at = now
            state.last_activity_at = now
            state.cooldown_started_at = None
            state.below_structure_bars = 0
            state.above_structure_bars = 0
            self._refresh_reference_volume(state, metrics)
            return state

        if metrics is None or metrics.price is None:
            return state

        above_structure = self._is_above_structure(metrics)
        below_structure = self._is_below_structure(metrics)
        strong_resume = self._has_resume_energy(metrics, state)
        is_new_bar = metrics.bar_timestamp is not None and metrics.bar_timestamp != state.last_bar_timestamp
        if is_new_bar:
            state.last_bar_timestamp = metrics.bar_timestamp
            if above_structure:
                state.above_structure_bars += 1
                state.below_structure_bars = 0
            elif below_structure:
                state.below_structure_bars += 1
                state.above_structure_bars = 0
            else:
                state.above_structure_bars = 0
                state.below_structure_bars = 0

        if state.state == "active":
            if above_structure or strong_resume:
                self._refresh_reference_volume(state, metrics)
            if strong_resume:
                state.last_activity_at = now
            if self._looks_dead_tape_without_vwap(metrics, now):
                self._transition(state, "cooldown", now)
                state.cooldown_started_at = now
                state.above_structure_bars = 0
                return state
            inactivity_minutes = max(0.0, (now - state.last_activity_at).total_seconds() / 60.0)
            low_volume = self._is_cooldown_volume(metrics, state)
            low_range = float(metrics.rolling_5m_range_pct or 0) <= self.config.cooldown_max_5m_range_pct
            if (
                state.below_structure_bars >= self.config.structure_bars
                and inactivity_minutes >= self.config.no_activity_minutes
                and low_volume
                and low_range
            ):
                self._transition(state, "cooldown", now)
                state.cooldown_started_at = now
                state.above_structure_bars = 0
            return state

        if state.state == "cooldown":
            if strong_resume and above_structure:
                self._transition(state, "resume_probe", now)
                state.above_structure_bars = max(1, state.above_structure_bars)
                return state
            if self._should_drop(state, now, metrics, above_structure):
                self._transition(state, "dropped", now)
            return state

        if state.state == "resume_probe":
            if not above_structure:
                self._transition(state, "cooldown", now)
                state.cooldown_started_at = state.cooldown_started_at or now
                state.above_structure_bars = 0
                return state
            if strong_resume and state.above_structure_bars >= self.config.resume_hold_bars:
                self._transition(state, "active", now)
                state.last_activity_at = now
                state.cooldown_started_at = None
                self._refresh_reference_volume(state, metrics)
                return state
            if not strong_resume:
                self._transition(state, "cooldown", now)
                state.cooldown_started_at = state.cooldown_started_at or now
            return state

        if state.state == "dropped" and is_confirmed:
            return self.promote(normalized_symbol, now, metrics)
        return state

    def _transition(self, state: RetainedSymbolState, next_state: str, now: datetime) -> None:
        if state.state == next_state:
            return
        state.state = next_state
        state.state_changed_at = now

    @staticmethod
    def _is_above_structure(metrics: FeedRetentionMetrics) -> bool:
        if metrics.price is None or metrics.ema20 is None:
            return False
        if metrics.price < metrics.ema20:
            return False
        if metrics.vwap is not None and metrics.price < metrics.vwap:
            return False
        return True

    @staticmethod
    def _is_below_structure(metrics: FeedRetentionMetrics) -> bool:
        if metrics.price is None or metrics.ema20 is None or metrics.vwap is None:
            return False
        return metrics.price < metrics.ema20 and metrics.price < metrics.vwap

    def _has_resume_energy(self, metrics: FeedRetentionMetrics, state: RetainedSymbolState) -> bool:
        rolling_volume = float(metrics.rolling_5m_volume or 0)
        rolling_range_pct = float(metrics.rolling_5m_range_pct or 0)
        required_volume = max(
            self.config.resume_min_5m_volume_abs,
            state.active_reference_5m_volume * self.config.resume_min_5m_volume_ratio,
        )
        return (
            self._is_above_structure(metrics)
            and rolling_range_pct >= self.config.resume_min_5m_range_pct
            and rolling_volume >= required_volume
        )

    def _is_cooldown_volume(self, metrics: FeedRetentionMetrics, state: RetainedSymbolState) -> bool:
        rolling_volume = float(metrics.rolling_5m_volume or 0)
        baseline = float(state.active_reference_5m_volume or 0)
        if baseline <= 0:
            return False
        return rolling_volume <= baseline * self.config.cooldown_volume_ratio

    def _should_drop(
        self,
        state: RetainedSymbolState,
        now: datetime,
        metrics: FeedRetentionMetrics,
        above_structure: bool,
    ) -> bool:
        if state.cooldown_started_at is None:
            return False
        cooldown_minutes = max(0.0, (now - state.cooldown_started_at).total_seconds() / 60.0)
        return (
            cooldown_minutes >= self.config.drop_cooldown_minutes
            and not above_structure
            and float(metrics.rolling_5m_range_pct or 0) <= self.config.drop_max_5m_range_pct
            and float(metrics.rolling_5m_volume or 0) <= self.config.drop_max_5m_volume_abs
        )

    def _looks_dead_tape_without_vwap(self, metrics: FeedRetentionMetrics, now: datetime) -> bool:
        if metrics.vwap is not None or metrics.price is None or metrics.ema20 is None:
            return False
        if now.hour < 16:
            return False
        ema20 = float(metrics.ema20)
        if ema20 <= 0:
            return False
        distance_pct = abs(float(metrics.price) - ema20) / ema20 * 100.0
        return (
            float(metrics.rolling_5m_volume or 0) <= self.config.drop_max_5m_volume_abs
            and float(metrics.rolling_5m_range_pct or 0) <= max(self.config.drop_max_5m_range_pct, 1.25)
            and distance_pct <= 0.5
        )

    @staticmethod
    def _refresh_reference_volume(
        state: RetainedSymbolState,
        metrics: FeedRetentionMetrics | None,
    ) -> None:
        if metrics is None or metrics.rolling_5m_volume is None:
            return
        rolling_volume = float(metrics.rolling_5m_volume)
        if rolling_volume <= 0:
            return
        if state.active_reference_5m_volume <= 0:
            state.active_reference_5m_volume = rolling_volume
            return
        state.active_reference_5m_volume = (state.active_reference_5m_volume * 0.8) + (rolling_volume * 0.2)
