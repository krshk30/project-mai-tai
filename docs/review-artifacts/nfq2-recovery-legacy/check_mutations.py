"""One safety mutation per child process; never rewrite the checkout."""
import inspect
import sys
import textwrap

import pytest

from project_mai_tai.oms.eh_fresh_price import EhFreshPriceMixin


MUTATIONS = {
    "R1": ("_nfq2_recovery_order", 'or account.name != payload.broker_account_name', 'or False',
           'test_exact_durable_identity_mismatch_blocks_without_read[account]'),
    "R2": ("_nfq2_recovery_order", 'or dispatch and any(dispatch.get(key) != value for key, value in identity.items())',
           'or False', 'test_exact_durable_identity_mismatch_blocks_without_read[dispatch_generation]'),
    "R3": ("_nfq2_commit_recovery", 'if current != identity or self._nfq2_reason(session, before) != "dispatch_uncertain":',
           'if False:', 'test_post_read_durable_races_never_release[quantity]'),
    "R4": ("_nfq2_transition", 'DashboardSnapshot.payload["dispatch"]["generation"].as_string() ==\n'
           '            (before.dispatch or {}).get("generation"),', '',
           'test_post_read_durable_races_never_release[generation]'),
    "R5": ("_nfq2_recovery_order", 'if dispatch.get("fill_seen"):', 'if False:',
           'test_readback_positive_fill_is_sticky_across_zero_and_restart'),
    "R6": ("_nfq2_commit_recovery", 'if not identity["local_no_wire"] and not empty:', 'if False:',
           'test_cancel_ack_without_explicit_zero_does_not_finish_or_recover'),
    "R7": ("_recover_nfq2_uncertain", 'if key in busy or', 'if False or',
           'test_recovery_busy_fence_sends_one_read_and_never_queues'),
    "R8": ("_nfq2_recovery_order", 'or omd.get("nfq2_retry_token") != generation', 'or False',
           'test_exact_durable_identity_mismatch_blocks_without_read[order_generation]'),
    "R9": ("_nfq2_finish_transition", 'if not self._nfq2_transition(session, hold, after, reason=reason, outcome=outcome):',
           'if not (self._nfq2_save(session, after) or True):',
           'test_finish_durable_generation_race_never_overwrites_new_owner'),
    "R10": ("_claim_nfq2_retry", 'if not self._nfq2_transition(session, before, after):',
            'if not (self._nfq2_save(session, after) or True):',
            'test_claim_persists_exact_generation_before_any_wire_and_refuses_durable_race'),
    "R11": ("_recover_nfq2_uncertain", 'if not self._nfq2_same(self._nfq2_holds.get(key), before):',
            'if False:', 'test_runtime_generation_change_during_read_never_retires_durable_owner'),
    "L1": ("_nfq2_pre_submit", 'if not self._nfq2_bound_identity(event) or not self._nfq2_segment_current(session, event):',
           'if False:', 'test_legacy_identity_refused_even_with_fresh_quote_no_guessed_migration'),
    "L2": ("_nfq2_pre_submit", 'if not self._nfq2_bound_identity(event) or not self._nfq2_segment_current(session, event):',
           'if not self._nfq2_bound_identity(event):',
           'test_current_segment_is_required_even_when_canonical_identity_and_quote_are_fresh'),
    "L3": ("_nfq2_refuse_pre_wire", 'and owned is None', 'and True',
           'test_unsafe_legacy_does_not_reset_owned_intent_or_existing_bound_hold[pending]'),
    "L4": ("_nfq2_pre_submit", 'if uncertain is not None:', 'if False:',
           'test_missing_order_never_expires_or_resends_and_off_still_blocks'),
    "L5": ("_nfq2_observe", 'if not self._nfq2_bound_identity(event):', 'if False:',
           'test_unsafe_legacy_does_not_reset_owned_intent_or_existing_bound_hold[held]'),
    "L6": ("_nfq2_intent_matches", 'and intent.quantity == event.payload.quantity', 'and True',
           'test_only_exact_local_legacy_prewire_refusal_finishes[quantity]'),
}


def main():
    name = sys.argv[1]
    method, old, new, test = MUTATIONS[name]
    original = getattr(EhFreshPriceMixin, method)
    source = textwrap.dedent(inspect.getsource(original))
    assert source.count(old) == 1, (name, source.count(old))
    namespace = dict(original.__globals__)
    exec(compile("from __future__ import annotations\n" + source.replace(old, new),
                 f"<nfq2-residual-mutation-{name}>", "exec"), namespace)
    method_value = namespace[method]
    setattr(EhFreshPriceMixin, method, staticmethod(method_value) if method == "_nfq2_intent_matches" else method_value)
    rc = pytest.main(["-p", "no:cacheprovider", "-q", "--tb=short",
                      "tests/unit/test_nfq2_recovery_legacy.py::" + test])
    print(f"RESIDUAL_MUTATION {name} pytest_rc={rc} {'RED' if rc == 1 else 'NOT_RED'}")
    return 0 if rc == 1 else 1


if __name__ == "__main__":
    raise SystemExit(main())
