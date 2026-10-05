"""Assertion-only restart/proof falsifiers; mutate imported memory, never files."""
import os
from pathlib import Path
import subprocess
import sys

MUTATIONS = {
    "restore_blanket_later_intent_veto": ("oms.atr_reprice_runtime",
        'if (row.status == "rejected" and payload.get("refusal_origin") == "client_abort"',
        'if (False and row.status == "rejected" and payload.get("refusal_origin") == "client_abort"',
        "all_eight_and_all_five"),
    "drop_predecessor_proof": ("oms.atr_reprice_runtime",
        'md.get("fanout_predecessor_attempt_id") in local_clients', 'True', "local_retry_chain"),
    "drop_generation_wire_fence": ("oms.atr_reprice_runtime",
        'if candidates or not event.payload.metadata.get("rpg_resting_generation"):',
        'if not event.payload.metadata.get("rpg_resting_generation"):', "local_retry_chain"),
    "drop_local_identity_match": ("oms.atr_reprice_runtime",
        'if not self._rpg_matches_local_open(opening, event):', 'if False:', "local_retry_chain"),
    "drop_terminal_probe_once_claim": ("oms.atr_reprice_runtime",
        'or job.get("terminal_rejection_probe_started_at") is not None', 'or False', "probe_crash_claim"),
    "drop_probe_committed_client_binding": ("oms.atr_reprice_runtime",
        'BrokerOrder.client_order_id == old["client_order_id"], BrokerOrder.symbol == old["symbol"],',
        'BrokerOrder.symbol == old["symbol"],', "probe_requires_exact_committed"),
    "drop_strict_read_parent_binding": ("broker_adapters.atr_buy_readback",
        'if str(body.get("orderId", "")) != request.metadata["broker_order_id"]:',
        'if False:', "probe_unproven_or_filled"),
    "drop_rejected_zero_fill_proof": ("broker_adapters.atr_buy_readback",
        'str(body.get("status", "")).upper() == "REJECTED" and filled == 0',
        'str(body.get("status", "")).upper() == "REJECTED"', "probe_unproven_or_filled"),
    "drop_rejected_zero_remaining_proof": ("broker_adapters.atr_buy_readback",
        '_number(body.get("remainingQuantity")) == 0', 'True', "probe_unproven_or_filled"),
    "drop_concurrent_fill_fence": ("oms.atr_reprice_runtime",
        'and not known_fill)', ')', "zero_cannot_override_fill"),
    "claim_fill_accounted_without_truth_path": ("oms.atr_reprice_runtime",
        'recorded = await self._rpg_record_fill(old, report)', 'recorded = True', "positive_fill_with_price"),
}

root, failed = Path.cwd(), False
for name, (module, old, new, selected) in MUTATIONS.items():
    code = f"""
import importlib, inspect, pytest
module = importlib.import_module('project_mai_tai.' + {module!r})
source = inspect.getsource(module)
assert source.count({old!r}) == 1, 'changed mutation anchor'
exec(compile(source.replace({old!r}, {new!r}), module.__file__, 'exec'), module.__dict__)
raise SystemExit(pytest.main(['-q', '-p', 'no:cacheprovider', 'tests/unit/test_rpgstuck1_startup.py', '-k', {selected!r}]))
"""
    run = subprocess.run([sys.executable, "-B", "-c", code], cwd=root, capture_output=True, text=True,
        env={**os.environ, "PYTHONPATH": str(root / "src"), "PYTHONDONTWRITEBYTECODE": "1"})
    killed = run.returncode == 1 and "FAILED tests/unit/" in run.stdout and "AssertionError" in run.stdout
    print(f"{name}: {'KILLED' if killed else 'NOT_PROVEN'} rc={run.returncode}")
    if not killed:
        print(run.stdout, run.stderr)
    failed |= not killed
raise SystemExit(int(failed))
