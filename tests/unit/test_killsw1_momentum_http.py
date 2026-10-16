"""Actual HTTP STOP -> controlled existing MOM owner -> durable cleanup chain."""

import asyncio
from decimal import Decimal
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select

from project_mai_tai.db.models import BotControlAttempt, BrokerAccount, Strategy, VirtualPosition
from project_mai_tai.oms import service as oms_module
from project_mai_tai.oms.bot_control_runtime import BotControlRuntime
from project_mai_tai.positions_read_receipt import PositionsReadReceiptWriter
from project_mai_tai.services.bot_control_routes import CSRF_COOKIE, ControlScope, build_control_router
from tests.unit.test_momlive1_exit_recovery import fresh
from tests.unit.test_momlive1_soft_exit_chase import exit_orders, prepared


async def real_controlled_sync(monkeypatch, now, service, account):
    """Use the real OMS receipt producer/writer, with only broker I/O controlled."""
    monkeypatch.setattr(oms_module, "utcnow", lambda: now[0])
    writer = PositionsReadReceiptWriter(service.session_factory)
    service._positions_read_receipt_writer = writer
    await service.sync_broker_positions(account_names=[account])
    batch = writer.queue.get_nowait()
    try:
        await asyncio.to_thread(writer._persist, [batch])
    finally:
        writer.queue.task_done()


def configure_live_control_scope(service, runtime):
    # The generic runtime fixture seeds the unused account in environment=test.
    # A live STOP deliberately rejects that mixed scope; configure BOTH fake
    # venue accounts as live explicitly instead of relaxing the real boundary.
    with service.session_factory.begin() as unit:
        for name in runtime.accounts:
            service.store.ensure_broker_account(unit, name=name,
                provider=service.settings.provider_for_account(name), environment="live")


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["schwab", "webull"])
@pytest.mark.parametrize("hour,session", [(12, "AM"), (14, "NORMAL"), (21, "PM")])
async def test_http_stop_uses_same_actor_waits_for_post_cancel_wire_receipt_and_never_sells_foreign(
    monkeypatch, provider, hour, session,
):
    now, broker, service, runtime, identity = await prepared(provider, hour=hour, send_exit=False)
    configure_live_control_scope(service, runtime)
    service.settings.bot_control_enabled = True
    control = BotControlRuntime(service.session_factory)
    service._bot_control_runtime = control
    with service.session_factory.begin() as unit:
        strategy = unit.scalar(select(Strategy).where(Strategy.code == "momentum_warn_live"))
        accounts = tuple(unit.scalars(select(BrokerAccount.id).where(BrokerAccount.name.in_(runtime.accounts))).all())
        account = unit.scalar(select(BrokerAccount.id).where(BrokerAccount.name == runtime.bindings[identity]))
        foreign = Strategy(id=uuid4(), code="manual-killsw1", name="Manual", execution_mode="live")
        unit.add(foreign)
        unit.flush()
        manual_id = uuid4()
        unit.add(VirtualPosition(id=manual_id, strategy_id=foreign.id, broker_account_id=account,
                                 symbol="SCNI", quantity=Decimal(100), average_price=Decimal(3)))
        strategy_id = strategy.id
    control.store.seed(strategy_id)
    app = FastAPI()
    app.include_router(build_control_router(control.store, lambda code: ControlScope(strategy_id, accounts)))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 1234)),
        base_url="https://project-mai-tai.live", headers={"X-Mai-Tai-Operator": "operator"},
    ) as browser:
        page = await browser.get("/bot-control/momentum_warn_live")
        assert page.status_code == 200
        fields = {"csrf_token": browser.cookies[CSRF_COOKIE], "request_id": str(uuid4()),
                  "command_id": str(uuid4()), "expected_generation": "0", "action": "STOP"}
        posted = await browser.post("/bot-control/momentum_warn_live", data=fields,
            headers={"Origin": "https://project-mai-tai.live", "Accept": "application/json"})
        assert posted.status_code == 200 and posted.json()["phase"] == "PENDING_CENSUS"
        await control.refresh()
        assert not runtime._entry_enabled()
        await runtime.actor.restore_before_serving()
        assert exit_orders(broker) == []  # Prior sync receipt is NOT post-release evidence.
        assert runtime.native.handles[identity].status == "terminal"
        with service.session_factory() as unit:
            protection = unit.scalar(select(BotControlAttempt).where(
                BotControlAttempt.operation == "CANCEL_PROTECTION"))
            assert protection is not None and protection.status == "TERMINAL"
        fresh(now, service, 1)
        await real_controlled_sync(monkeypatch, now, service, runtime.bindings[identity])
        runtime.actor._observations.clear()
        await runtime.actor._service_owned(identity)
        closes = exit_orders(broker)
        assert len(closes) == 1 and closes[0].quantity == Decimal(33)
        assert closes[0].broker_account_name == runtime.bindings[identity]
        assert closes[0].metadata["session"] == session
        assert closes[0].order_type == ("market" if session == "NORMAL" else "limit")
        # The controlled venue now reports the real own close execution. This
        # leaves the manual100 untouched; neither account is asserted flat.
        venue = broker.orders[closes[0].client_order_id]
        venue.status, venue.quantity, venue.price = "filled", Decimal(33), Decimal(3)
        fresh(now, service, 2)
        runtime.actor._observations.clear()
        await runtime.actor._service_owned(identity)
        assert runtime.book.episodes[identity].state == "closed"
        control.executor.register("momentum_warn_live", runtime._control_cleanup_progress)
        control.executor.schedule(control.states)
        await asyncio.gather(*control.executor.tasks.values())
        state, phase = control.store.describe(strategy_id)
        assert phase == "SETTLED" and state.desired_state == "STOP"
        assert state.active_command_id is None and not state.admission_open
        with service.session_factory() as unit:
            assert unit.get(VirtualPosition, manual_id).quantity == Decimal(100)
            attempts = unit.scalars(select(BotControlAttempt)).all()
            assert {row.operation for row in attempts} == {"CANCEL_PROTECTION", "SUBMIT_EXIT"}
            assert all(row.status == "TERMINAL" for row in attempts)
        fields.update(request_id=str(uuid4()), command_id=str(uuid4()), expected_generation="1", action="ON")
        resumed = await browser.post("/bot-control/momentum_warn_live", data=fields,
            headers={"Origin": "https://project-mai-tai.live", "Accept": "application/json"})
        assert resumed.status_code == 200 and resumed.json()["generation"] == 2
        await control.refresh()
        assert runtime._entry_enabled()
        assert len(exit_orders(broker)) == 1
    await control.executor.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["schwab", "webull"])
async def test_broker_refusal_keeps_stop_and_survives_restart_without_another_sell(monkeypatch, provider):
    from dataclasses import replace
    from project_mai_tai.db.models import SystemIncident
    from project_mai_tai.momentum_live.runtime import OmsLiveRuntime

    now, broker, service, runtime, identity = await prepared(provider, hour=14, send_exit=False)
    configure_live_control_scope(service, runtime)
    service.settings.bot_control_enabled = True
    control = BotControlRuntime(service.session_factory)
    service._bot_control_runtime = control
    with service.session_factory() as unit:
        strategy = unit.scalar(select(Strategy.id).where(Strategy.code == "momentum_warn_live"))
        accounts = tuple(unit.scalars(select(BrokerAccount.id).where(BrokerAccount.name.in_(runtime.accounts))).all())
    control.store.seed(strategy)
    control.store.request(strategy_id=strategy, command_id=uuid4(), request_id=uuid4(),
        expected_generation=0, action="STOP", account_ids=accounts,
        actor_reference="operator", reason="controlled refusal")
    await control.refresh()
    await runtime.actor.restore_before_serving()
    assert exit_orders(broker) == []
    fresh(now, service, 1)
    await real_controlled_sync(monkeypatch, now, service, runtime.bindings[identity])
    original = broker.submit_order

    async def refuse(request):
        reports = await original(request)
        if request.metadata.get("exit_reason") == "KILLSW1_STOP":
            broker.orders[request.client_order_id].status = "rejected"
            return [replace(report, event_type="rejected", reason="shares reserved") for report in reports]
        return reports

    broker.submit_order = refuse
    runtime.actor._observations.clear()
    await runtime.actor._service_owned(identity)
    assert len(exit_orders(broker)) == 1
    assert runtime.runtime[identity]["killsw1_broker_refused"] == exit_orders(broker)[0].client_order_id
    with service.session_factory() as unit:
        incidents = unit.scalars(select(SystemIncident).where(
            SystemIncident.payload["source"].as_string() == "KILLSW1")).all()
        assert len(incidents) == 1 and incidents[0].status == "open"
    await runtime.close()
    restarted = OmsLiveRuntime(service, clock=lambda: now[0], identity="321:654")
    try:
        await restarted.start()
        fresh(now, service, 10)
        await real_controlled_sync(monkeypatch, now, service, restarted.bindings[identity])
        restarted.actor._observations.clear()
        await restarted.actor._service_owned(identity)
        assert len(exit_orders(broker)) == 1
        assert not control.store.hydrate(strategy).admission_open
        assert restarted.runtime[identity]["killsw1_broker_refused"]
    finally:
        await restarted.close()
        await control.executor.close()
