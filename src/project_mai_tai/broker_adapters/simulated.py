from __future__ import annotations

from decimal import Decimal
from uuid import uuid4

from project_mai_tai.broker_adapters.protocols import ExecutionReport, OrderRequest


class SimulatedBrokerAdapter:
    async def submit_order(self, request: OrderRequest) -> list[ExecutionReport]:
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
