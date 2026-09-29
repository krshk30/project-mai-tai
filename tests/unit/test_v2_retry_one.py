from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from project_mai_tai.db.models import (
    Base,
    BrokerAccount,
    BrokerOrder,
    DashboardSnapshot,
    Fill,
    OmsManagedPosition,
    Strategy,
    TradeIntent,
)
from project_mai_tai.fanout_outcome_consumer import FanoutOutcome
from project_mai_tai.fanout_identity import fanout_slot_id
from project_mai_tai.settings import Settings
from project_mai_tai.services.schwab_1m_v2_bot import (
    SchwabV2BotService,
    _retry_one_closes_by_symbol,
)
from project_mai_tai.strategy_core.schwab_1m_v2 import OHLCVBar, SchwabV2Strategy
from project_mai_tai.v2_flip_entry_ownership import (
    FlipConfirmationClose,
    FlipEntryOwnershipStore,
    FlipPositionBook,
    FlipPositionClose,
    FlipPositionLeg,
)


NOW_MS = 1_790_179_800_000
PRIMARY = "live:schwab_1m_v2"
WEBULL = "live:orb"


def _settings(*, enabled: bool, max_retries: int = 1, dual: bool = False) -> Settings:
    return Settings(
        strategy_schwab_1m_v2_confirmed_window_enabled=True,
        strategy_schwab_1m_v2_cw_v2_enabled=True,
        strategy_schwab_1m_v2_cw_v2_resting_entry_enabled=True,
        strategy_schwab_1m_v2_cw_v2_reactive_entry_enabled=True,
        strategy_schwab_1m_v2_cw_v2_reclaim_enabled=False,
        strategy_schwab_1m_v2_flip_owned_first_entry_enabled=True,
        strategy_schwab_1m_v2_retry_one_enabled=enabled,
        strategy_schwab_1m_v2_retry_one_max_retries=max_retries,
        strategy_schwab_1m_v2_dual_broker_fanout_enabled=dual,
        strategy_schwab_1m_v2_webull_resting_mirror_enabled=dual,
        strategy_schwab_1m_v2_account_name=PRIMARY,
        strategy_schwab_1m_v2_webull_account_name=WEBULL,
    )


def _strategy(
    *,
    enabled: bool = True,
    max_retries: int = 1,
    dual: bool = False,
    persist_error: bool = False,
    restore_readable: bool = True,
) -> tuple[SchwabV2Strategy, list[int], list[tuple[str, int, int]]]:
    strategy = SchwabV2Strategy(
        _settings(enabled=enabled, max_retries=max_retries, dual=dual)
    )
    clock = [NOW_MS]
    retry_writes: list[tuple[str, int, int]] = []
    strategy._now_ms = lambda: clock[0]
    strategy._resting_in_window = lambda now=None: True
    strategy._resting_session_is_eh = lambda now=None: False
    strategy._liquidity_floor_ok = lambda _state: True
    strategy._resting_stop_ask_allows = lambda _state, _line, slot: True
    strategy.configure_fanout_identity_persistence(lambda *_args: None)

    def _persist_budget(symbol: str, segment_id: int, count: int) -> None:
        if persist_error and count > 0:
            raise RuntimeError("budget store unavailable")
        retry_writes.append((symbol, segment_id, count))

    strategy.configure_flip_entry_ownership(
        lambda *_args: None,
        restore_readable=True,
        retry_budget_persist=_persist_budget,
        retry_budget_restore_readable=restore_readable,
    )
    return strategy, clock, retry_writes


def _bar(ts: int = NOW_MS) -> OHLCVBar:
    return OHLCVBar(ts, 3.80, 3.90, 3.75, 3.82, 25_000)


def _sell_segment(strategy: SchwabV2Strategy, clock: list[int], symbol: str) -> int:
    state = strategy.watchlist_state(symbol)
    state.bars.append(_bar(clock[0]))
    state.atr_short_flip_bar_ts = clock[0]
    _book(strategy, clock, symbol)
    strategy._cw_v2_track(state, {"flip": "SELL"})
    return clock[0]


def _book(
    strategy: SchwabV2Strategy,
    clock: list[int],
    symbol: str,
    *,
    legs: tuple[FlipPositionLeg, ...] = (),
    closes: tuple[FlipPositionClose, ...] = (),
    confirmation_closes: tuple[FlipConfirmationClose, ...] = (),
) -> None:
    strategy.apply_flip_position_book(
        FlipPositionBook(
            observed_at_ms=clock[0],
            readable=True,
            legs_by_symbol={symbol: legs} if legs else {},
            closes_by_symbol={symbol: closes} if closes else {},
            confirmation_closes_by_symbol=(
                {symbol: confirmation_closes} if confirmation_closes else {}
            ),
        )
    )


def _place_first(
    strategy: SchwabV2Strategy, clock: list[int], symbol: str
) -> tuple[object, int]:
    state = strategy.watchlist_state(symbol)
    if strategy._retry_one_enabled and state.retry_one_segment_id <= 0:
        _sell_segment(strategy, clock, symbol)
    state.bars.append(_bar(clock[0]))
    _book(strategy, clock, symbol)
    strategy._queue_resting_place(state, 3.859, slot="first")
    primary = strategy.drain_pending_intents()
    assert len(primary) == 1
    return state, int(primary[0].metadata["fanout_segment_id"])


def test_bkyi_two_closes_hold_only_their_sell_cycle(
    caplog: pytest.LogCaptureFixture,
) -> None:
    strategy, clock, _writes = _strategy()
    segment_a = _sell_segment(strategy, clock, "BKYI")
    state, _ = _place_first(strategy, clock, "BKYI")
    _fill_primary(strategy, clock, "BKYI", "bkyi-first")
    _close_primary(strategy, clock, "BKYI", "bkyi-first", "CONFIRMATION_EXIT")

    clock[0] += 21 * 60_000
    _book(strategy, clock, "BKYI")
    strategy._queue_resting_place(state, 3.80, slot="first")
    assert len(strategy.drain_pending_intents()) == 1
    _fill_primary(strategy, clock, "BKYI", "bkyi-retry")
    _close_primary(strategy, clock, "BKYI", "bkyi-retry", "CONFIRMATION_EXIT")
    assert state.flip_owner_phase == "consumed"

    strategy._queue_resting_place(state, 3.70, slot="first")
    assert strategy.drain_pending_intents() == []

    clock[0] += 50 * 60_000
    caplog.set_level(logging.INFO)
    segment_b = _sell_segment(strategy, clock, "BKYI")
    assert segment_b > segment_a
    strategy._queue_resting_place(state, 3.70, slot="first")
    assert len(strategy.drain_pending_intents()) == 1
    assert f"segment_id={segment_b} closes_in_segment=0 retries_left=1 action=reset_new_segment" in caplog.text


def test_new_sell_admits_after_two_prior_closes_on_base_compatible_setup() -> None:
    strategy = SchwabV2Strategy(_settings(enabled=True))
    clock = [NOW_MS]
    strategy._now_ms = lambda: clock[0]
    strategy._resting_in_window = lambda now=None: True
    strategy._resting_session_is_eh = lambda now=None: False
    strategy._liquidity_floor_ok = lambda _state: True
    strategy._resting_stop_ask_allows = lambda _state, _line, slot: True
    strategy.configure_fanout_identity_persistence(lambda *_args: None)
    strategy.configure_flip_entry_ownership(
        lambda *_args: None,
        retry_budget_persist=lambda *_args: None,
    )
    segment_a = _sell_segment(strategy, clock, "BASE_RED")
    state = strategy.watchlist_state("BASE_RED")
    _book(strategy, clock, "BASE_RED")
    strategy._queue_resting_place(state, 3.80, slot="first")
    assert len(strategy.drain_pending_intents()) == 1
    _fill_primary(strategy, clock, "BASE_RED", "first-row")
    _close_primary(strategy, clock, "BASE_RED", "first-row", "CONFIRMATION_EXIT")
    clock[0] += 60_000
    _book(strategy, clock, "BASE_RED")
    strategy._queue_resting_place(state, 3.80, slot="first")
    assert len(strategy.drain_pending_intents()) == 1
    _fill_primary(strategy, clock, "BASE_RED", "second-row")
    _close_primary(strategy, clock, "BASE_RED", "second-row", "CONFIRMATION_EXIT")
    strategy._queue_resting_place(state, 3.80, slot="first")
    assert strategy.drain_pending_intents() == []
    clock[0] += 60_000
    assert _sell_segment(strategy, clock, "BASE_RED") > segment_a
    strategy._queue_resting_place(state, 3.80, slot="first")
    assert len(strategy.drain_pending_intents()) == 1


def test_old_cycle_close_does_not_spend_new_cycle_budget() -> None:
    strategy, clock, _writes = _strategy()
    segment_a = _sell_segment(strategy, clock, "OVERLAP")
    state, _ = _place_first(strategy, clock, "OVERLAP")
    _fill_primary(strategy, clock, "OVERLAP", "old-row")

    clock[0] += 60_000
    segment_b = _sell_segment(strategy, clock, "OVERLAP")
    assert segment_b > segment_a
    _close_primary(strategy, clock, "OVERLAP", "old-row", "CW_HARD_STOP")
    assert state.retry_one_segment_id == segment_b
    assert state.retry_one_closes_in_segment == 0


def test_late_old_cycle_close_is_persisted_under_its_own_id() -> None:
    strategy, clock, writes = _strategy()
    segment_a = _sell_segment(strategy, clock, "LATE")
    state, _ = _place_first(strategy, clock, "LATE")
    state.flip_owner_retry_closes_at_place = 1
    clock[0] += 60_000
    segment_b = clock[0]
    state.bars.append(_bar(segment_b))
    state.atr_short_flip_bar_ts = segment_b
    strategy._retry_one_start_segment_on_sell(state, live=True)
    assert segment_b > segment_a
    assert state.retry_one_closes_in_segment == 0
    assert strategy._retry_one_release_or_hold(state, exit_reason="CW_HARD_STOP")
    assert writes[-1] == ("LATE", segment_a, 2)
    assert state.retry_one_segment_id == segment_b
    assert state.retry_one_closes_in_segment == 0


def test_retry_after_budget_write_does_not_count_the_same_close_twice() -> None:
    strategy, clock, writes = _strategy()
    _sell_segment(strategy, clock, "CRASH")
    state, _ = _place_first(strategy, clock, "CRASH")
    retire = strategy._retire_flip_owner_opportunity
    strategy._retire_flip_owner_opportunity = lambda *_args, **_kwargs: False
    try:
        assert not strategy._retry_one_release_or_hold(
            state, exit_reason="CONFIRMATION_EXIT"
        )
    finally:
        strategy._retire_flip_owner_opportunity = retire
    assert state.retry_one_closes_in_segment == 1
    assert strategy._retry_one_release_or_hold(
        state, exit_reason="CONFIRMATION_EXIT"
    )
    assert [count for _, _, count in writes] == [0, 1]


def test_exhausted_cycle_stays_held_after_restart_but_new_cycle_does_not() -> None:
    strategy, clock, _writes = _strategy()
    segment_a = _sell_segment(strategy, clock, "RESTART")
    state = strategy.watchlist_state("RESTART")
    state.retry_one_closes_in_segment = 2

    restarted, restarted_clock, _ = _strategy()
    restarted.configure_flip_entry_ownership(
        lambda *_args: None,
        retry_budget_persist=lambda *_args: None,
        restored_retry_budgets={"RESTART": (segment_a, 2)},
    )
    restored = restarted.watchlist_state("RESTART")
    restored.bars.append(_bar(restarted_clock[0]))
    restored.atr_short_flip_bar_ts = segment_a
    _book(restarted, restarted_clock, "RESTART")
    restarted._queue_resting_place(restored, 3.80, slot="first")
    assert restarted.drain_pending_intents() == []

    restarted_clock[0] += 60_000
    segment_b = _sell_segment(restarted, restarted_clock, "RESTART")
    assert segment_b > segment_a
    restarted._queue_resting_place(restored, 3.80, slot="first")
    assert len(restarted.drain_pending_intents()) == 1

    restarted_again, third_clock, _ = _strategy()
    restarted_again.configure_flip_entry_ownership(
        lambda *_args: None,
        retry_budget_persist=lambda *_args: None,
        restored_retry_budgets={"RESTART": (segment_b, 0)},
    )
    third_clock[0] = segment_b + 60_000
    third_state = restarted_again.watchlist_state("RESTART")
    third_state.bars.append(_bar(third_clock[0]))
    third_state.atr_short_flip_bar_ts = segment_b
    _book(restarted_again, third_clock, "RESTART")
    restarted_again._queue_resting_place(third_state, 3.80, slot="first")
    assert len(restarted_again.drain_pending_intents()) == 1


def test_restart_preserves_the_owned_rests_causal_cycle() -> None:
    strategy, clock, _writes = _strategy()
    segment_a = _sell_segment(strategy, clock, "OWNED")
    state, opportunity = _place_first(strategy, clock, "OWNED")
    record = strategy._flip_owner_record(state)
    assert record.retry_segment_id == segment_a
    assert record.retry_closes_at_place == 0

    restarted, _clock, _ = _strategy()
    restarted.configure_flip_entry_ownership(
        lambda *_args: None,
        active_segments={"OWNED": opportunity},
        restored={"OWNED": record},
        retry_budget_persist=lambda *_args: None,
        restored_retry_budgets={"OWNED": (segment_a, 0)},
    )
    restored = restarted.watchlist_state("OWNED")
    assert restored.flip_owner_retry_segment_id == segment_a
    assert restored.flip_owner_retry_closes_at_place == 0
    assert restored.flip_owner_phase == "resting"


def test_third_sell_cycle_same_day_has_a_new_budget() -> None:
    strategy, clock, writes = _strategy()
    segment_a = _sell_segment(strategy, clock, "THIRDCYCLE")
    state = strategy.watchlist_state("THIRDCYCLE")
    state.retry_one_closes_in_segment = 2
    clock[0] += 60_000
    segment_b = _sell_segment(strategy, clock, "THIRDCYCLE")
    state.retry_one_closes_in_segment = 2
    clock[0] += 60_000
    segment_c = _sell_segment(strategy, clock, "THIRDCYCLE")
    assert segment_a < segment_b < segment_c
    assert state.retry_one_closes_in_segment == 0
    assert writes[-1] == ("THIRDCYCLE", segment_c, 0)
    strategy._queue_resting_place(state, 3.80, slot="first")
    assert len(strategy.drain_pending_intents()) == 1


def test_premarket_soft_rest_uses_the_same_cycle_budget() -> None:
    strategy, clock, _writes = _strategy(dual=True)
    strategy._eh_resting_enabled = True
    strategy._resting_session_is_eh = lambda now=None: True
    _sell_segment(strategy, clock, "PREMARKET")
    state = strategy.watchlist_state("PREMARKET")
    state.retry_one_closes_in_segment = 2
    strategy._queue_resting_place(state, 3.80, slot="first")
    assert state.resting_active is False
    assert strategy.drain_pending_intents() == []
    assert strategy.drain_webull_direct_intents() == []

    clock[0] += 60_000
    _sell_segment(strategy, clock, "PREMARKET")
    strategy._queue_resting_place(state, 3.80, slot="first")
    assert state.resting_active is True
    assert state.resting_is_broker_order is False
    assert strategy.drain_pending_intents() == []
    assert strategy.drain_webull_direct_intents() == []


def test_unknown_segment_and_replayed_sell_cannot_start_a_budget() -> None:
    strategy, clock, writes = _strategy()
    state = strategy.watchlist_state("REPLAY")
    state.bars.append(_bar(clock[0] - 60_000))
    state.atr_short_flip_bar_ts = clock[0] - 60_000
    _book(strategy, clock, "REPLAY")
    strategy._cw_v2_track(state, {"flip": "SELL"})
    assert state.retry_one_segment_id == 0
    assert writes == []
    strategy._queue_resting_place(state, 3.80, slot="first")
    assert strategy.drain_pending_intents() == []
    clock[0] += 60_000
    state.bars.append(_bar(clock[0]))
    state.atr_short_flip_bar_ts = clock[0]
    _book(strategy, clock, "REPLAY")
    strategy._cw_v2_track(
        state, {"flip": "SELL", "observation_phase": "replay"}
    )
    assert state.retry_one_segment_id == 0
    assert writes == []
    clock[0] += 60_000
    _sell_segment(strategy, clock, "REPLAY")
    strategy._queue_resting_place(state, 3.80, slot="first")
    assert len(strategy.drain_pending_intents()) == 1


def test_replayed_sell_cannot_borrow_an_older_zero_close_budget(
    caplog: pytest.LogCaptureFixture,
) -> None:
    strategy, clock, writes = _strategy()
    segment_a = _sell_segment(strategy, clock, "STALESELL")
    state = strategy.watchlist_state("STALESELL")
    assert state.retry_one_closes_in_segment == 0

    clock[0] += 60_000
    state.bars.append(_bar(clock[0]))
    state.atr_short_flip_bar_ts = clock[0]
    _book(strategy, clock, "STALESELL")
    strategy._cw_v2_track(state, {"flip": "SELL", "observation_phase": "replay"})
    assert state.retry_one_segment_id == segment_a
    assert writes == [("STALESELL", segment_a, 0)]

    caplog.set_level(logging.INFO)
    strategy._queue_resting_place(state, 3.80, slot="first")
    assert strategy.drain_pending_intents() == []
    assert "reason=retry_segment_mismatch" in caplog.text


def test_failed_new_segment_reset_marks_budget_unreadable_and_refuses_rest(
    caplog: pytest.LogCaptureFixture,
) -> None:
    strategy, clock, writes = _strategy()
    segment_a = _sell_segment(strategy, clock, "RESETFAIL")
    state = strategy.watchlist_state("RESETFAIL")

    def _reject_reset(_symbol: str, segment_id: int, _count: int) -> None:
        if segment_id != segment_a:
            raise RuntimeError("retry budget reset unavailable")

    strategy._retry_one_budget_persist = _reject_reset
    clock[0] += 60_000
    caplog.set_level(logging.INFO)
    _sell_segment(strategy, clock, "RESETFAIL")

    assert state.retry_one_segment_id == segment_a
    assert state.retry_one_budget_readable is False
    assert writes == [("RESETFAIL", segment_a, 0)]
    strategy._queue_resting_place(state, 3.80, slot="first")
    assert strategy.drain_pending_intents() == []
    assert "reason=retry_budget_unreadable" in caplog.text


def test_readded_watch_does_not_reset_on_its_replayed_sell() -> None:
    strategy, clock, writes = _strategy()
    state = strategy.watchlist_state("READD")
    state.retry_one_watch_start_ms = clock[0] + 30_000
    clock[0] += 60_000
    state.bars.append(_bar(clock[0] - 60_000))
    state.atr_short_flip_bar_ts = clock[0] - 60_000
    _book(strategy, clock, "READD")
    strategy._cw_v2_track(state, {"flip": "SELL"})
    assert state.retry_one_segment_id == 0
    assert writes == []
    clock[0] += 60_000
    _sell_segment(strategy, clock, "READD")
    assert state.retry_one_segment_id == clock[0]


def _leg(account: str, row_id: str, clock: list[int]) -> FlipPositionLeg:
    return FlipPositionLeg(account, row_id, clock[0], 1 if account == WEBULL else 2)


def _close(account: str, row_id: str, reason: str) -> FlipPositionClose:
    return FlipPositionClose(account, row_id, reason)


def _fill_webull(
    strategy: SchwabV2Strategy,
    symbol: str,
    opportunity: int,
    mirror,
) -> None:
    strategy.apply_fanout_outcome(
        FanoutOutcome(
            record_id=uuid4(),
            created_at=datetime.now(UTC),
            symbol=symbol,
            segment_id=opportunity,
            slot=mirror.metadata["fanout_slot"],
            slot_id=mirror.metadata["fanout_slot_id"],
            attempt_id=f"{symbol}-webull-fill",
            outcome="filled",
            evidence_id=f"{symbol}-webull-evidence",
            broker_account_name=WEBULL,
        )
    )


def _fill_primary(
    strategy: SchwabV2Strategy,
    clock: list[int],
    symbol: str,
    row_id: str,
) -> None:
    strategy.update_position(symbol, 2, held_qty=2)
    _book(strategy, clock, symbol, legs=(_leg(PRIMARY, row_id, clock),))


def _close_primary(
    strategy: SchwabV2Strategy,
    clock: list[int],
    symbol: str,
    row_id: str,
    reason: str,
) -> None:
    strategy.update_position(symbol, 0, held_qty=0)
    _book(strategy, clock, symbol, closes=(_close(PRIMARY, row_id, reason),))


def test_retry_one_defaults_off_and_preserves_confirmation_only_reset() -> None:
    assert Settings().strategy_schwab_1m_v2_retry_one_enabled is False
    assert Settings().strategy_schwab_1m_v2_retry_one_max_retries == 1

    stopped, clock, _writes = _strategy(enabled=False)
    state, _opportunity = _place_first(stopped, clock, "OFFSTOP")
    _fill_primary(stopped, clock, "OFFSTOP", "off-stop-row")
    _close_primary(stopped, clock, "OFFSTOP", "off-stop-row", "CW_HARD_STOP")
    assert state.flip_owner_phase == "consumed"

    confirmed, clock, _writes = _strategy(enabled=False)
    state, opportunity = _place_first(confirmed, clock, "OFFCONF")
    _fill_primary(confirmed, clock, "OFFCONF", "off-conf-row")
    confirmed.update_position("OFFCONF", 0, held_qty=0)
    _book(
        confirmed,
        clock,
        "OFFCONF",
        confirmation_closes=(
                FlipConfirmationClose(
                    PRIMARY,
                    "off-conf-row",
                    fanout_slot_id(
                        strategy_code="schwab_1m_v2",
                        symbol="OFFCONF",
                        segment_id=opportunity,
                        slot="resting",
                    ),
            ),
        ),
    )
    assert opportunity > 0
    assert state.flip_owner_phase == "idle"


def test_vsa_webull_only_hard_stop_releases_but_needs_a_later_rest(
    caplog: pytest.LogCaptureFixture,
) -> None:
    strategy, clock, writes = _strategy(dual=True)
    state, opportunity = _place_first(strategy, clock, "VSA")
    mirror = strategy.drain_webull_direct_intents()[0]
    _fill_webull(strategy, "VSA", opportunity, mirror)
    _book(strategy, clock, "VSA", legs=(_leg(WEBULL, "vsa-webull-row", clock),))

    clock[0] += 60_000
    caplog.set_level(logging.INFO)
    _book(
        strategy,
        clock,
        "VSA",
        closes=(_close(WEBULL, "vsa-webull-row", "CW_HARD_STOP"),),
    )

    assert state.flip_owner_phase == "idle"
    assert [count for _, _, count in writes] == [0, 1]
    assert strategy.drain_pending_intents() == []
    assert strategy.drain_webull_direct_intents() == []
    assert "closes_in_segment=1 retries_left=1 action=released" in caplog.text

    strategy._queue_resting_place(state, 3.859, slot="first")
    assert len(strategy.drain_pending_intents()) == 1
    assert len(strategy.drain_webull_direct_intents()) == 1


def test_broker_oco_close_releases_the_first_try() -> None:
    strategy, clock, _writes = _strategy()
    state, _opportunity = _place_first(strategy, clock, "OCO")
    _fill_primary(strategy, clock, "OCO", "oco-row")
    _close_primary(strategy, clock, "OCO", "oco-row", "OCO_RESOLVED_FLAT")
    assert state.flip_owner_phase == "idle"
    assert state.retry_one_closes_in_segment == 1


def test_one_open_sibling_cannot_release_the_shared_opportunity() -> None:
    strategy, clock, writes = _strategy(dual=True)
    state, opportunity = _place_first(strategy, clock, "SIBLING")
    mirror = strategy.drain_webull_direct_intents()[0]
    strategy.update_position("SIBLING", 2, held_qty=2)
    _fill_webull(strategy, "SIBLING", opportunity, mirror)
    _book(
        strategy,
        clock,
        "SIBLING",
        legs=(
            _leg(PRIMARY, "sibling-primary", clock),
            _leg(WEBULL, "sibling-webull", clock),
        ),
    )
    strategy.update_position("SIBLING", 0, held_qty=0)
    _book(
        strategy,
        clock,
        "SIBLING",
        legs=(_leg(PRIMARY, "sibling-primary", clock),),
        closes=(_close(WEBULL, "sibling-webull", "CW_HARD_STOP"),),
    )

    assert state.flip_owner_phase == "provisional"
    assert state.flip_owner_opportunity_id == opportunity
    assert [count for _, _, count in writes] == [0]


def test_flat_siblings_wait_for_every_exact_close_before_releasing_once() -> None:
    strategy, clock, writes = _strategy(dual=True)
    state, opportunity = _place_first(strategy, clock, "ATTRLAG")
    mirror = strategy.drain_webull_direct_intents()[0]
    _fill_webull(strategy, "ATTRLAG", opportunity, mirror)
    strategy.update_position("ATTRLAG", 2, held_qty=2)
    _book(
        strategy,
        clock,
        "ATTRLAG",
        legs=(
            _leg(PRIMARY, "attrlag-primary", clock),
            _leg(WEBULL, "attrlag-webull", clock),
        ),
    )
    assert state.flip_owner_position_ids == {
        PRIMARY: "attrlag-primary",
        WEBULL: "attrlag-webull",
    }

    strategy.update_position("ATTRLAG", 0, held_qty=0)
    _book(
        strategy,
        clock,
        "ATTRLAG",
        closes=(_close(WEBULL, "attrlag-webull", "CW_HARD_STOP"),),
    )

    assert state.flip_owner_phase == "unknown"
    assert state.flip_owner_opportunity_id == opportunity
    assert [count for _, _, count in writes] == [0]

    clock[0] += 20_000
    _book(
        strategy,
        clock,
        "ATTRLAG",
        closes=(
            _close(PRIMARY, "attrlag-primary", "CW_HARD_STOP"),
            _close(WEBULL, "attrlag-webull", "CW_HARD_STOP"),
        ),
    )

    assert state.flip_owner_phase == "idle"
    assert [count for _, _, count in writes] == [0, 1]
    _book(
        strategy,
        clock,
        "ATTRLAG",
        closes=(
            _close(PRIMARY, "attrlag-primary", "CW_HARD_STOP"),
            _close(WEBULL, "attrlag-webull", "CW_HARD_STOP"),
        ),
    )
    assert [count for _, _, count in writes] == [0, 1]


def test_retry_budget_persist_failure_holds_the_owner_fail_closed(
    caplog: pytest.LogCaptureFixture,
) -> None:
    strategy, clock, writes = _strategy(persist_error=True)
    state, opportunity = _place_first(strategy, clock, "PERSISTFAIL")
    _fill_primary(strategy, clock, "PERSISTFAIL", "persist-fail-row")
    caplog.set_level(logging.INFO)
    _close_primary(
        strategy,
        clock,
        "PERSISTFAIL",
        "persist-fail-row",
        "CW_HARD_STOP",
    )

    assert state.flip_owner_phase == "consumed"
    assert state.flip_owner_opportunity_id == opportunity
    assert state.retry_one_budget_readable is False
    assert [count for _, _, count in writes] == [0]
    assert "action=held reason=retry_budget_persist_failed" in caplog.text


def test_unreadable_retry_budget_restore_refuses_the_first_entry(
    caplog: pytest.LogCaptureFixture,
) -> None:
    strategy, clock, writes = _strategy(restore_readable=False)
    state = strategy.watchlist_state("RESTOREFAIL")
    state.bars.append(_bar(clock[0]))
    _book(strategy, clock, "RESTOREFAIL")
    caplog.set_level(logging.INFO)

    strategy._queue_resting_place(state, 3.859, slot="first")

    assert state.retry_one_budget_readable is False
    assert strategy.drain_pending_intents() == []
    assert writes == []
    assert "reason=retry_budget_unreadable" in caplog.text


def test_retry_budget_holds_the_second_close_until_the_next_sell(
    caplog: pytest.LogCaptureFixture,
) -> None:
    strategy, clock, writes = _strategy()
    state, _opportunity = _place_first(strategy, clock, "BUDGET")
    _fill_primary(strategy, clock, "BUDGET", "budget-row-1")
    _close_primary(strategy, clock, "BUDGET", "budget-row-1", "CW_HARD_STOP")
    assert state.flip_owner_phase == "idle"

    clock[0] += 60_000
    _book(strategy, clock, "BUDGET")
    strategy._queue_resting_place(state, 3.80, slot="first")
    strategy.drain_pending_intents()
    _fill_primary(strategy, clock, "BUDGET", "budget-row-2")
    caplog.set_level(logging.INFO)
    _close_primary(strategy, clock, "BUDGET", "budget-row-2", "CW_FLOOR")

    assert state.flip_owner_phase == "consumed"
    assert state.retry_one_closes_in_segment == 2
    assert [count for _, _, count in writes] == [0, 1, 2]
    assert "closes_in_segment=2 retries_left=0 action=held" in caplog.text
    assert "reason=retry_budget_exhausted" in caplog.text

    state.flip_owner_evidence_at_ms = clock[0]
    strategy._end_flip_owner_on_sell(state)
    assert state.flip_owner_phase == "idle"
    _book(strategy, clock, "BUDGET")
    strategy._queue_resting_place(state, 3.70, slot="first")
    assert strategy.drain_pending_intents() == []

    state.flip_owner_evidence_readable = True
    state.flip_owner_evidence_at_ms = clock[0]
    strategy._apply_session_anchor_reset(
        state,
        state.atr_session_anchor_ms + 86_400_000,
        owner_boundary_is_current=True,
    )
    assert state.retry_one_closes_in_segment == 0
    assert state.flip_owner_phase == "idle"
    _book(strategy, clock, "BUDGET")
    strategy._queue_resting_place(state, 3.70, slot="first")
    assert strategy.drain_pending_intents() == []
    clock[0] += 86_400_000
    _sell_segment(strategy, clock, "BUDGET")
    strategy._queue_resting_place(state, 3.70, slot="first")
    assert len(strategy.drain_pending_intents()) == 1


def test_max_retries_two_holds_the_third_close() -> None:
    strategy, clock, writes = _strategy(max_retries=2)
    state = None
    for attempt in range(1, 4):
        if attempt > 1:
            _book(strategy, clock, "THREE")
        state, _opportunity = _place_first(strategy, clock, "THREE")
        _fill_primary(strategy, clock, "THREE", f"three-row-{attempt}")
        _close_primary(
            strategy,
            clock,
            "THREE",
            f"three-row-{attempt}",
            "CONFIRMATION_EXIT",
        )
        clock[0] += 60_000
    assert state is not None
    assert state.flip_owner_phase == "consumed"
    assert [count for _, _, count in writes] == [0, 1, 2, 3]


def test_dcoy_three_close_replay_takes_exactly_one_retry() -> None:
    strategy, clock, _writes = _strategy()
    state, _opportunity = _place_first(strategy, clock, "DCOY")
    _fill_primary(strategy, clock, "DCOY", "dcoy-1053")
    _close_primary(strategy, clock, "DCOY", "dcoy-1053", "CONFIRMATION_EXIT")

    clock[0] += 10 * 60_000
    _book(strategy, clock, "DCOY")
    strategy._queue_resting_place(state, 5.68, slot="first")
    strategy.drain_pending_intents()
    _fill_primary(strategy, clock, "DCOY", "dcoy-1103")
    _close_primary(strategy, clock, "DCOY", "dcoy-1103", "CONFIRMATION_EXIT")

    clock[0] += 49 * 60_000
    strategy._queue_resting_place(state, 5.33, slot="first")
    assert strategy.drain_pending_intents() == []
    assert state.flip_owner_phase == "consumed"
    assert state.retry_one_closes_in_segment == 2


def test_retry_budget_restores_the_exact_sell_cycle() -> None:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine, tables=[DashboardSnapshot.__table__])
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    store = FlipEntryOwnershipStore(factory)
    now = datetime(2026, 9, 23, 18, 0, tzinfo=UTC)
    store.record_retry_budget("VSA", 123_000, 1, now=now)
    store.record_retry_budget("VSA", 123_000, 2, now=now.replace(minute=1))

    assert store.restore_retry_budgets(now=now.replace(minute=2)) == {
        "VSA": (123_000, 2)
    }
    store.record_retry_budget("VSA", 123_000, 1, now=now.replace(minute=2))
    assert store.restore_retry_budgets(now=now.replace(minute=2, second=1)) == {
        "VSA": (123_000, 2)
    }
    store.record_retry_budget("VSA", 124_000, 0, now=now.replace(minute=3))
    store.record_retry_budget("VSA", 123_000, 3, now=now.replace(minute=4))
    assert store.restore_retry_budgets(now=now.replace(minute=5)) == {
        "VSA": (124_000, 0)
    }


def test_legacy_daily_budget_has_no_cycle_identity_until_fresh_sell() -> None:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine, tables=[DashboardSnapshot.__table__])
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    now = datetime(2026, 9, 29, 18, 0, tzinfo=UTC)
    with factory() as session:
        session.add(
            DashboardSnapshot(
                snapshot_type="v2_retry_one_budget",
                payload={
                    "schema_version": 1,
                    "strategy_code": "schwab_1m_v2",
                    "symbol": "LEGACY",
                    "closes_today": 2,
                },
                created_at=now,
            )
        )
        session.commit()
    restored = FlipEntryOwnershipStore(factory).restore_retry_budgets(now=now)
    assert restored == {"LEGACY": (0, 2)}

    strategy, clock, writes = _strategy()
    strategy.configure_flip_entry_ownership(
        lambda *_args: None,
        retry_budget_persist=lambda symbol, segment, count: writes.append(
            (symbol, segment, count)
        ),
        restored_retry_budgets=restored,
    )
    state = strategy.watchlist_state("LEGACY")
    state.bars.append(_bar(clock[0]))
    state.atr_short_flip_bar_ts = clock[0]
    _book(strategy, clock, "LEGACY")
    strategy._queue_resting_place(state, 3.80, slot="first")
    assert strategy.drain_pending_intents() == []
    _sell_segment(strategy, clock, "LEGACY")
    strategy._queue_resting_place(state, 3.80, slot="first")
    assert len(strategy.drain_pending_intents()) == 1
    assert writes[-1] == ("LEGACY", clock[0], 0)


def test_real_close_shape_requires_a_full_order_and_exact_closed_episode() -> None:
    entered = datetime(2026, 9, 23, 19, 29, 30, tzinfo=UTC)
    closed = datetime(2026, 9, 23, 19, 30, 45, tzinfo=UTC)
    managed = SimpleNamespace(
        id="2180c5db-d5bb-4384-9382-a4da77cba7da",
        broker_account_name=WEBULL,
        symbol="VSA",
        entry_time=entered,
        updated_at=closed,
    )
    order = SimpleNamespace(
        id="vsa-close-order",
        quantity=Decimal("1"),
        order_type="market",
        symbol="VSA",
        payload={"oms_v2_managed_exit": "true"},
    )
    account = SimpleNamespace(name=WEBULL)
    intent = SimpleNamespace(
        reason="oms_v2_managed_exit:CW_HARD_STOP",
        payload={"metadata": {"oms_v2_managed_exit": "true"}},
    )
    partial = SimpleNamespace(quantity=Decimal("0.5"), filled_at=closed)
    assert _retry_one_closes_by_symbol([managed], [(partial, order, account, intent)]) == {}

    full = SimpleNamespace(quantity=Decimal("1"), filled_at=closed)
    closes = _retry_one_closes_by_symbol([managed], [(full, order, account, intent)])
    assert closes["VSA"] == (
        FlipPositionClose(WEBULL, managed.id, "CW_HARD_STOP"),
    )


def test_position_book_reads_a_real_hard_stop_close_for_the_exact_episode() -> None:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(
        engine,
        tables=[
            DashboardSnapshot.__table__,
            OmsManagedPosition.__table__,
            Strategy.__table__,
            BrokerAccount.__table__,
            TradeIntent.__table__,
            BrokerOrder.__table__,
            Fill.__table__,
        ],
    )
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    closed_at = datetime.now(UTC) - timedelta(seconds=5)
    entered_at = closed_at - timedelta(minutes=1)
    with factory() as session:
        strategy = Strategy(code="schwab_1m_v2", name="v2", execution_mode="live")
        account = BrokerAccount(
            name=WEBULL,
            provider="webull",
            environment="production",
        )
        session.add_all([strategy, account])
        session.flush()
        managed = OmsManagedPosition(
            strategy_code="schwab_1m_v2",
            broker_account_name=WEBULL,
            symbol="VSA",
            entry_price=Decimal("3.87"),
            original_quantity=1,
            current_quantity=0,
            entry_path="ATR Flip",
            entry_time=entered_at,
            status="closed",
            updated_at=closed_at,
        )
        intent = TradeIntent(
            strategy_id=strategy.id,
            broker_account_id=account.id,
            symbol="VSA",
            side="sell",
            intent_type="close",
            quantity=Decimal("1"),
            reason="oms_v2_managed_exit:CW_HARD_STOP",
            status="filled",
            payload={"metadata": {"oms_v2_managed_exit": "true"}},
            created_at=closed_at,
            updated_at=closed_at,
        )
        session.add_all([managed, intent])
        session.flush()
        order = BrokerOrder(
            intent_id=intent.id,
            strategy_id=strategy.id,
            broker_account_id=account.id,
            client_order_id="vsa-cw-hard-stop",
            broker_order_id="vsa-cw-hard-stop-broker",
            symbol="VSA",
            side="sell",
            order_type="market",
            time_in_force="day",
            quantity=Decimal("1"),
            status="filled",
            payload={},
            submitted_at=closed_at,
            updated_at=closed_at,
        )
        session.add(order)
        session.flush()
        session.add(
            Fill(
                order_id=order.id,
                strategy_id=strategy.id,
                broker_account_id=account.id,
                broker_fill_id="vsa-cw-hard-stop-fill",
                symbol="VSA",
                side="sell",
                quantity=Decimal("1"),
                price=Decimal("3.56"),
                filled_at=closed_at,
                payload={},
            )
        )
        managed_id = str(managed.id)
        session.commit()

    bot = SchwabV2BotService(
        Settings(
            strategy_schwab_1m_v2_flip_owned_first_entry_enabled=True,
            strategy_schwab_1m_v2_retry_one_enabled=True,
            strategy_schwab_1m_v2_account_name=PRIMARY,
            strategy_schwab_1m_v2_webull_account_name=WEBULL,
        ),
        session_factory=factory,
    )
    book = bot._fetch_flip_position_book()

    assert book.readable is True
    assert book.closes_by_symbol["VSA"] == (
        FlipPositionClose(WEBULL, managed_id, "CW_HARD_STOP"),
    )
