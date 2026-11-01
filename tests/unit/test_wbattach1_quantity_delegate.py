"""Service contract for the shared locked ownership proof; no broker proof invented."""

from datetime import UTC, datetime
from decimal import Decimal
import logging
from types import SimpleNamespace
from uuid import UUID

import pytest
from sqlalchemy import select

from project_mai_tai.db.models import BrokerOrder, OmsManagedPosition
from project_mai_tai.oms import managed_sell_prewire as prewire, service as module
from project_mai_tai.oms.service import OmsRiskService
from tests.unit.test_managed_sell_prewire import lane  # noqa: F401
from tests.unit.oms_sqlite_fixtures import owned_sqlite as owned_sqlite


@pytest.fixture(autouse=True)
def controlled_clock(monkeypatch):
    monkeypatch.setattr(module, "utcnow", lambda: datetime(2026, 10, 16, 17, tzinfo=UTC))
    monkeypatch.setattr(module, "_extended_hours_session", lambda now=None: None)


@pytest.mark.asyncio
@pytest.mark.parametrize("pending_allowed", [False, True])
@pytest.mark.parametrize("basis,expected", [
    ((8, None, ""), 8),
    ((8, Decimal(0), ""), 0),
    ((8, Decimal(3), ""), 3),
    ((0, None, "invalid_own_quantity"), 0),
])
async def test_service_delegate_passes_exact_session_context_and_permission(monkeypatch, pending_allowed, basis, expected):
    service = object.__new__(OmsRiskService)
    service.settings = SimpleNamespace()  # WBEXIT2/3 read service.settings since 10-28; absent switch = off
    service.logger = logging.getLogger(__name__)
    context = {"cw_target_bound_row_id": "original-row"}
    unit = object()
    calls = []

    def shared(actual_service, session, **kwargs):
        calls.append((actual_service, session, kwargs))
        return basis

    async def worker(operation, *, commit):
        assert commit is False
        return operation(unit)

    monkeypatch.setattr(prewire, "read_managed_sell_quantity", shared)
    service._run_db = worker
    result = await service._v2_managed_sell_owned_quantity(managed_row_id="original-row",
        strategy_code="schwab_1m_v2", broker_account_name="live:orb", symbol="ABC",
        requested_quantity=10, target_context=context, allow_pending_fill_writer=pending_allowed)
    assert result == expected
    assert len(calls) == 1 and calls[0][0] is service and calls[0][1] is unit
    assert calls[0][2] == {"managed_row_id": "original-row", "strategy_code": "schwab_1m_v2",
        "broker_account_name": "live:orb", "symbol": "ABC", "target_context": context,
        "allow_pending_fill_writer": pending_allowed}
    assert calls[0][2]["target_context"] is context


@pytest.mark.asyncio
@pytest.mark.parametrize("reason,allowed", [
    ("oms_v2_managed_exit:CW_TARGET", False),
    ("oms_v2_managed_exit:CW_HARD_STOP", True),
])
async def test_real_emitter_forwards_only_existing_pending_writer_policy(lane, monkeypatch, reason, allowed):  # noqa: F811
    service, sessions, row_id, account = lane
    service.settings.oms_v2_exit_release_reservation_enabled = False
    context = {"cw_target_bound_row_id": row_id, "cw_target_entry_order_id": "",
               "cw_target_entry_price": "5", "cw_target_quantity": "10"}
    calls = []

    async def capture(actual_service, **kwargs):
        assert actual_service is service
        calls.append(kwargs)
        return None  # Stop at the real preparation call, before any broker await.

    async def forbidden(_request):
        pytest.fail("refused preparation cannot reach a broker")

    monkeypatch.setattr(prewire, "prepare_managed_sell", capture)
    service.broker_adapter = SimpleNamespace(submit_order=forbidden)
    with sessions() as unit:
        row = unit.get(OmsManagedPosition, UUID(row_id))
        await service._emit_v2_managed_sell(unit, row, intent_type="close", quantity=10,
            reference_price=5.1 if not allowed else 4.5, reason=reason, bid=5.0,
            confirmation_context=context)
    assert len(calls) == 1
    assert calls[0]["target_context"] is context
    assert calls[0]["allow_pending_fill_writer"] is allowed
    with sessions() as unit:
        assert unit.scalar(select(BrokerOrder)) is None
