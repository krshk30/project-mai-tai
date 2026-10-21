"""Per-submit read-only assertion carried through control and adapter awaits."""

from contextlib import contextmanager
from contextvars import ContextVar
from decimal import Decimal
import logging


_ENTRY_WIRE = ContextVar("mombuffer1_entry_wire", default=None)


def entry_wire_active():
    return _ENTRY_WIRE.get() is not None


@contextmanager
def entry_wire_check(request, validate):
    token = _ENTRY_WIRE.set((request, validate))
    try:
        yield
    finally:
        _ENTRY_WIRE.reset(token)


def validate_entry_wire(*, quantity, limit, symbol=None):
    bound = _ENTRY_WIRE.get()
    if bound is None:
        return
    request, validate = bound
    context = validate() or {}
    try:
        matches = (Decimal(str(quantity)) == request.quantity
                   and Decimal(str(limit)) == Decimal(request.metadata["limit_price"])
                   and (symbol is None or symbol == request.symbol))
    except (ArithmeticError, TypeError, ValueError):
        matches = False
    if not matches:
        logging.getLogger("momentum-live").warning(
            "[MOMLIVE1-REFUSED] symbol=%s reason=mombuffer1_serialized_wire_changed_no_dispatch "
            "ask=%s ask_age_ms=%s limit=%s cap=%s quantity=%s wire_limit=%s wire_quantity=%s "
            "settlement=not_authorized_after_submit",
            request.symbol, context.get("ask", "UNKNOWN"), context.get("ask_age_ms", "UNKNOWN"),
            request.metadata.get("limit_price", "UNKNOWN"), request.metadata.get("cap_price", "UNKNOWN"),
            request.quantity, limit, quantity,
        )
        raise RuntimeError("mombuffer1_serialized_wire_changed_no_dispatch")
