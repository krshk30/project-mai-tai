"""Decision-only Polygon paper-exit runtime.

This module deliberately imports neither event envelopes nor broker code. Its public outputs are
``PaperDecision`` values, which the strategy service may persist but can never publish as orders.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from hashlib import sha256
from typing import Literal, Mapping
from uuid import UUID
from zoneinfo import ZoneInfo

from project_mai_tai.market_halts import (
    HALT_MIN_PRINT_GAP,
    HALT_MIN_QUOTE_UPDATES,
    HaltWindow,
    LiveHaltTracker,
)

EASTERN = ZoneInfo("America/New_York")
PAPER_STRATEGY_CODE = "polygon_30s"
MIRROR_ARM = "mirror"
ACCEPTED_RESTING_SOURCES = frozenset(
    {"cw-v2-resting", "rth_resting", "rth_resting_mirror", "eh_resting"}
)
NEUTRAL_FANOUT_VARIANTS = frozenset({"cw-v2-fanout"})
EXIT_REASON_PRIORITY = {
    "TARGET": 0,
    "HARD_STOP": 1,
    "CONFIRMATION_EXIT": 2,
    "ATR_SELL": 3,
    "16:00": 4,
}


@dataclass(frozen=True)
class PaperRuleConfig:
    id: UUID
    target_pct: Decimal
    stop_pct: Decimal
    effective_at: datetime
    confirmation_bars: int = 1

    def __post_init__(self) -> None:
        if self.target_pct <= 0 or self.stop_pct <= 0:
            raise ValueError("paper target and stop percentages must be positive")
        if self.effective_at.tzinfo is None:
            raise ValueError("paper config effective_at must be timezone-aware")
        if self.confirmation_bars < 1:
            raise ValueError("confirmation bar count must be at least one")


@dataclass(frozen=True)
class PaperSourceFill:
    fill_id: UUID
    broker_fill_id: str
    broker_account_name: str
    venue: Literal["schwab", "webull"]
    symbol: str
    quantity: Decimal
    price: Decimal
    filled_at: datetime
    fanout_slot_id: str
    entry_slot: Literal["first", "reclaim"]
    source: str

    def __post_init__(self) -> None:
        if not self.broker_fill_id.strip():
            raise ValueError("mirror fill requires a broker_fill_id")
        if not self.fanout_slot_id.strip():
            raise ValueError("mirror fill requires a fanout_slot_id")
        if self.entry_slot not in {"first", "reclaim"}:
            raise ValueError("mirror fill requires a first or reclaim entry_slot")
        if self.quantity <= 0 or self.price <= 0:
            raise ValueError("mirror fill quantity and price must be positive")
        if self.filled_at.tzinfo is None:
            raise ValueError("mirror fill time must be timezone-aware")

    @property
    def session_date(self) -> date:
        return self.filled_at.astimezone(EASTERN).date()


@dataclass(frozen=True)
class PaperDecision:
    event_key: str
    logical_id: str
    arm: Literal["mirror"]
    event_type: str
    session_date: date
    symbol: str
    observed_at: datetime
    price: Decimal | None
    quantity: Decimal | None
    config_id: UUID | None
    venue: str = ""
    source_fill_id: UUID | None = None
    broker_fill_id: str | None = None
    detail: Mapping[str, object] = field(default_factory=dict)


@dataclass
class _PaperLeg:
    identity: str
    logical_id: str
    arm: Literal["mirror"]
    symbol: str
    venue: str
    entry_at: datetime
    entry_price: Decimal
    quantity: Decimal
    config: PaperRuleConfig
    source_fill_id: UUID | None = None
    broker_fill_id: str | None = None
    source_legs: dict[UUID, PaperSourceFill] = field(default_factory=dict)
    exit_at: datetime | None = None
    exit_price: Decimal | None = None
    exit_reason: str = ""


@dataclass(frozen=True)
class _PendingExit:
    reason: str
    observed_at: datetime


def _source_from_metadata(metadata: Mapping[str, object]) -> str:
    sources = {
        str(metadata.get(key, "")).strip().lower()
        for key in ("fanout_source", "atr_variant")
        if str(metadata.get(key, "")).strip()
    }
    classified = sources - NEUTRAL_FANOUT_VARIANTS
    accepted = classified & ACCEPTED_RESTING_SOURCES
    rejected = classified - ACCEPTED_RESTING_SOURCES
    if accepted and rejected:
        return "conflicting:" + ",".join(sorted(classified))
    if accepted:
        return sorted(accepted)[0]
    if rejected:
        return sorted(rejected)[0]
    return ""


def resting_fill_classification(metadata: Mapping[str, object]) -> tuple[bool, str]:
    """Apply the exact live resting predicate; no reason-string or price inference."""
    slot = str(metadata.get("cw_entry_slot", "")).strip().lower()
    source = _source_from_metadata(metadata)
    if slot != "first":
        return False, f"cw_entry_slot={slot or 'missing'}"
    if source not in ACCEPTED_RESTING_SOURCES:
        return False, f"resting_source={source or 'missing'}"
    if source == "cw-v2-resting" and str(metadata.get("resting_entry", "")).lower() != "true":
        return False, "primary resting_entry stamp missing"
    return True, source


def logical_mirror_id(fill: PaperSourceFill) -> str:
    # A slot may be reused after a completed trade; the durable fill remains unique.
    raw = f"mirror-fill:{fill.fill_id}"
    return sha256(raw.encode("utf-8")).hexdigest()


def mirror_acceptance(*, live: int, matched: int, missed: int, phantom: int) -> str:
    if live == 0 and matched == 0 and missed == 0 and phantom == 0:
        return "UNEXERCISED"
    if live <= 0 or matched + missed != live or missed > 0 or phantom > 0:
        return "FAIL"
    return "PASS"


def completed_session_acceptance(
    *, coupling_verdict: str, session_complete: bool, matched: int, terminal: int
) -> str:
    if coupling_verdict != "PASS":
        return coupling_verdict
    if not session_complete or terminal != matched:
        return "IN_PROGRESS"
    return "PASS"


def terminal_evidence_covers(
    *,
    final_fill_at: datetime,
    final_quantity: Decimal,
    terminal_at: datetime,
    terminal_quantity: Decimal | None,
) -> bool:
    """Require terminal evidence after the source fill for its complete quantity."""
    fill_at = final_fill_at.replace(tzinfo=final_fill_at.tzinfo or UTC).astimezone(UTC)
    exit_at = terminal_at.replace(tzinfo=terminal_at.tzinfo or UTC).astimezone(UTC)
    return exit_at >= fill_at and terminal_quantity == final_quantity


class PaperExitRuntime:
    """Owns paper state only; every externally visible action is a ``PaperDecision``."""

    def __init__(self, config: PaperRuleConfig) -> None:
        self._configs = [config]
        self._legs: dict[str, _PaperLeg] = {}
        self._fill_ids: set[UUID] = set()
        self._pending_flip_at: dict[str, datetime] = {}
        self._confirmation_event_fill_ids: set[UUID] = set()
        self._confirmation_evaluated = 0
        self._confirmation_fired = 0
        self._confirmation_long = 0
        self._last_executable_bid: dict[str, tuple[datetime, Decimal]] = {}
        self._halt_trackers: dict[str, LiveHaltTracker] = {}
        self._pending_halt_exits: dict[str, _PendingExit] = {}
        self._release_after_print: set[str] = set()
        self._halt_event_keys: set[str] = set()
        self._halt_suppression_keys: set[str] = set()
        self._pending_terminal_decisions: dict[str, PaperDecision] = {}
        self._watchlist: set[str] = set()
        self._acceptance: dict[str, object] = {
            "verdict": "UNEXERCISED",
            "live": 0,
            "matched": 0,
            "missed": 0,
            "phantom": 0,
            "venues": {},
        }

    def update_config(self, config: PaperRuleConfig) -> None:
        if any(existing.id == config.id for existing in self._configs):
            return
        self._configs.append(config)
        self._configs.sort(key=lambda item: (item.effective_at, str(item.id)))

    def config_at(self, at: datetime) -> PaperRuleConfig:
        eligible = [config for config in self._configs if config.effective_at <= at]
        if not eligible:
            raise RuntimeError("no paper rule config effective at entry")
        return eligible[-1]

    def set_watchlist(self, symbols: list[str] | set[str]) -> None:
        self._watchlist = {str(symbol).strip().upper() for symbol in symbols if str(symbol).strip()}

    def set_acceptance(self, acceptance: Mapping[str, object]) -> None:
        self._acceptance = dict(acceptance)

    def add_mirror_fill(
        self,
        fill: PaperSourceFill,
        *,
        late: bool = False,
        reemit_evidence: bool = False,
    ) -> list[PaperDecision]:
        if (
            fill.broker_account_name != "live:schwab_1m_v2"
            or fill.venue != "schwab"
            or fill.entry_slot != "first"
            or fill.source not in ACCEPTED_RESTING_SOURCES
        ):
            return []
        if fill.fill_id in self._fill_ids:
            if not reemit_evidence:
                return []
            config = self.config_at(fill.filled_at)
            return [
                PaperDecision(
                    event_key=f"LATE_MIRROR:{fill.fill_id}",
                    logical_id=logical_mirror_id(fill),
                    arm=MIRROR_ARM,
                    event_type="LATE_MIRROR",
                    session_date=fill.session_date,
                    symbol=fill.symbol.upper(),
                    observed_at=fill.filled_at.astimezone(UTC),
                    price=fill.price,
                    quantity=fill.quantity,
                    config_id=config.id,
                    venue=fill.venue,
                    source_fill_id=fill.fill_id,
                    broker_fill_id=fill.broker_fill_id,
                    detail={
                        "source": fill.source,
                        "fanout_slot_id": fill.fanout_slot_id,
                        "entry_slot": fill.entry_slot,
                        "reason": "durable entry evidence retry",
                    },
                )
            ]
        config = self.config_at(fill.filled_at)
        logical_id = logical_mirror_id(fill)
        identity = f"mirror:{fill.fill_id}"
        self._fill_ids.add(fill.fill_id)
        self._legs[identity] = _PaperLeg(
            identity=identity,
            logical_id=logical_id,
            arm=MIRROR_ARM,
            symbol=fill.symbol.upper(),
            venue=fill.venue,
            entry_at=fill.filled_at.astimezone(UTC),
            entry_price=fill.price,
            quantity=fill.quantity,
            config=config,
            source_fill_id=fill.fill_id,
            broker_fill_id=fill.broker_fill_id,
            source_legs={fill.fill_id: fill},
        )
        kind = "LATE_MIRROR" if late else "MIRROR_ENTRY"
        return [
            PaperDecision(
                event_key=f"{kind}:{fill.fill_id}",
                logical_id=logical_id,
                arm=MIRROR_ARM,
                event_type=kind,
                session_date=fill.session_date,
                symbol=fill.symbol.upper(),
                observed_at=fill.filled_at.astimezone(UTC),
                price=fill.price,
                quantity=fill.quantity,
                config_id=config.id,
                venue=fill.venue,
                source_fill_id=fill.fill_id,
                broker_fill_id=fill.broker_fill_id,
                detail={
                    "source": fill.source,
                    "fanout_slot_id": fill.fanout_slot_id,
                    "entry_slot": fill.entry_slot,
                },
            )
        ]

    def mark_atr_sell(self, symbol: str, observed_at: datetime) -> None:
        self._pending_flip_at[symbol.upper()] = observed_at.astimezone(UTC)

    def on_trade(self, *, symbol: str, observed_at: datetime) -> list[PaperDecision]:
        """Use prints only as proof that a suspected/confirmed gap has ended."""
        normalized = symbol.upper()
        at = observed_at.astimezone(UTC)
        tracker = self._halt_trackers.setdefault(normalized, LiveHaltTracker())
        window = tracker.observe_print(at)
        pending = any(
            identity in self._pending_halt_exits
            for identity, leg in self._legs.items()
            if leg.symbol == normalized and leg.exit_at is None
        )
        if window is not None or pending:
            self._release_after_print.add(normalized)
        if window is None:
            return []
        decisions = self._halt_window_decisions(normalized, window, confirmed_at=at)
        decisions.extend(self._suppression_decisions(normalized, window))
        return decisions

    def on_atr_sell(self, *, symbol: str, observed_at: datetime) -> list[PaperDecision]:
        """Arm the stamped SELL; execution waits for the first later executable bid."""
        normalized = symbol.upper()
        at = observed_at.astimezone(UTC)
        decisions: list[PaperDecision] = []
        for leg in self._legs.values():
            if leg.symbol != normalized or leg.entry_at > at:
                continue
            quote_has_passed_flip = (
                leg.exit_at is None
                and normalized in self._last_executable_bid
                and self._last_executable_bid[normalized][0] > at
            )
            exit_has_passed_flip = leg.exit_at is not None and at < leg.exit_at
            if not quote_has_passed_flip and not exit_has_passed_flip:
                continue
            prior_reason = leg.exit_reason or "OPEN"
            leg.exit_at = at
            leg.exit_price = None
            leg.exit_reason = "UNANSWERABLE"
            decision = PaperDecision(
                event_key=f"UNANSWERABLE:{leg.identity}:late-atr:{at.isoformat()}",
                logical_id=leg.logical_id,
                arm=leg.arm,
                event_type="UNANSWERABLE",
                session_date=leg.entry_at.astimezone(EASTERN).date(),
                symbol=leg.symbol,
                observed_at=at,
                price=None,
                quantity=leg.quantity,
                config_id=leg.config.id,
                venue=leg.venue,
                source_fill_id=leg.source_fill_id,
                broker_fill_id=leg.broker_fill_id,
                detail={
                    "late_trigger_at": at.isoformat(),
                    "supersedes_reason": prior_reason,
                    "reason": "ATR SELL arrived after quote processing passed its timestamp; first executable bid unknown",
                },
            )
            self._remember_terminal(decision)
            decisions.append(decision)
        self.mark_atr_sell(normalized, at)
        for identity, leg in self._legs.items():
            if leg.symbol != normalized or leg.exit_at is not None or leg.entry_at > at:
                continue
            self._stage_exit(identity, reason="ATR_SELL", observed_at=at)
        tracker = self._halt_trackers.get(normalized)
        if tracker is not None and tracker.confirmed and tracker.last_print_at is not None:
            decisions.extend(
                self._suppression_decisions(
                    normalized,
                    HaltWindow(tracker.last_print_at, at, tracker.quote_updates),
                )
            )
        return decisions

    def on_confirmation_exit(
        self,
        *,
        source_fill_id: UUID,
        observed_at: datetime,
        atr_state: str,
        confirmation_bars: int,
        config_effective_at: datetime,
    ) -> list[PaperDecision]:
        """Consume v2's stamped Schwab-bar decision without recomputing ATR in paper."""
        if source_fill_id in self._confirmation_event_fill_ids:
            return []
        matching = [
            (identity, leg, leg.source_legs.get(source_fill_id))
            for identity, leg in self._legs.items()
            if source_fill_id in leg.source_legs
        ]
        if len(matching) != 1:
            return []
        identity, leg, source = matching[0]
        if source is None or source.entry_slot != "first":
            # Reclaims are not an eligible population. Do not count, stage, or mutate them.
            return []
        self._confirmation_event_fill_ids.add(source_fill_id)
        at = observed_at.astimezone(UTC)
        self._confirmation_evaluated += 1
        detail = {
            "atr_state": atr_state,
            "confirmation_bars": confirmation_bars,
            "config_effective_at": config_effective_at.isoformat(),
            "source": "v2_schwab_1m_stamped",
            "denominator": self._confirmation_evaluated,
        }
        if leg.exit_at is not None:
            return [
                self._decision(
                    leg,
                    "CONFIRMATION_SUPERSEDED",
                    at,
                    leg.exit_price,
                    reason=leg.exit_reason,
                    extra_detail=detail,
                )
            ]
        latest = self._last_executable_bid.get(leg.symbol)
        if latest is not None and latest[0] > at:
            leg.exit_at = at
            leg.exit_reason = "UNANSWERABLE"
            decision = self._decision(
                leg,
                "UNANSWERABLE",
                at,
                None,
                reason="confirmation arrived after quote processing passed its timestamp",
                extra_detail=detail,
            )
            return [decision]
        if atr_state.lower() == "long":
            self._confirmation_long += 1
            return [
                self._decision(
                    leg,
                    "CONFIRMATION_STATE_LONG",
                    at,
                    None,
                    reason="continue",
                    extra_detail=detail,
                )
            ]
        self._confirmation_fired += 1
        self._stage_exit(identity, reason="CONFIRMATION_EXIT", observed_at=at)
        return [
            self._decision(
                leg,
                "CONFIRMATION_EXIT_FIRED",
                at,
                None,
                reason="CONFIRMATION_EXIT",
                extra_detail=detail,
            )
        ]

    def restore_exit(
        self,
        *,
        logical_id: str,
        observed_at: datetime,
        price: Decimal | None,
        reason: str,
        force: bool = False,
    ) -> None:
        for leg in self._legs.values():
            if leg.logical_id != logical_id or (leg.exit_at is not None and not force):
                continue
            leg.exit_at = observed_at.astimezone(UTC)
            leg.exit_price = price
            leg.exit_reason = reason

    def restore_confirmation_evidence(
        self, *, source_fill_id: UUID, atr_state: str
    ) -> None:
        if source_fill_id in self._confirmation_event_fill_ids:
            return
        self._confirmation_event_fill_ids.add(source_fill_id)
        self._confirmation_evaluated += 1
        if atr_state.lower() == "long":
            self._confirmation_long += 1
        else:
            self._confirmation_fired += 1

    def on_quote(
        self,
        *,
        symbol: str,
        bid: Decimal | None,
        ask: Decimal | None,
        observed_at: datetime,
    ) -> list[PaperDecision]:
        normalized = symbol.upper()
        at = observed_at.astimezone(UTC)
        decisions: list[PaperDecision] = []
        if bid is None or bid <= 0:
            return decisions
        self._last_executable_bid[normalized] = (at, bid)
        tracker = self._halt_trackers.setdefault(normalized, LiveHaltTracker())
        halt = tracker.observe_quote(at)
        release_after_print = normalized in self._release_after_print
        if release_after_print:
            self._release_after_print.discard(normalized)
        elif halt.newly_confirmed and halt.last_print_at is not None:
            provisional = HaltWindow(halt.last_print_at, at, halt.quote_updates)
            decisions.extend(self._halt_window_decisions(normalized, provisional, confirmed_at=at))
            decisions.extend(self._suppression_decisions(normalized, provisional))
        flip_at = self._pending_flip_at.get(normalized)
        for identity, leg in self._legs.items():
            if leg.symbol != normalized or leg.exit_at is not None:
                continue
            target = leg.entry_price * (Decimal("1") + leg.config.target_pct / Decimal("100"))
            stop = leg.entry_price * (Decimal("1") - leg.config.stop_pct / Decimal("100"))
            reason = ""
            flip_precedes_quote = flip_at is not None and leg.entry_at <= flip_at < at
            flip_ties_quote = flip_at is not None and leg.entry_at <= flip_at == at
            if flip_precedes_quote:
                reason = "ATR_SELL"
            elif bid >= target:
                reason = "TARGET"
            elif bid <= stop:
                reason = "HARD_STOP"
            elif flip_ties_quote:
                reason = "ATR_SELL"
            elif at.astimezone(EASTERN).time() >= time(16, 0):
                reason = "16:00"
            pending = self._pending_halt_exits.get(identity)
            if reason:
                pending = self._stage_exit(identity, reason=reason, observed_at=at)
                if halt.state == "CONFIRMED" and halt.last_print_at is not None:
                    decisions.extend(
                        self._suppression_decisions(
                            normalized,
                            HaltWindow(halt.last_print_at, at, halt.quote_updates),
                        )
                    )
            if pending is None or not release_after_print:
                continue
            leg.exit_at = at
            leg.exit_price = bid
            leg.exit_reason = pending.reason
            self._pending_halt_exits.pop(identity, None)
            decision = self._decision(
                leg,
                "PAPER_EXIT",
                at,
                bid,
                reason=pending.reason,
                extra_detail={
                    "trigger_observed_at": pending.observed_at.isoformat(),
                    "execution_proof": "first quote after a later trade print",
                },
            )
            decisions.append(decision)
        if flip_at is not None and flip_at <= at and not any(
            leg.symbol == normalized and leg.exit_at is None for leg in self._legs.values()
        ):
            self._pending_flip_at.pop(normalized, None)
        return decisions

    def on_clock(self, observed_at: datetime) -> list[PaperDecision]:
        """Make a quote-less close explicit after the 16:00 event-time backstop."""
        now = observed_at.astimezone(UTC)
        now_et = now.astimezone(EASTERN)
        close_et = datetime.combine(now_et.date(), time(16, 0), tzinfo=EASTERN)
        if now_et < close_et:
            return []
        decisions: list[PaperDecision] = []
        close_utc = close_et.astimezone(UTC)
        for identity, leg in self._legs.items():
            if leg.exit_at is not None or leg.entry_at > close_utc:
                continue
            self._stage_exit(identity, reason="16:00", observed_at=close_utc)
            tracker = self._halt_trackers.get(leg.symbol)
            if tracker is not None and tracker.confirmed and tracker.last_print_at is not None:
                decisions.extend(
                    self._suppression_decisions(
                        leg.symbol,
                        HaltWindow(tracker.last_print_at, now, tracker.quote_updates),
                    )
                )
        if now_et < close_et + timedelta(minutes=1):
            return decisions
        for leg in self._legs.values():
            if leg.exit_at is not None or leg.entry_at > close_et.astimezone(UTC):
                continue
            latest = self._last_executable_bid.get(leg.symbol)
            pending = self._pending_halt_exits.get(leg.identity)
            if latest is not None and latest[0] >= close_et.astimezone(UTC) and pending is None:
                continue
            leg.exit_at = close_et.astimezone(UTC)
            leg.exit_reason = "UNANSWERABLE"
            self._pending_halt_exits.pop(leg.identity, None)
            decisions.append(
                self._decision(
                    leg,
                    "UNANSWERABLE",
                    close_et.astimezone(UTC),
                    None,
                    reason=(
                        "exit trigger remained inside an unresolved or confirmed print gap; "
                        "no post-reopen quote"
                        if pending is not None
                        else "no executable bid at or after 16:00 ET"
                    ),
                )
            )
        return decisions

    def _stage_exit(
        self, identity: str, *, reason: str, observed_at: datetime
    ) -> _PendingExit:
        candidate = _PendingExit(reason=reason, observed_at=observed_at.astimezone(UTC))
        current = self._pending_halt_exits.get(identity)
        if current is None or (
            candidate.observed_at,
            EXIT_REASON_PRIORITY[candidate.reason],
        ) < (
            current.observed_at,
            EXIT_REASON_PRIORITY[current.reason],
        ):
            self._pending_halt_exits[identity] = candidate
            return candidate
        return current

    def _halt_window_decisions(
        self, symbol: str, window: HaltWindow, *, confirmed_at: datetime
    ) -> list[PaperDecision]:
        event_key = f"HALT_CONFIRMED:{symbol}:{window.last_print_at.isoformat()}"
        if event_key in self._halt_event_keys:
            return []
        self._halt_event_keys.add(event_key)
        return [
            PaperDecision(
                event_key=event_key,
                logical_id=f"halt:{symbol}:{window.last_print_at.isoformat()}",
                arm=MIRROR_ARM,
                event_type="HALT_CONFIRMED",
                session_date=confirmed_at.astimezone(EASTERN).date(),
                symbol=symbol,
                observed_at=confirmed_at,
                price=None,
                quantity=None,
                config_id=None,
                detail={
                    "last_print_at": window.last_print_at.isoformat(),
                    "confirmed_at": confirmed_at.isoformat(),
                    "quote_updates": window.quote_updates,
                    "minimum_print_gap_seconds": int(HALT_MIN_PRINT_GAP.total_seconds()),
                    "minimum_quote_updates": HALT_MIN_QUOTE_UPDATES,
                },
            )
        ]

    def _suppression_decisions(
        self, symbol: str, window: HaltWindow
    ) -> list[PaperDecision]:
        decisions: list[PaperDecision] = []
        for identity, pending in self._pending_halt_exits.items():
            leg = self._legs.get(identity)
            if (
                leg is None
                or leg.symbol != symbol
                or leg.exit_at is not None
                or pending.observed_at <= window.last_print_at
            ):
                continue
            event_key = (
                f"HALT_TRIGGER_SUPPRESSED:{identity}:{pending.observed_at.isoformat()}:"
                f"{pending.reason}"
            )
            if event_key in self._halt_suppression_keys:
                continue
            self._halt_suppression_keys.add(event_key)
            decisions.append(
                PaperDecision(
                    event_key=event_key,
                    logical_id=leg.logical_id,
                    arm=leg.arm,
                    event_type="HALT_TRIGGER_SUPPRESSED",
                    session_date=leg.entry_at.astimezone(EASTERN).date(),
                    symbol=leg.symbol,
                    observed_at=window.reopen_print_at,
                    price=None,
                    quantity=leg.quantity,
                    config_id=leg.config.id,
                    venue=leg.venue,
                    source_fill_id=leg.source_fill_id,
                    broker_fill_id=leg.broker_fill_id,
                    detail={
                        "reason": pending.reason,
                        "trigger_observed_at": pending.observed_at.isoformat(),
                        "last_print_at": window.last_print_at.isoformat(),
                        "reopen_or_confirmation_at": window.reopen_print_at.isoformat(),
                    },
                )
            )
        return decisions

    def _decision(
        self,
        leg: _PaperLeg,
        event_type: str,
        at: datetime,
        price: Decimal | None,
        *,
        reason: str = "",
        extra_detail: Mapping[str, object] | None = None,
    ) -> PaperDecision:
        decision = PaperDecision(
            event_key=f"{event_type}:{leg.identity}:{at.isoformat()}",
            logical_id=leg.logical_id,
            arm=leg.arm,
            event_type=event_type,
            session_date=leg.entry_at.astimezone(EASTERN).date(),
            symbol=leg.symbol,
            observed_at=at,
            price=price,
            quantity=leg.quantity,
            config_id=leg.config.id,
            venue=leg.venue,
            source_fill_id=leg.source_fill_id,
            broker_fill_id=leg.broker_fill_id,
            detail={
                "reason": reason,
                "entry_price": str(leg.entry_price),
                "target_pct": str(leg.config.target_pct),
                "stop_pct": str(leg.config.stop_pct),
                "confirmation_bars": leg.config.confirmation_bars,
                "config_effective_at": leg.config.effective_at.isoformat(),
                **dict(extra_detail or {}),
            },
        )
        if event_type in {"PAPER_EXIT", "UNANSWERABLE"}:
            self._remember_terminal(decision)
        return decision

    def _remember_terminal(self, decision: PaperDecision) -> None:
        self._pending_terminal_decisions[decision.event_key] = decision

    def pending_terminal_decisions(self) -> list[PaperDecision]:
        return list(self._pending_terminal_decisions.values())

    def acknowledge_decisions(self, event_keys: set[str]) -> None:
        for event_key in event_keys:
            self._pending_terminal_decisions.pop(event_key, None)

    def summary(self) -> dict[str, object]:
        open_legs = [leg for leg in self._legs.values() if leg.exit_at is None]
        closed_legs = [leg for leg in self._legs.values() if leg.exit_at is not None]
        return {
            "strategy": PAPER_STRATEGY_CODE,
            "account_name": "paper:polygon_30s",
            "interval_secs": 30,
            "watchlist": sorted(self._watchlist),
            "prewarm_symbols": [],
            "positions": [
                {
                    "ticker": leg.symbol,
                    "quantity": float(leg.quantity),
                    "entry_price": float(leg.entry_price),
                    "entry_time": leg.entry_at.isoformat(),
                    "path": leg.arm,
                    "config_id": str(leg.config.id),
                }
                for leg in open_legs
            ],
            "pending_open_symbols": [],
            "pending_close_symbols": [],
            "pending_scale_levels": [],
            "daily_pnl": 0.0,
            "closed_today": [
                {
                    "ticker": leg.symbol,
                    "path": leg.arm,
                    "entry_price": float(leg.entry_price),
                    "exit_price": float(leg.exit_price or 0),
                    "entry_time": leg.entry_at.isoformat(),
                    "exit_time": leg.exit_at.isoformat() if leg.exit_at else "",
                    "reason": leg.exit_reason,
                }
                for leg in closed_legs
            ],
            "recent_decisions": [],
            "indicator_snapshots": [],
            "bar_counts": {},
            "last_tick_at": {},
            "data_health": {"status": "healthy"},
            "retention_states": [],
            "paper_exit": {
                "mirror_open": sum(leg.arm == MIRROR_ARM for leg in open_legs),
                "halt_suppression": {
                    "status": "MEASURED" if self._halt_event_keys else "UNEXERCISED",
                    "suppressed_triggers": len(self._halt_suppression_keys),
                    "confirmed_halts": len(self._halt_event_keys),
                    "denominator": len(self._halt_event_keys),
                },
                "confirmation_exit": {
                    "status": "MEASURED" if self._confirmation_evaluated else "UNEXERCISED",
                    "evaluated": self._confirmation_evaluated,
                    "fired": self._confirmation_fired,
                    "state_long": self._confirmation_long,
                    "denominator": self._confirmation_evaluated,
                },
                "config": {
                    "id": str(self._configs[-1].id),
                    "target_pct": str(self._configs[-1].target_pct),
                    "stop_pct": str(self._configs[-1].stop_pct),
                    "confirmation_bars": self._configs[-1].confirmation_bars,
                    "effective_at": self._configs[-1].effective_at.isoformat(),
                },
                "acceptance": dict(self._acceptance),
            },
        }

    def mirror_logical_ids(self) -> set[str]:
        return {leg.logical_id for leg in self._legs.values() if leg.arm == MIRROR_ARM}

    def has_fill(self, fill_id: UUID) -> bool:
        return fill_id in self._fill_ids

    def open_symbols(self) -> set[str]:
        return {leg.symbol for leg in self._legs.values() if leg.exit_at is None}
