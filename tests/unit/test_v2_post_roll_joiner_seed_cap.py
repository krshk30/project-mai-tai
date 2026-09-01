"""C42: the 04:00 session boundary must floor a joiner's seed-cap clock."""

from __future__ import annotations

import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from project_mai_tai.services.schwab_1m_v2_bot import SchwabV2BotService
from project_mai_tai.strategy_core.schwab_1m_v2 import session_start_ts_ms


ET = ZoneInfo("America/New_York")


def _ms(year: int, month: int, day: int, hour: int, minute: int = 0) -> int:
    return int(datetime(year, month, day, hour, minute, tzinfo=ET).timestamp() * 1000)


BOOT_MS = _ms(2026, 8, 31, 3)
NOW_MS = _ms(2026, 9, 1, 5)
SESSION_ANCHOR_MS = session_start_ts_ms(NOW_MS)
MAX_ENTRIES = 2


class _State:
    def __init__(self, arm_ts: int) -> None:
        self.cw_armed = True
        self.cw_arm_bar_ts = arm_ts
        self.cw_entries_this_flip = 0
        self.cw_resting_taken = False
        self.cw_reclaim_taken = False


class _Strategy:
    _cw_armed_segment_safety_enabled = True
    _cw_v2_max_entries_per_flip = MAX_ENTRIES

    def __init__(self, state: _State) -> None:
        self._boot_ms = BOOT_MS
        self._state = state

    @staticmethod
    def _now_ms() -> int:
        return NOW_MS

    def watchlist_state(self, symbol: str) -> _State:
        return self._state


def _bot(state: _State, watch_start: dict[str, int] | None = None) -> SchwabV2BotService:
    bot = object.__new__(SchwabV2BotService)
    bot.strategy = _Strategy(state)
    bot._watch_start_ms = watch_start or {}
    return bot


def test_post_roll_joiner_with_boot_fallback_caps_a_previous_session_arm(caplog) -> None:
    """The 09-01 shape: no 04:00 population, then a joiner replays an 08-31 arm."""

    stale = _State(_ms(2026, 8, 31, 10))
    bot = _bot(stale)  # no symbol stamp: the old code fell back to pre-session BOOT_MS

    with caplog.at_level(logging.INFO):
        bot._cap_reconstructed_segment("JOINER", stage="rest-warmup")

    assert stale.cw_entries_this_flip == MAX_ENTRIES
    assert stale.cw_resting_taken is True
    assert stale.cw_reclaim_taken is True
    line = next(
        record.getMessage()
        for record in caplog.records
        if "[V2-CW-SEED-CAP] JOINER" in record.getMessage()
    )
    assert f"session_anchor={SESSION_ANCHOR_MS}" in line


def test_post_roll_joiner_keeps_an_arm_observed_after_join() -> None:
    join_ms = _ms(2026, 9, 1, 4, 6)
    fresh = _State(_ms(2026, 9, 1, 4, 7))
    bot = _bot(fresh, {"JOINER": join_ms})

    bot._cap_reconstructed_segment("JOINER", stage="rest-warmup")

    assert fresh.cw_entries_this_flip == 0
    assert fresh.cw_resting_taken is False
    assert fresh.cw_reclaim_taken is False


def test_the_later_symbol_join_still_beats_the_session_anchor() -> None:
    join_ms = _ms(2026, 9, 1, 5, 30)
    pre_join = _State(_ms(2026, 9, 1, 5, 20))
    bot = _bot(pre_join, {"JOINER": join_ms})

    bot._cap_reconstructed_segment("JOINER", stage="rest-warmup")

    assert pre_join.cw_entries_this_flip == MAX_ENTRIES
    assert pre_join.cw_resting_taken is True
    assert pre_join.cw_reclaim_taken is True
