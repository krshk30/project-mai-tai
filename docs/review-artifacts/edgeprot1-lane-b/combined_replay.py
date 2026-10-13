"""Controlled E1+B composition; archived prints are opportunities, not broker fills."""
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import select

from project_mai_tai.db.models import Fill
from tests.unit.test_edgeprot1_webull_edge import ACCT, SYM, replay


@pytest.mark.asyncio
async def test_combined_mndr_owned_pair_target_floor_and_no_duplicate_sell(monkeypatch):
    fixture = replay.__wrapped__(monkeypatch)
    service, factory, wire = next(fixture)
    try:
        await service._v2_rth_edge_bracket()
        target, stop = wire.legs[0]
        assert target["limit_price"] == "2.62" and target["quantity"] == "120"
        assert target["support_trading_session"] == "CORE"
        assert stop["stop_price"] == "2.29" and stop["order_type"] == "STOP_LOSS"
        # Conditional execution after 100 @2.62 at14.575Z and 1000 @2.6291 at15.562Z.
        detail = dict(symbol=SYM, quantity=Decimal("120"), price=Decimal("2.62"),
            filled_at=datetime(2026, 10, 12, 13, 30, 15, 562000, tzinfo=UTC),
            broker_order_id="CONTROLLED-MNDR-TARGET", entry_broker_order_id="LOCAL-ENTRY",
            exit_base_client_order_id=wire.requests[0].client_order_id)
        assert await service._close_resolved_oco_managed_row(ACCT, SYM, detail=detail)
        await service._evaluate_v2_managed_exit(ACCT, SYM)
        assert len(wire.requests) == 1
        with factory() as session:
            fill, = session.scalars(select(Fill).where(Fill.side == "sell")).all()
            assert fill.price == Decimal("2.62") and fill.quantity == 120
    finally:
        fixture.close()
