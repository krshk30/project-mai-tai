"""Pure KILLSW1 cleanup decisions, never broker I/O or account-net ownership."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, time
from decimal import ROUND_FLOOR, Decimal
from uuid import UUID
from zoneinfo import ZoneInfo

from project_mai_tai.momentum_live_sec import US_MARKET_HOLIDAYS

_ET = ZoneInfo("America/New_York")


@dataclass(frozen=True)
class OwnedCleanup:
    strategy_id: UUID
    account_id: UUID
    symbol: str
    binding_key: str
    generation: int
    provider: str
    owner_proven: bool
    own_remaining: Decimal | None
    broker_available: Decimal | None
    observed_at: datetime | None
    entry_terminal: bool | None
    entry_order_ids: tuple[str, ...] = ()
    protection_terminal: bool | None = None
    protection_order_ids: tuple[str, ...] = ()
    exit_working_or_uncertain: bool = False
    bid: Decimal | None = None
    bid_at: datetime | None = None


@dataclass(frozen=True)
class CleanupPlan:
    action: str
    reason: str
    order_ids: tuple[str, ...] = ()
    quantity: Decimal | None = None
    order_type: str | None = None
    session: str | None = None
    limit_price: Decimal | None = None


def _fresh(stamp: datetime | None, now: datetime) -> bool:
    return (
        stamp is not None and stamp.tzinfo is not None and 0 <= (now - stamp).total_seconds() <= 2
    )


def _quantity(value: Decimal | None) -> bool:
    return isinstance(value, Decimal) and value.is_finite() and value >= 0


def _order_ids(values: tuple[str, ...]) -> bool:
    return (
        bool(values)
        and all(isinstance(value, str) and value and value == value.strip() for value in values)
        and len(set(values)) == len(values)
    )


def plan_cleanup(
    evidence: OwnedCleanup,
    *,
    strategy_id: UUID,
    account_id: UUID,
    generation: int,
    now: datetime,
) -> CleanupPlan:
    """Return one next operation; caller must re-read after EVERY await/action.

    A working/uncertain exit blocks even if a later snapshot says flat. No broker
    cancel ACK is converted into terminal proof. Callers must bind the returned
    IDs to the same exact durable owner before preparing an attempt.
    """
    if now.tzinfo is None:
        return CleanupPlan("BLOCKED", "clock_unknown")
    now = now.astimezone(UTC)
    if (
        not evidence.owner_proven
        or evidence.strategy_id != strategy_id
        or evidence.account_id != account_id
        or evidence.generation != generation
        or not evidence.binding_key
        or not evidence.symbol
    ):
        return CleanupPlan("BLOCKED", "owner_or_generation_unknown")
    if evidence.provider not in {"schwab", "webull"}:
        return CleanupPlan("BLOCKED", "provider_unsupported")
    if not _fresh(evidence.observed_at, now):
        return CleanupPlan("BLOCKED", "owner_evidence_not_fresh")
    if evidence.exit_working_or_uncertain:
        return CleanupPlan("WAIT", "existing_exit_requires_reconciliation")
    if evidence.entry_terminal is not True:
        if evidence.entry_terminal is False and _order_ids(evidence.entry_order_ids):
            return CleanupPlan("CANCEL_ENTRY", "owned_entries_first", evidence.entry_order_ids)
        return CleanupPlan("BLOCKED", "entry_terminal_unknown")
    if not _quantity(evidence.own_remaining):
        return CleanupPlan("BLOCKED", "owned_quantity_unknown")
    if evidence.own_remaining == 0:
        if evidence.protection_terminal is True:
            return CleanupPlan("SETTLED", "proven_owned_flat_and_terminal")
        if evidence.protection_terminal is False and _order_ids(evidence.protection_order_ids):
            return CleanupPlan(
                "CANCEL_PROTECTION", "flat_owner_remaining_children", evidence.protection_order_ids
            )
        return CleanupPlan("BLOCKED", "protection_terminal_unknown")
    if not _quantity(evidence.broker_available):
        return CleanupPlan("BLOCKED", "broker_available_unknown")
    local = now.astimezone(_ET)
    if (
        local.weekday() >= 5
        or local.date() in US_MARKET_HOLIDAYS
        or not time(4) <= local.time() < time(20)
        or (evidence.provider == "schwab" and local.time() < time(7))
    ):
        return CleanupPlan("BLOCKED", "broker_session_unsupported_keep_protection")
    regular = time(9, 30) <= local.time() < time(16)
    session = "NORMAL" if regular else "AM" if local.time() < time(9, 30) else "PM"
    limit = None
    if not regular:
        if (
            not isinstance(evidence.bid, Decimal)
            or not evidence.bid.is_finite()
            or evidence.bid <= 0
            or not _fresh(evidence.bid_at, now)
        ):
            return CleanupPlan("BLOCKED", "executable_bid_not_fresh_keep_protection")
        tick = Decimal("0.01") if evidence.bid >= 1 else Decimal("0.0001")
        limit = evidence.bid.quantize(tick, rounding=ROUND_FLOOR)
        if limit <= 0:
            return CleanupPlan("BLOCKED", "bid_below_tick_keep_protection")
    if evidence.protection_terminal is not True:
        if evidence.protection_terminal is False and _order_ids(evidence.protection_order_ids):
            return CleanupPlan(
                "CANCEL_PROTECTION", "executable_exit_ready", evidence.protection_order_ids
            )
        return CleanupPlan("BLOCKED", "protection_terminal_unknown")
    quantity = min(evidence.own_remaining, evidence.broker_available)
    if quantity == 0:
        return CleanupPlan("BLOCKED", "no_available_owned_quantity")
    return CleanupPlan(
        "SUBMIT_EXIT",
        "owned_close",
        quantity=quantity,
        order_type="market" if regular else "limit",
        session=session,
        limit_price=limit,
    )
