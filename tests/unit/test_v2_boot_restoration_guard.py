"""P1 boot hold: absence before initial state restoration is never safety."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest

from project_mai_tai.events import (
    StrategyStateSnapshotEvent,
    StrategyStateSnapshotPayload,
)
from project_mai_tai.market_data.schwab_v2_rest_client import ChartBar
from project_mai_tai.services.schwab_1m_v2_bot import SchwabV2BotService
from project_mai_tai.settings import Settings


class _ReadableEmptySession:
    """A readable eligibility/seed database with no matching rows."""

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def scalar(self, *_args, **_kwargs):
        return None

    def execute(self, *_args, **_kwargs):
        return SimpleNamespace(
            scalars=lambda: SimpleNamespace(all=lambda: [])
        )


def _bot(**overrides) -> SchwabV2BotService:
    settings = {
        "strategy_schwab_1m_v2_cw_armed_segment_safety_enabled": True,
    }
    settings.update(overrides)
    bot = SchwabV2BotService(
        settings=Settings(**settings)
    )
    bot.session_factory = lambda: _ReadableEmptySession()
    return bot


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
    assert held[0].levelno == logging.ERROR


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
    assert "restoration_complete=0 evaluated=0" in caplog.text
    assert "confirmed=unknown rest_warmed=unknown" in caplog.text
    assert "scanner_evaluated=0" in caplog.text


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

    assert bot._boot_scanner_selected == set()
    assert bot._watchlist == set()
    assert bot._boot_state_restoration_complete is False
    assert bot.strategy._entries_held is True
    assert "restoration_complete=0 evaluated=0" in caplog.text
    assert "confirmed=unknown rest_warmed=unknown" in caplog.text
    assert "scanner_evaluated=0" in caplog.text


def test_schwab_ineligible_exclusion_removes_the_scanner_denominator(monkeypatch) -> None:
    """The population used for restoration must be the same post-exclusion population selected."""
    bot = _bot()
    monkeypatch.setattr(bot, "_schwab_ineligible_symbols", lambda: {"DAIC"})

    _apply_snapshot(bot, ["DAIC"])

    assert bot._watchlist == set()
    assert bot._boot_scanner_selected == set()
    assert bot._boot_state_restoration_complete is False


def test_empty_scanner_with_held_symbol_is_not_a_nonempty_restoration_population(
    monkeypatch, caplog
) -> None:
    """B1: protected/held union must not launder a live scanner count=0 snapshot."""
    bot = _bot()
    bot.strategy._entries_held = False
    monkeypatch.setattr(bot, "_protected_symbols", lambda: {"CYN2"})
    monkeypatch.setattr(bot, "_seed_strategy_bars_from_db", lambda _symbol: True)
    bot._rest_warmup_done.add("CYN2")

    with caplog.at_level(logging.WARNING):
        _apply_snapshot(bot, [])
        bot._cw_boot_hold_check()

    assert bot._watchlist == {"CYN2"}
    assert bot._boot_scanner_selected == set()
    assert bot._boot_state_restoration_complete is False
    assert bot.strategy._entries_held is True
    assert "scanner_evaluated=0 evaluated=1" in caplog.text
    assert "confirmed=unknown rest_warmed=unknown" in caplog.text


def test_prior_excluded_snapshot_cannot_launder_later_held_only_population(
    monkeypatch,
) -> None:
    """D1 exploit: identical held-only end state cannot depend on an earlier snapshot."""
    bot = _bot()
    bot.strategy._entries_held = False
    seed_results = iter([False, True])
    monkeypatch.setattr(
        bot,
        "_seed_strategy_bars_from_db",
        lambda _symbol: next(seed_results),
    )

    # Pass 1 is deliberately non-empty and unreadable. If the current-snapshot assignment below
    # is mutated back to a once-ever union (``|=``), DAIC survives into pass 2 and launders the
    # held-only population into a completed restoration.
    _apply_snapshot(bot, ["DAIC"])
    assert bot._boot_scanner_selected == {"DAIC"}
    assert bot._boot_state_restoration_complete is False

    monkeypatch.setattr(bot, "_protected_symbols", lambda: {"HELD1"})
    bot._rest_warmup_done.add("HELD1")
    _apply_snapshot(bot, [])
    bot._cw_boot_hold_check()

    assert bot._watchlist == {"HELD1"}
    assert bot._boot_scanner_selected == set()
    assert bot._boot_state_restoration_complete is False
    assert bot.strategy._entries_held is True


@pytest.mark.parametrize(
    ("schwab_result", "webull_result", "unreadable_reason"),
    [
        ({"DAIC"}, {"DAIC"}, None),
        (None, {"DAIC"}, "schwab_ineligible_unreadable"),
        ({"DAIC"}, None, "webull_ineligible_unreadable"),
    ],
)
def test_ineligible_population_must_be_readable_before_boot_release(
    monkeypatch,
    caplog,
    schwab_result,
    webull_result,
    unreadable_reason,
) -> None:
    """Readable exclusions and unreadable exclusions both keep the zero-population boot held."""
    bot = _bot(
        strategy_schwab_1m_v2_dual_broker_fanout_enabled=True,
        strategy_schwab_1m_v2_webull_account_name="live:orb",
    )
    bot.strategy._entries_held = False
    monkeypatch.setattr(bot, "_schwab_ineligible_symbols", lambda: schwab_result)
    monkeypatch.setattr(bot, "_webull_ineligible_symbols", lambda: webull_result)
    monkeypatch.setattr(bot.strategy, "cw_armed_segments", lambda: [])

    with caplog.at_level(logging.WARNING):
        _apply_snapshot(bot, ["DAIC"])
        bot._cw_boot_hold_check()

    expected_selected = set() if unreadable_reason is None else {"DAIC"}
    assert bot._watchlist == expected_selected
    assert bot._boot_scanner_selected == expected_selected
    assert bot._boot_state_restoration_complete is False
    assert bot.strategy._entries_held is True
    if unreadable_reason is not None:
        assert "reason=ineligible_exclusion_unreadable" in caplog.text
        assert "snapshot_applied=1" in caplog.text
        assert "last_known_exclusions_applied=0" in caplog.text


def test_unreadable_exclusion_telemetry_counts_the_last_known_set(monkeypatch, caplog) -> None:
    bot = _bot()
    bot._schwab_ineligible_cache = {"DAIC", "XOS"}
    monkeypatch.setattr(bot, "_schwab_ineligible_symbols", lambda: None)

    with caplog.at_level(logging.WARNING):
        _apply_snapshot(bot, ["DAIC", "XOS", "YYGH"])

    assert "last_known_exclusions_applied=2" in caplog.text


def test_unreadable_exclusion_without_new_symbols_reports_unknown_counts(
    monkeypatch, caplog
) -> None:
    """A same-watchlist retry evaluated no new seed reads, so 0/0 is not a measurement."""
    bot = _bot()
    bot._boot_exclusion_sources_readable = False
    bot._boot_scanner_selected = {"DAIC"}
    monkeypatch.setattr(
        bot,
        "_seed_strategy_bars_from_db",
        lambda _symbol: (_ for _ in ()).throw(
            AssertionError("an empty new-symbol population must not be seeded")
        ),
    )

    with caplog.at_level(logging.WARNING):
        bot._try_complete_boot_state_restoration({"DAIC"}, new_symbols=set())

    assert "evaluated=1 confirmed=unknown could_not_tell=unknown" in caplog.text
    assert "reason=ineligible_exclusion_unreadable" in caplog.text
    assert "confirmed=0 could_not_tell=0" not in caplog.text


def test_ineligible_loader_query_failure_is_not_cached_empty() -> None:
    bot = _bot()
    bot._schwab_ineligible_cache = set()

    def fail_factory():
        raise RuntimeError("db unavailable")

    bot.session_factory = fail_factory

    assert bot._schwab_ineligible_symbols() is None


def test_webull_ineligible_loader_query_failure_is_not_cached_empty() -> None:
    bot = _bot(
        strategy_schwab_1m_v2_dual_broker_fanout_enabled=True,
        strategy_schwab_1m_v2_webull_account_name="live:orb",
    )
    bot._webull_ineligible_cache = set()

    def fail_factory():
        raise RuntimeError("db unavailable")

    bot.session_factory = fail_factory

    assert bot._webull_ineligible_symbols() is None


def test_missing_session_factory_is_unreadable_for_both_ineligible_loaders() -> None:
    bot = _bot(
        strategy_schwab_1m_v2_dual_broker_fanout_enabled=True,
        strategy_schwab_1m_v2_webull_account_name="live:orb",
    )
    bot.session_factory = None

    assert bot._schwab_ineligible_symbols() is None
    assert bot._webull_ineligible_symbols() is None


def test_post_latch_exclusion_blip_applies_snapshot_without_reholding_entries(
    monkeypatch, caplog
) -> None:
    """Fail closed on exclusion evidence, not by freezing scanner turnover mid-session."""
    bot = _bot(
        strategy_schwab_1m_v2_dual_broker_fanout_enabled=True,
        strategy_schwab_1m_v2_webull_account_name="live:orb",
    )
    bot._boot_state_restoration_complete = True
    bot.strategy._entries_held = False
    bot._watchlist = {"DAIC", "XOS"}
    bot._rest_warmup_done = {"DAIC", "XOS"}
    released: list[str] = []
    monkeypatch.setattr(bot, "_schwab_ineligible_symbols", lambda: None)
    monkeypatch.setattr(bot, "_webull_ineligible_symbols", lambda: None)
    monkeypatch.setattr(bot, "_seed_strategy_bars_from_db", lambda _symbol: True)
    monkeypatch.setattr(
        bot.strategy,
        "release_and_drop_symbol",
        lambda symbol: released.append(symbol),
    )

    with caplog.at_level(logging.WARNING):
        _apply_snapshot(bot, ["AAPL", "TSLA"])

    assert bot._watchlist == {"AAPL", "TSLA"}
    assert released == ["DAIC", "XOS"]
    assert bot._boot_state_restoration_complete is True
    assert bot.strategy._entries_held is False
    assert "restoration_complete=1" in caplog.text
    assert "snapshot_applied=1" in caplog.text


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


def test_post_latch_new_symbol_stays_streamer_gated_until_rest_warmup(
    monkeypatch,
) -> None:
    """A later symbol cannot enter by riding the fleet latch past its REST gate."""
    bot = _bot()
    seed_results = iter([True, False])
    monkeypatch.setattr(
        bot,
        "_seed_strategy_bars_from_db",
        lambda _symbol: next(seed_results),
    )
    fed: list[str] = []

    async def record(symbol: str, _bar: ChartBar, *, observation_phase: str) -> None:
        fed.append(f"{symbol}:{observation_phase}")

    monkeypatch.setattr(bot, "_handle_bar", record)

    _apply_snapshot(bot, ["DAIC"])
    assert bot._boot_state_restoration_complete is True

    _apply_snapshot(bot, ["DAIC", "YYGH"], rest_warmed=False)
    assert "YYGH" not in bot._rest_warmup_done
    bar = ChartBar("YYGH", 1.0, 1.0, 1.0, 1.0, 100, 1_788_000_000_000)
    asyncio.run(bot._handle_bar_from_streamer("YYGH", bar))

    assert fed == []
    assert bot._streamer_pending["YYGH"] == [bar]

    # The same symbol becomes reachable after the one variable this gate owns changes: REST
    # warmup. The failed seed is reported but does not invent a separate silent gate.
    bot._rest_warmup_done.add("YYGH")
    asyncio.run(bot._handle_bar_from_streamer("YYGH", bar))
    assert fed == ["YYGH:live"]


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


def test_db_seed_is_not_enough_until_rest_warmup_finishes(monkeypatch, caplog) -> None:
    """B3: REST replay can arm after DB seed; it is part of the release precondition."""
    bot = _bot()
    bot.strategy._entries_held = False
    monkeypatch.setattr(bot, "_seed_strategy_bars_from_db", lambda _symbol: True)

    with caplog.at_level(logging.WARNING):
        _apply_snapshot(bot, ["DAIC"], rest_warmed=False)
        bot._cw_boot_hold_check()
    assert bot._boot_state_restoration_complete is False
    assert bot.strategy._entries_held is True
    assert "reason=rest_warmup_incomplete" in caplog.text
    assert "warmup_pending=1 warmup_pending_symbols=DAIC" in caplog.text

    bot._rest_warmup_done.add("DAIC")
    bot._try_complete_boot_state_restoration(bot._watchlist, new_symbols=set())
    bot._cw_boot_hold_check()
    assert bot._boot_state_restoration_complete is True
    assert bot.strategy._entries_held is False


def test_rest_warmup_wave_is_rate_limited_with_a_population_denominator(
    monkeypatch, caplog
) -> None:
    """Twenty-five warmup callbacks produce one WARN, not a log-masking 25-line wave."""
    bot = _bot()
    symbols = {f"SYM{i:02d}" for i in range(25)}
    bot.strategy._entries_held = True
    monkeypatch.setattr(bot, "_schwab_ineligible_symbols", lambda: set())
    monkeypatch.setattr(bot, "_seed_strategy_bars_from_db", lambda _symbol: True)
    monkeypatch.setattr(
        "project_mai_tai.services.schwab_1m_v2_bot.time.monotonic",
        lambda: 100.0,
    )

    with caplog.at_level(logging.INFO):
        _apply_snapshot(bot, sorted(symbols), rest_warmed=False)
        for symbol in sorted(symbols):
            bot._rest_warmup_done.add(symbol)
            bot._try_complete_boot_state_restoration(bot._watchlist, new_symbols=set())

    incomplete = [
        record
        for record in caplog.records
        if "reason=rest_warmup_incomplete" in record.message
    ]
    assert len(incomplete) == 1
    assert "evaluated=25 confirmed=25" in incomplete[0].message
    assert "rest_warmed=0 warmup_pending=25" in incomplete[0].message
    assert "restoration_complete=1 evaluated=25 confirmed=25 rest_warmed=25" in caplog.text


def test_every_incomplete_restoration_reason_is_rate_limited_independently(
    monkeypatch, caplog
) -> None:
    """Each 25-callback wave emits one warning; a changed reason is visible immediately."""
    bot = _bot()
    selected = {"DAIC"}
    monkeypatch.setattr(
        "project_mai_tai.services.schwab_1m_v2_bot.time.monotonic",
        lambda: 100.0,
    )

    def run_wave(reason: str) -> None:
        before = len(
            [record for record in caplog.records if f"reason={reason}" in record.message]
        )
        for _ in range(25):
            bot._try_complete_boot_state_restoration(selected, new_symbols=set())
        after = len(
            [record for record in caplog.records if f"reason={reason}" in record.message]
        )
        assert after - before == 1

    with caplog.at_level(logging.WARNING):
        bot._boot_exclusion_sources_readable = False
        run_wave("ineligible_exclusion_unreadable")

        bot._boot_exclusion_sources_readable = True
        selected = set()
        run_wave("empty_evaluated_population_after_exclusions")

        selected = {"DAIC"}
        bot._boot_scanner_selected = set()
        run_wave("empty_tradeable_scanner_population")

        bot._boot_scanner_selected = {"DAIC"}
        monkeypatch.setattr(bot, "_seed_strategy_bars_from_db", lambda _symbol: False)
        run_wave("state_seed_incomplete")

        monkeypatch.setattr(bot, "_seed_strategy_bars_from_db", lambda _symbol: True)
        bot._rest_warmup_done.clear()
        run_wave("rest_warmup_incomplete")


def test_real_rest_warmup_callback_completes_the_latch(monkeypatch) -> None:
    """Pin the production ordering: DB seed first, then the fresh REST callback opens the latch."""
    bot = _bot()
    bot._watchlist = {"DAIC"}
    bot._boot_scanner_selected = {"DAIC"}
    bot._boot_exclusion_sources_readable = True
    bot._db_seeded = {"DAIC"}
    bot.strategy._entries_held = True

    async def no_feed(*_args, **_kwargs) -> None:
        return None

    monkeypatch.setattr(bot, "_handle_bar", no_feed)
    monkeypatch.setattr(bot, "_drain_streamer_pending", no_feed)
    monkeypatch.setattr(bot, "_cap_reconstructed_segment", lambda *_args, **_kwargs: None)
    fresh = ChartBar(
        "DAIC",
        1.0,
        1.0,
        1.0,
        1.0,
        100,
        int(datetime.now(UTC).timestamp() * 1000),
    )

    asyncio.run(bot._handle_bar_from_rest("DAIC", fresh))
    bot._cw_boot_hold_check()

    assert bot._rest_warmup_done == {"DAIC"}
    assert bot._boot_state_restoration_complete is True
    assert bot.strategy._entries_held is False


def test_flag_off_preserves_seed_without_hold_or_restoration_snapshot(monkeypatch) -> None:
    bot = _bot(strategy_schwab_1m_v2_cw_armed_segment_safety_enabled=False)
    seeded: list[str] = []
    monkeypatch.setattr(
        bot,
        "_seed_strategy_bars_from_db",
        lambda symbol: seeded.append(symbol) or True,
    )

    _apply_snapshot(bot, ["DAIC"])
    bot.strategy._entries_held = False
    bot._cw_boot_hold_check()

    assert seeded == ["DAIC"]
    assert bot._boot_state_restoration_complete is False
    assert bot.strategy._entries_held is False

    written: list[dict] = []

    class _Redis:
        async def xadd(self, _stream, fields, **_kwargs):
            written.append(fields)

    bot.redis = _Redis()
    asyncio.run(bot._publish_bot_state())
    payload = __import__("json").loads(written[0]["data"])["payload"]
    assert "restoration_complete" not in payload
    assert "warmup_pending_symbols" not in payload


def test_state_payload_names_each_warmup_pending_symbol(monkeypatch) -> None:
    bot = _bot()
    bot._watchlist = {"DAIC", "YYGH", "XOS"}
    bot._rest_warmup_done = {"XOS"}
    written: list[dict] = []

    class _Redis:
        async def xadd(self, _stream, fields, **_kwargs):
            written.append(fields)

    bot.redis = _Redis()
    monkeypatch.setattr(
        bot,
        "_fetch_reportable_state",
        lambda: {
            "positions": [],
            "pending_open": [],
            "pending_close": [],
            "daily_pnl": 0.0,
            "closed_today": [],
        },
    )

    asyncio.run(bot._publish_bot_state())

    payload = __import__("json").loads(written[0]["data"])["payload"]
    assert payload["warmup_pending_symbols"] == ["DAIC", "YYGH"]


def test_pydantic_accepts_one_as_enabled_like_the_pager_parser() -> None:
    """Regression: Settings accepts ``1`` as true, so the live pager parser must as well."""
    settings = Settings(strategy_schwab_1m_v2_cw_armed_segment_safety_enabled="1")
    assert settings.strategy_schwab_1m_v2_cw_armed_segment_safety_enabled is True


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
    assert {record.levelno for record in held} == {logging.WARNING}
    assert bot.strategy._entries_held is True


def test_repeated_pre_restoration_checks_cannot_toggle_the_hold_open(
    monkeypatch,
) -> None:
    """The held assignment is idempotent on every cycle, not merely true on the final cycle."""
    bot = _bot()
    bot.strategy._entries_held = True
    monkeypatch.setattr(bot.strategy, "cw_armed_segments", lambda: [])
    monkeypatch.setattr(
        "project_mai_tai.services.schwab_1m_v2_bot.time.monotonic",
        lambda: 100.0,
    )

    for _ in range(4):
        bot._cw_boot_hold_check()
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
