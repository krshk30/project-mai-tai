"""SOURCE UNRUN: exact named WB3 preparation task, real O14/cap/final path.

Main alone composes/reviews/fences before execution. Existing positive and all
previous 39 UNRUN function bodies are unchanged. Only provider data/latency and
observation wrappers change; no application permission function returns success.
"""
import asyncio
from dataclasses import replace
from threading import Event, local

import pytest
from sqlalchemy import select

from project_mai_tai.db.models import BrokerOrder, BrokerOrderEvent, Fill, OmsManagedPosition, VirtualPosition
from project_mai_tai.events import TradeIntentEvent, TradeIntentPayload
from project_mai_tai.oms import wbexit2, wbexit3_vp_zero as vp, webull_attach_riskoff as wb
from project_mai_tai.oms.managed_sell_prewire import prepare_managed_sell
from tests.unit.test_wbattach1_riskoff import make_case as make_case, closes
from tests.unit.test_wbexit2_route import close_case
from tests.unit.test_wbexit3_obligation import obligations
from tests.unit.test_wbexit3_same_day_vp_zero import same_day_case


def matching_event(request):
    return TradeIntentEvent(source_service="CONTROLLED-named-prepare", payload=TradeIntentPayload(
        strategy_code=request.strategy_code, broker_account_name=request.broker_account_name,
        symbol=request.symbol, side=request.side, quantity=request.quantity,
        intent_type=request.intent_type, reason=request.reason, metadata=dict(request.metadata)))


def actual_lease(service):
    call = wb._ATR_CALL.get()
    lease = call.__dict__.get(vp._PREPARE_LEASE) if call is not None else None
    return call, lease


@pytest.mark.asyncio
async def test_exact_joined_prepare_child_captures_real_commit_before_revocation(make_case, monkeypatch):
    case = await same_day_case(make_case, monkeypatch)
    service, _, broker, _, _ = case
    actual_db, actual_revoke = service._run_db, vp.revoke_prepare_permit
    captures, revoked = [], []
    async def observed_db(unit, *args, **kwargs):
        call, lease = actual_lease(service)
        if unit.__name__ != "prepare" or lease is None:
            return await actual_db(unit, *args, **kwargs)
        permit, child = lease["permit"], asyncio.current_task()
        assert isinstance(permit, vp.PreparePermit) and permit.purpose == "WB3_PREPARE_ONLY"
        assert lease["used"] and lease["child"] is child and lease["nonce"] is permit.nonce
        assert child is call.wb2_pending and child.get_coro() is permit.work
        assert child is not call.task and permit.owner_task is call.task
        assert permit.atr_nonce == call.call_id and permit.token == wbexit2.wbexit3._ATTEMPT.get()["token"]
        assert permit.managed_id == call.managed_id
        intent_id = await actual_db(unit, *args, **kwargs)  # Real session/locked cap/reservation/commit.
        assert intent_id is not None and call.wb2_prepared is None
        assert call.__dict__[vp._PREPARE_LEASE] is lease and not lease["revoked"]
        captures.append((call, lease, intent_id))
        return intent_id
    def observed_revoke(actual_service, call, permit):
        assert captures and actual_service is service
        _, lease, intent_id = captures[-1]
        assert call.wb2_pending is None and lease["child"].done()
        assert call.wb2_prepared[0] == intent_id  # Existing joined completed callback already captured creator.
        actual_revoke(actual_service, call, permit)
        assert lease["revoked"] and vp._PREPARE_LEASE not in call.__dict__
        revoked.append(permit)
    monkeypatch.setattr(service, "_run_db", observed_db)
    monkeypatch.setattr(vp, "revoke_prepare_permit", observed_revoke)
    await close_case(case, "CW_TARGET")
    assert len(captures) == len(revoked) == 1
    assert [request.quantity for request in closes(broker)] == [27] and broker.total == 1000
    assert obligations(case)[0]["phase"] == "RESOLVED"


@pytest.mark.asyncio
@pytest.mark.parametrize("duplicate", ["foreign_task", "same_child_reentry"])
async def test_live_prepare_role_is_not_a_second_preparation_permission(make_case, monkeypatch, duplicate):
    case = await same_day_case(make_case, monkeypatch)
    service, _, broker, _, clock = case
    actual_db = service._run_db
    in_duplicate, unauthorized_db, checked = [False], [], []
    async def observed_db(unit, *args, **kwargs):
        if in_duplicate[0]:
            unauthorized_db.append(unit.__name__)
            return await actual_db(unit, *args, **kwargs)
        call, lease = actual_lease(service)
        if unit.__name__ == "prepare" and lease is not None:
            permit = lease["permit"]
            assert lease["used"] and lease["child"] is asyncio.current_task()
            before = dict(permit.request.metadata)
            async def duplicate_prepare():
                in_duplicate[0] = True
                try:
                    with pytest.raises(ValueError, match="wbattach1_live_owner_call_unknown"):
                        await prepare_managed_sell(service, event=matching_event(permit.request),
                            request=permit.request, managed_row_id=permit.managed_id, now=clock[0])
                finally:
                    in_duplicate[0] = False
            if duplicate == "foreign_task":
                await asyncio.create_task(duplicate_prepare())
            else:
                await duplicate_prepare()  # Same pending task/work, but the role is already consumed.
            assert unauthorized_db == [] and permit.request.metadata == before
            checked.append(duplicate)
        return await actual_db(unit, *args, **kwargs)
    monkeypatch.setattr(service, "_run_db", observed_db)
    await close_case(case, "CW_TARGET")
    assert checked == [duplicate] and unauthorized_db == []
    assert [request.quantity for request in closes(broker)] == [27] and broker.total == 1000


@pytest.mark.asyncio
async def test_second_task_same_request_id_after_revoke_is_refused_before_DB(make_case, monkeypatch):
    case = await same_day_case(make_case, monkeypatch)
    service, _, broker, _, clock = case
    actual_validate, actual_db = wbexit2.validate_before_wire, service._run_db
    release, late_tasks, unauthorized_db = asyncio.Event(), [], []
    in_late = [False]
    async def observed_db(unit, *args, **kwargs):
        if in_late[0]:
            unauthorized_db.append(unit.__name__)
        return await actual_db(unit, *args, **kwargs)
    async def observed_validate(actual_service, call, request):
        assert vp._PREPARE_LEASE not in call.__dict__ and call.wb2_prepared is not None
        repeated = replace(request, metadata=dict(request.metadata))
        assert repeated.client_order_id == request.client_order_id and repeated is not request
        async def late_replay():
            await release.wait()  # Inherited context survives parent completion; permit does not.
            in_late[0] = True
            try:
                with pytest.raises(ValueError, match="wbattach1_live_owner_call_unknown"):
                    await prepare_managed_sell(service, event=matching_event(repeated), request=repeated,
                        managed_row_id=call.managed_id, now=clock[0])
            finally:
                in_late[0] = False
        late_tasks.append(asyncio.create_task(late_replay()))
        return await actual_validate(actual_service, call, request)
    monkeypatch.setattr(service, "_run_db", observed_db)
    monkeypatch.setattr(wbexit2, "validate_before_wire", observed_validate)
    try:
        await close_case(case, "CW_TARGET")
        assert len(late_tasks) == 1 and not late_tasks[0].done()
        assert [request.quantity for request in closes(broker)] == [27]
        release.set()
        await asyncio.wait_for(late_tasks[0], timeout=3)
        assert unauthorized_db == [] and broker.total == 1000
    finally:
        release.set()
        if late_tasks:
            await asyncio.gather(*late_tasks, return_exceptions=True)


@pytest.mark.asyncio
async def test_request_metadata_change_after_admission_cannot_enter_prepare_DB(make_case, monkeypatch):
    case = await same_day_case(make_case, monkeypatch)
    service, _, broker, _, _ = case
    actual_join, actual_db = wbexit2.joined, service._run_db
    altered, prepare_db = [], []
    async def observed_join(actual_service, call, work, **kwargs):
        lease = call.__dict__.get(vp._PREPARE_LEASE)
        if lease is not None and lease["permit"].work is work:
            assert not lease["used"] and not altered
            lease["permit"].request.metadata["reference_price"] = "999.00"
            altered.append(lease)
        return await actual_join(actual_service, call, work, **kwargs)
    async def observed_db(unit, *args, **kwargs):
        if unit.__name__ == "prepare":
            prepare_db.append(unit.__name__)
        return await actual_db(unit, *args, **kwargs)
    monkeypatch.setattr(wbexit2, "joined", observed_join)
    monkeypatch.setattr(service, "_run_db", observed_db)
    await close_case(case, "CW_TARGET")
    assert len(altered) == 1 and altered[0]["revoked"] and prepare_db == []
    assert not closes(broker) and obligations(case)[0]["phase"] == "RETRY"
    with case[1]() as db:
        assert db.scalar(select(VirtualPosition.quantity)) == 0
        assert db.scalar(select(OmsManagedPosition)).current_quantity == 27
        assert len(list(db.scalars(select(Fill)))) == 1


@pytest.mark.asyncio
async def test_recovered_prepare_commit_cancellation_retains_exact_role_until_thread_exit(make_case, monkeypatch):
    case = await same_day_case(make_case, monkeypatch)
    service, factory, broker, _, _ = case
    actual_db, actual_revoke = service._run_db, vp.revoke_prepare_permit
    entered, release, physically_done = Event(), Event(), Event()
    thread_state = local()
    state, revoked = {}, []

    class ObservedSession:
        def __enter__(self):
            self.actual = factory()
            return self.actual.__enter__()
        def __exit__(self, *exception):
            tracked = getattr(thread_state, "prepare", False)
            try:
                result = self.actual.__exit__(*exception)
                if tracked and exception[0] is None:
                    # The real _run_db already committed, and its real session
                    # has exited. Only physical delegate completion is delayed.
                    entered.set()
                    assert release.wait(5), "CONTROLLED committed preparation thread was not released"
                return result
            finally:
                if tracked:
                    thread_state.prepare = False
                    physically_done.set()

    async def observed_db(unit, *args, **kwargs):
        call, lease = actual_lease(service)
        if unit.__name__ != "prepare" or lease is None:
            return await actual_db(unit, *args, **kwargs)
        assert lease["used"] and lease["child"] is asyncio.current_task()
        state.update(call=call, lease=lease, request=lease["permit"].request)
        def actual_prepare(db):
            thread_state.prepare = True
            intent_id = unit(db)  # Real locked ownership/cap/O14 preparation.
            state["intent_id"] = intent_id
            return intent_id
        return await actual_db(actual_prepare, *args, **kwargs)

    def observed_revoke(actual_service, call, permit):
        assert physically_done.is_set() and entered.is_set()
        assert call.wb2_pending is None and state["lease"]["child"].done()
        assert call.wb2_prepared[0] == state["intent_id"]  # Actual committed creator receipt captured first.
        actual_revoke(actual_service, call, permit)
        assert state["lease"]["revoked"] and vp._PREPARE_LEASE not in call.__dict__
        revoked.append(permit)

    monkeypatch.setattr(service, "session_factory", ObservedSession)
    monkeypatch.setattr(service, "_run_db", observed_db)
    monkeypatch.setattr(vp, "revoke_prepare_permit", observed_revoke)
    task = asyncio.create_task(close_case(case, "CW_TARGET"))
    key = (broker.account, broker.symbol)
    try:
        assert await asyncio.wait_for(asyncio.to_thread(entered.wait, 2), timeout=3)
        call, lease = state["call"], state["lease"]
        assert state["intent_id"] is not None and call.wb2_prepared is None
        with factory() as db:
            order = db.scalar(select(BrokerOrder).where(BrokerOrder.client_order_id == state["request"].client_order_id))
            assert order.intent_id == state["intent_id"] and order.status == "pending" and order.submitted_at is None
        for message in ("first-named-prepare-cancel", "second-named-prepare-cancel"):
            task.cancel(message)
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            assert not task.done() and not physically_done.is_set()
            assert call.__dict__[vp._PREPARE_LEASE] is lease and not lease["revoked"]
            assert call.wb2_pending is lease["child"] and not lease["child"].done()
            assert service._wbattach_exit_owners[key] is call
            assert obligations(case)[0]["active_token"] is not None
            assert not closes(broker) and revoked == []
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=3)
        assert physically_done.is_set() and len(revoked) == 1
        assert vp._PREPARE_LEASE not in call.__dict__ and call.wb2_pending is None
        assert key not in service._wbattach_exit_owners and not closes(broker)
        assert obligations(case)[0]["active_token"] is None
        with factory() as db:
            order = db.scalar(select(BrokerOrder).where(BrokerOrder.client_order_id == state["request"].client_order_id))
            assert order.intent_id == state["intent_id"] and order.status == "aborted"
            audits = list(db.scalars(select(BrokerOrderEvent).where(BrokerOrderEvent.order_id == order.id,
                BrokerOrderEvent.event_type == "aborted", BrokerOrderEvent.event_source == "client")))
            assert len(audits) == 1 and audits[0].payload["proven_not_sent"] is True
            assert db.scalar(select(VirtualPosition.quantity)) == 0
            assert db.scalar(select(OmsManagedPosition)).current_quantity == 27
            fills = list(db.scalars(select(Fill)))
            assert len(fills) == 1 and fills[0].side == "buy"
    finally:
        release.set()
        if not task.done():
            task.cancel("fixture-cleanup")
        await asyncio.gather(task, return_exceptions=True)
        if state:
            assert await asyncio.wait_for(asyncio.to_thread(physically_done.wait, 3), timeout=4)
