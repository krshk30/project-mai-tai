"""[codex] Observe prefix-volume reachability without changing repair or trading code."""
from __future__ import annotations

import asyncio
from copy import deepcopy
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "scripts"), str(ROOT / "src"), str(ROOT)]

from line_repair_preopen_replay import repair, replay, setup  # noqa: E402

DECISION_FIELDS = (
    "state", "level", "age", "reading_status", "runtime_flips_ms",
    "waiting_buy_bar_ms", "waiting_buy_trigger", "rebuild_buys",
    "reading_entry_allowed", "runtime_intents",
)


def scale_prefix(row, factor):
    changed = deepcopy(row)
    for bar in changed["preopen"].get("results", []):
        bar["v"] = int(bar["v"] * factor)
    return changed


async def observe(row):
    decision = await replay(row)
    current = decision["reading_bar_ms"]
    case, _ = setup(row, current, admission_control=True)
    await repair(row, case, current)
    state = case.strategy.watchlist_state(row["symbol"])
    prefix = [bar for bar in state.bars if bar.timestamp_ms < row["boundary_ms"]]
    recent = list(state.bars)[-case.strategy.cfg.rel_vol_length:]
    last = state.bars[-1] if state.bars else None
    return {
        "decisions": {field: decision[field] for field in DECISION_FIELDS},
        "general_buffer_prefix_bars": len(prefix),
        "general_buffer_prefix_volume": sum(bar.volume for bar in prefix),
        "relative_volume_window_prefix_bars": sum(
            bar.timestamp_ms < row["boundary_ms"] for bar in recent),
        "relative_volume_window_mean": sum(bar.volume for bar in recent) / len(recent)
            if recent else None,
        "vwap_sum_v": state.vwap_sum_v,
        "vwap_sum_pv": state.vwap_sum_pv,
        "vwap": case.strategy._current_vwap(state, fallback=last.close if last else 0),
        "evaluated_prev_vwap": state.prev_vwap,
        "last_bar_ms": last.timestamp_ms if last else None,
        "liquidity_floor_input_volume": last.volume if last else None,
        "seed_db_bar_writes": case.bot._persist_bar.call_count,
    }


async def audit(row):
    samples = {str(factor): await observe(scale_prefix(row, factor)) for factor in (1, 0, 100)}
    baseline = samples["1"]
    decision_equal = all(sample["decisions"] == baseline["decisions"]
                         for sample in samples.values())
    leaked = baseline["general_buffer_prefix_bars"] > 0
    return {"day": row["day"], "symbol": row["symbol"],
            "prefix_volume_decisions_identical": decision_equal,
            "volume_isolation": "FAIL" if leaked else "NO_PREFIX_CONTROL",
            "samples": samples}


async def main():
    rows = json.loads((ROOT / "tests/fixtures/line_repair_preopen_20261005_09.json")
                      .read_text())["rows"]
    failed = False
    for row in rows:
        result = await audit(row)
        failed |= result["volume_isolation"] == "FAIL" or not result["prefix_volume_decisions_identical"]
        print(json.dumps(result), flush=True)
    return int(failed)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
