"""Controlled SCNI/MEDS shapes, not invented historical broker answers."""

from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from project_mai_tai.broker_adapters.protocols import BrokerPositionSnapshot, OrderRequest
from project_mai_tai.db.models import AccountPosition, VirtualPosition
from project_mai_tai.events import TradeIntentEvent, TradeIntentPayload
from project_mai_tai.oms import service as module
from project_mai_tai.oms.webull_owned_positions import bot_owned_quantity
from tests.unit import test_orbown1_exit_quantity as exits
from tests.unit import test_webull_mirror_on_fill as mirror
from tests.unit import test_edgeprot1_webull_edge as edge

@pytest.fixture
def owned_clock(monkeypatch):
    # Match the defining module's retained clock only while its replay is in use.
    edge.owned_clock.__wrapped__(monkeypatch)


@pytest.fixture
def replay(monkeypatch, owned_clock):
    yield from edge.replay.__wrapped__(monkeypatch)


@pytest.fixture(autouse=True)
def controlled_rth_session(monkeypatch):
    monkeypatch.setattr(module, "_is_regular_market_session", lambda now=None: True)


def seed(service, symbol, *, owned=None, strategy_code="orb", operator=1000):
    with service.session_factory.begin() as session:
        strategy = service.store.ensure_strategy(session, strategy_code)
        account = service.store.ensure_broker_account(
            session, mirror.WEBULL, provider="webull", environment="test",
        )
        session.add(AccountPosition(
            broker_account_id=account.id, symbol=symbol,
            quantity=Decimal(operator), average_price=Decimal("2"),
        ))
        if owned is not None:
            session.add(VirtualPosition(
                strategy_id=strategy.id, broker_account_id=account.id,
                symbol=symbol, quantity=Decimal(owned), average_price=Decimal("2"),
            ))
        return strategy.id, account.id


@pytest.mark.parametrize("symbol", ["SCNI", "MEDS"])
@pytest.mark.parametrize("session_label", ["PRE_MARKET", "NORMAL"])
@pytest.mark.asyncio
async def test_operator_holding_does_not_block_either_mirror_entry(symbol, session_label):
    adapter = mirror._CaptureAdapter()
    service = mirror._oms(adapter=adapter)
    seed(service, symbol)
    with service.session_factory() as session:
        assert service._fanout_webull_collision_reason(
            session=session, broker_account_name=mirror.WEBULL, symbol=symbol,
        ) is None
    mirror._seed_fresh_ask(service, symbol, ask=3)
    await service._mirror_v2_fill_to_webull(
        symbol=symbol, quantity=Decimal(2), schwab_fill_price=2.5,
        source_metadata={"session": session_label},
    )
    assert len(adapter.requests) == 1
    assert adapter.requests[0].quantity == 2


@pytest.mark.parametrize("owner", ["orb", "orb_schwab", "schwab_1m_v2"])
@pytest.mark.asyncio
async def test_bot_or_orb_virtual_ownership_blocks_both_entries(owner):
    adapter = mirror._CaptureAdapter()
    service = mirror._oms(adapter=adapter)
    seed(service, "SCNI", owned=2, strategy_code=owner)
    with service.session_factory() as session:
        assert bot_owned_quantity(session, account_name=mirror.WEBULL, symbol="SCNI") == 2
        assert service._fanout_webull_collision_reason(
            session=session, broker_account_name=mirror.WEBULL, symbol="SCNI",
        ) == "fanout_webull_collision_held"
    await service._mirror_v2_fill_to_webull(
        symbol="SCNI", quantity=Decimal(2), schwab_fill_price=2.5, source_metadata={},
    )
    assert adapter.requests == []


@pytest.mark.parametrize("owned,requested,expected", [(2, 10, 2), (2, 1, 1), (0, 10, 0), (None, 10, 0)])
@pytest.mark.asyncio
async def test_stop_rejection_fallback_cannot_sell_operator_or_orb_shares(owned, requested, expected):
    service = mirror._oms(adapter=mirror._CaptureAdapter())
    strategy_id, account_id = seed(service, "SCNI", owned=owned, strategy_code="schwab_1m_v2")
    service._refresh_broker_position_quantity = AsyncMock(return_value=Decimal(1002))
    service._record_order_reports = AsyncMock(return_value=[])
    event = TradeIntentEvent(source_service="test", payload=TradeIntentPayload(
        strategy_code="schwab_1m_v2", broker_account_name=mirror.WEBULL,
        symbol="SCNI", side="sell", intent_type="close",
        quantity=Decimal(requested), reason="HARD_STOP",
    ))
    request = OrderRequest(
        client_order_id="CONTROLLED-fallback", broker_account_name=mirror.WEBULL,
        strategy_code="schwab_1m_v2", symbol="SCNI", side="sell",
        intent_type="close", quantity=Decimal(requested), reason="HARD_STOP",
    )
    with service.session_factory.begin() as session:
        from project_mai_tai.db.models import BrokerAccount, Strategy

        await service._process_stop_reject_market_fallback(
            session=session, strategy=session.get(Strategy, strategy_id),
            broker_account=session.get(BrokerAccount, account_id),
            original_event=event, original_request=request, rejection_reason="stop rejected",
        )
    assert [r.quantity for r in service.broker_adapter.requests] == ([Decimal(expected)] if expected else [])


@pytest.mark.parametrize("tag,bid", [("CW_TARGET", 10.5), ("CW_HARD_STOP", 9.1), ("CW_FLIP", 10.)])
@pytest.mark.asyncio
async def test_managed_exit_never_adopts_1000_operator_shares(monkeypatch, tag, bid):
    service, _, adapter, row_id, _, _, _ = exits.setup(
        monkeypatch, exits.ACCOUNTS[1], managed=2, owned=2, aggregate=1004,
    )
    if tag == "CW_FLIP":
        service._arm_cw_flip_pending(
            (exits.ACCOUNTS[1], "MI"), bar_time_ms=int(datetime.now(UTC).timestamp() * 1000) - 1,
            managed_row_id=str(row_id),
        )
    service._latest_quotes_by_symbol["MI"] = {
        "bid": bid, "ask": bid + .01, "received_at": datetime.now(UTC),
    }
    await service._evaluate_v2_managed_exit(exits.ACCOUNTS[1], "MI")
    assert adapter.submit_order.call_args.args[0].quantity == 2


def test_fills_and_realized_pnl_leave_operator_and_orb_ledger_untouched():
    service = mirror._oms(adapter=mirror._CaptureAdapter())
    v2_id, account_id = seed(service, "SCNI", owned=2, strategy_code="schwab_1m_v2", operator=1004)
    with service.session_factory.begin() as session:
        orb = service.store.ensure_strategy(session, "orb")
        session.add(VirtualPosition(strategy_id=orb.id, broker_account_id=account_id,
                                    symbol="SCNI", quantity=Decimal(2), average_price=Decimal(2)))
        service.store.apply_fill_to_positions(
            session, strategy_id=v2_id, broker_account_id=account_id, symbol="SCNI",
            side="sell", quantity=Decimal(2), price=Decimal(3), reported_at=datetime.now(UTC),
        )
        v2 = service.store.get_virtual_position(session, strategy_id=v2_id,
                                               broker_account_id=account_id, symbol="SCNI")
        assert (v2.quantity, v2.realized_pnl) == (0, 2)
        assert service.store.get_virtual_position(session, strategy_id=orb.id,
                                                 broker_account_id=account_id, symbol="SCNI").quantity == 2
        assert session.scalar(select(AccountPosition.quantity).where(AccountPosition.symbol == "SCNI")) == 1002


@pytest.mark.asyncio
async def test_0930_pair_uses_bound_bot_quantity_not_operator_total(replay):
    service, sessions, wire = replay
    with sessions.begin() as session:
        from project_mai_tai.db.models import BrokerAccount

        account = session.scalar(select(BrokerAccount).where(BrokerAccount.name == edge.ACCT))
        session.add(AccountPosition(broker_account_id=account.id, symbol=edge.SYM,
                                    quantity=Decimal(1120), average_price=Decimal("2.49")))
    await service._v2_rth_edge_bracket()
    assert [r.quantity for r in wire.requests] == [Decimal(120)]
    assert all(leg["quantity"] == "120" for leg in wire.legs[0])


@pytest.mark.asyncio
async def test_borrowed_owned_positions_replay_keeps_original_pre0930_gate(replay, monkeypatch):
    service, _, wire = replay
    before = edge.EDGE.replace(hour=9, minute=29, second=59, microsecond=0).astimezone(UTC)

    class BeforeEdge(datetime):
        @classmethod
        def now(cls, tz=None):
            return before.astimezone(tz) if tz else before.replace(tzinfo=None)

    monkeypatch.setattr(module, "datetime", BeforeEdge)
    monkeypatch.setattr(module, "utcnow", lambda: before)
    wire.fetch_armed_native_oco_symbols = AsyncMock(side_effect=AssertionError("pre0930 broker read"))
    wire.list_account_positions = AsyncMock(side_effect=AssertionError("pre0930 positions read"))
    assert not service._v2_rth_edge_bracket_due()
    await service._v2_rth_edge_bracket()
    wire.fetch_armed_native_oco_symbols.assert_not_awaited()
    wire.list_account_positions.assert_not_awaited()
    assert not wire.requests and not wire.legs


@pytest.mark.asyncio
async def test_operator_coexistence_cannot_be_inferred_as_phantom_flat():
    service = mirror._oms(adapter=mirror._CaptureAdapter())
    service.broker_adapter.list_account_positions = AsyncMock(return_value=[
        BrokerPositionSnapshot(mirror.WEBULL, "SCNI", Decimal(1002), Decimal(2)),
    ])
    assert await service._broker_symbol_is_flat(mirror.WEBULL, "SCNI") is False
