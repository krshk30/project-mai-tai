"""Reviewer DSY-shaped controls; local adapters, not historical broker replay."""


from tests.unit.oms_sqlite_fixtures import owned_sqlite as owned_sqlite
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest
from project_mai_tai.broker_adapters.protocols import OrderRequest
from project_mai_tai.db.models import BrokerAccount, OmsManagedPosition, Strategy, VirtualPosition
from project_mai_tai.events import TradeIntentEvent, TradeIntentPayload
from project_mai_tai.oms import service as module
from project_mai_tai.oms.webull_owned_positions import bot_owned_quantity, fallback_sell_quantity
from tests.unit import test_webull_mirror_on_fill as mirror
from tests.unit.test_wbhold1_owned_positions import seed

ACCOUNT = mirror.WEBULL
STRATEGY = "schwab_1m_v2"


@pytest.fixture(autouse=True)
def controlled_session(monkeypatch):
    monkeypatch.setattr(module, "_is_regular_market_session", lambda now=None: True)
    monkeypatch.setattr(module, "_extended_hours_session", lambda now=None: None)


def held(*, virtual=0, managed=3, owner=STRATEGY):
    service = mirror._oms(adapter=mirror._CaptureAdapter())
    strategy_id, account_id = seed(service, "DSY", owned=virtual, strategy_code=owner)
    with service.session_factory.begin() as session:
        row = service.store.create_managed_position(
            session, strategy_code=owner, broker_account_name=ACCOUNT,
            symbol="DSY", entry_price=Decimal(2), quantity=managed,
        )
        row_id = row.id
    return service, strategy_id, account_id, row_id


@pytest.mark.parametrize("owner", ["orb", "orb_schwab", STRATEGY])
@pytest.mark.asyncio
async def test_zero_virtual_managed_holding_blocks_both_entry_routes(owner):
    service, _, _, _ = held(owner=owner)
    with service.session_factory() as session:
        assert bot_owned_quantity(session, account_name=ACCOUNT, symbol="DSY") == 3
        assert service._fanout_webull_collision_reason(
            session=session, broker_account_name=ACCOUNT, symbol="DSY",
        ) == "fanout_webull_collision_managed"
    mirror._seed_fresh_ask(service, "DSY", ask=2)
    await service._mirror_v2_fill_to_webull(
        symbol="DSY", quantity=Decimal(1), schwab_fill_price=2, source_metadata={},
    )
    assert service.broker_adapter.requests == []


@pytest.mark.parametrize("virtual,managed,expected", [(0, 3, 3), (None, 3, 3), (2, 3, 3), (5, 3, 5), (3, 3, 3)])
def test_owned_totals_use_max_without_double_counting(virtual, managed, expected):
    service, strategy_id, account_id, _ = held(virtual=virtual, managed=managed)
    with service.session_factory() as session:
        assert bot_owned_quantity(session, account_name=ACCOUNT, symbol="DSY") == expected
        assert fallback_sell_quantity(
            session, strategy_id=strategy_id, account_id=account_id, symbol="DSY",
            requested=Decimal(99), broker_available=Decimal(1000),
        ) == expected


def test_collision_sums_all_strategies_but_fallback_keeps_this_strategy():
    service, v2_id, account_id, _ = held(virtual=2, managed=5)
    with service.session_factory.begin() as session:
        orb = service.store.ensure_strategy(session, "orb")
        session.add(VirtualPosition(
            strategy_id=orb.id, broker_account_id=account_id, symbol="DSY",
            quantity=Decimal(7), average_price=Decimal(2),
        ))
        orb_id = orb.id
    with service.session_factory() as session:
        assert bot_owned_quantity(session, account_name=ACCOUNT, symbol="DSY") == 9
        assert fallback_sell_quantity(
            session, strategy_id=v2_id, account_id=account_id, symbol="DSY",
            requested=Decimal(99), broker_available=Decimal(1000),
        ) == 5
        assert fallback_sell_quantity(
            session, strategy_id=orb_id, account_id=account_id, symbol="DSY",
            requested=Decimal(99), broker_available=Decimal(1000),
        ) == 7


@pytest.mark.parametrize("change", ["closed", "account", "symbol", "strategy"])
def test_managed_backstop_cannot_adopt_foreign_or_closed_rows(change):
    service, strategy_id, account_id, row_id = held()
    with service.session_factory.begin() as session:
        row = session.get(OmsManagedPosition, row_id)
        if change == "closed":
            row.status = "closed"
        elif change == "account":
            row.broker_account_name = "other"
        elif change == "symbol":
            row.symbol = "OTHER"
        else:
            row.strategy_code = "orb"
            service.store.ensure_strategy(session, "orb")
    with service.session_factory() as session:
        assert fallback_sell_quantity(
            session, strategy_id=strategy_id, account_id=account_id, symbol="DSY",
            requested=Decimal(99), broker_available=Decimal(1000),
        ) == 0
        assert bot_owned_quantity(session, account_name=ACCOUNT, symbol="DSY") == (3 if change == "strategy" else 0)


@pytest.mark.parametrize("broker_available,event_qty,wire_qty,expected", [
    (1003, 99, 99, 3), (1, 99, 99, 1), (1003, 1, 99, 1),
    (1003, 99, 2, 2), (0, 99, 99, 0), (None, 99, 99, 0),
])
@pytest.mark.asyncio
async def test_zero_virtual_fallback_sells_managed_qty_with_fresh_ceilings(
    broker_available, event_qty, wire_qty, expected,
):
    service, strategy_id, account_id, _ = held()
    service._refresh_broker_position_quantity = AsyncMock(
        return_value=None if broker_available is None else Decimal(broker_available),
    )
    service._record_order_reports = AsyncMock(return_value=[])
    event = TradeIntentEvent(source_service="test", payload=TradeIntentPayload(
        strategy_code=STRATEGY, broker_account_name=ACCOUNT, symbol="DSY", side="sell",
        intent_type="close", quantity=Decimal(event_qty), reason="HARD_STOP",
    ))
    request = OrderRequest(
        client_order_id="CONTROLLED-DSY-fallback", broker_account_name=ACCOUNT,
        strategy_code=STRATEGY, symbol="DSY", side="sell", intent_type="close",
        quantity=Decimal(wire_qty), reason="HARD_STOP",
    )
    with service.session_factory.begin() as session:
        await service._process_stop_reject_market_fallback(
            session=session, strategy=session.get(Strategy, strategy_id),
            broker_account=session.get(BrokerAccount, account_id),
            original_event=event, original_request=request, rejection_reason="stop rejected",
        )
    service._refresh_broker_position_quantity.assert_awaited_once()
    assert [r.quantity for r in service.broker_adapter.requests] == ([Decimal(expected)] if expected else [])


@pytest.mark.asyncio
async def test_fallback_reads_managed_decrease_after_broker_await():
    service, strategy_id, account_id, row_id = held()

    async def refresh(**_kwargs):
        with service.session_factory.begin() as session:
            session.get(OmsManagedPosition, row_id).current_quantity = 1
        return Decimal(1003)

    service._refresh_broker_position_quantity = refresh
    service._record_order_reports = AsyncMock(return_value=[])
    event = TradeIntentEvent(source_service="test", payload=TradeIntentPayload(
        strategy_code=STRATEGY, broker_account_name=ACCOUNT, symbol="DSY", side="sell",
        intent_type="close", quantity=Decimal(3), reason="HARD_STOP",
    ))
    request = OrderRequest(
        client_order_id="CONTROLLED-DSY-decrease", broker_account_name=ACCOUNT,
        strategy_code=STRATEGY, symbol="DSY", side="sell", intent_type="close",
        quantity=Decimal(3), reason="HARD_STOP",
    )
    with service.session_factory.begin() as session:
        await service._process_stop_reject_market_fallback(
            session=session, strategy=session.get(Strategy, strategy_id),
            broker_account=session.get(BrokerAccount, account_id),
            original_event=event, original_request=request, rejection_reason="stop rejected",
        )
    assert [r.quantity for r in service.broker_adapter.requests] == [Decimal(1)]


def test_managed_orb_quantity_does_not_authorize_v2_fallback():
    service, _, account_id, _ = held(owner="orb")
    with service.session_factory.begin() as session:
        v2_id = service.store.ensure_strategy(session, STRATEGY).id
    with service.session_factory() as session:
        assert bot_owned_quantity(session, account_name=ACCOUNT, symbol="DSY") == 3
        assert fallback_sell_quantity(
            session, strategy_id=v2_id, account_id=account_id, symbol="DSY",
            requested=Decimal(99), broker_available=Decimal(1003),
        ) == 0
