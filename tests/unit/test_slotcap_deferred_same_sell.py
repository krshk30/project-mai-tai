"""SLOTCAP controls. Synthetic owner/line operands are not historical OLOX proof."""
import pytest

from project_mai_tai.strategy_core.schwab_1m_v2 import OHLCVBar, SymbolState
from tests.unit.test_slotclear1_fresh_sell import seeded, recorded_probes
from tests.unit.test_v2_flip_owned_first_entry import _book, _leg, PRIMARY


def deferred():
    strategy, state, clock, identities, owners, _ = seeded(partial=True)
    now, bar, signal = list(recorded_probes("SXTC"))[0]
    clock[0] = now
    state.bars.append(bar)
    state.atr_state = "short"
    state.atr_short_flip_bar_ts = bar.timestamp_ms
    state.gap_hold_active = True
    strategy._gap_hold_enabled = True
    strategy._entry_window_closed_for_session = lambda: False
    _book(strategy, clock, state.symbol)
    strategy._observe_atr_sell(state, signal)
    assert state.slotclear_deferred_sell is not None
    assert state.cw_resting_taken and state.cw_reclaim_taken
    return strategy, state, clock, bar, identities, owners


def advance(strategy, state, clock, bar):
    # Five minutes after the SELL: its freshness is proof-at-observation, while
    # the current completed bar and owner evidence must independently be fresh.
    current = OHLCVBar(bar.timestamp_ms + 300000, 1.0, 1.1, 0.9, 1.0, 10000)
    state.bars.append(current)
    clock[0] = current.timestamp_ms + 62000
    _book(strategy, clock, state.symbol)
    state.gap_hold_contiguous_bars = 2 * strategy._atr_period


def test_gap_resume_releases_same_sell_once_without_dispatch_or_new_identity():
    strategy, state, clock, bar, identities, owners = deferred()
    observations = list(strategy.pending_atr_sell_observations())
    budget = (state.retry_one_segment_id, state.retry_one_closes_in_segment)
    assert budget == (bar.timestamp_ms, 0)
    advance(strategy, state, clock, bar)
    writes = (len(identities), len(owners))
    assert strategy._maybe_resume_gap_hold(state)
    assert not state.cw_resting_taken and not state.cw_reclaim_taken
    assert state.slotclear_last_fresh_sell_bar_ms == bar.timestamp_ms
    assert state.slotclear_deferred_sell is None
    state.cw_resting_taken = state.cw_reclaim_taken = True
    strategy._retry_slotclear_deferred_sell(state)
    assert state.cw_resting_taken and state.cw_reclaim_taken
    assert list(strategy.pending_atr_sell_observations()) == observations
    assert (len(identities), len(owners)) == writes
    assert (state.retry_one_segment_id, state.retry_one_closes_in_segment) == budget
    assert strategy.drain_pending_intents() == []
    assert strategy.drain_webull_direct_intents() == []


@pytest.mark.parametrize("field,value", [
    ("flip_owner_phase", "unknown"), ("flip_owner_opportunity_id", 123),
    ("fanout_segment_id", 123), ("flip_owner_flip_bar_ts", 123),
    ("flip_owner_provisional_started_ms", 123), ("flip_owner_first_rest_placed", True),
    ("flip_owner_fill_accounts", {"live:orb"}),
    ("flip_owner_position_ids", {"live:orb": "owned"}),
    ("flip_owner_position_entry_ms", {"live:orb": 123}),
    ("flip_owner_open_positions", 1), ("position_qty", 1), ("position_qty_held", 1),
    ("resting_active", True), ("webull_resting_active", True),
    ("cw_v2_emit_claimed", True), ("fanout_webull_claimed", True),
    ("fanout_claim_outcome", "filled"), ("flip_owner_evidence_readable", False),
    ("flip_owner_evidence_at_ms", 1), ("restart_rest_evidence_pending", True),
    ("restart_owner_unmanaged", True),
])
def test_resume_preserves_every_owner_and_inflight_fence(field, value):
    strategy, state, clock, bar, _, _ = deferred()
    advance(strategy, state, clock, bar)
    if field == "restart_owner_unmanaged":
        # Preserve this original case ID, but use the real producer of unowned
        # exposure instead of dynamically adding a nonexistent state attribute.
        _book(strategy, clock, state.symbol, _leg(PRIMARY, "unowned", entered_ms=clock[0]))
        assert state.flip_owner_phase == "unknown"
        assert state.flip_owner_open_positions
    else:
        setattr(state, field, value)
    assert strategy._maybe_resume_gap_hold(state)
    assert state.cw_resting_taken and state.cw_reclaim_taken
    assert state.slotclear_last_fresh_sell_bar_ms == 0


@pytest.mark.parametrize("gate", ["line", "held", "restore", "removed", "off", "strict_off",
                                  "postclose", "replay", "stale", "gap"])
def test_current_state_gates_still_hold_deferred_release(gate):
    strategy, state, clock, bar, _, _ = deferred()
    advance(strategy, state, clock, bar)
    state.gap_hold_active = False
    if gate == "line":
        strategy.line_buy_ready = lambda symbol: False
    elif gate == "held":
        strategy._entries_held = True
    elif gate == "restore":
        strategy._flip_owner_restore_readable = False
    elif gate == "removed":
        strategy._removed_wait_gate_closed = lambda symbol: True
    elif gate == "off":
        strategy.settings.strategy_schwab_1m_v2_slotclear_fresh_sell_enabled = False
    elif gate == "strict_off":
        strategy._flip_owned_first_entry_enabled = False
    elif gate == "postclose":
        strategy._entry_window_closed_for_session = lambda: True
    elif gate == "replay":
        strategy._bar_observation_phase = "replay"
    elif gate == "stale":
        clock[0] += 600000
    elif gate == "gap":
        state.gap_hold_active = True
    strategy._retry_slotclear_deferred_sell(state)
    assert state.cw_resting_taken and state.cw_reclaim_taken


@pytest.mark.parametrize("change", ["buy", "segment", "watch", "boot", "session", "reset"])
def test_superseded_proof_is_discarded(change):
    strategy, state, clock, bar, _, _ = deferred()
    advance(strategy, state, clock, bar)
    if change == "buy":
        state.atr_state = "long"
    elif change == "segment":
        state.atr_short_flip_bar_ts += 60000
    elif change == "watch":
        state.slotclear_reconstructed_watch_start_ms += 1
    elif change == "boot":
        strategy._boot_ms += 1
    elif change == "session":
        state.bars.append(OHLCVBar(bar.timestamp_ms + 86400000, 1, 1, 1, 1, 1))
    elif change == "reset":
        strategy._reset_atr_indicator_state(state, bar.timestamp_ms + 86400000)
    strategy._retry_slotclear_deferred_sell(state)
    assert state.slotclear_deferred_sell is None
    assert state.cw_resting_taken and state.cw_reclaim_taken


def test_line_readiness_after_resume_retries_current_bar_without_replaying_sell():
    strategy, state, clock, bar, _, _ = deferred()
    advance(strategy, state, clock, bar)
    strategy.line_buy_ready = lambda symbol: False
    strategy._maybe_resume_gap_hold(state)
    assert state.slotclear_deferred_sell is not None
    strategy.line_buy_ready = lambda symbol: True
    strategy._cw_v2_track(state, None)
    assert not state.cw_resting_taken and not state.cw_reclaim_taken
    assert state.slotclear_last_fresh_sell_bar_ms == bar.timestamp_ms
    assert len(strategy.pending_atr_sell_observations()) == 1


@pytest.mark.parametrize("change", ["signal_replay", "handler_replay", "stale", "prewatch",
                                    "wrong_segment", "no_gap", "no_cap", "duplicate"])
def test_non_live_or_unbound_sell_cannot_mint_deferred_proof(change):
    strategy, state, clock, _, _, _ = seeded(partial=True)
    now, bar, signal = list(recorded_probes("SXTC"))[0]
    signal = dict(signal)
    clock[0] = now
    state.bars.append(bar)
    state.gap_hold_active = True
    strategy._gap_hold_enabled = True
    state.atr_short_flip_bar_ts = bar.timestamp_ms
    if change == "signal_replay":
        signal["observation_phase"] = "replay"
    elif change == "handler_replay":
        strategy._bar_observation_phase = "replay"
    elif change == "stale":
        clock[0] += 600000
    elif change == "prewatch":
        state.slotclear_reconstructed_watch_start_ms = bar.timestamp_ms + 60001
    elif change == "wrong_segment":
        state.atr_short_flip_bar_ts -= 60000
    elif change == "no_gap":
        state.gap_hold_active = False
    elif change == "no_cap":
        state.slotclear_reconstructed_watch_start_ms = 0
    elif change == "duplicate":
        state.slotclear_last_fresh_sell_bar_ms = bar.timestamp_ms
    strategy._observe_atr_sell(state, signal)
    assert state.slotclear_deferred_sell is None


def test_gap_held_completed_bar_records_proof_before_entry_tracker_is_reached():
    strategy, state, clock, _, _, _ = seeded(partial=True)
    now, bar, signal = list(recorded_probes("SXTC"))[0]
    clock[0] = now
    state.bars.append(bar)
    state.atr_state = "short"
    state.atr_short_flip_bar_ts = bar.timestamp_ms
    state.gap_hold_active = True
    strategy._gap_hold_enabled = True
    state.gap_hold_contiguous_bars = 1
    assert strategy._evaluate_completed_bar(
        state, is_new_bar=True, restored=True, restored_signal=signal) is None
    assert state.slotclear_deferred_sell == (
        bar.timestamp_ms, state.slotclear_reconstructed_watch_start_ms, strategy._boot_ms)
    assert state.cw_resting_taken and state.cw_reclaim_taken
    assert len(strategy.pending_atr_sell_observations()) == 1
    assert strategy.drain_pending_intents() == []


def test_restart_state_has_no_deferred_proof_from_last_sell_marker():
    strategy, state, clock, bar, _, _ = deferred()
    restarted = SymbolState(symbol=state.symbol)
    restarted.restart_last_live_sell_ms = bar.timestamp_ms
    restarted.atr_short_flip_bar_ts = bar.timestamp_ms
    restarted.cw_resting_taken = restarted.cw_reclaim_taken = True
    strategy._retry_slotclear_deferred_sell(restarted)
    assert restarted.slotclear_deferred_sell is None
    assert restarted.cw_resting_taken and restarted.cw_reclaim_taken


@pytest.mark.parametrize("budget", ["spent", "newer", "unreadable", "persist_failure"])
def test_original_sell_budget_guards_are_not_reset_at_resume(budget):
    strategy, state, clock, _, _, _ = seeded(partial=True)
    now, bar, signal = list(recorded_probes("SXTC"))[0]
    clock[0] = now
    state.bars.append(bar)
    state.atr_state = "short"
    state.atr_short_flip_bar_ts = bar.timestamp_ms
    state.gap_hold_active = True
    strategy._gap_hold_enabled = True
    strategy._entry_window_closed_for_session = lambda: False
    writes = []
    def persist(*args):
        writes.append(args)
        if budget == "persist_failure":
            raise RuntimeError("controlled durable write failure")
    strategy._retry_one_budget_persist = persist
    if budget in {"spent", "newer"}:
        state.retry_one_segment_id = bar.timestamp_ms + (60000 if budget == "newer" else 0)
        state.retry_one_closes_in_segment = 1 + strategy._retry_one_max_retries
    elif budget == "unreadable":
        state.retry_one_budget_readable = False
    original = (state.retry_one_segment_id, state.retry_one_closes_in_segment)
    strategy._observe_atr_sell(state, signal)
    advance(strategy, state, clock, bar)
    strategy._maybe_resume_gap_hold(state)
    assert (state.retry_one_segment_id, state.retry_one_closes_in_segment) == original
    assert not strategy._strict_first_rest_admitted(state, slot="first")
    assert len(writes) == (1 if budget == "persist_failure" else 0)


def test_recorded_bars_after_deferred_sell_use_current_first_rest_once():
    strategy, state, clock, bar, _, _ = deferred()
    current = list(recorded_probes("SXTC"))[3]
    now, current_bar, signal = current
    clock[0] = now
    state.bars.append(current_bar)
    state.atr_state = signal["state"]
    state.atr_trail = signal["trail"]
    state.atr_state_age = signal["state_age"]
    _book(strategy, clock, state.symbol)
    state.gap_hold_contiguous_bars = 2 * strategy._atr_period
    assert strategy._maybe_resume_gap_hold(state)
    assert state.slotclear_last_fresh_sell_bar_ms == bar.timestamp_ms
    strategy._cw_v2_resting_track(state, signal)
    primary = strategy.drain_pending_intents()
    mirror = strategy.drain_webull_direct_intents()
    assert len(primary) == len(mirror) == 1
    assert primary[0].metadata["cw_entry_slot"] == "first"
    assert primary[0].metadata["fanout_slot_id"] == mirror[0].metadata["fanout_slot_id"]
    assert state.retry_one_segment_id == bar.timestamp_ms
    assert state.flip_owner_retry_segment_id == bar.timestamp_ms
    strategy._retry_slotclear_deferred_sell(state)
    strategy._cw_v2_resting_track(state, signal)
    assert strategy.drain_pending_intents() == []
    assert strategy.drain_webull_direct_intents() == []
