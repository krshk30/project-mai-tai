"""Real service owner loops, controlled preparation/callback ports; no seed proof.

Real constructor, no DB, provider, native book, timeline or readiness assignment.
Outer timeout bounds these functional controls, not a production performance gate.
"""

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from project_mai_tai.market_data.schwab_v2_rest_client import ChartBar
from project_mai_tai.services import schwab_1m_v2_bot as service
from project_mai_tai.settings import Settings
from tools import l12_boot_owner_clock as clocks


NOW = 1793012400000  # CONTROLLED Monday07:00, not recorded chronology.


@asynccontextmanager
async def scene(*, budget=16):
    bot = service.SchwabV2BotService(Settings(_env_file=None,
        strategy_schwab_1m_v2_keepwatch1_enabled=True,
        strategy_schwab_1m_v2_line_chart_restoration_enabled=True,
        strategy_schwab_1m_v2_brokerdecides1_enabled=False,
        strategy_schwab_1m_v2_bar_poll_interval_seconds=1,
        massive_api_key=""))
    clock = SimpleNamespace(now_ns=NOW * 1000000)
    clock.advance = lambda value: setattr(clock, "now_ns", value)
    bot.strategy = SimpleNamespace(_now_ms=lambda: clock.now_ns // 1000000, _symbol_states={})
    assert bot._keepwatch_producer_anchor_ms == 0
    assert not bot._keepwatch_producer_pins
    bot._subscription_symbols = lambda: {"AAA", "BBB"}
    ready, prepares, consumed = set(), [], []

    async def prepare(symbol, bar, **kwargs):
        prepares.append((symbol, clock.now_ns))
        return symbol in ready

    async def callback(symbol, bar, **kwargs):
        consumed.append((symbol, clock.now_ns, kwargs.get("_keepwatch_prepared")))

    # Delegate enqueue/dispatch/wait/inbox-complete/shutdown to actual APP code.
    # Only the history result and external delivery callback are controlled.
    bot._keepwatch_prepare_bar = prepare
    bot._handle_bar_from_rest = callback
    bot._handle_bar_from_streamer = callback
    records = []
    owner = clocks.OwnerClock(bot, service, clock, ("AAA", "BBB"), records,
        maximum_actions=budget, before_action=lambda event: None)
    value = SimpleNamespace(bot=bot, clock=clock, owner=owner, ready=ready,
        prepares=prepares, consumed=consumed, records=records)
    async with asyncio.timeout(5):
        with owner.bound():
            try:
                yield value
            finally:
                await service.SchwabV2BotService._stop_keepwatch_bar_owners(bot)


def enqueue_both(case):
    for symbol in ("AAA", "BBB"):
        case.bot._enqueue_keepwatch_bar(symbol,
            ChartBar(symbol, 1.0, 1.1, 0.9, 1.0, 10, NOW - 60000,
                producer="schwab"), "rest")  # Explicit CONTROLLED candle source.


@pytest.mark.asyncio
async def test_actual_two_owners_park_and_delegate_both_at_shared_deadline():
    async with scene() as case:
        enqueue_both(case)
        await case.owner.settle()
        assert len(case.owner.parked) == 2
        assert {row["event"].symbol for row in case.owner.parked.values()} == {"AAA", "BBB"}
        assert {row["deadline_ns"] for row in case.owner.parked.values()} == {NOW * 1000000 + 1000000000}
        case.ready.update({"AAA", "BBB"})
        await case.owner.advance_before(NOW * 1000000 + 1000000000)
        await case.owner.wake_due()
        assert sorted(symbol for symbol, _, _ in case.consumed) == ["AAA", "BBB"]
        assert all(prepared is True for _, _, prepared in case.consumed)
        assert case.owner.actions == 4 and not case.owner.parked
        assert all(box.accepted == box.consumed == 1 and not box.pending
            for box in case.bot._keepwatch_bar_inboxes.values())
        assert all(row["conservation"] for row in case.records)


@pytest.mark.asyncio
async def test_same_clock_membership_step_precedes_owner_retry_without_skipping_it():
    async with scene() as case:
        enqueue_both(case)
        await case.owner.settle()
        deadline = NOW * 1000000 + 1000000000
        steps = [dict(kind="membership", clock_ns=deadline), dict(kind="history_poll", clock_ns=deadline)]
        seen = []
        async for step in clocks.scheduled(steps, case.clock, case.owner):
            seen.append(step["kind"])
            if step["kind"] == "membership":
                assert not case.consumed
                case.ready.update({"AAA", "BBB"})
        assert seen == ["membership", "history_poll"]
        assert len(case.consumed) == 2 and case.owner.actions == 4


@pytest.mark.asyncio
async def test_one_symbol_event_wake_does_not_broadcast_to_sibling():
    async with scene() as case:
        enqueue_both(case)
        await case.owner.settle()
        case.ready.add("BBB")
        case.bot._keepwatch_bar_events["BBB"].set()
        await case.owner.settle()
        assert [symbol for symbol, _, _ in case.consumed] == ["BBB"]
        assert {row["event"].symbol for row in case.owner.parked.values()} == {"AAA"}
        assert len(case.bot._keepwatch_bar_inboxes["AAA"].pending) == 1


@pytest.mark.asyncio
async def test_model_clock_cannot_skip_either_pending_owner_deadline():
    async with scene() as case:
        enqueue_both(case)
        await case.owner.settle()
        with pytest.raises(clocks.OwnerProtocolRefusal, match="skip"):
            case.clock.advance(NOW * 1000000 + 2000000000)
        assert case.clock.now_ns == NOW * 1000000 and len(case.owner.parked) == 2


@pytest.mark.asyncio
async def test_combined_action_budget_refuses_without_consuming_or_dropping_sibling():
    async with scene(budget=1) as case:
        enqueue_both(case)
        with pytest.raises(clocks.OwnerProtocolRefusal, match="envelope"):
            await case.owner.settle()
        assert not case.consumed
        assert sum(box.accepted for box in case.bot._keepwatch_bar_inboxes.values()) == 2
        assert sum(len(box.pending) for box in case.bot._keepwatch_bar_inboxes.values()) == 2


@pytest.mark.asyncio
async def test_actual_shutdown_joins_both_owners_but_keeps_pending_unknown_work():
    async with scene() as case:
        enqueue_both(case)
        await case.owner.settle()
        tasks = tuple(case.bot._keepwatch_bar_tasks.values())
        await service.SchwabV2BotService._stop_keepwatch_bar_owners(case.bot)
        assert len(tasks) == 2 and all(task.done() for task in tasks)
        assert not case.bot._keepwatch_bar_tasks and not case.owner.parked
        assert sum(len(box.pending) for box in case.bot._keepwatch_bar_inboxes.values()) == 2
        assert sum(box.consumed for box in case.bot._keepwatch_bar_inboxes.values()) == 0


@pytest.mark.asyncio
async def test_accounting_contradiction_on_sibling_is_not_adopted_as_success():
    async with scene() as case:
        enqueue_both(case)
        await case.owner.settle()
        case.bot._keepwatch_bar_inboxes["BBB"].accepted += 1
        with pytest.raises(clocks.OwnerProtocolRefusal, match="accounting"):
            case.owner.record("CONTROLLED_CONTRADICTION", "BBB")


@pytest.mark.asyncio
async def test_unexpected_cancelled_actual_owner_stops_clock_as_unknown():
    async with scene() as case:
        enqueue_both(case)
        await case.owner.settle()
        task = case.bot._keepwatch_bar_tasks["BBB"]
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await asyncio.sleep(0)
        with pytest.raises(clocks.OwnerProtocolRefusal, match="failed"):
            await case.owner.settle()
        assert "BBB" in case.bot._keepwatch_unknown


@pytest.mark.asyncio
async def test_nonowner_wait_for_uses_actual_asyncio_with_exact_original_timeout():
    actual_wait_for = asyncio.wait_for
    delegated = AsyncMock(wraps=actual_wait_for)

    async def value():
        return "ACTUAL_ASYNCIO_COMPLETION"

    with patch.object(clocks, "asyncio", SimpleNamespace(wait_for=delegated)):
        assert await clocks.AsyncioPort(None).wait_for(value(), 0.375) == "ACTUAL_ASYNCIO_COMPLETION"
    assert delegated.await_count == 1 and delegated.await_args.args[1] == 0.375


@pytest.mark.asyncio
async def test_foreign_owner_event_cannot_borrow_other_clock():
    async with scene() as case:
        foreign = clocks.OwnerEvent(SimpleNamespace(), "AAA")
        with pytest.raises(clocks.OwnerProtocolRefusal, match="foreign"):
            await clocks.AsyncioPort(case.owner).wait_for(foreign.wait(), 1)


@pytest.mark.asyncio
async def test_nondelegated_task_cannot_park_as_actual_owner():
    async with scene() as case:
        event = case.bot._keepwatch_bar_events["AAA"]
        with pytest.raises(clocks.OwnerProtocolRefusal, match="actual owned"):
            await clocks.AsyncioPort(case.owner).wait_for(event.wait(), 1)
