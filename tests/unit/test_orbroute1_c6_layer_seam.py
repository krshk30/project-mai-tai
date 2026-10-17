"""Unrun separable-layer controls: routing flags select owners, never weaken C6."""

from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest

from project_mai_tai.db.models import BrokerOrder, Strategy
from project_mai_tai.oms import orb_schwab_eod as owner
from project_mai_tai.oms.bot_control import ControlConflict
from project_mai_tai.oms.bot_control_atr import AtrStopDriver
from project_mai_tai.oms.bot_control_legacy import LegacyStopDriver
from project_mai_tai.oms.bot_control_orb import OrbStopDriver, _cancel_entry, drive_orb_stop
from project_mai_tai.oms.service import OmsRiskService
from tests.unit.test_killsw1_legacy import legacy as legacy
from tests.unit.test_orbroute1_oms_chain import setup as setup


@pytest.mark.parametrize("control_on", [False, True])
@pytest.mark.parametrize("atr_on", [False, True])
@pytest.mark.parametrize("orb_on", [False, True])
def test_actual_oms_registers_each_owner_only_under_its_own_flag(setup, control_on, atr_on, orb_on):
    original, sessions, broker = setup
    service = OmsRiskService(
        settings=original.settings.model_copy(update={
            "bot_control_enabled": control_on,
            "strategy_schwab_1m_v2_country_route_enabled": atr_on,
            "orb_country_routing_enabled": orb_on,
        }), redis_client=original.redis, session_factory=sessions, broker_adapter=broker,
    )
    runtime = service._bot_control_runtime
    if not control_on:
        assert runtime is None
    else:
        assert type(runtime.executor.drivers["schwab_1m_v2"]) is (AtrStopDriver if atr_on else LegacyStopDriver)
        assert type(runtime.executor.drivers["orb_schwab"]) is (OrbStopDriver if orb_on else LegacyStopDriver)
    assert broker.sent == []


@pytest.mark.asyncio
async def test_disabled_orb_driver_and_direct_stop_do_not_read_or_submit():
    async def forbidden(*args, **kwargs):
        pytest.fail("flag-OFF STOP reached an owner or database")

    service = SimpleNamespace(settings=SimpleNamespace(
        bot_control_enabled=True, orb_country_routing_enabled=False), _run_db=forbidden)
    assert (await drive_orb_stop(service, None, None)).phase == "BLOCKED_UNKNOWN"
    with pytest.raises(ValueError, match="orb_stop_context_missing"):
        await owner._attempt(service, None, None, lambda: datetime.now(UTC),
                             reason=owner.STOP_REASON, stop_context=object())


@pytest.mark.asyncio
async def test_orb_entry_cancel_keeps_c6_read_first_partial_native_refusal(legacy):
    with legacy.sessions.begin() as session:
        session.get(Strategy, legacy.strategy).code = "orb_schwab"
        entry = session.get(BrokerOrder, legacy.entries[0])
        entry.payload = {**entry.payload, "bracket": "true", "native_oco_bracket": "true"}
    legacy.broker.filled = Decimal(3)
    legacy.broker.read_event = "partially_filled"
    with pytest.raises(ControlConflict, match="partial_native_parent_protection_UNPROVEN"):
        await _cancel_entry(legacy.service, legacy.lease, legacy.journal,
                            legacy.entries[0], lambda: datetime.now(UTC))
    assert legacy.broker.sent == []
    assert len(legacy.broker.reads) == 1
