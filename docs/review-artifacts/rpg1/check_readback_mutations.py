"""In-memory mutations; never modify the application files or contact a broker."""
import os
from pathlib import Path
import subprocess
import sys


MUTATIONS = {
    "schwab_zero_fallback": (
        'filled = _number(body.get("filledQuantity"))',
        'filled = _number(body.get("filledQuantity")) or request.quantity',
        "recorded_terminal_cancel_explicit_zero or canceled_activity",
    ),
    "schwab_cancel_activity_counted": (
        'if activity.get("executionType") == "CANCELED":', 'if False:',
        "canceled_activity",
    ),
    "schwab_absent_fill_default_zero": (
        'filled = _number(body.get("filledQuantity"))',
        'filled = _number(body.get("filledQuantity", 0))',
        "absent_filled_field",
    ),
    "webull_absent_fill_default_zero": (
        'filled = _number(item.get("filled_qty"))',
        'filled = _number(item.get("filled_qty", 0))',
        "absent_filled_field",
    ),
    "partial_ignored_after_cancel": (
        'if filled > 0:', 'if False:',
        "dollar_size_partial",
    ),
    "webull_absence_is_cancel": (
        'return unknown("unreadable_parent_item")',
        'return AtrBuyReadback("cancelled_empty", "bad", Decimal(0), terminal_cancel=True)',
        "multiple_items",
    ),
    "schwab_wrong_id_allowed": (
        'if str(body.get("orderId", "")) != request.metadata["broker_order_id"]:',
        'if False:',
        "wrong_or_unbound_order",
    ),
    "unknown_execution_count_ignored": (
        'if execution_qty > filled:', 'if False:',
        "positive_execution_contradicts_zero",
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
import project_mai_tai.broker_adapters.atr_buy_readback as module
source = inspect.getsource(module)
assert source.count({old!r}) == 1, 'mutation anchor changed'
exec(compile(source.replace({old!r}, {new!r}), module.__file__, 'exec'), module.__dict__)
raise SystemExit(pytest.main(['-q', '-p', 'no:cacheprovider',
    'tests/unit/test_rpg1_buy_readback.py', '-k', {selected!r}]))
"""
        result = subprocess.run([sys.executable, "-B", "-c", source], cwd=root,
                                env=environment, text=True, capture_output=True)
        killed = result.returncode == 1 and "FAILED tests/unit/" in result.stdout
        print(f"{name}: {'RED' if killed else 'NOT_PROVEN'} rc={result.returncode}")
        if not killed:
            print(result.stdout, result.stderr)
        failed |= not killed
    return int(failed)


if __name__ == "__main__":
    raise SystemExit(main())
