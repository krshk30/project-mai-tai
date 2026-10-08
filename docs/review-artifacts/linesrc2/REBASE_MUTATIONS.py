"""[codex] Isolated semantic mutations; absolute own-worktree imports, no source edits."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "linesrc1"))
import MUTATIONS as harness  # noqa: E402

BOT = "project_mai_tai.services.schwab_1m_v2_bot"
STRATEGY = "project_mai_tai.strategy_core.schwab_1m_v2"
REPAIR = "project_mai_tai.market_data.line_repair"
LINE = "tests/unit/test_linesrc2_live_fallback.py::"
COMPOSE = "tests/unit/test_linesrc2_gapline_composition.py::"
GAP = "tests/unit/test_gapline1.py::"
CASES = {
    "fallback_disabled": (REPAIR,
        "return bool(not self.published and not self.fallback and self.first_closed_at_ms",
        "return bool(False and not self.published and not self.fallback and self.first_closed_at_ms",
        LINE + "test_repair_budget_deadline_not_started_without_closed_bar"),
    "five_attempt_cap_removed": (REPAIR, "self.attempts < 5", "True",
        LINE + "test_repair_retries_five_at_one_minute_then_afterhours_no_requests"),
    "retry_spacing_removed": (REPAIR, "now_ms + 60_000", "now_ms",
        LINE + "test_retry_spacing_is_from_dispatch_not_from_earlier_queue"),
    "gap_swallowed_by_line_budget": (BOT, '        if reason == "gap_resume":\n',
        "        if False:\n", COMPOSE + "test_gap_event_budget_survives_finished_line_repair_and_does_not_restart_it"),
    "gap_consumes_line_budget": (BOT,
        '            if reason != "gap_resume":\n                self._line_repair_for(symbol, reason).started(self.strategy._now_ms())\n',
        '            self._line_repair_for(symbol, reason).started(self.strategy._now_ms())\n',
        COMPOSE + "test_gap_event_budget_survives_finished_line_repair_and_does_not_restart_it"),
    "carry_disabled": (STRATEGY, "state.line_live_shadow or carry", "state.line_live_shadow",
        GAP + "test_gapline_incomplete_line_advances_carry_without_admission_or_slot_unconsuming"),
    "shadow_carry_double_advance": (STRATEGY,
        '                self._update_atr_state(state, state.bars[-1], state_only=True)\n',
        '                self._update_atr_state(state, state.bars[-1], state_only=True)\n'
        '                self._update_atr_state(state, state.bars[-1], state_only=True)\n',
        COMPOSE + "test_shadow_plus_carry_advances_once_and_does_not_unconsume_or_trade"),
    "fallback_strategy_bypass_removed": (STRATEGY,
        "                and not state.line_live_fallback):\n", "                ):\n",
        LINE + "test_each_parser_rejection_logs_shape_and_publishes_legacy_live_line"),
    "late_repair_live_trade_path": (BOT,
        '                fallback = state.line_live_fallback\n                state.line_live_fallback = False\n',
        '                fallback = state.line_live_fallback\n',
        LINE + "test_late_repair_never_calls_live_trading_path_after_fallback"),
    "ten_bar_wait_waived": (STRATEGY, "        required = 2 * self._atr_period\n", "        required = 1\n",
        GAP + "test_gapline_resume_requires_ten_real_contiguous_bars"),
    "positive_gap_barrier_removed": (BOT, " and not ledger.has_unrecovered_traded_gap()", "",
        "tests/unit/test_linesrc1_review_regressions.py::test_stale_reconciliation_does_not_skip_positive_tape_or_mark_db_complete"),
}
harness.CASES = CASES


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--child", nargs=2)
    parser.add_argument("--receipt", type=Path)
    parser.add_argument("--output", type=Path, default=Path("/tmp/linesrc2-rebase-mutations.json"))
    args = parser.parse_args()
    if args.child:
        return harness.child(*args.child, args.receipt)
    folder = args.output.with_suffix("")
    folder.mkdir(exist_ok=True)
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "PYTHONPATH": f"{ROOT / 'src'}:{ROOT}"}
    rows = []
    for name in CASES:
        runs = {}
        for mode in ("baseline", "mutant"):
            receipt, log = folder / f"{name}-{mode}.json", folder / f"{name}-{mode}.log"
            result = subprocess.run([sys.executable, __file__, "--child", name, mode,
                                     "--receipt", str(receipt)], cwd=ROOT, env=env, capture_output=True)
            log.write_bytes(result.stdout + result.stderr)
            data = json.loads(receipt.read_text()) if receipt.exists() else {"harness_error": "no receipt"}
            data.update(receipt=str(receipt), log=str(log), process_returncode=result.returncode)
            runs[mode] = data
        baseline, mutant = runs["baseline"], runs["mutant"]
        killed = bool(baseline.get("baseline_pass") and mutant.get("exit_code") == 1
                      and mutant.get("assertion_failures") and not mutant.get("non_assertion_errors")
                      and not baseline.get("harness_error") and not mutant.get("harness_error"))
        rows.append({"mutation": name, "semantic_assertion_kill": killed, "runs": runs})
        print(f"{name}: {'SEMANTIC KILL' if killed else 'NOT A VALID KILL'}", flush=True)
    args.output.write_text(json.dumps({"worktree": str(ROOT), "mutations": rows}, indent=2) + "\n")
    return 0 if all(row["semantic_assertion_kill"] for row in rows) else 1


if __name__ == "__main__":
    raise SystemExit(main())
