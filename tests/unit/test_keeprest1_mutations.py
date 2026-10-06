"""Assertion-red semantic mutations in memory; never rewrite strategy files."""

import inspect
import textwrap

import pytest

from project_mai_tai.strategy_core.schwab_1m_v2 import SchwabV2Strategy
from tests.unit import test_keeprest1 as proof

MUTANTS = [
    ("waiting_disabled", "_cw_v2_resting_track",
     "if keep_rest and state.resting_active and not state.resting_flip_ms:",
     "if False and state.resting_active and not state.resting_flip_ms:", "wait"),
    ("taken_cross_misclassified_waiting", "_cw_v2_resting_track",
     "if keep_rest and state.resting_active and not state.resting_flip_ms:",
     "if keep_rest and state.resting_active:", "taken"),
    ("frozen_price_moves", "_cw_v2_resting_track",
     "if state.resting_buy_frozen:\n", "if state.resting_buy_frozen:\n            state.resting_level *= 0.5\n", "wait"),
    ("sell_cleanup_removed", "_cw_v2_track",
     "if state.resting_buy_frozen and state.resting_active and not state.resting_flip_ms:",
     "if False and state.resting_active and not state.resting_flip_ms:", "sell"),
    ("thin_bar_cleanup_removed", "_cw_v2_resting_track",
     "if bar_ms > state.resting_frozen_floor_bar_ms:", "if False:", "thin"),
    ("thin_quote_callbacks_count_as_bars", "_cw_v2_resting_track",
     "if bar_ms > state.resting_frozen_floor_bar_ms:", "if True:", "thin"),
    ("fill_freeze_latch_not_cleared", "_clear_resting_fill_latch",
     "state.resting_buy_frozen = False", "state.resting_buy_frozen = True", "fill"),
    ("cross_dedup_removed", "_eh_resting_cross_check",
     "if state.position_qty != 0 or state.resting_flip_ms:", "if state.position_qty != 0:", "cross"),
]


@pytest.mark.parametrize("mutation", MUTANTS, ids=[row[0] for row in MUTANTS])
def test_keeprest1_mutation_is_assertion_red(monkeypatch, caplog, mutation):
    name, method_name, old, new, target = mutation
    original = getattr(SchwabV2Strategy, method_name)
    source = textwrap.dedent(inspect.getsource(original))
    assert source.count(old) == 1, f"mutation drift: {name}"
    namespace = {}
    exec(compile(source.replace(old, new), f"<KEEPREST1:{name}>", "exec"),
         original.__globals__, namespace)
    mutated = namespace[method_name]
    if method_name == "_clear_resting_fill_latch":
        mutated = staticmethod(mutated)
    monkeypatch.setattr(SchwabV2Strategy, method_name, mutated)
    # Only assertion failure counts as killed. Import/setup/runtime errors do not.
    with pytest.raises(AssertionError):
        if target == "wait":
            proof.test_waiting_buy_keeps_exact_line_for_hours_without_new_timer(False, True, caplog)
        elif target == "taken":
            proof.test_recorded_bot_shape_waiting_buy_or_taken_cross_is_not_relabelled(proof.CASES[2])
        elif target == "sell":
            proof.test_sell_cancels_one_owned_waiting_order_and_mirror(True)
        elif target == "thin":
            proof.test_three_completed_thin_bars_cancel_not_three_evaluations()
        elif target == "fill":
            proof.test_waiting_freeze_canonical_cleanup("fill")
        else:
            proof.test_all_on_later_olox_price_proxy_crosses_once_with_sizing_and_grace(False)
