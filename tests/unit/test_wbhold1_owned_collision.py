"""H-only controls. Broker boundaries are local fakes, never original live fills."""
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from project_mai_tai.broker_adapters.protocols import BrokerPositionSnapshot, ExecutionReport, OrderRequest
from project_mai_tai.db.models import AccountPosition, BrokerAccount, Fill, OmsManagedPosition, Strategy, VirtualPosition
from project_mai_tai.oms import service as module
from project_mai_tai.oms.webull_owned_positions import bot_owned_quantity
from project_mai_tai.reconciliation import ReconciliationService
from project_mai_tai.services.control_plane import _build_bot_position_rows
from tests.unit import test_oms_intent_refusal_provenance as entry
from tests.unit import test_orbown1_exit_quantity as exits
from tests.unit import test_webull_mirror_on_fill as mirror
from tests.unit.test_edgeprot1_webull_edge import replay as edge_replay  # noqa: F401

ACCOUNT = "live:orb"


@pytest.fixture(autouse=True)
def controlled_session(monkeypatch):
    monkeypatch.setattr(module, "_is_regular_market_session", lambda now=None: True)
    monkeypatch.setattr(module, "_extended_hours_session", lambda now=None: None)
    monkeypatch.setattr(module.OmsRiskService, "_market_is_fillable", lambda self, now=None: True)


def seed(service, factory, *, symbol="SCNI", quantity=1000, owned=0, strategy="orb"):
    with factory.begin() as session:
        account = service.store.ensure_broker_account(
            session, ACCOUNT, provider="webull", environment="test",
        )
        session.add(AccountPosition(broker_account_id=account.id, symbol=symbol,
                                    quantity=Decimal(quantity), average_price=Decimal("1.86")))
        if owned:
            bot = service.store.ensure_strategy(session, strategy)
            session.add(VirtualPosition(strategy_id=bot.id, broker_account_id=account.id,
                                        symbol=symbol, quantity=Decimal(owned),
                                        average_price=Decimal("1.86")))


@pytest.mark.parametrize("symbol", ["SCNI", "MEDS"])
@pytest.mark.parametrize("quantity", [1000, -1000])
def test_manual_account_total_does_not_fence(symbol, quantity):
    factory = entry._session_factory()
    service = entry._service(factory)
    seed(service, factory, symbol=symbol, quantity=quantity)
    with factory() as session:
        assert service._fanout_webull_collision_reason(
            session=session, broker_account_name=ACCOUNT, symbol=symbol,
        ) is None


@pytest.mark.parametrize("strategy", ["orb", "orb_schwab", "schwab_1m_v2"])
@pytest.mark.parametrize("owned", [1, -1])
def test_each_owned_ledger_fences_even_without_broker_backing(strategy, owned):
    factory = entry._session_factory()
    service = entry._service(factory)
    seed(service, factory, quantity=0, owned=owned, strategy=strategy)
    with factory() as session:
        assert service._fanout_webull_collision_reason(
            session=session, broker_account_name=ACCOUNT, symbol="SCNI",
        ) == "fanout_webull_collision_held"


def test_owned_query_is_account_symbol_scoped_and_does_not_net_lots():
    factory = entry._session_factory()
    service = entry._service(factory)
    seed(service, factory, owned=1)
    with factory.begin() as session:
        v2 = service.store.ensure_strategy(session, "schwab_1m_v2")
        account = service.store.ensure_broker_account(
            session, ACCOUNT, provider="webull", environment="test",
        )
        session.add(VirtualPosition(strategy_id=v2.id, broker_account_id=account.id,
                                    symbol="SCNI", quantity=-1, average_price=Decimal("1")))
    with factory() as session:
        assert bot_owned_quantity(session, account_name=ACCOUNT, symbol="SCNI") == 2
        assert bot_owned_quantity(session, account_name="other", symbol="SCNI") == 0
        assert bot_owned_quantity(session, account_name=ACCOUNT, symbol="OTHER") == 0
    with factory.begin() as session:
        for row in session.scalars(select(VirtualPosition)):
            row.quantity = 0
    with factory() as session:
        assert bot_owned_quantity(session, account_name=ACCOUNT, symbol="SCNI") == 0


@pytest.mark.parametrize("fence", ["armed", "managed"])
def test_armed_and_managed_fences_unchanged(fence):
    factory = entry._session_factory()
    service = entry._service(factory)
    seed(service, factory)
    if fence == "armed":
        service._armed_hard_stops[("orb", ACCOUNT, "SCNI")] = SimpleNamespace(
            broker_account_name=ACCOUNT, symbol="SCNI",
        )
    else:
        with factory.begin() as session:
            service.store.create_managed_position(
                session, strategy_code="orb", broker_account_name=ACCOUNT, symbol="SCNI",
                entry_price=Decimal("1.86"), quantity=1,
            )
    with factory() as session:
        assert service._fanout_webull_collision_reason(
            session=session, broker_account_name=ACCOUNT, symbol="SCNI",
        ) == f"fanout_webull_collision_{fence}"


@pytest.mark.asyncio
@pytest.mark.parametrize("symbol", ["SCNI", "MEDS"])
@pytest.mark.parametrize("session_code", ["AM", "NORMAL", "PM"])
@pytest.mark.parametrize("owned", [0, 2])
async def test_controlled_named_leg_proceeds_or_orb_fences(monkeypatch, symbol, session_code, owned):
    factory = entry._session_factory()
    adapter = mirror._CaptureAdapter()
    service = entry._service(
        factory, adapter=adapter, strategy_schwab_1m_v2_dual_broker_fanout_enabled=True,
        strategy_schwab_1m_v2_webull_account_name=ACCOUNT,
    )
    seed(service, factory, symbol=symbol, owned=owned)
    monkeypatch.setattr(module, "_extended_hours_session", lambda now=None: None if session_code == "NORMAL" else session_code)
    monkeypatch.setattr(module, "_is_regular_market_session", lambda now=None: session_code == "NORMAL")
    monkeypatch.setattr(service, "_market_is_fillable", lambda now=None: True)
    result = await service.process_trade_intent(entry._v2_open(symbol))
    if owned:
        assert not adapter.requests
        assert result[-1].payload.reason == "fanout_webull_collision_held"
    else:
        assert len(adapter.requests) == 1
        assert adapter.requests[0].quantity == 1
        assert result[-1].payload.status == "accepted"


@pytest.mark.asyncio
@pytest.mark.parametrize("owned", [0, 2])
async def test_legacy_mirror_uses_same_ownership_definition(owned):
    adapter = mirror._CaptureAdapter()
    service = mirror._oms(adapter=adapter, webull_account=ACCOUNT)
    seed(service, service.session_factory, owned=owned)
    mirror._seed_fresh_ask(service, "SCNI", ask=1.86)
    await service._mirror_v2_fill_to_webull(
        symbol="SCNI", quantity=Decimal("3"), schwab_fill_price=1.86, source_metadata={},
    )
    assert len(adapter.requests) == (0 if owned else 1)


def coexist(monkeypatch, *, fill=False, session_code="NORMAL"):
    result = exits.setup(monkeypatch, ACCOUNT, managed=3, owned=3, aggregate=1003, fill=fill)
    service, factory, adapter, row_id, position, v2_id, broker_id = result
    with factory.begin() as session:
        for row in session.scalars(select(VirtualPosition)):
            if row.strategy_id != v2_id or row.broker_account_id != broker_id or row.symbol != "MI":
                session.delete(row)  # isolate manual 1000 + bot 3 from the helper's other lots
    monkeypatch.setattr(module, "_extended_hours_session", lambda now=None: None if session_code == "NORMAL" else session_code)
    monkeypatch.setattr(module, "_is_regular_market_session", lambda now=None: session_code == "NORMAL")
    adapter.list_account_positions.return_value = [BrokerPositionSnapshot(
        broker_account_name=ACCOUNT, symbol="MI", quantity=Decimal(1003), average_price=Decimal(10),
    )]
    return result


@pytest.mark.asyncio
@pytest.mark.parametrize("tag,bid", [("CW_TARGET", 10.5), ("CW_HARD_STOP", 9.1), ("CW_FLIP", 10.)])
@pytest.mark.parametrize("session_code", ["AM", "NORMAL", "PM"])
async def test_real_evaluator_sells_only_bot_quantity_with_manual_1000(monkeypatch, tag, bid, session_code):
    service, factory, adapter, row_id, _, v2_id, broker_id = coexist(monkeypatch, session_code=session_code, fill=True)
    if tag == "CW_FLIP":
        service._arm_cw_flip_pending((ACCOUNT, "MI"),
            bar_time_ms=int(datetime.now(UTC).timestamp() * 1000) - 1, managed_row_id=str(row_id))
    service._latest_quotes_by_symbol["MI"] = {"bid": bid, "ask": bid + .01, "received_at": datetime.now(UTC)}
    await service._evaluate_v2_managed_exit(ACCOUNT, "MI")
    adapter.submit_order.assert_awaited_once()
    request = adapter.submit_order.call_args.args[0]
    assert request.quantity == 3 and request.reason == f"oms_v2_managed_exit:{tag}"
    assert request.metadata["session"] == session_code
    with factory() as session:
        assert service.store.get_virtual_position(session, strategy_id=v2_id,
            broker_account_id=broker_id, symbol="MI").quantity == 0
        assert service.store.get_account_position(session, broker_account_id=broker_id, symbol="MI").quantity == 1000
        assert session.get(OmsManagedPosition, row_id).status == "closed"
        fill = session.scalar(select(Fill))
        assert fill.quantity == 3 and fill.strategy_id == v2_id


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["confirmation", "eod_pm", "overnight", "handover"])
async def test_other_exit_routes_use_managed_quantity(monkeypatch, path):
    service, factory, adapter, row_id, position, _, _ = coexist(monkeypatch, session_code="PM")
    service._latest_quotes_by_symbol["MI"] = {"bid": 10., "ask": 10.01, "received_at": datetime.now(UTC)}
    if path == "confirmation":
        await service._emit_v2_exit_on_loop_claimed(
            ACCOUNT, "MI", position, 10., kind="CW_FLIP", reference_price=10.,
            reason="oms_v2_managed_exit:CONFIRMATION_EXIT", bid=10., close_on_fill=True,
            expected_managed_row_id=str(row_id),
        )
    elif path == "eod_pm":
        adapter.fetch_exit_legs_for_entry.return_value = {"unsafe": False, "filled": False}
        assert await service._v2_eod_place_pm_exit(ACCOUNT, "MI", True)
    elif path == "overnight":
        service.settings.oms_v2_overnight_flatten_enabled = True
        service._v2_overnight_flatten_due = lambda now=None: True
        await service._v2_overnight_flatten()
    else:
        service.settings.oms_v2_eod_oco_transition_enabled = True
        service._v2_eod_oco_transition_due = lambda now=None: True
        service._release_v2_eod_oco = AsyncMock(return_value=exits.ExitPairReleaseResult(outcome="released"))
        service._latest_quotes_by_symbol["MI"]["bid"] = 9.1  # handover to hard stop
        await service._v2_eod_oco_transition()
        service._release_v2_eod_oco.assert_awaited_once()
    adapter.submit_order.assert_awaited_once()
    assert adapter.submit_order.call_args.args[0].quantity == 3


@pytest.mark.asyncio
async def test_edge_pair_uses_120_bot_shares_with_manual_1000(edge_replay):  # noqa: F811
    service, factory, wire = edge_replay
    from tests.unit import test_edgeprot1_webull_edge as edge
    with factory.begin() as session:
        order = edge._entry(session)
        session.add_all([
            AccountPosition(broker_account_id=order.broker_account_id, symbol=edge.SYM,
                            quantity=1120, average_price=Decimal("2.49")),
            VirtualPosition(strategy_id=order.strategy_id, broker_account_id=order.broker_account_id,
                            symbol=edge.SYM, quantity=120, average_price=Decimal("2.49")),
        ])
    wire.list_account_positions = AsyncMock(return_value=[BrokerPositionSnapshot(
        edge.ACCT, edge.SYM, Decimal(1120), Decimal("2.49"),
    )])
    await service._v2_rth_edge_bracket()
    assert len(wire.requests) == 1
    assert wire.requests[0].quantity == 120
    assert all(Decimal(leg["quantity"]) == 120 for leg in wire.legs[0])


@pytest.mark.asyncio
@pytest.mark.parametrize("remaining", [20, 0, "entry_changed"])
async def test_bound_edge_attach_rechecks_owned_quantity_after_broker_await(edge_replay, remaining):  # noqa: F811
    service, factory, wire = edge_replay
    from tests.unit import test_edgeprot1_webull_edge as edge
    with factory.begin() as session:
        order = edge._entry(session)
        session.add(VirtualPosition(strategy_id=order.strategy_id, broker_account_id=order.broker_account_id,
                                    symbol=edge.SYM, quantity=120, average_price=Decimal("2.49")))

    async def changed_while_reading(account):
        with factory.begin() as session:
            if remaining == "entry_changed":
                edge._row(session).entry_client_order_id = "CONTROLLED-new-episode"
            else:
                edge._row(session).current_quantity = remaining
                session.scalar(select(VirtualPosition).where(VirtualPosition.symbol == edge.SYM)).quantity = remaining
        return [BrokerPositionSnapshot(account, edge.SYM, Decimal(1120), Decimal("2.49"))]

    wire.list_account_positions = changed_while_reading
    await service._v2_rth_edge_bracket()
    assert [request.quantity for request in wire.requests] == ([Decimal(remaining)] if remaining == 20 else [])


@pytest.mark.asyncio
async def test_bound_attach_retries_when_fill_row_is_not_yet_visible(edge_replay, monkeypatch):  # noqa: F811
    service, factory, wire = edge_replay
    from tests.unit import test_edgeprot1_webull_edge as edge
    object.__setattr__(service.settings, "oms_webull_protect_attempts", 2)
    object.__setattr__(service.settings, "oms_webull_protect_interval_seconds", 1)
    reads = []

    async def not_yet_visible(account):
        reads.append(account)
        if len(reads) == 1:
            with factory.begin() as session:
                edge._row(session).status = "closed"  # open-row lookup has the same missing result
        return [BrokerPositionSnapshot(account, edge.SYM, Decimal(1120), Decimal("2.49"))]

    async def commit_becomes_visible(_seconds):
        with factory.begin() as session:
            edge._row(session).status = "open"

    wire.list_account_positions = not_yet_visible
    monkeypatch.setattr(module.asyncio, "sleep", commit_becomes_visible)
    await service._v2_rth_edge_bracket()
    assert len(reads) == 2
    assert [request.quantity for request in wire.requests] == [Decimal(120)]


@pytest.mark.asyncio
async def test_coexistence_reconcile_never_clears_live_bot_row(monkeypatch):
    service, factory, _, row_id, _, v2_id, broker_id = coexist(monkeypatch)
    with factory() as session:
        assert service.store.clear_virtual_positions_without_account_backing(session) == []
        row = session.get(OmsManagedPosition, row_id)
        for _ in range(service._V2_EXIT_RECONCILE_AFTER_FAILURES + 1):
            assert not await service._v2_close_reconcile_flat(session, ACCOUNT, "MI", row)
        assert row.status == "open" and row.current_quantity == 3
        ledger = service.store.get_virtual_position(session, strategy_id=v2_id,
            broker_account_id=broker_id, symbol="MI")
        ledger.quantity = 0
        session.flush()
        assert service.store.restore_virtual_positions_from_managed(session) == [(broker_id, "MI", Decimal(3))]
        assert ledger.quantity == 3  # restores bot 3, never broker 1003


def test_page_manual_only_absent_and_bot_pnl_uses_three_shares():
    account = {"broker_account_name": ACCOUNT, "symbol": "SCNI", "quantity": 1003,
               "market_value": 2006}
    bot = {"strategy_code": "schwab_1m_v2", "account_name": ACCOUNT,
           "runtime_kind": "schwab_1m_v2", "positions": []}
    data = {"virtual_positions": [], "account_positions": [account]}
    assert "No open positions" in _build_bot_position_rows(data, bot)
    bot["positions"] = [{"ticker": "SCNI", "quantity": 3, "entry_price": 1.86}]
    data["virtual_positions"] = [{"strategy_code": "schwab_1m_v2", "broker_account_name": ACCOUNT,
                                  "symbol": "SCNI", "quantity": 3, "average_price": 1.86}]
    html = _build_bot_position_rows(data, bot)
    assert html.count("<tr") == 1
    assert "$+0.42" in html and "$+140.42" not in html
    # Broker Qty is an explicitly separate account-total column, never Bot Qty or P&L.
    assert ">1003<" in html


@pytest.mark.asyncio
@pytest.mark.parametrize("event_qty,request_qty,broker_qty,expected", [
    (1003, 3, 1003, 3), (1003, 1, 1003, 1), (1, 3, 1003, 1), (1003, 3, 2, 2),
])
async def test_real_fallback_records_only_own_capped_fill(monkeypatch, event_qty, request_qty, broker_qty, expected):
    service, factory, adapter, row_id, _, v2_id, broker_id = coexist(monkeypatch, fill=True)
    adapter.list_account_positions.return_value = [BrokerPositionSnapshot(
        ACCOUNT, "MI", Decimal(broker_qty), Decimal(10),
    )]
    with factory.begin() as session:
        orb = service.store.ensure_strategy(session, "orb")
        session.add(VirtualPosition(strategy_id=orb.id, broker_account_id=broker_id,
                                    symbol="MI", quantity=500, average_price=Decimal(9)))
        orb_id = orb.id
    event = entry._v2_close("MI")
    event.payload.quantity = Decimal(event_qty)
    request = OrderRequest(client_order_id="CONTROLLED-stop-refused",
        broker_account_name=ACCOUNT, strategy_code="schwab_1m_v2", symbol="MI",
        side="sell", intent_type="close", quantity=Decimal(request_qty), reason="HARD_STOP")
    with factory.begin() as session:
        await service._process_stop_reject_market_fallback(
            session=session, strategy=session.get(Strategy, v2_id),
            broker_account=session.get(BrokerAccount, broker_id), original_event=event,
            original_request=request, rejection_reason="CONTROLLED stop rejected",
        )
    adapter.submit_order.assert_awaited_once()
    assert adapter.submit_order.call_args.args[0].quantity == expected
    with factory() as session:
        fill = session.scalar(select(Fill))
        assert fill.quantity == expected and fill.strategy_id == v2_id
        owned = service.store.get_virtual_position(session, strategy_id=v2_id,
            broker_account_id=broker_id, symbol="MI")
        assert owned.quantity == 3 - expected
        assert service.store.get_virtual_position(session, strategy_id=orb_id,
            broker_account_id=broker_id, symbol="MI").quantity == 500
        assert session.get(OmsManagedPosition, row_id).current_quantity == 3 - expected


@pytest.mark.asyncio
async def test_real_buy_and_oversized_close_book_only_bot_fills_and_pnl():
    factory = entry._session_factory()
    adapter = mirror._CaptureAdapter()
    service = entry._service(factory, adapter=adapter,
        strategy_schwab_1m_v2_dual_broker_fanout_enabled=True,
        strategy_schwab_1m_v2_webull_account_name=ACCOUNT)
    seed(service, factory)

    async def fill(request):
        adapter.requests.append(request)
        return [ExecutionReport(
            event_type="filled", origin="broker", client_order_id=request.client_order_id,
            broker_order_id=f"CONTROLLED-{request.side}", broker_fill_id=f"CONTROLLED-{request.side}",
            symbol=request.symbol, side=request.side, intent_type=request.intent_type,
            quantity=request.quantity, filled_quantity=request.quantity,
            fill_price=Decimal("2.00") if request.side == "buy" else Decimal("2.20"),
        )]

    adapter.submit_order = fill

    async def local_positions(account_name):
        quantity = Decimal(1000) + sum(
            request.quantity if request.side == "buy" else -request.quantity
            for request in adapter.requests
        )
        return [BrokerPositionSnapshot(account_name, "SCNI", quantity, Decimal("1.86"))]

    adapter.list_account_positions = local_positions
    buy = entry._v2_open("SCNI")
    buy.payload.quantity = Decimal(3)
    assert (await service.process_trade_intent(buy))[-1].payload.status == "filled"
    with factory() as session:
        ledger = session.scalar(select(VirtualPosition).where(VirtualPosition.symbol == "SCNI"))
        assert ledger.quantity == 3 and ledger.average_price == Decimal("2.00")
        broker = session.scalar(select(AccountPosition).where(AccountPosition.symbol == "SCNI"))
        assert broker.quantity == 1003
        reconciler = ReconciliationService(settings=service.settings,
            redis_client=service.redis, session_factory=factory)
        findings = reconciler._build_position_findings(session)
        mismatch = next(f for f in findings if f.finding_type == "position_quantity_mismatch")
        assert mismatch.payload["our_quantity"] == "3.00000000"
        assert mismatch.payload["direction"] == "broker_more_than_net_fills"
        assert mismatch.severity == "critical"  # existing safety signal is disclosed, not suppressed
        assert ledger.quantity == 3 and broker.quantity == 1003
    close = entry._v2_close("SCNI")
    close.payload.quantity = Decimal(1003)  # even a wrongly oversized intent is capped
    assert (await service.process_trade_intent(close))[-1].payload.status == "filled"
    assert [r.quantity for r in adapter.requests] == [3, 3]
    with factory() as session:
        ledger = session.scalar(select(VirtualPosition).where(VirtualPosition.symbol == "SCNI"))
        assert ledger.quantity == 0 and ledger.realized_pnl == Decimal("0.60")
        broker = session.scalar(select(AccountPosition).where(AccountPosition.symbol == "SCNI"))
        assert broker.quantity == 1000
        fills = session.scalars(select(Fill)).all()
        assert len(fills) == 2 and all(f.quantity == 3 and f.strategy_id == ledger.strategy_id for f in fills)
