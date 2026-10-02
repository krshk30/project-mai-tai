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


def resting_wire_limit(
    stop: Decimal, limit: Decimal, *, leg: str, native_schwab_bracket: bool = False
) -> Decimal:
    """Price a resting buy on the same tick grid used by its broker submit path."""
    if leg == "schwab":
        if not native_schwab_bracket:
            return limit
        # The OMS native-bracket formatter uses float formatting, then lifts a
        # valid raw band one tick when rounding collapses it.
        wire_stop = Decimal(f"{float(stop):.2f}" if stop > 1 else f"{float(stop):.4f}")
        wire_limit = Decimal(f"{float(limit):.2f}" if limit > 1 else f"{float(limit):.4f}")
        if limit > stop and wire_limit <= wire_stop:
            tick = Decimal("0.0001") if wire_stop < 1 else Decimal("0.01")
            raised = wire_stop + tick
            wire_limit = Decimal(f"{float(raised):.2f}" if raised > 1 else f"{float(raised):.4f}")
        return wire_limit
    if leg == "webull":
        stop_tick = Decimal("0.01") if stop >= 1 else Decimal("0.0001")
        limit_tick = Decimal("0.01") if limit >= 1 else Decimal("0.0001")
        wire_stop = stop.quantize(stop_tick, rounding=ROUND_HALF_UP)
        wire_limit = limit.quantize(limit_tick, rounding=ROUND_HALF_UP)
        if limit > stop and wire_limit <= wire_stop:
            wire_limit = wire_stop + (Decimal("0.01") if wire_stop >= 1 else Decimal("0.0001"))
        return wire_limit
    raise ValueError(f"unknown entry leg: {leg}")
