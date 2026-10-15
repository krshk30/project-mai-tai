"""An uncertain attribution writer cannot override positive own exit fills."""
from types import SimpleNamespace
from uuid import UUID

import pytest

from project_mai_tai.db.models import OmsManagedPosition
from tests.unit.test_oco_duplicate_close_after_close import seed_episode


@pytest.mark.asyncio
@pytest.mark.parametrize("account", ["live:schwab_1m_v2", "live:orb"])
@pytest.mark.parametrize("hour", [12, 13])
async def test_protective_pending_writer_kept_but_proven_full_native_exit_never_sells(account, hour, monkeypatch):
    service, factory, row_id, _, _ = seed_episode(account, hour=hour)
    pending = SimpleNamespace(row_id=row_id)
    service._oco_exit_fill_pending[(account, "GLND")] = pending

    async def forbidden(*args, **kwargs):
        pytest.fail("proven full native exit must not release/cancel/submit")

    monkeypatch.setattr(service, "_release_exit_reservation_before_close", forbidden)
    monkeypatch.setattr(service.broker_adapter, "submit_order", forbidden)
    with factory() as session:
        result = await service._emit_v2_managed_sell(
            session, session.get(OmsManagedPosition, UUID(row_id)), intent_type="close",
            quantity=150, reference_price=4.19, reason="oms_v2_managed_exit:HARD", bid=4.19,
        )
    assert not result and result.ownership_refused
    assert service._oco_exit_fill_pending[(account, "GLND")] is pending
