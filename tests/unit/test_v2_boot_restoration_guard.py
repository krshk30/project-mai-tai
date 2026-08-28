"""P1 boot hold: absence before initial state restoration is never safety."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace

from project_mai_tai.events import (
    StrategyStateSnapshotEvent,
    StrategyStateSnapshotPayload,
)
from project_mai_tai.services.schwab_1m_v2_bot import SchwabV2BotService
from project_mai_tai.settings import Settings


def _bot(**overrides) -> SchwabV2BotService:
    settings = {
        "strategy_schwab_1m_v2_cw_armed_segment_safety_enabled": True,
    }
    settings.update(overrides)
    return SchwabV2BotService(
        settings=Settings(**settings)
    )


def _apply_snapshot(
    bot: SchwabV2BotService,
    watchlist: list[str],
    *,
    rest_warmed: bool = True,
) -> None:
    if rest_warmed:
        bot._rest_warmup_done.update(watchlist)
    event = StrategyStateSnapshotEvent(
        source_service="strategy-engine",
        payload=StrategyStateSnapshotPayload(watchlist=watchlist),
    )
    bot._apply_strategy_state_event(
        {"data": event.model_dump_json()},
        max_watchlist=25,
    )


def test_zero_segments_hold_until_restoration_then_release(monkeypatch) -> None:
    """The live 10.181s case: one completion bit makes both outcomes reachable.

    Before the scanner applies its initial snapshot, zero segments means zero restored states and
    must remain HELD. After the same zero result is backed by completed restoration, it releases.
    """
    bot = _bot()
    bot.strategy._entries_held = False
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
    _apply_snapshot(bot, ["DAIC"])

    assert observed_during_seed == [False]
    assert bot._boot_state_restoration_complete is True


def test_unreadable_restoration_source_cannot_release_empty_state(monkeypatch) -> None:
    bot = _bot()
    bot.strategy._entries_held = False
    monkeypatch.setattr(bot, "_seed_strategy_bars_from_db", lambda _symbol: False)
    _apply_snapshot(bot, ["DAIC"])
    bot._cw_boot_hold_check()

    assert bot._boot_state_restoration_complete is False
    assert bot.strategy._entries_held is True


def test_empty_current_watchlist_cannot_vacuously_complete_restoration(
    monkeypatch, caplog
) -> None:
    """Mutant A: deleting the non-empty denominator guard must fail this control.

    Production emitted ``watchlist updated count=0`` on 2026-08-27. Python's ``all([])`` is True,
    but that population contains nothing whose restoration was checked.
    """
    bot = _bot()
    bot.strategy._entries_held = False
    monkeypatch.setattr(bot.strategy, "cw_armed_segments", lambda: [])

    with caplog.at_level(logging.DEBUG):
        _apply_snapshot(bot, [])
        bot._cw_boot_hold_check()

    assert bot._boot_state_restoration_complete is False
    assert bot.strategy._entries_held is True
    assert "restoration_complete=0 evaluated=0 confirmed=0" in caplog.text
    assert "scanner_population_observed=0" in caplog.text


def test_nonempty_scanner_reduced_to_zero_selected_cannot_complete(
    monkeypatch, caplog
) -> None:
    """D1: gate the actual evaluated population after exclusions, not the raw snapshot."""
    bot = _bot(protected_symbols="CYN")
    bot.strategy._entries_held = False
    monkeypatch.setattr(bot.strategy, "cw_armed_segments", lambda: [])
    monkeypatch.setattr(
        bot,
        "_seed_strategy_bars_from_db",
        lambda _symbol: (_ for _ in ()).throw(AssertionError("zero symbols must be seeded")),
    )

    with caplog.at_level(logging.DEBUG):
        _apply_snapshot(bot, ["CYN"])
        bot._cw_boot_hold_check()

    assert bot._boot_scanner_population_observed is True
    assert bot._watchlist == set()
    assert bot._boot_state_restoration_complete is False
    assert bot.strategy._entries_held is True
    assert "restoration_complete=0 evaluated=0 confirmed=0 rest_warmed=0" in caplog.text
    assert "scanner_population_observed=1" in caplog.text


def test_empty_scanner_with_held_symbol_is_not_a_nonempty_restoration_population(
    monkeypatch,
) -> None:
    """B1: protected/held union must not launder a live scanner count=0 snapshot."""
    bot = _bot()
    bot.strategy._entries_held = False
    monkeypatch.setattr(bot, "_protected_symbols", lambda: {"CYN2"})
    monkeypatch.setattr(bot, "_seed_strategy_bars_from_db", lambda _symbol: True)
    bot._rest_warmup_done.add("CYN2")

    _apply_snapshot(bot, [])
    bot._cw_boot_hold_check()

    assert bot._watchlist == {"CYN2"}
    assert bot._boot_scanner_population_observed is False
    assert bot._boot_state_restoration_complete is False
    assert bot.strategy._entries_held is True


def test_completed_restoration_is_a_one_way_latch_across_empty_refresh(
    monkeypatch,
) -> None:
    """Mutant B: assigning the completion result on every refresh must fail this control."""
    bot = _bot()
    monkeypatch.setattr(bot, "_seed_strategy_bars_from_db", lambda _symbol: True)
    monkeypatch.setattr(bot.strategy, "cw_armed_segments", lambda: [])

    _apply_snapshot(bot, ["DAIC"])
    assert bot._boot_state_restoration_complete is True

    # A later valid empty snapshot is ordinary watchlist turnover, not a second boot. It must not
    # re-arm the fleet-wide boot gate mid-session.
    _apply_snapshot(bot, [])
    bot._cw_boot_hold_check()

    assert bot._boot_state_restoration_complete is True
    assert bot.strategy._entries_held is False


def test_completed_restoration_is_one_way_across_nonempty_failed_addition(
    monkeypatch,
) -> None:
    """B2/M13: a later non-empty seed failure cannot reassign the completed boot fact."""
    bot = _bot()
    results = iter([True, False])
    monkeypatch.setattr(bot, "_seed_strategy_bars_from_db", lambda _symbol: next(results))
    monkeypatch.setattr(bot.strategy, "cw_armed_segments", lambda: [])

    _apply_snapshot(bot, ["DAIC"])
    assert bot._boot_state_restoration_complete is True

    _apply_snapshot(bot, ["YYGH"])
    bot._cw_boot_hold_check()

    assert bot._boot_state_restoration_complete is True
    assert bot.strategy._entries_held is False


def test_same_watchlist_retries_unreadable_restoration_until_confirmed(
    monkeypatch,
) -> None:
    bot = _bot()
    results = iter([False, True])
    monkeypatch.setattr(bot, "_seed_strategy_bars_from_db", lambda _symbol: next(results))

    _apply_snapshot(bot, ["DAIC"])
    assert bot._boot_state_restoration_complete is False

    _apply_snapshot(bot, ["DAIC"])
    assert bot._boot_state_restoration_complete is True


def test_db_seed_is_not_enough_until_rest_warmup_finishes(monkeypatch) -> None:
    """B3: REST replay can arm after DB seed; it is part of the release precondition."""
    bot = _bot()
    bot.strategy._entries_held = False
    monkeypatch.setattr(bot, "_seed_strategy_bars_from_db", lambda _symbol: True)

    _apply_snapshot(bot, ["DAIC"], rest_warmed=False)
    bot._cw_boot_hold_check()
    assert bot._boot_state_restoration_complete is False
    assert bot.strategy._entries_held is True

    bot._rest_warmup_done.add("DAIC")
    bot._try_complete_boot_state_restoration(bot._watchlist, new_symbols=set())
    bot._cw_boot_hold_check()
    assert bot._boot_state_restoration_complete is True
    assert bot.strategy._entries_held is False


def test_pre_restoration_hold_warning_is_rate_limited(monkeypatch, caplog) -> None:
    bot = _bot()
    bot.strategy._entries_held = False
    moments = iter([100.0, 105.0, 161.0])
    monkeypatch.setattr(
        "project_mai_tai.services.schwab_1m_v2_bot.time.monotonic",
        lambda: next(moments),
    )

    with caplog.at_level(logging.WARNING):
        bot._cw_boot_hold_check()
        bot._cw_boot_hold_check()
        bot._cw_boot_hold_check()

    held = [record for record in caplog.records if "[V2-BOOT-HOLD] HELD" in record.message]
    assert len(held) == 2
    assert bot.strategy._entries_held is True


def test_pre_restoration_hold_names_dangerous_symbols(monkeypatch, caplog) -> None:
    bot = _bot()
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
            },
            {
                "symbol": "YYGH",
                "dangerous": True,
                "entries_this_flip": 0,
                "max_entries": 2,
            },
        ],
    )

    with caplog.at_level(logging.WARNING):
        bot._cw_boot_hold_check()

    assert bot.strategy._entries_held is True
    assert "dangerous_observed=2 dangerous_symbols=DAIC,YYGH" in caplog.text


class _SeedSession:
    def __init__(self, rows, *, fail_query: bool = False) -> None:
        self.rows = rows
        self.fail_query = fail_query

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, *_args, **_kwargs):
        if self.fail_query:
            raise RuntimeError("db unavailable")
        return SimpleNamespace(
            scalars=lambda: SimpleNamespace(all=lambda: list(self.rows))
        )


def _seed_row() -> SimpleNamespace:
    return SimpleNamespace(
        bar_time=datetime(2026, 8, 27, 14, 0, tzinfo=UTC),
        open_price=Decimal("1"),
        high_price=Decimal("1"),
        low_price=Decimal("1"),
        close_price=Decimal("1"),
        volume=100,
    )


def test_real_seed_path_confirmed_empty_is_success() -> None:
    bot = _bot()
    bot.session_factory = lambda: _SeedSession([])

    assert bot._seed_strategy_bars_from_db("EMPTY") is True
    assert "EMPTY" in bot._db_seeded


def test_real_seed_path_missing_session_and_query_failure_are_not_success() -> None:
    bot = _bot()
    bot.session_factory = None
    assert bot._seed_strategy_bars_from_db("NONE") is False
    assert "NONE" not in bot._db_seeded

    bot.session_factory = lambda: _SeedSession([], fail_query=True)
    assert bot._seed_strategy_bars_from_db("FAIL") is False
    assert "FAIL" not in bot._db_seeded


def test_real_seed_path_replay_failure_is_retryable_not_half_restored(monkeypatch) -> None:
    bot = _bot()
    rows = [_seed_row()]
    bot.session_factory = lambda: _SeedSession(rows)
    monkeypatch.setattr(bot, "_truncate_seed_rows_at_gap", lambda _session, _symbol, got: got)
    monkeypatch.setattr(bot, "_strategy_on_bar", lambda *_args, **_kwargs: (_ for _ in ()).throw(
        RuntimeError("replay failed")
    ))

    assert bot._seed_strategy_bars_from_db("HALF") is False
    assert "HALF" not in bot._db_seeded

    monkeypatch.setattr(bot, "_strategy_on_bar", lambda *_args, **_kwargs: None)
    assert bot._seed_strategy_bars_from_db("HALF") is True
    assert "HALF" in bot._db_seeded
