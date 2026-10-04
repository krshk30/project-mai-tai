"""Exact surviving review guards plus configured-window/rollback mutations.

Each mutation runs only in a child interpreter; no tracked source or broker writes.
"""
import os
from pathlib import Path
import subprocess
import sys


MUTATIONS = {
    "P3_v2_window_removed": ("strategy_core.schwab_1m_v2",
        'elif not within_rth_entry_window(now, self.settings):', 'elif False:', "current_reprice_callback"),
    "P4_wrong_dispatch_allowed": ("oms.atr_reprice_runtime",
        '                    or self.__dict__.get("_rpg_dispatch_token") != token\n', '', "p4_wrong_dispatch"),
    "P8_terminal_proof_removed": ("broker_adapters.atr_buy_readback",
        '            and self.terminal_cancel\n', '', "p8_dataclass"),
    "F1_final_window_removed": ("oms.atr_reprice_runtime",
        'or not within_rth_entry_window(self._rpg_now(), self.settings)):', 'or False):', "configured_cutoff"),
    "flag_off_ignored": ("strategy_core.schwab_1m_v2",
        'and getattr(settings, "strategy_schwab_1m_v2_atr_reprice_handoff_enabled", False)):',
        'and True):', "flag_off_new"),
    "flag_off_abandons_inflight": ("oms.atr_reprice_runtime",
        'async def _rpg_begin_cancel(self, event):',
        'async def _rpg_begin_cancel(self, event):\n        if not self.settings.strategy_schwab_1m_v2_atr_reprice_handoff_enabled:\n            return []',
        "switch_off_preserves"),
}


def main():
    root = Path(__file__).resolve().parents[3]
    env = {**os.environ, "PYTHONPATH": str(root / "src"), "PYTHONDONTWRITEBYTECODE": "1"}
    failed = False
    for name, (module, old, new, selected) in MUTATIONS.items():
        source = f"""
import importlib, inspect, pytest
module = importlib.import_module('project_mai_tai.' + {module!r})
source = inspect.getsource(module)
assert source.count({old!r}) == 1, 'mutation anchor changed'
exec(compile(source.replace({old!r}, {new!r}), module.__file__, 'exec'), module.__dict__)
raise SystemExit(pytest.main(['-q', '-p', 'no:cacheprovider',
    'tests/unit/test_rpg1_review_guards.py', '-k', {selected!r}]))
"""
        result = subprocess.run([sys.executable, "-B", "-c", source], cwd=root,
                                env=env, text=True, capture_output=True)
        killed = result.returncode == 1 and "FAILED tests/unit/" in result.stdout
        print(f"{name}: {'RED' if killed else 'NOT_PROVEN'} rc={result.returncode}")
        print(result.stdout)
        if result.stderr:
            print(result.stderr, file=sys.stderr)
        failed |= not killed
    return int(failed)


if __name__ == "__main__":
    raise SystemExit(main())
