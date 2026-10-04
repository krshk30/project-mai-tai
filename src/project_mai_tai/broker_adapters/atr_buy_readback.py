"""Strict parent BUY evidence for RPG1, separate from generic execution reports.

An absent fill field is not zero. Cancellation activity is not execution. These
readbacks never submit an order or turn an unreadable order into permission.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from decimal import Decimal, InvalidOperation
from typing import Literal

from project_mai_tai.broker_adapters.protocols import OrderRequest


@dataclass(frozen=True)
class AtrBuyReadback:
    outcome: Literal["cancelled_empty", "fills", "working", "unknown"]
    reason: str
    cumulative_filled: Decimal | None = None
    fill_price: Decimal | None = None
    terminal_cancel: bool = False
    broker_status: str = ""
    broker_order_id: str | None = None

    @property
    def can_replace(self) -> bool:
        # _result emits cancelled_empty only for terminal cancellation. Keep the
        # explicit check as a defensive contract for independently constructed
        # readbacks; an outcome label alone is never replacement authority.
        return (
            self.outcome == "cancelled_empty"
            and self.terminal_cancel
            and self.cumulative_filled == Decimal(0)
        )


def unknown(reason: str) -> AtrBuyReadback:
    return AtrBuyReadback("unknown", reason)


def _number(value: object) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    return number if number.is_finite() and number >= 0 else None


def scoped_request(request: OrderRequest, *, allow_webull_client_identity: bool = False) -> bool:
    return (
        request.strategy_code == "schwab_1m_v2"
        and request.side == "buy"
        and request.intent_type == "cancel"
        and request.metadata.get("resting_entry_cancel") == "true"
        and (bool(request.metadata.get("broker_order_id", "").strip())
             or (allow_webull_client_identity
                 and request.metadata.get("atr_reprice_identity") == "webull_client_order_id"))
        and bool(request.client_order_id.strip())
        and request.quantity.is_finite()
        and request.quantity > 0
    )


def _result(status: str, filled: Decimal, price: Decimal | None) -> AtrBuyReadback:
    terminal = status in {"CANCELED", "CANCELLED"}
    # Any confirmed fill consumes the old opportunity. It never grants permission
    # to re-buy either the full amount or the remainder, even after cancellation.
    if filled > 0:
        return AtrBuyReadback("fills", "buy_has_fills", filled, price, terminal, status)
    if terminal:
        return AtrBuyReadback("cancelled_empty", "terminal_cancel_explicit_zero", filled,
                              terminal_cancel=True, broker_status=status)
    if status in {"WORKING", "ACCEPTED", "PENDING_CANCEL", "PENDING", "QUEUED", "SUBMITTED",
                  "AWAITING_PARENT_ORDER", "AWAITING_CONDITION"}:
        return AtrBuyReadback("working", "not_terminal_cancel", filled, broker_status=status)
    return unknown("unknown_or_inconsistent_status")


def schwab_buy_readback(request: OrderRequest, body: object) -> AtrBuyReadback:
    if not scoped_request(request):
        return unknown("not_atr_resting_buy_cancel")
    if not isinstance(body, dict):
        return unknown("unreadable_order")
    if str(body.get("orderId", "")) != request.metadata["broker_order_id"]:
        return unknown("parent_identity_mismatch")
    legs = body.get("orderLegCollection")
    if not isinstance(legs, list) or len(legs) != 1 or not isinstance(legs[0], dict):
        return unknown("unreadable_parent_leg")
    leg = legs[0]
    instrument = leg.get("instrument")
    if (
        not isinstance(instrument, dict)
        or instrument.get("symbol") != request.symbol
        or leg.get("instruction") != "BUY"
        or _number(body.get("quantity")) != request.quantity
        or _number(leg.get("quantity")) != request.quantity
    ):
        return unknown("parent_shape_mismatch")
    filled = _number(body.get("filledQuantity"))
    if filled is None or filled > request.quantity:
        return unknown("cumulative_fills_unreadable")
    activities = body.get("orderActivityCollection", [])
    if not isinstance(activities, list):
        return unknown("unreadable_activities")
    execution_qty = Decimal(0)
    execution_value = Decimal(0)
    for activity in activities:
        if not isinstance(activity, dict):
            return unknown("unreadable_activity")
        if activity.get("executionType") == "CANCELED":
            continue
        if activity.get("executionType") != "FILL":
            if activity.get("executionLegs"):
                return unknown("unrecognised_execution_type")
            continue
        executions = activity.get("executionLegs")
        if not isinstance(executions, list):
            return unknown("unreadable_executions")
        for execution in executions:
            if not isinstance(execution, dict):
                return unknown("unreadable_execution")
            qty, price = _number(execution.get("quantity")), _number(execution.get("price"))
            if qty is None or price is None or qty <= 0 or price <= 0:
                return unknown("unreadable_fill_execution")
            execution_qty += qty
            execution_value += qty * price
    if execution_qty > filled:
        return unknown("cumulative_fills_contradict_executions")
    price = execution_value / execution_qty if execution_qty == filled and filled > 0 else None
    return _result(str(body.get("status", "")).upper(), filled, price)


def webull_buy_readback(request: OrderRequest, body: object) -> AtrBuyReadback:
    if not scoped_request(request, allow_webull_client_identity=True):
        return unknown("not_atr_resting_buy_cancel")
    if not isinstance(body, dict):
        return unknown("unreadable_order")
    broker_id = str(body.get("order_id") or "").strip()
    expected_id = request.metadata.get("broker_order_id")
    if (
        body.get("client_order_id") != request.client_order_id
        or not broker_id
        or (expected_id and broker_id != expected_id)
    ):
        return unknown("parent_identity_mismatch")
    items = body.get("items")
    if not isinstance(items, list) or len(items) != 1 or not isinstance(items[0], dict):
        return unknown("unreadable_parent_item")
    item = items[0]
    if (
        item.get("symbol") != request.symbol
        or item.get("side") != "BUY"
        or _number(item.get("qty")) != request.quantity
    ):
        return unknown("parent_shape_mismatch")
    filled = _number(item.get("filled_qty"))
    if filled is None or filled > request.quantity:
        return unknown("cumulative_fills_unreadable")
    price = _number(item.get("filled_price"))
    if price == 0:
        price = None
    result = _result(str(item.get("order_status", "")).upper(), filled, price)
    return replace(result, broker_order_id=broker_id) if result.outcome != "unknown" else result
