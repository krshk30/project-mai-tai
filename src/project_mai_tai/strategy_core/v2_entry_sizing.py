"""Shared fixed-dollar sizing for the two schwab_1m_v2 entry legs."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP


def sized_entry_quantity(
    notional_usd: Decimal,
    price: Decimal | None,
    legacy_quantity: int,
    max_shares: int,
) -> int:
    """Return whole shares at the committed order price; zero notional is the off-switch."""
    if notional_usd < 0:
        raise ValueError("entry notional must be nonnegative")
    if notional_usd == 0:
        return legacy_quantity
    if max_shares < 1:
        raise ValueError("entry max shares must be positive")
    if price is None or not price.is_finite() or price <= 0:
        raise ValueError("entry price must be positive and finite")
    try:
        rounded = int((notional_usd / price).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    except (InvalidOperation, ZeroDivisionError) as exc:
        raise ValueError("entry price cannot be sized") from exc
    return min(max(rounded, 1), max_shares)
