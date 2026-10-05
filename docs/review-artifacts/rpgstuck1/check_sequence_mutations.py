"""Prove the new wire-level cases detect raw guards and missing OMS rounding."""
import os
from pathlib import Path
import subprocess
import sys

mutations = {
    "restore_raw_authorization_comparison": ("oms.atr_reprice_runtime",
        "def _rpg_canonical_prices(self, md, account):",
        'def _rpg_canonical_prices(self, md, account):\n        return Decimal(md["stop_price"]), Decimal(md["limit_price"])'),
    "omit_actual_oms_stop_rounding": ("oms.service",
        'md["stop_price"] = _schwab_round(float(md["stop_price"]))',
        'md["stop_price"] = str(md["stop_price"])'),
    "omit_actual_oms_limit_rounding": ("oms.service",
        'md["limit_price"] = _schwab_round(float(md["limit_price"]))',
        'md["limit_price"] = str(md["limit_price"])'),
}
root, failed = Path.cwd(), False
for name, (module, old, new) in mutations.items():
    code = f"""
import importlib, inspect, pytest
module = importlib.import_module('project_mai_tai.' + {module!r})
source = inspect.getsource(module)
assert source.count({old!r}) == 1, 'changed mutation anchor'
exec(compile(source.replace({old!r}, {new!r}), module.__file__, 'exec'), module.__dict__)
raise SystemExit(pytest.main(['-q', '-p', 'no:cacheprovider', 'tests/unit/test_rpgstuck1_schwab_sequences.py']))
"""
    result = subprocess.run([sys.executable, "-B", "-c", code], cwd=root, capture_output=True, text=True,
        env={**os.environ, "PYTHONPATH": str(root / "src"), "PYTHONDONTWRITEBYTECODE": "1"})
    killed = result.returncode == 1 and "FAILED tests/unit/" in result.stdout
    print(f"{name}: {'KILLED' if killed else 'NOT_PROVEN'} rc={result.returncode}")
    if not killed:
        print(result.stdout, result.stderr)
    failed |= not killed
raise SystemExit(int(failed))
