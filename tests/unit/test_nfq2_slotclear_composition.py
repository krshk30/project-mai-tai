"""Joint cards; recorded bars with explicitly controlled quotes/book/broker state."""
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from project_mai_tai.db.models import TradeIntent

from tests.unit.test_nfq2_eh_fresh_price import biya, hold, quote, retry
from tests.unit.test_nfq2_eh_fresh_price import service as recorded_service
from tests.unit.test_slotclear1_fresh_flip import buy, crossing, lpcn
from tests.unit.test_slotclear1_fresh_sell import observe, recorded_probes, seeded


@pytest.fixture
def service(monkeypatch):
    return recorded_service.__wrapped__(monkeypatch)


@pytest.mark.parametrize("fanout", [False, True])
@pytest.mark.parametrize("nfq", [False, True])
def test_joint_fresh_buy_identity_and_dedup_with_nfq_enabled(fanout, nfq):
    strategy, state, clock, _, _ = lpcn()
    strategy.settings.oms_v2_eh_fresh_price_enabled = nfq
    strategy.settings.strategy_schwab_1m_v2_slotclear_fresh_sell_enabled = True
    strategy._dual_broker_fanout_enabled = fanout
    buy(strategy, state)
    draft = strategy.on_quote("LPCN", crossing(clock))
    assert draft is not None
    assert draft.metadata["slotclear_first"] == "true"
    assert draft.metadata["cw_entry_slot"] == "first"
    assert draft.metadata["fanout_segment_id"] and draft.metadata["fanout_slot_id"]
    mirror = strategy.drain_webull_fanout_intents()
    assert len(mirror) == int(fanout)
    if mirror:
        assert mirror[0].metadata["fanout_slot_id"] == draft.metadata["fanout_slot_id"]
    assert strategy.on_quote("LPCN", crossing(clock)) is None
    assert not strategy.drain_webull_fanout_intents()


@pytest.mark.parametrize("webull", [False, True])
def test_joint_slotclear_fresh_reactive_never_borrows_held_cap(service, webull):
    service.settings.strategy_schwab_1m_v2_slotclear_fresh_flip_enabled = True
    service.settings.strategy_schwab_1m_v2_slotclear_fresh_sell_enabled = True
    service.settings.oms_v2_eh_entry_max_cross_pct = 2.0  # SLOT authority remains narrower.
    event = biya(webull=webull, reactive=True)
    event.payload.metadata["slotclear_first"] = "true"
    quote(service, "2.56")
    intent = SimpleNamespace(id=None, status="created", payload={})
    with service.session_factory() as session:
        assert service._apply_v2_eh_reactive_entry(session=session, event=event, intent=intent) is not None
    assert event.payload.metadata["abandon_reason_code"] == "ASK_PAST_CROSS_CAP"


@pytest.mark.asyncio
@pytest.mark.parametrize("webull", [False, True])
async def test_joint_slotclear_real_nfq_dispatch_retains_held_cap_and_single_claim(service, webull):
    service.settings.strategy_schwab_1m_v2_slotclear_fresh_flip_enabled = True
    service.settings.strategy_schwab_1m_v2_slotclear_fresh_sell_enabled = True
    event = biya(webull=webull, reactive=True)
    event.payload.metadata["slotclear_first"] = "true"
    hold(service, event)
    quote(service, "2.56")
    await service._evaluate_nfq2_holds("BIYA")
    message = retry(service)
    assert service._claim_nfq2_retry(message)
    assert not service._claim_nfq2_retry(message)
    with service.session_factory() as session:
        intent = session.scalars(select(TradeIntent)).first()
        assert service._apply_v2_eh_reactive_entry(session=session, event=message, intent=intent) is None
    assert Decimal("2.56") <= Decimal(message.payload.metadata["limit_price"]) <= Decimal("2.5654")


def test_joint_recorded_sell_rearms_three_bars_without_reactive_permission():
    strategy, state, clock, _, _, _ = seeded(partial=True)
    strategy.settings.oms_v2_eh_fresh_price_enabled = True
    strategy.settings.strategy_schwab_1m_v2_slotclear_fresh_flip_enabled = True
    for index, probe in enumerate(list(recorded_probes("SXTC"))[:4]):
        observe(strategy, state, clock, probe)
        primary = strategy.drain_pending_intents()
        mirror = strategy.drain_webull_direct_intents()
        assert len(primary) == len(mirror) == int(index == 3)
        assert state.slotclear_fresh_buy_bar_ms == 0
        if primary:
            assert primary[0].metadata["fanout_slot_id"] == mirror[0].metadata["fanout_slot_id"]
