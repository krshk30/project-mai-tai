"""Run NFQ1's four required destructive mutations in isolated Python processes.

Only in-memory methods/defaults change. No checkout, fixture, or production file is edited.
Run with the current worktree's src on PYTHONPATH and the approved test interpreter.
"""
from __future__ import annotations

import subprocess
import sys


PRELUDE = """
import inspect
import textwrap
import pytest
from project_mai_tai.oms import service
from project_mai_tai.oms import mirror_fresh_price as nfq
from project_mai_tai.settings import Settings
def mutate_method(cls, name, old, new, namespace):
    source = textwrap.dedent(inspect.getsource(getattr(cls, name)))
    assert source.count(old) == 1, (name, old)
    changed = {}
    exec(compile(source.replace(old, new), '<nfq1-mutant>', 'exec'), namespace, changed)
    setattr(cls, name, changed[name])
"""

MUTANTS = {
    "back_to_2s": (
        "nfq.MirrorFreshPriceMixin._mirror_max_age_ms = lambda self: 2000",
        "recorded_3_to_9_second_quotes_reach_wire_shape",
    ),
    "remove_hold": (
        "nfq.MirrorFreshPriceMixin._nfq_pre_submit = lambda self, session, event: None",
        "absent_price_holds_without_broker_order",
    ),
    "remove_duplicate_check": (
        "mutate_method(nfq.MirrorFreshPriceMixin, '_nfq_retirement_reason', "
        "'return \"duplicate_buy\"', 'pass', vars(nfq))",
        "durable_duplicate_buy_guard_rechecks_before_serial_dispatch",
    ),
    "premarket_age_changed": (
        "mutate_method(service.OmsRiskService, '_apply_v2_eh_resting_entry', "
        "'getattr(self.settings, \"oms_v2_eh_resting_entry_quote_max_age_ms\", 2000)', "
        "'getattr(self.settings, \"oms_v2_webull_mirror_quote_max_age_ms\", 10000)', vars(service))",
        "premarket_resting_path_uses_shared_two_seconds_even_with_mirror_ten",
    ),
}


def main() -> int:
    survivors = []
    for name, (mutation, selector) in MUTANTS.items():
        command = PRELUDE + "\n" + mutation + "\n" + (
            "raise SystemExit(pytest.main(['tests/unit/test_nfq1_mirror_fresh_price.py', "
            f"'-q', '--tb=short', '-k', {selector!r}]))"
        )
        result = subprocess.run([sys.executable, "-c", command], capture_output=True, text=True)
        # Collection/runtime setup errors are not mutation kills. pytest exit 1 must contain
        # an actual assertion failure in a selected scenario.
        killed = result.returncode == 1 and "AssertionError" in result.stdout
        print(f"{name}: {'KILLED' if killed else 'NOT KILLED'}")
        print(result.stdout.strip().splitlines()[-1] if result.stdout.strip() else result.stderr)
        if not killed:
            survivors.append(name)
            print(result.stdout, result.stderr)
    return int(bool(survivors))


if __name__ == "__main__":
    raise SystemExit(main())
