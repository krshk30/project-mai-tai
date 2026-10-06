"""Run isolated in-memory safety mutations; never change the tracked source."""
from __future__ import annotations

import inspect
import subprocess
import sys
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

MUTATIONS = {
    "M1-send-cache-fail-open": (
        "_emit_v2_exit_on_loop", "if schwab and native_release_row_id != snapshot.managed_row_id:",
        "if False:",
    ),
    "M2-release-unknown-admitted": (
        "_emit_v2_exit_on_loop", 'if result != "released":', "if False:",
    ),
    "M3-child-fill-ignored": (
        "_emit_v2_exit_on_loop", 'if result == "resolved_by_fill":', "if False:",
    ),
    "M4-parallel-claim-removed": (
        "_emit_v2_exit_on_loop", "claim in inflight or snapshot.dedup_active", "snapshot.dedup_active",
    ),
    "M5-stale-quantity-admitted": (
        "_emit_v2_exit_on_loop", "or current.current_quantity != snapshot.current_quantity\n"
        "                    or current.current_quantity != int(position.quantity)", "",
    ),
    "M6-original-reject-proof-removed": (
        "_emit_v2_managed_sell", "if await self._reserve1_original_close_rejected(request, reports):",
        "if True:",
    ),
    "M7-recovery-child-fill-ignored": (
        "_emit_v2_managed_sell", 'if release == "resolved_by_fill":', "if False:",
    ),
    "M8-episode-retry-cap-removed": (
        "_emit_v2_managed_sell", "and retry_key not in retry_used", "",
    ),
    "M9-client-reject-counted-as-broker": (
        "_reserve1_original_close_rejected", 'and report.origin == "broker"', "",
    ),
    "M10-original-partial-fill-admitted": (
        "_reserve1_original_close_rejected", "and report.intent_type == \"close\" and report.filled_quantity == 0",
        'and report.intent_type == "close"',
    ),
    "M11-note-state-not-measured": (
        "_log_native_oco_note", 'state = "absent" if age is None else "fresh" if 0 <= age <= bound else "stale"',
        'state = "fresh"',
    ),
    "M12-sync-end-not-logged": (
        "sync_broker_state", '"[OMS-BROKER-SYNC-PASS] id=%d phase=end outcome=%s duration_ms=%.3f "',
        '"[OMS-BROKER-SYNC-PASS] id=%d phase=missing outcome=%s duration_ms=%.3f "',
    ),
    "M13-post-cancel-read-removed": (
        "adapter:release_native_oco_for_close", "confirmed = await self._fetch_order(account, entry_order_id)",
        "confirmed = root",
    ),
    "M14-evaluation-note-not-logged": (
        "_evaluate_v2_managed_exit", "self._log_native_oco_note(acct, symbol)", "pass",
    ),
    "M15-armed-release-bypassed": (
        "_reserve1_trigger_schwab_hard_stop",
        "if not await release_current():\n            return\n        events =",
        "if False:\n            return\n        events =",
    ),
    "M16-armed-unknown-admitted": (
        "_reserve1_trigger_schwab_hard_stop", 'if result != "released":', "if False:",
    ),
    "M17-armed-child-fill-not-attributed": (
        "_reserve1_trigger_schwab_hard_stop", 'if result == "resolved_by_fill":', "if False:",
    ),
    "M18-armed-shared-claim-removed": (
        "_reserve1_trigger_schwab_hard_stop", "claim in inflight or stop.close_in_flight",
        "stop.close_in_flight",
    ),
    "M19-armed-episode-check-removed": (
        "_reserve1_trigger_schwab_hard_stop", "if current != episode:", "if False:",
    ),
    "M20-armed-original-rejection-proof-removed": (
        "_reserve1_trigger_schwab_hard_stop",
        "if not await self._reserve1_original_close_rejected(request, reports):", "if False:",
    ),
    "M21-orb-old-trip-admitted": (
        "_reserve1_hard_stop_episode", "Fill.filled_at >= position.opened_at,", "",
    ),
    "M22-known-native-guard-dedup-removed": (
        "_trigger_hard_stop", "if has_native_guard:", "if False:",
    ),
    "M23-armed-note-log-removed": (
        "_trigger_hard_stop", "self._log_native_oco_note(stop.broker_account_name, stop.symbol)", "pass",
    ),
    "M24-armed-recovery-cap-removed": (
        "_reserve1_trigger_schwab_hard_stop", "request is None or claim in retry_used", "request is None",
    ),
    "M25-orb-external-send-admitted": (
        "process_trade_intent", 'if strategy_code == "orb_schwab" and not reserve1_dispatch:',
        'if strategy_code == "orb_schwab" and False:',
    ),
}


def run_case(name: str) -> int:
    from project_mai_tai.oms.service import OmsRiskService
    from project_mai_tai.broker_adapters.schwab import SchwabBrokerAdapter
    import pytest

    method, old, new = MUTATIONS[name]
    target = OmsRiskService
    if method.startswith("adapter:"):
        target = SchwabBrokerAdapter
        method = method.split(":", 1)[1]
    original = getattr(target, method)
    source = textwrap.dedent(inspect.getsource(original))
    assert source.count(old) == 1, (name, source.count(old))
    namespace = {}
    exec(compile(source.replace(old, new), original.__code__.co_filename, "exec"),
         original.__globals__, namespace)
    setattr(target, method, namespace[method])
    return pytest.main(["tests/unit/test_reserve1.py", "-q", "--tb=short", "-p", "no:cacheprovider"])


if __name__ == "__main__":
    if len(sys.argv) == 2:
        raise SystemExit(run_case(sys.argv[1]))
    survivors = []
    for name in MUTATIONS:
        result = subprocess.run([sys.executable, __file__, name], cwd=ROOT, text=True,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=60)
        # Only an assertion/test failure counts as RED, never a harness/import error.
        red = result.returncode == 1 and " failed" in result.stdout
        print(f"{name}: {'RED' if red else 'NOT_RED'}", flush=True)
        print(result.stdout, flush=True)
        if not red:
            survivors.append(name)
    raise SystemExit(bool(survivors))
