"""[codex] Independent-review reproductions, recorded prices and controlled I/O only."""
import asyncio
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime
from unittest.mock import AsyncMock, Mock, patch

import pytest

from project_mai_tai.events import StrategyStateSnapshotEvent, StrategyStateSnapshotPayload
from project_mai_tai.market_data.schwab_v2_rest_client import SchwabV2RestClient
from project_mai_tai.strategy_core.session_line_restore import build_session_line
from tests.unit.test_line_chart_restoration_integration import _bars, _bot, _ingest, _ms, _payload
from tests.unit.test_linesrc1_event_driven import _scanner, _seed


@contextmanager
def _clock(now):
    class RecordedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            value = datetime.fromtimestamp(now[0] / 1000, UTC)
            return value.astimezone(tz) if tz is not None else value.replace(tzinfo=None)

    with patch("project_mai_tai.services.schwab_1m_v2_bot.datetime", RecordedDateTime), patch(
        "project_mai_tai.strategy_core.schwab_1m_v2.datetime", RecordedDateTime,
    ):
        yield


async def _sparse_provider_seed():
    bars = _bars("RETO", _ms("2026-10-05T11:05:00-04:00"))
    # A controlled sparse provider envelope with a real retained candle withheld.
    # Positive tape below is a controlled query result, not a historical receipt.
    bars = [bar for bar in bars if bar.timestamp_ms != _ms("2026-10-05T10:55:00-04:00")]
    bot = _bot("RETO", bars[-1].timestamp_ms)
    now = [bars[-1].timestamp_ms + 61_000]
    bot.strategy._now_ms = lambda: now[0]
    _ingest(bot, bars, live=False)
    bot._rest_warmup_done.add("RETO")
    bot._persist_bar = lambda *args: None
    bot.rest_client = SchwabV2RestClient(bot.settings, on_chart_bar=bot._handle_bar_from_rest,
                                      on_quote=AsyncMock())
    bot.rest_client._authorized_get = Mock(return_value=_payload("RETO", bars))
    await bot._handle_bar_from_streamer("RETO", bars[-1])
    bot._request_line_repairs(_scanner(bot, {"RETO"}))
    await bot._line_source_events_pass()
    return bot, now, bars


@pytest.mark.asyncio
@pytest.mark.parametrize("stale_at", ["stored", "tape"])
async def test_stale_reconciliation_does_not_skip_positive_tape_or_mark_db_complete(stale_at):
    bot, now, bars = await _sparse_provider_seed()
    ledger = bot._line_sessions["RETO"]
    pairs = ledger.gap_pairs()
    assert len(pairs) == 1
    bot.session_factory = object()
    stored, tape = Mock(return_value=[]), Mock(return_value=list(pairs))
    bot._read_line_session_bars, bot._read_line_gap_trades = stored, tape
    started, release = asyncio.Event(), asyncio.Event()
    real = asyncio.to_thread
    target = stored if stale_at == "stored" else tape
    blocked_once = False

    async def controlled_thread(fn, *args):
        nonlocal blocked_once
        if fn is target and not blocked_once:
            blocked_once = True
            started.set()
            await release.wait()
        return await real(fn, *args)

    with patch("project_mai_tai.services.schwab_1m_v2_bot.asyncio.to_thread", controlled_thread):
        worker = asyncio.create_task(bot._line_restoration_pass())
        await asyncio.wait_for(started.wait(), 2)
        first = _bars("RETO", bars[-1].timestamp_ms + 60_000)[-1]
        now[0] = first.timestamp_ms + 61_000
        await bot._handle_bar_from_streamer("RETO", first)
        release.set()
        await worker
    assert ("RETO", ledger.epoch) not in bot._line_db_checked
    assert "RETO" in bot._line_reconcile_pending
    for bar in [row for row in _bars("RETO") if row.timestamp_ms > first.timestamp_ms]:
        now[0] = bar.timestamp_ms + 61_000
        await bot._handle_bar_from_streamer("RETO", bar)
        await bot._line_restoration_pass()
    assert tape.call_count >= (2 if stale_at == "tape" else 1)
    assert ledger.prepare() is None and ledger.incomplete_reason == "traded_gap_unrecovered"
    assert not bot._line_buy_ready("RETO") and bot.rest_client._authorized_get.call_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["streamer", "rest"])
async def test_persist_paused_callback_rewakes_after_strategy_ingest_and_delivers_sell_once(source):
    bot, now, bars = await _seed(clock="2026-10-05T11:24:00-04:00")
    live = _bars("RETO", _ms("2026-10-05T11:25:00-04:00"))[-1]
    assert live.timestamp_ms == bars[-1].timestamp_ms + 60_000
    previous = bot._line_published["RETO"]
    bot._drain_atr_sell_observations = AsyncMock()  # Retain real generated observations.
    started, release = asyncio.Event(), asyncio.Event()
    real = asyncio.to_thread

    async def controlled_thread(fn, *args):
        if fn is bot._persist_bar:
            started.set()
            await release.wait()
            return None
        return await real(fn, *args)

    now[0] = live.timestamp_ms + 61_000
    with _clock(now), patch("project_mai_tai.services.schwab_1m_v2_bot.asyncio.to_thread", controlled_thread):
        handler = bot._handle_bar_from_streamer if source == "streamer" else bot._handle_bar_from_rest
        callback = asyncio.create_task(handler("RETO", live))
        await asyncio.wait_for(started.wait(), 2)
        await bot._line_restoration_pass()
        assert bot._line_published["RETO"] is previous and not bot._line_buy_ready("RETO")
        release.set()
        await callback
    # No helper dirty injection: the real callback must leave this notification.
    assert "RETO" in bot._line_dirty
    await bot._line_restoration_pass()
    assert bot._line_published["RETO"].request.current_bar_ms == live.timestamp_ms
    assert bot._line_buy_ready("RETO")
    assert len(bot.strategy._pending_atr_sell_observations) == 1
    bot._line_dirty.add("RETO")  # An explicit duplicate worker wake, not the recovery wake.
    await bot._line_restoration_pass()
    assert len(bot.strategy._pending_atr_sell_observations) == 1
    assert bot.rest_client._authorized_get.call_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["streamer", "rest"])
async def test_validated_suffix_correction_preserves_seed_and_never_replays_old_sell(source):
    bot, now, bars = await _seed(clock="2026-10-05T11:24:00-04:00", database=True)
    ledger = bot._line_sessions["RETO"]
    seed = ledger._coverage
    bot._drain_atr_sell_observations = AsyncMock()
    live = _bars("RETO", _ms("2026-10-05T11:25:00-04:00"))[-1]
    now[0] = live.timestamp_ms + 61_000
    handler = bot._handle_bar_from_streamer if source == "streamer" else bot._handle_bar_from_rest
    await handler("RETO", live)
    await bot._line_restoration_pass()
    assert len(bot.strategy._pending_atr_sell_observations) == 1
    # Counterfactual volume correction to a retained live candle, not a raw receipt.
    corrected = replace(live, volume=live.volume + 1)
    await handler("RETO", corrected)
    assert not bot._line_buy_ready("RETO")
    await bot._line_restoration_pass()
    assert ledger._coverage is seed and not ledger._seed_invalidated
    assert bot._line_buy_ready("RETO")
    oracle = build_session_line(ledger.prepare(), bot.strategy._atr_period, bot.strategy._atr_factor)
    assert bot._line_published["RETO"].snapshot == oracle
    assert len(bot.strategy._pending_atr_sell_observations) == 1
    assert bot.rest_client._authorized_get.call_count == 1 and not bot._line_source_pending
    assert bot._read_line_session_bars.call_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("constructed", ["06:58:00", "07:00:00"])
async def test_initial_0701_scanner_without_candles_waits_then_empty_repairs_are_bounded(constructed):
    bot = _bot("RETO", _ms(f"2026-10-05T{constructed}-04:00"))
    now = [_ms("2026-10-05T07:01:00-04:00")]
    bot.strategy._now_ms = lambda: now[0]
    bot._persist_bar = lambda *args: None
    bot.rest_client = SchwabV2RestClient(bot.settings, on_chart_bar=bot._handle_bar_from_rest,
                                      on_quote=AsyncMock())
    # Counterfactual empty RETO envelope; the four measured empty symbols remain
    # separate receipts. No RETO-empty historical claim or invented OHLC.
    bot.rest_client._authorized_get = Mock(return_value={"symbol": "RETO", "empty": True, "candles": []})
    with _clock(now):
        event = StrategyStateSnapshotEvent(
            source_service="strategy-engine", produced_at=datetime.fromtimestamp(now[0] / 1000, UTC),
            payload=StrategyStateSnapshotPayload(top_confirmed=[{"symbol": "RETO", "confirmed_at": "07:01:00"}]),
        )
        bot._apply_strategy_state_event({"data": event.model_dump_json()}, max_watchlist=25)
        assert not bot._line_source_pending
        bot.rest_client._authorized_get.assert_not_called()
        # RETO's actual retained first candle is 07:13, not an invented 07:00 bar.
        first = _bars("RETO")[0]
        assert first.timestamp_ms == _ms("2026-10-05T07:13:00-04:00")
        now[0] = first.timestamp_ms + 61_000
        await bot._handle_bar_from_streamer("RETO", first)
        await bot._line_source_events_pass()
        assert bot.rest_client._authorized_get.call_count == 1 and not bot._line_source_pending
        for bar in _bars("RETO")[1:12]:
            now[0] = bar.timestamp_ms + 61_000
            await bot._handle_bar_from_streamer("RETO", bar)
            event.produced_at = datetime.fromtimestamp(now[0] / 1000, UTC)
            bot._apply_strategy_state_event({"data": event.model_dump_json()}, max_watchlist=25)
            if bot._line_source_pending:
                await asyncio.wait_for(bot._line_source_events_pass(), 2)
            assert bot.rest_client._authorized_get.call_count <= 5
            assert bot.strategy.watchlist_state("RETO").line_live_fallback
    assert bot.rest_client._authorized_get.call_count == 5
