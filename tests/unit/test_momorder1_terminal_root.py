"""Durable actual single-root finality wins over a stale working handle."""

from copy import deepcopy
from decimal import Decimal as D
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from project_mai_tai.broker_adapters.webull_order_reads import QueryBudgetUnavailable
from project_mai_tai.db.models import BrokerOrder, BrokerOrderEvent, Fill, VirtualPosition
from tests.unit.test_momorder1_runtime_db import CASES, held_case, seed, utc


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["webull", "schwab"])
@pytest.mark.parametrize("session_name,at", [
    ("AM", "2026-10-19T12:29:43.376Z"),
    ("PM", "2026-10-19T21:00:00Z"),
])
@pytest.mark.parametrize("status,quantity", [("filled", D(50)), ("cancelled", D(7))])
@pytest.mark.parametrize("reason", ["hard_stop_15pct", "time_300s", "KILLSW1_STOP"])
@pytest.mark.parametrize("unavailable", ["budget", "transport"])
async def test_durable_terminal_single_root_skips_read_cancel_and_claim_despite_working_handle(
    tmp_path, monkeypatch, provider, session_name, at, status, quantity, reason, unavailable,
):
    # Historical PM/AM clocks avoid weakening trusted execution-time validation.
    h = await seed(tmp_path, CASES[1], provider=provider, at=utc(at))
    try:
        _, handle = await held_case(h)
        assert handle.session == session_name and handle.status == "working"
        request = h.venue.orders[handle.base_coid].request
        h.venue.put(request, status, quantity, D("2.10"), h.now[0])
        report = h.venue.report(request)
        await h.runtime._record(request, [report])
        # Deliberately do NOT refresh the handle through backend.read(): this is
        # the race where accounting already knows more than the cached owner.
        assert handle.status == "working" and handle.cumulative_exited == 0

        def accounting():
            with h.sf() as session:
                root = session.scalar(select(BrokerOrder).where(
                    BrokerOrder.client_order_id == handle.base_coid))
                fills = session.scalars(select(Fill).where(Fill.order_id == root.id)).all()
                assert root.status == status
                assert root.payload["momlive_terminal_broker_event"] == status
                assert len(fills) == 1 and fills[0].quantity == quantity
                assert fills[0].price == D("2.10")
                vp = session.scalar(select(VirtualPosition).where(
                    VirtualPosition.strategy_id == root.strategy_id,
                    VirtualPosition.broker_account_id == root.broker_account_id,
                    VirtualPosition.symbol == "YMAT"))
                assert vp.quantity == 50 - quantity
                assert vp.realized_pnl == quantity * D("0.10")
                return (root.id, root.status, deepcopy(root.payload),
                    tuple((f.id, f.quantity, f.price) for f in fills),
                    vp.quantity, vp.realized_pnl,
                    frozenset(session.scalars(select(BrokerOrder.id)).all()),
                    frozenset(session.scalars(select(BrokerOrderEvent.id)).all()))

        before = accounting()
        h.runtime.runtime[h.identity]["soft_exit_reason"] = reason
        runtime_before = deepcopy(h.runtime.runtime[h.identity])
        calls_before = list(h.venue.calls)
        error = (QueryBudgetUnavailable("controlled exhausted detail budget")
                 if unavailable == "budget" else TimeoutError("controlled unavailable transport"))
        # These unavailable ports must never be reached: existing exact terminal
        # proof needs no new permit, transport, cancel authorization or fake wire.
        fresh = AsyncMock(side_effect=error)
        ordinary = AsyncMock(side_effect=error)
        http = AsyncMock(side_effect=error)
        authorize = AsyncMock(side_effect=AssertionError("terminal root claimed cleanup"))
        monkeypatch.setattr(h.runtime.backend, "fresh_cancel_read", fresh)
        monkeypatch.setattr(h.venue, "fetch_order_update", ordinary)
        monkeypatch.setattr(h.venue.schwab, "_authorized_request_json", http)
        monkeypatch.setattr(h.runtime.backend, "_authorize_native_cancel", authorize)

        for _ in range(2):
            await h.runtime.backend.cancel(handle, h.runtime)
            assert accounting() == before
        fresh.assert_not_awaited()
        ordinary.assert_not_awaited()
        http.assert_not_awaited()
        authorize.assert_not_awaited()
        assert h.venue.calls == calls_before
        assert h.runtime.runtime[h.identity] == runtime_before
        assert f"native_cancel_wires:{handle.base_coid}" not in runtime_before
        assert f"native_cancel_cleanup:{handle.base_coid}" not in runtime_before
        assert handle.status == "working" and handle.cumulative_exited == 0
        assert not [row for row in h.venue.orders.values() if row.request.metadata.get("exit_reason")]
    finally:
        h.engine.dispose()
