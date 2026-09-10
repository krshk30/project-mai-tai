from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from project_mai_tai.fanout_outcome_consumer import FanoutOutcome
from project_mai_tai.market_data.schwab_v2_rest_client import Quote
from project_mai_tai.settings import Settings
from project_mai_tai.strategy_core.schwab_1m_v2 import (
    OHLCVBar,
    SchwabV2Strategy,
    SymbolState,
)
from project_mai_tai.v2_flip_entry_ownership import (
    FlipEntryOwnershipRecord,
    FlipPositionBook,
    FlipPositionLeg,
)


NOW_MS = 1_788_973_000_000
PRIMARY = "live:schwab_1m_v2"
WEBULL = "live:orb"


def test_flip_owned_first_entry_fail_safe_defaults_off() -> None:
    assert Settings().strategy_schwab_1m_v2_flip_owned_first_entry_enabled is False


def _bar(timestamp_ms: int = NOW_MS) -> OHLCVBar:
    return OHLCVBar(
        timestamp_ms=timestamp_ms,
        open=2.90,
        high=3.00,
        low=2.85,
        close=2.98,
        volume=25_000,
    )


def _signal(flip: str | None = None, *, state: str = "short") -> dict[str, object]:
    return {
        "touch": False,
        "touch_price": None,
        "flip": flip,
        "flip_level": 2.90 if flip == "BUY" else None,
        "trail": 2.90,
        "loss": 0.0,
        "state": state,
        "state_age": 3,
    }


def _settings(*, strict: bool, dual: bool = False) -> Settings:
    return Settings(
        strategy_schwab_1m_v2_confirmed_window_enabled=True,
        strategy_schwab_1m_v2_cw_v2_enabled=True,
        strategy_schwab_1m_v2_cw_v2_reclaim_enabled=False,
        strategy_schwab_1m_v2_cw_v2_reactive_entry_enabled=True,
        strategy_schwab_1m_v2_cw_v2_resting_entry_enabled=True,
        strategy_schwab_1m_v2_dual_broker_fanout_enabled=dual,
        strategy_schwab_1m_v2_webull_resting_mirror_enabled=dual,
        strategy_schwab_1m_v2_flip_owned_first_entry_enabled=strict,
        strategy_schwab_1m_v2_account_name=PRIMARY,
        strategy_schwab_1m_v2_webull_account_name=WEBULL,
    )


def _strategy(
    *, strict: bool = True, dual: bool = False
) -> tuple[
    SchwabV2Strategy,
    list[int],
    list[tuple[str, int, bool, str]],
    list[tuple[FlipEntryOwnershipRecord, bool, str]],
]:
    strategy = SchwabV2Strategy(_settings(strict=strict, dual=dual))
    clock = [NOW_MS]
    strategy._now_ms = lambda: clock[0]
    strategy._resting_in_window = lambda now=None: True
    strategy._resting_session_is_eh = lambda now=None: False
    strategy._liquidity_floor_ok = lambda _state: True
    strategy._resting_stop_ask_allows = lambda _state, _line, slot: True
    identity_writes: list[tuple[str, int, bool, str]] = []
    owner_writes: list[tuple[FlipEntryOwnershipRecord, bool, str]] = []
    strategy.configure_fanout_identity_persistence(
        lambda symbol, segment, active, reason: identity_writes.append(
            (symbol, segment, active, reason)
        )
    )
    strategy.configure_flip_entry_ownership(
        lambda record, active, reason: owner_writes.append((record, active, reason)),
        restore_readable=True,
    )
    return strategy, clock, identity_writes, owner_writes


def _book(
    strategy: SchwabV2Strategy,
    clock: list[int],
    symbol: str,
    *legs: FlipPositionLeg,
    readable: bool = True,
) -> None:
    strategy.apply_flip_position_book(
        FlipPositionBook(
            observed_at_ms=clock[0],
            readable=readable,
            legs_by_symbol={symbol: tuple(legs)} if legs else {},
        )
    )


def _leg(account: str, row_id: str, *, entered_ms: int = NOW_MS) -> FlipPositionLeg:
    return FlipPositionLeg(
        account_name=account,
        managed_row_id=row_id,
        entry_time_ms=entered_ms,
        quantity=2 if account == PRIMARY else 1,
    )


def _place_first(
    strategy: SchwabV2Strategy,
    clock: list[int],
    symbol: str,
) -> tuple[SymbolState, int]:
    state = strategy.watchlist_state(symbol)
    state.bars.append(_bar(clock[0]))
    _book(strategy, clock, symbol)
    strategy._queue_resting_place(state, 2.90, slot="first")
    primary = strategy.drain_pending_intents()
    assert len(primary) == 1
    assert primary[0].metadata["cw_entry_slot"] == "first"
    assert primary[0].metadata["cw_arm_bar_ts"] == "0"
    assert primary[0].metadata["fanout_identity_schema"] == "entry_opportunity_v2"
    return state, int(primary[0].metadata["fanout_segment_id"])


def _buy_flip(strategy: SchwabV2Strategy, state: SymbolState, clock: list[int]) -> None:
    clock[0] += 1_000
    state.bars.append(_bar(clock[0]))
    strategy._cw_v2_track(state, _signal("BUY", state="long"))


def test_ftft_flip_consumes_the_first_entry_until_the_next_sell_flip() -> None:
    strategy, clock, _identity_writes, _owner_writes = _strategy()
    state, opportunity = _place_first(strategy, clock, "FTFT")

    strategy.update_position("FTFT", 2, held_qty=2)
    strategy._cw_v2_resting_track(state, _signal(state="short"))
    assert not state.resting_active
    _book(strategy, clock, "FTFT", _leg(PRIMARY, "ftft-primary"))
    _buy_flip(strategy, state, clock)
    assert state.flip_owner_phase == "bound"

    strategy.update_position("FTFT", 0, held_qty=0)
    _book(strategy, clock, "FTFT")
    assert state.flip_owner_phase == "bound"
    assert state.flip_owner_opportunity_id == opportunity

    state.cw_bars_waited = 2
    state.cw_segment_high = 3.0
    placements: list[str] = []
    original_place = strategy._queue_resting_place

    def record_place(
        target: SymbolState, line: float, *, slot: str = "first"
    ) -> None:
        placements.append(slot)
        original_place(target, line, slot=slot)

    strategy._queue_resting_place = record_place
    strategy._cw_v2_reclaim_resting_track(state)
    assert placements == ["reclaim"], "the live reclaim producer must reach admission"
    assert strategy.drain_pending_intents() == []

    # The durable owner remains load-bearing even if the legacy first-slot bit is lost.
    state.cw_resting_taken = False
    strategy._cw_v2_resting_track(state, _signal(state="short"))
    assert placements == ["reclaim", "first"]
    assert strategy.drain_pending_intents() == []

    quote = Quote("FTFT", 3.09, 3.11, 3.10, clock[0], 0)
    assert strategy._cw_v2_quote(state, quote) is None

    state.bars.append(_bar(clock[0] + 60_000))
    strategy._cw_v2_track(state, _signal("SELL", state="short"))
    assert state.flip_owner_phase == "idle"
    assert state.flip_owner_opportunity_id == 0


def test_sune_preflip_close_releases_then_mints_a_new_first_opportunity() -> None:
    strategy, clock, identity_writes, _owner_writes = _strategy()
    state, first_opportunity = _place_first(strategy, clock, "SUNE")

    strategy.update_position("SUNE", 2, held_qty=2)
    _book(strategy, clock, "SUNE", _leg(PRIMARY, "sune-first"))
    strategy.update_position("SUNE", 0, held_qty=0)
    _book(strategy, clock, "SUNE")

    assert state.flip_owner_phase == "idle"
    assert state.flip_owner_opportunity_id == 0
    assert identity_writes[-1][1:3] == (first_opportunity, False)

    clock[0] += 60_000
    _book(strategy, clock, "SUNE")
    strategy._queue_resting_place(state, 3.566, slot="first")
    second = strategy.drain_pending_intents()
    assert len(second) == 1
    second_opportunity = int(second[0].metadata["fanout_segment_id"])
    assert second_opportunity > first_opportunity
    assert second[0].metadata["cw_entry_slot"] == "first"


def test_dual_broker_fill_is_one_episode_and_waits_for_both_siblings_to_close() -> None:
    strategy, clock, _identity_writes, _owner_writes = _strategy(dual=True)
    state, opportunity = _place_first(strategy, clock, "DUAL")
    mirror = strategy.drain_webull_direct_intents()
    assert len(mirror) == 1

    strategy.update_position("DUAL", 2, held_qty=2)
    outcome = FanoutOutcome(
        record_id=uuid4(),
        created_at=datetime.now(UTC),
        symbol="DUAL",
        segment_id=opportunity,
        slot=mirror[0].metadata["fanout_slot"],
        slot_id=mirror[0].metadata["fanout_slot_id"],
        attempt_id="webull-first",
        outcome="filled",
        evidence_id="webull-fill",
        broker_account_name=WEBULL,
    )
    assert strategy.apply_fanout_outcome(outcome) == "consumed"
    _book(
        strategy,
        clock,
        "DUAL",
        _leg(PRIMARY, "dual-primary"),
        _leg(WEBULL, "dual-webull"),
    )
    _buy_flip(strategy, state, clock)
    assert state.flip_owner_phase == "bound"
    assert state.flip_owner_fill_accounts == {PRIMARY, WEBULL}

    strategy.update_position("DUAL", 0, held_qty=0)
    _book(strategy, clock, "DUAL", _leg(WEBULL, "dual-webull"))
    assert state.flip_owner_phase == "bound"
    _book(strategy, clock, "DUAL")
    assert state.flip_owner_phase == "bound", "a close does not reopen the same flip"

    state.bars.append(_bar(clock[0] + 60_000))
    strategy._cw_v2_track(state, _signal("SELL", state="short"))
    assert state.flip_owner_phase == "idle"


def test_tnon_position_book_waits_for_late_webull_fill_then_reconciles() -> None:
    strategy, clock, _identity_writes, _owner_writes = _strategy(dual=True)
    state, opportunity = _place_first(strategy, clock, "TNON")
    mirror = strategy.drain_webull_direct_intents()[0]

    strategy.update_position("TNON", 2, held_qty=2)
    _book(
        strategy,
        clock,
        "TNON",
        _leg(PRIMARY, "tnon-primary"),
        _leg(WEBULL, "tnon-webull"),
    )

    assert state.flip_owner_phase == "provisional"
    assert state.flip_owner_fill_accounts == {PRIMARY}
    assert state.flip_owner_position_ids == {PRIMARY: "tnon-primary"}
    assert strategy.flip_entry_observability()["cross_account_pending"] == 1

    outcome = FanoutOutcome(
        record_id=uuid4(),
        created_at=datetime.now(UTC),
        symbol="TNON",
        segment_id=opportunity,
        slot=mirror.metadata["fanout_slot"],
        slot_id=mirror.metadata["fanout_slot_id"],
        attempt_id="tnon-webull",
        outcome="filled",
        evidence_id="tnon-webull-fill",
        broker_account_name=WEBULL,
    )
    assert strategy.apply_fanout_outcome(outcome) == "consumed"
    _book(
        strategy,
        clock,
        "TNON",
        _leg(PRIMARY, "tnon-primary"),
        _leg(WEBULL, "tnon-webull"),
    )

    assert state.flip_owner_phase == "provisional"
    assert state.flip_owner_fill_accounts == {PRIMARY, WEBULL}
    assert state.flip_owner_position_ids == {
        PRIMARY: "tnon-primary",
        WEBULL: "tnon-webull",
    }


def test_fill_arriving_after_unknown_is_recorded_and_recovers_the_open_episode() -> None:
    strategy, clock, _identity_writes, _owner_writes = _strategy(dual=True)
    state, opportunity = _place_first(strategy, clock, "LATEWEBULL")
    mirror = strategy.drain_webull_direct_intents()[0]
    strategy.update_position("LATEWEBULL", 2, held_qty=2)
    _book(strategy, clock, "LATEWEBULL", _leg(PRIMARY, "late-primary"))

    clock[0] += 15_001
    _book(
        strategy,
        clock,
        "LATEWEBULL",
        _leg(PRIMARY, "late-primary"),
        _leg(WEBULL, "late-webull"),
    )
    assert state.flip_owner_phase == "unknown"

    assert strategy.apply_fanout_outcome(
        FanoutOutcome(
            record_id=uuid4(),
            created_at=datetime.now(UTC),
            symbol="LATEWEBULL",
            segment_id=opportunity,
            slot=mirror.metadata["fanout_slot"],
            slot_id=mirror.metadata["fanout_slot_id"],
            attempt_id="late-webull",
            outcome="filled",
            evidence_id="late-webull-fill",
            broker_account_name=WEBULL,
        )
    ) == "consumed"
    assert state.flip_owner_phase == "unknown"
    assert state.flip_owner_fill_accounts == {PRIMARY, WEBULL}

    _book(
        strategy,
        clock,
        "LATEWEBULL",
        _leg(PRIMARY, "late-primary"),
        _leg(WEBULL, "late-webull"),
    )
    assert state.flip_owner_phase == "provisional"
    assert state.flip_owner_position_ids == {
        PRIMARY: "late-primary",
        WEBULL: "late-webull",
    }


def test_unknown_preflip_owner_recovers_to_idle_after_late_fill_then_flat_book() -> None:
    strategy, clock, identity_writes, _owner_writes = _strategy(dual=True)
    state, opportunity = _place_first(strategy, clock, "RECOVER")
    mirror = strategy.drain_webull_direct_intents()[0]
    strategy.update_position("RECOVER", 2, held_qty=2)
    _book(
        strategy,
        clock,
        "RECOVER",
        _leg(PRIMARY, "recover-primary"),
        _leg(WEBULL, "recover-webull"),
    )
    clock[0] += 15_001
    _book(
        strategy,
        clock,
        "RECOVER",
        _leg(PRIMARY, "recover-primary"),
        _leg(WEBULL, "recover-webull"),
    )
    assert state.flip_owner_phase == "unknown"

    assert strategy.apply_fanout_outcome(
        FanoutOutcome(
            record_id=uuid4(),
            created_at=datetime.now(UTC),
            symbol="RECOVER",
            segment_id=opportunity,
            slot=mirror.metadata["fanout_slot"],
            slot_id=mirror.metadata["fanout_slot_id"],
            attempt_id="recover-webull",
            outcome="filled",
            evidence_id="recover-webull-fill",
            broker_account_name=WEBULL,
        )
    ) == "consumed"
    assert state.flip_owner_phase == "unknown"
    assert state.flip_owner_fill_accounts == {PRIMARY, WEBULL}
    assert state.flip_owner_position_ids == {
        PRIMARY: "recover-primary",
        WEBULL: "recover-webull",
    }

    strategy.update_position("RECOVER", 0, held_qty=0)
    _book(strategy, clock, "RECOVER")
    assert state.flip_owner_phase == "idle"
    assert state.flip_owner_opportunity_id == 0
    assert ("RECOVER", opportunity, False, "unknown_preflip_owner_flat_after_settle") in identity_writes


def test_unknown_postflip_flat_owner_stays_consumed_until_the_sell_flip() -> None:
    strategy, clock, _identity_writes, _owner_writes = _strategy()
    state, opportunity = _place_first(strategy, clock, "CONSUMED")
    strategy.update_position("CONSUMED", 2, held_qty=2)
    _book(strategy, clock, "CONSUMED", _leg(PRIMARY, "consumed-row"))
    _buy_flip(strategy, state, clock)
    assert state.flip_owner_phase == "bound"

    strategy._set_flip_owner_unknown(state, reason="test_inconclusive_read")
    strategy.update_position("CONSUMED", 0, held_qty=0)
    clock[0] += 15_001
    _book(strategy, clock, "CONSUMED")

    assert state.flip_owner_phase == "bound"
    assert state.flip_owner_opportunity_id == opportunity
    strategy._queue_resting_place(state, 2.90, slot="first")
    assert strategy.drain_pending_intents() == []

    state.bars.append(_bar(clock[0] + 60_000))
    strategy._cw_v2_track(state, _signal("SELL", state="short"))
    assert state.flip_owner_phase == "idle"


def test_unknown_owner_refuses_a_later_position_outside_the_fill_episode() -> None:
    strategy, clock, _identity_writes, _owner_writes = _strategy()
    state, opportunity = _place_first(strategy, clock, "LATER")
    strategy.update_position("LATER", 2, held_qty=2)
    strategy._set_flip_owner_unknown(state, reason="test_inconclusive_read")

    clock[0] += 60_000
    _book(
        strategy,
        clock,
        "LATER",
        _leg(PRIMARY, "later-position", entered_ms=clock[0]),
    )

    assert state.flip_owner_phase == "unknown"
    assert state.flip_owner_opportunity_id == opportunity
    assert state.flip_owner_position_ids == {}
    strategy._queue_resting_place(state, 2.90, slot="first")
    assert strategy.drain_pending_intents() == []


def test_webull_only_fill_consumes_the_flip_without_a_schwab_position() -> None:
    strategy, clock, _identity_writes, _owner_writes = _strategy(dual=True)
    state, opportunity = _place_first(strategy, clock, "WONLY")
    mirror = strategy.drain_webull_direct_intents()[0]
    outcome = FanoutOutcome(
        record_id=uuid4(),
        created_at=datetime.now(UTC),
        symbol="WONLY",
        segment_id=opportunity,
        slot=mirror.metadata["fanout_slot"],
        slot_id=mirror.metadata["fanout_slot_id"],
        attempt_id="webull-only",
        outcome="filled",
        evidence_id="webull-only-fill",
        broker_account_name=WEBULL,
    )

    assert strategy.apply_fanout_outcome(outcome) == "consumed"
    _book(strategy, clock, "WONLY", _leg(WEBULL, "webull-only-row"))
    _buy_flip(strategy, state, clock)

    assert state.flip_owner_phase == "bound"
    assert state.flip_owner_position_ids == {WEBULL: "webull-only-row"}


def test_dual_preflip_episode_releases_only_after_both_siblings_close() -> None:
    strategy, clock, _identity_writes, _owner_writes = _strategy(dual=True)
    state, opportunity = _place_first(strategy, clock, "PRE2")
    mirror = strategy.drain_webull_direct_intents()[0]
    strategy.update_position("PRE2", 2, held_qty=2)
    strategy.apply_fanout_outcome(
        FanoutOutcome(
            record_id=uuid4(),
            created_at=datetime.now(UTC),
            symbol="PRE2",
            segment_id=opportunity,
            slot=mirror.metadata["fanout_slot"],
            slot_id=mirror.metadata["fanout_slot_id"],
            attempt_id="preflip-dual",
            outcome="filled",
            evidence_id="preflip-dual-fill",
            broker_account_name=WEBULL,
        )
    )
    _book(
        strategy,
        clock,
        "PRE2",
        _leg(PRIMARY, "pre2-primary"),
        _leg(WEBULL, "pre2-webull"),
    )

    strategy.update_position("PRE2", 0, held_qty=0)
    _book(strategy, clock, "PRE2", _leg(WEBULL, "pre2-webull"))
    assert state.flip_owner_phase == "provisional"
    assert state.flip_owner_opportunity_id == opportunity

    _book(strategy, clock, "PRE2")
    assert state.flip_owner_phase == "idle"
    assert state.flip_owner_opportunity_id == 0


def test_unknown_or_stale_position_evidence_refuses_a_first_rest() -> None:
    strategy, clock, _identity_writes, _owner_writes = _strategy()
    state = strategy.watchlist_state("UNK")
    strategy._queue_resting_place(state, 2.0, slot="first")
    assert strategy.drain_pending_intents() == []

    _book(strategy, clock, "UNK")
    clock[0] += 16_000
    strategy._queue_resting_place(state, 2.0, slot="first")
    assert strategy.drain_pending_intents() == []
    counts = strategy.flip_entry_observability()
    assert counts["admission_evaluated"] == 2
    assert counts["admission_refused_unknown"] == 2


def test_unreadable_restore_refuses_first_rest_through_live_producer() -> None:
    strategy, clock, _identity_writes, _owner_writes = _strategy()
    strategy.configure_flip_entry_ownership(
        lambda *_args: None,
        restore_readable=False,
    )
    state = strategy.watchlist_state("BLINDREST")
    state.bars.append(_bar(clock[0]))
    _book(strategy, clock, "BLINDREST")

    strategy._cw_v2_resting_track(state, _signal(state="short"))

    assert strategy.drain_pending_intents() == []
    counts = strategy.flip_entry_observability()
    assert counts["admission_evaluated"] == 1
    assert counts["admission_refused_unknown"] == 1


def test_open_position_evidence_refuses_first_rest_through_live_producer() -> None:
    strategy, clock, _identity_writes, _owner_writes = _strategy()
    state = strategy.watchlist_state("OPENREST")
    state.bars.append(_bar(clock[0]))
    _book(strategy, clock, "OPENREST")
    state.flip_owner_open_positions = {PRIMARY: _leg(PRIMARY, "open-rest-row")}

    strategy._cw_v2_resting_track(state, _signal(state="short"))

    assert strategy.drain_pending_intents() == []
    counts = strategy.flip_entry_observability()
    assert counts["admission_evaluated"] == 1
    assert counts["admission_refused_unknown"] == 1


def test_replaced_managed_row_fails_closed() -> None:
    strategy, clock, _identity_writes, _owner_writes = _strategy()
    state, _opportunity = _place_first(strategy, clock, "ROW")
    strategy.update_position("ROW", 2, held_qty=2)
    _book(strategy, clock, "ROW", _leg(PRIMARY, "row-one"))
    _book(strategy, clock, "ROW", _leg(PRIMARY, "row-two"))

    assert state.flip_owner_phase == "unknown"
    strategy._queue_resting_place(state, 2.0, slot="first")
    assert strategy.drain_pending_intents() == []


def test_position_row_without_matching_fill_evidence_is_not_adopted() -> None:
    strategy, clock, _identity_writes, _owner_writes = _strategy()
    state, _opportunity = _place_first(strategy, clock, "LATE")

    _book(strategy, clock, "LATE", _leg(PRIMARY, "unattributed-row"))

    assert state.flip_owner_phase == "resting"
    assert state.flip_owner_position_ids == {}

    clock[0] += 15_001
    _book(strategy, clock, "LATE", _leg(PRIMARY, "unattributed-row"))

    assert state.flip_owner_phase == "unknown"
    assert state.flip_owner_position_ids == {}


def test_live_session_reset_without_an_owner_keeps_the_symbol_idle() -> None:
    strategy, clock, _identity_writes, owner_writes = _strategy()
    state = strategy.watchlist_state("EMPTY")
    state.bars.append(_bar(clock[0]))

    strategy._apply_session_anchor_reset(state, clock[0])

    assert state.flip_owner_phase == "idle"
    assert state.flip_owner_opportunity_id == 0
    assert owner_writes == []


def test_sell_flip_with_unreadable_position_evidence_cannot_release_ownership() -> None:
    strategy, clock, identity_writes, _owner_writes = _strategy()
    state, opportunity = _place_first(strategy, clock, "BLIND")
    strategy.update_position("BLIND", 2, held_qty=2)
    _book(strategy, clock, "BLIND", _leg(PRIMARY, "blind-row"))
    _buy_flip(strategy, state, clock)
    assert state.flip_owner_phase == "bound"

    _book(strategy, clock, "BLIND", readable=False)
    state.bars.append(_bar(clock[0] + 60_000))
    strategy._cw_v2_track(state, _signal("SELL", state="short"))

    assert state.flip_owner_phase == "unknown"
    assert state.flip_owner_opportunity_id == opportunity
    assert not any(not active for _symbol, _segment, active, _reason in identity_writes)


def test_close_after_flip_but_before_binding_cannot_masquerade_as_preflip_close() -> None:
    strategy, clock, identity_writes, _owner_writes = _strategy()
    state, opportunity = _place_first(strategy, clock, "RACE")
    strategy.update_position("RACE", 2, held_qty=2)
    _book(strategy, clock, "RACE", _leg(PRIMARY, "race-row"))
    state.flip_owner_evidence_at_ms -= 16_000

    _buy_flip(strategy, state, clock)
    assert state.flip_owner_phase == "provisional"
    _book(strategy, clock, "RACE")

    assert state.flip_owner_phase == "unknown"
    assert state.flip_owner_opportunity_id == opportunity
    counts = strategy.flip_entry_observability()
    assert counts["preflip_close_released"] == 0
    assert counts["preflip_close_unknown"] == 1
    assert not any(not active for _symbol, _segment, active, _reason in identity_writes)


def test_working_first_rest_has_durable_owner_before_any_fill_or_flip() -> None:
    strategy, clock, _identity_writes, owner_writes = _strategy()
    state, opportunity = _place_first(strategy, clock, "REST")

    assert state.flip_owner_phase == "resting"
    record, active, reason = owner_writes[-1]
    assert (record.opportunity_id, record.phase, active, reason) == (
        opportunity,
        "resting",
        True,
        "first_rest_working",
    )

    restored = SchwabV2Strategy(_settings(strict=True))
    restored._now_ms = lambda: clock[0]
    restored.configure_fanout_identity_persistence(
        lambda *_args: None,
        {"REST": opportunity},
    )
    restored.configure_flip_entry_ownership(
        lambda *_args: None,
        active_segments={"REST": opportunity},
        restored={"REST": record},
    )
    restored_state = restored.watchlist_state("REST")
    assert restored_state.flip_owner_phase == "resting"
    assert restored_state.flip_owner_opportunity_id == opportunity


def test_restart_with_owner_restores_consumed_while_ownerless_opportunity_refuses() -> None:
    restored = FlipEntryOwnershipRecord(
        symbol="RST",
        opportunity_id=NOW_MS,
        phase="bound",
        flip_bar_ts=NOW_MS - 1_000,
        provisional_started_ms=NOW_MS - 2_000,
        fill_accounts=(PRIMARY,),
        position_ids={PRIMARY: "restart-row"},
        position_entry_ms={PRIMARY: NOW_MS - 2_000},
    )
    strategy = SchwabV2Strategy(_settings(strict=True))
    strategy._now_ms = lambda: NOW_MS
    strategy.configure_fanout_identity_persistence(lambda *_args: None, {"RST": NOW_MS})
    strategy.configure_flip_entry_ownership(
        lambda *_args: None,
        active_segments={"RST": NOW_MS},
        restored={"RST": restored},
    )
    state = strategy.watchlist_state("RST")
    strategy.apply_flip_position_book(
        FlipPositionBook(NOW_MS, True, {"RST": (_leg(PRIMARY, "restart-row"),)})
    )
    assert state.flip_owner_phase == "bound"
    assert not strategy._strict_first_rest_admitted(state, slot="first")

    orphan = SchwabV2Strategy(_settings(strict=True))
    orphan._now_ms = lambda: NOW_MS
    orphan.configure_fanout_identity_persistence(lambda *_args: None, {"ORPH": NOW_MS})
    orphan.configure_flip_entry_ownership(
        lambda *_args: None,
        active_segments={"ORPH": NOW_MS},
        restored={},
    )
    assert orphan.watchlist_state("ORPH").flip_owner_phase == "unknown"


def test_flag_off_reproduces_ftft_1201_reclaim_without_store_reads() -> None:
    strategy, clock, _identity_writes, owner_writes = _strategy(strict=False)
    clock[0] = int(datetime(2026, 9, 9, 16, 1, tzinfo=UTC).timestamp() * 1000)
    ftft = strategy.watchlist_state("FTFT")
    ftft.bars.append(_bar(clock[0]))
    ftft.cw_armed = True
    ftft.cw_bars_waited = 2
    ftft.cw_segment_high = 2.50
    ftft.last_resting_placed_slot = "first"
    strategy.update_position("FTFT", 2, held_qty=2)
    strategy.update_position("FTFT", 0, held_qty=0)
    strategy._cw_v2_reclaim_resting_track(ftft)
    ftft_reclaim = strategy.drain_pending_intents()
    assert len(ftft_reclaim) == 1
    assert ftft_reclaim[0].metadata["cw_entry_slot"] == "reclaim"
    assert "fanout_identity_schema" not in ftft_reclaim[0].metadata
    assert owner_writes == []


def test_flag_off_reproduces_sune_1306_fresh_first_without_store_reads() -> None:
    strategy, clock, _identity_writes, owner_writes = _strategy(strict=False)
    clock[0] = int(datetime(2026, 9, 9, 17, 6, tzinfo=UTC).timestamp() * 1000)
    sune = strategy.watchlist_state("SUNE")
    sune.bars.append(_bar(clock[0]))
    strategy._cw_v2_resting_track(sune, _signal(state="short"))
    sune_first = strategy.drain_pending_intents()
    assert len(sune_first) == 1
    assert sune_first[0].metadata["cw_entry_slot"] == "first"
    assert "fanout_identity_schema" not in sune_first[0].metadata
    assert owner_writes == []


def test_persistence_failure_refuses_the_first_rest() -> None:
    strategy = SchwabV2Strategy(_settings(strict=True))
    strategy._now_ms = lambda: NOW_MS
    strategy._resting_in_window = lambda now=None: True
    strategy._resting_session_is_eh = lambda now=None: False
    strategy.configure_fanout_identity_persistence(lambda *_args: None)

    def fail_owner_write(*_args: object) -> None:
        raise RuntimeError("store unavailable")

    strategy.configure_flip_entry_ownership(fail_owner_write, restore_readable=True)
    state = strategy.watchlist_state("FAIL")
    _book(strategy, [NOW_MS], "FAIL")
    strategy._queue_resting_place(state, 2.0, slot="first")

    assert strategy.drain_pending_intents() == []
    assert state.flip_owner_phase == "unknown"


def _reclaim_ready_state(
    strategy: SchwabV2Strategy,
    clock: list[int],
) -> SymbolState:
    state = strategy.watchlist_state("OFF")
    state.bars.append(_bar())
    _book(strategy, clock, "OFF")
    state.cw_armed = True
    state.cw_bars_waited = 2
    state.cw_segment_high = 3.0
    state.cw_flip_level = 2.5
    state.cw_bar_low_so_far = 2.8
    return state


def test_strict_mode_disables_the_rested_reclaim_producer() -> None:
    strategy, clock, _identity_writes, _owner_writes = _strategy()
    state = _reclaim_ready_state(strategy, clock)
    placements: list[str] = []
    original_place = strategy._queue_resting_place

    def record_place(
        target: SymbolState, line: float, *, slot: str = "first"
    ) -> None:
        placements.append(slot)
        original_place(target, line, slot=slot)

    strategy._queue_resting_place = record_place

    strategy._cw_v2_reclaim_resting_track(state)

    assert placements == ["reclaim"]
    assert strategy.drain_pending_intents() == []
    counts = strategy.flip_entry_observability()
    assert counts["admission_evaluated"] == 1


def test_strict_mode_disables_the_reactive_reclaim_producer() -> None:
    strategy, clock, _identity_writes, _owner_writes = _strategy()
    state = _reclaim_ready_state(strategy, clock)
    quote = Quote("OFF", 3.09, 3.11, 3.10, clock[0], 0)

    assert strategy._cw_v2_quote(state, quote) is None


def test_strict_mode_keeps_the_first_atr_trail_software_rest_in_extended_hours() -> None:
    settings = _settings(strict=True)
    settings.strategy_schwab_1m_v2_cw_v2_eh_resting_entry_enabled = True
    strategy = SchwabV2Strategy(settings)
    now_ms = int(datetime(2026, 9, 9, 12, 0, tzinfo=UTC).timestamp() * 1000)
    strategy._now_ms = lambda: now_ms
    strategy._resting_in_window = lambda now=None: True
    strategy._resting_session_is_eh = lambda now=None: True
    strategy._liquidity_floor_ok = lambda _state: True
    strategy.configure_fanout_identity_persistence(lambda *_args: None)
    strategy.configure_flip_entry_ownership(lambda *_args: None, restore_readable=True)
    state = strategy.watchlist_state("PRE")
    state.bars.append(_bar(now_ms))
    strategy.apply_flip_position_book(FlipPositionBook(now_ms, True, {}))

    strategy._cw_v2_resting_track(state, _signal(state="short"))
    assert state.resting_active is True
    assert strategy.drain_pending_intents() == []

    intent = strategy.on_quote(
        "PRE",
        Quote("PRE", 2.89, 2.91, 2.90, now_ms, 0),
    )
    assert intent is not None
    assert intent.metadata["cw_entry_slot"] == "first"
    assert intent.metadata["fanout_identity_schema"] == "entry_opportunity_v2"


def test_rollback_ignores_strict_records_and_needs_no_cleanup() -> None:
    strategy = SchwabV2Strategy(_settings(strict=False))
    calls: list[object] = []
    strategy.configure_flip_entry_ownership(
        lambda *args: calls.append(args),
        active_segments={"ROLL": NOW_MS},
        restored={
            "ROLL": FlipEntryOwnershipRecord(
                symbol="ROLL",
                opportunity_id=NOW_MS,
                phase="unknown",
                flip_bar_ts=0,
                provisional_started_ms=NOW_MS,
                fill_accounts=(),
                position_ids={},
                position_entry_ms={},
            )
        },
        restore_readable=False,
    )
    state = strategy.watchlist_state("ROLL")
    assert state.flip_owner_phase == "idle"
    assert calls == []
