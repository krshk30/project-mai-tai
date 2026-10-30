"""SOURCE UNRUN. Four C1 01:32 narrow controls; no full/native boot claim."""
from copy import deepcopy
from datetime import datetime
import json
import logging

import pytest
from sqlalchemy import select

from project_mai_tai.db.models import BrokerOrder, Fill, OmsManagedPosition, SystemIncident, VirtualPosition
from project_mai_tai.oms import orb_protection_work, wbexit2, wbexit3
from tests.unit.wbexit3_narrow_boot_fixture import (
    build_case, canonical, finish_case, receipt, rows, run_narrow_cut, seed_controlled_aged_duties,
)


def observe_actual_orb_selector(monkeypatch):
    seen = []
    actual = orb_protection_work.load_protection_work
    def observed(db, account):
        result = actual(db, account)  # Real guard/full query; no injected work/permission.
        seen.append({"account": account, "work": [str(value) for value in result]})
        return result
    monkeypatch.setattr(orb_protection_work, "load_protection_work", observed)
    return seen


def assert_actual_historical_orb(case, seen):
    service, factory, _, _, _, _, binding = case
    assert service.settings.orb_webull_account_name == "live:orb"
    assert seen == [{"account": "live:orb", "work": []}]
    with factory() as db:
        actual = db.scalar(orb_protection_work.protection_guard_query("live:orb"))
        assert str(actual) == binding["orb_protection_work"]["candidate"]["id"]
        order = db.get(BrokerOrder, actual)
        assert order.payload == binding["orb_protection_work"]["candidate"]["payload"]
        assert order.status == "rejected"
        assert db.scalar(select(VirtualPosition.id).where(
            VirtualPosition.strategy_id == order.strategy_id,
            VirtualPosition.broker_account_id == order.broker_account_id,
            VirtualPosition.symbol == order.symbol)) is None
        assert db.scalar(select(Fill.id).where(Fill.order_id == order.id)) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("on", [False, True], ids=["WB3-OFF", "WB3-ON"])
async def test_real_close_empty_wb_owned_projection_narrow_cut(tmp_path, monkeypatch, record_property, on):
    case = build_case(tmp_path, on=on)
    service, factory, broker, redis, engine, _, _ = case
    seen = observe_actual_orb_selector(monkeypatch)
    try:
        before = rows(factory)
        result2, summary, spawned = await run_narrow_cut(case)
        after = rows(factory)
        assert before == after
        assert summary == {"orders": 0, "terminal_orders": 0}
        assert result2["status"] == "INVENTORY_READY" and result2["owners"] == []
        assert service._wbexit2_boot_complete is True and service._wbexit2_pending == {}
        assert service.__dict__.get("_wbexit3_pending", {}) == {}
        assert (service.__dict__.get("_wbexit3_boot_complete") is True) is on
        assert broker.calls == redis.calls == [] and spawned
        assert_actual_historical_orb(case, seen)
        with factory() as db:
            assert list(db.scalars(select(SystemIncident))) == []
            assert list(db.scalars(select(OmsManagedPosition))) == []
            assert db.scalar(select(BrokerOrder.id).where(BrokerOrder.side == "sell")) is None
        record_property("WB3_narrow_real_empty_boot", json.dumps(receipt(case, real=True, on=on,
            before=before, after=after, result2=result2, summary=summary, orb_spawned=spawned,
            inventory=[]), sort_keys=True))
    finally:
        await finish_case(case)


@pytest.mark.asyncio
@pytest.mark.parametrize("on", [False, True], ids=["WB3-OFF", "WB3-ON"])
async def test_controlled_prior_day_duties_retained_unknown_no_boot_sell(
        tmp_path, monkeypatch, record_property, caplog, on):
    case = build_case(tmp_path, on=on)
    service, factory, broker, redis, engine, captured, _ = case
    # Business observation clock only. These are explicitly CONTROLLED ages;
    # the real export contains no prior-day WB2/WB3 owner to replay.
    monkeypatch.setattr(wbexit3, "_now", lambda: captured)
    state2, state3 = seed_controlled_aged_duties(case)
    # Real constructor force-resets root logging; capture actual service records.
    service.logger.addHandler(caplog.handler)
    seen = observe_actual_orb_selector(monkeypatch)
    try:
        before = rows(factory)
        with caplog.at_level(logging.CRITICAL):
            result2, summary, spawned = await run_narrow_cut(case)
        after = rows(factory)
        assert before == after  # No new duty/order/Fill or rewriting old nonce/quantity.
        assert summary == {"orders": 0, "terminal_orders": 0}
        assert result2["status"] == "INVENTORY_READY_HOLDS_PRESENT"
        assert result2["orphan_nonces"] == 1
        assert service._wbexit2_pending[(state2["identity"]["account"], "AIOK")] == state2
        assert service._wbexit2_boot_issues == [{"owner_id": state2["owner_id"],
            "reason": "prior_invocation_physical_quiescence_UNKNOWN"}]
        if on:
            assert service._wbexit3_pending[state3["owner_id"]] == state3
            assert any("WBEXIT3-BOOT-UNKNOWN" in value.message for value in caplog.records)
            assert any("WBEXIT3-PAGED" in value.message and state3["owner_id"] in value.message
                       for value in caplog.records)
        else:
            assert "_wbexit3_pending" not in service.__dict__
        assert broker.calls == redis.calls == [] and spawned
        assert_actual_historical_orb(case, seen)
        inventory = []
        with factory() as db:
            incidents = list(db.scalars(select(SystemIncident).order_by(SystemIncident.id)))
            assert len(incidents) == 2 and all(value.status == "open" for value in incidents)
            actual = {value.payload["source"]: deepcopy(value.payload) for value in incidents}
            assert actual == {"wbexit2": state2, "wbexit3": state3}
            assert db.scalar(select(OmsManagedPosition.current_quantity)) == 27
            assert db.scalar(select(OmsManagedPosition.status)) == "open"
            assert db.scalar(select(BrokerOrder.id).where(BrokerOrder.side == "sell")) is None
            assert len(list(db.scalars(select(Fill)))) == 1  # CONTROLLED original BUY only.
            for value in incidents:
                state = value.payload
                age = (captured - datetime.fromisoformat(state["opened_at"])).total_seconds()
                assert age == 24 * 3600 + 15 * 60 and age > 24 * 3600
                inventory.append({"source": state["source"], "owner_id": str(value.id),
                    "opened_at": state["opened_at"], "age_seconds": age, "status": value.status,
                    "phase": state["phase"], "prior_nonce": state.get("invocation", state.get("active_token")),
                    "native": "UNKNOWN", "quantity": state.get("remaining", state.get("requested"))})
        record_property("WB3_narrow_controlled_aged_boot", canonical(receipt(case, real=False, on=on,
            before=before, after=after, result2=result2, summary=summary, orb_spawned=spawned,
            inventory=inventory)))
    finally:
        service.logger.removeHandler(caplog.handler)
        await finish_case(case)
