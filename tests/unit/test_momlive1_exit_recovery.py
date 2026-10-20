from dataclasses import replace
from datetime import timedelta
from decimal import Decimal as D

import pytest

from project_mai_tai.momentum_live.ledger import load_live_ledger
from project_mai_tai.momentum_live.runtime import OmsLiveRuntime
from tests.unit.test_momlive1_runtime import event, offer_and_drain, setup


async def held():
    now, broker, service, runtime = setup()
    await runtime.start()
    await offer_and_drain(runtime, event(now[0]))
    await runtime.close()
    identity = next(iter(runtime.book.episodes))
    now[0] += timedelta(seconds=301)
    fresh(now, service)
    return now, broker, service, runtime, identity


def fresh(now, service, seconds=0):
    now[0] += timedelta(seconds=seconds)
    service._latest_quotes_by_symbol["SCNI"] = {"ask": D(3), "bid": D(3), "received_at": now[0]}


def sells(broker):
    return [row for row in broker.sent if row.metadata.get("exit_reason")]


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["rejected", "expired", "cancelled"])
async def test_definite_terminal_exit_replaces_once_on_next_fresh_quote_after_two_seconds(status):
    now, broker, service, runtime, identity = await held()
    submit = broker.submit_order
    async def reject(request):
        reports = await submit(request)
        if request.metadata.get("exit_reason"):
            broker.orders[request.client_order_id].status = status
            return [await broker.fetch_order_update(request)]
        return reports
    broker.submit_order = reject
    position = (await runtime._observe(identity)).position
    await runtime._exit(position, "time_300s")
    assert len(sells(broker)) == 1
    fresh(now, service, 1.999999)
    await runtime._exit((await runtime._observe(identity)).position, "time_300s")
    assert len(sells(broker)) == 1
    fresh(now, service, .000001)
    await runtime._exit((await runtime._observe(identity)).position, "time_300s")
    assert len(sells(broker)) == 2
    assert {row.quantity for row in sells(broker)} == {D(33)}
    assert len({row.client_order_id for row in sells(broker)}) == 2
    assert len([row for row in broker.sent if row.side == "buy"]) == 1
    await runtime._exit((await runtime._observe(identity)).position, "time_300s")
    assert len(sells(broker)) == 2


@pytest.mark.asyncio
async def test_no_fresh_quote_after_native_release_stays_armed_then_sends():
    now, broker, service, runtime, identity = await held()
    submit, aged = broker.submit_order, []
    async def cancel_and_age(request):
        result = await submit(request)
        if request.intent_type == "cancel":
            # MOMORDER1 cancels exact legs, not the generic pair transport.
            aged.append(request.client_order_id)
            now[0] += timedelta(seconds=3)
        return result
    broker.submit_order = cancel_and_age
    await runtime._exit((await runtime._observe(identity)).position, "time_300s")
    assert set(aged) == set(runtime.native.handles[identity].child_ids) and len(aged) == 2
    assert sells(broker) == []
    assert runtime.runtime[identity]["soft_exit_reason"] == "time_300s"
    position = (await runtime._observe(identity)).position
    await runtime._protect(position)
    assert position.pending_exit_reason == "time_300s"
    assert not position.native_confirmed
    fresh(now, service)
    await runtime._exit(position, position.exit_reason(D(3), quote_ns=runtime.now_ns(), now_ns=runtime.now_ns()))
    assert len(sells(broker)) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("answer", ["missing", "working", "client_terminal"])
async def test_unknown_ack_or_nonbroker_terminal_reads_original_never_blind_second_sell(answer):
    now, broker, service, runtime, identity = await held()
    submit = broker.submit_order
    async def unknown(request):
        result = await submit(request)
        if request.metadata.get("exit_reason"):
            raise TimeoutError("unknown wire ACK")
        return result
    broker.submit_order = unknown
    with pytest.raises(TimeoutError):
        await runtime._exit((await runtime._observe(identity)).position, "time_300s")
    fetch, reads = broker.fetch_order_update, []
    async def answer_read(request):
        if request.metadata.get("exit_reason"):
            reads.append(request.client_order_id)
            if answer == "missing":
                return None
            report = await fetch(request)
            return replace(report, event_type="cancelled", origin="client") if answer == "client_terminal" else report
        return await fetch(request)
    broker.fetch_order_update = answer_read
    fresh(now, service, 3)
    await runtime._exit((await runtime._observe(identity)).position, "time_300s")
    assert reads and set(reads) == {sells(broker)[0].client_order_id}
    assert len(sells(broker)) == 1


@pytest.mark.asyncio
async def test_partial_terminal_exit_remaining_only_then_flat_stops_and_ledger_stays_open():
    now, broker, service, runtime, identity = await held()
    await runtime._exit((await runtime._observe(identity)).position, "time_300s")
    row = broker.orders[sells(broker)[0].client_order_id]
    row.status, row.quantity, row.price = "cancelled", D(5), D(3)
    fresh(now, service, 2)
    position = (await runtime._observe(identity)).position
    assert position.exited == 5
    await runtime._exit(position, "time_300s")
    assert [request.quantity for request in sells(broker)] == [D(33), D(28)]
    ledger = load_live_ledger(service.session_factory, now[0].date())
    assert len(ledger["positions"]) == 1 and ledger["positions"][0]["remaining_quantity"] == "28.00000000"
    row = broker.orders[sells(broker)[1].client_order_id]
    row.status, row.quantity, row.price = "filled", D(28), D(3)
    fresh(now, service, 2)
    position = (await runtime._observe(identity)).position
    assert position.remaining == 0
    await runtime._exit(position, "time_300s")
    assert len(sells(broker)) == 2
    assert load_live_ledger(service.session_factory, now[0].date())["positions"] == []


@pytest.mark.asyncio
async def test_switch_off_after_sixteen_and_restore_keeps_held_exit_armed():
    now, broker, service, runtime, identity = await held()
    await runtime._exit((await runtime._observe(identity)).position, "time_300s")
    broker.orders[sells(broker)[0].client_order_id].status = "expired"
    service.settings.momentum_live_enabled = False
    fresh(now, service, 7*3600)
    restarted = OmsLiveRuntime(service, clock=lambda: now[0], identity="987:654")
    assert await restarted.start()
    try:
        assert len(sells(broker)) == 2
        assert sells(broker)[1].metadata["session"] == "PM"
        assert sells(broker)[1].quantity == 33
        assert len([row for row in broker.sent if row.side == "buy"]) == 1
    finally:
        await restarted.close()


@pytest.mark.asyncio
async def test_pre_wire_quote_expiry_is_proven_unsent_and_next_fresh_quote_recovers():
    now, broker, service, runtime, identity = await held()
    save = runtime._save
    aged = [False]
    async def slow_save(episode):
        await save(episode)
        if runtime.runtime[identity].get("soft_exit_sent") and not aged[0]:
            aged[0] = True
            now[0] += timedelta(seconds=3)
    runtime._save = slow_save
    await runtime._exit((await runtime._observe(identity)).position, "time_300s")
    assert sells(broker) == []
    snapshot = await runtime._inspect(identity)
    assert any(row["status"] == "aborted" and row["request"].metadata.get("momlive_exit_never_dispatched")
               for row in snapshot["orders"])
    fresh(now, service)
    await runtime._exit((await runtime._observe(identity)).position, "time_300s")
    assert len(sells(broker)) == 1 and sells(broker)[0].quantity == 33


@pytest.mark.asyncio
async def test_terminal_history_does_not_disable_held_exit_after_sixty_four_attempts():
    now, broker, service, runtime, identity = await held()
    submit = broker.submit_order
    async def rejected(request):
        reports = await submit(request)
        if request.metadata.get("exit_reason"):
            broker.orders[request.client_order_id].status = "rejected"
            return [await broker.fetch_order_update(request)]
        return reports
    broker.submit_order = rejected
    for _ in range(66):
        fresh(now, service, 2)
        await runtime._exit((await runtime._observe(identity)).position, "time_300s")
    assert len(sells(broker)) == 66
    assert len({request.client_order_id for request in sells(broker)}) == 66
    assert all(request.quantity == 33 for request in sells(broker))
    snapshot = await runtime._inspect(identity)
    assert snapshot["order_count"] > 64 and len(snapshot["orders"]) <= 64
    assert snapshot["quantity"] == 33 and snapshot["exited"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["rejected", "expired", "cancelled"])
async def test_terminal_native_pair_keeps_watched_target_armed_for_owned_quantity(status):
    now, broker, service, runtime, identity = await held()
    now[0] -= timedelta(seconds=301)
    handle = runtime.native.handles[identity]
    for coid in handle.child_ids:
        broker.orders[coid].status = status
    fresh(now, service)
    service._latest_quotes_by_symbol["SCNI"]["bid"] = D("3.20")
    position = (await runtime._observe(identity)).position
    await runtime._protect(position)
    assert not position.native_confirmed and not position.release_unknown
    reason = position.exit_reason(D("3.20"), quote_ns=runtime.now_ns(), now_ns=runtime.now_ns())
    assert reason == "target_5pct"
    await runtime._exit(position, reason)
    assert len(sells(broker)) == 1 and sells(broker)[0].quantity == 33


@pytest.mark.asyncio
async def test_actual_dispatch_spacing_survives_unequal_save_latency_original_j_case():
    now, broker, service, runtime, identity = await held()
    save, submit = runtime._save, broker.submit_order
    submit_times, delayed = [], [False]
    async def slow_first_save(episode):
        await save(episode)
        if runtime.runtime[identity].get("soft_exit_sent") and not delayed[0]:
            delayed[0] = True
            now[0] += timedelta(seconds=.9)
    async def rejected(request):
        reports = await submit(request)
        if request.metadata.get("exit_reason"):
            submit_times.append(runtime.now_ns())
            broker.orders[request.client_order_id].status = "rejected"
            return [await broker.fetch_order_update(request)]
        return reports
    runtime._save, broker.submit_order = slow_first_save, rejected
    await runtime._exit((await runtime._observe(identity)).position, "time_300s")
    journal = runtime.runtime[identity]["soft_exit_attempt_ns"]
    assert submit_times[0]-journal == 900_000_000
    fresh(now, service, 1.1)  # Original defective journal+2s / actual call+1.1s.
    await runtime._exit((await runtime._observe(identity)).position, "time_300s")
    assert len(submit_times) == 1
    fresh(now, service, .899999)
    await runtime._exit((await runtime._observe(identity)).position, "time_300s")
    assert len(submit_times) == 1
    fresh(now, service, .000001)
    await runtime._exit((await runtime._observe(identity)).position, "time_300s")
    assert len(submit_times) == 2 and submit_times[1]-submit_times[0] == 2_000_000_000


@pytest.mark.asyncio
@pytest.mark.parametrize("answer", ["rejected", "unknown"])
async def test_crash_missing_dispatch_completion_requires_terminal_read_then_two_second_hold(answer):
    now, broker, service, runtime, identity = await held()
    await runtime._exit((await runtime._observe(identity)).position, "time_300s")
    original = sells(broker)[0]
    broker.orders[original.client_order_id].status = "rejected" if answer == "rejected" else "accepted"
    runtime.runtime[identity].pop("soft_exit_dispatch_completed_ns")
    await runtime._save(runtime.book.snapshot()[0])  # Crash pre-completion journal.
    fresh(now, service, 3)
    restarted = OmsLiveRuntime(service, clock=lambda: now[0], identity="987:654")
    assert await restarted.start()
    await restarted.close()
    assert len(sells(broker)) == 1
    if answer == "unknown":
        assert "soft_exit_terminal_verified_ns" not in restarted.runtime[identity]
        broker.orders[original.client_order_id].status = "rejected"
        fresh(now, service, 1)
        await restarted._exit((await restarted._observe(identity)).position, "time_300s")
        assert len(sells(broker)) == 1
    assert restarted.runtime[identity]["soft_exit_terminal_verified_ns"] == restarted.now_ns()
    fresh(now, service, 1.999999)
    await restarted._exit((await restarted._observe(identity)).position, "time_300s")
    assert len(sells(broker)) == 1
    fresh(now, service, .000001)
    await restarted._exit((await restarted._observe(identity)).position, "time_300s")
    assert len(sells(broker)) == 2 and sells(broker)[1].quantity == 33


@pytest.mark.asyncio
async def test_delayed_broker_completion_starts_conservative_two_second_spacing():
    now, broker, service, runtime, identity = await held()
    submit, starts = broker.submit_order, []
    async def slow_response(request):
        if request.metadata.get("exit_reason"):
            starts.append(runtime.now_ns())
            if len(starts) == 1:
                now[0] += timedelta(seconds=.6)
        reports = await submit(request)
        if request.metadata.get("exit_reason"):
            broker.orders[request.client_order_id].status = "rejected"
            return [await broker.fetch_order_update(request)]
        return reports
    broker.submit_order = slow_response
    await runtime._exit((await runtime._observe(identity)).position, "time_300s")
    completed = runtime.runtime[identity]["soft_exit_dispatch_completed_ns"]
    assert completed-starts[0] == 600_000_000
    fresh(now, service, 1.4)
    await runtime._exit((await runtime._observe(identity)).position, "time_300s")
    assert len(starts) == 1
    fresh(now, service, .6)
    await runtime._exit((await runtime._observe(identity)).position, "time_300s")
    assert len(starts) == 2 and starts[1]-completed == 2_000_000_000


@pytest.mark.asyncio
async def test_second_unknown_call_crash_cannot_reuse_prior_completion_proof():
    now, broker, service, runtime, identity = await held()
    submit, save = broker.submit_order, runtime._save
    async def second_unknown(request):
        reports = await submit(request)
        if request.metadata.get("exit_reason"):
            if len(sells(broker)) == 1:
                broker.orders[request.client_order_id].status = "rejected"
                return [await broker.fetch_order_update(request)]
            if len(sells(broker)) != 2:
                return reports
            durable = (await runtime._inspect(identity))["runtime"]
            assert "soft_exit_dispatch_completed_ns" not in durable
            assert "soft_exit_terminal_verified_ns" not in durable
            raise TimeoutError("second dispatch ACK UNKNOWN")
        return reports
    async def crash_completion_save(episode):
        values = runtime.runtime[identity]
        if values.get("soft_exit_attempt") == 2 and values.get("soft_exit_dispatch_completed_ns"):
            raise OSError("controlled lost completion persistence")
        await save(episode)
    broker.submit_order, runtime._save = second_unknown, crash_completion_save
    await runtime._exit((await runtime._observe(identity)).position, "time_300s")
    fresh(now, service, 3)
    with pytest.raises(OSError):
        await runtime._exit((await runtime._observe(identity)).position, "time_300s")
    assert len(sells(broker)) == 2
    fresh(now, service, 3)
    restarted = OmsLiveRuntime(service, clock=lambda: now[0], identity="987:654")
    assert await restarted.start()
    await restarted.close()
    assert "soft_exit_terminal_verified_ns" not in restarted.runtime[identity]
    assert len(sells(broker)) == 2
    broker.orders[sells(broker)[1].client_order_id].status = "rejected"
    fresh(now, service, 1)
    await restarted._exit((await restarted._observe(identity)).position, "time_300s")
    assert len(sells(broker)) == 2
    verified = restarted.runtime[identity]["soft_exit_terminal_verified_ns"]
    fresh(now, service, 2)
    await restarted._exit((await restarted._observe(identity)).position, "time_300s")
    assert len(sells(broker)) == 3 and restarted.now_ns()-verified == 2_000_000_000
