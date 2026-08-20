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
    # ⛔⭐⭐ Q1 — WHO REFUSED. `event_type="rejected"` is written both when the BROKER refused a
    # request we sent and when WE abandoned one that never left the process. Collapsed into one
    # word, every reject count is contaminated, and "the broker is rejecting us" and "we are
    # aborting our own orders" read identically — two findings that point at different code.
    #
    #   "broker"  — a response came back from the venue and it was a refusal (HTTP >= 400, or a
    #               status the broker reported). The order EXISTED at the broker.
    #   "client"  — we never got that far: a pre-flight guard, a missing account or order id, a
    #               payload we could not build, a transport error, or an abandon/cancel WE chose.
    #   "unknown" — not yet classified. ⛔ THE DEFAULT ON PURPOSE.
    #
    # ⛔ The default is "unknown", never "broker". A site nobody has labelled must not silently
    # acquire the label that carries blame — that is how the original contamination happened, one
    # convenient default at a time. An honest gap is countable; a confident wrong label is not.
    origin: Literal["broker", "client", "unknown"] = "unknown"


@dataclass(frozen=True)
class BrokerPositionSnapshot:
    broker_account_name: str
    symbol: str
    quantity: Decimal
    average_price: Decimal
    market_value: Decimal | None = None
    as_of: datetime = field(default_factory=utcnow)


class BrokerAdapter(Protocol):
    async def submit_order(self, request: OrderRequest) -> list[ExecutionReport]:
        """Submit an order and return the resulting execution reports."""

    async def fetch_order_update(self, request: OrderRequest) -> ExecutionReport | None:
        """Return the broker's current status for an existing order, if available."""

    async def list_account_positions(self, broker_account_name: str) -> list[BrokerPositionSnapshot]:
        """Return the broker's current position snapshots for a specific account."""
