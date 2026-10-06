"""Local historical-method replay; requires full Git history, never live cache."""
import ast
import asyncio
import json
import subprocess

import pytest

from project_mai_tai.strategy_core import schwab_1m_v2 as v2_module
from tests.unit.test_t43_olox_cancel_chain import C9_TOKEN, GENERATION, PRIOR_TOKEN, chain_runtime

BASELINE = "aaac903e56dfd40610a7d429162ef562e73c689e"


async def main():
    source = subprocess.check_output([
        "git", "show", f"{BASELINE}:src/project_mai_tai/strategy_core/schwab_1m_v2.py",
    ], text=True)
    definition = next(node for node in ast.parse(source).body
                      if isinstance(node, ast.ClassDef) and node.name == "SchwabV2Strategy")
    for equal_generation in (True, False):
        namespace = dict(vars(v2_module))
        with pytest.MonkeyPatch.context() as patch:
            for node in definition.body:
                if isinstance(node, ast.FunctionDef) and node.name in {
                        "rpg_handoff_authorization", "_rpg_entry_owned", "_queue_resting_place"}:
                    exec(compile(ast.Module(body=[node], type_ignores=[]), "baseline-cache-control", "exec"), namespace)
                    patch.setattr(v2_module.SchwabV2Strategy, node.name, namespace[node.name])
            h = await chain_runtime(patch)
            assert h.state.resting_schwab_quantity == 0 and h.state.resting_active
            assert h.state.resting_schwab_generation == GENERATION
            owned = h.strategy._rpg_entry_owned(h.state, account="live:schwab_1m_v2")
            assert not owned
            if not equal_generation:
                h.state.resting_schwab_generation = ""
            generation_before = h.state.resting_schwab_generation
            quantity_before = h.state.resting_schwab_quantity
            h.strategy._queue_resting_place(h.state, h.state.atr_trail)
            primary = h.strategy.drain_pending_intents()
            mirror = h.strategy.drain_webull_direct_intents()
            print(json.dumps({
                "baseline": BASELINE, "replay_time_et": "2026-10-06 10:16:03",
                "equal_generation": equal_generation, "primary_drafts": len(primary),
                "mirror_drafts": len(mirror), "quantity_before": quantity_before,
                "quantity_after": h.state.resting_schwab_quantity,
                "state_generation_before": generation_before,
                "replacement_generation": GENERATION, "rpg_entry_owned": owned,
                "prior_phase": h.strategy._rpg_handoffs[str(PRIOR_TOKEN)]["phase"],
                "c9_phase": h.strategy._rpg_handoffs[str(C9_TOKEN)]["phase"],
                "limits": "Historical placed reports recorded; cache retention/quotes controlled. Live-cache cause UNMEASURED.",
            }, sort_keys=True))
            assert len(primary) == (0 if equal_generation else 1)
            assert not mirror


if __name__ == "__main__":
    asyncio.run(main())
