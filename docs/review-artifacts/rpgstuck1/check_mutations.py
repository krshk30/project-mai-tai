"""Assertion-only falsifiers, isolated in imported memory; no tracked-file writes."""
import os
from pathlib import Path
import subprocess
import sys

MUTATIONS = {
    "raw_price_comparison": ("oms.atr_reprice_runtime",
        'def _rpg_canonical_prices(self, md, account):',
        'def _rpg_canonical_prices(self, md, account):\n        return Decimal(md["stop_price"]), Decimal(md["limit_price"])',
        "recorded_e3_authorization"),
    "drop_canonical_difference_guard": ("oms.atr_reprice_runtime",
        'def _rpg_canonical_prices(self, md, account):',
        'def _rpg_canonical_prices(self, md, account):\n        return (0, 0)',
        "canonical_real_difference"),
    "release_without_clear_proof": ("strategy_core.schwab_1m_v2",
        'and not old_buy_proven_clear(job):', 'and False:',
        "terminal_release_requires"),
    "distance_unknown_relabelled_local": ("oms.atr_reprice_runtime",
        'elif (deferred is not None and deferred.local_no_wire',
        'elif (deferred is not None',
        "distance_hold_cannot"),
    "admission_log_flood": ("strategy_core.schwab_1m_v2",
        'if logged.get(state.symbol) == bar_key:', 'if False:',
        "twenty_bars_admission"),
    "admission_permission_cached": ("strategy_core.schwab_1m_v2",
        'if logged.get(state.symbol) == bar_key:\n            return allowed',
        'if logged.get(state.symbol) == bar_key:\n            return True',
        "twenty_bars_admission"),
    "same_segment_terminal_latch": ("strategy_core.schwab_1m_v2",
        'and not old_buy_proven_clear(job):', 'and job["segment_id"] == state.fanout_segment_id:',
        "actual_apus_refused or actual_veea_unknown"),
    "durable_uncertain_dispatch_ignored": ("oms.atr_reprice_runtime",
        'if (row.status != "rejected" or payload.get("refusal_origin") != "skipped_before_submit"\n                    or payload.get("refusal_code") != "webull_mirror_precheck_deferred"):',
        'if False:', "actual_veea_persisted_precheck"),
    "distance_retry_missing_durable_claim": ("oms.service",
        'or self._is_webull_mirror_deferred_resubmit(event)):', 'or False):',
        "distance_retry_persists"),
}

root = Path.cwd()
failed = False
for name, (module, old, new, selected) in MUTATIONS.items():
    code = f"""
import importlib, inspect, pytest
module = importlib.import_module('project_mai_tai.' + {module!r})
source = inspect.getsource(module)
assert source.count({old!r}) == 1, 'changed mutation anchor'
exec(compile(source.replace({old!r}, {new!r}), module.__file__, 'exec'), module.__dict__)
raise SystemExit(pytest.main(['-q', '-p', 'no:cacheprovider', 'tests/unit/test_rpgstuck1.py', '-k', {selected!r}]))
"""
    run = subprocess.run([sys.executable, "-B", "-c", code], cwd=root, capture_output=True, text=True,
        env={**os.environ, "PYTHONPATH": str(root / "src"), "PYTHONDONTWRITEBYTECODE": "1"})
    killed = run.returncode == 1 and "FAILED tests/unit/" in run.stdout
    print(f"{name}: {'KILLED' if killed else 'NOT_PROVEN'} rc={run.returncode}")
    if not killed:
        print(run.stdout, run.stderr)
    failed |= not killed
raise SystemExit(int(failed))
