"""Recorded AIXI ingress with a controlled wire; boot never proves broker state."""
from copy import deepcopy
from datetime import datetime
from decimal import Decimal
import json
from pathlib import Path
from threading import get_ident
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select

from project_mai_tai.db.models import BrokerOrder, DashboardSnapshot, Fill
from project_mai_tai.events import TradeIntentEvent, TradeIntentPayload
from project_mai_tai.oms import service as oms
from project_mai_tai.oms.atr_reprice_handoff import HandoffJournal, SNAPSHOT_TYPE, RETIRED_OFF_SNAPSHOT_TYPE
from tests.unit.test_rpg1_runtime import runtime
from tests.unit.test_rpgstuck1_startup import real_bot_startup, real_oms_startup, startup_harness

RECORDED = json.loads((Path(__file__).parents[1] / 'fixtures/rpgretire1_aixi_20261008.json').read_text())


@pytest.mark.asyncio
@pytest.mark.parametrize('enabled', [False, True])
@pytest.mark.parametrize('service', ['oms', 'v2'])
async def test_boot_retirement_is_offloop_off_only_and_preserves_every_payload(monkeypatch, enabled, service):
    h = await startup_harness(monkeypatch, handoff_enabled=enabled)
    journal = HandoffJournal(h.factory)
    before = dict(journal.jobs())
    with h.factory() as session:
        unrelated_id = uuid4()
        session.add(DashboardSnapshot(id=unrelated_id, snapshot_type='unrelated', payload={'keep': True}))
        orders_before = [(o.id, o.status, o.payload) for o in session.scalars(select(BrokerOrder))]
        parent = session.scalar(select(BrokerOrder))
        fill_id = uuid4()
        session.add(Fill(id=fill_id, order_id=parent.id, strategy_id=parent.strategy_id,
            broker_account_id=parent.broker_account_id, symbol=parent.symbol, side='buy', quantity=1, price=1))
        session.commit()
    main_thread = get_ident()
    threads = []
    original = HandoffJournal.retire_disabled

    def retire(self):
        threads.append(get_ident())
        return original(self)

    monkeypatch.setattr(HandoffJournal, 'retire_disabled', retire)
    if service == 'oms':
        # Startup's ON worker is outside this retirement-only control.
        async def started(_stop):
            h.service._rpg_retry_started().set()
        monkeypatch.setattr(h.service, '_run_rpg_retry_loop', started)
        await real_oms_startup(monkeypatch, h)
    else:
        if not enabled:
            h.bot._rpg_known_jobs = dict(before)
            h.strategy._rpg_handoffs = {str(k): v for k, v in before.items()}
        # ON hydration is verified separately by the recorded startup tests.
        if enabled:
            async def hydrate():
                h.strategy._rpg_handoffs = {str(k): v for k, v in before.items()}
            monkeypatch.setattr(h.bot, '_rpg_handoff_pass', hydrate)
        await real_bot_startup(monkeypatch, h)
    assert bool(threads) is (not enabled)
    assert all(thread != main_thread for thread in threads)
    with h.factory() as session:
        rows = list(session.scalars(select(DashboardSnapshot).where(DashboardSnapshot.id.in_(before))))
        assert {r.id: r.payload for r in rows} == before
        assert {r.snapshot_type for r in rows} == {SNAPSHOT_TYPE if enabled else RETIRED_OFF_SNAPSHOT_TYPE}
        assert session.get(DashboardSnapshot, unrelated_id).payload == {'keep': True}
        assert session.get(Fill, fill_id).quantity == 1
        assert [(o.id, o.status, o.payload) for o in session.scalars(select(BrokerOrder))] == orders_before
    if not enabled:
        assert journal.retire_disabled() == 0
    assert not h.adapter.opens and not h.adapter.cancels and not h.adapter.reads


@pytest.mark.asyncio
@pytest.mark.parametrize('service', ['oms', 'v2'])
async def test_off_boot_unreadable_retirement_propagates_instead_of_serving(monkeypatch, service):
    h = await startup_harness(monkeypatch, handoff_enabled=False)
    before = dict(HandoffJournal(h.factory).jobs())

    def broken(_self):
        raise RuntimeError('controlled retirement DB unreadable')

    monkeypatch.setattr(HandoffJournal, 'retire_disabled', broken)
    with pytest.raises(RuntimeError, match='controlled retirement DB unreadable'):
        await (real_oms_startup(monkeypatch, h) if service == 'oms' else real_bot_startup(monkeypatch, h))
    assert dict(HandoffJournal(h.factory).jobs()) == before
    assert not h.adapter.opens and not h.adapter.cancels and not h.adapter.reads


@pytest.mark.asyncio
@pytest.mark.parametrize('enabled,boot', [(False, False), (False, True), (True, False)])
async def test_original_aixi_0750_schwab_leg_sent_off_but_owned_on(monkeypatch, enabled, boot):
    regular = oms._is_regular_market_session
    fillable = oms.OmsRiskService._market_is_fillable
    h = await runtime(monkeypatch, 'schwab')
    monkeypatch.setattr(oms, '_is_regular_market_session', regular)
    monkeypatch.setattr(oms.OmsRiskService, '_market_is_fillable', fillable)
    raw = RECORDED['intent']
    h.clock[0] = datetime.fromisoformat(raw['created_at'])
    h.service.settings.strategy_schwab_1m_v2_atr_reprice_handoff_enabled = enabled
    ticket = RECORDED['ticket']
    with h.factory() as session:
        # Reconstruct only the archived ownership record as a stale active ticket.
        session.add(DashboardSnapshot(id=UUID(ticket['id']), snapshot_type=SNAPSHOT_TYPE,
            created_at=datetime.fromisoformat(ticket['created_at']), payload=deepcopy(ticket['payload'])))
        session.commit()
    if boot:
        assert h.service._rpg_retire_disabled_at_boot() == 1
    event = TradeIntentEvent(event_id=UUID(raw['event_id']), source_service=raw['source_service'],
        produced_at=h.clock[0], payload=TradeIntentPayload(**{key: raw[key] for key in (
            'strategy_code', 'broker_account_name', 'symbol', 'side', 'intent_type', 'reason')},
            quantity=Decimal(raw['quantity']), metadata=deepcopy(raw['metadata'])))
    result = await h.service.process_trade_intent(event)
    assert len(h.adapter.opens) == (0 if enabled else 1)
    assert result[0].payload.status == ('aborted' if enabled else 'accepted')
    if enabled:
        assert result[0].payload.reason == raw['recorded_refusal']
    else:
        wire, = h.adapter.opens
        assert (wire.symbol, wire.broker_account_name, wire.quantity, wire.order_type,
                wire.metadata['limit_price']) == ('AIXI', raw['broker_account_name'], Decimal(270), 'limit', '2.22')
        with h.factory() as session:
            order = session.scalar(select(BrokerOrder).where(BrokerOrder.client_order_id == wire.client_order_id))
            assert order.status == 'accepted' and order.broker_order_id == 'simulated-replacement'
    assert not h.adapter.cancels and not h.adapter.reads
    with h.factory() as session:
        rows = list(session.scalars(select(DashboardSnapshot).where(DashboardSnapshot.id == UUID(ticket['id']))))
        assert len(rows) == 1 and rows[0].payload == ticket['payload']
        assert rows[0].snapshot_type == (RETIRED_OFF_SNAPSHOT_TYPE if boot else SNAPSHOT_TYPE)
