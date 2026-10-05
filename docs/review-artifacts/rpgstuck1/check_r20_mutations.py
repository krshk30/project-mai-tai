"""Literal runtime-edge and held-notice controls; memory only, assertion red."""
import os
from pathlib import Path
import subprocess
import sys

controls = {
    "runtime_edge_wake_disabled": ("oms.atr_reprice_runtime",
        "and await asyncio.to_thread(self._rpg_rejected_old_probe_eligible, job)", "and False",
        "r20_recorded_reto_runtime_exhaustion and eligible"),
    "held_notice_dedup_disabled": ("oms.atr_reprice_runtime",
        'if job.get("blocked_notice_at") is not None:', "if False:", "held_notice_replay"),
    "R15_proven_hold_expiry_disabled": ("strategy_core.schwab_1m_v2",
        '"expired" if old_buy_proven_clear(job) else "wait"', '"wait"', "b4_hold"),
    "R15_literal_unproven_hold_expires": ("strategy_core.schwab_1m_v2",
        '"expired" if old_buy_proven_clear(job) else "wait"', '"expired"', "b4_hold"),
}
failed = False
for name, (module, old, new, selected) in controls.items():
    code = f"""
import importlib, inspect, pytest
module = importlib.import_module('project_mai_tai.' + {module!r})
source = inspect.getsource(module)
assert source.count({old!r}) == 1, 'changed mutation anchor'
exec(compile(source.replace({old!r}, {new!r}), module.__file__, 'exec'), module.__dict__)
raise SystemExit(pytest.main(['-q', '-p', 'no:cacheprovider',
    'tests/unit/test_rpgstuck1_runtime_exhaustion.py',
    'tests/unit/test_rpgstuck1_review_completion.py', '-k', {selected!r}]))
"""
    run = subprocess.run([sys.executable, "-B", "-c", code], capture_output=True, text=True,
        env={**os.environ, "PYTHONPATH": "src", "PYTHONDONTWRITEBYTECODE": "1"})
    Path(f"/tmp/rpgstuck-r20-{name}-mutant.txt").write_text(run.stdout + run.stderr)
    killed = run.returncode == 1 and "FAILED tests/unit/" in run.stdout and "AssertionError" in run.stdout
    print(f"{name}: {'KILLED' if killed else 'NOT_PROVEN'} rc={run.returncode}", flush=True)
    failed |= not killed
raise SystemExit(int(failed))
