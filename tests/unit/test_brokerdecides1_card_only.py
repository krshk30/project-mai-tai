"""Claude18:21 card-only scope; no retry journal rewrite or historical inference."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from project_mai_tai.brokerdecides1_entry import (
    bind_reconstructed_opportunity, card_fill_segment_definite, entry_decision, reconstructed_first_admitted,
)
from project_mai_tai.strategy_core.schwab_1m_v2 import SchwabV2Strategy
from tests.unit.test_brokerdecides1_reconstructed_first import cap_case


NOW = int(datetime(2026, 10, 22, 22, 30, 15, tzinfo=UTC).timestamp() * 1000)
SHORT = NOW // 60_000 * 60_000 - 120_000


def card_case():
    strategy, state = cap_case()
    state.atr_short_flip_bar_ts = SHORT
    state.retry_one_segment_id = 0  # A note, NOT an invented budget to persist.
    state.cw_seed_cap_watch_start_ms = SHORT + 60_000
    state.bars = [SimpleNamespace(timestamp_ms=SHORT + 60_000)]
    strategy._now_ms = lambda: NOW
    strategy.settings.strategy_schwab_1m_v2_brokerdecides1_max_age_ms = 20_000
    memory = strategy._brokerdecides1_memory
    snapshot = memory.snapshot
    memory.apply(replace(snapshot, generation=2, started_ms=NOW - 200, completed_ms=NOW - 10,
        books=tuple(replace(book, wire_started_ms=NOW - 100, wire_completed_ms=NOW - 20)
                    for book in snapshot.books)))
    return strategy, state


def test_actual_current_short_card_answers_missing_note_without_creating_budget():
    strategy, state = card_case()
    before = (state.retry_one_segment_id, state.retry_one_closes_in_segment,
        state.cw_seed_cap_watch_start_ms, state.cw_resting_taken, state.cw_reclaim_taken)
    assert card_fill_segment_definite(strategy, state)
    assert SchwabV2Strategy._strict_first_rest_admitted(strategy, state, slot="first")
    assert before == (state.retry_one_segment_id, state.retry_one_closes_in_segment,
        state.cw_seed_cap_watch_start_ms, state.cw_resting_taken, state.cw_reclaim_taken)
    assert state.flip_owner_phase == "idle" and not state.flip_owner_opportunity_id


def test_exact_new_opportunity_does_not_expire_card_permission_or_mint_retry_zero():
    strategy, state = card_case()
    assert reconstructed_first_admitted(strategy, state)
    state.fanout_segment_id = state.flip_owner_opportunity_id = NOW
    bind_reconstructed_opportunity(state, NOW)
    assert reconstructed_first_admitted(strategy, state)
    assert state.retry_one_segment_id == 0 and state.retry_one_closes_in_segment == 0


def test_fresh_packet_straddling_fill_session_rollover_is_not_known_coverage():
    strategy, state = card_case()
    boundary = int(datetime(2026, 10, 22, 8, tzinfo=UTC).timestamp() * 1000)
    strategy._now_ms = lambda: boundary + 1000
    state.atr_short_flip_bar_ts = boundary
    memory = strategy._brokerdecides1_memory
    snapshot = memory.snapshot
    memory.apply(replace(snapshot, generation=3, started_ms=boundary - 1000,
        completed_ms=boundary + 100, books=tuple(replace(book,
            wire_started_ms=boundary - 500, wire_completed_ms=boundary + 50)
            for book in snapshot.books)))
    assert entry_decision(strategy, state.symbol).allowed  # Fresh native facts alone.
    assert not card_fill_segment_definite(strategy, state)  # Exact census boundary unknown.
    assert state.retry_one_segment_id == 0


@pytest.mark.parametrize("condition", [
    "working", "position", "inflight", "filled", "unknown_local", "unknown_native",
    "stale_25s", "crossday", "not_minute", "missing_line", "session_roll",
    "missing_snapshot", "clock_back", "reserved", "unreviewed_age_bound",
])
def test_card_unknown_or_positive_economic_evidence_is_not_a_definite_negative(condition):
    strategy, state = card_case()
    memory, snapshot = strategy._brokerdecides1_memory, strategy._brokerdecides1_memory.snapshot
    changes = {"generation": 3}
    if condition in {"working", "position", "unknown_native"}:
        field = {"working": "working", "position": "positions", "unknown_native": "complete"}[condition]
        value = False if condition == "unknown_native" else frozenset({state.symbol})
        changes["books"] = (snapshot.books[0], replace(snapshot.books[1], **{field: value}))
    elif condition == "inflight":
        changes["inflight"] = frozenset({state.symbol})
    elif condition == "filled":
        changes["filled_segments"] = frozenset({(state.symbol, SHORT)})
    elif condition == "unknown_local":
        changes["local_complete"] = False
    elif condition == "stale_25s":
        strategy._now_ms = lambda: NOW + 25_000
    elif condition == "crossday":
        state.atr_short_flip_bar_ts -= 86400_000
    elif condition == "not_minute":
        state.atr_short_flip_bar_ts += 1
    elif condition == "missing_line":
        state.atr_short_flip_bar_ts = 0
    elif condition == "session_roll":
        tomorrow = datetime.fromtimestamp(NOW / 1000, UTC) + timedelta(days=1)
        strategy._now_ms = lambda: int(tomorrow.timestamp() * 1000)
    elif condition == "clock_back":
        strategy._now_ms = lambda: NOW - 1000
    elif condition == "reserved":
        memory.reserve(state.symbol, "unknown-ACK")
    elif condition == "unreviewed_age_bound":
        strategy.settings.strategy_schwab_1m_v2_brokerdecides1_max_age_ms = 30_000
    if condition == "missing_snapshot":
        memory.snapshot = None
    else:
        memory.apply(replace(snapshot, **changes))
    assert not card_fill_segment_definite(strategy, state)
    assert not reconstructed_first_admitted(strategy, state)
    assert state.retry_one_segment_id == 0 and state.cw_resting_taken


@pytest.mark.parametrize("guard", ["unreadable", "spent", "mismatch", "restore", "owner", "phase"])
def test_card_does_not_erase_hard_or_positive_history_note(guard):
    strategy, state = card_case()
    assert card_fill_segment_definite(strategy, state)
    if guard == "unreadable":
        state.retry_one_budget_readable = False
    elif guard == "spent":
        state.retry_one_closes_in_segment = 1
    elif guard == "mismatch":
        state.retry_one_segment_id = SHORT - 60_000
    elif guard == "restore":
        strategy._flip_owner_restore_readable = False
    elif guard == "owner":
        strategy._removed_wait_has_owner = lambda s: True
    else:
        state.flip_owner_phase = "unknown"
    assert not reconstructed_first_admitted(strategy, state)
    assert state.cw_resting_taken and state.cw_reclaim_taken
