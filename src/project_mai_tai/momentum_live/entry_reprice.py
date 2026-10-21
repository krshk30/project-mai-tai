"""Price-only changes before dispatch; never a broker replace or retry."""

from dataclasses import replace
from decimal import Decimal, ROUND_FLOOR

from project_mai_tai.momentum_live.admission import Refusal, Route


PRICE_FIELDS = ("limit_price", "reference_price")
MAX_PRICE_UPDATES = 2


class UnsentEntryConflict(Refusal):
    """The durable owner changed; do not try client-only terminal settlement."""


def validate_entry_reprice(previous, current, route: Route) -> None:
    try:
        metadata = {**previous.metadata, **{key: current.metadata[key] for key in PRICE_FIELDS}}
        if replace(previous, quantity=current.quantity, metadata=metadata) != current:
            raise Refusal("mombuffer1_sealed_entry_changed")
        limit = Decimal(current.metadata["limit_price"])
        cap = Decimal(current.metadata["cap_price"])
        signal = Decimal(current.metadata["signal_price"])
        tick = Decimal(".0001") if limit <= 1 else Decimal(".01")
        if (current.side != "buy" or current.intent_type != "open" or current.order_type != "limit"
                or current.time_in_force != "day" or current.metadata.get("momlive_one_shot") != "true"
                or not limit.is_finite() or not cap.is_finite() or not signal.is_finite()
                or not 0 < limit <= cap or cap != signal * Decimal("1.015")
                or limit % tick or Decimal(current.metadata["reference_price"]) != limit
                or current.quantity != max(1, int((Decimal(100) / limit).to_integral_value(rounding=ROUND_FLOOR)))
                or route == Route.WEBULL and limit < 1 and current.quantity < 100
                or "bot_control_permit" in previous.metadata or "bot_control_permit" in current.metadata):
            raise Refusal("mombuffer1_wire_invariants_invalid")
    except (KeyError, ArithmeticError, TypeError) as exc:
        raise Refusal("mombuffer1_wire_invariants_invalid") from exc


def price_changed(previous, current) -> bool:
    return previous.quantity != current.quantity or any(
        previous.metadata.get(key) != current.metadata.get(key) for key in PRICE_FIELDS)
