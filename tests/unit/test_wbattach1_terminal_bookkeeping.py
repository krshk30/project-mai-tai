"""Synthetic source controls; no historical POM/native acceptance is implied."""

from copy import deepcopy
from datetime import timedelta
from decimal import Decimal
import json

import pytest
from sqlalchemy import select

from project_mai_tai.broker_adapters import webull as adapter_module, webull_order_reads as reads_module
from project_mai_tai.broker_adapters.routing import RoutingBrokerAdapter
from project_mai_tai.broker_adapters.webull import WebullAccountConfig, WebullBrokerAdapter
from project_mai_tai.broker_adapters.webull_order_reads import QueryBudget, TodayOrderReader
from project_mai_tai.db.models import BrokerOrder, Fill, OmsManagedPosition, TradeIntent, VirtualPosition
from project_mai_tai.oms import webull_attach_riskoff as wb
from tests.unit.test_wbattach1_riskoff import NOW, attach, make_case as make_case, restarted


def protection(session):
    return session.scalar(select(TradeIntent).where(TradeIntent.reason == "WBATTACH1_PROTECTION"))


def economic_rows(factory):
    with factory() as session:
        return {
            "orders": [(str(o.id), str(o.intent_id), o.client_order_id, o.broker_order_id, o.status,
                        o.quantity, deepcopy(o.payload)) for o in session.scalars(select(BrokerOrder).order_by(BrokerOrder.id))],
            "fills": [(str(f.id), str(f.order_id), str(f.strategy_id), str(f.broker_account_id), f.broker_fill_id,
                       f.quantity, f.price, f.filled_at, deepcopy(f.payload))
                      for f in session.scalars(select(Fill).order_by(Fill.id))],
            "positions": [(str(p.id), p.quantity, p.average_price)
                          for p in session.scalars(select(VirtualPosition).order_by(VirtualPosition.id))],
            "managed": [(str(r.id), str(r.entry_order_id), r.status, r.current_quantity)
                        for r in session.scalars(select(OmsManagedPosition).order_by(OmsManagedPosition.id))],
        }


def cached_adapter(case, monkeypatch, *, winner="T", filled=10, damage=None):
    service, factory, broker, entry_id, _ = case
    monkeypatch.setattr(adapter_module, "datetime", wb.datetime)
    monkeypatch.setattr(reads_module, "datetime", wb.datetime)
    # Observation is strictly later than the fixed execution clock, not the
    # native cancellation event time (which this API fixture does not provide).
    case[4][0] = NOW + timedelta(milliseconds=1)
    with factory() as session:
        entry = session.get(BrokerOrder, entry_id)
        base = entry.payload[wb.KEY]["attempts"][-1]["base"]
    rows = []
    for suffix in ("T", "S"):
        coid = WebullBrokerAdapter._combo_leg_coid(base, suffix)
        quantity = filled if suffix == winner else 0
        item = {"symbol": "ABC", "side": "SELL", "quantity": "10",
                "status": "FILLED" if quantity == 10 else "CANCELLED", "filled_qty": str(quantity),
                "filled_price": "11" if quantity else None,
                "last_filled_time": NOW.isoformat() if quantity else None}
        raw = {"client_order_id": coid, "order_id": "native-" + suffix, "account_id": "ACC1", "items": [item]}
        if suffix != winner:
            if damage == "working":
                item["status"] = "WORKING"
            elif damage == "missing":
                continue
            elif damage == "wrong-account":
                item["account_id"] = "OTHER"
            elif damage == "wrong-symbol":
                item["symbol"] = "OTHER"
            elif damage == "wrong-quantity":
                item["quantity"] = "11"
            elif damage == "missing-fill-quantity":
                item.pop("filled_qty")
            elif damage == "wrong-coid":
                raw["client_order_id"] = "foreign-child"
            elif damage == "conflicting-status":
                raw["status"] = "WORKING"
            elif damage == "same-native-id":
                raw["order_id"] = "native-" + winner
        rows.append(raw)
    clock = [0.]
    reader = TodayOrderReader(QueryBudget(clock=lambda: clock[0]), 5., clock=lambda: clock[0])
    reader.read("ACC1", "unused", lambda _: (200, {"orders": rows, "has_next": False}))
    scan = reader.scans["ACC1"]
    if damage == "incomplete":
        scan.complete = False
    elif damage == "failed":
        scan.failed = True
    elif damage == "stale":
        clock[0] = 2.
        # Age alone is now valid for terminal proof. Keep this negative as an
        # old-page acquisition at/before the winning fill, without re-dating it.
        scan.rows = {coid: (at, NOW, body) for coid, (at, _acquired, body) in scan.rows.items()}
    elif damage == "prior-session":
        scan.session = "2000-01-01"
    adapter = WebullBrokerAdapter.__new__(WebullBrokerAdapter)
    adapter.accounts_by_name = {broker.account: WebullAccountConfig(account_id="ACC1")}
    adapter._list_primary_enabled = True
    adapter._today_reader = reader
    adapter._today_page = lambda *_: (503, {})  # Never construct a real SDK/provider in these controls.
    service.broker_adapter = adapter
    return adapter, base


async def close_economically(case, winner="T"):
    service, factory, broker, entry_id, _ = case
    with factory() as session:
        entry = session.get(BrokerOrder, entry_id)
        base = entry.payload[wb.KEY]["attempts"][-1]["base"]
        row_id = str(session.scalar(select(OmsManagedPosition)).id)
    assert await service._close_resolved_oco_managed_row(
        broker.account, "ABC", expected_row_id=row_id, detail={
            "symbol": "ABC", "entry_broker_order_id": "entry-native", "exit_base_client_order_id": base,
            "quantity": Decimal(10), "price": Decimal(11), "filled_at": NOW,
            "broker_order_id": "native-" + winner})


@pytest.mark.asyncio
async def test_accepted_placement_records_exact_combo_base_and_children(make_case):
    case = make_case()
    assert await attach(case)
    with case[1]() as session:
        intent = protection(session)
        entry = session.get(BrokerOrder, case[3])
        attempt = entry.payload[wb.KEY]["attempts"][-1]
        metadata = intent.payload["metadata"]
        assert intent.status == "accepted"
        assert metadata["webull_protect_combo_id"] == attempt["combo_id"] == "pair-root"
        assert metadata["webull_protect_base_client_order_id"] == attempt["base"]
        assert [metadata["webull_protect_target_client_order_id"], metadata["webull_protect_stop_client_order_id"]] == [
            WebullBrokerAdapter._combo_leg_coid(attempt["base"], s) for s in ("T", "S")]
        assert session.scalars(select(BrokerOrder).where(BrokerOrder.intent_id == intent.id)).all() == []
    assert len(case[2].requests) == 1 and case[2].cancelled == []


@pytest.mark.asyncio
@pytest.mark.parametrize("winner", ["T", "S"])
async def test_natural_fill_then_restart_terminalizes_only_bookkeeping(make_case, monkeypatch, winner):
    case = make_case()
    assert await attach(case)
    await close_economically(case, winner)
    case = restarted(case)
    cached_adapter(case, monkeypatch, winner=winner)
    before = economic_rows(case[1])
    await wb.reconcile_cached_protection_intents(case[0], ["live:orb"])
    with case[1]() as session:
        intent = protection(session)
        assert intent.status == "filled"
        after_payload = deepcopy(intent.payload)
        assert json.loads(after_payload["metadata"]["wbattach1_terminal_bookkeeping"])["combo_id"] == "pair-root"
        assert session.scalars(select(BrokerOrder).where(BrokerOrder.intent_id == intent.id)).all() == []
    await wb.reconcile_cached_protection_intents(case[0], None)
    with case[1]() as session:
        assert protection(session).payload == after_payload
    assert economic_rows(case[1]) == before


@pytest.mark.asyncio
async def test_both_terminal_zero_cancels_bookkeeping_without_closing_position(make_case, monkeypatch):
    case = make_case()
    assert await attach(case)
    cached_adapter(case, monkeypatch, filled=0)
    before = economic_rows(case[1])
    await wb.reconcile_cached_protection_intents(case[0], None)
    with case[1]() as session:
        assert protection(session).status == "cancelled"
    assert economic_rows(case[1]) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("damage", ["working", "missing", "wrong-account", "wrong-symbol", "wrong-quantity",
    "missing-fill-quantity", "wrong-coid", "conflicting-status", "same-native-id", "incomplete", "failed",
    "stale", "prior-session"])
async def test_unknown_pair_never_terminalizes_or_changes_money(make_case, monkeypatch, damage):
    case = make_case()
    assert await attach(case)
    await close_economically(case)
    cached_adapter(case, monkeypatch, damage=damage)
    before = economic_rows(case[1])
    await wb.reconcile_cached_protection_intents(case[0], None)
    with case[1]() as session:
        assert protection(session).status == "accepted"
        assert "wbattach1_terminal_bookkeeping" not in protection(session).payload["metadata"]
    assert economic_rows(case[1]) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("damage", ["partial", "missing-ledger", "open-managed", "wrong-price", "wrong-time",
    "wrong-intent", "wrong-base", "missing-combo", "duplicate-attempt", "wrong-managed", "foreign-account",
    "zero-conflicting-ledger"])
async def test_ledger_and_episode_binding_are_required(make_case, monkeypatch, damage):
    case = make_case()
    assert await attach(case)
    if damage not in {"partial", "missing-ledger", "open-managed"}:
        await close_economically(case)
    elif damage == "missing-ledger":
        with case[1].begin() as session:
            row = session.scalar(select(OmsManagedPosition))
            case[0].store.close_managed_position(session, row)
    elif damage == "open-managed":
        await close_economically(case)
        with case[1].begin() as session:
            row = session.scalar(select(OmsManagedPosition))
            assert row.entry_order_id == case[3] and row.status == "closed" and row.current_quantity == 0
            # Deliberate negative state AFTER the real economic wrapper; do not
            # bypass its managed-entry proof with an unstamped direct writer call.
            row.status, row.current_quantity = "open", 10
    cached_adapter(case, monkeypatch, filled=2 if damage == "partial" else 0 if damage == "zero-conflicting-ledger" else 10)
    with case[1].begin() as session:
        intent = protection(session)
        metadata = dict(intent.payload["metadata"])
        if damage == "wrong-base":
            metadata["webull_protect_base_client_order_id"] = "foreign-base"
        elif damage == "missing-combo":
            metadata.pop("webull_protect_combo_id")
        elif damage == "wrong-managed":
            metadata["managed_row_id"] = str(intent.id)
        elif damage == "foreign-account":
            intent.broker_account_id = intent.strategy_id
        elif damage == "duplicate-attempt":
            entry = session.get(BrokerOrder, case[3])
            journal = deepcopy(entry.payload[wb.KEY])
            journal["attempts"].append(deepcopy(journal["attempts"][0]))
            entry.payload = {**entry.payload, wb.KEY: journal}
        elif damage in {"wrong-price", "wrong-time", "wrong-intent"}:
            fill = session.scalar(select(Fill).where(Fill.side == "sell"))
            if damage == "wrong-price":
                fill.price = Decimal(12)
            elif damage == "wrong-time":
                fill.filled_at += timedelta(seconds=1)
            else:
                session.get(BrokerOrder, fill.order_id).intent_id = intent.id
        intent.payload = {**intent.payload, "metadata": metadata}
    before = economic_rows(case[1])
    await wb.reconcile_cached_protection_intents(case[0], None)
    with case[1]() as session:
        assert protection(session).status == "accepted"
    assert economic_rows(case[1]) == before


@pytest.mark.asyncio
async def test_foreign_account_selection_does_not_settle(make_case, monkeypatch):
    case = make_case()
    assert await attach(case)
    await close_economically(case)
    cached_adapter(case, monkeypatch)
    await wb.reconcile_cached_protection_intents(case[0], ["live:other"])
    with case[1]() as session:
        assert protection(session).status == "accepted"


@pytest.mark.asyncio
async def test_lost_ack_is_not_repaired_from_child_presence(make_case, monkeypatch):
    case = make_case()
    case[2].mode = "late"
    assert not await attach(case)
    cached_adapter(case, monkeypatch, filled=0)
    before = economic_rows(case[1])
    await wb.reconcile_cached_protection_intents(case[0], None)
    with case[1]() as session:
        assert protection(session).status == "pending"
    assert economic_rows(case[1]) == before


@pytest.mark.asyncio
async def test_other_historical_attempt_is_not_released_or_rewritten(make_case, monkeypatch):
    case = make_case()
    assert await attach(case)
    await close_economically(case)
    cached_adapter(case, monkeypatch)
    with case[1].begin() as session:
        entry = session.get(BrokerOrder, case[3])
        journal = deepcopy(entry.payload[wb.KEY])
        historical = {**journal["attempts"][0], "base": "historical-original-base",
                      "intent_id": str(entry.intent_id), "phase": "released"}
        journal["attempts"].insert(0, historical)
        entry.payload = {**entry.payload, wb.KEY: journal}
    before = economic_rows(case[1])
    await wb.reconcile_cached_protection_intents(case[0], None)
    with case[1]() as session:
        assert protection(session).status == "filled"
    assert economic_rows(case[1]) == before


@pytest.mark.asyncio
async def test_real_sync_hook_runs_after_native_poll_without_extra_broker_reads(make_case, monkeypatch):
    case = make_case()
    assert await attach(case)
    service = case[0]
    sequence = []
    async def orders(**_kwargs):
        return {"orders": 0, "terminal_orders": 0}
    async def poll():
        await close_economically(case)
        cached_adapter(case, monkeypatch)
        sequence.append("poll")
    async def positions(**_kwargs):
        with case[1]() as session:
            assert protection(session).status == "accepted"
        sequence.append("positions")
        return {"accounts": 1, "positions": 0}
    async def no_refresh(*_args):
        return None
    service.settings.orb_live_schwab_orders_enabled = False
    service.sync_broker_orders = orders
    service.sync_broker_positions = positions
    service._poll_native_oco_exits = poll
    service._refresh_native_oco_armed_state = no_refresh
    service.settings.strategy_schwab_1m_v2_false_flip_enabled = True
    class Signal:
        def set(self):
            with case[1]() as session:
                assert protection(session).status == "accepted"
            sequence.append("signal")
    monkeypatch.setattr(service, "_falseflip_signal", lambda: Signal())
    for name in ("_maybe_emit_p0a_census", "_maybe_emit_order_event_census", "_maybe_emit_broker_read_census"):
        monkeypatch.setattr(service, name, lambda: None)
    result = await service._sync_broker_state_pass(account_names=["live:orb"])
    assert result["orders"] == 0 and sequence == ["poll", "positions", "signal"]
    with case[1]() as session:
        assert protection(session).status == "filled"


def test_router_uses_existing_webull_cache_and_never_constructs_provider():
    def forbidden():
        pytest.fail("cached bookkeeping instantiated a provider")
    router = RoutingBrokerAdapter(default_provider="schwab", provider_by_account={"live:orb": "webull"},
                                  factories_by_provider={"webull": forbidden, "schwab": forbidden})
    assert router.cached_attach_pair_terminal("live:orb", "ABC", "base", Decimal(10)) is None
    class Cached:
        def cached_attach_pair_terminal(self, *args):
            return args
    router._adapters_by_provider["webull"] = Cached()
    assert router.cached_attach_pair_terminal("live:orb", "ABC", "base", Decimal(10)) == (
        "live:orb", "ABC", "base", Decimal(10))
    assert router.cached_attach_pair_terminal("live:schwab", "ABC", "base", Decimal(10)) is None


@pytest.mark.asyncio
async def test_cached_proof_does_not_claim_budget_or_mutate_shared_rows(make_case, monkeypatch):
    case = make_case()
    assert await attach(case)
    adapter, base = cached_adapter(case, monkeypatch, filled=0)
    reader = adapter._today_reader
    before = deepcopy(reader.budget.attempts)
    proof = adapter.cached_attach_pair_terminal("live:orb", "ABC", base, Decimal(10))
    assert proof is not None
    proof["children"][0]["evidence"]["complete"] = False
    again = adapter.cached_attach_pair_terminal("live:orb", "ABC", base, Decimal(10))
    assert again["children"][0]["evidence"]["complete"] is True
    assert reader.budget.attempts == before


def test_cache_busy_returns_unknown_without_waiting_for_broker_scan():
    reader = TodayOrderReader(QueryBudget(), 5.)
    with reader.lock:
        assert reader.peek_complete("ACC1", ["baseT", "baseS"]) is None
