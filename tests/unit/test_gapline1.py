"""GAPLINE1 controls using retained RETO candles; hold timing is controlled."""

import asyncio
import threading
from datetime import datetime
from unittest.mock import AsyncMock, Mock

import pytest

from project_mai_tai.settings import Settings
from project_mai_tai.strategy_core.schwab_1m_v2 import OHLCVBar, SchwabV2Strategy
from tests.unit.test_line_chart_restoration_integration import (
    _bars, _bot, _ingest, _ms, _proof,
)


def _ohlcv(bar):
    return OHLCVBar(bar.timestamp_ms, bar.open, bar.high, bar.low, bar.close, bar.volume)


def _seed(*, carry=True, line=False, short=False):
    bars = _bars("RETO", None if short else _ms("2026-10-05T10:48:00-04:00"))
    strategy = SchwabV2Strategy(Settings(
        strategy_schwab_1m_v2_gap_hold_enabled=True,
        strategy_schwab_1m_v2_gap_line_carry_enabled=carry,
        strategy_schwab_1m_v2_line_chart_restoration_enabled=line,
    ))
    state = strategy.watchlist_state("RETO")
    for bar in bars:
        strategy._update_atr_state(state, _ohlcv(bar), state_only=True)
    strategy._now_ms = lambda: _ms("2026-10-05T11:30:01-04:00" if short
                                 else "2026-10-05T11:06:01-04:00")
    return strategy, state


def _hold(strategy):
    assert strategy.begin_gap_hold(
        "RETO", detected_at_ms=strategy._now_ms(),
        last_bar_age_s=1001, last_print_age_s=1,
    )


@pytest.mark.parametrize("line", [False, True])
@pytest.mark.parametrize("short", [False, True])
def test_gapline_initial_hold_preserves_entire_indicator_and_consumed_segment(line, short):
    strategy, state = _seed(line=line, short=short)
    snapshot = strategy._atr_indicator_snapshot(state)
    assert snapshot["atr_state"] == ("short" if short else "long")
    state.atr_fired_in_short_seg = True
    state.atr_guard = "FIRED"
    _hold(strategy)
    assert strategy._atr_indicator_snapshot(state) == snapshot
    assert state.atr_fired_in_short_seg is True
    assert state.atr_guard == "FIRED"
    assert state.line_restore_reset_after_ms == 0
    assert state.gap_hold_active and state.gap_hold_contiguous_bars == 0


def test_gapline_recovery_gap_preserves_current_math_but_restarts_clean_bar_wait():
    strategy, state = _seed(line=True)
    _hold(strategy)
    bars = _bars("RETO")
    after = [bar for bar in bars if bar.timestamp_ms >= _ms("2026-10-05T11:06:00-04:00")]
    for bar in after[:4]:
        strategy._prepare_gap_hold_bar(state, _ohlcv(bar))
    snapshot = strategy._atr_indicator_snapshot(state)
    token = state.gap_line_repair_token
    strategy._prepare_gap_hold_bar(state, _ohlcv(after[5]))
    assert strategy._atr_indicator_snapshot(state) == snapshot
    assert state.gap_line_repair_token != token
    assert state.gap_hold_contiguous_bars == 1
    assert not strategy._maybe_resume_gap_hold(state)


def test_gapline_resume_requires_ten_real_contiguous_bars():
    strategy, state = _seed(line=True)
    _hold(strategy)
    after = [bar for bar in _bars("RETO")
             if bar.timestamp_ms >= _ms("2026-10-05T11:06:00-04:00")]
    for index, bar in enumerate(after[:10], 1):
        strategy._prepare_gap_hold_bar(state, _ohlcv(bar))
        assert strategy._maybe_resume_gap_hold(state) is (index == 10)
        assert state.gap_hold_active is (index < 10)
    assert state.gap_hold_contiguous_bars == 10


@pytest.mark.parametrize("line", [False, True])
def test_gapline_off_retains_initial_and_recovery_reseed(line):
    strategy, state = _seed(carry=False, line=line)
    _hold(strategy)
    assert state.atr_state is None and state.atr_trail is None
    if line:
        assert state.line_restore_reset_after_ms == strategy._now_ms()
    first = _ohlcv(_bars("RETO")[-3])
    strategy._prepare_gap_hold_bar(state, first)
    state.atr_state = "short"
    state.atr_trail = 2.1
    strategy._prepare_gap_hold_bar(state, _ohlcv(_bars("RETO")[-1]))
    assert state.atr_state is None and state.atr_trail is None


def test_gapline_carry_does_not_change_session_anchor_reset():
    strategy, state = _seed()
    tomorrow = int(datetime.fromisoformat("2026-10-06T04:00:00-04:00").timestamp() * 1000)
    strategy._now_ms = lambda: tomorrow
    strategy._apply_session_anchor_reset(state, tomorrow)
    assert state.atr_session_anchor_ms == tomorrow
    assert state.atr_state is None and state.atr_trail is None


@pytest.mark.asyncio
async def test_gapline_first_live_bar_queues_one_offloop_repair_per_hold_and_recovery():
    prefix = _bars("RETO", _ms("2026-10-05T10:48:00-04:00"))
    current = _ms("2026-10-05T11:06:00-04:00")
    bot = _bot("RETO", current, strategy_schwab_1m_v2_gap_hold_enabled=True,
               strategy_schwab_1m_v2_gap_line_carry_enabled=True)
    _ingest(bot, prefix, live=False)
    _hold(bot.strategy)
    state = bot.strategy.watchlist_state("RETO")
    first = next(bar for bar in _bars("RETO") if bar.timestamp_ms == current)
    bot.strategy._prepare_gap_hold_bar(state, _ohlcv(first))
    bot._wake_line_after_ingest("RETO")
    assert bot._line_source_pending["RETO"][-1] == "gap_resume"
    identity = bot._line_source_identity["RETO"]
    bot._wake_line_after_ingest("RETO")
    assert bot._line_source_identity["RETO"] == identity
    bars = _bars("RETO", current)
    proof = _proof(bot._line_sessions["RETO"], bars)
    threads = []
    main = threading.get_ident()

    def fetch(*args):
        threads.append(threading.get_ident())
        return bars, proof

    bot.rest_client = Mock()
    bot.rest_client.fetch_session_history = Mock(side_effect=fetch)
    await asyncio.wait_for(bot._line_source_events_pass(), 2)
    assert len(threads) == 1 and threads[0] != main
    second = next(bar for bar in _bars("RETO") if bar.timestamp_ms == current + 120_000)
    bot.strategy._now_ms = lambda: second.timestamp_ms + 61_000
    bot.strategy._prepare_gap_hold_bar(state, _ohlcv(second))
    bot._wake_line_after_ingest("RETO")
    assert bot._line_source_pending["RETO"][-1] == "gap_resume"
    assert bot._line_source_identity["RETO"] == identity + 1


@pytest.mark.asyncio
async def test_gapline_recorded_reto_backfill_repairs_without_replaying_a_buy():
    prefix = _bars("RETO", _ms("2026-10-05T10:48:00-04:00"))
    current = _ms("2026-10-05T11:06:00-04:00")
    bot = _bot("RETO", current, strategy_schwab_1m_v2_gap_hold_enabled=True,
               strategy_schwab_1m_v2_gap_line_carry_enabled=True)
    ledger = _ingest(bot, prefix, live=False)
    ledger.attest(_proof(ledger, prefix))
    await bot._rebuild_session_line("RETO", ledger)
    before = bot.strategy._atr_indicator_snapshot(bot.strategy.watchlist_state("RETO"))
    _hold(bot.strategy)
    state = bot.strategy.watchlist_state("RETO")
    assert bot.strategy._atr_indicator_snapshot(state) == before
    first = next(bar for bar in _bars("RETO") if bar.timestamp_ms == current)
    _ingest(bot, [first])
    bot._wake_line_after_ingest("RETO")
    bars = _bars("RETO", current)
    bot.rest_client = Mock()
    bot.rest_client.fetch_session_history = Mock(return_value=(bars, _proof(ledger, bars)))
    bot._maybe_emit = AsyncMock()
    await asyncio.wait_for(bot._line_source_events_pass(), 2)
    await bot._line_restoration_pass()
    from project_mai_tai.strategy_core.session_line_restore import build_session_line

    expected = build_session_line(ledger.prepare(), 5, 3.5)
    assert state.atr_state == dict(expected.indicator)["atr_state"]
    assert state.atr_state_age == dict(expected.indicator)["atr_state_age"]
    assert state.atr_trail == dict(expected.indicator)["atr_trail"]
    assert not bot._line_buy_ready("RETO")
    assert state.gap_hold_active and state.gap_hold_contiguous_bars == 1
    assert all(call.args == (None,) for call in bot._maybe_emit.call_args_list)


def test_gapline_incomplete_line_advances_carry_without_admission_or_slot_unconsuming():
    strategy, state = _seed(line=True)
    control, control_state = _seed(line=False)
    _hold(strategy)
    state.atr_fired_in_short_seg = True
    state.atr_guard = "FIRED"
    strategy._line_readiness = lambda symbol: False
    bars = [bar for bar in _bars("RETO")
            if bar.timestamp_ms >= _ms("2026-10-05T11:06:00-04:00")]
    for bar in bars[:10]:
        control._update_atr_state(control_state, _ohlcv(bar), state_only=True)
        strategy.on_observed_bar("RETO", bar, observation_phase="live")
        assert strategy._atr_indicator_snapshot(state) == control._atr_indicator_snapshot(control_state)
        assert not strategy.line_buy_ready("RETO")
        assert not strategy._pending_intents
        assert not strategy._pending_webull_direct_intents
        assert not strategy._pending_webull_fanout_intents
        assert not strategy._pending_atr_sell_observations
        assert state.atr_fired_in_short_seg is True
        assert state.atr_guard == "FIRED"
    assert not state.gap_hold_active


@pytest.mark.asyncio
async def test_gapline_late_recorded_reto_interior_bar_refreshes_proof_not_live_tails_or_duplicates():
    current = _ms("2026-10-05T11:06:00-04:00")
    bot = _bot("RETO", current, strategy_schwab_1m_v2_gap_hold_enabled=True,
               strategy_schwab_1m_v2_gap_line_carry_enabled=True)
    prefix = _bars("RETO", _ms("2026-10-05T10:48:00-04:00"))
    ledger = _ingest(bot, prefix, live=False)
    _hold(bot.strategy)
    first = next(bar for bar in _bars("RETO") if bar.timestamp_ms == current)
    _ingest(bot, [first])
    bot._wake_line_after_ingest("RETO")
    sparse = [bar for bar in _bars("RETO", current)
              if not _ms("2026-10-05T10:49:00-04:00") <= bar.timestamp_ms
              <= _ms("2026-10-05T11:04:00-04:00")]
    # Positive tape admission is a controlled fence, not a recovered tape receipt.
    ledger.mark_traded_pair((_ms("2026-10-05T10:48:00-04:00"), _ms("2026-10-05T11:05:00-04:00")))
    bot.rest_client = Mock()
    bot.rest_client.fetch_session_history = Mock(return_value=(sparse, _proof(ledger, sparse)))
    await asyncio.wait_for(bot._line_source_events_pass(), 2)
    for bar in [bar for bar in _bars("RETO") if current < bar.timestamp_ms <= current + 9 * 60_000]:
        bot.strategy._now_ms = lambda bar=bar: bar.timestamp_ms + 61_000
        _ingest(bot, [bar])
        bot._wake_line_after_ingest("RETO")
        await bot._line_restoration_pass()
    assert not bot.strategy.gap_hold_active("RETO")
    assert not bot._line_buy_ready("RETO")
    assert bot.rest_client.fetch_session_history.call_count == 1
    late = next(bar for bar in _bars("RETO") if bar.timestamp_ms == _ms("2026-10-05T10:49:00-04:00"))
    bot._observe_line_bar("RETO", late, source_callback=True)
    assert bot._line_source_pending["RETO"][-1] == "gap_resume"
    complete = _bars("RETO", current + 9 * 60_000)
    bot.rest_client.fetch_session_history.return_value = (complete, _proof(ledger, complete))
    await asyncio.wait_for(bot._line_source_events_pass(), 2)
    await bot._line_restoration_pass()
    assert bot._line_buy_ready("RETO")
    assert not bot.strategy.watchlist_state("RETO").gap_line_carry_pending
    for _ in range(3):
        bot._observe_line_bar("RETO", late, source_callback=True)
    assert not bot._line_source_pending
    assert bot.rest_client.fetch_session_history.call_count == 2


@pytest.mark.asyncio
async def test_gapline_held_exit_coverage_repairs_but_cannot_admit_a_buy():
    current = _ms("2026-10-05T11:06:00-04:00")
    bot = _bot("RETO", current, strategy_schwab_1m_v2_gap_hold_enabled=True,
               strategy_schwab_1m_v2_gap_line_carry_enabled=True)
    bot._watchlist.clear()
    bot._exit_coverage = {"RETO"}
    bot._sync_line_epochs()
    _ingest(bot, _bars("RETO", _ms("2026-10-05T10:48:00-04:00")), live=False)
    _hold(bot.strategy)
    first = next(bar for bar in _bars("RETO") if bar.timestamp_ms == current)
    _ingest(bot, [first])
    bot._wake_line_after_ingest("RETO")
    assert bot._line_source_pending["RETO"][-1] == "gap_resume"
    ledger = bot._line_sessions["RETO"]
    bars = _bars("RETO", current)
    bot.rest_client = Mock()
    bot.rest_client.fetch_session_history = Mock(return_value=(bars, _proof(ledger, bars)))
    await asyncio.wait_for(bot._line_source_events_pass(), 2)
    await bot._line_restoration_pass()
    assert "RETO" in bot._line_published
    assert not bot._line_buy_ready("RETO")
    bot.rest_client.fetch_session_history.assert_called_once()


def test_gapline_default_off_catalog_on():
    import json
    from pathlib import Path

    assert Settings().strategy_schwab_1m_v2_gap_line_carry_enabled is False
    catalog = json.loads((Path(__file__).parents[2] / "ops/health/expected_flags.json").read_text())
    entry = next(row for row in catalog["flags"]
                 if row["name"] == "strategy_schwab_1m_v2_gap_line_carry_enabled")
    assert entry["expected"] is True
    assert entry["owning_service"] == "schwab-1m-v2"
