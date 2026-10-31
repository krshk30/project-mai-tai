"""KWSTALE1 controlled real owner lifetimes; no broker or admission proof."""

import asyncio
import logging
from dataclasses import replace
from unittest.mock import Mock

import pytest

from project_mai_tai.services.keepwatch_bar_owner import BarInbox, OwnedBar
from tests.unit.test_keepwatch_session_producer_pin import candle, published


async def rejected_inbox():
    bot, ledger, bars, raw, state, queued = await published()
    inbox = bot._keepwatch_bar_inboxes["IZM"] = BarInbox(1)
    bot._keepwatch_bar_events["IZM"] = asyncio.Event()
    work = OwnedBar(bars[-1], "rest", ledger.anchor_ms, ledger.epoch)
    assert inbox.offer(work)
    # The incident's rejected stamp was zero. It must not turn into a watermark.
    rejected = replace(work, bar=replace(candle(), timestamp_ms=0))
    assert not inbox.offer(rejected)
    assert inbox.rejected == 1 and inbox.overflow_first_ms == 0
    hold = Mock(wraps=bot._keepwatch_hold_unknown)
    bot._keepwatch_hold_unknown = hold
    return bot, inbox, work, rejected, state, queued, hold


def controlled_resting_preimage(bot, state):
    # Explicit later waiting-order input after a recovery; not a placement/fill.
    # Reset both dedup sets so they cannot hide an erroneous second owner hold.
    bot._keepwatch_unknown.discard("IZM")
    bot._keepwatch_cancel_requested.discard("IZM")
    state.resting_active = state.webull_resting_active = True
    assert bot.strategy._entries_held
    assert not bot.strategy._flip_owner_restore_readable


async def run_new_owner(bot):
    task = asyncio.create_task(bot._run_keepwatch_bar_owner("IZM"))
    await asyncio.wait_for(task, 2)
    assert task.done() and not task.cancelled()


def overflow_errors(caplog):
    return [r for r in caplog.records if r.levelno == logging.ERROR
            and "[V2-KEEPWATCH1-BAR-OWNER]" in r.getMessage()
            and "rejected=" in r.getMessage()]


@pytest.mark.asyncio
async def test_one_rejection_is_held_once_across_six_later_owner_tasks(caplog):
    bot, inbox, work, rejected, state, queued, hold = await rejected_inbox()
    caplog.set_level(logging.ERROR)
    await run_new_owner(bot)
    assert hold.call_count == 1
    hold.assert_called_once_with("IZM", "bar_inbox_overflow")
    assert len(overflow_errors(caplog)) == 1
    first_cancels = len(queued)
    assert first_cancels > 0 and inbox.covered == 1 and not inbox.pending
    assert inbox.rejected_logged == inbox.rejected == 1
    for _ in range(6):
        controlled_resting_preimage(bot, state)
        assert inbox.offer(work)
        await run_new_owner(bot)
        assert "IZM" not in bot._keepwatch_unknown
        assert state.resting_active and state.webull_resting_active
        assert len(queued) == first_cancels
        assert hold.call_count == 1 and len(overflow_errors(caplog)) == 1
    assert inbox.covered == 7 and inbox.rejected == 1


@pytest.mark.asyncio
async def test_a_later_second_rejection_still_holds_and_cancels_once(caplog):
    bot, inbox, work, rejected, state, queued, hold = await rejected_inbox()
    caplog.set_level(logging.ERROR)
    await run_new_owner(bot)
    first_cancels = len(queued)
    assert first_cancels > 0 and hold.call_count == 1
    controlled_resting_preimage(bot, state)
    assert inbox.offer(work)
    assert not inbox.offer(rejected)
    assert inbox.rejected == 2 and inbox.rejected_logged == 1
    await run_new_owner(bot)
    assert hold.call_count == 2
    assert len(overflow_errors(caplog)) == 2
    assert "rejected=2" in overflow_errors(caplog)[-1].getMessage()
    assert "IZM" in bot._keepwatch_unknown and len(queued) > first_cancels
    assert inbox.rejected_logged == inbox.rejected == 2
    second_cancels = len(queued)
    controlled_resting_preimage(bot, state)
    assert inbox.offer(work)
    await run_new_owner(bot)
    assert hold.call_count == 2 and len(overflow_errors(caplog)) == 2
    assert len(queued) == second_cancels
