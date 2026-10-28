"""Post-freeze development controls; no historical/native/LNKS acceptance.

Real owned enqueue, callbacks, preparation and thread joining are exercised at
explicit controlled I/O ports. No guard, completion predicate or held bit is
replaced. Main alone may execute these after source review.
"""

import asyncio
from dataclasses import replace
from datetime import UTC, datetime
import threading
from unittest.mock import Mock

import pytest

from project_mai_tai.services.keepwatch_bar_owner import BarInbox, OwnedBar
from tests.unit.test_keepwatch_central_boot_retry import (
    assert_held, controlled_bot, controlled_proof,
)
from tests.unit.test_keepwatch_session_producer_pin import (
    ANCHOR, candle, published, response,
)


def ready_fleet(bot):
    # Explicit CONTROLLED fleet inputs, not an original scanner/native claim.
    bot._boot_scanner_selected = set(bot._watchlist)
    bot._boot_exclusion_sources_readable = True
    bot._rest_warmup_done.update(bot._watchlist)


async def published_fleet(*, count=40):
    bot, ledger, bars, raw, state, queued = await published(
        bars=tuple(candle(index, close=10 - index * .1) for index in range(count)))
    ready_fleet(bot)
    assert not bot._boot_state_restoration_complete
    assert bot.strategy._entries_held
    return bot, ledger, bars, raw, state, queued


async def one_pass_sleep(_stop, _seconds):
    # The caller requests one actual pass; no background cadence is accelerated.
    await asyncio.sleep(0)


@pytest.mark.parametrize("scope_kind", ["other_symbol", "old_anchor", "old_epoch"])
def test_attributed_unrelated_work_does_not_block_current_fleet(scope_kind):
    bot = controlled_bot()
    controlled_proof(bot)
    bot._sync_line_epochs()
    epoch = bot._line_sessions["IZM"].epoch
    scope = {"other_symbol": ("LNKS", ANCHOR, epoch),
        "old_anchor": ("IZM", ANCHOR - 86_400_000, epoch),
        "old_epoch": ("IZM", ANCHOR, epoch + 1)}[scope_kind]
    token = object()
    bot._keepwatch_history_scopes[token] = scope
    # Opaque task identity in this synchronous readset control; no task runs.
    native = object()
    bot._keepwatch_native_reads.add(native)
    bot._keepwatch_native_read_scopes[native] = scope
    bot._cw_boot_hold_check()
    assert bot._boot_state_restoration_complete
    assert not bot.strategy._entries_held
    assert bot._keepwatch_history_scopes[token] == scope
    assert bot._keepwatch_native_read_scopes[native] == scope


@pytest.mark.parametrize("epoch_kind", ["current", "unresolved_ingress"])
def test_pending_current_work_blocks_until_real_inbox_completion(epoch_kind):
    bot = controlled_bot()
    controlled_proof(bot)
    bot._sync_line_epochs()
    epoch = bot._line_sessions["IZM"].epoch if epoch_kind == "current" else 0
    inbox = bot._keepwatch_bar_inboxes["IZM"] = BarInbox(500)
    work = OwnedBar(candle(9), "rest", ANCHOR, epoch)
    assert inbox.offer(work)
    assert_held(bot)
    assert bot._keepwatch_boot_retry_deferred_reason == "selected_owner_pending"
    inbox.complete(work)
    bot._cw_boot_hold_check()
    assert bot._boot_state_restoration_complete
    assert not bot.strategy._entries_held
    assert inbox.consumed == 1 and not inbox.pending


@pytest.mark.parametrize("kind", ["old_anchor", "old_epoch", "other_symbol"])
def test_stale_or_unselected_pending_inbox_is_not_a_global_barrier(kind):
    bot = controlled_bot()
    controlled_proof(bot)
    bot._sync_line_epochs()
    epoch = bot._line_sessions["IZM"].epoch
    symbol = "LNKS" if kind == "other_symbol" else "IZM"
    anchor = ANCHOR - 86_400_000 if kind == "old_anchor" else ANCHOR
    bar = replace(candle(9), symbol=symbol, timestamp_ms=anchor + 9 * 60_000)
    work = OwnedBar(bar, "rest", anchor, epoch + 1 if kind == "old_epoch" else epoch)
    inbox = bot._keepwatch_bar_inboxes[symbol] = BarInbox(500)
    assert inbox.offer(work)
    bot._cw_boot_hold_check()
    assert bot._boot_state_restoration_complete and not bot.strategy._entries_held
    assert inbox.pending[bar.timestamp_ms] is work  # Not silently expired/drained.


@pytest.mark.asyncio
async def test_healthy_parked_owner_with_empty_inbox_is_not_a_boot_dependency():
    bot = controlled_bot()
    controlled_proof(bot)
    parked = asyncio.Event()
    task = asyncio.create_task(parked.wait())
    bot._keepwatch_bar_tasks["IZM"] = task
    bot._keepwatch_bar_inboxes["IZM"] = BarInbox(500)
    try:
        bot._cw_boot_hold_check()
        assert bot._boot_state_restoration_complete and not bot.strategy._entries_held
        assert not task.done()
    finally:
        parked.set()
        await task


def test_unregistered_native_task_is_explicit_unknown_not_invented_scope():
    bot = controlled_bot()
    controlled_proof(bot)
    native = object()
    bot._keepwatch_native_reads.add(native)
    assert_held(bot)
    assert bot._keepwatch_boot_retry_deferred_reason == "native_scope_unknown"
    assert native not in bot._keepwatch_native_read_scopes
    bot._keepwatch_native_reads.remove(native)
    bot._cw_boot_hold_check()
    assert bot._boot_state_restoration_complete and not bot.strategy._entries_held


def test_deferral_still_delegates_unchanged_dangerous_segment_check(monkeypatch):
    bot = controlled_bot()
    controlled_proof(bot)
    bot._keepwatch_bar_accepting = False
    actual = bot.strategy.cw_armed_segments
    observed = Mock(wraps=actual)
    monkeypatch.setattr(bot.strategy, "cw_armed_segments", observed)
    assert_held(bot)
    assert observed.call_count == 1
    assert bot._keepwatch_boot_retry_deferred_reason == "shutdown"


def test_multi_symbol_fleet_retries_only_after_last_current_operation_settles():
    bot = controlled_bot(symbols=("IZM", "LNKS"))
    for symbol in bot._watchlist:
        controlled_proof(bot, symbol)
    bot._sync_line_epochs()
    tokens = {symbol: object() for symbol in bot._watchlist}
    for symbol, token in tokens.items():
        bot._keepwatch_history_scopes[token] = (symbol, ANCHOR, bot._line_sessions[symbol].epoch)
    assert_held(bot)
    bot._keepwatch_history_scopes.pop(tokens["IZM"])
    assert_held(bot)
    bot._keepwatch_history_scopes.pop(tokens["LNKS"])
    bot._cw_boot_hold_check()
    assert bot._boot_state_restoration_complete and not bot.strategy._entries_held


@pytest.mark.asyncio
async def test_actual_history_operation_defers_during_read_then_releases_after_owner_settles(monkeypatch):
    bot, ledger, bars, raw, state, queued = await published_fleet()
    next_bar = candle(40, close=6.0)
    now = next_bar.timestamp_ms + 61_000
    bot.strategy._now_ms = lambda: now
    full = response((*bars, next_bar), (), now=now)
    entered, finish = threading.Event(), threading.Event()

    def read(*_args):
        entered.set()
        if not finish.wait(5):
            raise AssertionError("controlled history read was not released")
        return full

    monkeypatch.setattr("project_mai_tai.market_data.keepwatch_history.read_keepwatch_history", read)
    monkeypatch.setattr("project_mai_tai.services.schwab_1m_v2_bot.sleep_or_stop", one_pass_sleep)
    task = asyncio.create_task(bot._keepwatch_history_pass())
    try:
        assert await asyncio.to_thread(entered.wait, 3)
        assert list(bot._keepwatch_history_scopes.values()) == [("IZM", ANCHOR, ledger.epoch)]
        assert_held(bot)
        assert bot._keepwatch_boot_retry_deferred_reason == "selected_history_active"
    finally:
        finish.set()
        await asyncio.wait_for(asyncio.shield(task), 5)
        owners = tuple(bot._keepwatch_bar_tasks.values())
        await asyncio.wait_for(asyncio.gather(*(asyncio.shield(owner) for owner in owners)), 5)
    assert not bot._keepwatch_history_scopes
    assert not bot._keepwatch_native_reads and not bot._keepwatch_native_read_scopes
    assert not bot._keepwatch_bar_inboxes["IZM"].pending
    bot._cw_boot_hold_check()
    assert bot._boot_state_restoration_complete and not bot.strategy._entries_held


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["rest", "streamer"])
async def test_actual_owner_callback_pending_then_consumed_releases_without_scanner(monkeypatch, source):
    bot, ledger, bars, raw, state, queued = await published_fleet()
    next_bar = candle(40, close=6.0)
    bot.strategy._now_ms = lambda: next_bar.timestamp_ms + 61_000
    full = response((*bars, next_bar), ())
    monkeypatch.setattr("project_mai_tai.market_data.keepwatch_history.read_keepwatch_history",
                        lambda *_args: full)
    name = "_handle_bar_from_rest" if source == "rest" else "_handle_bar_from_streamer"
    callback = getattr(bot, name)
    entered, finish = asyncio.Event(), asyncio.Event()

    async def observed(*args, **kwargs):
        entered.set()
        await finish.wait()
        return await callback(*args, **kwargs)

    monkeypatch.setattr(bot, name, observed)
    bot._enqueue_keepwatch_bar("IZM", next_bar, source)
    owner = bot._keepwatch_bar_tasks["IZM"]
    try:
        await asyncio.wait_for(entered.wait(), 5)
        # Cached prefix admission need not refetch the current minute. This is
        # actual already-proven prefix state, not a fabricated current response.
        assert bot._keepwatch_proven["IZM"].through_ms >= bars[-1].timestamp_ms
        assert_held(bot)
        assert bot._keepwatch_boot_retry_deferred_reason == "selected_owner_pending"
        assert not owner.done()
    finally:
        finish.set()
        try:
            await asyncio.wait_for(asyncio.shield(owner), 5)
        finally:
            if not owner.done():
                await bot._stop_keepwatch_bar_owners()
    inbox = bot._keepwatch_bar_inboxes["IZM"]
    assert inbox.consumed == 1 and not inbox.pending
    assert inbox.failures == inbox.rejected == 0
    assert not bot._keepwatch_native_reads and not bot._keepwatch_native_read_scopes
    bot._cw_boot_hold_check()
    assert bot._boot_state_restoration_complete and not bot.strategy._entries_held
    assert not bot.strategy._flip_owner_restore_readable  # No native permission fabricated.


@pytest.mark.asyncio
async def test_actual_owner_revision_during_callback_cannot_clear_replacement(monkeypatch):
    bot, ledger, bars, raw, state, queued = await published_fleet()
    first = candle(40, close=6.0)
    revised = replace(first, close=5.99)
    bot.strategy._now_ms = lambda: first.timestamp_ms + 61_000
    current = [first]
    monkeypatch.setattr("project_mai_tai.market_data.keepwatch_history.read_keepwatch_history",
                        lambda *_args: response((*bars, current[0]), ()))
    actual = bot._handle_bar_from_rest
    entered, finish = asyncio.Event(), asyncio.Event()
    observed_closes = []

    async def callback(*args, **kwargs):
        observed_closes.append(args[1].close)
        entered.set()
        await finish.wait()
        return await actual(*args, **kwargs)

    monkeypatch.setattr(bot, "_handle_bar_from_rest", callback)
    bot._enqueue_keepwatch_bar("IZM", first, "rest")
    owner = bot._keepwatch_bar_tasks["IZM"]
    try:
        await asyncio.wait_for(entered.wait(), 5)
        current[0] = revised
        bot._enqueue_keepwatch_bar("IZM", revised, "rest")
        replacement = bot._keepwatch_bar_inboxes["IZM"].pending[first.timestamp_ms]
        assert replacement.bar.close == 5.99
        assert_held(bot)
        assert bot._keepwatch_boot_retry_deferred_reason == "selected_owner_pending"
    finally:
        finish.set()
        try:
            await asyncio.wait_for(asyncio.shield(owner), 5)
        finally:
            if not owner.done():
                await bot._stop_keepwatch_bar_owners()
    inbox = bot._keepwatch_bar_inboxes["IZM"]
    assert inbox.revised >= 1 and inbox.consumed == 1 and not inbox.pending
    assert inbox.failures == inbox.rejected == 0
    assert observed_closes[0] == 6.0 and observed_closes[-1] == 5.99
    assert state.bars[-1].close == 5.99
    bot._cw_boot_hold_check()
    assert bot._boot_state_restoration_complete and not bot.strategy._entries_held


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_count", [0, 1, 2])
async def test_actual_native_read_scope_lasts_through_completion_or_cancellation_join(monkeypatch, cancel_count):
    bot = controlled_bot()
    controlled_proof(bot)
    bot._sync_line_epochs()
    epoch = bot._line_sessions["IZM"].epoch
    started, finish = threading.Event(), threading.Event()
    raw = response(tuple(candle(index) for index in range(10)), ())

    def read(*_args):
        started.set()
        if not finish.wait(5):
            raise AssertionError("controlled native port was not released")
        return raw

    monkeypatch.setattr("project_mai_tai.market_data.keepwatch_history.read_keepwatch_history", read)
    outer = asyncio.create_task(bot._read_keepwatch_history_owned("IZM", ANCHOR,
                                                               raw.through_ms))
    stopper = None
    try:
        assert await asyncio.to_thread(started.wait, 3)
        native, = bot._keepwatch_native_reads
        assert bot._keepwatch_native_read_scopes[native] == ("IZM", ANCHOR, epoch)
        assert_held(bot)
        assert bot._keepwatch_boot_retry_deferred_reason == "selected_native_unjoined"
        if cancel_count:
            outer.cancel()
            await asyncio.sleep(0)
        if cancel_count == 2:
            outer.cancel()
            with pytest.raises(asyncio.CancelledError):
                await outer
        assert native in bot._keepwatch_native_reads and not native.done()
        assert bot._keepwatch_native_read_scopes[native] == ("IZM", ANCHOR, epoch)
        if cancel_count:
            stopper = asyncio.create_task(bot._stop_keepwatch_bar_owners())
            await asyncio.sleep(0)
            assert not stopper.done()
            assert_held(bot)
            assert bot._keepwatch_boot_retry_deferred_reason == "shutdown"
    finally:
        finish.set()
        if cancel_count:
            with pytest.raises(asyncio.CancelledError):
                await outer
        else:
            assert await outer is raw
        if stopper is not None:
            await asyncio.wait_for(stopper, 5)
        await asyncio.sleep(0)  # Actual task completion callback retires registration.
    assert not bot._keepwatch_native_reads and not bot._keepwatch_native_read_scopes
    if not cancel_count:
        bot._cw_boot_hold_check()
        assert bot._boot_state_restoration_complete and not bot.strategy._entries_held


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["fresh", "replay", "empty_tail", "caught_up"])
async def test_real_history_pass_scopes_cleanup_and_eventual_retry(monkeypatch, kind):
    count = 30 if kind == "replay" else 40
    bot, ledger, bars, raw, state, queued = await published_fleet(count=count)
    now = ANCHOR + (40 if kind == "caught_up" else 41) * 60_000 + 1_000
    bot.strategy._now_ms = lambda: now
    full_bars = (*bars, candle(40, close=6.0)) if kind == "fresh" else (
        tuple(candle(index, close=10 - index * .1) for index in range(40))
        if kind == "replay" else bars)
    through = ANCHOR + (39 if kind == "caught_up" else 40) * 60_000
    full = response(full_bars, (), through=through, now=now)
    selected_scopes = []
    calls = []

    def read(*args):
        calls.append(args[-1])
        selected_scopes.extend(bot._keepwatch_history_scopes.values())
        return full

    monkeypatch.setattr("project_mai_tai.market_data.keepwatch_history.read_keepwatch_history", read)
    monkeypatch.setattr("project_mai_tai.services.schwab_1m_v2_bot.sleep_or_stop", one_pass_sleep)
    try:
        await bot._keepwatch_history_pass()
        owners = tuple(bot._keepwatch_bar_tasks.values())
        await asyncio.wait_for(asyncio.gather(*(asyncio.shield(task) for task in owners)), 5)
        assert not bot._keepwatch_history_scopes
        assert not bot._keepwatch_native_reads and not bot._keepwatch_native_read_scopes
        if kind == "caught_up":
            assert not calls and not selected_scopes
        else:
            assert calls and all(scope == ("IZM", ANCHOR, ledger.epoch) for scope in selected_scopes)
            assert bot._keepwatch_proven["IZM"].through_ms == through
        if kind in {"empty_tail", "replay"}:
            assert state.bars[-1].timestamp_ms == ANCHOR + 39 * 60_000
            assert all(bar.timestamp_ms != through for bar in state.bars)
        bot._cw_boot_hold_check()
        assert bot._boot_state_restoration_complete and not bot.strategy._entries_held
    finally:
        await bot._stop_keepwatch_bar_owners()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [ValueError("CONTROLLED_READ_ERROR"), asyncio.CancelledError()])
async def test_real_history_failure_or_cancellation_retires_operation_without_proof(monkeypatch, failure):
    bot = controlled_bot()
    bot._sync_line_epochs()
    bot._configure_flip_entry_ownership_store({})
    bot.strategy.configure_falseflip({}, readable=False)
    before = bot._keepwatch_proven.copy()

    def read(*_args):
        assert bot._keepwatch_history_scopes
        raise failure

    monkeypatch.setattr("project_mai_tai.market_data.keepwatch_history.read_keepwatch_history", read)
    monkeypatch.setattr("project_mai_tai.services.schwab_1m_v2_bot.sleep_or_stop", one_pass_sleep)
    if isinstance(failure, asyncio.CancelledError):
        with pytest.raises(asyncio.CancelledError):
            await bot._keepwatch_history_pass()
    else:
        await bot._keepwatch_history_pass()
    assert not bot._keepwatch_history_scopes
    assert not bot._keepwatch_native_reads and not bot._keepwatch_native_read_scopes
    assert bot._keepwatch_proven == before == {}
    assert_held(bot)


def test_post_latch_calls_do_not_scan_or_reseed_new_work(monkeypatch):
    bot = controlled_bot()
    controlled_proof(bot)
    bot._cw_boot_hold_check()
    assert bot._boot_state_restoration_complete
    guard = Mock(side_effect=AssertionError("post-latch ordering scan"))
    monkeypatch.setattr(bot, "_keepwatch_boot_retry_quiescent", guard)
    bot._watchlist.add("LNKS")
    for _ in range(100):
        bot._cw_boot_hold_check()
    assert not guard.called and "LNKS" not in bot._keepwatch_proven


def test_direct_completion_caller_cannot_bypass_current_pending_owner():
    """Original strict direct-caller exposure, retained without an assertion waiver."""
    bot = controlled_bot()
    controlled_proof(bot)
    bot._sync_line_epochs()
    epoch = bot._line_sessions["IZM"].epoch
    inbox = bot._keepwatch_bar_inboxes["IZM"] = BarInbox(500)
    work = OwnedBar(candle(9), "rest", ANCHOR, epoch)
    assert inbox.offer(work)
    bot._try_complete_boot_state_restoration(bot._watchlist, new_symbols=set())
    assert not bot._boot_state_restoration_complete
    assert bot.strategy._entries_held
    assert inbox.pending[work.bar.timestamp_ms] is work


@pytest.mark.asyncio
async def test_actual_rest_warmup_completion_cannot_bypass_other_selected_pending_work(monkeypatch):
    """Connected direct-caller exposure; original strict assertions retained."""
    bot, ledger, bars, raw, state, queued = await published_fleet()
    bot._watchlist.add("LNKS")
    bot._boot_scanner_selected.add("LNKS")
    bot.strategy.watchlist_state("LNKS")
    controlled_proof(bot, "LNKS")
    bot._sync_line_epochs()
    other_epoch = bot._line_sessions["LNKS"].epoch
    other_inbox = bot._keepwatch_bar_inboxes["LNKS"] = BarInbox(500)
    other = OwnedBar(replace(candle(39), symbol="LNKS"), "rest", ANCHOR, other_epoch)
    assert other_inbox.offer(other)
    bot._rest_warmup_done.discard("IZM")
    next_bar = candle(40, close=6.0)
    bot.strategy._now_ms = lambda: next_bar.timestamp_ms + 61_000

    class ReceiptClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.fromtimestamp(bot.strategy._now_ms() / 1000, UTC).astimezone(tz)

    # The real warmup predicate uses service datetime, not strategy._now_ms.
    # Bind both to this controlled receipt; never mark the symbol warm by hand.
    monkeypatch.setattr("project_mai_tai.services.schwab_1m_v2_bot.datetime", ReceiptClock)
    monkeypatch.setattr("project_mai_tai.market_data.keepwatch_history.read_keepwatch_history",
                        lambda *_args: response((*bars, next_bar), ()))
    bot._enqueue_keepwatch_bar("IZM", next_bar, "rest")
    owner = bot._keepwatch_bar_tasks["IZM"]
    try:
        await asyncio.wait_for(asyncio.shield(owner), 5)
        assert "IZM" in bot._rest_warmup_done
        assert not bot._boot_state_restoration_complete
        assert bot.strategy._entries_held
        assert other_inbox.pending[other.bar.timestamp_ms] is other
    finally:
        await bot._stop_keepwatch_bar_owners()


def test_direct_deferral_is_retryable_without_new_scanner_or_readiness_assignment():
    bot = controlled_bot()
    controlled_proof(bot)
    bot._sync_line_epochs()
    epoch = bot._line_sessions["IZM"].epoch
    inbox = bot._keepwatch_bar_inboxes["IZM"] = BarInbox(500)
    work = OwnedBar(candle(9), "streamer", ANCHOR, epoch)
    assert inbox.offer(work)
    bot._try_complete_boot_state_restoration(bot._watchlist, new_symbols=set())
    assert not bot._boot_state_restoration_complete and bot.strategy._entries_held
    assert bot._keepwatch_boot_retry_deferred_reason == "selected_owner_pending"
    inbox.complete(work)
    bot._cw_boot_hold_check()
    assert bot._boot_state_restoration_complete and not bot.strategy._entries_held
    assert not inbox.pending and inbox.consumed == 1
