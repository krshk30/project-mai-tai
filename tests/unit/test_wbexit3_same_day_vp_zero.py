"""SOURCE UNRUN: same-day log-reconstructed VP false-zero path, not native PASS.

AIOK retained operands: requested27/managed27/ledger0E-8/CW_TARGET/FLAT_INFERRED
(n=0), C12047 + C12356 same-day BUY ruling. Actual native history is NOT retained;
BUY clock/IDs, raw today rows, all no-SELL history and page receipts are CONTROLLED.
Main must compose/review/fence this exact source before any execution.
"""
from datetime import datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select

from project_mai_tai.db.models import AccountPosition, BrokerOrder, Fill, OmsManagedPosition, TradeIntent, VirtualPosition
from tests.unit.test_wbattach1_riskoff import make_case as make_case, closes
from tests.unit.test_wbexit2_route import close_case, owner_case, saved
from tests.unit.test_wbexit3_obligation import enable, obligations
from tests.unit.wbexit3_day_history_port import SameDayHistoryBroker, bind_real_history_reader


async def same_day_case(make_case, monkeypatch, *, on=True, stamp="2026-10-23T19:04:00+00:00", port_type=SameDayHistoryBroker):
    decision_at = datetime.fromisoformat(stamp)
    # Historical-Friday CONTROLLED setup: BUY18:40 -> protection18:41 -> retained release18:48 -> decision19:04.
    # Exact original BUY/protection/native terminal event clocks remain UNKNOWN.
    case = await owner_case(make_case, port_type=port_type, symbol="AIOK", quantity="27",
        entry="10.91", at_clock=decision_at - timedelta(minutes=23))
    service, factory, broker, entry_id, clock = case
    clock[0] = decision_at
    broker.bid = float(broker.entry_price * Decimal("1.20"))  # CONTROLLED fresh executable CW_TARGET quote; no price guard bypass.
    broker.controlled_buy_at = decision_at - timedelta(minutes=24)
    with factory.begin() as db:
        entry = db.get(BrokerOrder, entry_id)
        entry.created_at = entry.submitted_at = broker.controlled_buy_at
        db.get(TradeIntent, entry.intent_id).created_at = broker.controlled_buy_at
        for fill in db.scalars(select(Fill)):
            fill.filled_at = broker.controlled_buy_at
        db.scalar(select(OmsManagedPosition)).entry_time = broker.controlled_buy_at
        db.scalar(select(VirtualPosition)).quantity = Decimal(0)
    await enable(case, on)
    bind_real_history_reader(case, monkeypatch)
    return case


def no_execution(case):
    _, factory, broker, _, _ = case
    assert not broker.cancelled and not closes(broker)
    state = saved(case)
    assert Decimal(state["original_quantity"]) == Decimal(state["remaining"]) == 27
    assert state["sell"] is None and state["invocation"] is None
    with factory() as db:
        row = db.scalar(select(OmsManagedPosition))
        assert row.status == "open" and row.current_quantity == 27
        assert db.scalar(select(VirtualPosition.quantity)) == 0
        fills = list(db.scalars(select(Fill)))
        assert len(fills) == 1 and fills[0].side == "buy" and fills[0].quantity == 27


@pytest.mark.asyncio
async def test_AIOK_same_day_CONTROLLED_complete_no_SELL_history_real_cap_cancel_prepare_wire(make_case, monkeypatch):
    case = await same_day_case(make_case, monkeypatch)
    case[2].originals_already_terminal = True  # Retained RELEASED2/already_released sequence; typed IDs/status are CONTROLLED.
    await close_case(case, "CW_TARGET")
    broker = case[2]
    assert broker.history_calls == 1 and len(broker.history_page_calls) == 1
    assert broker.positions_reads == 2  # Early diagnostic + separately required POST-terminal receipt.
    assert not broker.cancelled  # Already-released originals do not authorize an extra cancellation.
    assert [r.quantity for r in closes(broker)] == [Decimal(27)]
    assert broker.total == 1000  # All manual inventory remains held.
    assert [row.quantity for row in await broker.list_account_positions(broker.account)] == [Decimal(1000)]
    with case[1]() as db:
        assert db.scalar(select(AccountPosition.quantity)) == 1000
        row = db.scalar(select(OmsManagedPosition))
        assert row.status == "closed" and row.current_quantity == 0
        assert db.scalar(select(VirtualPosition.quantity)) == 0  # No synthetic restoration.
        fills = list(db.scalars(select(Fill)))
        assert sorted((f.side, f.quantity) for f in fills) == [("buy", Decimal(27)), ("sell", Decimal(27))]
    state = saved(case)
    assert state["phase"] == "RESOLVED" and Decimal(state["original_quantity"]) == 27
    assert obligations(case)[0]["phase"] == "RESOLVED"


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["manual_filled", "manual_partial", "manual_second_page",
    "working_sell", "unreadable", "partial_pages", "missing_client", "alias_conflict",
    "original_partial", "original_filled", "original_native_changed", "missing_fill_time",
    "working_original_geometry_changed", "envelope_status_unknown", "envelope_status_null",
    "envelope_status_conflict", "native_alias_null", "native_alias_empty", "naive_fill_time"])
async def test_any_origin_execution_working_or_unproven_full_history_keeps_job_open(make_case, monkeypatch, mode):
    case = await same_day_case(make_case, monkeypatch)
    broker, clock = case[2], case[4]
    broker.history_mode = mode
    for attempt in range(3):
        if attempt:
            clock[0] += timedelta(seconds=61)
        await close_case(case, "CW_TARGET")
        no_execution(case)
        assert broker.positions_reads == attempt + 1
    assert obligations(case)[0]["phase"] == "PAGED"
    assert broker.history_calls == 3
    if mode == "manual_second_page":
        assert len(broker.history_page_calls) == 6  # Full account pages, including the adverse tail.


@pytest.mark.asyncio
async def test_native_10_below_bot27_never_becomes_partial_10_sale(make_case, monkeypatch):
    case = await same_day_case(make_case, monkeypatch)
    case[2].native_quantity = Decimal(10)
    await close_case(case, "CW_TARGET")
    assert case[2].positions_reads == 1 and case[2].history_calls == 0
    no_execution(case)


@pytest.mark.asyncio
async def test_prior_day_BUY_has_no_inclusive_history_NOT_COVERED(make_case, monkeypatch):
    case = await same_day_case(make_case, monkeypatch, stamp="2026-10-22T19:04:00+00:00")
    broker, clock = case[2], case[4]
    broker.history_mode = "unreadable"
    await close_case(case, "CW_TARGET")  # Real duty admitted on the original same-day episode.
    assert broker.history_calls == 1 and obligations(case)[0]["phase"] == "RETRY"
    broker.history_mode = "clean"
    clock[0] += timedelta(days=1)  # Next valid Friday RTH session, carried original duty.
    for _ in range(2):
        await close_case(case, "CW_TARGET")
        clock[0] += timedelta(seconds=61)
        no_execution(case)
    assert broker.history_calls == 1  # Today's list can never cover yesterday's BUY.
    assert obligations(case)[0]["phase"] == "PAGED"


@pytest.mark.asyncio
async def test_new_history_capability_OFF_is_not_called(make_case, monkeypatch):
    case = await same_day_case(make_case, monkeypatch, on=False)
    await close_case(case, "CW_TARGET")
    assert case[2].positions_reads == 0 and case[2].history_calls == 0
    assert obligations(case) == []
    no_execution(case)


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["managed_quantity", "VP_nonzero", "entry_native_identity", "exit_Fill", "expired_native_history"])
async def test_durable_change_during_native_await_revokes_proof_before_cancel(make_case, monkeypatch, change):
    case = await same_day_case(make_case, monkeypatch)
    def mutate():
        with case[1].begin() as db:
            if change == "managed_quantity":
                db.scalar(select(OmsManagedPosition)).current_quantity = 26
            elif change == "VP_nonzero":
                db.scalar(select(VirtualPosition)).quantity = Decimal(1)
            elif change == "entry_native_identity":
                db.get(BrokerOrder, case[3]).broker_order_id = "CONTROLLED-changed-entry-native"
            elif change == "exit_Fill":
                order = db.scalar(select(BrokerOrder).where(BrokerOrder.side == "sell"))
                assert order is not None  # Existing real protection-order writer supplies its identity.
                db.add(Fill(order_id=order.id, strategy_id=order.strategy_id,
                    broker_account_id=order.broker_account_id, symbol=order.symbol, side="sell",
                    broker_fill_id="CONTROLLED-new-exit-Fill", quantity=Decimal(1), price=Decimal("10.91"),
                    filled_at=case[4][0]))
            else:
                case[4][0] += timedelta(seconds=3)
    case[2].on_history = mutate
    if change == "entry_native_identity":
        with pytest.raises(ValueError, match="wbexit3_episode_changed_no_resolution"):
            await close_case(case, "CW_TARGET")
    else:
        await close_case(case, "CW_TARGET")
    assert case[2].history_calls == 1 and not case[2].cancelled and not closes(case[2])
    duty = obligations(case)[0]
    if change == "entry_native_identity":
        assert duty["phase"] == "OPEN" and duty["active_token"] is not None  # Changed identity is retained UNKNOWN, never borrowed/released.
    else:
        assert duty["phase"] == "RETRY" and duty["active_token"] is None
    assert Decimal(saved(case)["original_quantity"]) == Decimal(saved(case)["remaining"]) == 27


@pytest.mark.asyncio
async def test_proof_is_not_reused_by_next_invocation_after_POST_terminal_read_UNKNOWN(make_case, monkeypatch):
    case = await same_day_case(make_case, monkeypatch)
    broker, clock = case[2], case[4]
    broker.post_terminal_positions_unknown = True
    await close_case(case, "CW_TARGET")
    assert broker.history_calls == 1 and len(broker.cancelled) == 1 and not closes(broker)
    assert obligations(case)[0]["active_token"] is None
    clock[0] += timedelta(seconds=11)
    broker.post_terminal_positions_unknown = broker.positions_unknown = False
    broker.history_mode = "unreadable"
    await close_case(case, "CW_TARGET")
    assert broker.history_calls == 2 and not closes(broker)  # Prior attempt's proof never lends ownership.
    assert obligations(case)[0]["active_token"] is None
    assert Decimal(saved(case)["original_quantity"]) == Decimal(saved(case)["remaining"]) == 27
    with case[1]() as db:
        assert db.scalar(select(VirtualPosition.quantity)) == 0
        assert db.scalar(select(OmsManagedPosition)).status == "open"


@pytest.mark.asyncio
async def test_real_control_and_routing_forward_same_account_history_through_full_exit(make_case, monkeypatch):
    from types import SimpleNamespace
    from project_mai_tai.broker_adapters.routing import RoutingBrokerAdapter
    from project_mai_tai.oms.bot_control_runtime import ControlledBrokerAdapter
    case = await same_day_case(make_case, monkeypatch)
    service, _, broker, _, _ = case
    factories = []
    def webull():
        factories.append("webull")
        return broker
    def wrong_provider():
        pytest.fail("account history routed to the wrong provider")
    async def entry_authorize(*args):
        pytest.fail("a history read/SELL cannot invoke entry authorization")
    router = RoutingBrokerAdapter(default_provider="schwab", provider_by_account={broker.account: "webull"},
        factories_by_provider={"webull": webull, "schwab": wrong_provider})
    service.broker_adapter = ControlledBrokerAdapter(router, SimpleNamespace(authorize=entry_authorize))
    await close_case(case, "CW_TARGET")
    assert factories == ["webull"] and broker.history_calls == 1 and len(broker.cancelled) == 1
    assert [r.quantity for r in closes(broker)] == [Decimal(27)] and broker.total == 1000
    assert [row.quantity for row in await broker.list_account_positions(broker.account)] == [Decimal(1000)]
    with case[1]() as db:
        assert db.scalar(select(AccountPosition.quantity)) == 1000
    assert obligations(case)[0]["phase"] == "RESOLVED"
    assert broker.history_adapter._query_budget.foreground_reads == 0
    assert not broker.history_adapter._foreground_query_tasks


@pytest.mark.asyncio
async def test_routing_missing_named_history_capability_has_no_ordinary_fallback():
    from types import SimpleNamespace
    from project_mai_tai.broker_adapters.routing import RoutingBrokerAdapter
    from project_mai_tai.oms.bot_control_runtime import ControlledBrokerAdapter
    async def forbidden(*args, **kwargs):
        pytest.fail("ordinary positions/order GET cannot prove inclusive native history")
    provider = SimpleNamespace(list_account_positions=forbidden, fetch_order_update=forbidden)
    router = RoutingBrokerAdapter(default_provider="webull", provider_by_account={"live:schwab_1m_v2": "webull"},
        factories_by_provider={"webull": lambda: provider})
    adapter = ControlledBrokerAdapter(router, SimpleNamespace())
    with pytest.raises(RuntimeError, match="wbexit3_same_day_history_UNAVAILABLE"):
        await adapter.read_wbexit3_day_history("live:schwab_1m_v2", after=datetime.fromisoformat("2026-10-23T19:04:00+00:00"))


@pytest.mark.asyncio
async def test_native_history_cancellation_joins_real_foreground_thread_budget_and_owner(make_case, monkeypatch):
    import asyncio
    from threading import Event
    from tests.unit.test_wbexit3_early_probe import unchanged_quantity_and_no_wire
    case = await same_day_case(make_case, monkeypatch)
    service, _, broker, _, _ = case
    from types import SimpleNamespace
    from project_mai_tai.broker_adapters.routing import RoutingBrokerAdapter
    from project_mai_tai.oms.bot_control_runtime import ControlledBrokerAdapter
    router = RoutingBrokerAdapter(default_provider="webull", provider_by_account={broker.account: "webull"},
        factories_by_provider={"webull": lambda: broker})
    service.broker_adapter = ControlledBrokerAdapter(router, SimpleNamespace())
    adapter = broker.history_adapter
    entered, release, physically_done = Event(), Event(), Event()
    actual_page = adapter._today_page
    actual_blocking = adapter._wbexit3_day_history_blocking
    def held_page(*args):
        entered.set()
        assert release.wait(5), "CONTROLLED history-page latency was not released"
        return actual_page(*args)
    def observed_blocking(*args):
        try:
            return actual_blocking(*args)
        finally:
            physically_done.set()
    monkeypatch.setattr(adapter, "_today_page", held_page)
    monkeypatch.setattr(adapter, "_wbexit3_day_history_blocking", observed_blocking)
    task = asyncio.create_task(close_case(case, "CW_TARGET"))
    key = (broker.account, broker.symbol)
    try:
        assert await asyncio.wait_for(asyncio.to_thread(entered.wait, 2), timeout=3)
        assert router._adapters_by_provider["webull"] is broker
        for message in ("first-history-cancel", "second-history-cancel"):
            task.cancel(message)
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            assert not task.done() and not physically_done.is_set()
            assert key in service._wbattach_exit_owners
            assert service._wbattach_exit_owners[key].wb2_pending is not None
            assert obligations(case)[0]["active_token"] is not None
            assert adapter._query_budget.foreground_reads == 1 and adapter._foreground_query_tasks
            assert not broker.cancelled and not closes(broker)
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=3)
        assert physically_done.is_set() and key not in service._wbattach_exit_owners
        assert adapter._query_budget.foreground_reads == 0 and not adapter._foreground_query_tasks
        assert obligations(case)[0]["active_token"] is None
        unchanged_quantity_and_no_wire(case)
    finally:
        release.set()
        if not task.done():
            task.cancel("fixture-cleanup")
        await asyncio.gather(task, return_exceptions=True)
        assert await asyncio.wait_for(asyncio.to_thread(physically_done.wait, 3), timeout=4)


@pytest.mark.asyncio
@pytest.mark.parametrize("missing", ["settings", "flag"])
async def test_public_prepare_retains_minimal_legacy_service_without_WB3_settings(make_case, missing):
    from types import SimpleNamespace
    from project_mai_tai.oms.managed_sell_prewire import prepare_managed_sell
    from tests.unit.test_managed_sell_prewire import attempt
    service, factory, broker, _, clock = make_case()
    # These are the actual existing helper dependencies, backed by real store/DB
    # methods; no ownership/cap/preparation helper is replaced with success.
    legacy = SimpleNamespace(store=service.store, logger=service.logger, _run_db=service._run_db,
        _cw_target_binding_matches=service._cw_target_binding_matches,
        _stored_owned_oco_balance=service._stored_owned_oco_balance,
        _record_internal_risk_pass=service._record_internal_risk_pass)
    if missing == "flag":
        legacy.settings = SimpleNamespace()
    with factory() as db:
        row_id = str(db.scalar(select(OmsManagedPosition)).id)
    event, request = attempt(broker.account)
    intent_id = await prepare_managed_sell(legacy, event=event, request=request,
        managed_row_id=row_id, now=clock[0])
    assert intent_id is not None and not broker.requests
    with factory() as db:
        prepared = db.scalar(select(BrokerOrder).where(BrokerOrder.client_order_id == request.client_order_id))
        assert prepared.intent_id == intent_id and prepared.status == "pending" and prepared.submitted_at is None
        assert prepared.broker_order_id is None and prepared.quantity == 10
        assert db.scalar(select(VirtualPosition.quantity)) == 10


@pytest.mark.asyncio
async def test_every_BUY_Fill_uses_earliest_timestamp_not_last_or_parent_clock(make_case, monkeypatch):
    case = await same_day_case(make_case, monkeypatch)
    broker = case[2]
    broker.controlled_last_buy_at = broker.controlled_buy_at + timedelta(seconds=45)
    with case[1].begin() as db:
        first = db.scalar(select(Fill))
        first.quantity = Decimal(10)
        db.add(Fill(order_id=first.order_id, strategy_id=first.strategy_id, broker_account_id=first.broker_account_id,
            broker_fill_id="CONTROLLED-second-BUY-Fill", symbol=first.symbol, side="buy", quantity=Decimal(17),
            price=first.price, filled_at=broker.controlled_last_buy_at))
    broker.history_mode = "manual_between_BUYs"  # SELL at +30s, before last BUY Fill at +45s.
    await close_case(case, "CW_TARGET")
    assert broker.history_calls == 1 and not broker.cancelled and not closes(broker)
    assert obligations(case)[0]["phase"] == "RETRY"
    with case[1]() as db:
        fills = list(db.scalars(select(Fill)))
        assert len(fills) == 2 and all(fill.side == "buy" for fill in fills)
        assert sum((fill.quantity for fill in fills), Decimal(0)) == 27
        assert db.scalar(select(VirtualPosition.quantity)) == 0
        assert db.scalar(select(OmsManagedPosition)).current_quantity == 27


@pytest.mark.asyncio
async def test_raw_history_429_keeps_shared_backoff_no_HTTP_on_immediate_or_admitted_retry(make_case, monkeypatch):
    case = await same_day_case(make_case, monkeypatch)
    broker, clock = case[2], case[4]
    broker.history_mode = "unreadable"  # Actual new page wrapper sees raw HTTP429.
    await close_case(case, "CW_TARGET")
    assert broker.history_calls == 1 and len(broker.history_page_calls) == 1
    budget = broker.history_adapter._query_budget
    assert budget.blocked_until["list-today"] > clock[0].timestamp()
    with pytest.raises(ValueError, match="history_incomplete"):
        await broker.history_adapter.read_wbexit3_day_history(broker.account, after=clock[0])
    assert len(broker.history_page_calls) == 1  # No page transport call while budget is blocked.
    assert budget.foreground_reads == 0 and not broker.history_adapter._foreground_query_tasks
    clock[0] += timedelta(seconds=11)  # Duty admits retry; existing shared30s backoff still wins.
    await close_case(case, "CW_TARGET")
    assert broker.history_calls == 2 and len(broker.history_page_calls) == 1
    assert obligations(case)[0]["phase"] == "RETRY"
    no_execution(case)
    assert budget.blocked_until["list-today"] > clock[0].timestamp() and budget.foreground_reads == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("api", ["admit", "size", "prepare"])
async def test_inherited_foreign_task_cannot_dispatch_recovered_cap_DB_or_reservation(make_case, monkeypatch, api):
    import asyncio
    from dataclasses import replace
    from project_mai_tai.oms import wbexit3, wbexit3_vp_zero, webull_attach_riskoff as wb
    from project_mai_tai.oms.managed_sell_prewire import prepare_managed_sell
    from tests.unit.test_managed_sell_prewire import attempt
    case = await same_day_case(make_case, monkeypatch)
    service, _, broker, _, clock = case
    actual_run_db = service._run_db
    inside_foreign, observed_foreign = [False], []
    async def observed_run_db(*args, **kwargs):
        if inside_foreign[0]:
            observed_foreign.append(args[0].__name__)
        return await actual_run_db(*args, **kwargs)
    monkeypatch.setattr(service, "_run_db", observed_run_db)
    visited = []
    async def inherited_provider_task():
        call, current = wb._ATR_CALL.get(), wbexit3._ATTEMPT.get()
        assert call.task is current["task"] and call.task is not asyncio.current_task()
        assert isinstance(current.get("vp_zero_exit_proof"), wbexit3_vp_zero.ExitProof)
        assert service._wbattach_exit_owners[(broker.account, broker.symbol)] is call
        event, request = attempt(broker.account, quantity=Decimal(27))
        event.payload.symbol, event.payload.reason = broker.symbol, call.reason
        request = replace(request, symbol=broker.symbol, reason=call.reason)
        before = dict(request.metadata)
        inside_foreign[0] = True
        try:
            with pytest.raises(ValueError, match="wbattach1_live_owner_call_unknown"):
                if api == "admit":
                    await wbexit3_vp_zero.admit(service, call)
                elif api == "size":
                    await service._v2_managed_sell_owned_quantity(managed_row_id=call.managed_id,
                        strategy_code="schwab_1m_v2", broker_account_name=broker.account,
                        symbol=broker.symbol, requested_quantity=27)
                else:
                    await prepare_managed_sell(service, event=event, request=request,
                        managed_row_id=call.managed_id, now=clock[0])
            assert observed_foreign == [] and request.metadata == before
            visited.append(api)
        finally:
            inside_foreign[0] = False
    broker.on_postterminal = inherited_provider_task
    await close_case(case, "CW_TARGET")
    assert visited == [api] and observed_foreign == []
    assert [r.quantity for r in closes(broker)] == [Decimal(27)] and broker.total == 1000
    assert obligations(case)[0]["phase"] == "RESOLVED"


@pytest.mark.asyncio
async def test_exact_native27_minimum_with_consistent_zero_manual_baseline(make_case, monkeypatch):
    from tests.unit.wbexit3_day_history_port import NoManualHistoryBroker
    case = await same_day_case(make_case, monkeypatch, port_type=NoManualHistoryBroker)
    broker = case[2]
    broker.total = Decimal(27)  # Separate CONTROLLED account: bot27, manual0.
    broker.originals_already_terminal = True
    with case[1].begin() as db:
        db.scalar(select(AccountPosition)).quantity = Decimal(27)
    await close_case(case, "CW_TARGET")
    assert [request.quantity for request in closes(broker)] == [Decimal(27)]
    assert broker.total == 0 and obligations(case)[0]["phase"] == "RESOLVED"
    assert [row.quantity for row in await broker.list_account_positions(broker.account)] == [Decimal(0)]
    with case[1]() as db:
        assert db.scalar(select(AccountPosition.quantity)) == 0
        assert db.scalar(select(VirtualPosition.quantity)) == 0
        assert db.scalar(select(OmsManagedPosition)).status == "closed"
