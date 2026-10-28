"""C11202 bounded boot census; controlled operands, not original-case acceptance."""

import asyncio
from dataclasses import replace
import json
import logging

import pytest

from project_mai_tai.services.keepwatch_bar_owner import BarInbox, OwnedBar
from project_mai_tai.services.schwab_1m_v2_bot import (
    KEEPWATCH_BOOT_DEFERRAL_INTERVALS, KEEPWATCH_BOOT_DEFERRAL_SECONDS,
    STATE_PUBLISH_INTERVAL_SECONDS,
)
from tests.unit.test_keepwatch_central_boot_retry import (
    assert_held, controlled_bot, controlled_proof,
)
from tests.unit.test_keepwatch_scoped_boot_ordering import one_pass_sleep, published_fleet
from tests.unit.test_keepwatch_session_producer_pin import ANCHOR, candle, response


def fleet():
    names = ("IZM",) + tuple("S" + chr(ord("A") + index) for index in range(17))
    bot = controlled_bot(symbols=names)
    bot._keepwatch.update(ANCHOR, set(names), bot.strategy._now_ms(),
                          {name: "short" for name in names}, retain_entries=True)
    for symbol in names:
        controlled_proof(bot, symbol)
    bot._sync_line_epochs()
    assert all(bot._keepwatch_entry_allowed(symbol) for symbol in names)
    clock = [100.0]
    bot._keepwatch_boot_retry_clock = lambda: clock[0]
    return bot, clock, names


def pending(bot, *, age_ms=120_000):
    now = bot.strategy._now_ms()
    stamp = ((now - age_ms + 59_999) // 60_000) * 60_000
    # Keep a real minute-aligned candle and move the controlled observation
    # forward at most one minute, never fabricate a fractional bar timestamp.
    bot.strategy._now_ms = lambda: stamp + age_ms
    bar = replace(candle(0), timestamp_ms=stamp)
    ledger = bot._line_sessions["IZM"]
    inbox = bot._keepwatch_bar_inboxes["IZM"] = BarInbox(500)
    work = OwnedBar(bar, "rest", ANCHOR, ledger.epoch)
    assert inbox.offer(work)
    return inbox, work


@pytest.mark.parametrize("age_ms,held", [(180_000, True), (180_001, False)])
def test_exact_existing_180s_age_boundary_is_not_relaxed(age_ms, held):
    bot, clock, names = fleet()
    inbox, work = pending(bot, age_ms=age_ms)
    bot._cw_boot_hold_check()
    assert bot.strategy._entries_held is held
    assert bot._boot_state_restoration_complete is (not held)
    assert inbox.pending[work.bar.timestamp_ms] is work  # No fake expiry/drain.
    assert ("IZM" in bot._keepwatch_boot_excluded_symbols()) is (not held)


def test_stale_one_of_18_releases_healthy_fleet_but_stays_unknown_with_warning(caplog):
    bot, clock, names = fleet()
    inbox, work = pending(bot, age_ms=180_001)
    with caplog.at_level(logging.WARNING):
        bot._cw_boot_hold_check()
        bot._cw_boot_hold_check()
    assert bot._boot_state_restoration_complete and not bot.strategy._entries_held
    assert bot._watchlist == set(names) and len(bot._subscription_symbols()) == 18
    assert not bot._keepwatch_entry_allowed("IZM") and "IZM" in bot._keepwatch_unknown
    assert all(bot._keepwatch_entry_allowed(name) for name in names if name != "IZM")
    assert inbox.pending[work.bar.timestamp_ms] is work and inbox.consumed == inbox.expired == 0
    warnings = [row for row in caplog.records if "[V2-BOOT-EXCLUDE]" in row.message]
    assert len(warnings) == 1 and warnings[0].levelno == logging.WARNING
    assert "symbol=IZM" in warnings[0].message and "reason=pending_work_stale" in warnings[0].message
    status = bot._keepwatch_boot_retry_state()["excluded"]["IZM"]
    assert status["status"] == "UNKNOWN" and status["entry_allowed"] is False
    # A later source cleanup cannot launder the sticky session quarantine.
    bot._keepwatch_unknown.discard("IZM")
    assert not bot._keepwatch_entry_allowed("IZM")


def test_12_elapsed_intervals_bound_same_symbol_not_call_count(caplog):
    bot, clock, names = fleet()
    inbox, work = pending(bot)
    assert KEEPWATCH_BOOT_DEFERRAL_INTERVALS == 12
    assert STATE_PUBLISH_INTERVAL_SECONDS == 5
    assert KEEPWATCH_BOOT_DEFERRAL_SECONDS == 60
    for interval in range(12):
        clock[0] = 100.0 + interval * 5
        assert_held(bot)
        assert not bot._keepwatch_boot_excluded_symbols()
        assert bot._keepwatch_boot_deferrals["IZM"]["intervals"] == interval
    clock[0] = 159.999
    assert_held(bot)
    clock[0] = 160.0
    with caplog.at_level(logging.WARNING):
        bot._cw_boot_hold_check()
    assert bot._boot_state_restoration_complete and not bot.strategy._entries_held
    assert bot._keepwatch_boot_retry_state()["excluded"]["IZM"]["elapsed_seconds"] == 60.0
    assert not bot._keepwatch_entry_allowed("IZM")
    assert inbox.pending[work.bar.timestamp_ms] is work
    assert "reason=consecutive_deferral_bound" in caplog.text


def test_many_same_clock_callbacks_do_not_spend_60s_budget():
    bot, clock, names = fleet()
    pending(bot)
    for _ in range(100):
        assert_held(bot)
    assert not bot._keepwatch_boot_excluded_symbols()
    assert bot._keepwatch_boot_deferrals["IZM"]["intervals"] == 0


def test_successful_quiescent_observation_breaks_consecutive_deferral_budget():
    bot, clock, names = fleet()
    inbox, work = pending(bot)
    assert_held(bot)
    bot._boot_exclusion_sources_readable = False  # Explicit unreadable input keeps boot closed.
    inbox.complete(work)
    clock[0] = 159.0
    assert_held(bot)
    assert "IZM" not in bot._keepwatch_boot_deferrals
    assert inbox.offer(work)
    assert_held(bot)
    clock[0] = 160.0
    assert_held(bot)
    assert not bot._keepwatch_boot_excluded_symbols()
    assert bot._keepwatch_boot_deferrals["IZM"]["started_at"] == 159.0


@pytest.mark.asyncio
async def test_queued_not_inflight_history_does_not_defer(monkeypatch):
    bot, ledger, bars, raw, state, queued = await published_fleet()
    next_bar = candle(40, close=6.0)
    bot.strategy._now_ms = lambda: next_bar.timestamp_ms + 61_000
    full = response((*bars, next_bar), ())
    monkeypatch.setattr("project_mai_tai.market_data.keepwatch_history.read_keepwatch_history",
                        lambda *_args: full)
    monkeypatch.setattr("project_mai_tai.services.schwab_1m_v2_bot.sleep_or_stop", one_pass_sleep)
    await bot._keepwatch_provider_lock.acquire()
    task = asyncio.create_task(bot._keepwatch_history_pass())
    try:
        await asyncio.sleep(0)
        assert not task.done() and not bot._keepwatch_history_scopes
        assert not bot._keepwatch_native_reads
        bot._cw_boot_hold_check()
        assert bot._boot_state_restoration_complete and not bot.strategy._entries_held
        assert bot._keepwatch_boot_retry_deferred_reason is None
    finally:
        bot._keepwatch_provider_lock.release()
        try:
            await asyncio.wait_for(asyncio.shield(task), 5)
            owners = tuple(bot._keepwatch_bar_tasks.values())
            await asyncio.wait_for(asyncio.gather(*(asyncio.shield(owner) for owner in owners)), 5)
        finally:
            await bot._stop_keepwatch_bar_owners()
    assert not bot._keepwatch_history_scopes and not bot._keepwatch_native_read_scopes


@pytest.mark.asyncio
async def test_native_scope_exists_before_set_membership(monkeypatch):
    bot, clock, names = fleet()
    raw = response(tuple(candle(index) for index in range(10)), ())
    checked = []

    class ObservedSet(set):
        def add(self, native):
            checked.append(bot._keepwatch_native_read_scopes[native])
            super().add(native)

    bot._keepwatch_native_reads = ObservedSet()
    monkeypatch.setattr("project_mai_tai.market_data.keepwatch_history.read_keepwatch_history",
                        lambda *_args: raw)
    assert await bot._read_keepwatch_history_owned("IZM", ANCHOR, raw.through_ms) is raw
    assert checked == [("IZM", ANCHOR, bot._line_sessions["IZM"].epoch)]
    assert not bot._keepwatch_native_reads and not bot._keepwatch_native_read_scopes


@pytest.mark.asyncio
async def test_actual_published_state_and_hold_warning_include_reason_symbol(monkeypatch, caplog):
    bot, clock, names = fleet()
    pending(bot)
    events = []

    class LocalTransport:
        async def xadd(self, stream, fields, **kwargs):
            events.append(json.loads(fields["data"]))
            return "CONTROLLED_LOCAL_ONLY"

    bot.redis = LocalTransport()
    # Report-only controlled local summary, not a native book or trading guard.
    monkeypatch.setattr(bot, "_fetch_reportable_state", lambda: {
        "positions": [], "pending_open": [], "pending_close": [],
        "daily_pnl": 0, "closed_today": []})
    with caplog.at_level(logging.WARNING):
        await bot._publish_bot_state()
    assert len(events) == 1
    retry = events[0]["payload"]["data_health"]["boot_retry"]
    assert retry["deferred_reason"] == "selected_owner_pending"
    assert retry["deferred_symbol"] == "IZM" and retry["bound_seconds"] == 60
    assert "deferred_reason=selected_owner_pending deferred_symbol=IZM" in caplog.text
    clock[0] = 160.0
    await bot._publish_bot_state()
    excluded = events[-1]["payload"]["data_health"]["boot_retry"]["excluded"]["IZM"]
    assert excluded["status"] == "UNKNOWN" and excluded["entry_allowed"] is False


def test_all_excluded_is_not_vacuous_fleet_completion():
    bot = controlled_bot()
    controlled_proof(bot)
    bot._sync_line_epochs()
    pending(bot, age_ms=180_001)
    assert_held(bot)
    assert bot._keepwatch_boot_excluded_symbols() == {"IZM"}
    assert not bot._keepwatch_entry_allowed("IZM")


def test_exclusion_does_not_waive_original_dangerous_arm_check():
    bot, clock, names = fleet()
    pending(bot, age_ms=180_001)
    state = bot.strategy.watchlist_state("IZM")
    bot.strategy._boot_ms = ANCHOR + 60_000
    state.cw_armed, state.cw_arm_bar_ts, state.cw_entries_this_flip = True, ANCHOR, 0
    bot._cw_boot_hold_check()
    assert bot._boot_state_restoration_complete
    assert bot.strategy._entries_held  # Original safety check remains authoritative.
    assert not bot._keepwatch_entry_allowed("IZM")


def test_session_scoped_exclusion_does_not_claim_next_session_unknown_as_ready():
    bot, clock, names = fleet()
    pending(bot, age_ms=180_001)
    bot._cw_boot_hold_check()
    assert "IZM" in bot._keepwatch_boot_excluded_symbols()
    bot.strategy._now_ms = lambda: ANCHOR + 86_400_000 + 11 * 60_000
    assert not bot._keepwatch_boot_excluded_symbols()
    assert not bot._keepwatch_entry_allowed("IZM")  # Old proof is NOT a new-session seed.


def test_excluding_one_symbol_does_not_forge_another_symbols_missing_seed():
    bot, clock, names = fleet()
    pending(bot, age_ms=180_001)
    bot._keepwatch_proven.pop("SA")
    assert_held(bot)
    assert "IZM" in bot._keepwatch_boot_excluded_symbols()
    assert "SA" not in bot._keepwatch_boot_excluded_symbols()
