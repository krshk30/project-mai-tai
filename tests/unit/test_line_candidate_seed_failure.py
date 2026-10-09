"""Provider failures cannot add an entry hold after the live fallback deadline."""
import asyncio
import time
from unittest.mock import Mock

import pytest

from tests.unit.test_line_chart_restoration_integration import _bot, _payload
from tests.unit.test_linesrc2_live_fallback import _failed_bot


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["unavailable", "slow", "empty"])
async def test_seed_failure_leaves_zero_additional_held_names(failure):
    bot, now, prefix, suffix = _failed_bot()
    bot.rest_client._authorized_get = Mock(return_value=_payload("RETO", prefix))
    if failure == "unavailable":
        bot._atr_massive_seed_client.fetch_preopen.side_effect = RuntimeError("provider unavailable")
    elif failure == "slow":
        bot._atr_massive_seed_timeout_seconds = 0.01

        def slow(*_):
            time.sleep(0.05)
            return ()

        bot._atr_massive_seed_client.fetch_preopen.side_effect = slow
    else:
        bot._atr_massive_seed_client.fetch_preopen.return_value = ()
    await bot._handle_bar_from_streamer("RETO", prefix[-1])
    await asyncio.wait_for(bot._line_source_events_pass(), 2)
    await bot._rebuild_session_line("RETO", bot._line_sessions["RETO"])
    now[0] += 60_000
    bot._line_repair_maintenance()
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
        await bot._rebuild_session_line("RETO", bot._line_sessions["RETO"])
        await control._handle_bar_from_streamer("RETO", bar)
        state = bot.strategy.watchlist_state("RETO")
        expected = control.strategy.watchlist_state("RETO")
        assert (state.atr_state, state.atr_trail, state.atr_state_age) == (
            expected.atr_state, expected.atr_trail, expected.atr_state_age)
        assert bot._line_buy_ready("RETO")
    assert bot._atr_massive_seed_client.fetch_preopen.call_count == 1
    print(f"LINE-SEED-FAILURE case={failure} names=1 held=0 bounded_deadline=60s")
