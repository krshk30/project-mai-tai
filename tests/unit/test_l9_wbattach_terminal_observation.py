"""L9 synthetic causal controls; STAK shape is not an actual native receipt."""

import asyncio
from contextlib import contextmanager
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from threading import Event, get_ident
from uuid import UUID

import pytest
from sqlalchemy import select

from project_mai_tai.broker_adapters import webull_order_reads as reads_module, webull as adapter_module
from project_mai_tai.broker_adapters.routing import RoutingBrokerAdapter
from project_mai_tai.broker_adapters.webull import WebullAccountConfig, WebullBrokerAdapter
from project_mai_tai.broker_adapters.webull_order_reads import QueryBudget, TodayOrderReader
from project_mai_tai.db.models import BrokerOrder, Fill, OmsManagedPosition, TradeIntent, VirtualPosition
from project_mai_tai.oms import webull_attach_riskoff as wb
from tests.unit.test_wbattach1_riskoff import NOW, attach, make_case as make_case
from tests.unit.test_wbattach1_terminal_bookkeeping import (
    cached_adapter, close_economically, economic_rows, protection,
)
from tests.unit.test_wbattach1_bounded_bookkeeping import pending
from tests.unit.test_webull_adapter import _ServerException


STAK_FILL = datetime(2026, 10, 22, 14, 11, 49, 25000, tzinfo=UTC)


async def stak_control(make_case):
    """Adapt only synthetic setup preimages, then use the actual economic wrapper."""
    case = make_case()
    assert await attach(case)
    service, factory, broker, entry_id, clock = case
    entry_at = STAK_FILL.replace(hour=14, minute=0, second=29, microsecond=0)
    base = "synthetic-STAK-protect"
    with factory.begin() as session:
        entry = session.get(BrokerOrder, entry_id)
        entry.symbol, entry.quantity, entry.submitted_at = "STAK", Decimal(189), entry_at
        parent = session.get(TradeIntent, entry.intent_id)
        parent.symbol, parent.quantity, parent.created_at = "STAK", Decimal(189), entry_at
        buy = session.scalar(select(Fill).where(Fill.side == "buy"))
        buy.symbol, buy.quantity, buy.price, buy.filled_at = "STAK", Decimal(189), Decimal("1.59"), entry_at
        position = session.scalar(select(VirtualPosition))
        position.symbol, position.quantity, position.average_price = "STAK", Decimal(189), Decimal("1.59")
        row = session.scalar(select(OmsManagedPosition))
        row.symbol, row.current_quantity, row.original_quantity = "STAK", 189, 189
        row.entry_price, row.entry_time = Decimal("1.59"), entry_at
        journal = deepcopy(entry.payload[wb.KEY])
        journal["attempts"][-1].update(base=base, quantity="189", combo_id="synthetic-STAK-combo")
        entry.payload = {**entry.payload, wb.KEY: journal, "webull_protect_base_client_order_id": base}
        intent = protection(session)
        intent.symbol, intent.quantity = "STAK", Decimal(189)
        intent.created_at = entry_at + timedelta(seconds=8)
        intent.payload = {**intent.payload, "metadata": {**intent.payload["metadata"],
            "webull_protect_base_client_order_id": base, "webull_protect_combo_id": "synthetic-STAK-combo",
            "webull_protect_target_client_order_id": WebullBrokerAdapter._combo_leg_coid(base, "T"),
            "webull_protect_stop_client_order_id": WebullBrokerAdapter._combo_leg_coid(base, "S")}}
        row_id = str(row.id)
    clock[0] = STAK_FILL + timedelta(seconds=1)
    await service._bind_webull_protect_memory(broker.account, "STAK", base, "owned-entry")
    assert await service._close_resolved_oco_managed_row(broker.account, "STAK", expected_row_id=row_id,
        detail={"symbol": "STAK", "entry_broker_order_id": "entry-native", "exit_base_client_order_id": base,
                "quantity": Decimal(189), "price": Decimal("1.46"), "filled_at": STAK_FILL,
                "broker_order_id": "synthetic-STAK-stop"})
    return case, base


def stak_reader(case, base, monkeypatch, *, damage=None):
    service, _factory, broker, _entry, clock = case
    monkeypatch.setattr(reads_module, "datetime", wb.datetime)
    monkeypatch.setattr(adapter_module, "datetime", wb.datetime)
    rows = []
    for suffix in ("T", "S"):
        stop = suffix == "S"
        item = {"symbol": "STAK", "side": "SELL", "quantity": "189",
                "order_status": "FILLED" if stop else "CANCELLED", "filled_qty": "189" if stop else "0"}
        if stop:
            item.update(filled_price="1.46", last_filled_time=STAK_FILL.isoformat())
        if damage == "working" and not stop:
            item["order_status"] = "WORKING"
        elif damage == "partial" and stop:
            item.update(order_status="PARTIALLY_FILLED", filled_qty="188")
        elif damage == "overfill" and stop:
            item["filled_qty"] = "190"
        rows.append({"account_id": "ACC1", "client_order_id": WebullBrokerAdapter._combo_leg_coid(base, suffix),
            "order_id": "synthetic-STAK-stop" if stop else "synthetic-STAK-target", "items": [item]})
    mono = [0.]
    reader = TodayOrderReader(QueryBudget(clock=lambda: mono[0]), 15, clock=lambda: mono[0])
    reader.refresh_one_page("ACC1", lambda _: (200, {"orders": rows, "has_next": False}))
    scan = reader.scans["ACC1"]
    if damage in {"before_winner", "equal_winner", "sibling_before_winner"}:
        for coid, (at, acquired, body) in list(scan.rows.items()):
            if damage != "sibling_before_winner" or coid == rows[0]["client_order_id"]:
                scan.rows[coid] = (at, STAK_FILL if damage == "equal_winner" else STAK_FILL - timedelta(seconds=1), body)
    elif damage == "failed":
        scan.failed = True
    elif damage == "incomplete":
        scan.complete = False
    elif damage == "future":
        scan.rows = {coid: (at, clock[0] + timedelta(seconds=1), body)
                     for coid, (at, _acquired, body) in scan.rows.items()}
    mono[0] += 3600.
    adapter = WebullBrokerAdapter.__new__(WebullBrokerAdapter)
    adapter.accounts_by_name = {broker.account: WebullAccountConfig(account_id="ACC1")}
    adapter._list_primary_enabled, adapter._today_reader = True, reader
    calls = []
    adapter._today_page = lambda *_: calls.append("read") or (503, {})
    # Retained proof controls must not be rescued by a fresh scan.
    monkeypatch.setattr(adapter, "refresh_attach_terminal_scan", lambda _: None)
    service.broker_adapter = adapter
    return adapter, reader, calls


@pytest.mark.asyncio
async def test_stak_189_stop_shape_settles_from_old_complete_postfill_observations(make_case, monkeypatch):
    case, base = await stak_control(make_case)
    adapter, reader, calls = stak_reader(case, base, monkeypatch)
    before = economic_rows(case[1])
    assert reader.peek_complete("ACC1", adapter.exit_pair_leg_client_order_ids(base)) is None
    proof = adapter.cached_attach_pair_terminal("live:orb", "STAK", base, Decimal(189))
    assert proof is not None
    sibling = next(child for child in proof["children"] if child["status"] == "cancelled")
    assert sibling["filled_quantity"] == "0" and sibling["native_terminal_at"] is None
    assert sibling["native_terminal_time_state"] == "UNKNOWN"
    assert datetime.fromisoformat(sibling["terminal_observed_at"]) > STAK_FILL
    await wb.reconcile_cached_protection_intents(case[0], ["live:orb"])
    with case[1]() as session:
        assert protection(session).status == "filled"
        assert session.scalars(select(BrokerOrder).where(BrokerOrder.intent_id == protection(session).id)).all() == []
    assert calls == [] and economic_rows(case[1]) == before


@pytest.mark.asyncio
async def test_absent_scan_gets_one_fresh_worker_http_then_settles_stak_shape(make_case, monkeypatch):
    case, base = await stak_control(make_case)
    adapter, reader, _calls = stak_reader(case, base, monkeypatch)
    rows = [deepcopy(value[2]) for value in reader.scans["ACC1"].rows.values()]
    reader.scans.clear()
    calls, loop_thread = [], get_ident()

    def page(account, cursor):
        assert get_ident() != loop_thread
        calls.append((account.account_id, cursor))
        return 200, {"orders": rows, "has_next": False}

    adapter._today_page = page
    monkeypatch.setattr(adapter, "refresh_attach_terminal_scan",
        WebullBrokerAdapter.refresh_attach_terminal_scan.__get__(adapter, WebullBrokerAdapter))
    before = economic_rows(case[1])
    await wb.reconcile_cached_protection_intents(case[0], None)
    with case[1]() as session:
        assert protection(session).status == "filled"
    assert calls == [("ACC1", "")] and economic_rows(case[1]) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("damage", ["before_winner", "equal_winner", "sibling_before_winner",
                                    "working", "partial", "overfill", "failed", "incomplete", "future"])
async def test_no_settlement_from_wrong_causality_status_quantity_or_scan(make_case, monkeypatch, damage):
    case, base = await stak_control(make_case)
    stak_reader(case, base, monkeypatch, damage=damage)
    before = economic_rows(case[1])
    await wb.reconcile_cached_protection_intents(case[0], None)
    with case[1]() as session:
        assert protection(session).status == "accepted"
    assert economic_rows(case[1]) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("damage", ["missing_fill", "price", "time", "open_managed"])
async def test_postfill_native_observation_never_bypasses_economic_guards(make_case, monkeypatch, damage):
    case, base = await stak_control(make_case)
    stak_reader(case, base, monkeypatch)
    with case[1].begin() as session:
        fill = session.scalar(select(Fill).where(Fill.side == "sell"))
        if damage == "missing_fill":
            session.delete(fill)
        elif damage == "price":
            fill.price = Decimal("1.47")
        elif damage == "time":
            fill.filled_at += timedelta(milliseconds=1)
        else:
            row = session.scalar(select(OmsManagedPosition))
            row.status, row.current_quantity = "open", 189
    before = economic_rows(case[1])
    await wb.reconcile_cached_protection_intents(case[0], None)
    with case[1]() as session:
        assert protection(session).status == "accepted"
    assert economic_rows(case[1]) == before


def scanning_adapter(case, monkeypatch, *, status=200):
    adapter = WebullBrokerAdapter.__new__(WebullBrokerAdapter)
    adapter.host = "synthetic-host"
    adapter.accounts_by_name = {name: WebullAccountConfig(account_id=identity) for name, identity in (
        ("live:orb", "ACC1"), ("live:alias", "ACC1"), ("live:other", "ACC2"))}
    adapter._list_primary_enabled = True
    mono = [0.]
    adapter._today_reader = TodayOrderReader(QueryBudget(clock=lambda: mono[0]), 15, clock=lambda: mono[0])
    monkeypatch.setattr(reads_module, "datetime", wb.datetime)
    calls = []

    def page(account, cursor):
        calls.append((account.account_id, cursor, get_ident()))
        if status == "sdk429":
            raise _ServerException("TOO_MANY_REQUESTS", "synthetic429", 429)
        return status, {"orders": [{"client_order_id": "page-" + str(len(calls))}], "has_next": True}

    adapter._today_page = page
    case[0].broker_adapter = adapter
    return adapter, mono, calls


def scan_pending(case, ids, account="live:orb"):
    pending(case, ids, account=account)
    with case[1].begin() as session:
        for intent in session.scalars(select(TradeIntent).where(TradeIntent.reason == "WBATTACH1_PROTECTION")):
            intent.payload = {"metadata": {"webull_protect_base_client_order_id": "synthetic-base"}}


@pytest.mark.asyncio
async def test_one_http_per_physical_account_entire_three_batch_sweep_not_per_alias_or_intent(make_case, monkeypatch):
    case = make_case()
    scan_pending(case, range(1, 4))
    scan_pending(case, range(4, 11), "live:alias")
    scan_pending(case, range(11, 21), "live:other")
    adapter, mono, calls = scanning_adapter(case, monkeypatch)
    monkeypatch.setattr(wb, "monotonic", lambda: 0.)
    loop_thread = get_ident()
    before = economic_rows(case[1])
    for _ in range(3):
        await wb.reconcile_cached_protection_intents(case[0], None)
    assert [call[:2] for call in calls] == [("ACC1", ""), ("ACC2", "")]
    assert all(thread != loop_thread for _, _, thread in calls)
    mono[0] += 15.
    for _ in range(3):
        await wb.reconcile_cached_protection_intents(case[0], None)
    assert [call[:2] for call in calls[2:]] == [("ACC1", "page-1"), ("ACC2", "page-2")]
    assert all(not scan.complete for scan in adapter._today_reader.scans.values())
    assert economic_rows(case[1]) == before


@pytest.mark.asyncio
async def test_failed_commit_does_not_repeat_refresh_claim_in_same_sweep(make_case, monkeypatch):
    case = make_case()
    scan_pending(case, range(1, 11))
    _adapter, mono, calls = scanning_adapter(case, monkeypatch)
    monkeypatch.setattr(wb, "monotonic", lambda: 0.)
    factory = case[1]

    @contextmanager
    def fail_commit():
        with factory() as session:
            session.commit = lambda: (_ for _ in ()).throw(RuntimeError("synthetic commit failure"))
            yield session

    case[0].session_factory = fail_commit
    with pytest.raises(RuntimeError, match="synthetic commit failure"):
        await wb.reconcile_cached_protection_intents(case[0], None)
    assert len(calls) == 1 and case[0]._wbattach_bookkeeping_sweep.cursors == {}
    case[0].session_factory = factory
    mono[0] += 15.
    await wb.reconcile_cached_protection_intents(case[0], None)
    assert len(calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [429, "sdk429"])
async def test_returned_or_sdk_429_never_retries_same_account_within_sweep(make_case, monkeypatch, status):
    case = make_case()
    scan_pending(case, range(1, 21))
    adapter, mono, calls = scanning_adapter(case, monkeypatch, status=status)
    monkeypatch.setattr(wb, "monotonic", lambda: 0.)
    for _ in range(3):
        await wb.reconcile_cached_protection_intents(case[0], None)
        mono[0] += 15.
    assert len(calls) == 1
    assert adapter._today_reader.scans["ACC1"].failed


@pytest.mark.asyncio
async def test_cancelled_waiter_cannot_release_worker_refresh_claim_or_overlap(make_case, monkeypatch):
    case = make_case()
    scan_pending(case, range(1, 11))
    adapter, _mono, calls = scanning_adapter(case, monkeypatch)
    monkeypatch.setattr(wb, "monotonic", lambda: 0.)
    entered, release = Event(), Event()
    original = adapter._today_page

    def blocked(*args):
        entered.set()
        assert release.wait(2.), "synthetic page barrier timed out"
        return original(*args)

    adapter._today_page = blocked
    first = asyncio.create_task(wb.reconcile_cached_protection_intents(case[0], None))
    try:
        assert await asyncio.to_thread(entered.wait, 2.)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        await wb.reconcile_cached_protection_intents(case[0], None)
        assert case[0]._wbattach_bookkeeping_sweep.lock.locked() and calls == []
    finally:
        release.set()
        async def unlocked():
            while case[0]._wbattach_bookkeeping_sweep.lock.locked():
                await asyncio.sleep(.001)
        await asyncio.wait_for(unlocked(), 2.)
    await wb.reconcile_cached_protection_intents(case[0], None)
    assert len(calls) == 1


def test_refresh_router_never_constructs_provider_and_maps_existing_aliases():
    def forbidden():
        pytest.fail("terminal scan instantiated a provider")
    router = RoutingBrokerAdapter(default_provider="schwab", provider_by_account={"live:orb": "webull"},
        factories_by_provider={"webull": forbidden, "schwab": forbidden})
    assert router.attach_terminal_scan_scope("live:orb") is None
    assert router.refresh_attach_terminal_scan("live:orb") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("scope", [None, ["live:other"]])
async def test_no_pending_in_selected_scope_means_no_fresh_http(make_case, monkeypatch, scope):
    case = make_case()
    if scope is not None:
        scan_pending(case, range(1, 3))
    _adapter, _mono, calls = scanning_adapter(case, monkeypatch)
    await wb.reconcile_cached_protection_intents(case[0], scope)
    assert calls == []


@pytest.mark.asyncio
async def test_malformed_legacy_metadata_does_not_block_cursor_or_refresh_later_rows(make_case, monkeypatch):
    case = make_case()
    scan_pending(case, range(1, 11))
    with case[1].begin() as session:
        for identifier, payload in ((1, ["unknown"]), (2, {"metadata": []}),
                                    (3, {"metadata": {"webull_protect_base_client_order_id": 123}})):
            session.get(TradeIntent, UUID(int=identifier)).payload = payload
    _adapter, _mono, calls = scanning_adapter(case, monkeypatch)
    monkeypatch.setattr(wb, "monotonic", lambda: 0.)
    await wb.reconcile_cached_protection_intents(case[0], None)
    assert case[0]._wbattach_bookkeeping_sweep.cursors[None][0] == UUID(int=8)
    await wb.reconcile_cached_protection_intents(case[0], None)
    assert case[0]._wbattach_bookkeeping_sweep.cursors[None] == (None, None) and len(calls) == 1
