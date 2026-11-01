"""Exact bound history conflicts must not fall back to stale positive ownership."""
from tests.unit.oms_sqlite_fixtures import owned_sqlite as owned_sqlite
from decimal import Decimal as D
from unittest.mock import Mock
from uuid import UUID

import pytest
from sqlalchemy import select

from project_mai_tai.db.models import Fill, OmsManagedPosition, VirtualPosition
from tests.unit.test_oco_duplicate_close_after_close import seed_episode, quantity


@pytest.mark.asyncio
@pytest.mark.parametrize("account", ["live:schwab_1m_v2", "live:orb"])
@pytest.mark.parametrize("hour", [12, 13])
@pytest.mark.parametrize("defect", ["overfill", "missing_entry_execution", "entry_nan", "exit_nan", "exit_infinity", "missing_exit_id", "wrong_exit_id"])
async def test_corrupt_bound_history_refuses_before_release_or_submit(account, hour, defect, monkeypatch):
    service, factory, row_id, _, child_id = seed_episode(account, hour=hour)
    service.logger = Mock()
    if defect in {"overfill", "missing_entry_execution", "missing_exit_id", "wrong_exit_id"}:
        with factory.begin() as session:
            if defect == "overfill":
                child_fill = session.scalar(select(Fill).where(Fill.order_id == child_id))
                child_fill.quantity = D(151)
            elif defect == "missing_entry_execution":
                session.delete(session.scalar(select(Fill).where(Fill.side == "buy")))
            else:
                child_fill = session.scalar(select(Fill).where(Fill.order_id == child_id))
                child_fill.broker_fill_id = None if defect == "missing_exit_id" else "foreign:execution"
    else:
        # PG numeric supports NaN; SQLite cannot retain it faithfully. Inject a
        # corrupt ORM read while leaving persisted VP/managed quantities positive.
        async def corrupt_read(fn, *, commit=True):
            with factory() as session:
                side = "buy" if defect == "entry_nan" else "sell"
                execution = session.scalar(select(Fill).where(Fill.side == side))
                execution.quantity = D("Infinity" if defect == "exit_infinity" else "NaN")
                with session.no_autoflush:
                    return fn(session)
        monkeypatch.setattr(service, "_run_db", corrupt_read)

    async def forbidden(*args, **kwargs):
        pytest.fail("conflicting bound history reached release/cancel/submit")
    monkeypatch.setattr(service, "_release_exit_reservation_before_close", forbidden)
    monkeypatch.setattr(service.broker_adapter, "submit_order", forbidden)
    with factory() as session:
        row = session.get(OmsManagedPosition, UUID(row_id))
        assert row.current_quantity == session.scalar(select(VirtualPosition.quantity)) == 150
        result = await service._emit_v2_managed_sell(session, row,
            intent_type="close", quantity=150, reference_price=4.19,
            reason="oms_v2_managed_exit:CW_TARGET", bid=4.19)
    assert not result and result.ownership_refused
    assert await quantity(service, account, row_id) == 0
    assert "bound_oco_history_unknown:" in service.logger.warning.call_args.args[-1]


@pytest.mark.asyncio
@pytest.mark.parametrize("account", ["live:schwab_1m_v2", "live:orb"])
@pytest.mark.parametrize("hour", [12, 13])
@pytest.mark.parametrize("fill_id", [None, "foreign:cancel", "1008287070667:0"])
async def test_cancelled_zero_sibling_is_not_a_positive_execution(account, hour, fill_id):
    service, factory, row_id, _, child_id = seed_episode(account, hour=hour)
    with factory.begin() as session:
        fill = session.scalar(select(Fill).where(Fill.order_id == child_id))
        fill.quantity, fill.price = D(0), D(0)
        fill.broker_fill_id = fill_id
    assert await quantity(service, account, row_id) == 150
