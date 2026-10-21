"""Canonical positive prices for bindings to database Numeric(18, 8) columns."""

from decimal import ROUND_HALF_UP, Decimal


def stored_price(value: Decimal | float | str) -> Decimal:
    price = Decimal(str(value))
    if not price.is_finite() or price <= 0:
        raise ValueError("stored price must be finite and positive")
    # Match the PostgreSQL scale, including ties; never compare with an epsilon.
    price = price.quantize(Decimal("0.00000001"), rounding=ROUND_HALF_UP)
    if price <= 0 or price >= Decimal("10000000000"):
        raise ValueError("stored price outside Numeric(18, 8)")
    return price
