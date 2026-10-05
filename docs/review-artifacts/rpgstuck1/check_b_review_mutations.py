"""Sixteen B1-B6/later-fill falsifiers; assertion failures only, memory only."""
import os
from pathlib import Path
import subprocess
import sys

MUTATIONS = {
    "B1_global_webull_veto": ("strategy_core.schwab_1m_v2",
        "webull_blocked = self._rpg_entry_owned(state, account=webull_account)",
        "webull_blocked = self._rpg_entry_owned(state)", "b1_recorded"),
    "B1_primary_generation_mutated": ("strategy_core.schwab_1m_v2",
        "if not primary_blocked:\n            state.resting_schwab_generation = generation",
        "if True:\n            state.resting_schwab_generation = generation", "b1_recorded"),
    "B1_primary_quantity_mutated": ("strategy_core.schwab_1m_v2",
        "if not primary_blocked:\n            state.resting_schwab_quantity = int(schwab_sized[0]) if schwab_sized else 0",
        "if True:\n            state.resting_schwab_quantity = int(schwab_sized[0]) if schwab_sized else 0", "b1_recorded"),
    "B2_generation_wire_ignored": ("oms.atr_reprice_runtime",
        'if candidates or not event.payload.metadata.get("rpg_resting_generation"):',
        'if not event.payload.metadata.get("rpg_resting_generation"):', "b2_any"),
    "B3_precheck_origin_ignored": ("oms.atr_reprice_runtime",
        'payload.get("refusal_origin") != "skipped_before_submit"', "False", "b3_only"),
    "B3_precheck_code_ignored": ("oms.atr_reprice_runtime",
        'payload.get("refusal_code") != "webull_mirror_precheck_deferred"', "False", "b3_only"),
    "B4_unproven_hold_expires": ("strategy_core.schwab_1m_v2",
        '"expired" if old_buy_proven_clear(job) else "wait"', '"expired"', "b4_hold"),
    "B5_empty_turns_query": ("oms.atr_reprice_runtime",
        "if startup or active or pending or dirty:", "if True:", "b5_startup_then180"),
    "B5_notifications_dropped": ("oms.atr_reprice_runtime",
        "tokens = await asyncio.to_thread(self._rpg_mark_committed_evidence, event)", "tokens = []", "b5_matching"),
    "B5_wrong_generation_wakes": ("oms.atr_reprice_runtime",
        'DashboardSnapshot.payload["old"]["metadata"]["rpg_resting_generation"].as_string() == generation',
        "True", "b5_matching"),
    "B5_duplicate_hash_ignored": ("oms.atr_reprice_runtime",
        'if job.get("local_evidence_wakeup_hash") == digest:', "if False:", "b5_matching"),
    "B5_hash_not_durable": ("oms.atr_reprice_runtime",
        "local_evidence_wakeup_hash=digest", 'local_evidence_wakeup_hash=""', "b5_matching"),
    "B5_failed_scan_spins": ("oms.atr_reprice_runtime",
        "delay = max(1.0, float(self.settings.oms_broker_sync_interval_seconds))", "delay = 0.0", "b5_failed_startup"),
    "B6_probe_repeats": ("oms.atr_reprice_runtime",
        'or job.get("terminal_rejection_probe_started_at") is not None', "or False", "probe_crash_claim"),
    "B6_remaining_zero_ignored": ("broker_adapters.atr_buy_readback",
        '_number(body.get("remainingQuantity")) == 0', "True", "probe_unproven_or_filled"),
    "later_accounted_fill_ignored": ("oms.atr_reprice_handoff",
        'if filled is not None or order.status in {"filled", "partially_filled"}:',
        "if False:", "later_recorded_sckt_fill_overrides"),
}

root, failed = Path.cwd(), False
for name, (module, old, new, selected) in MUTATIONS.items():
    code = f"""
import importlib, inspect, pytest
module = importlib.import_module('project_mai_tai.' + {module!r})
source = inspect.getsource(module)
assert source.count({old!r}) == 1, 'changed mutation anchor'
exec(compile(source.replace({old!r}, {new!r}), module.__file__, 'exec'), module.__dict__)
raise SystemExit(pytest.main(['-q', '-p', 'no:cacheprovider',
    'tests/unit/test_rpgstuck1_review_completion.py', 'tests/unit/test_rpgstuck1_startup.py',
    'tests/unit/test_rpgstuck1_later_startup.py', '-k', {selected!r}]))
"""
    run = subprocess.run([sys.executable, "-B", "-c", code], cwd=root, capture_output=True, text=True,
        env={**os.environ, "PYTHONPATH": str(root / "src"), "PYTHONDONTWRITEBYTECODE": "1"})
    killed = run.returncode == 1 and "FAILED tests/unit/" in run.stdout and "AssertionError" in run.stdout
    print(f"{name}: {'KILLED' if killed else 'NOT_PROVEN'} rc={run.returncode}", flush=True)
    if not killed:
        print(run.stdout, run.stderr, flush=True)
    failed |= not killed
raise SystemExit(int(failed))
