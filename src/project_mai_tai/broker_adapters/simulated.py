from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any
from uuid import uuid4

from project_mai_tai.broker_adapters.protocols import (
    BrokerPositionSnapshot,
    ExecutionReport,
    OrderRequest,
)


@dataclass
class _PositionState:
    quantity: Decimal = Decimal("0")
    average_price: Decimal = Decimal("0")


class SimulatedBrokerAdapter:
    def __init__(self) -> None:
        self._positions: dict[str, dict[str, _PositionState]] = {}

    async def submit_order(self, request: OrderRequest) -> list[ExecutionReport]:
        if request.intent_type == "cancel":
            return [
                ExecutionReport(
                    event_type="rejected",
                    client_order_id=request.client_order_id,
                    broker_order_id=str(request.metadata.get("broker_order_id", "")).strip() or None,
                    symbol=request.symbol,
                    side=request.side,
                    intent_type="cancel",
                    quantity=request.quantity,
                    reason="simulated adapter fills immediately; no open order remains to cancel",
                    metadata=dict(request.metadata),
                )
            ]

        reference_price = request.metadata.get("reference_price")
        broker_order_id = f"sim-order-{uuid4().hex[:16]}"

        if reference_price is None or reference_price == "":
            return [
                ExecutionReport(
                    event_type="rejected",
                    client_order_id=request.client_order_id,
                    broker_order_id=broker_order_id,
                    symbol=request.symbol,
                    side=request.side,
                    intent_type=request.intent_type,
                    quantity=request.quantity,
                    reason="missing reference_price",
                    metadata=dict(request.metadata),
                )
            ]

        fill_price = Decimal(str(reference_price))
        self._apply_fill(
            broker_account_name=request.broker_account_name,
            symbol=request.symbol,
            side=request.side,
            quantity=request.quantity,
            price=fill_price,
        )
        return [
            ExecutionReport(
                event_type="accepted",
                client_order_id=request.client_order_id,
                broker_order_id=broker_order_id,
                symbol=request.symbol,
                side=request.side,
                intent_type=request.intent_type,
                quantity=request.quantity,
                reason=request.reason,
                metadata=dict(request.metadata),
            ),
            ExecutionReport(
                event_type="filled",
                client_order_id=request.client_order_id,
                broker_order_id=broker_order_id,
                broker_fill_id=f"{broker_order_id}-fill-1",
                symbol=request.symbol,
                side=request.side,
                intent_type=request.intent_type,
                quantity=request.quantity,
                filled_quantity=request.quantity,
                fill_price=fill_price,
                reason=request.reason,
                metadata=dict(request.metadata),
            ),
        ]

    async def fetch_order_update(self, request: OrderRequest) -> ExecutionReport | None:
        del request
        return None

    async def list_account_positions(self, broker_account_name: str) -> list[BrokerPositionSnapshot]:
        account_positions = self._positions.get(broker_account_name, {})
        snapshots: list[BrokerPositionSnapshot] = []
        for symbol, state in sorted(account_positions.items()):
            if state.quantity <= 0:
                continue
            snapshots.append(
                BrokerPositionSnapshot(
                    broker_account_name=broker_account_name,
                    symbol=symbol,
                    quantity=state.quantity,
                    average_price=state.average_price,
                    market_value=state.quantity * state.average_price,
                )
            )
        return snapshots

    def _apply_fill(
        self,
        *,
        broker_account_name: str,
        symbol: str,
        side: str,
        quantity: Decimal,
        price: Decimal,
    ) -> None:
        account_positions = self._positions.setdefault(broker_account_name, {})
        position = account_positions.setdefault(symbol, _PositionState())

        if side == "buy":
            new_quantity = position.quantity + quantity
            if new_quantity > 0:
                weighted_cost = position.average_price * position.quantity + price * quantity
                position.average_price = weighted_cost / new_quantity
            position.quantity = new_quantity
            return

        sell_quantity = min(position.quantity, quantity)
        position.quantity -= sell_quantity
        if position.quantity <= 0:
            position.quantity = Decimal("0")
            position.average_price = Decimal("0")

    def seed_account_positions(
        self,
        broker_account_name: str,
        positions: dict[str, dict[str, Any]],
    ) -> None:
        account_positions: dict[str, _PositionState] = {}
        for symbol, raw in positions.items():
            account_positions[symbol] = _PositionState(
                quantity=Decimal(str(raw.get("quantity", "0"))),
                average_price=Decimal(str(raw.get("average_price", "0"))),
            )
        self._positions[broker_account_name] = account_positions
