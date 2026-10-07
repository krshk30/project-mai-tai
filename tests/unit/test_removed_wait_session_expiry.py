"""[codex] Recorded OLOX opportunity; explicitly controlled request/clock and future episode."""
import copy
import json
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from project_mai_tai.db.models import Base
from project_mai_tai.services.schwab_1m_v2_bot import SchwabV2BotService
from project_mai_tai.v2_removed_wait import RemovedWait, RemovedWaitProof, RemovedWaitStore
from tests.unit.test_clearwait1_removed_wait import _strategy, PRIMARY, WEBULL

RAW = json.loads((Path(__file__).parents[1] / "fixtures/removed_wait_olox_20261006_retained.json").read_text())
OLD = int(RAW["opportunity_id"])


def _ms(value):
    return int(datetime.fromisoformat(value).timestamp() * 1000)


def _case(*, now="2026-10-07T04:01:00-04:00", episode=0, phase="unknown"):
    strategy, _, clock, writes = _strategy()
    # Controlled reported legacy fallback quantities, not measured order sizing.
    strategy._atr_qty, strategy._webull_fanout_qty = 2, 1
    clock[0] = _ms(now)
    state = strategy.watchlist_state("OLOX")
    state.flip_owner_phase = phase
    state.flip_owner_opportunity_id = state.fanout_segment_id = episode
    # Token/time are controlled: the retained census did not capture this removal row.
    request = RemovedWait("OLOX", OLD, "CONTROLLED-OLOX-REMOVAL", _ms("2026-10-06T16:00:00-04:00"), (PRIMARY, WEBULL))
    return strategy, state, clock, writes, request


def _restore(strategy, writes, request):
    strategy.configure_removed_wait(lambda *args: writes.append(("removal", args)),
                                    restored={"OLOX": request}, readable=True)


def test_recorded_olox_episode_restarts_no_stale_qty_barrier_but_unknown_stays_closed():
    assert len(RAW["orders"]) == 12 and all(row["status"] == "cancelled" for row in RAW["orders"])
    assert len(RAW["jobs"]) == 11
    # These terminal rows do not prove the unknown full request; never attest from absence.
    for _ in range(2):
        strategy, state, clock, writes, request = _case(episode=OLD)
        before = copy.deepcopy(state)
        _restore(strategy, writes, request)
        assert not strategy._pending_intents and not strategy._pending_webull_direct_intents
        strategy.expire_removed_wait_requests()
        strategy.apply_removed_wait_proofs([RemovedWaitProof(request, clock[0], False, "dispatch_history_unknown")])
        assert state == before and strategy._removed_wait_requests == {"OLOX": request}
        assert not writes and not strategy._strict_first_rest_admitted(state, slot="first")


def test_current_session_unknown_still_queues_exact_cancellation_and_holds():
    strategy, state, clock, writes, request = _case(now="2026-10-06T16:01:00-04:00", episode=OLD)
    _restore(strategy, writes, request)
    assert [d.quantity for d in strategy._pending_intents] == [2]
    assert [d.quantity for d in strategy._pending_webull_direct_intents] == [1]
    for _ in range(2):
        strategy.expire_removed_wait_requests()
        strategy.apply_removed_wait_proofs([RemovedWaitProof(request, clock[0], False, "dispatch_history_unknown")])
        assert strategy._removed_wait_requests == {"OLOX": request} and not writes
        assert not strategy.line_buy_ready("OLOX")


@pytest.mark.asyncio
async def test_current_unknown_service_poll_keeps_retrying_evidence_without_release():
    strategy, state, clock, writes, request = _case(now="2026-10-06T16:01:00-04:00", episode=OLD)
    _restore(strategy, writes, request)
    before = copy.deepcopy(state)
    proof = RemovedWaitProof(request, clock[0], False, "dispatch_history_unknown")
    store = SimpleNamespace(proofs=Mock(return_value=(proof,)))
    bot = object.__new__(SchwabV2BotService)
    bot.strategy, bot._removed_wait_store = strategy, store
    bot.settings = strategy.settings
    for _ in range(2):
        await bot._removed_wait_poll()
        assert strategy._removed_wait_requests == {"OLOX": request}
        assert state == before and not writes
    assert store.proofs.call_count == 2
    store.proofs.assert_called_with((request,), {PRIMARY, WEBULL})


def test_superseded_request_retirement_is_durable_across_local_store_restart():
    current = _ms("2026-10-07T04:00:01-04:00")
    strategy, state, _, _, request = _case(episode=current)
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    store = RemovedWaitStore(sessionmaker(bind=engine))
    store.record(request, True)
    assert store.restore() == {"OLOX": request}
    before = copy.deepcopy(state)
    strategy.configure_removed_wait(store.record, restored=store.restore(), readable=True)
    assert store.restore() == {} and state == before
    restarted, next_state, _, _, _ = _case(episode=current)
    restarted.configure_removed_wait(store.record, restored=store.restore(), readable=True)
    assert not restarted._removed_wait_requests
    assert next_state.flip_owner_phase == "unknown" and next_state.fanout_segment_id == current
    assert not restarted._pending_intents and not restarted._pending_webull_direct_intents


@pytest.mark.parametrize("phase", ["unknown", "resting", "bound"])
def test_superseded_prior_request_ends_only_bookkeeping_new_same_name_order_untouched(phase):
    current = _ms("2026-10-07T04:00:01-04:00")
    strategy, state, _, writes, request = _case(episode=current, phase=phase)
    state.resting_active = state.webull_resting_active = True
    if phase == "bound":
        state.flip_owner_fill_accounts.add(WEBULL)
        state.flip_owner_position_ids[WEBULL] = "CONTROLLED-CURRENT-POSITION"
        state.position_qty_held = True
    before = copy.deepcopy(state)
    _restore(strategy, writes, request)
    assert strategy._removed_wait_requests == {} and writes == [("removal", (request, False))]
    assert state == before
    assert not strategy._pending_intents and not strategy._pending_webull_direct_intents
    strategy.roll_stale_session_state(strategy._now_ms(), is_protected=lambda *_: True)
    assert state == before


def test_positive_prior_terminal_proof_expires_request_without_releasing_unknown_owner():
    strategy, state, clock, writes, request = _case(episode=OLD)
    _restore(strategy, writes, request)
    before = copy.deepcopy(state)
    # Controlled assessor verdict, not a claim that the legacy OLOX journal exists.
    strategy.apply_removed_wait_proofs([RemovedWaitProof(request, clock[0], True, "terminal_unfilled_removed_wait")])
    assert not strategy._removed_wait_requests and writes == [("removal", (request, False))]
    assert state == before and not strategy._strict_first_rest_admitted(state, slot="first")


@pytest.mark.parametrize("owned", ["fill", "position_id", "open_position", "held", "quantity", "consumed"])
def test_fill_or_position_arriving_after_proof_keeps_prior_episode_owned(owned):
    strategy, state, clock, writes, request = _case(episode=OLD)
    _restore(strategy, writes, request)
    if owned == "fill":
        state.flip_owner_fill_accounts.add(WEBULL)
    elif owned == "position_id":
        state.flip_owner_position_ids[WEBULL] = "CONTROLLED-OWN-POSITION"
    elif owned == "open_position":
        state.flip_owner_open_positions[WEBULL] = "CONTROLLED-OWN-ROW"
    elif owned == "held":
        state.position_qty_held = True
    elif owned == "quantity":
        state.position_qty = 1
    else:
        state.flip_owner_phase = "consumed"
    before = copy.deepcopy(state)
    strategy.apply_removed_wait_proofs([RemovedWaitProof(request, clock[0], True, "terminal_unfilled_removed_wait")])
    assert strategy._removed_wait_requests == {"OLOX": request} and state == before and not writes


@pytest.mark.parametrize("damage", ["missing", "same", "future", "request_current", "stale_proof", "unrecognized_proof", "unreadable", "write_failure"])
def test_incomplete_or_failed_expiry_keeps_request_and_ownership(damage):
    current = _ms("2026-10-07T04:00:01-04:00")
    episode = 0 if damage == "missing" else OLD if damage in {"same", "stale_proof", "unrecognized_proof"} else current
    strategy, state, clock, writes, request = _case(episode=episode)
    if damage == "future":
        state.flip_owner_opportunity_id = state.fanout_segment_id = clock[0] + 60_000
    if damage == "request_current":
        request = replace(request, requested_at_ms=clock[0])
    strategy._removed_wait_requests["OLOX"] = request
    if damage == "unreadable":
        strategy._removed_wait_restore_readable = False
    if damage == "write_failure":
        def fail(*_):
            raise OSError("controlled persistence failure")
        strategy._removed_wait_persist = fail
    before = copy.deepcopy(state)
    proof = None
    if damage in {"stale_proof", "unrecognized_proof"}:
        proof = RemovedWaitProof(request, clock[0] - 600_000 if damage == "stale_proof" else clock[0],
                                 True, "terminal_unfilled_removed_wait" if damage == "stale_proof" else "absence")
    assert not strategy._expire_removed_wait_request(request, proof)
    assert strategy._removed_wait_requests == {"OLOX": request} and state == before


def test_clock_roll_drops_only_prior_token_barriers_and_keeps_unknown_owner():
    strategy, state, clock, writes, request = _case(now="2026-10-07T03:59:59-04:00", episode=OLD)
    _restore(strategy, writes, request)
    assert strategy._pending_intents and strategy._pending_webull_direct_intents
    unrelated = copy.deepcopy(strategy._pending_intents[0])
    unrelated.metadata["clearwait_removal_token"] = "CURRENT-UNRELATED-TOKEN"
    strategy._pending_intents.append(unrelated)
    before = copy.deepcopy(state)
    clock[0] = _ms("2026-10-07T04:00:00-04:00")
    strategy.expire_removed_wait_requests()
    assert strategy._pending_intents == [unrelated] and not strategy._pending_webull_direct_intents
    assert strategy._removed_wait_requests == {"OLOX": request} and state == before and not writes


def test_current_request_with_old_episode_does_not_cancel_current_same_name_order():
    strategy, state, clock, writes, request = _case(episode=OLD)
    request = replace(request, requested_at_ms=clock[0])
    state.resting_active = state.webull_resting_active = True
    before = copy.deepcopy(state)
    _restore(strategy, writes, request)
    strategy.expire_removed_wait_requests()
    assert not strategy._pending_intents and not strategy._pending_webull_direct_intents
    assert strategy._removed_wait_requests == {"OLOX": request} and state == before and not writes
