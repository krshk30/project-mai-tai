"""Controlled durable-preparation boundaries, not real broker execution proof."""

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D
import threading
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from project_mai_tai.broker_adapters.protocols import OrderRequest
from project_mai_tai.broker_adapters.protocols import ExecutionReport, BrokerPositionSnapshot
from project_mai_tai.db.base import Base
from project_mai_tai.db.models import (
    BrokerAccount, BrokerOrder, OmsManagedPosition, RiskCheck, Strategy, TradeIntent, VirtualPosition,
)
from project_mai_tai.events import TradeIntentEvent, TradeIntentPayload
from project_mai_tai.oms.managed_sell_prewire import abandon_unposted_sell, prepare_managed_sell
from project_mai_tai.oms.managed_sell_retirement import retire_previous_flat_sell
from project_mai_tai.oms.service import OmsRiskService
from project_mai_tai.settings import Settings


@pytest.fixture(params=[("live:schwab_1m_v2", "schwab"), ("live:orb", "webull")])
def lane(tmp_path, request):
    account_name, provider = request.param
    engine = create_engine(f"sqlite:///{tmp_path / 'prewire.sqlite'}")
    Base.metadata.create_all(engine, tables=[table for table in Base.metadata.sorted_tables
                                            if table.name not in {"market_trade_ticks", "market_quote_ticks"}])
    model_index = next(index for index in OmsManagedPosition.__table__.indexes
                      if index.name == "uq_oms_managed_positions_open_symbol")
    assert str(model_index.dialect_options["postgresql"]["where"]) == "status = 'open'"
    # Mirror the production OPEN-only uniqueness in this local SQLite fixture.
    with engine.begin() as connection:
        connection.exec_driver_sql("DROP INDEX uq_oms_managed_positions_open_symbol")
        connection.exec_driver_sql(
            "CREATE UNIQUE INDEX uq_oms_managed_positions_open_symbol "
            "ON oms_managed_positions (broker_account_name, symbol) WHERE status = 'open'")
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    service = OmsRiskService(Settings(_env_file=None, oms_adapter="paper", bot_control_enabled=False),
                             redis_client=None, session_factory=sessions, broker_adapter=SimpleNamespace())
    with sessions.begin() as unit:
        strategy = service.store.ensure_strategy(unit, "schwab_1m_v2", name="ATR", execution_mode="live")
        account = service.store.ensure_broker_account(unit, account_name, provider=provider, environment="production")
        row = service.store.create_managed_position(unit, strategy_code=strategy.code,
            broker_account_name=account_name, symbol="ABC", entry_price=D(5), quantity=10)
        unit.add(VirtualPosition(strategy_id=strategy.id, broker_account_id=account.id,
                                 symbol="ABC", quantity=D(10), average_price=D(5)))
        unit.flush()
        row_id = str(row.id)
    yield service, sessions, row_id, account_name
    engine.dispose()


def attempt(account, *, session="NORMAL", quantity=D(10), retry_of=None):
    metadata = {"oms_v2_managed_exit": "true", "session": session, "reference_price": "4.5"}
    if retry_of is not None:
        metadata.update(reserve1_retry="1", reserve1_retry_of=retry_of)
    event = TradeIntentEvent(source_service="controlled-prewire", payload=TradeIntentPayload(
        strategy_code="schwab_1m_v2", broker_account_name=account, symbol="ABC",
        side="sell", intent_type="close", quantity=quantity, reason="CONTROLLED_PREWIRE",
        metadata=dict(metadata)))
    request = OrderRequest(f"controlled-{uuid4().hex}", account, "schwab_1m_v2", "ABC",
                           "sell", "close", quantity, event.payload.reason, metadata,
                           order_type="market" if session == "NORMAL" else "limit", time_in_force="day")
    return event, request


@pytest.mark.asyncio
@pytest.mark.parametrize("market_session", ["PM", "NORMAL"])
@pytest.mark.parametrize("paper", [False, True])
async def test_managed_only_commits_without_inventing_vp_before_first_wire(lane, market_session, paper):
    service, sessions, row_id, account_name = lane
    with sessions.begin() as unit:
        row = unit.get(OmsManagedPosition, UUID(row_id))
        account = unit.scalar(select(BrokerAccount).where(BrokerAccount.name == account_name))
        unit.delete(unit.scalar(select(VirtualPosition)))
        if paper:
            account.name = "paper:webull_v2" if account.provider == "webull" else "paper:schwab_1m_v2"
            account_name = row.broker_account_name = account.name
            unit.scalar(select(Strategy)).execution_mode = "paper"
        assert account.environment == "production"  # Deployment environment is not execution mode.
    event, request = attempt(account_name, session=market_session)
    result = await prepare_managed_sell(service, event=event, request=request,
        managed_row_id=row_id, now=datetime.now(UTC))
    assert result is not None
    wires = []

    async def first_wire(actual):
        with sessions() as unit:
            order = unit.scalar(select(BrokerOrder).where(BrokerOrder.client_order_id == actual.client_order_id))
            assert order.intent_id == result and order.status == "pending"
            assert unit.get(TradeIntent, result).status == "pending"
            assert order.broker_order_id is None and order.submitted_at is None
            assert order.quantity == D(10) and order.payload["oms_managed_row_id"] == row_id
            assert order.payload["session"] == market_session
            assert unit.scalar(select(VirtualPosition)) is None
        wires.append(actual)
        return []

    service.broker_adapter = SimpleNamespace(submit_order=first_wire)
    await service.broker_adapter.submit_order(request)
    second_event, second = attempt(account_name, session=market_session)
    assert await prepare_managed_sell(service, event=second_event, request=second,
        managed_row_id=row_id, now=datetime.now(UTC)) is None
    assert wires == [request]


@pytest.mark.asyncio
@pytest.mark.parametrize("quantity", [D(0), D(9), D("NaN"), D("Infinity"), D(-1)])
async def test_present_invalid_or_insufficient_vp_is_not_managed_only(lane, monkeypatch, quantity):
    service, sessions, row_id, account = lane
    # Nonfinite values cannot round-trip through SQLite Numeric. Exercise the
    # actual decoder boundary without turning a PRESENT invalid row into absence.
    monkeypatch.setattr(service.store, "get_virtual_position",
        lambda *_args, **_kwargs: SimpleNamespace(quantity=quantity))
    event, request = attempt(account)
    assert await prepare_managed_sell(service, event=event, request=request,
        managed_row_id=row_id, now=datetime.now(UTC)) is None
    with sessions() as unit:
        assert unit.scalar(select(BrokerOrder)) is None
        assert unit.scalar(select(TradeIntent)) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("proof", ["partial", "flat", "overfill", "wrong-fill-id", "missing-entry-fill"])
async def test_managed_only_rechecks_exact_native_history_inside_prepare(lane, proof):
    from project_mai_tai.db.models import AccountPosition, Fill
    from project_mai_tai.oms.service import oco_exit_client_order_id
    from tests.unit.managed_entry_fixtures import bind_managed_entry

    service, sessions, row_id, account = lane
    with sessions.begin() as unit:
        row = unit.get(OmsManagedPosition, UUID(row_id))
        unit.delete(unit.scalar(select(VirtualPosition)))
        entry = bind_managed_entry(unit, row)
        unit.add(AccountPosition(broker_account_id=entry.broker_account_id, symbol="ABC",
                                 quantity=D(110), average_price=D(5)))
        if proof != "missing-entry-fill":
            unit.add(Fill(order_id=entry.id, strategy_id=entry.strategy_id,
                broker_account_id=entry.broker_account_id, broker_fill_id="own-entry-fill",
                symbol="ABC", side="buy", quantity=D(10), price=D(5), filled_at=datetime.now(UTC)))
        sold = D(10) if proof == "flat" else D(11) if proof == "overfill" else D(3)
        child_id = "owned-native-child"
        child = BrokerOrder(intent_id=entry.intent_id, strategy_id=entry.strategy_id,
            broker_account_id=entry.broker_account_id,
            client_order_id=oco_exit_client_order_id(entry.client_order_id, child_id),
            broker_order_id=child_id, symbol="ABC", side="sell", order_type="oco_exit",
            quantity=sold, status="filled", time_in_force="day", payload={})
        unit.add(child)
        unit.flush()
        unit.add(Fill(order_id=child.id, strategy_id=entry.strategy_id,
            broker_account_id=entry.broker_account_id,
            broker_fill_id="foreign-fill:3" if proof == "wrong-fill-id" else f"{child_id}:cumulative",
            symbol="ABC", side="sell", quantity=sold, price=D(5), filled_at=datetime.now(UTC)))
    event, request = attempt(account, quantity=D(7))
    # No caller boolean or previously cached owned quantity is passed to prepare.
    result = await prepare_managed_sell(service, event=event, request=request,
        managed_row_id=row_id, now=datetime.now(UTC))
    assert (result is not None) is (proof == "partial")
    with sessions() as unit:
        prepared = unit.scalar(select(BrokerOrder).where(BrokerOrder.client_order_id == request.client_order_id))
        assert (prepared is not None) is (proof == "partial")
        assert unit.scalar(select(VirtualPosition)) is None
        assert unit.scalar(select(AccountPosition)).quantity == D(110)
        if prepared is not None:
            assert prepared.quantity == D(7) and prepared.status == "pending"
            assert prepared.submitted_at is None and prepared.broker_order_id is None


@pytest.mark.asyncio
@pytest.mark.parametrize("fence", ["pending", "target", "changed-row"])
async def test_managed_only_does_not_bypass_pending_target_or_episode(lane, fence):
    service, sessions, row_id, account = lane
    with sessions.begin() as unit:
        unit.delete(unit.scalar(select(VirtualPosition)))
    context = None
    if fence == "pending":
        service._oco_exit_fill_pending = {(account, "ABC"): SimpleNamespace(row_id=row_id)}
    elif fence == "target":
        context = {"cw_target_bound_row_id": str(uuid4()), "cw_target_entry_order_id": "",
                   "cw_target_entry_price": "5", "cw_target_quantity": "10"}
    elif fence == "changed-row":
        row_id = str(uuid4())
    event, request = attempt(account)
    assert await prepare_managed_sell(service, event=event, request=request,
        managed_row_id=row_id, now=datetime.now(UTC), target_context=context) is None
    with sessions() as unit:
        assert unit.scalar(select(BrokerOrder)) is None
        assert unit.scalar(select(TradeIntent)) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("binding", ["unbound", "coid-only", "declared-missing"])
@pytest.mark.parametrize("ledger", ["absent", "zero", "short", "nonfinite"])
async def test_resolver_none_keeps_original_managed_only_policy_not_present_vp_override(
    lane, monkeypatch, binding, ledger,
):
    service, sessions, row_id, account = lane
    with sessions.begin() as unit:
        row = unit.get(OmsManagedPosition, UUID(row_id))
        row.entry_order_id = uuid4() if binding == "declared-missing" else None
        row.entry_client_order_id = "legacy-missing-entry" if binding != "unbound" else None
        own = unit.scalar(select(VirtualPosition))
        if ledger == "absent":
            unit.delete(own)
        elif ledger in {"zero", "short"}:
            own.quantity = D(0) if ledger == "zero" else D(9)
        # The original real resolver/native arbiter, not a mocked positive proof.
        assert service._find_oco_entry_order(unit, account, "ABC", row=row) is None
        assert service._stored_owned_oco_balance(unit, row) is None
        original_binding = row.entry_order_id, row.entry_client_order_id
    if ledger == "nonfinite":
        # SQLite Numeric cannot retain NaN; this is an explicit invalid decoder
        # response, never an absent ledger or a synthesized positive quantity.
        monkeypatch.setattr(service.store, "get_virtual_position",
            lambda *_args, **_kwargs: SimpleNamespace(quantity=D("NaN")))
    if ledger == "absent":
        assert await service._v2_managed_sell_owned_quantity(managed_row_id=row_id,
            strategy_code="schwab_1m_v2", broker_account_name=account,
            symbol="ABC", requested_quantity=10) == 10
    event, request = attempt(account)
    result = await prepare_managed_sell(service, event=event, request=request,
        managed_row_id=row_id, now=datetime.now(UTC))
    assert (result is not None) is (ledger == "absent")
    with sessions() as unit:
        row = unit.get(OmsManagedPosition, UUID(row_id))
        assert (row.entry_order_id, row.entry_client_order_id) == original_binding
        assert row.current_quantity == 10
        orders = unit.scalars(select(BrokerOrder)).all()
        intents = unit.scalars(select(TradeIntent)).all()
        if ledger == "absent":
            assert len(orders) == len(intents) == 1
            assert orders[0].side == intents[0].side == "sell"
            assert orders[0].quantity == intents[0].quantity == D(10)
            assert orders[0].status == intents[0].status == "pending"
            assert orders[0].submitted_at is None and orders[0].broker_order_id is None
            assert unit.scalar(select(VirtualPosition)) is None
            if binding == "declared-missing":
                assert orders[0].payload["entry_order_id"] == str(original_binding[0])
        else:
            assert not orders and not intents
            assert unit.scalar(select(VirtualPosition)) is not None


@pytest.mark.asyncio
async def test_managed_only_protective_pending_writer_permission_is_explicit(lane):
    service, sessions, row_id, account = lane
    with sessions.begin() as unit:
        unit.delete(unit.scalar(select(VirtualPosition)))
    writer = SimpleNamespace(row_id=row_id)
    service._oco_exit_fill_pending = {(account, "ABC"): writer}
    event, request = attempt(account)
    assert await prepare_managed_sell(service, event=event, request=request,
        managed_row_id=row_id, now=datetime.now(UTC), allow_pending_fill_writer=True) is not None
    assert service._oco_exit_fill_pending[(account, "ABC")] is writer


@pytest.mark.asyncio
async def test_bound_riskoff_owner_still_fences_normal_outer_seller(lane):
    from project_mai_tai.oms.webull_attach_riskoff import KEY
    from tests.unit.managed_entry_fixtures import bind_managed_entry

    service, sessions, row_id, account = lane
    with sessions.begin() as unit:
        row = unit.get(OmsManagedPosition, UUID(row_id))
        entry = bind_managed_entry(unit, row)
        entry.payload = {**entry.payload, KEY: {"exit_phase": "unknown"}}
    calls = []

    async def no_wire(request):
        calls.append(request)
        raise AssertionError("normal seller cannot replace an unknown riskoff owner")

    service.broker_adapter = SimpleNamespace(submit_order=no_wire)
    result = await service._emit_v2_exit_on_loop(account, "ABC", SimpleNamespace(quantity=10), 5.0,
        kind="HARD", reference_price=4.5, reason="oms_v2_managed_exit:CW_HARD_STOP",
        bid=4.5, close_on_fill=True)
    assert result == "refused" and calls == []
    with sessions() as unit:
        assert unit.scalar(select(BrokerOrder).where(BrokerOrder.side == "sell")) is None


def test_fixture_preserves_production_two_open_lots_constraint(lane):
    service, sessions, row_id, account = lane
    with pytest.raises(IntegrityError, match="UNIQUE constraint failed"):
        with sessions.begin() as unit:
            service.store.create_managed_position(unit, strategy_code="schwab_1m_v2",
                broker_account_name=account, symbol="ABC", entry_price=D(6), quantity=10)
    with sessions() as unit:
        rows = list(unit.scalars(select(OmsManagedPosition)))
        assert len(rows) == 1 and str(rows[0].id) == row_id
        assert rows[0].status == "open" and rows[0].current_quantity == 10
        assert unit.scalar(select(VirtualPosition)).quantity == D(10)


@pytest.mark.asyncio
@pytest.mark.parametrize("proof", ["fresh", "stale", "future", "preterminal"])
async def test_retirement_compares_wire_stamp_after_read_helper_without_refreshing_it(lane, monkeypatch, proof):
    from project_mai_tai.db.models import BrokerOrderEvent, Fill
    from project_mai_tai.oms import managed_sell_retirement, service as service_module

    service, sessions, row_id, account = lane
    wall = [datetime(2026, 10, 16, 14, 0, tzinfo=UTC)]

    class ClockMeta(type):
        def __instancecheck__(cls, value):
            return isinstance(value, datetime)

    class Clock(datetime, metaclass=ClockMeta):
        @classmethod
        def now(cls, tz=None):
            return wall[0].astimezone(tz) if tz else wall[0].replace(tzinfo=None)

    def helper_clock():
        # Actual uncached helper can sample later than the caller's prior clock.
        wall[0] += timedelta(microseconds=1)
        return wall[0]

    monkeypatch.setattr(managed_sell_retirement, "datetime", Clock)
    monkeypatch.setattr(service_module, "utcnow", helper_clock)
    event, request = attempt(account)
    old = wall[0] - timedelta(days=2)
    intent_id = await prepare_managed_sell(service, event=event, request=request,
        managed_row_id=row_id, now=old)
    assert intent_id is not None
    with sessions.begin() as unit:
        order = unit.scalar(select(BrokerOrder).where(BrokerOrder.intent_id == intent_id))
        order.broker_order_id, order.submitted_at = "exact-old-sell", old
        order_id = order.id
        unit.get(TradeIntent, intent_id).created_at = old
        unit.get(OmsManagedPosition, UUID(row_id)).current_quantity = 0
        unit.scalar(select(VirtualPosition)).quantity = D(0)
        provider = unit.scalar(select(BrokerAccount.provider).where(BrokerAccount.name == account))
    reads = []

    async def terminal(original):
        reads.append(("terminal", wall[0]))
        return ExecutionReport("filled", original.client_order_id, broker_order_id="exact-old-sell",
            symbol="ABC", side="sell", intent_type="close", quantity=D(10), filled_quantity=D(10),
            fill_price=D(5), broker_fill_id="exact-old-fill", origin="broker", reported_at=wall[0])

    async def positions(name):
        assert name == account
        wall[0] += timedelta(seconds=1)
        reads.append(("positions", wall[0]))
        return []

    def forbidden(*_args, **_kwargs):
        raise AssertionError("retirement cannot submit, cancel or book a historical fill")

    service.broker_adapter = SimpleNamespace(fetch_order_update=terminal,
        list_account_positions=positions, submit_order=forbidden, cancel_exit_pair=forbidden,
        positions_wire_read_age_seconds=lambda _: {"fresh": 0.0, "stale": 31.0,
                                                  "future": -1.0, "preterminal": 2.0}[proof])
    if proof == "future" and provider == "schwab":
        # Corrupt future evidence is distinct from the actual positive helper.
        service._positions_wire_read_at = lambda _: wall[0] + timedelta(seconds=1)
    monkeypatch.setattr(service.store, "record_fill_if_needed", forbidden)
    assert await retire_previous_flat_sell(service, order_id) is (proof == "fresh")
    assert [kind for kind, _ in reads] == ["terminal", "positions"]
    assert reads[1][1] > reads[0][1]
    with sessions() as unit:
        order = unit.get(BrokerOrder, order_id)
        archives = list(unit.scalars(select(BrokerOrderEvent).where(BrokerOrderEvent.order_id == order_id)))
        assert order.status == ("aborted" if proof == "fresh" else "pending")
        assert len(archives) == int(proof == "fresh")
        assert unit.scalar(select(Fill.id)) is None
        assert unit.scalar(select(VirtualPosition)).quantity == D(0)
        assert unit.get(OmsManagedPosition, UUID(row_id)).current_quantity == 0
        if archives:
            assert archives[0].payload["historical_fill_applied"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("foreign", [False, True])
async def test_normal_pending_persists_exact_managed_entry_episode(lane, foreign):
    service, sessions, row_id, account_name = lane
    with sessions.begin() as unit:
        strategy = unit.scalar(select(Strategy).where(Strategy.code == "schwab_1m_v2"))
        account = unit.scalar(select(BrokerAccount).where(BrokerAccount.name == account_name))
        buy = TradeIntentEvent(source_service="controlled-entry", payload=TradeIntentPayload(
            strategy_code=strategy.code, broker_account_name=account_name, symbol="ABC",
            side="buy", intent_type="open", quantity=D(10), reason="controlled-entry"))
        intent = service.store.create_trade_intent(unit, strategy=strategy, broker_account=account, event=buy)
        entry = service.store.get_or_create_order(unit, intent=intent, strategy_id=strategy.id,
            broker_account_id=account.id, client_order_id=f"controlled-buy-{uuid4().hex}",
            broker_order_id=f"controlled-root-{uuid4().hex}", symbol="ABC", side="buy",
            quantity=D(10), metadata={}, status="filled", order_type="limit", time_in_force="day")
        unit.flush()
        entry_id = str(entry.id)
        row = unit.get(OmsManagedPosition, UUID(row_id))
        row.entry_order_id, row.entry_client_order_id = entry.id, entry.client_order_id
    event, request = attempt(account_name)
    if foreign:
        request.metadata["entry_order_id"] = str(uuid4())
    result = await prepare_managed_sell(service, event=event, request=request,
                                        managed_row_id=row_id, now=datetime.now(UTC))
    assert (result is None) is foreign
    with sessions() as unit:
        sell = unit.scalar(select(BrokerOrder).where(BrokerOrder.client_order_id == request.client_order_id))
        if foreign:
            assert sell is None
        else:
            assert sell.payload["entry_order_id"] == entry_id
            assert request.metadata["entry_order_id"] == event.payload.metadata["entry_order_id"] == entry_id


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy", [False, True])
async def test_actual_order_sync_defers_old_prepared_sell_before_generic_fill_writer(lane, monkeypatch, legacy):
    service, sessions, row_id, account = lane
    event, request = attempt(account)
    old = datetime.now(UTC) - timedelta(days=2)
    intent_id = await prepare_managed_sell(service, event=event, request=request,
                                           managed_row_id=row_id, now=old)
    with sessions.begin() as unit:
        row = unit.scalar(select(BrokerOrder).where(BrokerOrder.intent_id == intent_id))
        row.submitted_at, row.broker_order_id = old, "original-old-seller"
        if legacy:
            row.payload = {key: value for key, value in row.payload.items()
                           if key not in {"managed_sell_prepared_at", "oms_managed_row_id"}}
        order_id = row.id
        unit.get(TradeIntent, intent_id).created_at = old
    called = []

    async def read_only_recovery(actual_service, identity):
        assert actual_service is service
        called.append(identity)
        return False  # UNKNOWN keeps the original row, no fill or new POST.

    def forbidden(*args, **kwargs):
        raise AssertionError("old managed SELL reached generic fill/submit path")

    from project_mai_tai.oms import managed_sell_retirement
    monkeypatch.setattr(managed_sell_retirement, "retire_previous_flat_sell", read_only_recovery)
    monkeypatch.setattr(service.store, "record_fill_if_needed", forbidden)
    service.broker_adapter = SimpleNamespace(fetch_order_update=forbidden, submit_order=forbidden)
    await service.sync_broker_orders(account_names=[account])
    assert called == [order_id]
    with sessions() as unit:
        assert unit.get(BrokerOrder, order_id).status == "pending"


@pytest.mark.asyncio
@pytest.mark.parametrize("own_flat", [True, False])
async def test_old_sell_retirement_preserves_unrelated_shares_with_exact_episode_net(lane, own_flat):
    service, sessions, row_id, account_name = lane
    from project_mai_tai.db.models import Fill
    old = datetime.now(UTC) - timedelta(days=2)
    with sessions.begin() as unit:
        strategy = unit.scalar(select(Strategy).where(Strategy.code == "schwab_1m_v2"))
        account = unit.scalar(select(BrokerAccount).where(BrokerAccount.name == account_name))
        buy_event = TradeIntentEvent(source_service="controlled-old-entry", payload=TradeIntentPayload(
            strategy_code=strategy.code, broker_account_name=account_name, symbol="ABC",
            side="buy", intent_type="open", quantity=D(10), reason="old-own-entry"))
        buy_intent = service.store.create_trade_intent(unit, strategy=strategy, broker_account=account, event=buy_event)
        buy_intent.status, buy_intent.created_at = "filled", old
        entry = service.store.get_or_create_order(unit, intent=buy_intent, strategy_id=strategy.id,
            broker_account_id=account.id, client_order_id="old-exact-entry",
            broker_order_id="old-exact-native-root", symbol="ABC", side="buy", quantity=D(10),
            metadata={}, status="filled", order_type="limit", time_in_force="day")
        entry.submitted_at = old
        unit.flush()
        unit.add(Fill(order_id=entry.id, strategy_id=strategy.id, broker_account_id=account.id,
            broker_fill_id="old-canonical-buy-10", symbol="ABC", side="buy", quantity=D(10),
            price=D(5), filled_at=old))
        row = unit.get(OmsManagedPosition, UUID(row_id))
        row.entry_order_id, row.entry_client_order_id = entry.id, entry.client_order_id
    event, request = attempt(account_name)
    intent_id = await prepare_managed_sell(service, event=event, request=request,
                                           managed_row_id=row_id, now=old)
    with sessions.begin() as unit:
        order = unit.scalar(select(BrokerOrder).where(BrokerOrder.intent_id == intent_id))
        order.submitted_at, order.broker_order_id = old, "old-original-exit"
        unit.get(TradeIntent, intent_id).created_at = old
        managed = unit.get(OmsManagedPosition, UUID(row_id))
        managed.status, managed.current_quantity = "closed", 0
        unit.scalar(select(VirtualPosition)).quantity = D(0)
        identity = order.id

    async def terminal(original):
        return ExecutionReport("filled" if own_flat else "cancelled", original.client_order_id,
            broker_order_id="old-original-exit", broker_fill_id="old-original-exit:cumulative",
            symbol="ABC", side="sell", intent_type="close",
            quantity=D(10), filled_quantity=D(10) if own_flat else D(9), fill_price=D("4.5"), origin="broker")

    async def positions(name):
        return [BrokerPositionSnapshot(name, "ABC", D(100), D(8))]  # Non-owned shares stay untouched.

    service.broker_adapter = SimpleNamespace(fetch_order_update=terminal, list_account_positions=positions,
                                             positions_wire_read_age_seconds=lambda _: 0.0)
    service._positions_wire_read_at = lambda _: datetime.now(UTC)
    assert await retire_previous_flat_sell(service, identity) is own_flat
    with sessions() as unit:
        assert unit.get(BrokerOrder, identity).status == ("aborted" if own_flat else "pending")
        assert unit.scalar(select(VirtualPosition)).quantity == 0
        assert len(unit.scalars(select(Fill)).all()) == 1  # Old SELL was archived, never booked as a new fill.
        if own_flat:
            from project_mai_tai.db.models import BrokerOrderEvent
            audit = unit.scalar(select(BrokerOrderEvent).where(BrokerOrderEvent.order_id == identity))
            assert audit.payload["broker_quantity_untouched"] == "100"
            proof = audit.payload["owned_flat_proof"]
            assert D(proof["bought"]) == D(proof["exited"]) == D(10)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [None, "working", "foreign-order", "client-origin",
    "no-broker-id", "same-session", "own-nonflat", "managed-nonflat", "broker-nonflat",
    "read-failed", "unknown-age", "stale-cache", "changed-row"])
async def test_previous_session_sell_retirement_is_archive_only_with_exact_flat_proof(lane, failure):
    service, sessions, row_id, account = lane
    event, request = attempt(account)
    old = datetime.now(UTC) - timedelta(days=2)
    intent_id = await prepare_managed_sell(service, event=event, request=request,
                                           managed_row_id=row_id, now=old)
    assert intent_id is not None
    with sessions.begin() as unit:
        order = unit.scalar(select(BrokerOrder).where(BrokerOrder.intent_id == intent_id))
        order.broker_order_id = None if failure == "no-broker-id" else "original-broker-sell"
        order.submitted_at = datetime.now(UTC) if failure == "same-session" else old
        unit.get(TradeIntent, intent_id).created_at = old
        unit.get(OmsManagedPosition, UUID(row_id)).current_quantity = (
            10 if failure == "managed-nonflat" else 0)
        unit.scalar(select(VirtualPosition)).quantity = D(10) if failure == "own-nonflat" else D(0)
        order_id = order.id
    reads = []

    async def read_original(original):
        reads.append(("order", original.client_order_id, original.metadata["broker_order_id"]))
        if failure == "changed-row":
            with sessions.begin() as unit:
                unit.get(BrokerOrder, order_id).payload = {"changed": True}
        return ExecutionReport(event_type="accepted" if failure == "working" else "filled",
            client_order_id="foreign" if failure == "foreign-order" else request.client_order_id,
            broker_order_id="original-broker-sell", symbol="ABC", side="sell", intent_type="close",
            quantity=D(10), filled_quantity=D(10), fill_price=D("4.5"),
            origin="client" if failure == "client-origin" else "broker")

    async def positions(name):
        reads.append(("positions", name))
        if failure == "read-failed":
            raise RuntimeError("controlled unavailable")
        return ([BrokerPositionSnapshot(name, "ABC", D(1), D(5))]
                if failure == "broker-nonflat" else [])

    service.broker_adapter = SimpleNamespace(fetch_order_update=read_original,
        list_account_positions=positions,
        positions_wire_read_age_seconds=lambda name: (
            None if failure == "unknown-age" else 3600 if failure == "stale-cache" else 0.0))
    service._positions_wire_read_at = lambda name: (
        datetime.now(UTC) - timedelta(hours=1) if failure in {"unknown-age", "stale-cache"}
        else datetime.now(UTC))
    result = await retire_previous_flat_sell(service, order_id)
    assert result is (failure is None)
    from project_mai_tai.db.models import BrokerOrderEvent, Fill
    with sessions() as unit:
        order = unit.get(BrokerOrder, order_id)
        archives = unit.scalars(select(BrokerOrderEvent).where(
            BrokerOrderEvent.order_id == order_id)).all()
        assert unit.scalar(select(Fill.id)) is None  # Historical fill is never booked today.
        assert unit.scalar(select(VirtualPosition)).quantity == (D(10) if failure == "own-nonflat" else 0)
        if failure is None:
            assert order.status == "aborted" and unit.get(TradeIntent, intent_id).status == "aborted"
            assert len(archives) == 1
            archive = archives[0].payload
            assert archive["original"]["order"]["status"] == "pending"
            assert archive["original"]["order"]["broker_order_id"] == "original-broker-sell"
            assert archive["terminal_report"]["filled_quantity"] == "10"
            assert archive["archive_only"] is True and archive["historical_fill_applied"] is False
            assert len(archive["original_sha256"]) == 64
        else:
            assert order.status == "pending" and not archives
    if failure in {"no-broker-id", "same-session"}:
        assert not reads
    elif failure in {"working", "foreign-order", "client-origin"}:
        assert len(reads) == 1
    else:
        assert [row[0] for row in reads] == ["order", "positions"]


@pytest.mark.asyncio
@pytest.mark.parametrize("session", ["AM", "NORMAL", "PM"])
async def test_durable_pending_visible_before_any_broker_await_and_duplicate_refused(lane, session):
    service, sessions, row_id, account = lane
    event, request = attempt(account, session=session)
    now = datetime.now(UTC)
    intent_id = await prepare_managed_sell(service, event=event, request=request,
                                          managed_row_id=row_id, now=now)
    assert intent_id is not None
    with sessions() as unit:
        order = unit.scalar(select(BrokerOrder).where(BrokerOrder.client_order_id == request.client_order_id))
        intent = unit.get(TradeIntent, intent_id)
        assert order.intent_id == intent.id and order.status == intent.status == "pending"
        assert order.submitted_at is None and order.broker_order_id is None
        assert order.quantity == intent.quantity == D(10)
        assert order.payload["oms_managed_row_id"] == row_id
        assert order.payload["session"] == session
        assert unit.scalar(select(RiskCheck.id).where(RiskCheck.intent_id == intent_id)) is not None
    second_event, second = attempt(account, session=session)
    assert await prepare_managed_sell(service, event=second_event, request=second,
                                      managed_row_id=row_id, now=now) is None
    assert await prepare_managed_sell(service, event=event, request=request,
                                      managed_row_id=row_id, now=now) is None
    with sessions() as unit:
        assert len(unit.scalars(select(BrokerOrder)).all()) == 1
        assert len(unit.scalars(select(TradeIntent)).all()) == 1


@pytest.mark.asyncio
async def test_reconstructed_service_sees_pending_after_controlled_crash(lane):
    service, sessions, row_id, account = lane
    event, request = attempt(account)
    now = datetime.now(UTC)
    assert await prepare_managed_sell(service, event=event, request=request,
                                      managed_row_id=row_id, now=now) is not None
    restarted = OmsRiskService(service.settings, redis_client=None, session_factory=sessions,
                               broker_adapter=SimpleNamespace())
    with sessions() as unit:
        snapshot = restarted._read_v2_managed_snapshot(unit, account, "ABC", True)
        assert snapshot.dedup_active and snapshot.managed_row_id == row_id
    next_event, next_request = attempt(account)
    assert await prepare_managed_sell(restarted, event=next_event, request=next_request,
                                      managed_row_id=row_id, now=now) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["missing-vp", "own-short", "closed", "foreign-row", "foreign-symbol"])
async def test_changed_ownership_never_creates_a_sell_reservation(lane, change):
    service, sessions, row_id, account = lane
    with sessions.begin() as unit:
        row = unit.get(OmsManagedPosition, UUID(row_id))
        own = unit.scalar(select(VirtualPosition))
        if change == "missing-vp":
            unit.delete(own)
        elif change == "own-short":
            own.quantity = D(9)
        elif change == "closed":
            row.status = "closed"
        elif change == "foreign-row":
            row.strategy_code = "orb_schwab"
        else:
            row.symbol = "OTHER"
    event, request = attempt(account)
    result = await prepare_managed_sell(service, event=event, request=request,
                                       managed_row_id=row_id, now=datetime.now(UTC))
    # TOP13:0x preserves managed-only ownership when VP is absent, not when
    # a present ledger contradicts the requested owned quantity.
    assert (result is not None) is (change == "missing-vp")
    with sessions() as unit:
        orders = unit.scalars(select(BrokerOrder)).all()
        intents = unit.scalars(select(TradeIntent)).all()
        if change == "missing-vp":
            assert len(orders) == len(intents) == 1
            assert orders[0].status == intents[0].status == "pending"
            assert orders[0].submitted_at is None and orders[0].broker_order_id is None
            assert unit.scalar(select(VirtualPosition)) is None
        else:
            assert not orders
            assert not intents


@pytest.mark.asyncio
async def test_prepare_commit_failure_cannot_return_wire_permission(lane, monkeypatch):
    service, sessions, row_id, account = lane
    event, request = attempt(account)
    original = sessions.class_.commit

    def fail_commit(unit):
        raise RuntimeError("controlled commit refusal")

    monkeypatch.setattr(sessions.class_, "commit", fail_commit)
    with pytest.raises(RuntimeError, match="controlled commit refusal"):
        await prepare_managed_sell(service, event=event, request=request,
                                   managed_row_id=row_id, now=datetime.now(UTC))
    monkeypatch.setattr(sessions.class_, "commit", original)
    with sessions() as unit:
        assert not unit.scalars(select(BrokerOrder)).all()
        assert not unit.scalars(select(TradeIntent)).all()


@pytest.mark.asyncio
async def test_pending_preparation_runs_db_and_commit_on_worker(lane, monkeypatch):
    service, sessions, row_id, account = lane
    event, request = attempt(account)
    main_thread = threading.get_ident()
    threads = []
    original = service.store.get_or_create_order

    def observed(*args, **kwargs):
        threads.append(threading.get_ident())
        return original(*args, **kwargs)

    monkeypatch.setattr(service.store, "get_or_create_order", observed)
    assert await prepare_managed_sell(service, event=event, request=request,
                                      managed_row_id=row_id, now=datetime.now(UTC)) is not None
    assert threads and all(thread != main_thread for thread in threads)


@pytest.mark.asyncio
async def test_generic_admitted_intent_is_not_recreated_and_has_pending_before_wire(lane):
    service, sessions, row_id, account = lane
    event, request = attempt(account)
    with sessions.begin() as unit:
        strategy = unit.scalar(select(VirtualPosition.strategy_id))
        broker_account = unit.scalar(select(VirtualPosition.broker_account_id))
        admitted = TradeIntent(strategy_id=strategy, broker_account_id=broker_account,
            symbol="ABC", side="sell", intent_type="close", quantity=D(10), status="pending",
            reason=event.payload.reason, payload={"metadata": dict(request.metadata)})
        unit.add(admitted)
        unit.flush()
        admitted_id = admitted.id
    assert await prepare_managed_sell(service, event=event, request=request,
        managed_row_id=row_id, now=datetime.now(UTC), admitted_intent_id=admitted_id) == admitted_id
    with sessions() as unit:
        assert len(unit.scalars(select(TradeIntent)).all()) == 1
        prepared = unit.scalar(select(BrokerOrder))
        assert prepared.intent_id == admitted_id and prepared.submitted_at is None


@pytest.mark.asyncio
async def test_creating_call_can_abandon_session_changed_before_its_first_wire(lane):
    service, sessions, row_id, account = lane
    event, request = attempt(account)
    now = datetime.now(UTC)
    intent_id = await prepare_managed_sell(service, event=event, request=request,
                                          managed_row_id=row_id, now=now)
    abort_id = await abandon_unposted_sell(service, request=request, intent_id=intent_id, now=now)
    with sessions() as unit:
        from project_mai_tai.db.models import BrokerOrderEvent
        audit = unit.get(BrokerOrderEvent, abort_id)
        assert audit is not None and audit.event_type == "aborted" and audit.event_source == "client"
        assert audit.payload["proven_not_sent"] is True
        assert audit.payload["client_order_id"] == request.client_order_id
        assert audit.payload["metadata"] == request.metadata
        assert unit.scalar(select(BrokerOrder)).status == "aborted"
        assert unit.get(TradeIntent, intent_id).status == "aborted"
        assert unit.get(OmsManagedPosition, UUID(row_id)).current_quantity == 10


@pytest.mark.asyncio
@pytest.mark.parametrize("changed", ["broker-id", "submitted", "working", "provenance"])
async def test_unposted_abort_never_overwrites_a_changed_pending_row(lane, changed):
    service, sessions, row_id, account = lane
    event, request = attempt(account)
    now = datetime.now(UTC)
    intent_id = await prepare_managed_sell(service, event=event, request=request,
                                          managed_row_id=row_id, now=now)
    with sessions.begin() as unit:
        order = unit.scalar(select(BrokerOrder))
        if changed == "broker-id":
            order.broker_order_id = "known-original-wire"
        elif changed == "submitted":
            order.submitted_at = now
        elif changed == "working":
            order.status = "accepted"
        else:
            order.payload = {**order.payload, "managed_sell_prepared_at": "different-owner"}
    with pytest.raises(ValueError, match="managed_sell_not_proven_unposted"):
        await abandon_unposted_sell(service, request=request, intent_id=intent_id, now=now)


@pytest.mark.asyncio
@pytest.mark.parametrize("strategy", ["schwab_1m_v2", "orb_schwab"])
async def test_actual_registered_native_hard_stop_sees_committed_pending_at_sender(monkeypatch, strategy):
    from tests.unit.test_reserve1 import armed_harness, sell_orders
    service, sessions, adapter, symbol, _, stop = armed_harness(monkeypatch, strategy=strategy)
    original = adapter.submit_order
    observed = []

    async def sender(request):
        with sessions() as unit:
            row = unit.scalar(select(BrokerOrder).where(BrokerOrder.client_order_id == request.client_order_id))
            assert row is not None and row.status == "pending" and row.submitted_at is None
            assert row.intent_id is not None and row.payload["managed_sell_prepared_at"]
            assert unit.get(TradeIntent, row.intent_id).side == "sell"
        observed.append(request.client_order_id)
        return await original(request)

    monkeypatch.setattr(adapter, "submit_order", sender)
    await service._trigger_hard_stop(stop, trigger_price=D("6.99"), trigger_source="bid")
    assert len(observed) == len(adapter.requests) == 1
    assert [row.status for row in sell_orders(sessions)] == ["filled"]


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["pending", "accepted", "partially_filled", "unknown", "cancel_pending"])
async def test_nonterminal_predecessor_never_authorizes_retry(lane, status):
    service, sessions, row_id, account = lane
    event, request = attempt(account)
    now = datetime.now(UTC)
    intent_id = await prepare_managed_sell(service, event=event, request=request,
                                          managed_row_id=row_id, now=now)
    with sessions.begin() as unit:
        original = unit.scalar(select(BrokerOrder))
        original.status = status
    retry_event, retry = attempt(account, retry_of=request.client_order_id)
    assert await prepare_managed_sell(service, event=retry_event, request=retry,
                                      managed_row_id=row_id, now=now, retry_intent_id=intent_id) is None
    with sessions() as unit:
        assert len(unit.scalars(select(BrokerOrder)).all()) == 1


@pytest.mark.asyncio
async def test_proven_rejected_retry_has_its_own_durable_coid_before_wire(lane):
    service, sessions, row_id, account = lane
    event, request = attempt(account)
    now = datetime.now(UTC)
    intent_id = await prepare_managed_sell(service, event=event, request=request,
                                          managed_row_id=row_id, now=now)
    with sessions.begin() as unit:
        unit.scalar(select(BrokerOrder)).status = "rejected"
    retry_event, retry = attempt(account, retry_of=request.client_order_id)
    assert await prepare_managed_sell(service, event=retry_event, request=retry,
                                      managed_row_id=row_id, now=now, retry_intent_id=intent_id) == intent_id
    with sessions() as unit:
        rows = unit.scalars(select(BrokerOrder)).all()
        assert len(rows) == 2 and {r.status for r in rows} == {"pending", "rejected"}
        prepared = next(r for r in rows if r.status == "pending")
        assert prepared.submitted_at is None and prepared.client_order_id == retry.client_order_id
        assert prepared.payload["reserve1_retry_of"] == request.client_order_id
        assert len(unit.scalars(select(TradeIntent)).all()) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["corrected", "refresh"])
async def test_corrected_and_refresh_roles_prepare_only_bound_terminal_predecessor(lane, kind):
    service, sessions, row_id, account = lane
    event, request = attempt(account)
    now = datetime.now(UTC)
    intent_id = await prepare_managed_sell(service, event=event, request=request,
                                          managed_row_id=row_id, now=now)
    with sessions.begin() as unit:
        unit.scalar(select(BrokerOrder)).status = "cancelled" if kind == "refresh" else "rejected"
    retry_event, retry = attempt(account, quantity=D(5) if kind == "refresh" else D(10))
    if kind == "refresh":
        retry.metadata["managed_sell_refresh_of"] = request.client_order_id
    else:
        retry.metadata.update(confirmation_corrected_retry="true",
                              confirmation_corrected_from_client_order_id=request.client_order_id)
    assert await prepare_managed_sell(service, event=retry_event, request=retry,
                                      managed_row_id=row_id, now=now, retry_intent_id=intent_id) == intent_id
    with sessions() as unit:
        pending = unit.scalar(select(BrokerOrder).where(BrokerOrder.client_order_id == retry.client_order_id))
        assert pending.status == "pending" and pending.submitted_at is None
        assert pending.quantity == retry.quantity and pending.payload["oms_managed_row_id"] == row_id


@pytest.mark.asyncio
async def test_replacement_never_borrows_predecessor_from_different_managed_episode(lane):
    service, sessions, row_id, account = lane
    event, request = attempt(account)
    now = datetime.now(UTC)
    intent_id = await prepare_managed_sell(service, event=event, request=request,
                                          managed_row_id=row_id, now=now)
    with sessions.begin() as unit:
        old = unit.scalar(select(BrokerOrder))
        old.status = "cancelled"
        old.payload = {**old.payload, "oms_managed_row_id": str(uuid4())}
    retry_event, retry = attempt(account)
    retry.metadata["managed_sell_refresh_of"] = request.client_order_id
    assert await prepare_managed_sell(service, event=retry_event, request=retry,
                                      managed_row_id=row_id, now=now, retry_intent_id=intent_id) is None
    with sessions() as unit:
        assert len(unit.scalars(select(BrokerOrder)).all()) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("episode", ["same", "missing-tag-new-lot", "different-tag-new-lot"])
async def test_actual_refresh_cannot_move_old_exit_decision_to_new_lot(lane, monkeypatch, episode):
    service, sessions, row_id, account = lane
    from project_mai_tai.oms import service as service_module
    now = datetime(2026, 10, 16, 14, 0, tzinfo=UTC)
    monkeypatch.setattr(service_module, "utcnow", lambda: now)
    event, request = attempt(account)
    intent_id = await prepare_managed_sell(service, event=event, request=request,
                                           managed_row_id=row_id, now=now)
    with sessions.begin() as unit:
        predecessor = unit.scalar(select(BrokerOrder).where(BrokerOrder.intent_id == intent_id))
        predecessor.status, predecessor.broker_order_id = "accepted", "original-seller"
        predecessor.submitted_at = now
        if episode != "same":
            old_row = unit.get(OmsManagedPosition, UUID(row_id))
            old_row.status, old_row.current_quantity = "closed", 0
            replacement_lot = service.store.create_managed_position(unit,
                strategy_code="schwab_1m_v2", broker_account_name=account, symbol="ABC",
                entry_price=D(6), quantity=10)
            unit.flush()
            assert str(replacement_lot.id) != row_id
            if episode == "missing-tag-new-lot":
                predecessor.payload = {key: value for key, value in predecessor.payload.items()
                                       if key != "oms_managed_row_id"}
        predecessor_id = predecessor.id
    sent = []

    async def pricing(**kwargs):
        return {**kwargs["order"].payload, "session": "NORMAL", "order_type": "market"}

    async def broker(wire):
        sent.append(wire)
        return [ExecutionReport(event_type="cancelled" if wire.intent_type == "cancel" else "accepted",
            client_order_id=wire.client_order_id,
            broker_order_id="original-seller" if wire.intent_type == "cancel" else "replacement-seller",
            symbol="ABC", side="sell", intent_type=wire.intent_type,
            quantity=wire.quantity, origin="broker", metadata=dict(wire.metadata), reported_at=now)]

    monkeypatch.setattr(service, "_build_refreshed_order_metadata", pricing)
    service.broker_adapter = SimpleNamespace(submit_order=broker)
    with sessions() as unit:
        await service._refresh_working_order(session=unit, order=unit.get(BrokerOrder, predecessor_id),
            intent=unit.get(TradeIntent, intent_id), strategy_code="schwab_1m_v2",
            broker_account_name=account, report=ExecutionReport("accepted", request.client_order_id,
                broker_order_id="original-seller", symbol="ABC", side="sell", intent_type="close",
                quantity=D(10), origin="broker", reported_at=now))
        unit.commit()
    assert [wire.intent_type for wire in sent] == (["cancel", "close"] if episode == "same" else ["cancel"])
    with sessions() as unit:
        orders = unit.scalars(select(BrokerOrder).where(BrokerOrder.intent_id == intent_id)).all()
        assert len(orders) == (2 if episode == "same" else 1)
        if episode != "same":
            new_lot = service.store.get_open_managed_position(unit, broker_account_name=account, symbol="ABC")
            assert new_lot.current_quantity == 10 and str(new_lot.id) != row_id
            assert unit.scalar(select(VirtualPosition)).quantity == D(10)


@pytest.mark.asyncio
@pytest.mark.parametrize("first_is_flatten", [False, True])
async def test_real_flatten_and_normal_owner_share_committed_pending_during_wire(
    lane, monkeypatch, first_is_flatten,
):
    service, sessions, row_id, account = lane
    service.settings.oms_v2_exit_management_enabled = True
    service.settings.oms_v2_exit_close_on_fill_enabled = True
    service.settings.oms_v2_overnight_flatten_enabled = True
    service._managed_v2_symbols = {(account, "ABC")}
    service._latest_quotes_by_symbol["ABC"] = {"bid": 4.5, "ask": 4.51}
    # This is an overlap/crash control, not a session-clock or broker-fill proof.
    monkeypatch.setattr(service, "_v2_overnight_flatten_due", lambda: True)
    entered, release = asyncio.Event(), asyncio.Event()
    sent = []

    async def paused_broker(request):
        with sessions() as unit:
            prepared = unit.scalar(select(BrokerOrder).where(
                BrokerOrder.client_order_id == request.client_order_id))
            assert prepared is not None and prepared.status == "pending"
            assert prepared.submitted_at is None
            assert prepared.payload["oms_managed_row_id"] == row_id
            assert unit.get(TradeIntent, prepared.intent_id).status == "pending"
        sent.append(request)
        entered.set()
        await release.wait()
        raise TimeoutError("controlled unknown broker outcome")

    service.broker_adapter = SimpleNamespace(submit_order=paused_broker)
    with sessions() as unit:
        snapshot = service._read_v2_managed_snapshot(unit, account, "ABC", True)

    async def normal():
        return await service._emit_v2_exit_on_loop_claimed(
            account, "ABC", service._hydrate_v2_position(snapshot), snapshot.entry_price,
            kind="HARD", reference_price=4.5, reason="oms_v2_managed_exit:HARD_STOP",
            bid=4.5, close_on_fill=True, expected_managed_row_id=row_id,
        )

    first = asyncio.create_task(service._v2_overnight_flatten() if first_is_flatten else normal())
    try:
        await asyncio.wait_for(entered.wait(), 2)
        if first_is_flatten:
            assert await asyncio.wait_for(normal(), 2) == "refused"
        else:
            await asyncio.wait_for(service._v2_overnight_flatten(), 2)
        assert len(sent) == 1 and not first.done()
        # A fresh process has no in-memory claim but must see the durable seller.
        restarted = OmsRiskService(service.settings, redis_client=None, session_factory=sessions,
                                   broker_adapter=SimpleNamespace())
        with sessions() as unit:
            assert restarted._read_v2_managed_snapshot(unit, account, "ABC", True).dedup_active
    finally:
        first.cancel()
        await asyncio.gather(first, return_exceptions=True)
    with sessions() as unit:
        rows = unit.scalars(select(BrokerOrder).where(BrokerOrder.side == "sell")).all()
        assert len(rows) == 1 and rows[0].status == "pending"
        assert unit.get(OmsManagedPosition, UUID(row_id)).current_quantity == 10
