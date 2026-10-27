"""Controlled stale callback boundaries; not historical/native acceptance."""

from dataclasses import replace
from datetime import UTC, datetime

import pytest

from project_mai_tai.services.keepwatch_bar_owner import BarConsumptionSuperseded
from tests.unit.test_keepwatch_scoped_boot_ordering import published_fleet
from tests.unit.test_keepwatch_session_producer_pin import candle


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["rest", "streamer", "direct"])
async def test_superseded_callback_cannot_rewrite_pin_ledger_or_warmup(route):
    bot, ledger, bars, raw, state, queued = await published_fleet()
    first = candle(40, close=6.0)
    revised = replace(first, close=5.99)
    bot.strategy._now_ms = lambda: first.timestamp_ms + 61_000
    bot._rest_warmup_done.discard("IZM")
    # Admit real work but inspect its callback within dispatch's actual context.
    # Suppress task scheduling only: no callback, pin or permission is mocked.
    from project_mai_tai.services.keepwatch_bar_owner import BarInbox, OwnedBar
    inbox = bot._keepwatch_bar_inboxes["IZM"] = BarInbox(500)
    pins = bot._keepwatch_pins_for("IZM", ledger.anchor_ms)
    pins.accept(first, now_ms=bot.strategy._now_ms())
    work = OwnedBar(first, "rest", ledger.anchor_ms, ledger.epoch)
    assert inbox.offer(work)
    before_bars = tuple(state.bars)
    before_counts = dict(bot._bar_counts)

    async def superseded_callback(symbol, prepared):
        replacement = OwnedBar(pins.accept(revised, now_ms=bot.strategy._now_ms()),
                               "rest", ledger.anchor_ms, ledger.epoch)
        assert inbox.offer(replacement)
        ledger.observe(replacement.bar)
        revision = ledger.revision
        with pytest.raises(BarConsumptionSuperseded):
            if route == "rest":
                await bot._handle_bar_from_rest(symbol, prepared, _keepwatch_prepared=True)
            elif route == "streamer":
                await bot._handle_bar_from_streamer(symbol, prepared, _keepwatch_prepared=True)
            else:
                await bot._handle_bar(symbol, prepared, observation_phase="live")
        assert pins.current(first.timestamp_ms).close == 5.99
        assert ledger.revision == revision
        assert inbox.pending[first.timestamp_ms] is replacement
        assert "IZM" not in bot._rest_warmup_done
        assert tuple(state.bars) == before_bars
        assert bot._bar_counts == before_counts
        assert bot.strategy._entries_held
        assert not bot.strategy._flip_owner_restore_readable
        # Preserve the actual dispatch refusal, not a swallowed stale callback.
        raise BarConsumptionSuperseded(symbol)

    assert not await bot._keepwatch_dispatch_bar(
        "IZM", first, superseded_callback, expected_work=work)
    assert inbox.pending[first.timestamp_ms].bar.close == 5.99
    assert inbox.consumed == inbox.covered == inbox.failures == 0
    assert "IZM" in bot._keepwatch_unknown


@pytest.mark.asyncio
async def test_unowned_callback_has_no_invented_owner_or_native_permission():
    bot, ledger, bars, raw, state, queued = await published_fleet()
    # No owner context exists: this is the unchanged direct-call contract.
    bot._keepwatch_fence_strategy_consumption("IZM")
    assert not bot._keepwatch_bar_inboxes
    assert bot.strategy._entries_held
    assert not bot.strategy._flip_owner_restore_readable


@pytest.mark.asyncio
@pytest.mark.parametrize("age_ms,expected", [(300_000, True), (300_001, False)])
async def test_controlled_service_clock_preserves_real_warmup_age_boundary(monkeypatch, age_ms, expected):
    bot, ledger, bars, raw, state, queued = await published_fleet()
    bar = candle(40, close=6.0)
    bot.strategy._now_ms = lambda: bar.timestamp_ms + 61_000
    bot._rest_warmup_done.discard("IZM")

    class ServiceClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.fromtimestamp((bar.timestamp_ms + age_ms) / 1000, UTC).astimezone(tz)

    monkeypatch.setattr("project_mai_tai.services.schwab_1m_v2_bot.datetime", ServiceClock)
    assert bot._mark_warmed_from_fresh_bar("IZM", bar, source="REST") is expected
    assert ("IZM" in bot._rest_warmup_done) is expected
    assert not bot._boot_state_restoration_complete and bot.strategy._entries_held
    assert not bot.strategy._flip_owner_restore_readable
