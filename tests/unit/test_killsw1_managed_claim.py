"""Exercise the real claimed emit's admission/commit boundary, with a paused sender."""

import asyncio
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from sqlalchemy import event

from project_mai_tai.db.models import (
    AccountPosition, BotControlCommand, BrokerOrder, Fill, OmsManagedPosition,
    Strategy, VirtualPosition,
)
from project_mai_tai.oms import bot_control_atr as atr
from project_mai_tai.oms.service import OmsRiskService, _ManagedSellEvents
from tests.unit import test_reserve1 as reserve_module
from tests.unit.test_reserve1 import ACCT, harness


async def postboot_apus_stop(monkeypatch):
    def boot_before_exposure(settings, **kwargs):
        settings = settings.model_copy(update={
            "bot_control_enabled": True,
            "strategy_schwab_1m_v2_country_route_enabled": True,
        })
        service = OmsRiskService(settings, **kwargs)
        # The real reserve fixture adds its APUS exposure AFTER construction.
        asyncio.run(service._bot_control_runtime.refresh())
        assert service._bot_control_runtime.boot_managed_ids == frozenset()
        return service

    with monkeypatch.context() as construction:
        construction.setattr(reserve_module, "OmsRiskService", boot_before_exposure)
        service, sessions, broker, symbol, now = await asyncio.to_thread(harness, monkeypatch)

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return now.astimezone(tz) if tz is not None else now.replace(tzinfo=None)

    monkeypatch.setattr(atr, "datetime", Clock)
    with sessions.begin() as unit:
        row = service.store.get_open_managed_position(unit, broker_account_name=ACCT, symbol=symbol)
        entry = unit.get(BrokerOrder, row.entry_order_id)
        strategy = unit.get(Strategy, entry.strategy_id)
        # Explicit live test ownership for the retained APUS parent/quantity;
        # these fixture executions are controlled, not additional recorded fills.
        strategy.execution_mode = "live"
        entry.submitted_at = row.entry_time
        assert entry.quantity == row.original_quantity == row.current_quantity
        unit.add_all([
            Fill(order_id=entry.id, strategy_id=entry.strategy_id,
                 broker_account_id=entry.broker_account_id, symbol=symbol, side="buy",
                 broker_fill_id=f"{entry.broker_order_id}:controlled-owned-entry",
                 quantity=entry.quantity, price=row.entry_price, filled_at=row.entry_time),
            VirtualPosition(strategy_id=entry.strategy_id, broker_account_id=entry.broker_account_id,
                            symbol=symbol, quantity=entry.quantity, average_price=row.entry_price),
            AccountPosition(broker_account_id=entry.broker_account_id, symbol=symbol,
                            quantity=entry.quantity, average_price=row.entry_price),
        ])
        strategy_id, account_id = entry.strategy_id, entry.broker_account_id
        managed_id = str(row.id)
    runtime = service._bot_control_runtime
    assert managed_id not in runtime.boot_managed_ids
    await asyncio.to_thread(runtime.store.seed, strategy_id)
    receipt = await asyncio.to_thread(runtime.store.request, strategy_id=strategy_id,
        command_id=uuid4(), request_id=uuid4(), expected_generation=0, action="STOP",
        account_ids=(account_id,), actor_reference="controlled-claim", reason="controlled APUS STOP")
    with sessions.begin() as unit:
        unit.get(BotControlCommand, receipt.command_id).accepted_at = now
    await runtime.refresh()
    lease = await asyncio.to_thread(runtime.store.claim_stop, receipt.command_id,
                                   "controlled-claim-owner", now=now)
    assert lease is not None and lease.scope_complete and lease.account_ids == (account_id,)
    assert runtime.boot_managed_ids == frozenset()
    return service, sessions, broker, symbol, now, lease, runtime.journal


@pytest.mark.asyncio
@pytest.mark.parametrize("first_reason,second_reason", [
    ("oms_v2_managed_exit:HARD_STOP", "KILLSW1_STOP"),
    ("KILLSW1_STOP", "oms_v2_managed_exit:HARD_STOP"),
])
async def test_existing_managed_owner_claim_is_shared_by_normal_and_stop_through_commit(
    monkeypatch, first_reason, second_reason,
):
    service, sessions, _, symbol, _, lease, journal = await postboot_apus_stop(monkeypatch)
    snapshot = await service._run_db(lambda unit: service._read_v2_managed_snapshot(unit, ACCT, symbol, True), commit=False)
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def paused_sender(unit, row, **kwargs):
        assert str(row.id) == snapshot.managed_row_id
        assert kwargs["quantity"] == snapshot.current_quantity
        context = atr.current_stop(service, row.id)
        if kwargs["reason"] == "KILLSW1_STOP":
            assert context is not None and context.lease == lease and context.journal is journal
        else:
            assert context is None
        calls.append((row.id, kwargs["reason"]))
        entered.set()
        await release.wait()
        if context is not None:
            context.phase = "EXIT_WORKING"
        return _ManagedSellEvents(submitted_quantity=kwargs["quantity"])

    monkeypatch.setattr(service, "_emit_v2_managed_sell", paused_sender)

    claimed_emit = service._emit_v2_exit_on_loop_claimed
    stop_ready, allow_stop = asyncio.Event(), asyncio.Event()
    stop_result = asyncio.get_running_loop().create_future()

    async def prepared_stop(*args, **kwargs):
        context = atr.current_stop(service, snapshot.managed_row_id)
        assert context is not None and context.lease == lease and context.journal is journal
        stop_ready.set()
        await allow_stop.wait()
        result = await claimed_emit(*args, **kwargs)
        stop_result.set_result(result)
        return result

    monkeypatch.setattr(service, "_emit_v2_exit_on_loop_claimed", prepared_stop)
    stop_driver = asyncio.create_task(atr.AtrStopDriver(service)(lease, journal))
    try:
        # Off-loop STOP inventory is prepared BEFORE measuring held-claim admission.
        # Both competing calls below enter the real claimed owner, not a fake context.
        await asyncio.wait_for(stop_ready.wait(), 2)
    except BaseException:
        stop_driver.cancel()
        await asyncio.gather(stop_driver, return_exceptions=True)
        raise

    async def invoke(reason):
        if reason == "KILLSW1_STOP":
            allow_stop.set()
            return await stop_result
        return await claimed_emit(
            ACCT, symbol, service._hydrate_v2_position(snapshot), snapshot.entry_price,
            kind="HARD", reference_price=8.0, reason=reason, bid=8.0,
            close_on_fill=True, expected_managed_row_id=snapshot.managed_row_id,
        )

    first = asyncio.create_task(invoke(first_reason))
    statements = []
    def statement(*args):
        statements.append(1)
    event.listen(sessions.kw["bind"], "before_cursor_execute", statement)
    try:
        await asyncio.wait_for(entered.wait(), 2)
        key = (ACCT, symbol, snapshot.managed_row_id)
        assert key in service._control_managed_exit_claims
        before = len(statements)
        assert await asyncio.wait_for(invoke(second_reason), 1) == "refused"
        assert len(statements) == before  # A held claim cannot cause a DB read on the tick.
        assert not first.done() and len(calls) == 1
    finally:
        release.set()
        allow_stop.set()
        await first
        await stop_driver
        event.remove(sessions.kw["bind"], "before_cursor_execute", statement)
    assert service._control_managed_exit_claims == {}
    assert service._control_managed_exit_symbol_owners == {}
    with sessions() as unit:
        assert service.store.get_open_managed_position(unit, broker_account_name=ACCT, symbol=symbol) is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("route_enabled", [False, True], ids=["flag-OFF", "contextless-ON"])
async def test_direct_stop_without_full_route_and_owned_context_has_no_io(monkeypatch, route_enabled):
    service, sessions, broker, symbol, _, lease, journal = await postboot_apus_stop(monkeypatch)
    snapshot = await service._run_db(
        lambda unit: service._read_v2_managed_snapshot(unit, ACCT, symbol, True), commit=False)
    statements, sent = [], []

    def statement(*args):
        statements.append(1)

    async def forbidden_sender(*args, **kwargs):
        sent.append(1)
        pytest.fail("contextless/flag-OFF STOP reached sender")

    monkeypatch.setattr(service, "_emit_v2_managed_sell", forbidden_sender)
    assert atr.current_stop(service) is None
    claimed_emit = service._emit_v2_exit_on_loop_claimed
    driver_task = None
    ready, proceed = asyncio.Event(), asyncio.Event()
    result = asyncio.get_running_loop().create_future()

    async def flag_off_at_owned_boundary(*args, **kwargs):
        context = atr.current_stop(service, snapshot.managed_row_id)
        assert context is not None and context.lease == lease and context.journal is journal
        ready.set()
        await proceed.wait()
        value = await claimed_emit(*args, **kwargs)
        result.set_result(value)
        return value

    if not route_enabled:
        monkeypatch.setattr(service, "_emit_v2_exit_on_loop_claimed", flag_off_at_owned_boundary)
        driver_task = asyncio.create_task(atr.AtrStopDriver(service)(lease, journal))
        try:
            await asyncio.wait_for(ready.wait(), 2)
        except BaseException:
            driver_task.cancel()
            await asyncio.gather(driver_task, return_exceptions=True)
            raise
    service.settings.strategy_schwab_1m_v2_country_route_enabled = route_enabled
    event.listen(sessions.kw["bind"], "before_cursor_execute", statement)
    try:
        if driver_task is not None:
            proceed.set()
            assert await asyncio.wait_for(result, 2) == "refused"
            await driver_task
        else:
            assert await claimed_emit(
                ACCT, symbol, service._hydrate_v2_position(snapshot), snapshot.entry_price,
                kind="HARD", reference_price=8.0, reason="KILLSW1_STOP", bid=8.0,
                close_on_fill=True, expected_managed_row_id=snapshot.managed_row_id,
            ) == "refused"
    finally:
        if driver_task is not None:
            if not driver_task.done():
                driver_task.cancel()
            await asyncio.gather(driver_task, return_exceptions=True)
        event.remove(sessions.kw["bind"], "before_cursor_execute", statement)
    assert not statements and not sent and not broker.calls and not broker.requests
    assert service.__dict__.get("_control_managed_exit_claims", {}) == {}
    with sessions() as unit:
        row = unit.get(OmsManagedPosition, UUID(snapshot.managed_row_id))
        assert row is not None and row.current_quantity == snapshot.current_quantity


@pytest.mark.asyncio
async def test_managed_sender_exception_releases_only_its_own_claim(monkeypatch):
    service, _, _, symbol, _ = harness(monkeypatch)
    service.settings.bot_control_enabled = True
    snapshot = await service._run_db(lambda unit: service._read_v2_managed_snapshot(unit, ACCT, symbol, True), commit=False)

    async def failed_sender(*args, **kwargs):
        raise OSError("controlled sender failure")

    monkeypatch.setattr(service, "_emit_v2_managed_sell", failed_sender)
    assert await service._emit_v2_exit_on_loop_claimed(
        ACCT, symbol, service._hydrate_v2_position(snapshot), snapshot.entry_price,
        kind="HARD", reference_price=8.0, reason="oms_v2_managed_exit:HARD_STOP", bid=8.0,
        close_on_fill=True, expected_managed_row_id=snapshot.managed_row_id,
    ) == "refused"
    assert service._control_managed_exit_claims == {}


@pytest.mark.asyncio
async def test_contended_admission_mutex_refuses_without_wait_or_sql(monkeypatch):
    service, _, _, symbol, _ = harness(monkeypatch)
    service.settings.bot_control_enabled = True
    acquired = []
    class ContendedLock:
        def acquire(self, *, blocking):
            acquired.append(blocking)
            assert blocking is False
            return False
        def release(self):
            pytest.fail("a mutex we did not acquire must not be released")
        def __enter__(self):
            pytest.fail("claim admission must never queue on a mutex")
        def __exit__(self, *args):
            pytest.fail("claim admission never entered the contended mutex")
    service._control_managed_exit_claim_lock = ContendedLock()
    def no_database():
        pytest.fail("contended claim reached SQL")
    monkeypatch.setattr(service, "session_factory", no_database)
    assert await service._emit_v2_exit_on_loop_claimed(
        ACCT, symbol, None, 8.0, kind="HARD", reference_price=8.0,
        reason="oms_v2_managed_exit:HARD_STOP", bid=8.0, close_on_fill=True,
    ) == "refused"
    assert acquired == [False]


@pytest.mark.asyncio
async def test_second_admission_mutex_busy_after_row_read_refuses_without_sender_or_claim(monkeypatch):
    service, sessions, _, symbol, _ = harness(monkeypatch)
    service.settings.bot_control_enabled = True
    snapshot = await service._run_db(
        lambda unit: service._read_v2_managed_snapshot(unit, ACCT, symbol, True), commit=False,
    )
    acquired, released, statements = [], [], []

    class SecondAcquireBusy:
        def acquire(self, *, blocking):
            assert blocking is False
            acquired.append(blocking)
            return len(acquired) == 1

        def release(self):
            assert len(acquired) == 1
            released.append(1)

        def __enter__(self):
            pytest.fail("second admission must not queue on a mutex")

        def __exit__(self, *args):
            pytest.fail("busy second admission never owns the mutex")

    service._control_managed_exit_claim_lock = SecondAcquireBusy()
    other_owner = object()
    other_key = (ACCT, "UNRELATED", "other-episode")
    service._control_managed_exit_claims = {other_key: other_owner}
    service._control_managed_exit_symbol_owners = {other_key[:2]: other_owner}

    async def forbidden_sender(*args, **kwargs):
        pytest.fail("busy second admission reached sender")

    def statement(*args):
        statements.append(1)

    monkeypatch.setattr(service, "_emit_v2_managed_sell", forbidden_sender)
    event.listen(sessions.kw["bind"], "before_cursor_execute", statement)
    try:
        assert await service._emit_v2_exit_on_loop_claimed(
            ACCT, symbol, service._hydrate_v2_position(snapshot), snapshot.entry_price,
            kind="HARD", reference_price=8.0, reason="oms_v2_managed_exit:HARD_STOP",
            bid=8.0, close_on_fill=True, expected_managed_row_id=snapshot.managed_row_id,
        ) == "refused"
    finally:
        event.remove(sessions.kw["bind"], "before_cursor_execute", statement)
    assert statements  # The existing episode row read precedes the second admission.
    assert acquired == [False, False]
    assert released == [1]  # Only the successful outer precheck owns a release.
    assert service._control_managed_exit_claims == {other_key: other_owner}
    assert service._control_managed_exit_symbol_owners == {other_key[:2]: other_owner}


@pytest.mark.asyncio
async def test_flag_off_claim_wrapper_does_not_touch_mutex():
    from types import SimpleNamespace
    from project_mai_tai.oms.service import _control_managed_exit_owner
    class ForbiddenLock:
        def acquire(self, **kwargs):
            pytest.fail("flag-OFF path touched claim admission")
        def __enter__(self):
            pytest.fail("flag-OFF path touched claim mutex")
    service = SimpleNamespace(settings=SimpleNamespace(bot_control_enabled=False),
                              _control_managed_exit_claim_lock=ForbiddenLock())
    called = []
    @_control_managed_exit_owner
    async def original(self, account, symbol):
        called.append((account, symbol))
        return "original-off"
    assert await original(service, ACCT, "ABC") == "original-off"
    assert called == [(ACCT, "ABC")]
