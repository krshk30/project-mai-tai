"""HTTP STOP through the real lease/journal/executor; only venue I/O is controlled."""

import asyncio
from datetime import datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select

from project_mai_tai.broker_adapters.protocols import BrokerPositionSnapshot, ExecutionReport
from project_mai_tai.db.models import (
    BotControlAttempt,
    BrokerAccount,
    BrokerOrder,
    Fill,
    Strategy,
    TradeIntent,
    VirtualPosition,
)
from project_mai_tai.oms import bot_control, bot_control_executor, orb_schwab_eod
from project_mai_tai.oms import service as oms_module
from project_mai_tai.oms.bot_control_orb import drive_orb_stop
from project_mai_tai.oms.bot_control_runtime import BotControlRuntime, ControlledBrokerAdapter
from project_mai_tai.positions_read_receipt import PositionsReadReceiptWriter
from project_mai_tai.services.bot_control_routes import (
    CSRF_COOKIE,
    ControlScope,
    build_control_router,
)
from tests.unit.test_orbroute1_oms_chain import SCHWAB, seed_filled_webull, setup as setup


@pytest.mark.parametrize("provider", ["schwab", "webull"])
@pytest.mark.parametrize("hour,session", [(12, "AM"), (14, "NORMAL"), (21, "PM")])
def test_http_orb_stop_later_fill_settles_real_journal_without_selling_manual_shares(
    setup,
    monkeypatch,
    provider,
    hour,
    session,
):
    service, sessions, broker = setup
    service.settings.bot_control_enabled = True
    runtime = BotControlRuntime(sessions)
    service._bot_control_runtime = runtime
    # Capture the real empty boot inventory BEFORE this controlled position opens.
    asyncio.run(runtime.refresh())
    entry_id, filled_at = seed_filled_webull(setup, monkeypatch)
    if provider == "webull":
        from project_mai_tai.oms.orb_country import reconcile_one

        asyncio.run(reconcile_one(service, entry_id))
    # PM STOP covers a held prior-day ORB lot, never a pre-market ORB entry.
    current = [(filled_at + timedelta(days=1)).replace(hour=hour, minute=0)]

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return current[0].astimezone(tz) if tz else current[0].replace(tzinfo=None)

    monkeypatch.setattr(bot_control, "datetime", Clock)
    monkeypatch.setattr(bot_control_executor, "datetime", Clock)
    monkeypatch.setattr("project_mai_tai.oms.orb_country.datetime", Clock)
    monkeypatch.setattr(oms_module, "utcnow", lambda: current[0])
    with sessions.begin() as unit:
        entry = unit.get(BrokerOrder, entry_id)
        strategy = unit.get(Strategy, entry.strategy_id)
        account = unit.get(BrokerAccount, entry.broker_account_id)
        strategy.execution_mode, account.environment = "live", "production"
        unit.get(TradeIntent, entry.intent_id).status = "filled"
        if provider == "schwab":
            account.name, account.provider = SCHWAB, "schwab"
            entry.payload = {**entry.payload, "orb_country_broker": "schwab"}
        manual = Strategy(
            id=uuid4(), code="controlled_manual", name="Manual", execution_mode="live"
        )
        unit.add(manual)
        unit.flush()
        manual_id = uuid4()
        unit.add(
            VirtualPosition(
                id=manual_id,
                strategy_id=manual.id,
                broker_account_id=account.id,
                symbol="ABC",
                quantity=Decimal(100),
                average_price=Decimal(5),
            )
        )
        strategy_id, account_id, account_name = strategy.id, account.id, account.name

    runtime.store.seed(strategy_id)
    writer = PositionsReadReceiptWriter(sessions)
    service._positions_read_receipt_writer = writer
    state = {"exit": None, "filled": False, "executed_at": None}
    original_read, original_submit = broker.fetch_order_update, broker.submit_order

    async def read(request):
        if request.side == "buy":
            return ExecutionReport(
                "filled",
                request.client_order_id,
                broker_order_id="entry-native",
                broker_fill_id="entry-execution",
                symbol="ABC",
                side="buy",
                intent_type="open",
                quantity=Decimal(10),
                filled_quantity=Decimal(10),
                fill_price=Decimal("5.53"),
                reported_at=filled_at,
                origin="broker",
            )
        if state["exit"] is not None and request.client_order_id == state["exit"].client_order_id:
            return ExecutionReport(
                "filled" if state["filled"] else "accepted",
                request.client_order_id,
                broker_order_id="controlled-own-exit",
                broker_fill_id="controlled-own-fill" if state["filled"] else None,
                symbol="ABC",
                side="sell",
                intent_type="close",
                quantity=Decimal(10),
                filled_quantity=Decimal(10) if state["filled"] else Decimal(0),
                fill_price=Decimal(6) if state["filled"] else None,
                reported_at=state["executed_at"] or current[0],
                origin="broker",
            )
        return await original_read(request)

    async def submit(request):
        if request.reason == orb_schwab_eod.STOP_REASON:
            with sessions() as unit:
                audit = unit.scalar(
                    select(BotControlAttempt).where(BotControlAttempt.operation == "SUBMIT_EXIT")
                )
                assert audit.status == "MAY_HAVE_SENT" and audit.dispatch_intent_at is not None
            assert state["exit"] is None  # A recovery cannot send a second order.
            state["exit"] = request
            broker.sent.append(request)
            return [
                ExecutionReport(
                    "accepted",
                    request.client_order_id,
                    broker_order_id="controlled-own-exit",
                    symbol="ABC",
                    side="sell",
                    intent_type="close",
                    quantity=request.quantity,
                    reported_at=current[0],
                    origin="broker",
                )
            ]
        return await original_submit(request)

    async def release(account, parent):
        assert account == account_name and parent == "entry-native"
        with sessions() as unit:
            audit = unit.scalar(
                select(BotControlAttempt).where(BotControlAttempt.binding_kind == "orb_native_tree")
            )
            assert audit.status == "MAY_HAVE_SENT"
        broker.cancels.append((account, parent))
        return "released"

    async def positions(account):
        assert account == account_name
        return [
            BrokerPositionSnapshot(
                account_name,
                "ABC",
                Decimal(100 if state["filled"] else 110),
                Decimal("5.53"),
                as_of=filled_at,
            )
        ]

    broker.fetch_order_update, broker.submit_order = read, submit
    broker.release_native_oco_for_close = release
    broker.list_account_positions = positions
    broker.positions_wire_read_age_seconds = lambda _account: 0.0
    broker.fetch_oco_exit_fill = lambda *_args, **_kwargs: asyncio.sleep(0, result=None)
    for suffix in ("T", "S"):
        broker.children[suffix] = ExecutionReport(
            "cancelled",
            "rebound-exact-owned-child",
            broker_order_id=f"native-{suffix}",
            symbol="ABC",
            side="sell",
            intent_type="close",
            quantity=Decimal(10),
            reported_at=filled_at,
            origin="broker",
        )
    service.broker_adapter = ControlledBrokerAdapter(broker, runtime)
    service._latest_quotes_by_symbol["ABC"] = {"bid": "5.647", "received_at": current[0]}
    actual_sync = service.sync_broker_positions

    async def sync_receipt(*, account_names):
        current[0] += timedelta(milliseconds=1)
        result = await actual_sync(account_names=account_names)
        while not writer.queue.empty():
            batch = writer.queue.get_nowait()
            try:
                await asyncio.to_thread(writer._persist, [batch])
            finally:
                writer.queue.task_done()
        return result

    # Keep the real OMS producer and writer; drain its actual test queue explicitly.
    service.sync_broker_positions = sync_receipt
    runtime.executor.register(
        "orb_schwab",
        lambda lease, journal: drive_orb_stop(service, lease, journal, clock=lambda: current[0]),
    )
    app = FastAPI()
    app.include_router(
        build_control_router(runtime.store, lambda _code: ControlScope(strategy_id, (account_id,)))
    )

    async def exercise():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 1234)),
            base_url="https://project-mai-tai.live",
            headers={"X-Mai-Tai-Operator": "operator"},
        ) as browser:
            assert (await browser.get("/bot-control/orb_schwab")).status_code == 200
            fields = dict(
                csrf_token=browser.cookies[CSRF_COOKIE],
                command_id=str(uuid4()),
                request_id=str(uuid4()),
                expected_generation="0",
                action="STOP",
            )
            headers = {"Origin": "https://project-mai-tai.live", "Accept": "application/json"}
            posted = await browser.post("/bot-control/orb_schwab", data=fields, headers=headers)
            assert posted.status_code == 200 and posted.json()["phase"] == "PENDING_CENSUS"
            await runtime.refresh()
            assert not runtime.admission_open("orb_schwab")
            runtime.executor.schedule(runtime.states)
            await asyncio.gather(*runtime.executor.tasks.values())
            assert runtime.executor.errors == {}
            request = state["exit"]
            assert request is not None and request.quantity == 10
            assert (
                request.broker_account_name == account_name
                and request.metadata["session"] == session
            )
            assert request.order_type == ("market" if session == "NORMAL" else "limit")
            with sessions() as unit:
                assert unit.get(VirtualPosition, manual_id).quantity == 100
                assert (
                    unit.scalar(
                        select(BotControlAttempt.status).where(
                            BotControlAttempt.operation == "SUBMIT_EXIT"
                        )
                    )
                    == "WORKING"
                )
            state["filled"], state["executed_at"] = True, current[0]
            current[0] += timedelta(seconds=1)
            await runtime.refresh()
            runtime.executor.schedule(runtime.states)
            await asyncio.gather(*runtime.executor.tasks.values())
            assert runtime.executor.errors == {}
            latch, phase = runtime.store.describe(strategy_id)
            assert phase == "SETTLED" and latch.desired_state == "STOP" and not latch.admission_open
            with sessions() as unit:
                assert unit.get(VirtualPosition, manual_id).quantity == 100
                own = unit.scalar(
                    select(VirtualPosition).where(VirtualPosition.strategy_id == strategy_id)
                )
                assert own.quantity == 0 and own.realized_pnl == Decimal("4.70")
                fills = unit.scalars(
                    select(Fill).where(Fill.side == "sell", Fill.strategy_id == strategy_id)
                ).all()
                assert len(fills) == 1 and fills[0].quantity == 10
                assert all(
                    row.status == "TERMINAL"
                    for row in unit.scalars(select(BotControlAttempt)).all()
                )
            assert (
                len(
                    [
                        request
                        for request in broker.sent
                        if request.reason == orb_schwab_eod.STOP_REASON
                    ]
                )
                == 1
            )
            fields.update(
                command_id=str(uuid4()),
                request_id=str(uuid4()),
                expected_generation="1",
                action="ON",
            )
            assert (
                await browser.post("/bot-control/orb_schwab", data=fields, headers=headers)
            ).status_code == 200
            assert runtime.store.hydrate(strategy_id).generation == 2
            # A completed prior STOP is history, not an unresolved exit of STOP2.
            fields.update(
                command_id=str(uuid4()),
                request_id=str(uuid4()),
                expected_generation="2",
                action="STOP",
            )
            again = await browser.post("/bot-control/orb_schwab", data=fields, headers=headers)
            assert again.status_code == 200 and again.json()["generation"] == 3
            await runtime.refresh()
            runtime.executor.schedule(runtime.states)
            await asyncio.gather(*runtime.executor.tasks.values())
            assert runtime.executor.errors == {}
            again_state, again_phase = runtime.store.describe(strategy_id)
            assert again_phase == "SETTLED" and again_state.desired_state == "STOP"
            assert (
                len(
                    [
                        request
                        for request in broker.sent
                        if request.reason == orb_schwab_eod.STOP_REASON
                    ]
                )
                == 1
            )
        await runtime.executor.close()

    asyncio.run(exercise())
