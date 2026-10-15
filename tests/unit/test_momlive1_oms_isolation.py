import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from project_mai_tai.events import TradeIntentEvent, TradeIntentPayload
from project_mai_tai.oms.service import OmsRiskService


def intent(kind="open"):
    return TradeIntentEvent(source_service="momentum-paper", payload=TradeIntentPayload(
        strategy_code="momentum_warn_live", broker_account_name="live:schwab_1m_v2",
        symbol="SCNI", side="buy" if kind == "open" else "sell", quantity=1,
        intent_type=kind, reason="controlled MOMLIVE1"))


def service(*, enabled, actor):
    result = OmsRiskService.__new__(OmsRiskService)
    result.settings = SimpleNamespace(momentum_live_enabled=enabled)
    result.logger = logging.getLogger("momlive1-isolation-control")
    result._momentum_live_actor = actor
    result._mirrorhold_scope = Mock(return_value=False)
    result._rpg_external_retry = Mock(side_effect=AssertionError("generic RPG reached"))
    result._refresh_drift_working_cache = AsyncMock(side_effect=AssertionError("generic DB reached"))
    result._run_db = AsyncMock(side_effect=AssertionError("generic DB reached"))
    return result


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", [False, True])
async def test_unrestored_new_strategy_never_falls_through_generic_broker_or_db(enabled, caplog):
    result = service(enabled=enabled, actor=None)
    with caplog.at_level(logging.WARNING):
        assert await result.process_trade_intent(intent()) == []
    assert "actor_not_restored generic_dispatch=never" in caplog.text
    result._rpg_external_retry.assert_not_called()
    result._run_db.assert_not_awaited()
    result._refresh_drift_working_cache.assert_not_awaited()


@pytest.mark.asyncio
async def test_manual_off_refuses_new_entry_without_losing_owned_close_handoff():
    actor = Mock()
    result = service(enabled=False, actor=actor)
    assert await result.process_trade_intent(intent()) == []
    actor.offer_intent.assert_not_called()
    close = intent("close")
    assert await result.process_trade_intent(close) == []
    actor.offer_intent.assert_called_once_with(close)
    result._run_db.assert_not_awaited()
    result._refresh_drift_working_cache.assert_not_awaited()


@pytest.mark.asyncio
async def test_enabled_entry_only_offers_to_own_bounded_actor():
    actor = Mock()
    result = service(enabled=True, actor=actor)
    event = intent()
    assert await result.process_trade_intent(event) == []
    actor.offer_intent.assert_called_once_with(event)
    result._rpg_external_retry.assert_not_called()
    result._run_db.assert_not_awaited()
    result._refresh_drift_working_cache.assert_not_awaited()
