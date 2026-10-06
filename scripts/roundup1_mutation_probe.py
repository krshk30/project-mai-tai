"""Run one in-memory mutation; never rewrite production source on disk."""

import argparse
import inspect
import textwrap
from decimal import ROUND_DOWN

import pytest

from project_mai_tai.oms.service import OmsRiskService
from project_mai_tai.strategy_core.schwab_1m_v2 import SchwabV2Strategy
from project_mai_tai.strategy_core import v2_entry_sizing as sizing


MUTATIONS = {
    "nearest": (sizing.resting_buy_stop, "rounding=ROUND_CEILING", "rounding=ROUND_HALF_UP"),
    "nearest_schwab_wire": (sizing.resting_buy_stop, "rounding=ROUND_CEILING", "rounding=ROUND_HALF_UP"),
    "nearest_webull_wire": (sizing.resting_buy_stop, "rounding=ROUND_CEILING", "rounding=ROUND_HALF_UP"),
    "nearest_pm_compare": (sizing.resting_buy_stop, "rounding=ROUND_CEILING", "rounding=ROUND_HALF_UP"),
    "floor": (sizing.resting_buy_stop, "rounding=ROUND_CEILING", "rounding=ROUND_DOWN"),
    "wrong_subdollar_tick": (sizing.resting_buy_stop,
        'if raw_four_decimal_price >= 1 else Decimal("0.0001")',
        'if raw_four_decimal_price >= 1 else Decimal("0.01")'),
    "raw_limit_sizing": (SchwabV2Strategy._queue_resting_place,
        'price=schwab_wire_limit, basis="stop_limit_wire_limit"',
        'price=raw_stop * Decimal("1.005"), basis="stop_limit_wire_limit"'),
    "pair_lift_removed": (sizing.resting_wire_limit,
        "if limit > stop and wire_limit <= wire_stop:", "if False:"),
    "pm_raw_trigger": (SchwabV2Strategy._eh_resting_cross_check,
        "trigger = self._active_resting_trigger(state)",
        "trigger = state.resting_level * (1 + self._resting_offset_pct_value() / 100)"),
    "restore_recalculates": (SchwabV2Strategy.rpg_handoff_authorization,
        'wire_stop = float(wire["stop_price"])',
        'wire_stop = self._resting_trigger_for_line(float(md["cw_flip_level"]))'),
    "legacy_exit_reference": (OmsRiskService._apply_v2_oco_bracket_entry,
        "entry = float(entry_ref)",
        'entry = float(md["cw_flip_level"]) * 1.005 if md.get("resting_buy_round_up") == "true" else float(entry_ref)'),
    "unproven_restore_releases": (SchwabV2Strategy._rpg_entry_owned,
        'if (self._resting_round_up_enabled() and phase == "placed"',
        'if (False and phase == "placed"'),
    "pa1_copies_nearest": (OmsRiskService._evaluate_webull_mirror_deferred_resubmits,
        'if (getattr(self.settings, "strategy_schwab_1m_v2_resting_buy_round_up_enabled", False)',
        'if (False'),
}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("name", choices=MUTATIONS)
    args = parser.parse_args()
    fn, old, new = MUTATIONS[args.name]
    source = textwrap.dedent(inspect.getsource(fn))
    if old not in source:
        raise RuntimeError("mutation site missing")
    fn.__globals__["ROUND_DOWN"] = ROUND_DOWN
    namespace = {}
    exec(compile(source.replace(old, new), f"<ROUNDUP1-{args.name}>", "exec"), fn.__globals__, namespace)
    fn.__code__ = namespace[fn.__name__].__code__
    selections = {
        "nearest_schwab_wire": "recorded_sckt_strategy_oms_adapter_wire_is_ceiling_not_nearest and schwab",
        "nearest_webull_wire": "recorded_sckt_strategy_oms_adapter_wire_is_ceiling_not_nearest and webull",
        "nearest_pm_compare": "recorded_meds_print_below_ceiling_cannot_cross_or_take_slot",
    }
    selection = ["-k", selections[args.name]] if args.name in selections else []
    rc = pytest.main(["-q", "tests/unit/test_roundup1.py", *selection, "--tb=short",
                      f"--junitxml=/tmp/roundup1-mutation-{args.name}.xml"])
    print(f"mutation={args.name} pytest_rc={rc} verdict={'RED' if rc == 1 else 'SURVIVED' if rc == 0 else 'UNMEASURED'}")
    raise SystemExit(0 if rc == 1 else 1)


if __name__ == "__main__":
    main()
