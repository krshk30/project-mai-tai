"""Memory-only ALL-ON controls and an explicit unmet literal placement experiment."""
import os
from pathlib import Path
import subprocess
import sys

controls = {
    "runtime_edge_disabled": ("""
import importlib, inspect
module = importlib.import_module('project_mai_tai.oms.atr_reprice_runtime')
old = 'and await asyncio.to_thread(self._rpg_rejected_old_probe_eligible, job)'
source = inspect.getsource(module)
assert source.count(old) == 1
exec(compile(source.replace(old, 'and False'), module.__file__, 'exec'), module.__dict__)
""", "tests/unit/test_rpgstuck1_all_on.py", "all_on_r20 and eligible", 2),
    "catalog_handoff_off": ("""
from ops.health import expected_flags_check as flags
original = flags.load_catalog
def load(path):
    return [dict(entry, expected=False) if entry['name'] == 'strategy_schwab_1m_v2_atr_reprice_handoff_enabled'
            else entry for entry in original(path)]
flags.load_catalog = load
""", "tests/unit/test_expected_flags_check.py", "covers_every_settings_bool", 1),
    "catalog_oms_omitted": ("""
from ops.health import expected_flags_check as flags
original = flags.load_catalog
def load(path):
    return [dict(entry, also_check_services=[]) if entry['name'] == 'strategy_schwab_1m_v2_pm_flip_wait_enabled'
            else entry for entry in original(path)]
flags.load_catalog = load
""", "tests/unit/test_expected_flags_check.py", "covers_every_settings_bool", 1),
    "literal_both_placed_unmet": ("""
import inspect
from tests.unit import test_rpgstuck1_all_on as module
old = 'assert job["phase"] == "refused" and "webull_mirror_precheck_deferred" in job["replacement_reasons"]'
source = inspect.getsource(module)
assert source.count(old) == 1
exec(compile(source.replace(old, 'assert job["phase"] == "placed"'), module.__file__, 'exec'), module.__dict__)
import sys
sys.modules['test_rpgstuck1_all_on'] = module
""", "tests/unit/test_rpgstuck1_all_on.py", "actual_apus0932_distance", 1),
}
failed = False
for name, (setup, path, selected, cases) in controls.items():
    code = setup + f"\nimport pytest\nraise SystemExit(pytest.main(['-q', '-p', 'no:cacheprovider', {path!r}, '-k', {selected!r}]))\n"
    run = subprocess.run([sys.executable, "-B", "-c", code], capture_output=True, text=True,
        env={**os.environ, "PYTHONPATH": "src", "PYTHONDONTWRITEBYTECODE": "1"})
    raw = run.stdout + run.stderr
    Path(f"/tmp/rpgstuck-all-on-{name}.txt").write_text(raw)
    assertion_red = (run.returncode == 1 and "AssertionError" in raw
        and sum(line.startswith("FAILED tests/unit/") for line in raw.splitlines()) == cases)
    label = "UNMET requirement" if name == "literal_both_placed_unmet" else "KILLED control"
    print(f"{name}: {label if assertion_red else 'NOT_PROVEN'} rc={run.returncode} cases={cases}", flush=True)
    failed |= not assertion_red
raise SystemExit(int(failed))
