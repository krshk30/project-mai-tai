"""CONTROLLED concurrency/budget contracts; no provider, timing or PG proof."""

import asyncio
from contextvars import ContextVar
from collections import deque

import pytest

from project_mai_tai import brokerdecides1_sync as sync
from project_mai_tai.broker_adapters.webull_order_reads import QueryBudget, QueryBudgetUnavailable
from project_mai_tai.brokerdecides1_collect import BACKGROUND_ALLOWED, require_background_priority
from tests.unit.test_brokerdecides1_sync import service


@pytest.mark.parametrize("pending", ["foreground", "queue", "terminal", "ordinary_permit", "backoff"])
def test_background_refuses_priority_work_without_queue_or_permit_consumption(pending):
    budget = QueryBudget(clock=lambda: 10.0)
    if pending == "foreground":
        budget.foreground_reads = 1
    elif pending == "queue":
        budget.waiting["detail"] = deque(["exit"])
    elif pending == "terminal":
        budget.terminal_inflight.add(("account", "order", "proof"))
    elif pending == "ordinary_permit":
        budget.claim("positions", "normal")
    else:
        budget.defer("positions", 30)
    before = list(budget.attempts.get("positions", ()))
    with pytest.raises(QueryBudgetUnavailable):
        budget.claim_background("positions", "L12")
    assert list(budget.attempts.get("positions", ())) == before
    assert not budget.background_at
    assert "L12" not in budget.waiting.get("positions", ())


def test_background_leaves_both_normal_permits_and_separate_shared_thirty_second_cap():
    now = [0.0]
    budget = QueryBudget(clock=lambda: now[0])
    budget.claim_background("positions", "v2_alias")
    budget.claim("positions", "normal_position")
    budget.claim("positions", "normal_exit")
    now[0] = 2.0
    with pytest.raises(QueryBudgetUnavailable):
        budget.claim_background("positions", "orb_alias")
    budget.claim("positions", "normal_position")
    now[0] = 30.0
    budget.claim_background("positions", "orb_alias")
    assert not budget.attempts["positions"]


@pytest.mark.parametrize("failure", [False, True])
def test_priority_scope_releases_on_success_and_exception(failure):
    budget = QueryBudget(clock=lambda: 0.0)
    try:
        with budget.foreground():
            assert budget.foreground_reads == 1
            if failure:
                raise ValueError("CONTROLLED")
    except ValueError:
        assert failure
    assert budget.foreground_reads == 0


def test_native_callback_refuses_new_request_when_original_sync_is_active():
    token = BACKGROUND_ALLOWED.set(lambda: False)
    try:
        with pytest.raises(ValueError, match="foreground_sync_has_priority"):
            require_background_priority()
    finally:
        BACKGROUND_ALLOWED.reset(token)


@pytest.mark.asyncio
async def test_retained_worker_thirty_second_limit_and_no_parent_context(monkeypatch):
    value = service(monkeypatch)
    now = [0.0]
    monkeypatch.setattr(sync, "refresh_clock", lambda: now[0])
    entered, release = asyncio.Event(), asyncio.Event()
    private = ContextVar("CONTROLLED_parent", default=None)
    calls = []

    async def begin(owner):
        assert private.get() is None
        calls.append("begin")
        entered.set()
        await release.wait()
        return None

    monkeypatch.setattr(sync, "begin_pass", begin)
    token = private.set("must_not_inherit_sync_sdk_frame")
    try:
        sync.schedule_refresh(value)
        first = value._brokerdecides1_refresh_task
        await entered.wait()
        now[0] = 31.0
        sync.schedule_refresh(value)
        assert value._brokerdecides1_refresh_task is first
        release.set()
        await first
        now[0] = 29.0
        sync.schedule_refresh(value)
        assert value._brokerdecides1_refresh_task is first
        now[0] = 30.0
        sync.schedule_refresh(value)
        await value._brokerdecides1_refresh_task
        assert calls == ["begin", "begin"]
    finally:
        private.reset(token)
        await sync.shutdown_refresh(value)


@pytest.mark.asyncio
async def test_original_sync_result_publication_does_not_await_stalled_native(monkeypatch):
    from project_mai_tai.oms.service import OmsRiskService
    from project_mai_tai.oms import wbquiet_shadow

    stages = []
    value = service(monkeypatch)
    entered, release = asyncio.Event(), asyncio.Event()

    async def prepare(*_):
        return None, None

    async def published(*_):
        stages.append("original_publication_complete")

    async def existing(*, account_names):
        stages.extend(("orders", "positions"))
        return {"orders": 1, "positions": 2}

    async def begin(owner):
        assert stages == ["orders", "positions", "original_publication_complete"]
        entered.set()
        await release.wait()
        return None

    value._sync_broker_state_pass = existing
    monkeypatch.setattr(wbquiet_shadow, "prepare", prepare)
    monkeypatch.setattr(wbquiet_shadow, "bind_pass", lambda *_: None)
    monkeypatch.setattr(wbquiet_shadow, "finish", published)
    monkeypatch.setattr(sync, "begin_pass", begin)
    assert await OmsRiskService.sync_broker_state(value) == {"orders": 1, "positions": 2}
    await entered.wait()
    first = value._brokerdecides1_refresh_task
    assert not first.done()
    assert await OmsRiskService.sync_broker_state(value) == {"orders": 1, "positions": 2}
    assert value._brokerdecides1_refresh_task is first
    release.set()
    await first
    assert await sync.shutdown_refresh(value)


@pytest.mark.asyncio
async def test_shutdown_cancels_own_worker_and_prevents_restart(monkeypatch):
    value = service(monkeypatch)
    entered = asyncio.Event()

    async def begin(owner):
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(sync, "begin_pass", begin)
    sync.schedule_refresh(value)
    await entered.wait()
    task = value._brokerdecides1_refresh_task
    assert await sync.shutdown_refresh(value)
    assert task.cancelled()
    sync.schedule_refresh(value)
    assert value._brokerdecides1_refresh_task is task


@pytest.mark.asyncio
async def test_failure_is_consumed_and_does_not_start_automatic_retry(monkeypatch):
    value = service(monkeypatch)
    now = [10.0]
    monkeypatch.setattr(sync, "refresh_clock", lambda: now[0])
    calls = []

    async def begin(owner):
        calls.append(1)
        raise ValueError("CONTROLLED_transport_unknown")

    monkeypatch.setattr(sync, "begin_pass", begin)
    sync.schedule_refresh(value)
    await value._brokerdecides1_refresh_task
    sync.schedule_refresh(value)
    assert calls == [1]
    assert await sync.shutdown_refresh(value)


@pytest.mark.asyncio
async def test_original_wire_stamps_expire_at_twenty_seconds_not_refresh_interval(monkeypatch):
    from project_mai_tai.brokerdecides1 import AdmissionMemory, decode
    from tests.unit.test_brokerdecides1_sync import native

    async def reader(account):
        return native(account)

    value = service(monkeypatch, reader)
    generation = await sync.begin_pass(value)
    await sync.finish_pass(value, generation, sync_succeeded=True)
    memory = AdmissionMemory()
    memory.apply(decode(value.redis.raw))
    original = memory.snapshot
    assert all(book.wire_started_ms == book.wire_completed_ms == 1000 for book in original.books)
    # WORX has a controlled fill; use another symbol to isolate freshness.
    at_boundary = memory.assess(accounts=sync.account_names(value.settings), symbol="LNKS",
        segment=900, now_ms=21000, max_age_ms=20000)
    assert not at_boundary.allowed and at_boundary.reason == "snapshot_stale"
    assert memory.snapshot is original
    assert all(book.wire_started_ms == 1000 for book in memory.snapshot.books)


def test_actual_collector_budget_refuses_second_position_page_not_partial_COMPLETE(monkeypatch):
    from project_mai_tai import brokerdecides1_collect as collector
    from tests.unit.test_brokerdecides1_webull_sdk_boundary import adapter_fixture

    adapter, requests, _, _ = adapter_fixture(monkeypatch, pagination=True)
    adapter._query_budget = QueryBudget(clock=lambda: 0.0)
    with pytest.raises(QueryBudgetUnavailable):
        collector._webull_blocking(adapter, "live:webull")
    assert len(requests) == 2  # Orders + first positions page, never guessed flat.
    assert not adapter._query_budget.attempts["positions"]
    assert adapter._query_budget.background_at["positions"] == 0.0


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
async def test_actual_foreground_delegate_retains_priority_until_delegate_end(monkeypatch, cancel):
    from project_mai_tai.broker_adapters.webull import WebullBrokerAdapter

    adapter = WebullBrokerAdapter.__new__(WebullBrokerAdapter)
    adapter._query_budget = QueryBudget(clock=lambda: 0.0)
    entered, release = asyncio.Event(), asyncio.Event()

    async def controlled_thread(function, *args):
        entered.set()
        await release.wait()
        return function(*args)

    monkeypatch.setattr(asyncio, "to_thread", controlled_thread)
    waiter = asyncio.create_task(adapter._foreground_query(lambda: "ORIGINAL_RESULT"))
    await entered.wait()
    if cancel:
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
    assert adapter._query_budget.foreground_reads == 1
    with pytest.raises(QueryBudgetUnavailable):
        adapter._query_budget.claim_background("positions", "L12")
    delegates = tuple(adapter._foreground_query_tasks)
    release.set()
    await asyncio.gather(*delegates)
    if not cancel:
        assert await waiter == "ORIGINAL_RESULT"
    assert adapter._query_budget.foreground_reads == 0
    assert not adapter._foreground_query_tasks


@pytest.mark.asyncio
async def test_shutdown_cannot_return_before_actual_native_delegate_finishes(monkeypatch):
    value = service(monkeypatch)
    release = asyncio.Event()
    pending = asyncio.create_task(release.wait())
    value._brokerdecides1_native_tasks = {pending}
    shutdown = asyncio.create_task(sync.shutdown_refresh(value))
    await asyncio.sleep(0)
    assert not shutdown.done() and not value._brokerdecides1_shutdown_joined
    assert pending in value._brokerdecides1_native_tasks and not pending.done()
    release.set()
    assert await shutdown and pending.done()


@pytest.mark.parametrize("enabled,expected", [(False, True), (True, True)])
def test_actual_ordinary_first_rest_skips_L12_snapshot_outside_exception(enabled, expected):
    from project_mai_tai.settings import Settings
    from project_mai_tai.strategy_core.schwab_1m_v2 import SchwabV2Strategy, SymbolState

    strategy = SchwabV2Strategy(Settings(_env_file=None,
        strategy_schwab_1m_v2_brokerdecides1_enabled=enabled,
        strategy_schwab_1m_v2_flip_owned_first_entry_enabled=False,
        strategy_schwab_1m_v2_false_flip_enabled=False))
    state = SymbolState(symbol="WORX")
    strategy._symbol_states["WORX"] = state
    # C1 16:12: no OLD stale/cap blocker means no L12 admission dependency.
    assert not state.cw_seed_cap_watch_start_ms
    assert strategy._strict_first_rest_admitted(state, slot="first") is expected
