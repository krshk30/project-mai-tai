"""Run isolated in-memory RED mutations; never edits application source files.

From repo root: PYTHONPATH=src python docs/review-artifacts/coldstart1/run_mutations.py
"""
from __future__ import annotations

import inspect
import json
from pathlib import Path
import subprocess
import sys
import textwrap

from project_mai_tai.services.momentum_paper_app import MomentumPaperService
from project_mai_tai.services.orb_app import OrbService
from project_mai_tai.services.orb_schwab_app import OrbSchwabService
from project_mai_tai.services.schwab_1m_v2_bot import SchwabV2BotService
from project_mai_tai.services.strategy_engine_app import StrategyEngineService


CASES = {
    "strategy_suppresses_empty": (
        StrategyEngineService, "_sync_market_data_subscriptions",
        "self.settings.market_data_subscription_startup_enabled", "False",
        "test_cold_boot_five_real_publishers_announce_recorded_empty_sets_within_60_seconds",
    ),
    "orb_suppresses_empty": (
        OrbService, "_sync_gateway_subscription",
        "self.settings.market_data_subscription_startup_enabled", "False",
        "test_cold_boot_five_real_publishers_announce_recorded_empty_sets_within_60_seconds",
    ),
    "orb_schwab_suppresses_empty": (
        OrbSchwabService, "_sync_gateway_subscription",
        "self.settings.market_data_subscription_startup_enabled", "False",
        "test_cold_boot_five_real_publishers_announce_recorded_empty_sets_within_60_seconds",
    ),
    "paper_suppresses_empty": (
        MomentumPaperService, "_sync_gateway_subscriptions",
        "self.settings.market_data_subscription_startup_enabled", "False",
        "test_weekend_paper_tick_announces_empty_once_without_stream_or_reference_reads",
    ),
    "v2_lost_ack_cache_not_invalidated": (
        SchwabV2BotService, "_sync_gateway_subscription",
        "self._last_gateway_symbols = None", "pass",
        "test_lost_ack_never_debounces_return_to_previous_acknowledged_set[False-schwab-1m-v2]",
    ),
    "observe_claims_symbols": (
        OrbSchwabService, "_sync_gateway_subscription",
        "symbols=[] if self._observe_only else desired", "symbols=desired",
        "test_observe_only_keeps_local_filter_but_only_announces_empty_owner[True]",
    ),
    "orb_schwab_forgets_held_symbols": (
        OrbSchwabService, "run",
        'self._exit_held_symbols = {entry["symbol"] for entry in entries}',
        "self._exit_held_symbols = set()",
        "test_orb_schwab_startup_restores_held_only_coverage_before_first_replace",
    ),
    "v2_unknown_becomes_empty": (
        SchwabV2BotService, "_startup_gateway_exit_coverage",
        "strict=True", "strict=False",
        "test_v2_startup_unknown_coverage_refuses_instead_of_empty[managed]",
    ),
    "orb_failed_startup_clears_coverage": (
        OrbSchwabService, "run",
        "if not self.settings.market_data_subscription_startup_enabled or self._gateway_subscription_announced:",
        "if True:",
        "test_orb_startup_failed_or_ambiguous_publish_never_cleans_up_to_empty",
    ),
    "v2_concurrent_debounce_not_serialized": (
        SchwabV2BotService, "_sync_gateway_subscription",
        "async with lock:", "async with __import__('contextlib').nullcontext():",
        "test_v2_inflight_replace_serializes_new_held_coverage_against_old_ack_cache",
    ),
    "v2_startup_hydration_skipped": (
        SchwabV2BotService, "run",
        "self._exit_coverage = await asyncio.to_thread(self._startup_gateway_exit_coverage)",
        "self._exit_coverage = set()",
        "test_v2_run_finishes_ownership_hydration_before_starting_scanner_and_poll",
    ),
    "strategy_lost_ack_debounced": (
        StrategyEngineService, "_sync_market_data_subscriptions",
        "self._market_data_subscription_uncertain = True",
        "self._market_data_subscription_uncertain = False",
        "test_lost_ack_never_debounces_return_to_previous_acknowledged_set[False-strategy-engine]",
    ),
    "orb_lost_ack_debounced": (
        OrbService, "_sync_gateway_subscription",
        "self._gateway_subscription_uncertain = True", "self._gateway_subscription_uncertain = False",
        "test_lost_ack_never_debounces_return_to_previous_acknowledged_set[False-orb]",
    ),
    "orb_schwab_lost_ack_debounced": (
        OrbSchwabService, "_sync_gateway_subscription",
        "self._gateway_subscription_uncertain = True", "self._gateway_subscription_uncertain = False",
        "test_lost_ack_never_debounces_return_to_previous_acknowledged_set[False-orb-schwab]",
    ),
    "paper_lost_ack_debounced": (
        MomentumPaperService, "_sync_gateway_subscriptions",
        "self._gateway_subscription_uncertain = True", "self._gateway_subscription_uncertain = False",
        "test_lost_ack_never_debounces_return_to_previous_acknowledged_set[False-momentum-paper]",
    ),
}


def main() -> int:
    if len(sys.argv) == 1:
        results = []
        for name in CASES:
            run = subprocess.run([sys.executable, str(Path(__file__).resolve()), name],
                                 capture_output=True, text=True, check=False)
            results.append({"mutation": name, "killed": run.returncode == 0})
            if run.returncode:
                print(run.stdout + run.stderr)
        print(json.dumps(results, indent=2))
        return 0 if all(row["killed"] for row in results) else 1
    owner, method, before, after, test = CASES[sys.argv[1]]
    original = getattr(owner, method)
    source = textwrap.dedent(inspect.getsource(original))
    assert source.count(before) == 1, "mutation no longer matches exactly once"
    namespace = {}
    exec(compile(source.replace(before, after), f"<mutation:{sys.argv[1]}>", "exec"),
         original.__globals__, namespace)
    setattr(owner, method, namespace[method])
    import pytest
    result = pytest.main(["-q", f"tests/unit/test_coldstart1_subscriptions.py::{test}", "--tb=short"])
    return 0 if result == pytest.ExitCode.TESTS_FAILED else 2


if __name__ == "__main__":
    raise SystemExit(main())
