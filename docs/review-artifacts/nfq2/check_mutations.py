"""Run one in-memory guard mutation without changing the checkout."""
from __future__ import annotations

import inspect
import sys
import textwrap

import pytest

from project_mai_tai.oms.eh_fresh_price import EhFreshPriceMixin


MUTATIONS = {
    "N1": ("_nfq2_reading", "symbol, self._nfq_now(), 10000", "symbol, self._nfq_now(), 2000",
           "test_biya_ten_second_price_is_fresh_and_never_gets_held_band"),
    "N2": ("_claim_nfq2_retry", "or hold.token != token", "or False",
           "test_biya_stale_serial_token_cannot_claim_a_requeued_hold"),
    "N3": ("_nfq2_pre_submit", "if not self._nfq2_applies(event):", "if True:",
           "test_biya_real_intent_and_serial_consumer_submit_once_after_hold"),
    "N4": ("_nfq2_held_dispatch", "return bool(hold is not None", "return True or bool(hold is not None",
           "test_biya_fresh_order_outside_half_percent_is_refused_not_given_held_cap"),
    "N5": ("_nfq2_observe", "if not self._nfq2_holds or", "if True or",
           "test_biya_cancel_after_enqueue_invalidates_the_serial_copy"),
    "N6": ("_evaluate_nfq2_holds", 'trigger * Decimal("1.01")', 'trigger * Decimal("1.005")',
           "test_biya_both_paths_and_accounts_hold_then_serially_recheck_one_percent"),
    "N7": ("_claim_nfq2_retry", 'or hold.phase != "queued"', "or False",
           "test_biya_both_paths_and_accounts_hold_then_serially_recheck_one_percent"),
    "N8": ("_nfq2_pre_submit", "if uncertain is not None:", "if False:",
           "test_biya_uncertain_dispatch_is_not_released_by_window_or_new_open"),
    "N9": ("_nfq2_reason", "for order in orders:", "for order in []:",
           "test_biya_restart_with_own_filled_slot_retires_hold_without_rebuy"),
    "N10": ("_claim_nfq2_retry", 'self._nfq2_feedback(session, event, "queued", "retry_claimed")', "pass",
            "test_biya_serial_rehold_moves_v2_attempt_then_giveup_releases_exact_webull_latch"),
    "N11": ("_nfq2_reason", 'return "eh_path_closed"', "pass",
            "test_biya_eh_hold_does_not_migrate_into_unpriced_regular_hours"),
    "N12": ("_evaluate_nfq2_holds", "if not matches:", "if False:",
            "test_nfq2_unrelated_or_stale_tick_never_opens_session_or_copies_hold_map"),
    "N13": ("_evaluate_nfq2_holds", "if not reading.fresh:", "if False:",
            "test_nfq2_unrelated_or_stale_tick_never_opens_session_or_copies_hold_map"),
    "N14": ("_evaluate_nfq2_holds", "if key in busy or", "if False or",
            "test_nfq2_slow_save_does_not_stall_loop_and_concurrent_quotes_queue_once"),
    "N15": ("_nfq2_transition", "if result.rowcount != 1:", "if False:",
            "test_nfq2_worker_cas_refuses_retired_generation_without_feedback"),
    "N16": ("_sweep_nfq2_holds", 'if now - self.__dict__.get("_eh_price_hold_last_sweep", float("-inf")) < cadence:',
            "if False:", "test_nfq2_periodic_proof_runs_off_loop_and_is_not_tick_rate_driven"),
    "N17": ("_evaluate_nfq2_holds",
            "changed = await self._run_db(lambda session: self._nfq2_transition(\n                session, before, after, reason=reason))",
            "with self.session_factory() as session:\n                changed = self._nfq2_transition(session, before, after, reason=reason)\n                session.commit()",
            "test_nfq2_quote_transition_persists_on_worker_without_tick_ownership_queries"),
}


def main():
    name = sys.argv[1]
    method, old, new, test = MUTATIONS[name]
    original = getattr(EhFreshPriceMixin, method)
    source = textwrap.dedent(inspect.getsource(original))
    assert source.count(old) == 1, (name, source.count(old))
    namespace = dict(original.__globals__)
    exec(compile("from __future__ import annotations\n" + source.replace(old, new),
                 f"<nfq2-mutation-{name}>", "exec"), namespace)
    setattr(EhFreshPriceMixin, method, namespace[method])
    module = "test_nfq2_tick_path" if name in {"N12", "N13", "N14", "N15", "N16", "N17"} else "test_nfq2_eh_fresh_price"
    result = pytest.main(["-p", "no:cacheprovider", "-q", f"tests/unit/{module}.py::" + test])
    print(f"MUTATION {name} pytest_rc={result} {'RED' if result == 1 else 'NOT_RED'}")
    return 0 if result == 1 else 1


if __name__ == "__main__":
    raise SystemExit(main())
