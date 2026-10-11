"""In-memory L1 guard mutations; no source, database, or service mutations."""

import inspect
from pathlib import Path
import subprocess
import sys
import textwrap

MUTANTS = {
    "working_orders": ("rules", "assess_clear_rules", 'if (prior or str(order.side).lower() == "buy")',
                       "if False"),
    "positions": ("rules", "assess_clear_rules", "if has_position:", "if False:"),
    "settle": ("rules", "assess_clear_rules", "if observed - request.requested_at_ms < SETTLE_MS:",
               "if False:"),
    "fill_rows": ("rules", "assess_clear_rules", "observed = int(now.timestamp() * 1000)",
                  "filled_order_ids = set(); observed = int(now.timestamp() * 1000)"),
    "unknown_cancel": ("rules", "assess_clear_rules",
                       '{"could_not_tell", "not_confirmed"}', "set()"),
    "previous_session": ("rules", "assess_clear_rules", "if prior:\n        return result",
                         "if False:\n        return result"),
    "exact_request_CAS": ("store", "commit_clear",
                          "latest.payload != request.payload(active=True)", "False"),
    "precommit_fence": ("store", "commit_clear", "if not valid():", "if False:"),
    "emit_generation": ("bot", "_removed_wait_poll",
                        "== symbol_generations.get(symbol, 0)",
                        '== getattr(self, "_clearwait_emit_generation_by_symbol", {}).get(symbol, 0)'),
    "symbol_active_count": ("bot", "_removed_wait_poll",
                            "not self._removed_wait_emits_inflight(symbol)", "True"),
    "superseded_global_freeze": ("strategy", "retry_superseded_removed_wait",
                                "current = self._removed_wait_requests.get(request.symbol)",
                                "self._removed_wait_restore_readable = False; "
                                "current = self._removed_wait_requests.get(request.symbol)"),
    "superseded_republication": ("strategy", "retry_superseded_removed_wait",
                                 "self._removed_wait_pending_publications[current.symbol] = current", "pass"),
    "superseded_stale_proof": ("strategy", "retry_superseded_removed_wait",
                              "self._removed_wait_pending_clears.pop(",
                              'self.__dict__.get("_unused_clears", {}).pop('),
    "pending_write_offloop": ("bot", "_removed_wait_poll",
                              "await asyncio.to_thread(store.commit_clear, plan, valid)",
                              "store.commit_clear(plan, valid)"),
    "publication_offloop": ("bot", "_removed_wait_poll",
                            "await asyncio.to_thread(store.record_rules, request, True, publication_valid)",
                            "store.record_rules(request, True, publication_valid)"),
    "publication_precommit_fence": ("store", "record_rules",
                                    "if valid is not None and not valid():", "if False:"),
    "zero_op_session_bound": ("store", "clear_rule_proofs",
                              "& (BrokerOrder.submitted_at >= zero_op_session)", ""),
    "later_bar_freshness": ("strategy", "removed_wait_clear_valid",
                            "0 <= self._now_ms() - plan.proof.observed_at_ms <= FLIP_OWNER_EVIDENCE_MAX_AGE_MS",
                            "True"),
}


def main():
    if len(sys.argv) == 1:
        killed = 0
        for name in MUTANTS:
            result = subprocess.run([sys.executable, __file__, name], capture_output=True, text=True)
            good = (result.returncode == 1 and "FAILED " in result.stdout
                    and ("AssertionError" in result.stdout or "DID NOT RAISE" in result.stdout))
            killed += good
            print(f"{name}: {'ASSERTION_RED' if good else 'SURVIVED_OR_ERROR'}", flush=True)
            if not good:
                print(result.stdout[-6000:] + result.stderr[-2000:], flush=True)
        print(f"L1_mutants_assertion_red={killed}/{len(MUTANTS)}", flush=True)
        return 0 if killed == len(MUTANTS) else 1
    import pytest
    from project_mai_tai import v2_removed_wait as rules
    from project_mai_tai.services import schwab_1m_v2_bot as bot
    from project_mai_tai.strategy_core import schwab_1m_v2 as strategy

    namespace, method, before, after = MUTANTS[sys.argv[1]]
    module = {"rules": rules, "store": rules, "bot": bot, "strategy": strategy}[namespace]
    target = {"rules": rules, "store": rules.RemovedWaitStore, "bot": bot.SchwabV2BotService,
              "strategy": strategy.SchwabV2Strategy}[namespace]
    source = inspect.getsource(getattr(target, method))
    assert before in source, "mutation target moved"
    scope = dict(vars(module))
    exec(compile(textwrap.dedent(source.replace(before, after)), "<L1-in-memory-mutant>", "exec"), scope)
    setattr(target, method, scope[method])
    return pytest.main([str(Path(__file__).with_name("test_v2_removed_wait_clear_rules.py")),
                        "-q", "--tb=short"])


if __name__ == "__main__":
    raise SystemExit(main())
