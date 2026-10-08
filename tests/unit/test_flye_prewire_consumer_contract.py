"""Synthetic consumer verdicts only: no token protocol, runtime or historical proof."""

from dataclasses import replace
from datetime import datetime
from uuid import NAMESPACE_URL, uuid5

import pytest

from project_mai_tai.v2_flip_entry_ownership import FlipPositionLeg
from project_mai_tai.v2_removed_wait import RemovedWaitProof
from tests.unit.test_flye_bound_owner_target_close import (
    FLYE, PRIMARY, WEBULL, book, confirm_controlled_next_entry, replay, sell,
)
from tests.unit.test_retryleft1 import strategy_for_record


def synthetic_opportunity(account, pm):
    template, _, recorded, _, _ = replay(account=account, pm=pm)
    record = replace(recorded, opportunity_id=recorded.opportunity_id + 1,
        position_ids={account: str(uuid5(NAMESPACE_URL, "synthetic-prewire-FLYE-" + account))})
    strategy, state, _, clock, writes = strategy_for_record(record,
        datetime.fromisoformat(FLYE["close_poll_utc"]), pm=pm, quantities=(280, 140))
    strategy.settings = template.settings.model_copy()
    strategy._boot_ms = record.retry_segment_id - 60_000
    state.retry_one_watch_start_ms = strategy._boot_ms
    strategy._resting_in_window = lambda now=None: True
    strategy._resting_session_is_eh = lambda now=None: pm
    strategy._eh_resting_enabled = pm
    strategy._liquidity_floor_ok = lambda _state: True
    strategy._resting_stop_ask_allows = lambda *_args, **_kwargs: True
    return strategy, state, record, clock, writes


@pytest.mark.parametrize("pm", [False, True])
@pytest.mark.parametrize("account", [PRIMARY, WEBULL])
@pytest.mark.parametrize("verdict", ["postcoverage_terminal", "legacy_unknown", "submitting_unknown",
                                    "concurrent_submit_unknown", "older_buy_unknown", "open_owned", "unknown_rows"])
def test_synthetic_postcoverage_vs_legacy_consumer_chronology(account, pm, verdict):
    strategy, state, record, clock, writes = synthetic_opportunity(account, pm)
    book(strategy, record, clock)
    request = strategy._removed_wait_requests["FLYE"]
    strategy.drain_pending_intents()
    strategy.drain_webull_direct_intents()
    assert record.opportunity_id != FLYE["opportunity_id"]
    assert request.purpose == "retry_exhausted"
    clock[0] = FLYE["fresh_sell_poll_ms"]
    book(strategy, record, clock)
    sell(strategy, state, clock)
    assert state.flip_owner_phase == "awaiting_close"
    assert strategy._removed_wait_evidence_wakes == {request}

    # Assumed final-producer verdicts, NOT an evaluation of epoch/token absence.
    # Runtime coverage, generation CAS and pre-wire commits belong to shared/F.
    terminal = verdict in {"postcoverage_terminal", "open_owned", "unknown_rows"}
    proof = RemovedWaitProof(request, clock[0], terminal,
        "unbound_symbol_terminal" if terminal else "unbound_schwab_causal_closure_unknown",
        tuple(record.position_ids.items()) if terminal else ())
    before = len(writes)
    strategy.apply_removed_wait_proofs((proof,))
    assert len(writes) == before
    legs = (FlipPositionLeg(account, record.position_ids[account],
                            record.position_entry_ms[account], 1),) if verdict == "open_owned" else ()
    book(strategy, record, clock, legs=legs, readable=verdict != "unknown_rows")
    released = verdict == "postcoverage_terminal"
    assert (state.flip_owner_phase == "idle") is released
    assert state.retry_one_closes_in_segment == 0
    assert ("FLYE" in strategy._removed_wait_requests) is not released
    assert strategy._strict_first_rest_admitted(state, slot="first") is released
    if not released:
        assert not strategy.drain_pending_intents()
        assert not strategy.drain_webull_direct_intents()
        return
    assert writes[-1] == (request, False)
    strategy._queue_resting_place(state, 2.558685, slot="first")
    assert state.resting_active
    if not pm:
        assert strategy.drain_pending_intents()[0].intent_type == "open"
        assert strategy.drain_webull_direct_intents()[0].intent_type == "open"
    confirm_controlled_next_entry(strategy, state, clock)
    assert state.flip_owner_phase == "bound"
    assert not strategy._strict_first_rest_admitted(state, slot="first")
