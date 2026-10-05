"""Account-wide OCO hints bound traffic, never establish managed-entry ownership."""
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from project_mai_tai.db.models import BrokerOrder
from project_mai_tai.oms import service as service_module
from tests.unit import test_ownmix1_entry_binding as entry_tests
from tests.unit.test_ownmix1_entry_binding import ACCT, V2_COID, _entry

replay = entry_tests.replay
NOW = datetime(2026, 10, 5, 13, 40, tzinfo=UTC)


@pytest.fixture(autouse=True)
def owned_clock(monkeypatch):
    monkeypatch.setattr(service_module, "utcnow", lambda: NOW)


@pytest.mark.parametrize("hinted", [set(), {"FOREIGN"}])
@pytest.mark.asyncio
async def test_empty_or_foreign_symbol_hint_skips_exact_parent(replay, monkeypatch, hinted):
    service, sessions, _, _ = replay
    service._managed_v2_symbols = {(ACCT, "MI")}
    hints, parents = [], []

    async def hint(account, symbols):
        hints.append((account, symbols))
        return hinted

    async def parent(*args):
        parents.append(args)
        return {"working": ["FOREIGN-T", "FOREIGN-S"], "filled": False, "unsafe": False}

    monkeypatch.setattr(service.broker_adapter, "fetch_armed_native_oco_symbols", hint)
    monkeypatch.setattr(service.broker_adapter, "fetch_exit_legs_for_entry", parent)
    await service._refresh_native_oco_armed_state([ACCT])
    assert hints == [(ACCT, ["MI"])] and parents == []
    assert not service._native_oco_stand_down_active(ACCT, "MI")
    with sessions() as session:
        assert entry_tests._row(service, session).entry_client_order_id == V2_COID


@pytest.mark.asyncio
async def test_missing_hint_capability_has_no_per_row_fallback(replay, monkeypatch):
    service, _, _, _ = replay
    service._managed_v2_symbols = {(ACCT, "MI")}
    parents = []

    async def parent(*args):
        parents.append(args)
        return {"working": ["T", "S"], "filled": False, "unsafe": False}

    monkeypatch.setattr(service.broker_adapter, "fetch_armed_native_oco_symbols", None)
    monkeypatch.setattr(service.broker_adapter, "fetch_exit_legs_for_entry", parent)
    await service._refresh_native_oco_armed_state([ACCT])
    assert parents == [] and not service._native_oco_stand_down_active(ACCT, "MI")


@pytest.mark.asyncio
async def test_failed_hint_does_not_renew_or_fall_back(replay, monkeypatch):
    service, _, _, _ = replay
    service._managed_v2_symbols = {(ACCT, "MI")}
    stamp = NOW - timedelta(seconds=20)
    service._native_oco_armed_confirmed_at[(ACCT, "MI")] = stamp
    parents = []

    async def hint(*args):
        raise RuntimeError("HTTP 429")

    async def parent(*args):
        parents.append(args)

    monkeypatch.setattr(service.broker_adapter, "fetch_armed_native_oco_symbols", hint)
    monkeypatch.setattr(service.broker_adapter, "fetch_exit_legs_for_entry", parent)
    await service._refresh_native_oco_armed_state([ACCT])
    assert parents == [] and service._native_oco_armed_confirmed_at[(ACCT, "MI")] == stamp


@pytest.mark.asyncio
async def test_six_bound_rows_use_one_hint_and_only_named_owned_parent(replay, monkeypatch):
    service, sessions, _, _ = replay
    symbols = ["MI", "AAA", "BBB", "CCC", "DDD", "EEE"]
    with sessions() as session:
        entry = _entry(session)
        parent_id = entry.broker_order_id
        for symbol in symbols[1:]:
            order = BrokerOrder(
                intent_id=entry.intent_id, strategy_id=entry.strategy_id,
                broker_account_id=entry.broker_account_id, symbol=symbol, side="buy",
                quantity=Decimal("1"), status="filled", client_order_id=f"owned-{symbol}",
                broker_order_id=f"owned-parent-{symbol}", payload={"native_oco_bracket": "true"},
                order_type="STOP_LIMIT", time_in_force="day",
            )
            session.add(order)
            session.flush()
            service.store.create_managed_position(
                session, strategy_code="schwab_1m_v2", broker_account_name=ACCT,
                symbol=symbol, quantity=1, entry_price=Decimal("3.31"),
                entry_order_id=order.id, entry_client_order_id=order.client_order_id,
            )
        session.commit()
    service._managed_v2_symbols = {(ACCT, symbol) for symbol in symbols}
    hints, parents = [], []

    async def hint(account, requested):
        hints.append((account, requested))
        return {"MI", "FOREIGN"}

    async def parent(account, parent):
        parents.append((account, parent))
        return {"working": ["T", "S"], "filled": False, "unsafe": False}

    monkeypatch.setattr(service.broker_adapter, "fetch_armed_native_oco_symbols", hint)
    monkeypatch.setattr(service.broker_adapter, "fetch_exit_legs_for_entry", parent)
    await service._refresh_native_oco_armed_state([ACCT])
    assert hints == [(ACCT, sorted(symbols))] and parents == [(ACCT, parent_id)]
    assert set(service._native_oco_armed_confirmed_at) == {(ACCT, "MI")}


@pytest.mark.asyncio
async def test_hint_is_fresh_each_sync_and_never_stands_down_by_itself(replay, monkeypatch):
    service, _, _, _ = replay
    service._managed_v2_symbols = {(ACCT, "MI")}
    hints, parents = [], []
    answers = iter([{"MI"}, set(), {"MI"}])

    async def hint(account, symbols):
        hints.append((account, symbols))
        return next(answers)

    async def parent(*args):
        parents.append(args)
        return {"working": [], "filled": False, "unsafe": False}

    monkeypatch.setattr(service.broker_adapter, "fetch_armed_native_oco_symbols", hint)
    monkeypatch.setattr(service.broker_adapter, "fetch_exit_legs_for_entry", parent)
    for _ in range(3):
        await service._refresh_native_oco_armed_state([ACCT])
        assert not service._native_oco_stand_down_active(ACCT, "MI")
    assert len(hints) == 3 and len(parents) == 2
