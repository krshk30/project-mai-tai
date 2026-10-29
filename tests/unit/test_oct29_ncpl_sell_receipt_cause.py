"""Shared CONTROLLED NCPL state for the ON/OFF guard regressions.

The original opposite 1PASS/1FAIL diagnostic is retained byte-for-byte in
analysis/OCT29-NCPL-SLOT-BASELINE-CAUSE-FIXTURE-IMMUTABLE-FIRST.py
and Git034686d3d. It is historical cause evidence, not a shipping failure test.
"""
from pathlib import Path

from project_mai_tai.settings import Settings
from project_mai_tai.strategy_core.schwab_1m_v2 import (
    FreshSellReleaseEvidence, OHLCVBar, SchwabV2Strategy,
)
from project_mai_tai.v2_removed_wait import RemovedWait, RemovedWaitProof

OLD = 1793291104243
SELL_BAR = 1793294700000
SELL_AT = 1793294762270
RECEIPT_AT = 1793294767056
ACCOUNTS = ("live:schwab_1m_v2", "live:orb")
LOG = Path("/Users/velkris/.codex/task-queues/analysis/"
           "OCT29-C6-ORIGINAL-CASE-RECEIPTS/P1-OWNER-BOUND/v2_NCPL.log")


def controlled():
    clock = [SELL_AT]
    strategy = SchwabV2Strategy(Settings(
        strategy_schwab_1m_v2_flip_owned_first_entry_enabled=True,
        strategy_schwab_1m_v2_cw_v2_enabled=True,
        strategy_schwab_1m_v2_fresh_sell_owner_release_enabled=True,
        strategy_schwab_1m_v2_removed_wait_clear_rules_enabled=True,
        strategy_schwab_1m_v2_account_name=ACCOUNTS[0],
        strategy_schwab_1m_v2_webull_account_name=ACCOUNTS[1],
    ))
    strategy._now_ms = lambda: clock[0]
    strategy._boot_ms = OLD - 60_000  # CONTROLLED boot/watch, not retained process proof.
    strategy.configure_fanout_identity_persistence(lambda *_: None)
    strategy.configure_flip_entry_ownership(lambda *_: None, restore_readable=True)
    request = RemovedWait("NCPL", OLD, "CONTROLLED_NOT_RETAINED_TOKEN",
                          SELL_AT - 12_173, ACCOUNTS, "retry_exhausted")
    strategy.configure_removed_wait(lambda *_: None, restored={"NCPL": request}, readable=True)
    state = strategy.watchlist_state("NCPL")
    state.fanout_segment_id = state.flip_owner_opportunity_id = OLD
    state.flip_owner_phase = "bound"
    state.flip_owner_flip_bar_ts = OLD - 4_243
    state.flip_owner_first_rest_placed = True
    state.cw_resting_taken = state.cw_reclaim_taken = True
    state.retry_one_watch_start_ms = strategy._boot_ms
    state.flip_owner_retry_segment_id = OLD - 60_000
    state.atr_short_flip_bar_ts = SELL_BAR
    state.atr_state = "short"
    state.bars = [OHLCVBar(SELL_BAR, 1.72, 1.72, 1.72, 1.72, 100)]
    # Positive empty owned-position/readable state is CONTROLLED; not recovered from logs.
    state.flip_owner_evidence_readable = True
    state.flip_owner_evidence_at_ms = clock[0]
    strategy._latch_fresh_sell_owner_release_boundary(state, {"flip": "SELL", "observation_phase": "live"})
    assert state.fresh_sell_owner_release_segment_ms == SELL_BAR
    return strategy, state, request, clock


def controlled_ack(strategy, request):
    """Model the existing off-loop durable acknowledgement, with no fixture DB."""
    plan = strategy._removed_wait_pending_clears.pop(request)
    assert strategy.removed_wait_clear_valid(plan)
    strategy._removed_wait_committed_clears[request] = plan
    return plan
