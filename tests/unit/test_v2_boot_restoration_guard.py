"""P1 boot hold: absence before initial state restoration is never safety."""

from __future__ import annotations

import logging

from project_mai_tai.events import (
    StrategyStateSnapshotEvent,
    StrategyStateSnapshotPayload,
)
from project_mai_tai.services.schwab_1m_v2_bot import SchwabV2BotService
from project_mai_tai.settings import Settings


def _bot() -> SchwabV2BotService:
    return SchwabV2BotService(
        settings=Settings(
            strategy_schwab_1m_v2_cw_armed_segment_safety_enabled=True,
        )
    )


def test_zero_segments_hold_until_restoration_then_release(monkeypatch) -> None:
    """The live 10.181s case: one completion bit makes both outcomes reachable.

    Before the scanner applies its initial snapshot, zero segments means zero restored states and
    must remain HELD. After the same zero result is backed by completed restoration, it releases.
    """
    bot = _bot()
    monkeypatch.setattr(bot.strategy, "cw_armed_segments", lambda: [])

    assert bot._boot_state_restoration_complete is False
    bot._cw_boot_hold_check()
    assert bot.strategy._entries_held is True

    bot._boot_state_restoration_complete = True
    bot._cw_boot_hold_check()
    assert bot.strategy._entries_held is False


def test_restoration_complete_with_one_dangerous_segment_stays_held(
    monkeypatch, caplog
) -> None:
    bot = _bot()
    bot._boot_state_restoration_complete = True
    bot.strategy._entries_held = False
    monkeypatch.setattr(
        bot.strategy,
        "cw_armed_segments",
        lambda: [
            {
                "symbol": "DAIC",
                "dangerous": True,
                "entries_this_flip": 0,
                "max_entries": 2,
            }
        ],
    )

    with caplog.at_level(logging.DEBUG):
        bot._cw_boot_hold_check()

    assert bot.strategy._entries_held is True
    held = [record for record in caplog.records if "[V2-BOOT-HOLD] HELD" in record.message]
    assert len(held) == 1
    assert held[0].levelno == logging.WARNING
    assert not [record for record in caplog.records if record.levelno >= logging.ERROR]


def test_current_snapshot_marks_restoration_complete_only_after_seed_replay(
    monkeypatch,
) -> None:
    bot = _bot()
    observed_during_seed: list[bool] = []

    def seed(_symbol: str) -> bool:
        observed_during_seed.append(bot._boot_state_restoration_complete)
        return True

    monkeypatch.setattr(bot, "_seed_strategy_bars_from_db", seed)
    event = StrategyStateSnapshotEvent(
        source_service="strategy-engine",
        payload=StrategyStateSnapshotPayload(watchlist=["DAIC"]),
    )

    bot._apply_strategy_state_event(
        {"data": event.model_dump_json()},
        max_watchlist=25,
    )

    assert observed_during_seed == [False]
    assert bot._boot_state_restoration_complete is True


def test_unreadable_restoration_source_cannot_release_empty_state(monkeypatch) -> None:
    bot = _bot()
    monkeypatch.setattr(bot, "_seed_strategy_bars_from_db", lambda _symbol: False)
    event = StrategyStateSnapshotEvent(
        source_service="strategy-engine",
        payload=StrategyStateSnapshotPayload(watchlist=["DAIC"]),
    )

    bot._apply_strategy_state_event(
        {"data": event.model_dump_json()},
        max_watchlist=25,
    )
    bot._cw_boot_hold_check()

    assert bot._boot_state_restoration_complete is False
    assert bot.strategy._entries_held is True
