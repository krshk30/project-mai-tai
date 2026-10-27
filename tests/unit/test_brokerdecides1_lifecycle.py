"""Actual OMS sync lifecycle, injected native/transport only; never a live service."""
import asyncio
import logging
from types import SimpleNamespace

import pytest

from project_mai_tai.oms.service import OmsRiskService
from project_mai_tai.services.schwab_1m_v2_bot import SchwabV2BotService


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["none", "sync", "schedule"])
async def test_actual_sync_lifecycle_retains_diagnostics_and_existing_result(monkeypatch, failure):
    from project_mai_tai.oms import wbquiet_shadow
    from project_mai_tai import brokerdecides1_sync as l12
    from project_mai_tai.sync_sdk_diagnostics import CURRENT, SdkFrame

    stages = []

    async def prepare(*args):
        return None, None

    async def shadow_finish(*args):
        stages.append("diagnostics_finished")

    async def sync(*, account_names):
        stages.append("sync")
        if failure == "sync":
            raise ValueError("existing sync failed")
        return {"orders": 3}

    def schedule(value):
        assert value._brokerdecides1_foreground_syncs == 0
        stages.append("schedule")
        if failure == "schedule":
            raise ConnectionError("publication unknown")

    monkeypatch.setattr(wbquiet_shadow, "prepare", prepare)
    monkeypatch.setattr(wbquiet_shadow, "bind_pass", lambda *args: None)
    monkeypatch.setattr(wbquiet_shadow, "finish", shadow_finish)
    monkeypatch.setattr(l12, "schedule_refresh", schedule)
    value = SimpleNamespace(settings=SimpleNamespace(strategy_schwab_1m_v2_brokerdecides1_enabled=True),
        logger=logging.getLogger(__name__), _sync_broker_state_pass=sync)
    parent = SdkFrame()
    token = CURRENT.set(parent)
    try:
        if failure == "sync":
            with pytest.raises(ValueError, match="existing sync failed"):
                await OmsRiskService.sync_broker_state(value)
            assert stages == ["sync", "diagnostics_finished"]
        else:
            assert await OmsRiskService.sync_broker_state(value) == {"orders": 3}
            assert stages == ["sync", "diagnostics_finished", "schedule"]
        assert CURRENT.get() is parent
        assert value._brokerdecides1_foreground_syncs == 0
    finally:
        CURRENT.reset(token)


@pytest.mark.asyncio
async def test_actual_sync_flag_off_does_not_enter_publisher(monkeypatch):
    from project_mai_tai.oms import wbquiet_shadow
    from project_mai_tai import brokerdecides1_sync as l12

    async def prepare(*args):
        return None, None

    async def done(*args):
        return None

    async def forbidden(*args):
        raise AssertionError("flag OFF must not enter L12")

    async def sync(*, account_names):
        return {"orders": 1}

    monkeypatch.setattr(wbquiet_shadow, "prepare", prepare)
    monkeypatch.setattr(wbquiet_shadow, "bind_pass", lambda *args: None)
    monkeypatch.setattr(wbquiet_shadow, "finish", done)
    monkeypatch.setattr(l12, "begin_pass", forbidden)
    value = SimpleNamespace(settings=SimpleNamespace(strategy_schwab_1m_v2_brokerdecides1_enabled=False),
        logger=logging.getLogger(__name__), _sync_broker_state_pass=sync)
    assert await OmsRiskService.sync_broker_state(value) == {"orders": 1}


@pytest.mark.asyncio
@pytest.mark.parametrize("flag", [True, False])
async def test_actual_background_position_poll_consumer_hook_precedes_existing_poll(monkeypatch, flag):
    from project_mai_tai import brokerdecides1_sync as l12

    stages = []

    class StopExistingPoll(Exception):
        pass

    async def refresh(value):
        stages.append("consumer")

    def maps():
        stages.append("existing_poll")
        raise StopExistingPoll()

    monkeypatch.setattr(l12, "refresh_consumer", refresh)
    value = SimpleNamespace(settings=SimpleNamespace(strategy_schwab_1m_v2_brokerdecides1_enabled=flag),
        strategy=SimpleNamespace(), _fetch_position_maps=maps)
    with pytest.raises(StopExistingPoll):
        await SchwabV2BotService._position_poll_pass(value)
    assert stages == (["consumer", "existing_poll"] if flag else ["existing_poll"])
