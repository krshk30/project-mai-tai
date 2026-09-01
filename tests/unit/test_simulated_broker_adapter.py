from __future__ import annotations

from decimal import Decimal

import pytest

from project_mai_tai.broker_adapters.protocols import OrderRequest
from project_mai_tai.broker_adapters.simulated import SimulatedBrokerAdapter


def request(*, intent_type: str = "open", metadata: dict | None = None) -> OrderRequest:
    return OrderRequest(
        client_order_id=f"paper-TEST-{intent_type}-1",
        broker_account_name="paper:polygon_30s",
        strategy_code="polygon_30s",
        symbol="TEST",
        side="buy",
        intent_type=intent_type,  # type: ignore[arg-type]
        quantity=Decimal("1"),
        reason="TEST",
        metadata=metadata or {},
    )


@pytest.mark.asyncio
async def test_missing_reference_price_is_a_client_abort() -> None:
    reports = await SimulatedBrokerAdapter().submit_order(request())

    assert len(reports) == 1
    assert reports[0].event_type == "rejected"
    assert reports[0].origin == "client"
    assert reports[0].reason == "missing reference_price"


@pytest.mark.asyncio
async def test_simulated_cancel_is_a_client_abort() -> None:
    reports = await SimulatedBrokerAdapter().submit_order(request(intent_type="cancel"))

    assert len(reports) == 1
    assert reports[0].event_type == "rejected"
    assert reports[0].origin == "client"
    assert "fills immediately" in reports[0].reason
