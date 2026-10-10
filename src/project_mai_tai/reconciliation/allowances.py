"""ALERTS1 A2: reviewed standing allowances for operator-closed reconciliation findings.

Each allowance names EXACTLY one historical mismatch the operator has ruled on. It downgrades
that one finding from critical to info; it never hides it. Every field must match, so a NEW
mismatch on the same symbol (different quantity, a later fill, a broker position, a live book
row) or the same mismatch on any other symbol/account still reports critical.

Adding a row here is a rule change: it needs the operator's ruling and a review.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

EASTERN_TZ = ZoneInfo("America/New_York")


@dataclass(frozen=True)
class PositionMismatchAllowance:
    allowance_id: str
    account_name: str
    symbol: str
    direction: str
    net_fill_balance: Decimal
    last_fill_date_et: date
    ruling: str


OPERATOR_CLOSED_POSITION_ALLOWANCES: tuple[PositionMismatchAllowance, ...] = (
    PositionMismatchAllowance(
        allowance_id="ALERTS1-MI-20261005",
        account_name="live:schwab_1m_v2",
        symbol="MI",
        direction="broker_missing_owned_position",
        net_fill_balance=Decimal("180"),
        last_fill_date_et=date(2026, 10, 5),
        ruling="operator 10-05: unrecorded +180 Schwab sale; leave it alone",
    ),
    PositionMismatchAllowance(
        allowance_id="ALERTS1-NXL-20261001",
        account_name="live:schwab_1m_v2",
        symbol="NXL",
        direction="broker_missing_owned_position",
        net_fill_balance=Decimal("2"),
        last_fill_date_et=date(2026, 10, 1),
        ruling="operator 10-01: +2 manual close; leave it alone",
    ),
)

ALLOWANCE_KEYS: frozenset[tuple[str, str]] = frozenset(
    (allowance.account_name, allowance.symbol) for allowance in OPERATOR_CLOSED_POSITION_ALLOWANCES
)


def match_position_allowance(
    *,
    account_name: str,
    symbol: str,
    direction: str,
    account_quantity: Decimal,
    our_quantity: Decimal,
    net_fill_balance: Decimal,
    last_fill_at: datetime | None,
    allowances: tuple[PositionMismatchAllowance, ...] = OPERATOR_CLOSED_POSITION_ALLOWANCES,
) -> PositionMismatchAllowance | None:
    """Return the allowance only when the finding is exactly the ruled historical item."""
    if last_fill_at is None or account_quantity != 0 or our_quantity != 0:
        return None
    if last_fill_at.tzinfo is None:
        # SQLite drops tzinfo; the column is stored as UTC.
        last_fill_at = last_fill_at.replace(tzinfo=UTC)
    last_fill_date = last_fill_at.astimezone(EASTERN_TZ).date()
    for allowance in allowances:
        if (
            allowance.account_name == account_name
            and allowance.symbol == symbol.upper()
            and allowance.direction == direction
            and allowance.net_fill_balance == net_fill_balance
            and allowance.last_fill_date_et == last_fill_date
        ):
            return allowance
    return None
