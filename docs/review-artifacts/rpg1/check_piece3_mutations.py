"""Fourteen-scenario mutations in child-process memory, no broker or source writes."""
import os
from pathlib import Path
import subprocess
import sys


MUTATIONS = {
    "blocked_notice_repeats": ("oms.atr_reprice_runtime",
        'if job.get("blocked_notice_at") is not None:', 'if False:', "unknown_cap"),
    "working_is_clear": ("oms.atr_reprice_handoff",
        'if not readback.can_replace:', 'if readback.outcome == "unknown":', "working_pending"),
    "latest_line_ignored": ("strategy_core.schwab_1m_v2",
        'line = float(state.atr_trail or 0) if slot == "first" else float(state.cw_segment_high)',
        'line = float(old["metadata"]["cw_flip_level"]) if slot == "first" else float(state.cw_segment_high)',
        "two_reprices"),
    "accepted_generation_not_restored": ("strategy_core.schwab_1m_v2",
        'state.resting_schwab_generation = md["rpg_resting_generation"]',
        'state.resting_schwab_generation = "earlier-generation"', "two_reprices"),
    "unknown_slot_unblocked": ("strategy_core.schwab_1m_v2",
        'elif phase not in {"placed", "filled", "expired", "refused"}:',
        'elif phase not in {"placed", "filled", "expired", "refused", "held_unknown"}:', "unknown_cap"),
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
    'tests/unit/test_rpg1_piece3_14.py', '-k', {selected!r}]))
"""
        result = subprocess.run([sys.executable, "-B", "-c", source], cwd=root,
                                env=env, text=True, capture_output=True)
        killed = result.returncode == 1 and "FAILED tests/unit/" in result.stdout
        print(f"{name}: {'RED' if killed else 'NOT_PROVEN'} rc={result.returncode}")
        if not killed:
            print(result.stdout, result.stderr)
        failed |= not killed
    return int(failed)


if __name__ == "__main__":
    raise SystemExit(main())
