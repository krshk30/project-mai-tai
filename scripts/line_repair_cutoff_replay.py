"""[codex] Offline chart gate; source reads never become live bars or orders."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT)]

from project_mai_tai.backtest.atr_oracle import Bar, compute_atr_trail  # noqa: E402
from project_mai_tai.market_data.schwab_v2_rest_client import SchwabV2RestClient  # noqa: E402
from tests.line_restore_acceptance_factory import make_line_restore_case  # noqa: E402


async def main():
    fixture = json.loads((ROOT / "tests/fixtures/line_repair_cutoff_1009_fresh.json").read_text())
    failures = 0
    for row in fixture["rows"]:
        current = row["cutoff_ms"] - 11 * 60_000  # 07:09 ET
        case = make_line_restore_case(symbol=row["symbol"], now_ms=current + 61_000)
        client = SchwabV2RestClient(case.settings, on_chart_bar=None, on_quote=None)
        bars, proof = client._parse_session_history(
            row["symbol"], row["anchor_ms"], current, row["schwab"],
        )
        chart = compute_atr_trail([
            Bar(bar["datetime"], bar["open"], bar["high"], bar["low"],
                bar["close"], bar["volume"])
            for bar in row["massive"] if bar["datetime"] <= current
        ])
        oracle = next((bar for bar in chart if bar["ts"] == current), None)
        rebuilt = False
        if proof is not None:
            with case.clock():
                ledger = case.bot._line_sessions[row["symbol"]]
                assert case.bot._accept_line_source(row["symbol"], ledger.epoch, bars, proof)
                rebuilt = await case.rebuild()
        snapshot = case.snapshot()
        matched = bool(rebuilt and oracle and snapshot["state"] == oracle["state"]
                       and snapshot["trail"] is not None
                       and round(snapshot["trail"], 4) == oracle["trail"])
        failures += not matched
        print(json.dumps({
            "symbol": row["symbol"], "day": row["day"],
            "source_read_at": row["as_of"], "schwab_bars": len(bars),
            "chart_bars": len(chart), "restored_state": snapshot["state"],
            "restored_level": snapshot["trail"],
            "chart_state": oracle["state"] if oracle else None,
            "chart_level": oracle["trail"] if oracle else None,
            "result": "PASS" if matched else "FAIL" if oracle else "UNMEASURED",
            "rebuild_buys": snapshot["buy_count"],
            "waiting_buy_before_mi_flip": "NOT CERTIFIED: chart gate failed" if not matched
                else "UNMEASURED: requires full delivery replay",
            "other_symbol_live_trace_parity": "UNMEASURED: no full historical delivery trace",
        }), flush=True)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
