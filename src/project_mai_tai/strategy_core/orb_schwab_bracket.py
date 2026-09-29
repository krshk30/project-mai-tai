"""Pure price plan for a two-share RTH ORB Schwab stop-limit bracket.

This module does not create an intent or contact a broker. The STOP_LIMIT OTOCO
shape still requires a Schwab preview before the live route can be enabled.
"""

from __future__ import annotations

from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP, ROUND_UP


def _tick(price: Decimal) -> Decimal:
    return Decimal("0.01") if price > 1 else Decimal("0.0001")


def _price(value: Decimal, rounding: str) -> Decimal:
    return value.quantize(_tick(value), rounding=rounding)


def build_orb_schwab_bracket_metadata(breakout_level: Decimal) -> dict[str, str]:
    """Anchor +5%/-8% exits to the rounded trigger; cap the buy at +0.5%."""
    if not breakout_level.is_finite() or breakout_level <= 0:
        raise ValueError("ORB breakout level must be finite and positive")

    trigger = _price(breakout_level, ROUND_UP)
    cap = _price(breakout_level * Decimal("1.005"), ROUND_DOWN)
    if cap < trigger:
        raise ValueError("ORB buy cap rounds below the broker trigger")
    target = _price(trigger * Decimal("1.05"), ROUND_HALF_UP)
    protect = _price(trigger * Decimal("0.92"), ROUND_HALF_UP)
    if not protect < trigger <= cap < target:
        raise ValueError("ORB bracket prices are not ordered safely")

    return {
        "orb_entry": "true",
        "execution_mode": "fixed_opening_high_resting",
        "order_type": "STOP_LIMIT",
        "time_in_force": "day",
        "stop_price": format(trigger, "f"),
        "limit_price": format(cap, "f"),
        "reference_price": format(trigger, "f"),
        "bracket": "true",
        "native_oco_bracket": "true",
        "bracket_entry_type": "STOP_LIMIT",
        "bracket_target_price": format(target, "f"),
        "bracket_stop_price": format(protect, "f"),
        "orb_exit_anchor": "broker_trigger_not_actual_fill",
        "orb_buy_cap_pct": "0.5",
        "orb_target_pct": "5",
        "orb_stop_pct": "8",
    }
