"""[codex] GAP+LINE ON with retained RETO bars; clocks/proofs/errors are controlled."""
import asyncio
from unittest.mock import Mock

import pytest

from project_mai_tai.market_data.schwab_v2_rest_client import SchwabV2RestClient
from tests.unit.test_gapline1 import _hold, _ohlcv, _seed
from tests.unit.test_line_chart_restoration_integration import _bars, _bot, _ingest, _ms, _proof


@pytest.mark.parametrize("status", ["published", "exhausted"])
@pytest.mark.asyncio
async def test_gap_event_budget_survives_finished_line_repair_and_does_not_restart_it(status):
    current = _ms("2026-10-05T11:06:00-04:00")
    bot = _bot("RETO", current, strategy_schwab_1m_v2_gap_hold_enabled=True,
               strategy_schwab_1m_v2_gap_line_carry_enabled=True)
    prefix = _bars("RETO", _ms("2026-10-05T10:48:00-04:00"))
    ledger = _ingest(bot, prefix, live=False)
    _hold(bot.strategy)
    repair = bot._line_repair_for("RETO")
    repair.attempts = 5 if status == "exhausted" else 1
    repair.published = status == "published"
    before = (repair.attempts, repair.next_attempt_ms, repair.published)
    assert not bot._queue_line_source_event("RETO", "reconfirmed_hole")
    first = next(bar for bar in _bars("RETO") if bar.timestamp_ms == current)
    state = bot.strategy.watchlist_state("RETO")
    bot.strategy._prepare_gap_hold_bar(state, _ohlcv(first))
    bot._wake_line_after_ingest("RETO")
    assert "RETO" in bot._line_source_pending
    assert bot._line_source_pending["RETO"][-1] == "gap_resume"
    identity = bot._line_source_identity["RETO"]
    bot._wake_line_after_ingest("RETO")
    assert bot._line_source_identity["RETO"] == identity
    bot.rest_client = Mock()
    bot.rest_client.fetch_session_history = Mock(side_effect=ValueError("controlled gap response failure"))
    await asyncio.wait_for(bot._line_source_events_pass(), 2)
    assert (repair.attempts, repair.next_attempt_ms, repair.published) == before
    assert ledger._coverage is None and not bot._line_buy_ready("RETO")
    for _ in range(3):
        bot._line_repair_maintenance()
        bot._wake_line_after_ingest("RETO")
    assert not bot._line_source_pending
    bot.rest_client.fetch_session_history.assert_called_once()


def test_shadow_plus_carry_advances_once_and_does_not_unconsume_or_trade(monkeypatch):
    strategy, state = _seed(line=True)
    control, control_state = _seed(line=False)
    _hold(strategy)
    state.line_live_shadow = True
    state.atr_fired_in_short_seg = True
    state.atr_guard = "FIRED"
    strategy._line_readiness = lambda _: False
    update = Mock(wraps=strategy._update_atr_state)
    monkeypatch.setattr(strategy, "_update_atr_state", update)
    first = next(bar for bar in _bars("RETO") if bar.timestamp_ms == _ms("2026-10-05T11:06:00-04:00"))
    control._update_atr_state(control_state, _ohlcv(first), state_only=True)
    strategy.on_observed_bar("RETO", first, observation_phase="live")
    assert update.call_count == 1 and update.call_args.kwargs == {"state_only": True}
    assert strategy._atr_indicator_snapshot(state) == control._atr_indicator_snapshot(control_state)
    assert state.atr_fired_in_short_seg and state.atr_guard == "FIRED"
    assert state.gap_hold_active and not strategy.line_buy_ready("RETO")
    assert not strategy._pending_intents and not strategy._pending_atr_sell_observations


@pytest.mark.asyncio
async def test_gap_repair_after_live_fallback_cannot_replay_a_historical_buy():
    current = _ms("2026-10-05T11:06:00-04:00")
    bot = _bot("RETO", current, strategy_schwab_1m_v2_gap_hold_enabled=True,
               strategy_schwab_1m_v2_gap_line_carry_enabled=True)
    ledger = _ingest(bot, _bars("RETO", _ms("2026-10-05T10:48:00-04:00")), live=False)
    _hold(bot.strategy)
    state = bot.strategy.watchlist_state("RETO")
    state.line_live_fallback = True
    first = next(bar for bar in _bars("RETO") if bar.timestamp_ms == current)
    _ingest(bot, [first])
    bot._wake_line_after_ingest("RETO")
    bars = _bars("RETO", current)
    # Real acceptance/worker path; completeness proof is controlled, not a historical envelope.
    bot.rest_client = Mock(spec=SchwabV2RestClient)
    bot.rest_client.fetch_session_history = Mock(return_value=(bars, _proof(ledger, bars)))
    await asyncio.wait_for(bot._line_source_events_pass(), 2)
    await bot._line_restoration_pass()
    assert "RETO" in bot._line_published and not state.line_live_fallback
    assert state.gap_hold_active and state.gap_hold_contiguous_bars == 1
    assert not bot._line_buy_ready("RETO")
    assert not bot.strategy._pending_intents and not bot.strategy._pending_webull_direct_intents
    bot.rest_client.fetch_session_history.assert_called_once()
