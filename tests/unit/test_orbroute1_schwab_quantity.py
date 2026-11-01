"""New ten-share Schwab ORB lifecycle versus unchanged legacy two-share controls."""


from tests.unit.oms_sqlite_fixtures import owned_sqlite as owned_sqlite
import asyncio
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select

from project_mai_tai.db.models import AccountPosition, BrokerOrder, Fill, VirtualPosition
from project_mai_tai.momentum_live_sec import CountryRecord, CountryRouter, CountryStore
from project_mai_tai.orb_country_route import select_orb_country_route
from project_mai_tai.orb_schwab_order_route import build_orb_schwab_open_intent
from tests.unit.test_orb_schwab_order_route import ACCOUNT, OPEN, _EodBroker, _service


def test_actual_schwab_ten_share_eod_closes_once_at_owned_size(monkeypatch):
    broker = _EodBroker()
    service, factory, _broker = _service(monkeypatch, broker=broker)
    service.settings.orb_country_routing_enabled = True
    service.settings.orb_webull_account_name = "live:orb"
    store = CountryStore(factory)
    store.put(CountryRecord("CLRO", "0000000001", "NY", "DE", True,
                            OPEN - timedelta(days=1), "READY", OPEN.date()))
    router = CountryRouter(store)
    router._records = store.load()
    selection = select_orb_country_route(router, "CLRO", OPEN,
        schwab_account=ACCOUNT, webull_account="live:orb")
    event = build_orb_schwab_open_intent(service.settings, "CLRO", Decimal("5.5284"), selection=selection)
    asyncio.run(service.process_trade_intent(event))
    fill_at = OPEN.replace(hour=14, minute=0)
    with factory.begin() as session:
        entry = session.scalar(select(BrokerOrder).where(BrokerOrder.side == "buy"))
        assert entry.quantity == 10
        entry.status = "filled"
        session.add(Fill(order_id=entry.id, strategy_id=entry.strategy_id,
            broker_account_id=entry.broker_account_id, broker_fill_id="entry-ten",
            symbol="CLRO", side="buy", quantity=Decimal(10), price=Decimal("5.53"),
            filled_at=fill_at, payload={}))
        session.add(VirtualPosition(strategy_id=entry.strategy_id, broker_account_id=entry.broker_account_id,
            symbol="CLRO", quantity=Decimal(10), average_price=Decimal("5.53"), opened_at=fill_at))
        session.add(AccountPosition(broker_account_id=entry.broker_account_id,
            symbol="CLRO", quantity=Decimal(10), average_price=Decimal("5.53")))
    original_read = broker.fetch_order_update
    async def parent_read(request):
        report = await original_read(request)
        return replace(report, quantity=Decimal(10), filled_quantity=Decimal(10),
                       origin="broker", reported_at=fill_at)
    broker.fetch_order_update = parent_read
    broker.quantity = Decimal(10)
    broker.after_release_quantity = Decimal(10)
    monkeypatch.setattr("project_mai_tai.oms.service.utcnow", lambda: OPEN.replace(hour=19, minute=55))
    asyncio.run(service._orb_schwab_eod_close())
    asyncio.run(service._orb_schwab_eod_close())
    closes = [r for r in broker.submitted if r.intent_type == "close"]
    assert len(closes) == 1
    assert closes[0].quantity == 10
    assert closes[0].broker_account_name == ACCOUNT
    with factory() as session:
        assert session.scalar(select(VirtualPosition)).quantity == 0
        assert len(session.scalars(select(Fill).where(Fill.side == "sell")).all()) == 1


def test_hidden_two_share_eod_quantity_mutation_is_detected(monkeypatch):
    monkeypatch.setattr("project_mai_tai.oms.orb_country.entry_quantity", lambda _entry: Decimal(2))
    with pytest.raises(AssertionError):
        test_actual_schwab_ten_share_eod_closes_once_at_owned_size(monkeypatch)
