from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Literal, Protocol


def utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class OrderRequest:
    client_order_id: str
    broker_account_name: str
    strategy_code: str
    symbol: str
    side: Literal["buy", "sell"]
    intent_type: Literal["open", "scale", "close", "cancel"]
    quantity: Decimal
    reason: str
    metadata: dict[str, str] = field(default_factory=dict)
    order_type: str = "market"
    time_in_force: str = "day"


@dataclass(frozen=True)
class ExecutionReport:
    event_type: Literal["accepted", "rejected", "filled", "partially_filled", "cancelled"]
    client_order_id: str
    broker_order_id: str | None = None
    broker_fill_id: str | None = None
    symbol: str = ""
    side: Literal["buy", "sell"] = "buy"
    intent_type: Literal["open", "scale", "close", "cancel"] = "open"
    quantity: Decimal = Decimal("0")
    filled_quantity: Decimal = Decimal("0")
    fill_price: Decimal | None = None
    reason: str = ""
    metadata: dict[str, str] = field(default_factory=dict)
    reported_at: datetime = field(default_factory=utcnow)


class BrokerAdapter(Protocol):
    async def submit_order(self, request: OrderRequest) -> list[ExecutionReport]:
        """Submit an order and return the resulting execution reports."""
