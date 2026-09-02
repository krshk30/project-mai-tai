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

EASTERN = ZoneInfo("America/New_York")
PAPER_STRATEGY_CODE = "polygon_30s"
MIRROR_ARM = "mirror"
INDEPENDENT_ARM = "independent"
ACCEPTED_RESTING_SOURCES = frozenset(
    {"cw-v2-resting", "rth_resting", "rth_resting_mirror", "eh_resting"}
)


@dataclass(frozen=True)
class PaperRuleConfig:
    id: UUID
    target_pct: Decimal
    stop_pct: Decimal
    effective_at: datetime

    def __post_init__(self) -> None:
        if self.target_pct <= 0 or self.stop_pct <= 0:
            raise ValueError("paper target and stop percentages must be positive")
        if self.effective_at.tzinfo is None:
            raise ValueError("paper config effective_at must be timezone-aware")


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
    source: str

    def __post_init__(self) -> None:
        if not self.broker_fill_id.strip():
            raise ValueError("mirror fill requires a broker_fill_id")
        if not self.fanout_slot_id.strip():
            raise ValueError("mirror fill requires a fanout_slot_id")
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
    arm: Literal["mirror", "independent"]
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
    arm: Literal["mirror", "independent"]
    symbol: str
    venue: str
    entry_at: datetime
    entry_price: Decimal
    quantity: Decimal
    config: PaperRuleConfig
    source_fill_id: UUID | None = None
    broker_fill_id: str | None = None
    source_legs: dict[UUID, PaperSourceFill] = field(default_factory=dict)
    independent_attempt_id: str = ""
    exit_at: datetime | None = None
    exit_price: Decimal | None = None
    exit_reason: str = ""


@dataclass
class _IndependentArm:
    attempt_id: str
    symbol: str
    level: Decimal
    armed_at: datetime


def _source_from_metadata(metadata: Mapping[str, object]) -> str:
    sources = {
        str(metadata.get(key, "")).strip().lower()
        for key in ("fanout_source", "atr_variant")
        if str(metadata.get(key, "")).strip()
    }
    accepted = sources & ACCEPTED_RESTING_SOURCES
    rejected = sources - ACCEPTED_RESTING_SOURCES
    if accepted and rejected:
        return "conflicting:" + ",".join(sorted(sources))
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
    raw = f"mirror:{fill.session_date.isoformat()}:{fill.symbol.upper()}:{fill.fanout_slot_id}"
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
    """Require terminal evidence for the final collapsed position, not an earlier partial leg."""
    fill_at = final_fill_at.replace(tzinfo=final_fill_at.tzinfo or UTC).astimezone(UTC)
    exit_at = terminal_at.replace(tzinfo=terminal_at.tzinfo or UTC).astimezone(UTC)
    return exit_at >= fill_at and terminal_quantity == final_quantity


class PaperExitRuntime:
    """Owns paper state only; every externally visible action is a ``PaperDecision``."""

    def __init__(self, config: PaperRuleConfig) -> None:
        self._configs = [config]
        self._legs: dict[str, _PaperLeg] = {}
        self._fill_ids: set[UUID] = set()
        self._mirror_identity_by_logical_id: dict[str, str] = {}
        self._pending_flip_at: dict[str, datetime] = {}
        self._last_executable_bid: dict[str, tuple[datetime, Decimal]] = {}
        self._pending_terminal_decisions: dict[str, PaperDecision] = {}
        self._independent_arms: dict[str, _IndependentArm] = {}
        self._independent_attempt_ids: set[str] = set()
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
                        "reason": "durable entry evidence retry",
                    },
                )
            ]
        config = self.config_at(fill.filled_at)
        logical_id = logical_mirror_id(fill)
        identity = f"mirror:{fill.fill_id}"
        self._fill_ids.add(fill.fill_id)
        existing_identity = self._mirror_identity_by_logical_id.get(logical_id)
        if existing_identity is not None:
            existing = self._legs[existing_identity]
            existing.source_legs[fill.fill_id] = fill
            legs = sorted(existing.source_legs.values(), key=lambda item: (item.filled_at, str(item.fill_id)))
            total_quantity = sum((item.quantity for item in legs), Decimal("0"))
            existing.entry_price = (
                sum((item.price * item.quantity for item in legs), Decimal("0"))
                / total_quantity
            )
            existing.quantity = total_quantity
            existing.entry_at = legs[0].filled_at.astimezone(UTC)
            existing.config = self.config_at(existing.entry_at)
            existing.source_fill_id = legs[0].fill_id
            existing.broker_fill_id = legs[0].broker_fill_id
            existing.venue = (
                legs[0].venue if len({item.venue for item in legs}) == 1 else "both"
            )
            return [
                PaperDecision(
                    event_key=f"MIRROR_LEG_COLLAPSED:{fill.fill_id}",
                    logical_id=logical_id,
                    arm=MIRROR_ARM,
                    event_type="MIRROR_LEG_COLLAPSED",
                    session_date=fill.session_date,
                    symbol=fill.symbol.upper(),
                    observed_at=fill.filled_at.astimezone(UTC),
                    price=fill.price,
                    quantity=fill.quantity,
                    config_id=existing.config.id,
                    venue=fill.venue,
                    source_fill_id=fill.fill_id,
                    broker_fill_id=fill.broker_fill_id,
                    detail={
                        "source": fill.source,
                        "fanout_slot_id": fill.fanout_slot_id,
                        "collapsed_into_fill_id": str(existing.source_fill_id),
                        "logical_quantity": str(existing.quantity),
                        "weighted_entry_price": str(existing.entry_price),
                        "source_fill_ids": [str(item.fill_id) for item in legs],
                    },
                )
            ]
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
        self._mirror_identity_by_logical_id[logical_id] = identity
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
                detail={"source": fill.source, "fanout_slot_id": fill.fanout_slot_id},
            )
        ]

    def arm_independent(
        self, *, attempt_id: str, symbol: str, level: Decimal, armed_at: datetime
    ) -> PaperDecision | None:
        normalized = symbol.upper()
        if not attempt_id.strip() or level <= 0:
            return None
        if attempt_id in self._independent_arms:
            return None
        if any(
            leg.arm == INDEPENDENT_ARM and leg.symbol == normalized and leg.exit_at is None
            for leg in self._legs.values()
        ):
            return None
        if any(arm.symbol == normalized for arm in self._independent_arms.values()):
            return None
        at = armed_at.astimezone(UTC)
        self._independent_arms[attempt_id] = _IndependentArm(
            attempt_id=attempt_id,
            symbol=normalized,
            level=level,
            armed_at=at,
        )
        self._independent_attempt_ids.add(attempt_id)
        return PaperDecision(
            event_key=f"INDEPENDENT_ARMED:{attempt_id}",
            logical_id=f"independent-arm:{attempt_id}",
            arm=INDEPENDENT_ARM,
            event_type="INDEPENDENT_ARMED",
            session_date=at.astimezone(EASTERN).date(),
            symbol=normalized,
            observed_at=at,
            price=level,
            quantity=None,
            config_id=None,
            detail={"attempt_id": attempt_id, "level": str(level)},
        )

    def restore_independent_arm(
        self, *, attempt_id: str, symbol: str, level: Decimal, armed_at: datetime
    ) -> None:
        self.arm_independent(
            attempt_id=attempt_id,
            symbol=symbol,
            level=level,
            armed_at=armed_at,
        )

    def restore_independent_entry(
        self,
        *,
        attempt_id: str,
        logical_id: str,
        symbol: str,
        entered_at: datetime,
        price: Decimal,
        quantity: Decimal,
    ) -> None:
        identity = f"independent:{attempt_id}"
        if identity in self._legs:
            return
        self._legs[identity] = _PaperLeg(
            identity=identity,
            logical_id=logical_id,
            arm=INDEPENDENT_ARM,
            symbol=symbol.upper(),
            venue="modelled",
            entry_at=entered_at.astimezone(UTC),
            entry_price=price,
            quantity=quantity,
            config=self.config_at(entered_at),
            independent_attempt_id=attempt_id,
        )
        self._independent_attempt_ids.add(attempt_id)
        self._independent_arms.pop(attempt_id, None)

    def mark_atr_sell(self, symbol: str, observed_at: datetime) -> None:
        self._pending_flip_at[symbol.upper()] = observed_at.astimezone(UTC)

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
        return decisions

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
        if ask is not None and ask > 0:
            for attempt_id, arm in list(self._independent_arms.items()):
                if arm.symbol != normalized or ask < arm.level:
                    continue
                config = self.config_at(at)
                identity = f"independent:{attempt_id}"
                logical_id = sha256(identity.encode("utf-8")).hexdigest()
                self._legs[identity] = _PaperLeg(
                    identity=identity,
                    logical_id=logical_id,
                    arm=INDEPENDENT_ARM,
                    symbol=normalized,
                    venue="modelled",
                    entry_at=at,
                    entry_price=ask,
                    quantity=Decimal("1"),
                    config=config,
                    independent_attempt_id=attempt_id,
                )
                decisions.append(
                    self._decision(self._legs[identity], "INDEPENDENT_ENTRY", at, ask)
                )
                self._independent_arms.pop(attempt_id, None)

        if bid is None or bid <= 0:
            return decisions
        self._last_executable_bid[normalized] = (at, bid)
        flip_at = self._pending_flip_at.get(normalized)
        for leg in self._legs.values():
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
            if not reason:
                continue
            leg.exit_at = at
            leg.exit_price = bid
            leg.exit_reason = reason
            decisions.append(self._decision(leg, "PAPER_EXIT", at, bid, reason=reason))
        if flip_at is not None and flip_at <= at:
            self._pending_flip_at.pop(normalized, None)
        return decisions

    def on_clock(self, observed_at: datetime) -> list[PaperDecision]:
        """Make a quote-less close explicit after the 16:00 event-time backstop."""
        now = observed_at.astimezone(UTC)
        now_et = now.astimezone(EASTERN)
        close_et = datetime.combine(now_et.date(), time(16, 0), tzinfo=EASTERN)
        if now_et < close_et + timedelta(minutes=1):
            return []
        decisions: list[PaperDecision] = []
        for leg in self._legs.values():
            if leg.exit_at is not None or leg.entry_at > close_et.astimezone(UTC):
                continue
            latest = self._last_executable_bid.get(leg.symbol)
            if latest is not None and latest[0] >= close_et.astimezone(UTC):
                continue
            leg.exit_at = close_et.astimezone(UTC)
            leg.exit_reason = "UNANSWERABLE"
            decisions.append(
                self._decision(
                    leg,
                    "UNANSWERABLE",
                    close_et.astimezone(UTC),
                    None,
                    reason="no executable bid at or after 16:00 ET",
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
                "config_effective_at": leg.config.effective_at.isoformat(),
                "independent_attempt_id": leg.independent_attempt_id,
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
            "pending_open_symbols": sorted({arm.symbol for arm in self._independent_arms.values()}),
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
                "independent_open": sum(leg.arm == INDEPENDENT_ARM for leg in open_legs),
                "independent": {
                    "armed": len(self._independent_attempt_ids),
                    "filled": sum(
                        leg.arm == INDEPENDENT_ARM for leg in self._legs.values()
                    ),
                    "pending": len(self._independent_arms),
                    "closed": sum(
                        leg.arm == INDEPENDENT_ARM for leg in closed_legs
                    ),
                },
                "config": {
                    "id": str(self._configs[-1].id),
                    "target_pct": str(self._configs[-1].target_pct),
                    "stop_pct": str(self._configs[-1].stop_pct),
                    "effective_at": self._configs[-1].effective_at.isoformat(),
                },
                "acceptance": dict(self._acceptance),
            },
        }

    def mirror_logical_ids(self) -> set[str]:
        return set(self._mirror_identity_by_logical_id)

    def has_fill(self, fill_id: UUID) -> bool:
        return fill_id in self._fill_ids

    def open_symbols(self) -> set[str]:
        return {leg.symbol for leg in self._legs.values() if leg.exit_at is None}
