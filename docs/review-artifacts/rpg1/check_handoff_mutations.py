"""Coordinator-only mutations, in memory; no broker or application-file writes."""
import os
from pathlib import Path
import subprocess
import sys


MUTATIONS = {
    "unknown_authorizes_replace": (
        "if not readback.can_replace:", "if False:", "unknown_and_working_ack",
    ),
    "retry_spacing_removed": (
        "READ_INTERVAL_SECONDS = 1.0", "READ_INTERVAL_SECONDS = 0.0", "unknown_and_working_ack",
    ),
    "read_budget_removed": (
        'if job["reads"] >= MAX_READS:', "if False:", "unknown_and_working_ack",
    ),
    "partial_ignored": (
        'if readback.outcome == "fills":', "if False:", "synthetic_dollar_partial",
    ),
    "cumulative_fill_can_be_forgotten": (
        'if job.get("no_rebuy"):', "if False:", "synthetic_dollar_partial",
    ),
    "expiry_still_submits": (
        'if decision.verdict != "ready":', "if False:", "cancel_clear_then_expiry",
    ),
    "scope_guard_removed": (
        "if not self._valid_replacement(old, replacement):", "if False:", "cannot_escape_old_order_scope",
    ),
    "restart_can_resubmit": (
        'if job["phase"] != "clear":',
        'if job["phase"] not in {"clear", "submitting", "submit_unknown"}:',
        "crash_after_submit_claim or submit_unknown_after_restart",
    ),
}


def main():
    root = Path(__file__).resolve().parents[3]
    environment = {**os.environ, "PYTHONPATH": str(root / "src"), "PYTHONDONTWRITEBYTECODE": "1"}
    unproven = False
    for name, (old, new, selected) in MUTATIONS.items():
        source = f"""
import inspect
import pytest
import project_mai_tai.oms.atr_reprice_handoff as module
source = inspect.getsource(module)
assert source.count({old!r}) == 1, 'mutation anchor changed'
exec(compile(source.replace({old!r}, {new!r}), module.__file__, 'exec'), module.__dict__)
raise SystemExit(pytest.main(['-q', '-p', 'no:cacheprovider',
    'tests/unit/test_rpg1_reprice_handoff.py', '-k', {selected!r}]))
"""
        result = subprocess.run([sys.executable, "-B", "-c", source], cwd=root,
                                env=environment, text=True, capture_output=True)
        killed = result.returncode == 1 and "FAILED tests/unit/" in result.stdout
        print(f"{name}: {'RED' if killed else 'NOT_PROVEN'} rc={result.returncode}")
        if not killed:
            print(result.stdout, result.stderr)
        unproven |= not killed
    return int(unproven)


if __name__ == "__main__":
    raise SystemExit(main())
