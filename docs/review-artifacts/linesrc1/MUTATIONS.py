#!/usr/bin/env python3
"""[codex] Isolated module/class compilation; semantic kills exclude harness errors."""
from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[3]
CLIENT = "project_mai_tai.market_data.schwab_v2_rest_client"
BOT = "project_mai_tai.services.schwab_1m_v2_bot"
LEDGER = "project_mai_tai.strategy_core.session_line_restore"
TEST = "tests/unit/test_linesrc1_anchored_session_poll.py::"
CASES = {
    "utc_not_et": (CLIENT, '.astimezone(ZoneInfo("America/New_York"))', '.astimezone(UTC)',
                   TEST + "test_exact_et_session_boundaries_and_dst"),
    "open_early": (CLIENT, "return 7 * 60 <=", "return 6 * 60 + 55 <=",
                   TEST + "test_exact_et_session_boundaries_and_dst"),
    "close_inclusive": (CLIENT, "eastern.minute < 16 * 60", "eastern.minute <= 16 * 60",
                        TEST + "test_exact_et_session_boundaries_and_dst"),
    "service_window": (BOT, "if not anchored_session_poll_open(now_ms):", "if False:",
                       TEST + "test_outside_session_service_is_quiet_without_epoch_change"),
    "client_window": (CLIENT, "if not anchored_session_poll_open(current + 60_000):", "if False:",
                      TEST + "test_client_also_fences_an_outside_session_context"),
    "provider_window": (CLIENT, "if not anchored_session_poll_open(current_bar_ms + 60_000):", "if False:",
                        TEST + "test_outside_session_provider_does_not_read_or_validate"),
    "missing_current_proof": (CLIENT,
        "        if bars[-1].timestamp_ms != current_bar_ms:\n            return bars, None\n", "",
        TEST + "test_recorded_missing_current_returns_bars_without_coverage_proof"),
    "missing_wait": (BOT, "            self._line_source_waiting[symbol] = epoch\n", "",
                     TEST + "test_missing_current_retains_epoch_but_blocks_entry_even_after_old_rebuild"),
    "buy_fence": (BOT,
        "        if ledger is not None and self._line_source_waiting.get(symbol) == ledger.epoch:\n            return False\n",
        "", TEST + "test_missing_current_retains_epoch_but_blocks_entry_even_after_old_rebuild"),
    "no_new_bar_dispatch": (CLIENT,
        "                if proof is None:\n"
        "                    # No current close is not source corruption or a failed epoch.\n"
        "                    # The service retains its old proof but fences new entries.\n"
        "                    self._on_session_history(symbol, epoch, bars, None)\n"
        "                    self._set_line_source_state(symbol, (\"waiting\", \"current_closed_candle_pending\"))\n"
        "                    return\n", "", TEST + "test_warning_only_on_symbol_state_transition_and_no_traceback"),
    "error_invalidation": (CLIENT,
        "                if context is not None:\n                    self._on_session_failure(symbol, context[0])\n",
        "", TEST + "test_duplicate_error_invalidates_previously_admitted_coverage"),
    "warning_dedup": (CLIENT, "        if previous is not None and previous[0] == state[0]:\n            return\n", "",
                      TEST + "test_warning_only_on_symbol_state_transition_and_no_traceback"),
    "dynamic_error_flood": (CLIENT,
        "        if previous is not None and previous[0] == state[0]:\n",
        "        if previous == state:\n",
        TEST + "test_changing_error_text_does_not_repeat_warning_or_skip_invalidation"),
    "traceback_reenabled": (CLIENT,
        'logger.warning("[V2-LINE-SOURCE-STATE] sym=%s state=%s reason=%s", symbol, *state)',
        'logger.warning("[V2-LINE-SOURCE-STATE] sym=%s state=%s reason=%s", symbol, *state, exc_info=True)',
        TEST + "test_warning_only_on_symbol_state_transition_and_no_traceback"),
    "duplicate_validation": (CLIENT, " or bar.timestamp_ms in ids", "",
                             TEST + "test_in_session_invalid_source_is_not_disguised_as_no_new_bar[duplicate]"),
    "foreign_validation": (CLIENT,
        "if not anchor_ms <= bar.timestamp_ms <= current_bar_ms or bar.timestamp_ms in ids:",
        "if bar.timestamp_ms in ids:",
        TEST + "test_in_session_invalid_source_is_not_disguised_as_no_new_bar[foreign]"),
    "epoch_fence": (BOT, "if ledger is None or ledger.epoch != epoch:", "if ledger is None:",
                    TEST + "test_old_epoch_response_cannot_set_or_clear_current_source_wait"),
    "recovery_stays_blocked": (BOT,
        "        self._line_source_waiting.pop(symbol, None)\n        return True\n",
        "        return True\n", TEST + "test_missing_current_retains_epoch_but_blocks_entry_even_after_old_rebuild"),
    "traded_hole_waived": (LEDGER,
        '            self.incomplete_reason = "traded_gap_unrecovered"\n            return None',
        '            self.incomplete_reason = "traded_gap_unrecovered"\n            pass',
        "tests/unit/test_line_restore_r6_spanning.py::test_recorded_jagx_withheld_traded_candle_cannot_be_spanned_as_unfillable"),
    "r6_clean_live_waived": (BOT,
        "            clean = all(current - offset * 60_000 in live\n"
        "                        for offset in range(2 * self.strategy._atr_period))",
        "            clean = True",
        "tests/unit/test_line_restore_r6_spanning.py::test_recorded_pmi_eleven_arrivals_do_not_release_ten_clean_live_bar_hold"),
    "service_first_close": (BOT,
        "        if not anchored_session_poll_open(current):\n            return None\n", "",
        TEST + "test_oct7_measured_empty_names_do_not_poll_before_first_0700_close"),
    "client_first_close": (CLIENT,
        "                    if not anchored_session_poll_open(current):\n                        return\n", "",
        TEST + "test_client_fences_context_before_first_0700_closed_candle"),
    "provider_first_close": (CLIENT,
        "        if not anchored_session_poll_open(current_bar_ms):\n            return [], None\n", "",
        TEST + "test_outside_session_provider_does_not_read_or_validate"),
    "empty_becomes_exception": (CLIENT,
        "        if not candles:\n            # A valid empty response has no bars or completeness proof yet.\n            return [], None\n",
        "        if not candles:\n            raise ValueError(\"session response completeness unproven\")\n",
        TEST + "test_oct7_measured_empty_shape_has_no_bars_or_proof_in_controlled_session"),
    "empty_manufactures_proof": (CLIENT,
        "        if not candles:\n            # A valid empty response has no bars or completeness proof yet.\n            return [], None\n",
        "        if not candles:\n            return [], object()\n",
        TEST + "test_oct7_measured_empty_shape_has_no_bars_or_proof_in_controlled_session"),
    "empty_bypasses_identity": (CLIENT,
        '        candles = payload.get("candles")\n        if (str(payload.get("symbol", "")).upper() != symbol.upper()\n',
        '        candles = payload.get("candles")\n        if payload.get("empty") is True and candles == []:\n            return [], None\n        if (str(payload.get("symbol", "")).upper() != symbol.upper()\n',
        TEST + "test_empty_is_not_a_bypass_for_foreign_or_malformed_source[foreign]"),
    "empty_contradiction_waived": (CLIENT,
        '        if payload.get("empty") is True:\n            raise ValueError("nonempty session response marked empty")\n', "",
        TEST + "test_empty_is_not_a_bypass_for_foreign_or_malformed_source[contradiction]"),
}


class Recorder:
    def __init__(self, name, mutant):
        self.name, self.mutant = name, mutant
        self.rows = []
        self.harness_error = None
        self.source_sha256 = None

    def pytest_configure(self):
        module_name, before, after, _ = CASES[self.name]
        module = importlib.import_module(module_name)
        source = Path(module.__file__).read_text()
        self.source_sha256 = hashlib.sha256(source.encode()).hexdigest()
        if not self.mutant:
            return
        try:
            if source.count(before) != 1:
                raise ValueError(f"mutation selector count={source.count(before)}, expected=1")
            # Compile the whole module/class, preserving __class__ cells for super().
            exec(compile(source.replace(before, after), "<isolated-linesrc1-mutation>", "exec"),
                 module.__dict__)
        except Exception as exc:
            self.harness_error = f"{type(exc).__name__}: {exc}"
            raise

    @pytest.hookimpl(hookwrapper=True)
    def pytest_runtest_makereport(self, item, call):
        outcome = yield
        report = outcome.get_result()
        exception = call.excinfo.value if call.excinfo is not None else None
        semantic = call.when == "call" and isinstance(exception, (AssertionError, pytest.fail.Exception))
        self.rows.append({"nodeid": item.nodeid, "phase": call.when, "outcome": report.outcome,
                          "exception_type": type(exception).__name__ if exception is not None else None,
                          "assertion_failure": semantic, "failure": str(report.longrepr) if report.failed else None})


def child(name, mode, path):
    recorder = Recorder(name, mode == "mutant")
    code = pytest.main(["-q", "-p", "no:cacheprovider", "--tb=short", CASES[name][3]], plugins=[recorder])
    errors = [r for r in recorder.rows if r["outcome"] == "failed" and not r["assertion_failure"]]
    assertions = [r for r in recorder.rows if r["outcome"] == "failed" and r["assertion_failure"]]
    data = {"exit_code": int(code), "source_sha256": recorder.source_sha256,
            "harness_error": recorder.harness_error, "reports": recorder.rows,
            "non_assertion_errors": errors, "assertion_failures": assertions,
            "baseline_pass": code == 0 and bool(recorder.rows) and not errors and not assertions}
    path.write_text(json.dumps(data, indent=2) + "\n")
    return int(code)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--child", nargs=2, metavar=("NAME", "MODE"))
    parser.add_argument("--receipt", type=Path)
    parser.add_argument("--output", type=Path, default=Path("/tmp/linesrc1-mutations.json"))
    parser.add_argument("--only", default="")
    args = parser.parse_args()
    if args.child:
        return child(*args.child, args.receipt)
    output = args.output.with_suffix("")
    output.mkdir(exist_ok=True)
    env = {**os.environ, "PYTHONPATH": "src:.", "PYTHONDONTWRITEBYTECODE": "1"}
    results = []
    for name in (args.only.split(",") if args.only else CASES):
        runs = {}
        for mode in ("baseline", "mutant"):
            receipt = output / f"{name}-{mode}.json"
            log = output / f"{name}-{mode}.log"
            proc = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--child", name, mode,
                                   "--receipt", str(receipt)], cwd=ROOT, env=env,
                                  stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            log.write_bytes(proc.stdout)
            data = json.loads(receipt.read_text()) if receipt.exists() else {"harness_error": "no receipt"}
            data.update(receipt=str(receipt), log=str(log), process_returncode=proc.returncode)
            runs[mode] = data
        baseline, mutant = runs["baseline"], runs["mutant"]
        killed = bool(baseline.get("baseline_pass") and mutant.get("exit_code") == 1
                      and mutant.get("assertion_failures") and not mutant.get("non_assertion_errors")
                      and not baseline.get("harness_error") and not mutant.get("harness_error"))
        results.append({"mutation": name, "semantic_assertion_kill": killed, "runs": runs})
        print(f"{name}: {'SEMANTIC KILL' if killed else 'NOT A VALID KILL'}", flush=True)
    data = {"scope": "Recorded prices with controlled source states; no checkout edits or production I/O",
            "mutations": results}
    args.output.write_text(json.dumps(data, indent=2) + "\n")
    return 0 if all(row["semantic_assertion_kill"] for row in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
