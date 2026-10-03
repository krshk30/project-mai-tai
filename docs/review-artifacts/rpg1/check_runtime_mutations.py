"""Mutate imported source in isolated processes, never tracked files or a broker."""
import os
from pathlib import Path
import subprocess
import sys


MUTATIONS = {
    "missing_runtime_dispatch": ("oms.service",
        'and event.payload.metadata.get("atr_reprice") == "true"):', 'and False):',
        "recorded_cancel_clear"),
    "missing_final_wire_guard": ("oms.atr_reprice_runtime",
        "def _rpg_open_refusal(self, event, *, session=None):",
        "def _rpg_open_refusal(self, event, *, session=None):\n        return None",
        "final_wire_guard"),
    "fill_not_accounted": ("oms.atr_reprice_runtime",
        "async def _rpg_record_fill(self, old, report):",
        "async def _rpg_record_fill(self, old, report):\n        return True",
        "controlled_partial_race or recorded_webull_full"),
    "buy_flip_does_not_expire": ("strategy_core.schwab_1m_v2",
        'elif slot == "first" and (state.atr_state != "short"',
        'elif slot == "first" and (False', "current_gate_ends"),
    "liquidity_gate_removed": ("strategy_core.schwab_1m_v2",
        'reason = "other_slot_owned"\n        elif not self._liquidity_floor_ok(state):',
        'reason = "other_slot_owned"\n        elif False:', "current_gate_ends"),
    "placed_latch_ignores_accounting": ("oms.atr_reprice_handoff",
        "def reconcile_feedback(self, token: UUID, job: dict) -> dict:",
        "def reconcile_feedback(self, token: UUID, job: dict) -> dict:\n        return job",
        "v2_restart_uses_committed"),
    "generic_reader_ignores_handoff": ("oms.atr_reprice_runtime",
        "def _rpg_owns_old_order(self, session, order, account):",
        "def _rpg_owns_old_order(self, session, order, account):\n        return False",
        "background_sync or inflight_generic"),
    "retained_identity_map_hides_revocation": ("oms.atr_reprice_handoff",
        "session.get(DashboardSnapshot, token, populate_existing=True)",
        "session.get(DashboardSnapshot, token)", "retained_session_object"),
    "webull_requires_late_broker_id": ("oms.atr_reprice_runtime",
        "if target is None or (not target.broker_order_id and not client_identity) or len(candidates) > 1:",
        "if target is None or not target.broker_order_id or len(candidates) > 1:",
        "client_only_acceptance"),
}


def main():
    root = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else Path(__file__).resolve().parents[3]
    env = {**os.environ, "PYTHONPATH": str(root / "src"), "PYTHONDONTWRITEBYTECODE": "1"}
    failed = False
    for name, (module, old, new, selected) in MUTATIONS.items():
        source = f"""
import importlib, inspect, pytest
module = importlib.import_module('project_mai_tai.' + {module!r})
source = inspect.getsource(module)
assert source.count({old!r}) == 1, 'mutation anchor changed'
exec(compile(source.replace({old!r}, {new!r}), module.__file__, 'exec'), module.__dict__)
raise SystemExit(pytest.main(['-q', '-p', 'no:cacheprovider',
    'tests/unit/test_rpg1_runtime.py', '-k', {selected!r}]))
"""
        result = subprocess.run([sys.executable, "-B", "-c", source], cwd=root,
                                env=env, text=True, capture_output=True)
        killed = result.returncode == 1 and "FAILED tests/unit/" in result.stdout
        print(f"{name}: {'RED' if killed else 'NOT_PROVEN'} rc={result.returncode}")
        if not killed:
            print(result.stdout, result.stderr)
        failed |= not killed
    return int(failed)


if __name__ == "__main__":
    raise SystemExit(main())
