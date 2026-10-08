"""Identity-bound entry-bar classification; no exit or entry-budget mutation.

These pure functions cannot send an order, change a stop, or refund an opportunity.
Runtime callers must additionally prove closure, cancellation and the durable budget.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation

from project_mai_tai.fanout_identity import fanout_slot_id
from project_mai_tai.fanout_segment_store import current_session_anchor


@dataclass(frozen=True)
class EntryIdentity:
    symbol: str
    account: str
    managed_row_id: str
    entry_order_id: str
    entry_client_order_id: str
    fill_order_id: str
    fill_client_order_id: str
    fill_ms: int
    opportunity_id: int
    slot_id: str
    managed_account: str
    managed_symbol: str
    fill_account: str
    fill_symbol: str


@dataclass(frozen=True)
class EntryBarClose:
    symbol: str
    bar_ms: int
    observed_at_ms: int
    close: Decimal
    trail: Decimal
    state: str
    observation_phase: str = "live"


@dataclass(frozen=True)
class Classification:
    kind: str
    reason: str
    identity: EntryIdentity
    bar: EntryBarClose

    @classmethod
    def from_payload(cls, payload: dict) -> Classification:
        if payload.get("schema_version") != 1 or payload.get("strategy_code") != "schwab_1m_v2":
            raise ValueError("entry classification source is unproven")
        identity = EntryIdentity(
            payload["symbol"], payload["account"], payload["managed_row_id"],
            payload["entry_order_id"], payload["entry_client_order_id"],
            payload["entry_order_id"], payload["entry_client_order_id"],
            int(payload["fill_ms"]), int(payload["opportunity_id"]), payload["slot_id"],
            payload["account"], payload["symbol"], payload["account"], payload["symbol"],
        )
        bar = EntryBarClose(payload["symbol"], int(payload["bar_ms"]),
                            int(payload["observed_at_ms"]), Decimal(payload["close"]),
                            Decimal(payload["trail"]), payload["state"])
        value = classify_entry(identity, bar)
        if value.kind != payload["classification"] or value.reason != payload["reason"]:
            raise ValueError("entry classification failed its proof check")
        return value

    def as_payload(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "strategy_code": "schwab_1m_v2",
            "symbol": self.identity.symbol,
            "account": self.identity.account,
            "managed_row_id": self.identity.managed_row_id,
            "entry_order_id": self.identity.entry_order_id,
            "entry_client_order_id": self.identity.entry_client_order_id,
            "fill_ms": self.identity.fill_ms,
            "opportunity_id": self.identity.opportunity_id,
            "slot_id": self.identity.slot_id,
            "bar_ms": self.bar.bar_ms,
            "observed_at_ms": self.bar.observed_at_ms,
            "close": str(self.bar.close),
            "trail": str(self.bar.trail),
            "state": self.bar.state,
            "classification": self.kind,
            "reason": self.reason,
        }


def classify_entry(identity: EntryIdentity, bar: EntryBarClose) -> Classification:
    """Only the exact filled entry and its completed live bar may classify a row."""
    reason = "unproven_entry_identity"
    kind = "UNKNOWN"
    if (
        not identity.symbol or identity.symbol != bar.symbol
        or not identity.account or not identity.managed_row_id
        or identity.managed_account != identity.account
        or identity.fill_account != identity.account
        or identity.managed_symbol != identity.symbol
        or identity.fill_symbol != identity.symbol
        or not identity.entry_order_id or not identity.entry_client_order_id
        or identity.entry_order_id != identity.fill_order_id
        or identity.entry_client_order_id != identity.fill_client_order_id
        or identity.opportunity_id <= 0 or identity.fill_ms <= 0
        or identity.slot_id != fanout_slot_id(
            strategy_code="schwab_1m_v2", symbol=identity.symbol,
            segment_id=identity.opportunity_id, slot="resting",
        )
    ):
        return Classification(kind, reason, identity, bar)
    if (bar.bar_ms <= 0 or bar.bar_ms % 60000
            or identity.fill_ms // 60000 * 60000 != bar.bar_ms
            or current_session_anchor(datetime.fromtimestamp(identity.fill_ms / 1000, UTC))
            != current_session_anchor(datetime.fromtimestamp(bar.bar_ms / 1000, UTC))):
        return Classification(kind, "entry_bar_mismatch", identity, bar)
    if bar.observation_phase != "live" or bar.observed_at_ms < bar.bar_ms + 60000:
        return Classification(kind, "entry_bar_not_closed_live", identity, bar)
    try:
        close, trail = Decimal(bar.close), Decimal(bar.trail)
        valid = close.is_finite() and trail.is_finite() and close > 0 and trail > 0
    except (InvalidOperation, TypeError, ValueError):
        valid = False
    if not valid:
        return Classification(kind, "entry_bar_price_unreadable", identity, bar)
    if bar.state == "short" and close < trail:
        return Classification("FALSE_FLIP", "entry_bar_closed_short", identity, bar)
    if bar.state == "long":
        return Classification("REAL_FLIP", "entry_bar_closed_long", identity, bar)
    return Classification(kind, "entry_bar_state_unproven", identity, bar)


def classified_exit_reason(classification: Classification, mechanism: str) -> str:
    """Preserve the execution mechanism while adding a proven classification label."""
    normalized = "_".join(str(mechanism).strip().lower().replace("-", "_").split())
    if classification.kind == "FALSE_FLIP":
        return f"false_flip_{normalized or 'unknown'}"
    return mechanism


def false_episode_proven_closed(
    classifications: tuple[Classification, ...],
    *,
    filled_accounts: frozenset[str],
    closed_rows: frozenset[tuple[str, str]],
) -> bool:
    """One false episode requires proof for every filled sibling, even across bars.

    This is classification proof only, not permission to place. Existing broker
    working-order, cancel-receipt, owner and manual-stop gates still own admission.
    """
    if not classifications or not filled_accounts:
        return False
    identities = [value.identity for value in classifications]
    if (
        any(value.kind != "FALSE_FLIP" or classify_entry(value.identity, value.bar) != value
            for value in classifications)
        or {value.account for value in identities} != filled_accounts
        or len({value.account for value in identities}) != len(identities)
        or len({(value.symbol, value.opportunity_id, value.slot_id)
                for value in identities}) != 1
    ):
        return False
    return all((value.account, value.managed_row_id) in closed_rows
               for value in identities)
