from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time
from decimal import Decimal, ROUND_FLOOR
import hashlib
from typing import Iterable
from zoneinfo import ZoneInfo

from project_mai_tai.momentum_paper.models import (
    MOMENTUM_STRATEGIES,
    MomentumTapeRecord,
    TradePrint,
)


_ET = ZoneInfo("America/New_York")
_DETECTION_START = time(4, 11)
_DETECTION_END = time(9, 30)
_DETECTION_MULTIPLIER = Decimal("1.20")
_TARGET_MULTIPLIER = Decimal("1.05")
_STOP_MULTIPLIER = Decimal("0.85")
_PAPER_NOTIONAL = Decimal("500")
_FILL_WINDOW_MS = 10_000
_PATH_WINDOW_MS = 600_000


def is_warrant_like(symbol: str) -> bool:
    value = symbol.upper()
    return value.endswith(("W", "WS", "WT", "U", "R"))


def symbol_is_eligible(symbol: str) -> bool:
    value = symbol.strip().upper()
    return bool(value) and "." not in value and len(value) <= 5


def detection_time_is_eligible(sip_ts_ms: int) -> bool:
    observed = datetime.fromtimestamp(sip_ts_ms / 1000, tz=UTC).astimezone(_ET)
    clock = observed.time().replace(tzinfo=None)
    return observed.weekday() < 5 and _DETECTION_START <= clock < _DETECTION_END


@dataclass
class _ActiveEvent:
    logical_id: str
    strategy_code: str
    window_seconds: int
    session_date: date
    symbol: str
    prior_close: Decimal
    reference: TradePrint
    detection: TradePrint
    condition_version: str
    window_print_count: int
    window_share_count: int
    warrant_like: bool
    excluded_prints: int = 0
    sequence: int = 0
    path: list[TradePrint] = field(default_factory=list)
    fill: TradePrint | None = None
    quantity: int | None = None
    exit: TradePrint | None = None
    exit_reason: str = ""
    pending_target: TradePrint | None = None
    mfe_pct: Decimal = Decimal("0")
    mae_pct: Decimal = Decimal("0")
    largest_gap_ms: int = 0
    last_eligible_path_ts_ms: int | None = None
    feed_gap: bool = False

    @property
    def path_deadline_ms(self) -> int:
        if self.fill is None:
            return self.detection.sip_ts_ms + _FILL_WINDOW_MS
        return self.fill.sip_ts_ms + _PATH_WINDOW_MS

    @property
    def status(self) -> str:
        if self.exit is not None:
            return self.exit_reason
        if self.fill is not None:
            return "OPEN"
        return "WAITING_FILL"


class MomentumPaperEngine:
    """Pure forward evaluator for the two frozen paper strategies."""

    def __init__(
        self,
        *,
        prior_closes: dict[str, Decimal],
        condition_version: str,
        coverage_started_ms: int,
    ) -> None:
        self.prior_closes = {
            str(symbol).upper(): Decimal(str(price)) for symbol, price in prior_closes.items()
        }
        self.condition_version = condition_version
        self.coverage_started_ms = int(coverage_started_ms)
        self._eligible_history: dict[str, deque[TradePrint]] = defaultdict(deque)
        self._excluded_history: dict[str, deque[TradePrint]] = defaultdict(deque)
        self._reference_after_ms: dict[tuple[str, str], int] = {}
        self._active: dict[str, _ActiveEvent] = {}
        self._completed: list[dict[str, object]] = []
        self._session_excluded = 0

    @property
    def active_events(self) -> tuple[dict[str, object], ...]:
        return tuple(self._summary(event) for event in self._active.values())

    @property
    def completed_events(self) -> tuple[dict[str, object], ...]:
        return tuple(self._completed)

    @property
    def session_excluded_prints(self) -> int:
        return self._session_excluded

    def seed_reentry_boundaries(self, rows: Iterable[MomentumTapeRecord]) -> None:
        for row in rows:
            if row.event_type not in {"FINAL", "NO_FILL", "UNANSWERABLE"}:
                continue
            exit_payload = row.payload.get("exit")
            boundary_ms = (
                int(exit_payload.get("sip_ts_ms", 0) or 0) if isinstance(exit_payload, dict) else 0
            )
            if not boundary_ms:
                boundary_ms = int(row.payload.get("terminal_boundary_sip_ts_ms", 0) or 0)
            if not boundary_ms:
                boundary_ms = int(row.observed_at.timestamp() * 1000)
            key = (row.strategy_code, row.symbol.upper())
            self._reference_after_ms[key] = max(
                boundary_ms,
                self._reference_after_ms.get(key, 0),
            )

    def ingest(self, trade: TradePrint) -> tuple[MomentumTapeRecord, ...]:
        trade = TradePrint(
            symbol=trade.symbol.upper(),
            sip_ts_ms=trade.sip_ts_ms,
            participant_ts_ms=trade.participant_ts_ms,
            price=trade.price,
            size=trade.size,
            trade_id=trade.trade_id,
            conditions=trade.conditions,
            eligible=trade.eligible,
            exclusion_reason=trade.exclusion_reason,
        )
        records: list[MomentumTapeRecord] = []
        event_records, path_strategies = self._advance_symbol_events(trade)
        records.extend(event_records)

        history = self._eligible_history[trade.symbol]
        excluded = self._excluded_history[trade.symbol]
        cutoff = trade.sip_ts_ms - 60_000
        while history and history[0].sip_ts_ms < cutoff:
            history.popleft()
        while excluded and excluded[0].sip_ts_ms < cutoff:
            excluded.popleft()

        if not trade.eligible:
            self._session_excluded += 1
            excluded.append(trade)
            return tuple(records)

        if self._can_detect(trade):
            for strategy_code, window_seconds in MOMENTUM_STRATEGIES.items():
                key = (strategy_code, trade.symbol)
                if self._has_unresolved_event(key):
                    continue
                reference_after_ms = self._reference_after_ms.get(key, 0)
                reference_pool = [
                    item
                    for item in history
                    if trade.sip_ts_ms - window_seconds * 1000 <= item.sip_ts_ms
                    and item.sip_ts_ms < trade.sip_ts_ms
                    and item.sip_ts_ms > reference_after_ms
                ]
                if not reference_pool:
                    continue
                reference = min(reference_pool, key=lambda item: item.price)
                if trade.price < reference.price * _DETECTION_MULTIPLIER:
                    continue
                window_excluded = sum(
                    1
                    for item in excluded
                    if trade.sip_ts_ms - window_seconds * 1000 <= item.sip_ts_ms
                    and item.sip_ts_ms < trade.sip_ts_ms
                )
                event = self._new_event(
                    strategy_code=strategy_code,
                    window_seconds=window_seconds,
                    reference=reference,
                    detection=trade,
                    window_prints=reference_pool,
                    excluded_prints=window_excluded,
                )
                self._active[event.logical_id] = event
                records.append(self._record(event, "DETECTED", trade, self._summary(event)))
                path_strategies.add(strategy_code)

        history.append(trade)
        records.extend(
            self._shared_path_record(strategy_code, trade)
            for strategy_code in sorted(path_strategies)
        )
        return tuple(records)

    def _has_unresolved_event(self, key: tuple[str, str]) -> bool:
        strategy_code, symbol = key
        return any(
            event.strategy_code == strategy_code and event.symbol == symbol and event.exit is None
            for event in self._active.values()
        )

    def advance_clock(self, now_ms: int) -> tuple[MomentumTapeRecord, ...]:
        records: list[MomentumTapeRecord] = []
        for event in list(self._active.values()):
            records.extend(self._resolve_pending_target(event, now_ms))
            if event.fill is None and now_ms > event.path_deadline_ms:
                records.extend(
                    self._finish(event, "NO_FILL", now_ms, "no_eligible_print_within_10s")
                )
            elif event.exit is not None and now_ms > event.path_deadline_ms:
                records.extend(self._finalize(event, now_ms))
        return tuple(records)

    def mark_feed_gap(self, started_ms: int, ended_ms: int) -> tuple[MomentumTapeRecord, ...]:
        records: list[MomentumTapeRecord] = []
        for event in self._active.values():
            if ended_ms < event.detection.sip_ts_ms or started_ms > event.path_deadline_ms:
                continue
            event.feed_gap = True
            records.append(
                self._record(
                    event,
                    "FEED_GAP",
                    datetime.fromtimestamp(ended_ms / 1000, tz=UTC),
                    {"started_ms": started_ms, "ended_ms": ended_ms},
                )
            )
        return tuple(records)

    def close_session(self, now_ms: int) -> tuple[MomentumTapeRecord, ...]:
        records: list[MomentumTapeRecord] = []
        records.extend(self.advance_clock(now_ms))
        for event in list(self._active.values()):
            if event.fill is None and now_ms > event.detection.sip_ts_ms + _FILL_WINDOW_MS:
                records.extend(
                    self._finish(event, "NO_FILL", now_ms, "no_eligible_print_within_10s")
                )
                continue
            reason = "feed_gap" if event.feed_gap else "session_tail_closed_before_complete_path"
            records.extend(self._finish(event, "UNANSWERABLE", now_ms, reason))
        return tuple(records)

    def _can_detect(self, trade: TradePrint) -> bool:
        prior_close = self.prior_closes.get(trade.symbol)
        return (
            detection_time_is_eligible(trade.sip_ts_ms)
            and symbol_is_eligible(trade.symbol)
            and prior_close is not None
            and prior_close >= Decimal("1.00")
            and trade.sip_ts_ms - self.coverage_started_ms >= 60_000
        )

    def _new_event(
        self,
        *,
        strategy_code: str,
        window_seconds: int,
        reference: TradePrint,
        detection: TradePrint,
        window_prints: list[TradePrint],
        excluded_prints: int,
    ) -> _ActiveEvent:
        logical_id = f"{strategy_code}:{detection.symbol}:{detection.sip_ts_ms}"
        return _ActiveEvent(
            logical_id=logical_id,
            strategy_code=strategy_code,
            window_seconds=window_seconds,
            session_date=detection.observed_at.astimezone(_ET).date(),
            symbol=detection.symbol,
            prior_close=self.prior_closes[detection.symbol],
            reference=reference,
            detection=detection,
            condition_version=self.condition_version,
            window_print_count=len(window_prints),
            window_share_count=sum(item.size for item in window_prints),
            warrant_like=is_warrant_like(detection.symbol),
            excluded_prints=excluded_prints,
            path=[detection],
            last_eligible_path_ts_ms=detection.sip_ts_ms,
        )

    def _advance_symbol_events(
        self, trade: TradePrint
    ) -> tuple[list[MomentumTapeRecord], set[str]]:
        records: list[MomentumTapeRecord] = []
        path_strategies: set[str] = set()
        for event in [item for item in self._active.values() if item.symbol == trade.symbol]:
            records.extend(self._resolve_pending_target(event, trade.sip_ts_ms))
            if event.logical_id not in self._active:
                continue
            if trade.sip_ts_ms > event.path_deadline_ms:
                if event.fill is None:
                    records.extend(
                        self._finish(
                            event,
                            "NO_FILL",
                            trade.sip_ts_ms,
                            "no_eligible_print_within_10s",
                        )
                    )
                    continue
                if event.exit is not None:
                    records.extend(self._finalize(event, trade.sip_ts_ms))
                    continue
                if not trade.eligible:
                    continue
                records.extend(self._set_exit(event, trade, "TIME"))
                records.extend(self._finalize(event, trade.sip_ts_ms))
                continue

            if trade.sip_ts_ms < event.detection.sip_ts_ms:
                continue
            if trade.sip_ts_ms <= event.path_deadline_ms or (
                event.fill is not None and event.exit is None and trade.eligible
            ):
                self._append_path(event, trade)
                path_strategies.add(event.strategy_code)
            if not trade.eligible:
                event.excluded_prints += 1
                continue

            if event.fill is None:
                if trade.sip_ts_ms <= event.detection.sip_ts_ms:
                    continue
                event.fill = trade
                event.quantity = max(
                    1,
                    int((_PAPER_NOTIONAL / trade.price).to_integral_value(rounding=ROUND_FLOOR)),
                )
                records.append(
                    self._record(
                        event,
                        "FILLED",
                        trade,
                        {
                            "fill": trade.payload(),
                            "quantity": event.quantity,
                            "latency_ms": trade.sip_ts_ms - event.detection.sip_ts_ms,
                        },
                    )
                )
                continue

            if trade.sip_ts_ms <= event.fill.sip_ts_ms:
                continue
            move = (trade.price / event.fill.price - Decimal("1")) * Decimal("100")
            event.mfe_pct = max(event.mfe_pct, move)
            event.mae_pct = min(event.mae_pct, move)
            if event.exit is not None:
                continue
            second = trade.sip_ts_ms // 1000
            if trade.price <= event.fill.price * _STOP_MULTIPLIER:
                event.pending_target = None
                records.extend(self._set_exit(event, trade, "STOP"))
            elif trade.price >= event.fill.price * _TARGET_MULTIPLIER:
                if event.pending_target is None:
                    event.pending_target = trade
            if (
                event.pending_target is not None
                and event.pending_target.sip_ts_ms // 1000 < second
                and event.exit is None
            ):
                records.extend(self._set_exit(event, event.pending_target, "TARGET"))
        return records, path_strategies

    def _resolve_pending_target(self, event: _ActiveEvent, now_ms: int) -> list[MomentumTapeRecord]:
        target = event.pending_target
        if target is None or event.exit is not None:
            return []
        if now_ms // 1000 <= target.sip_ts_ms // 1000:
            return []
        return self._set_exit(event, target, "TARGET")

    def _set_exit(
        self, event: _ActiveEvent, trade: TradePrint, reason: str
    ) -> list[MomentumTapeRecord]:
        if event.exit is not None:
            return []
        event.exit = trade
        event.exit_reason = reason
        event.pending_target = None
        key = (event.strategy_code, event.symbol)
        self._reference_after_ms[key] = max(
            trade.sip_ts_ms,
            self._reference_after_ms.get(key, 0),
        )
        return [
            self._record(
                event,
                "EXITED",
                trade,
                {"exit": trade.payload(), "exit_reason": reason},
            )
        ]

    @staticmethod
    def _append_path(event: _ActiveEvent, trade: TradePrint) -> None:
        if trade.eligible and event.last_eligible_path_ts_ms is not None:
            event.largest_gap_ms = max(
                event.largest_gap_ms,
                trade.sip_ts_ms - event.last_eligible_path_ts_ms,
            )
        if trade.eligible:
            event.last_eligible_path_ts_ms = trade.sip_ts_ms
        event.path.append(trade)

    def _finish(
        self, event: _ActiveEvent, status: str, now_ms: int, reason: str
    ) -> list[MomentumTapeRecord]:
        terminal_boundary_ms = self._terminal_boundary_ms(event, status, now_ms)
        if event.exit is None:
            key = (event.strategy_code, event.symbol)
            self._reference_after_ms[key] = max(
                terminal_boundary_ms,
                self._reference_after_ms.get(key, 0),
            )
        summary = self._summary(event)
        summary.update(
            {
                "status": status,
                "reason": reason,
                "path_complete": status == "FINAL" and reason == "path_complete",
                "terminal_boundary_sip_ts_ms": terminal_boundary_ms,
            }
        )
        record = self._record(
            event,
            status,
            datetime.fromtimestamp(now_ms / 1000, tz=UTC),
            summary,
        )
        self._completed.append(summary)
        self._active.pop(event.logical_id, None)
        return [record]

    def _finalize(self, event: _ActiveEvent, now_ms: int) -> list[MomentumTapeRecord]:
        if event.feed_gap:
            return self._finish(event, "UNANSWERABLE", now_ms, "feed_gap")
        if event.exit is None:
            return self._finish(event, "UNANSWERABLE", now_ms, "missing_exit_print")
        return self._finish(event, "FINAL", now_ms, "path_complete")

    @staticmethod
    def _terminal_boundary_ms(event: _ActiveEvent, status: str, now_ms: int) -> int:
        if status == "NO_FILL":
            return event.detection.sip_ts_ms + _FILL_WINDOW_MS
        if status == "UNANSWERABLE":
            return event.path_deadline_ms
        if event.exit is not None:
            return event.exit.sip_ts_ms
        return now_ms

    @staticmethod
    def _tape_logical_id(strategy_code: str, symbol: str, session_date: date) -> str:
        return f"momentum-tape:{session_date.isoformat()}:{strategy_code}:{symbol}"

    def _shared_path_record(self, strategy_code: str, trade: TradePrint) -> MomentumTapeRecord:
        session_date = trade.observed_at.astimezone(_ET).date()
        logical_id = self._tape_logical_id(strategy_code, trade.symbol, session_date)
        raw_identity = trade.trade_id.strip() or (
            f"anonymous:{trade.sip_ts_ms}:{trade.participant_ts_ms}:{trade.price}:"
            f"{trade.size}:{','.join(str(code) for code in trade.conditions)}"
        )
        trade_identity = hashlib.sha256(raw_identity.encode("utf-8")).hexdigest()[:24]
        return MomentumTapeRecord(
            event_key=f"{logical_id}:{trade_identity}:PATH_PRINT",
            logical_id=logical_id,
            strategy_code=strategy_code,
            event_type="PATH_PRINT",
            session_date=session_date,
            symbol=trade.symbol,
            observed_at=trade.observed_at,
            payload=trade.payload(),
        )

    def _summary(self, event: _ActiveEvent) -> dict[str, object]:
        fill_price = event.fill.price if event.fill is not None else None
        exit_price = event.exit.price if event.exit is not None else None
        pnl_pct = None
        pnl = None
        if fill_price is not None and exit_price is not None and event.quantity is not None:
            pnl_pct = (exit_price / fill_price - Decimal("1")) * Decimal("100")
            pnl = (exit_price - fill_price) * event.quantity
        return {
            "logical_id": event.logical_id,
            "strategy_code": event.strategy_code,
            "window_seconds": event.window_seconds,
            "session_date": event.session_date.isoformat(),
            "symbol": event.symbol,
            "prior_close": str(event.prior_close),
            "reference": event.reference.payload(),
            "detect": event.detection.payload(),
            "path_start": {**event.detection.payload(), "dt_ms": 0},
            "path_range": {
                "tape_logical_id": self._tape_logical_id(
                    event.strategy_code, event.symbol, event.session_date
                ),
                "start_sip_ts_ms": event.detection.sip_ts_ms,
                "end_sip_ts_ms": event.path_deadline_ms,
                "start_inclusive": True,
                "end_inclusive": True,
            },
            "window_print_count": event.window_print_count,
            "window_share_count": event.window_share_count,
            "excluded_prints": event.excluded_prints,
            "condition_version": event.condition_version,
            "warrant_like": event.warrant_like,
            "status": event.status,
            "fill": event.fill.payload() if event.fill is not None else None,
            "quantity": event.quantity,
            "exit": event.exit.payload() if event.exit is not None else None,
            "exit_reason": event.exit_reason,
            "pnl": str(pnl) if pnl is not None else None,
            "pnl_pct": str(pnl_pct) if pnl_pct is not None else None,
            "mfe_pct": str(event.mfe_pct),
            "mae_pct": str(event.mae_pct),
            "largest_no_print_gap_ms": event.largest_gap_ms,
            "path_print_count": len(event.path),
            "path_complete": False,
        }

    def _record(
        self,
        event: _ActiveEvent,
        event_type: str,
        observed: TradePrint | datetime,
        payload: dict[str, object],
    ) -> MomentumTapeRecord:
        event.sequence += 1
        observed_at = observed.observed_at if isinstance(observed, TradePrint) else observed
        return MomentumTapeRecord(
            event_key=f"{event.logical_id}:{event.sequence:08d}:{event_type}",
            logical_id=event.logical_id,
            strategy_code=event.strategy_code,
            event_type=event_type,
            session_date=event.session_date,
            symbol=event.symbol,
            observed_at=observed_at,
            payload=payload,
        )
