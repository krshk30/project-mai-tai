"""C1 17:34: exact NCPL handoff; non-retained account/receipt inputs CONTROLLED."""
from collections import Counter
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from ops.health.expected_flags_check import load_catalog, load_numeric_catalog
from project_mai_tai.services.schwab_1m_v2_bot import SchwabV2BotService
from project_mai_tai.settings import Settings
from project_mai_tai.strategy_core.schwab_1m_v2 import FreshSellReleaseEvidence, SchwabV2Config, SchwabV2Strategy
from project_mai_tai.v2_removed_wait import RemovedWaitProof
from tests.unit.test_oct29_ncpl_sell_receipt_cause import controlled, controlled_ack, RECEIPT_AT, SELL_BAR, OLD

FLAG = "strategy_schwab_1m_v2_retry_request_fresh_sell_handoff_enabled"


def setup(*, enabled=True, pm=False, purpose="retry_exhausted"):
    strategy, state, request, clock = controlled()
    setattr(strategy.settings, FLAG, enabled)
    if pm:
        shift = 6 * 3_600_000  # Explicit PM time-shift control, not the original NCPL clock.
        clock[0] -= shift
        strategy._boot_ms -= shift
        state.retry_one_watch_start_ms -= shift
        state.flip_owner_flip_bar_ts -= shift
        state.flip_owner_retry_segment_id -= shift
        state.bars[-1] = replace(state.bars[-1], timestamp_ms=SELL_BAR - shift)
        state.atr_short_flip_bar_ts = state.fresh_sell_owner_release_segment_ms = SELL_BAR - shift
        state.flip_owner_evidence_at_ms = clock[0]
    if purpose != request.purpose:
        request = replace(request, purpose=purpose)
        strategy._removed_wait_requests[state.symbol] = request
    return strategy, state, request, clock


def receipt(strategy, request, clock):
    reason = ("false_flip_leftovers_cancelled_owner_kept" if request.purpose == "false_flip_restore"
              else "retry_leftovers_cancelled_owner_kept")
    proof = RemovedWaitProof(request, clock[0], True, reason)
    assert not strategy.stage_removed_wait_clear(proof)
    assert controlled_ack(strategy, request).retirements == ()
    return proof


def test_flag_on_positive_proof_at_sell_still_releases_after_ordered_ack():
    strategy, state, request, clock = setup()
    strategy._fresh_sell_release_evidence[state.symbol] = FreshSellReleaseEvidence(
        request, clock[0], True, "flat_both_own_cancels_settled")
    assert not strategy._fresh_sell_release_owner(state, None)
    assert len(controlled_ack(strategy, request).retirements) == 2
    assert strategy._fresh_sell_release_owner(state, None)
    assert state.flip_owner_phase == "idle" and not state.cw_resting_taken


@pytest.mark.parametrize("pm", [False, True])
@pytest.mark.parametrize("purpose", ["retry_exhausted", "false_flip_restore"])
def test_late_receipt_after_sell_keeps_exact_request_until_owner_ack(pm, purpose):
    strategy, state, request, clock = setup(pm=pm, purpose=purpose)
    strategy._fresh_sell_release_evidence[state.symbol] = FreshSellReleaseEvidence(
        request, clock[0], False, "own_cancel_settling")
    strategy._end_flip_owner_on_sell(state)
    clock[0] += RECEIPT_AT - 1793294762270
    state.flip_owner_evidence_at_ms = clock[0]
    # Cached request-only acknowledgment models an apply race; it cannot replace owner proof.
    strategy.apply_removed_wait_proofs((receipt(strategy, request, clock),))
    assert strategy._removed_wait_requests[state.symbol] == request
    assert state.flip_owner_phase == "bound" and state.cw_resting_taken
    strategy._fresh_sell_release_evidence[state.symbol] = FreshSellReleaseEvidence(
        request, clock[0], True, "flat_both_own_cancels_settled")
    assert not strategy._fresh_sell_release_owner(state, None)
    plan = controlled_ack(strategy, request)
    assert len(plan.retirements) == 2
    assert strategy._fresh_sell_release_owner(state, None)
    assert state.flip_owner_phase == "idle" and state.fanout_segment_id == 0
    assert not state.cw_resting_taken and not state.cw_reclaim_taken


@pytest.mark.parametrize("pm", [False, True])
def test_flag_off_retains_baseline_request_consumption_and_bound_owner(pm):
    strategy, state, request, clock = setup(enabled=False, pm=pm)
    assert not strategy._retry_request_fresh_sell_handoff_pending(request)
    strategy.apply_removed_wait_proofs((receipt(strategy, request, clock),))
    assert state.symbol not in strategy._removed_wait_requests
    assert (state.symbol, OLD) in strategy._retry_leftover_receipts
    assert state.flip_owner_phase == "bound" and state.fanout_segment_id == OLD
    assert state.cw_resting_taken and state.cw_reclaim_taken


@pytest.mark.parametrize("fault", ["position", "held_position", "working_schwab", "working_webull",
    "partial_fill", "stale_sell", "identity", "stale_position_book", "stale_cancel",
    "cancel_unknown", "different_request", "replayed_sell"])
@pytest.mark.parametrize("pm", [False, True])
def test_negative_cannot_release_owned_or_unknown_episode(fault, pm):
    strategy, state, request, clock = setup(pm=pm)
    proof_request = request
    clear, at = True, clock[0]
    if fault == "position":
        state.position_qty = 1
    elif fault == "held_position":
        state.position_qty_held = 1
    elif fault == "working_schwab":
        state.resting_active = True
    elif fault == "working_webull":
        state.webull_resting_active = True
    elif fault == "partial_fill":
        state.flip_owner_open_positions = {"live:orb": SimpleNamespace(quantity=1)}
    elif fault == "stale_sell":
        state.fresh_sell_owner_release_segment_ms = 0
    elif fault == "identity":
        state.fanout_segment_id = OLD + 1
    elif fault == "stale_position_book":
        state.flip_owner_evidence_at_ms -= 15_001
    elif fault == "stale_cancel":
        at -= 15_001
    elif fault == "cancel_unknown":
        clear = False
    elif fault == "different_request":
        proof_request = replace(request, token="CONTROLLED_OTHER_TOKEN")
    elif fault == "replayed_sell":
        strategy._bar_observation_phase = "replay"
    strategy._fresh_sell_release_evidence[state.symbol] = FreshSellReleaseEvidence(
        proof_request, at, clear, "CONTROLLED_NEGATIVE")
    assert not strategy._fresh_sell_release_owner(state, None)
    assert state.flip_owner_phase == "bound" and state.cw_resting_taken
    assert not strategy._removed_wait_pending_clears


@pytest.mark.asyncio
@pytest.mark.parametrize("pending_before_sell", [False, True])
async def test_existing_poll_defers_request_only_commit_then_commits_owner_plan(pending_before_sell):
    strategy, state, request, clock = setup()
    strategy._removed_wait_enabled = True
    proof = RemovedWaitProof(request, clock[0], True, "retry_leftovers_cancelled_owner_kept")
    if pending_before_sell:
        assert not strategy.stage_removed_wait_clear(proof)
    writes = []
    class Store:
        # Explicit external I/O port; real poll, stage, validity and memory gates run unchanged.
        def clear_rule_proofs(self, requests, accounts):
            assert tuple(requests) == (request,)
            return (proof,)
        def commit_clear(self, plan, valid):
            assert valid()
            writes.append(plan)
    bot = SchwabV2BotService.__new__(SchwabV2BotService)
    bot.strategy, bot.settings, bot._removed_wait_store = strategy, strategy.settings, Store()
    await bot._removed_wait_poll()
    assert writes == [] and not strategy._removed_wait_pending_clears
    assert strategy._removed_wait_requests[state.symbol] == request
    strategy._fresh_sell_release_evidence[state.symbol] = FreshSellReleaseEvidence(
        request, clock[0], True, "flat_both_own_cancels_settled")
    assert not strategy._fresh_sell_release_owner(state, None)
    await bot._removed_wait_poll()
    assert len(writes) == 1 and len(writes[0].retirements) == 2
    assert state.flip_owner_phase == "bound"  # Commit alone never clears memory.
    assert strategy._fresh_sell_release_owner(state, None)
    assert state.flip_owner_phase == "idle"
    assert not strategy._removed_wait_requests


def test_default_off_exact_catalog_inventory_and_projection():
    assert getattr(Settings(), FLAG) is False
    root = Path(__file__).parents[2]
    entries = load_catalog(root / "ops/health/expected_flags.json")
    numeric = load_numeric_catalog(root / "ops/health/expected_numeric.json")
    # ccc94bd2c composed three reviewed diagnostics/WBEXIT3 audit rows.
    additions = {
        "oms_webull_exit3_enabled": "oms",
        "strategy_schwab_1m_v2_pm_softrest_quote_log_enabled": "schwab-1m-v2",
        "strategy_schwab_1m_v2_flip_print_diagnostic_enabled": "schwab-1m-v2",
    }
    assert len(entries) == 157 and len(numeric) == 5
    for name, owner in additions.items():
        added, = [e for e in entries if e["name"] == name]
        assert added["expected"] is True and added["owning_service"] == owner
        assert not added.get("also_check_services")
        assert Settings.model_fields[name].default is False
    assert sum(1 + len(e.get("also_check_services", [])) for e in numeric) == 8
    entry, = [e for e in entries if e["name"] == FLAG]
    assert entry["expected"] is True and entry["owning_service"] == "schwab-1m-v2"
    projection = Counter(s for e in entries for s in [e["owning_service"], *e.get("also_check_services", [])])
    assert sum(projection.values()) == 182


@pytest.mark.parametrize("config", [SchwabV2Config, SimpleNamespace])
def test_public_config_without_additive_flag_has_identical_off_handoff(config):
    _, _, request, _ = controlled()
    strategy = SchwabV2Strategy(config())
    assert not strategy._retry_request_fresh_sell_handoff_pending(request)
