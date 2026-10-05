"""Fresh fixture regression and exact reviewer R13/R16 controls, memory only."""
import os
from pathlib import Path
import subprocess
import sys

controls = {
    "fixture_shared_connection": ("tests.unit.test_rpg1_runtime", None, None,
        "tests/unit/test_rpgstuck1.py::test_runtime_worker_reader_close_cannot_rollback_serial_writer"),
    "exact_R13": ("project_mai_tai.strategy_core.schwab_1m_v2",
        "None if soft_rest or primary_blocked", "None if soft_rest",
        "tests/unit/test_rpgstuck1_review_completion.py::test_b1_recorded_reto_unknown_primary_clear_webull_places_webull_only"),
    "exact_R16": ("project_mai_tai.oms.atr_reprice_runtime",
        "self._rpg_persisted_local_open(session, event, candidates)",
        "self._rpg_persisted_local_open(session, event, [])",
        "tests/unit/test_rpgstuck1_review_completion.py::test_b2_any_generation_broker_row_vetoes_recorded_local_proof"),
}
failed = False
for name, (module, old, new, test) in controls.items():
    change = ("module._session_factory = importlib.import_module("
        "'tests.unit.test_oms_webull_mirror_deferred_resubmit')._session_factory") if old is None else f"""
source = inspect.getsource(module)
assert source.count({old!r}) == 1, 'changed mutation anchor'
exec(compile(source.replace({old!r}, {new!r}), module.__file__, 'exec'), module.__dict__)
"""
    run = subprocess.run([sys.executable, "-B", "-c", f"""
import importlib, inspect, pytest
module = importlib.import_module({module!r})
{change}
raise SystemExit(pytest.main(['-q', '-p', 'no:cacheprovider', {test!r}]))
"""], capture_output=True, text=True,
        env={**os.environ, "PYTHONPATH": "src", "PYTHONDONTWRITEBYTECODE": "1"})
    Path(f"/tmp/rpgstuck-fixture-{name}-mutant.txt").write_text(run.stdout + run.stderr)
    killed = run.returncode == 1 and "FAILED tests/unit/" in run.stdout and "AssertionError" in run.stdout
    print(f"{name}: {'KILLED' if killed else 'NOT_PROVEN'} rc={run.returncode}", flush=True)
    failed |= not killed
raise SystemExit(int(failed))
