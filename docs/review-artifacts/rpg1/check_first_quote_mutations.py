"""Run review mutations M1/M4/M5 without changing application files."""
import os
from pathlib import Path
import subprocess
import sys


MUTATIONS = {
    "M1": (
        "elif state.resting_active or state.position_qty_held or state.cw_resting_taken:",
        "elif False:",
        "test_occupied_first_slot_discards_wait_not_merely_blocks_order",
    ),
    "M4": (
        "elif self._entries_held or self.gap_hold_active(state.symbol):",
        "elif False:",
        "test_entry_hold_discards_wait_without_resuming_when_hold_clears",
    ),
    "M5": (
        '        self._finish_first_rest_quote_wait(state, action="gave_up", reason=reason)\n'
        '        if self._flip_owned_first_entry_enabled and (',
        '        if self._flip_owned_first_entry_enabled and (',
        "test_watch_removal_discards_wait_even_when_flip_owner_state_is_retained",
    ),
}


def main():
    root = Path(__file__).resolve().parents[3]
    environment = {**os.environ, "PYTHONPATH": str(root / "src"),
                   "PYTHONDONTWRITEBYTECODE": "1"}
    failed = False
    for name, (old, new, selected) in MUTATIONS.items():
        source = f"""
import inspect
import pytest
import project_mai_tai.strategy_core.schwab_1m_v2 as module
source = inspect.getsource(module)
assert source.count({old!r}) == 1, 'mutation anchor changed'
exec(compile(source.replace({old!r}, {new!r}), module.__file__, 'exec'), module.__dict__)
raise SystemExit(pytest.main(['-q', '-p', 'no:cacheprovider',
    'tests/unit/test_rpg1_pending_first_quote.py', '-k', {selected!r}]))
"""
        result = subprocess.run([sys.executable, "-B", "-c", source], cwd=root,
                                env=environment, text=True, capture_output=True)
        killed = result.returncode == 1 and "FAILED tests/unit/" in result.stdout
        print(f"{name}: {'RED' if killed else 'NOT_PROVEN'} rc={result.returncode}")
        print(result.stdout)
        if result.stderr:
            print(result.stderr, file=sys.stderr)
        failed |= not killed
    return int(failed)


if __name__ == "__main__":
    raise SystemExit(main())
