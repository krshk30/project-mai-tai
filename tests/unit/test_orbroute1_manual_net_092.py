"""092: manual account net is not bot ownership; no receipt gate on entry."""

import asyncio
from decimal import Decimal
import logging

import pytest

from project_mai_tai.broker_adapters.protocols import BrokerPositionSnapshot
from project_mai_tai.db.models import AccountPosition, VirtualPosition
from project_mai_tai.orb_schwab_order_route import build_orb_schwab_open_intent
from tests.unit.test_orbroute1_oms_chain import OPEN, SCHWAB, WEBULL, selected, setup as setup


def manual_net(setup, account):
    service, factory, broker = setup
    broker.positions = [BrokerPositionSnapshot(account, "ABC", Decimal(1000), Decimal(5), as_of=OPEN)]
    with factory.begin() as session:
        row = service.store.ensure_broker_account(session, account,
            provider="schwab" if account == SCHWAB else "webull", environment="production")
        session.add(AccountPosition(broker_account_id=row.id, symbol="ABC", quantity=Decimal(1000),
                                    average_price=Decimal(5)))
        return row.id


@pytest.mark.parametrize("account,is_us", [(SCHWAB, True), (WEBULL, False)])
def test_manual_thousand_shares_allows_routed_ten_and_logs_without_receipt(setup, account, is_us, caplog):
    service, factory, broker = setup
    manual_net(setup, account)
    event = build_orb_schwab_open_intent(service.settings, "ABC", Decimal(5),
                                       selection=selected(factory, service.settings, is_us))
    with caplog.at_level(logging.INFO):
        asyncio.run(service.process_trade_intent(event))
    assert len(broker.sent) == 1
    request = broker.sent[0]
    assert (request.broker_account_name, request.strategy_code, request.side,
            request.intent_type, request.quantity) == (account, "orb_schwab", "buy", "open", Decimal(10))
    assert "[ROUTE-OWNER-MANUAL-PRESENT]" in caplog.text


@pytest.mark.parametrize("account,is_us", [(SCHWAB, True), (WEBULL, False)])
def test_manual_net_exception_never_exempts_foreign_bot_vp(setup, account, is_us, caplog):
    service, factory, broker = setup
    account_id = manual_net(setup, account)
    with factory.begin() as session:
        foreign = service.store.ensure_strategy(session, "momentum_warn_live", name="MOM", execution_mode="live")
        session.add(VirtualPosition(strategy_id=foreign.id, broker_account_id=account_id, symbol="ABC",
                                    quantity=Decimal(3), average_price=Decimal(5)))
    event = build_orb_schwab_open_intent(service.settings, "ABC", Decimal(5),
                                       selection=selected(factory, service.settings, is_us))
    with caplog.at_level(logging.INFO):
        asyncio.run(service.process_trade_intent(event))
    assert not broker.sent and not broker.previews
    assert "[ROUTE-OWNER-CONFLICT]" in caplog.text


def test_flag_off_legacy_schwab_manual_net_veto_is_unchanged(setup, caplog):
    service, _factory, broker = setup
    service.settings.orb_country_routing_enabled = False
    manual_net(setup, SCHWAB)
    event = build_orb_schwab_open_intent(service.settings, "ABC", Decimal(5))
    with caplog.at_level(logging.INFO):
        asyncio.run(service.process_trade_intent(event))
    assert not broker.sent and not broker.previews
    assert "broker_position_held" in caplog.text
    assert "[ROUTE-OWNER-MANUAL-PRESENT]" not in caplog.text


@pytest.mark.parametrize("account,is_us", [(SCHWAB, True), (WEBULL, False)])
def test_failed_live_positions_read_still_refuses_routed_entry(setup, monkeypatch, account, is_us, caplog):
    service, factory, broker = setup
    manual_net(setup, account)

    async def unreadable(_account):
        raise RuntimeError("controlled failed positions read, never flat")
    monkeypatch.setattr(broker, "list_account_positions", unreadable)
    event = build_orb_schwab_open_intent(service.settings, "ABC", Decimal(5),
                                       selection=selected(factory, service.settings, is_us))
    asyncio.run(service.process_trade_intent(event))
    assert not broker.sent and not broker.previews
    assert "broker_position_unknown" in caplog.text
