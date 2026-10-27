"""PRICE1 retained-hold seams. Fake transactions are NOT native/DB proof."""

from contextlib import nullcontext
from datetime import UTC, datetime
from decimal import Decimal as D
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest

from project_mai_tai.events import TradeIntentEvent, TradeIntentPayload
from project_mai_tai.oms.eh_fresh_price import EhFreshPriceMixin, EhHold
from project_mai_tai.oms.mirror_fresh_price import MirrorFreshPriceMixin, MirrorReading, PriceHold, HELD_REASON
from project_mai_tai.settings import Settings


NOW = datetime(2026, 10, 22, 12, tzinfo=UTC)


def event():
    return TradeIntentEvent(source_service="schwab-1m-v2", payload=TradeIntentPayload(
        strategy_code="schwab_1m_v2", broker_account_name="live:orb", symbol="OLOX",
        side="buy", intent_type="open", quantity=D(2), reason="controlled retained wait",
        metadata={"entry_price": "1.20", "resting_level": "1.20", "fanout_slot_id": "controlled-slot"}))


@pytest.mark.asyncio
@pytest.mark.parametrize("price", [".99", ".99999"])
async def test_nfq2_current_under_one_retains_hold_before_any_cas_then_queues_once(price):
    service = EhFreshPriceMixin()
    original = event()
    hold = EhHold(original, uuid4(), uuid4())
    service._nfq2_holds[service._nfq2_key(original)] = hold
    reading = [MirrorReading(D(price), "ask", NOW, 0, True)]
    service._nfq2_reading = lambda symbol: reading[0]
    service._run_db = AsyncMock(return_value=True)
    service.redis = SimpleNamespace(xadd=AsyncMock())
    service.settings = Settings()
    service._nfq_now = lambda: NOW
    await service._evaluate_nfq2_holds("OLOX")
    service._run_db.assert_not_awaited()
    service.redis.xadd.assert_not_awaited()
    assert hold.phase == "held" and hold.token == "" and hold.dispatch is None
    reading[0] = MirrorReading(D("1.20"), "ask", NOW, 0, True)
    await service._evaluate_nfq2_holds("OLOX")
    assert hold.phase == "queued" and hold.token and hold.dispatch is None
    assert service._run_db.await_count == service.redis.xadd.await_count == 1
    await service._evaluate_nfq2_holds("OLOX")
    assert service.redis.xadd.await_count == 1


@pytest.mark.parametrize("price", [".99", ".99999"])
def test_nfq1_current_under_one_keeps_token_and_budget_then_queues_once(price):
    service = MirrorFreshPriceMixin()
    original = event()
    hold = PriceHold(original, uuid4())
    service._nfq_holds["controlled-slot"] = hold
    reading = [MirrorReading(D(price), "ask", NOW, 0, True)]
    service._mirror_reading = lambda symbol: reading[0]
    service.session_factory = lambda: nullcontext(SimpleNamespace(commit=lambda: None))
    service._nfq_retirement_reason = lambda session, current: None
    service._nfq_save = Mock()
    service._nfq_now = lambda: NOW
    assert service._prepare_nfq_queues("OLOX") == []
    assert hold.phase == "held" and hold.token == ""
    service._nfq_save.assert_not_called()
    reading[0] = MirrorReading(D("1.20"), "ask", NOW, 0, True)
    queued = service._prepare_nfq_queues("OLOX")
    assert len(queued) == 1 and queued[0][0] is hold and hold.phase == "queued"
    assert service._prepare_nfq_queues("OLOX") == []
    assert service._nfq_save.call_count == 1


def test_nfq1_pre_submit_under_one_uses_existing_held_contract_not_gave_up():
    service = MirrorFreshPriceMixin()
    original = event()
    service._nfq_enabled = lambda: True
    service._is_webull_resting_mirror_event = lambda value: True
    service._nfq_window_open = lambda value: True
    service._mirror_reading = lambda symbol: MirrorReading(D(".99999"), "ask", NOW, 0, True)
    service._nfq_hold = Mock()
    assert service._nfq_pre_submit(None, original) == HELD_REASON
    service._nfq_hold.assert_called_once_with(None, original, "PRICE1-REFUSED")


def test_nfq2_pre_submit_under_one_retains_intent_as_existing_held_state():
    service = EhFreshPriceMixin()
    original = event()
    service._nfq2_applies = lambda value: True
    service._nfq2_bound_identity = lambda value: True
    service._nfq2_segment_current = lambda session, value: True
    service._nfq2_reading = lambda symbol: MirrorReading(D(".99999"), "ask", NOW, 0, True)
    service._nfq2_save = Mock()
    service._nfq2_feedback = Mock()
    intent = SimpleNamespace(id=uuid4(), status="pending")
    assert service._nfq2_pre_submit(None, original, intent)
    held = service._nfq2_holds[service._nfq2_key(original)]
    assert intent.status == held.phase == "held" and held.token == "" and held.dispatch is None
    service._nfq2_feedback.assert_called_once_with(None, original, "held_no_fresh_quote", "PRICE1-REFUSED")
