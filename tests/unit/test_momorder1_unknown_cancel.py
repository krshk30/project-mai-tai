"""UNKNOWN status is not UNKNOWN identity, nor terminal/release proof."""

from datetime import UTC, datetime

import pytest

from project_mai_tai.broker_adapters.webull_order_reads import QueryBudget, QueryBudgetUnavailable
from project_mai_tai.momentum_live.admission import Refusal
from tests.unit.test_momlive1_exit_recovery import fresh
from tests.unit.test_momlive1_soft_exit_chase import exit_orders, prepared
from tests.unit.test_momorder1_runtime_db import CASES, held_case, seed


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["hard_stop_15pct", "time_300s"])
async def test_mandatory_exit_cancels_known_schwab_ids_on_unknown_status_but_never_sells_without_proof(reason):
    now, broker, service, runtime, identity = await prepared("schwab", 14, send_exit=False)
    handle = runtime.native.handles[identity]
    broker.native_status_unknown = True
    position = (await runtime._observe(identity)).position
    assert position.release_unknown
    await runtime._exit(position, reason)
    assert set(broker.cancelled) == {handle.base_coid + "T", handle.base_coid + "S"}
    assert len(broker.cancelled) == 2
    assert exit_orders(broker) == []
    position = (await runtime._observe(identity)).position
    assert position.release_unknown and position.remaining == 33
    fresh(now, service, 2)
    await runtime._exit(position, reason)
    assert len(broker.cancelled) == 2 and exit_orders(broker) == []
    broker.native_status_unknown = False
    fresh(now, service, 2)
    position = (await runtime._observe(identity)).position
    await runtime._exit(position, reason)
    assert len(exit_orders(broker)) == 1 and exit_orders(broker)[0].quantity == 33
    assert exit_orders(broker)[0].metadata["exit_reason"] == reason


@pytest.mark.asyncio
async def test_discretionary_unknown_schwab_status_defers_exact_child_cancels():
    _, broker, _, runtime, identity = await prepared("schwab", 14, send_exit=False)
    broker.native_status_unknown = True
    runtime.runtime[identity]["soft_exit_reason"] = "target_5pct"
    handle = runtime.native.handles[identity]
    with pytest.raises(Refusal, match="native_cancel_fresh_read_UNKNOWN"):
        await runtime.backend.cancel(handle, runtime)
    assert broker.cancelled == [] and exit_orders(broker) == []
    assert (await runtime._observe(identity)).position.release_unknown


@pytest.mark.asyncio
async def test_split_webull_detail_budget_retries_sibling_not_already_terminal_child(tmp_path, monkeypatch):
    h = await seed(tmp_path, CASES[1], at=datetime(2026, 10, 19, 14, tzinfo=UTC))
    try:
        _, handle = await held_case(h)
        target, stop = handle.child_ids
        tick = [100.0]
        budget = QueryBudget(lambda: tick[0])
        original = h.venue.fetch_order_update
        permitted = []

        async def bounded_read(request):
            if request.metadata.get("momlive_fresh_order_read") == "true":
                try:
                    budget.claim("detail", request.client_order_id, strict=True)
                except QueryBudgetUnavailable:
                    return None
                permitted.append(request.client_order_id)
            return await original(request)

        monkeypatch.setattr(h.venue, "fetch_order_update", bounded_read)
        with pytest.raises(Refusal, match="native_cancel_fresh_read_UNKNOWN"):
            await h.runtime.backend.cancel(handle, h.runtime)
        assert [(kind, coid) for kind, coid in h.venue.calls if kind == "cancel"] == [("cancel", target)]
        assert permitted == [target, target]
        assert len(budget.attempts["detail"]) == 2
        tick[0] += 2
        await h.runtime.backend.cancel(handle, h.runtime)
        assert [(kind, coid) for kind, coid in h.venue.calls if kind == "cancel"] == [
            ("cancel", target), ("cancel", stop)]
        assert permitted == [target, target, stop, stop]
        await h.runtime.backend.cancel(handle, h.runtime)
        assert permitted == [target, target, stop, stop]
        read = await h.runtime.backend.read(handle, h.runtime)
        assert read.terminal and read.cumulative_exited == 0
        assert not [row for row in h.venue.orders.values() if row.request.metadata.get("exit_reason")]
    finally:
        h.engine.dispose()
