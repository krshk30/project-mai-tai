"""Recorded candles, controlled provider failures; no historical response claim."""
import asyncio
import json
import logging
from datetime import datetime
from copy import deepcopy
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest

from project_mai_tai.market_data.line_repair import LineRepair
from project_mai_tai.market_data.schwab_v2_rest_client import ChartBar, SchwabV2RestClient
from tests.unit.test_line_chart_restoration_integration import _bars, _bot, _ingest, _ms, _payload
from tests.unit.test_linesrc1_event_driven import _scanner


def _failed_bot():
    bars = _bars("RETO")
    prefix = bars[:40]
    bot = _bot("RETO", prefix[-1].timestamp_ms)
    bot.strategy._atr_probe_all = True
    now = [prefix[-1].timestamp_ms + 61_000]
    bot.strategy._now_ms = lambda: now[0]
    _ingest(bot, prefix, live=False)
    bot._rest_warmup_done.add("RETO")
    bot._persist_bar = Mock()
    bot.rest_client = SchwabV2RestClient(bot.settings, on_chart_bar=AsyncMock(), on_quote=AsyncMock())
    bot._request_line_repairs(_scanner(bot, {"RETO"}))
    return bot, now, prefix, bars[40:]


def _damage(payload, branch, current):
    if branch == "malformed":
        payload["candles"][0] = None
    elif branch == "missing_price":
        del payload["candles"][0]["close"]
    elif branch == "bad_price":
        payload["candles"][0]["close"] = "not-a-number"
    elif branch == "invalid_range":
        payload["candles"][0]["high"] = -1
    elif branch == "duplicate":
        payload["candles"].append(deepcopy(payload["candles"][0]))
    elif branch == "foreign":
        payload["candles"][0]["datetime"] = current + 60_000
    elif branch == "foreign_symbol":
        payload["symbol"] = "OTHER"
    elif branch == "incomplete":
        payload["truncated"] = True
    elif branch == "missing_empty":
        del payload["empty"]
    elif branch == "non_list":
        payload["candles"] = {}
    elif branch == "oversized":
        payload["candles"] = payload["candles"] * 961
    elif branch == "contradictory_empty":
        payload["empty"] = True
    else:
        raise AssertionError(branch)
    return payload


@pytest.mark.asyncio
@pytest.mark.parametrize("branch", ["malformed", "missing_price", "bad_price", "invalid_range",
                                    "duplicate", "foreign", "foreign_symbol", "incomplete",
                                    "missing_empty", "non_list", "oversized", "contradictory_empty"])
async def test_each_parser_rejection_logs_shape_and_publishes_legacy_live_line(branch, caplog):
    caplog.set_level(logging.INFO)
    bot, now, prefix, suffix = _failed_bot()
    payload = _damage(_payload("RETO", prefix), branch, prefix[-1].timestamp_ms)
    bot.rest_client._authorized_get = Mock(return_value=payload)
    await bot._handle_bar_from_streamer("RETO", prefix[-1])
    await asyncio.wait_for(bot._line_source_events_pass(), 2)
    assert bot.rest_client._line_source_states["RETO"][0] == "error"
    assert "exception=" in caplog.text and "response_shape=" in caplog.text
    assert '"candle_count"' in caplog.text and '"envelope_flags"' in caplog.text
    state = bot.strategy.watchlist_state("RETO")
    assert not state.line_live_fallback and not bot._line_buy_ready("RETO")
    repair = bot._line_repairs["RETO"]
    now[0] = repair.first_closed_at_ms + 59_999
    bot._line_repair_maintenance()
    assert not state.line_live_fallback
    now[0] += 1
    bot._line_repair_maintenance()
    assert state.line_live_fallback

    control = _bot("RETO", prefix[-1].timestamp_ms,
                   strategy_schwab_1m_v2_line_chart_restoration_enabled=False)
    control.strategy._now_ms = lambda: now[0]
    for bar in prefix:
        control.strategy.on_observed_bar("RETO", bar, observation_phase="replay")
    control._rest_warmup_done.add("RETO")
    control._persist_bar = Mock()
    for bar in suffix[:12]:
        now[0] = bar.timestamp_ms + 61_000
        await bot._handle_bar_from_streamer("RETO", bar)
        await control._handle_bar_from_streamer("RETO", bar)
        expected = control.strategy.watchlist_state("RETO")
        assert (state.atr_state, state.atr_trail, state.atr_state_age) == (
            expected.atr_state, expected.atr_trail, expected.atr_state_age)
        assert bot._line_buy_ready("RETO")
    assert "[V2-ATR-PROBE]" in caplog.text


@pytest.mark.asyncio
async def test_missing_current_candle_is_waiting_not_error_then_falls_back(caplog):
    bot, now, prefix, _ = _failed_bot()
    bot.rest_client._authorized_get = Mock(return_value=_payload("RETO", prefix[:-1]))
    await bot._handle_bar_from_streamer("RETO", prefix[-1])
    await asyncio.wait_for(bot._line_source_events_pass(), 2)
    assert bot.rest_client._line_source_states["RETO"][0] == "waiting"
    assert "state=error" not in caplog.text
    now[0] += 60_000
    bot._line_repair_maintenance()
    assert bot.strategy.watchlist_state("RETO").line_live_fallback


@pytest.mark.asyncio
async def test_repair_retries_five_at_one_minute_then_afterhours_no_requests(caplog):
    bot, now, prefix, _ = _failed_bot()
    bot.rest_client.fetch_session_history = Mock(side_effect=ValueError("controlled incomplete envelope"))
    await bot._handle_bar_from_streamer("RETO", prefix[-1])
    for attempt in range(5):
        await asyncio.wait_for(bot._line_source_events_pass(), 2)
        assert bot.rest_client.fetch_session_history.call_count == attempt + 1
        now[0] += 59_999
        bot._line_repair_maintenance()
        assert not bot._line_source_pending
        now[0] += 1
        bot._line_repair_maintenance()
    assert not bot._line_source_pending
    now[0] = _ms("2026-10-05T20:00:00-04:00")
    for _ in range(360):
        now[0] += 5_000
        bot._line_repair_maintenance()
    assert bot.rest_client.fetch_session_history.call_count == 5
    assert "anchored poll failed" not in caplog.text


def test_repair_budget_deadline_not_started_without_closed_bar():
    repair = LineRepair(1, 1, "first_0700_closed")
    assert not repair.fallback_due(100_000_000)
    repair.first_closed_at_ms = 1000
    assert not repair.fallback_due(60_999)
    assert repair.fallback_due(61_000)
    repair.published = True
    assert not repair.fallback_due(61_000)


@pytest.mark.asyncio
async def test_live_callback_fallback_keeps_gap_and_ownership_barriers(monkeypatch):
    bot, now, prefix, suffix = _failed_bot()
    await bot._handle_bar_from_streamer("RETO", prefix[-1])
    now[0] += 60_000
    await bot._handle_bar_from_streamer("RETO", suffix[0])
    state = bot.strategy.watchlist_state("RETO")
    assert state.line_live_fallback and bot.strategy.line_buy_ready("RETO")
    monkeypatch.setattr(bot.strategy, "gap_hold_active", lambda symbol: True)
    assert not bot.strategy.line_buy_ready("RETO")
    monkeypatch.setattr(bot.strategy, "gap_hold_active", lambda symbol: False)
    monkeypatch.setattr(bot.strategy, "_removed_wait_gate_closed", lambda symbol: True)
    assert not bot.strategy.line_buy_ready("RETO")
    monkeypatch.setattr(bot.strategy, "_removed_wait_gate_closed", lambda symbol: False)
    bot._watchlist.clear()
    assert not bot.strategy.line_buy_ready("RETO")


@pytest.mark.asyncio
async def test_late_repair_never_calls_live_trading_path_after_fallback(monkeypatch):
    bot, now, prefix, suffix = _failed_bot()
    await bot._handle_bar_from_streamer("RETO", prefix[-1])
    now[0] += 60_000
    bot._line_repair_maintenance()
    state = bot.strategy.watchlist_state("RETO")
    assert state.line_live_fallback
    now[0] = suffix[0].timestamp_ms + 61_000
    states = []
    original = bot._strategy_on_bar

    def observe(*args, **kwargs):
        states.append(state.line_live_fallback)
        return original(*args, **kwargs)

    monkeypatch.setattr(bot, "_strategy_on_bar", observe)
    ledger = bot._line_sessions["RETO"]
    from tests.unit.test_line_chart_restoration_integration import _proof
    bars = prefix + suffix[:1]
    assert bot._accept_line_source("RETO", ledger.epoch, bars, _proof(ledger, bars))
    assert states == [False]
    assert state.line_live_fallback


@pytest.mark.asyncio
async def test_afterhours_remaining_retry_budget_makes_no_http_calls(caplog):
    bot, now, prefix, _ = _failed_bot()
    bot.rest_client.fetch_session_history = Mock(side_effect=ValueError("controlled response"))
    await bot._handle_bar_from_streamer("RETO", prefix[-1])
    await asyncio.wait_for(bot._line_source_events_pass(), 2)
    assert bot._line_repairs["RETO"].attempts == 1
    now[0] = _ms("2026-10-05T20:00:00-04:00")
    for _ in range(360):
        now[0] += 5_000
        bot._line_repair_maintenance()
        assert not bot._line_source_pending
    assert bot.rest_client.fetch_session_history.call_count == 1
    assert "anchored poll failed" not in caplog.text


@pytest.mark.asyncio
async def test_retry_spacing_is_from_dispatch_not_from_earlier_queue():
    bot, now, prefix, _ = _failed_bot()
    bot.rest_client.fetch_session_history = Mock(side_effect=ValueError("controlled response"))
    await bot._handle_bar_from_streamer("RETO", prefix[-1])
    now[0] += 59_000
    await asyncio.wait_for(bot._line_source_events_pass(), 2)
    now[0] += 1_000
    bot._line_repair_maintenance()
    assert not bot._line_source_pending
    now[0] += 59_000
    bot._line_repair_maintenance()
    assert bot._line_source_pending


@pytest.mark.asyncio
async def test_recorded_oct8_bars_with_0701_error_publish_without_fabricated_prices(caplog):
    caplog.set_level(logging.INFO)
    raw = json.loads((Path(__file__).parents[1] / "fixtures" / "linesrc2_morning_20261008.json").read_text())
    bots = {}
    controls = {}
    now = [0]
    for row in sorted(raw, key=lambda row: row["created_at"]):
        symbol = row["symbol"]
        now[0] = int(datetime.fromisoformat(row["created_at"]).timestamp() * 1000)
        if symbol not in bots:
            bot = _bot(symbol, int(row["timestamp_ms"]))
            bot.strategy._atr_probe_all = True
            bot.strategy._now_ms = lambda: now[0]
            bot._persist_bar = Mock()
            bot._rest_warmup_done.add(symbol)
            bot.rest_client = SchwabV2RestClient(bot.settings, on_chart_bar=AsyncMock(), on_quote=AsyncMock())
            bot.rest_client.fetch_session_history = Mock(side_effect=ValueError("controlled 07:01 repair failure"))
            bot._request_line_repairs(_scanner(bot, {symbol}))
            bots[symbol] = bot
            control = _bot(symbol, int(row["timestamp_ms"]),
                           strategy_schwab_1m_v2_line_chart_restoration_enabled=False)
            control.strategy._now_ms = lambda: now[0]
            control._persist_bar = Mock()
            control._rest_warmup_done.add(symbol)
            controls[symbol] = control
        bot = bots[symbol]
        bar = ChartBar(symbol, *(float(row[key]) for key in (
            "open_price", "high_price", "low_price", "close_price")),
            int(row["volume"]), int(row["timestamp_ms"]))
        await bot._handle_bar_from_streamer(symbol, bar)
        await controls[symbol]._handle_bar_from_streamer(symbol, bar)
        state = bot.strategy.watchlist_state(symbol)
        expected = controls[symbol].strategy.watchlist_state(symbol)
        assert (state.atr_state, state.atr_trail, state.atr_state_age) == (
            expected.atr_state, expected.atr_trail, expected.atr_state_age)
        if bot._line_source_pending:
            await asyncio.wait_for(bot._line_source_events_pass(), 2)
    assert {"DKI", "AIXI"} <= bots.keys()
    assert all(bot.strategy.watchlist_state(sym).line_live_fallback for sym, bot in bots.items()
               if len(bot.strategy.watchlist_state(sym).bars) >= 2)
    assert all(bot.strategy.watchlist_state(sym).atr_state in {"long", "short"}
               for sym, bot in bots.items() if len(bot.strategy.watchlist_state(sym).bars) >= 10)
    probes = [record for record in caplog.records if "[V2-ATR-PROBE]" in record.getMessage()]
    print(f"LINESRC2 recorded Oct8: rows={len(raw)} symbols={len(bots)} probes={len(probes)} "
          "response_failure=controlled; historical_envelopes=UNMEASURED")
    assert probes
