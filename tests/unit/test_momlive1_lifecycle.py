import asyncio
from decimal import Decimal as D

import pytest

from project_mai_tai.momentum_live.admission import Refusal
from project_mai_tai.momentum_live.lifecycle import EpisodeWorkQueue, OwnedPosition, QuoteDispatch


def position():
    return OwnedPosition("episode", "live:schwab_1m_v2", "SCNI", "own-parent", "own-order-id", D(2), D(3), 1_000_000_000)


@pytest.mark.parametrize("bid,reason", [("3.15", "target_5pct"), ("2.55", "hard_stop_15pct"), ("3", None)])
def test_exact_geometry_owned_partial_quantity(bid, reason):
    row = position()
    assert row.exit_reason(D(bid), quote_ns=2_000_000_000, now_ns=2_000_000_000) == reason
    assert row.remaining == 2


def test_native_confirmed_stands_down_price_but_not_time_exit():
    row = position()
    row.native_confirmed = True
    row.child_ids = ("own-target", "own-stop")
    assert row.exit_reason(D(2), quote_ns=2_000_000_000, now_ns=2_000_000_000) is None
    assert row.exit_reason(D(3), quote_ns=301_000_000_000, now_ns=301_000_000_000) == "time_300s"


@pytest.mark.parametrize("first,source,boundary", [(None, "send", 301_000_000_000),
                                                (2_000_000_000, "first_execution", 302_000_000_000)])
def test_approved_timer_selects_proven_first_else_send_with_exact_boundary(first, source, boundary):
    row = position()
    row.first_fill_ns, row.entry_send_ns = first, 1_000_000_000
    row.native_confirmed = True
    assert row.exit_clock_source == source
    assert row.exit_reason(D(3), quote_ns=boundary-1, now_ns=boundary-1) is None
    assert row.exit_reason(D(3), quote_ns=boundary, now_ns=boundary) == "time_300s"
    assert row.first_fill_ns == first


@pytest.mark.parametrize("age", [-1, 2_000_000_001])
def test_future_stale_quote_never_drives_exit(age):
    row = position()
    assert row.exit_reason(D(2), quote_ns=10_000_000_000-age, now_ns=10_000_000_000) is None


def test_late_atr_holding_cannot_enlarge_or_disable_owned_exit():
    row = position()
    assert row.claim_exit_quantity(broker_available=D(102), foreign_reserved=D(100)) == 2
    assert row.reserved == 2
    assert row.claim_exit_quantity(broker_available=D(102), foreign_reserved=D(100)) == 0


def test_release_exact_children_and_fill_race_before_own_remaining_sell():
    row = position()
    row.native_confirmed = True
    row.child_ids = ("own-target", "own-stop")
    with pytest.raises(Refusal, match="owner_mismatch"):
        row.release_confirmation(episode_id="episode", entry_order_id="own-order-id",
            requested_child_ids=("ATR-target", "ATR-stop"), terminal=True, cumulative_own_exit=D(0))
    row.release_confirmation(episode_id="episode", entry_order_id="own-order-id",
        requested_child_ids=row.child_ids, terminal=False, cumulative_own_exit=D(1))
    assert row.remaining == 1 and row.native_confirmed
    with pytest.raises(Refusal, match="unknown"):
        row.claim_exit_quantity(broker_available=D(101), foreign_reserved=D(100))
    row.release_confirmation(episode_id="episode", entry_order_id="own-order-id",
        requested_child_ids=row.child_ids, terminal=True, cumulative_own_exit=D(1))
    assert row.claim_exit_quantity(broker_available=D(101), foreign_reserved=D(100)) == 1


@pytest.mark.asyncio
async def test_quote_callback_only_coalesces_two_workers_with_blocked_io():
    release = asyncio.Event()
    started = asyncio.Event()
    calls = []
    async def worker(key):
        calls.append(key)
        started.set()
        await release.wait()
    work = EpisodeWorkQueue(worker)
    work.add_owner("pending")
    work.add_owner("partial")
    dispatch = QuoteDispatch(work, {"SCNI": ("pending", "partial")})
    dispatch.on_quote("SCNI")
    tasks = [asyncio.create_task(work.run()), asyncio.create_task(work.run())]
    await started.wait()
    await asyncio.sleep(0)
    for _ in range(1000):
        dispatch.on_quote("SCNI")
    assert len(calls) == 2 and work.high_water == 2
    assert work.running == {"pending", "partial"} and work.dirty == {"pending", "partial"}
    assert work.queue.qsize() == 0
    release.set()
    await work.queue.join()
    assert len(calls) == 4 and not work.dirty
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
