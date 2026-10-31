"""Bounded list-open decoding under C1's 2026-10-29 11:58 source ruling.

Rows are inferred from retained detail/list-today records, not a retained
nonempty list-open response. Unknown row semantics hold every readable symbol.
An unreadable symbol, envelope, or continuation fails the account collection.
"""

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import re

WORKING = frozenset({"SUBMITTED", "PENDING", "WORKING", "ACCEPTED", "QUEUED",
                     "PARTIAL_FILLED", "PARTIAL FILLED", "PARTIALLY_FILLED"})
TERMINAL = frozenset({"FILLED", "CANCELLED", "CANCELED", "REJECTED", "EXPIRED", "FAILED"})


@dataclass(frozen=True)
class OpenOrder:
    symbols: frozenset[str]
    order_id: str | None
    client_order_id: str | None
    working: bool | None
    unknown_reason: str | None


def _identity(row, keys):
    values = [row[key] for key in keys if key in row]
    if (not values or any(not isinstance(v, str) or not v or len(v) > 128
                          or any(not 33 <= ord(c) <= 126 for c in v)
                          or v != values[0] for v in values)):
        return None
    return values[0]


def _quantity(row, keys):
    values = [row[key] for key in keys if key in row]
    if not values or any(isinstance(v, bool) or v is None for v in values):
        return None
    try:
        numbers = [Decimal(str(v)) for v in values]
    except (InvalidOperation, ValueError):
        return None
    if any(not v.is_finite() or v < 0 or v != numbers[0] for v in numbers):
        return None
    return numbers[0]


def _row(raw, native_account):
    items = raw.get("items") if isinstance(raw, dict) else None
    if not isinstance(items, list) or not 1 <= len(items) <= 32:
        raise ValueError("Webull_open_unlocated_symbol_UNKNOWN")
    located = set()
    for item in items:
        value = item.get("symbol") if isinstance(item, dict) else None
        if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9./^\-]{0,31}", value):
            raise ValueError("Webull_open_unlocated_symbol_UNKNOWN")
        located.add(value.upper())
    order_id = _identity(raw, ("order_id", "orderId"))
    client_id = _identity(raw, ("client_order_id", "clientOrderId"))

    def unknown(reason):
        return OpenOrder(frozenset(located), order_id, client_id, None, reason)

    if any(str(row[key]) != native_account for row in [raw, *items]
           for key in ("account_id", "accountId") if key in row):
        return unknown("foreign_account")
    if len(items) != 1:
        return unknown("items_not_exactly_one")
    item = items[0]
    if not order_id or not client_id:
        return unknown("order_identity")
    # A MASTER can hide working children even if its entry item is terminal.
    if (not isinstance(raw.get("combo_type"), str)
            or raw["combo_type"] not in {"NORMAL", "STOP_LOSS", "STOP_PROFIT"}):
        return unknown("combo_scope")
    if any(raw.get(key) or item.get(key) for key in ("children", "legs", "orders")):
        return unknown("nested_order_scope")
    statuses = [row[key] for row in (item, raw)
                for key in ("order_status", "status", "orderStatus") if key in row]
    item_status = any(key in item for key in ("order_status", "status", "orderStatus"))
    if (not item_status or not statuses
            or any(not isinstance(v, str) or v.upper() not in WORKING | TERMINAL for v in statuses)):
        return unknown("status_unknown")
    canonical = {v.upper().replace("PARTIAL FILLED", "PARTIAL_FILLED")
                 .replace("PARTIALLY_FILLED", "PARTIAL_FILLED")
                 .replace("CANCELED", "CANCELLED") for v in statuses}
    if len(canonical) != 1:
        return unknown("status_conflict")
    qty = _quantity(item, ("qty", "quantity"))
    filled = _quantity(item, ("filled_qty", "filledQty", "filled_quantity", "filledQuantity"))
    if (not isinstance(item.get("side"), str) or item["side"] not in {"BUY", "SELL", "SHORT"}
            or qty is None or qty <= 0 or filled is None or filled > qty):
        return unknown("side_or_qty_unknown")
    status = statuses[0].upper()
    if ((status in {"PARTIAL_FILLED", "PARTIAL FILLED", "PARTIALLY_FILLED"} and not 0 < filled < qty)
            or status == "FILLED" and filled != qty):
        return unknown("filled_quantity_conflict")
    return OpenOrder(frozenset(located), order_id, client_id, status in WORKING, None)


def open_order_page(body, *, native_account, request_account):
    """Return decoded rows, explicit continuation, and its proven last-row cursor."""
    if (not isinstance(native_account, str) or not native_account
            or request_account != native_account or not isinstance(body, dict)
            or "success" in body and body["success"] is not True
            or body.get("error_code") or any(k in body for k in ("error", "errors", "errorCode"))):
        raise ValueError("Webull_open_envelope_or_request_binding_UNKNOWN")
    if any(str(body[key]) != native_account for key in ("account_id", "accountId") if key in body):
        raise ValueError("Webull_open_envelope_account_UNKNOWN")
    orders = body.get("orders")
    flags = [body[key] for key in ("has_next", "hasNext") if key in body]
    if (not isinstance(orders, list) or len(orders) > 100 or not flags
            or any(type(v) is not bool or v != flags[0] for v in flags)):
        raise ValueError("Webull_open_continuation_UNKNOWN")
    if "pageSize" in body and (type(body["pageSize"]) is not int or not 0 <= body["pageSize"] <= 100):
        raise ValueError("Webull_open_page_size_UNKNOWN")
    if not orders:
        # Preserve the actual terminal-empty proof; missing metadata is not flat.
        if (flags[0] or type(body.get("pageSize")) is not int or body["pageSize"] != 0
                or set(body) - {"orders", "has_next", "hasNext", "pageSize", "account_id", "accountId"}):
            raise ValueError("Webull_open_terminal_empty_shape_UNKNOWN")
        return [], False, None
    rows = [_row(raw, native_account) for raw in orders]
    cursor = rows[-1].client_order_id if flags[0] else None
    if flags[0] and (not cursor or rows[-1].unknown_reason == "foreign_account"):
        raise ValueError("Webull_open_cursor_UNKNOWN")
    return rows, flags[0], cursor
