"""In-memory guard removals; source files and live services are never changed."""

import inspect
from pathlib import Path
import subprocess
import sys
import textwrap

MUTANTS = {
    "strict_ask_trigger": ("strategy", "_eh_resting_cross_check",
                           "elif ask < trigger:", "elif False:"),
    "flip_wait": ("strategy", "_cw_v2_resting_track",
                  'if self._pm_rest_feature(state, "pm_flip_wait"):', 'if False:'),
    "software_move": ("strategy", "_reprice_resting",
                      'getattr(getattr(self, "settings", None), "strategy_schwab_1m_v2_pm_rest_reprice_enabled", False)',
                      'False'),
    "handoff_admission": ("strategy", "_queue_resting_cancel",
                          'getattr(settings, "strategy_schwab_1m_v2_atr_reprice_handoff_enabled", False)', 'False'),
    "nfq_price_hold": ("nfq", "_nfq_enabled",
                       'return bool(getattr(self.settings, "oms_v2_webull_mirror_fresh_price_enabled", True))',
                       'return False'),
    "gap_authorization": ("strategy", "rpg_handoff_authorization",
                          'elif self._entries_held or self.gap_hold_active(state.symbol):', 'elif False:'),
}


def main():
    if len(sys.argv) == 1:
        killed = 0
        for name in MUTANTS:
            result = subprocess.run([sys.executable, __file__, name], capture_output=True, text=True)
            good = result.returncode == 1 and "AssertionError" in result.stdout and "FAILED " in result.stdout
            killed += good
            print(f"{name}: {'ASSERTION_RED' if good else 'SURVIVED_OR_ERROR'}", flush=True)
            print(result.stdout, flush=True)
            if result.stderr:
                print(result.stderr, flush=True)
        print(f"all_on_mutations_assertion_red={killed}/{len(MUTANTS)}", flush=True)
        return 0 if killed == len(MUTANTS) else 1
    from project_mai_tai.strategy_core import schwab_1m_v2 as strategy
    from project_mai_tai.oms import mirror_fresh_price as nfq
    import pytest

    namespace, method, before, after = MUTANTS[sys.argv[1]]
    module, cls = (strategy, strategy.SchwabV2Strategy) if namespace == "strategy" else (nfq, nfq.MirrorFreshPriceMixin)
    source = inspect.getsource(getattr(cls, method))
    assert source.count(before) == 1
    scope = dict(vars(module))
    exec(compile(textwrap.dedent(source.replace(before, after)), "<all-on-in-memory-mutant>", "exec"), scope)
    setattr(cls, method, scope[method])
    return pytest.main([str(Path(__file__).with_name("test_all_on_pm.py")), "-q", "--tb=short"])


if __name__ == "__main__":
    raise SystemExit(main())
