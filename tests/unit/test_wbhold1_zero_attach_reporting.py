"""Reviewer16:0x: zero-size refusal must not hide behind an old protect handle."""
from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select

from project_mai_tai.db.models import BrokerOrder, SystemIncident
from project_mai_tai.oms import service as module
from project_mai_tai.oms.service import OmsRiskService
from tests.unit import test_edgeprot1_webull_edge as edge
from tests.unit.test_wbhold1_attach_backstop import attach, ledger, controlled_clock as controlled_clock

replay = edge.replay


@pytest.mark.asyncio
@pytest.mark.parametrize('reason', ['requested_zero', 'software_reserved'])
@pytest.mark.parametrize('old_handle', [False, True])
async def test_zero_size_counts_failed_and_pages_once_with_or_without_old_handle(
    replay, monkeypatch, reason, old_handle,
):
    service, factory, wire = replay
    ledger(factory, 0)
    if old_handle:
        assert await service._persist_webull_protect_base(
            edge.ACCT, edge.SYM, 'CONTROLLED-OLD-CANCELLED-PAIR',
            entry_client_order_id='MNDR-OWNED-ENTRY',
        )
    if reason == 'software_reserved':
        with factory.begin() as session:
            entry = edge._entry(session)
            session.add(BrokerOrder(intent_id=entry.intent_id, strategy_id=entry.strategy_id,
                broker_account_id=entry.broker_account_id, client_order_id='CONTROLLED-SELL-RESERVATION',
                symbol=edge.SYM, side='sell', status='accepted', quantity=Decimal(120),
                order_type='LIMIT', time_in_force='day', submitted_at=edge.NOW, payload={}))
    assert not await attach(service, 0 if reason == 'requested_zero' else 120)
    assert wire.requests == []
    assert (edge.ACCT, edge.SYM) in service._webull_protect_failed
    with factory() as session:
        assert edge._entry(session).payload[service._WEBULL_PROTECT_STATE_KEY] == 'attach_failed'
        row_id = str(edge._row(session).id)
    # Private SQLite and controlled clock only. No real waits or incident writes.
    assert await service._check_webull_uncovered_shares() == 0
    monkeypatch.setattr(module, 'utcnow', lambda: edge.NOW+timedelta(seconds=31))
    assert await service._check_webull_uncovered_shares() == 1
    assert await service._check_webull_uncovered_shares() == 0
    with factory() as session:
        incident, = session.scalars(select(SystemIncident)).all()
        assert incident.payload['managed_row_id'] == row_id
        assert incident.payload['cause'] == ('reattach_failed' if old_handle else 'never_protected')
    if reason == 'software_reserved':
        with factory.begin() as session:
            session.scalar(select(BrokerOrder).where(
                BrokerOrder.client_order_id == 'CONTROLLED-SELL-RESERVATION')).status = 'cancelled'
    assert await attach(service)
    assert [r.quantity for r in wire.requests] == [Decimal(120)]
    assert (edge.ACCT, edge.SYM) not in service._webull_protect_failed
    with factory() as session:
        assert edge._entry(session).payload[service._WEBULL_PROTECT_STATE_KEY] == 'resting'
    assert await service._check_webull_uncovered_shares() == 0


@pytest.mark.asyncio
async def test_zero_size_failure_durable_marker_survives_empty_memory_restart(replay, monkeypatch):
    service, factory, wire = replay
    ledger(factory, 0)
    assert await service._persist_webull_protect_base(
        edge.ACCT, edge.SYM, 'CONTROLLED-OLD-CANCELLED-PAIR',
        entry_client_order_id='MNDR-OWNED-ENTRY')
    assert not await attach(service, 0)
    fresh = OmsRiskService(settings=service.settings, redis_client=edge._Redis(), session_factory=factory)
    assert not fresh.__dict__.get('_webull_protect_failed')
    assert await fresh._check_webull_uncovered_shares() == 0
    monkeypatch.setattr(module, 'utcnow', lambda: edge.NOW+timedelta(seconds=31))
    assert await fresh._check_webull_uncovered_shares() == 1
    with factory() as session:
        incident, = session.scalars(select(SystemIncident)).all()
        assert incident.payload['cause'] == 'reattach_failed'
    assert wire.requests == []


@pytest.mark.asyncio
async def test_zero_size_retains_memory_page_when_durable_mark_cannot_write(replay, monkeypatch):
    service, factory, wire = replay
    ledger(factory, 0)
    assert await service._persist_webull_protect_base(
        edge.ACCT, edge.SYM, 'CONTROLLED-OLD-CANCELLED-PAIR',
        entry_client_order_id='MNDR-OWNED-ENTRY')

    async def failed_mark(*_):
        return False

    monkeypatch.setattr(service, '_persist_webull_protect_state', failed_mark)
    assert not await attach(service, 0)
    assert (edge.ACCT, edge.SYM) in service._webull_protect_failed
    with factory() as session:
        assert edge._entry(session).payload[service._WEBULL_PROTECT_STATE_KEY] == 'resting'
    assert await service._check_webull_uncovered_shares() == 0
    monkeypatch.setattr(module, 'utcnow', lambda: edge.NOW+timedelta(seconds=31))
    assert await service._check_webull_uncovered_shares() == 1
    assert wire.requests == []
