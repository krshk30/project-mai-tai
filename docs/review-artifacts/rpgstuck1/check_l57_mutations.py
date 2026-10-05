"""Reviewer literal paired begin-wake and recovered-active-wake removals."""
import os
from pathlib import Path
import subprocess
import sys

controls = {
    "L5_begin_both_wakes_removed": (
        "    async def _rpg_begin_cancel(self, event):\n"
        "        self._rpg_retry_dirty = True\n"
        "        self._rpg_retry_signal().set()\n",
        "    async def _rpg_begin_cancel(self, event):\n", "l5_idle", 2),
    "L7_recovered_active_wake_removed": (
        '        if starting_phase == "held_unknown" and job["phase"] in ACTIVE_PHASES - {"held_unknown"}:\n'
        "            self._rpg_retry_dirty = True\n"
        "            self._rpg_retry_signal().set()\n",
        "", "l7_recorded", 2),
}
failed = False
for name, (old, new, selected, failures) in controls.items():
    code = f"""
import importlib, inspect, pytest
module = importlib.import_module('project_mai_tai.oms.atr_reprice_runtime')
source = inspect.getsource(module)
assert source.count({old!r}) == 1, 'changed literal reviewer mutation anchor'
exec(compile(source.replace({old!r}, {new!r}), module.__file__, 'exec'), module.__dict__)
raise SystemExit(pytest.main(['-q', '-p', 'no:cacheprovider',
    'tests/unit/test_rpgstuck1_loop_liveness.py', '-k', {selected!r}, '--tb=short']))
"""
    run = subprocess.run([sys.executable, "-B", "-c", code], capture_output=True, text=True,
        env={**os.environ, "PYTHONPATH": "src", "PYTHONDONTWRITEBYTECODE": "1"})
    raw = run.stdout + run.stderr
    Path(f"/tmp/rpgstuck-l57-{name}-mutant.txt").write_text(raw)
    killed = run.returncode == 1 and "AssertionError" in raw and sum(
        line.startswith("FAILED tests/unit/") for line in raw.splitlines()) == failures
    print(f"{name}: {'ASSERTION_RED' if killed else 'NOT_PROVEN'} rc={run.returncode}", flush=True)
    failed |= not killed
raise SystemExit(int(failed))
