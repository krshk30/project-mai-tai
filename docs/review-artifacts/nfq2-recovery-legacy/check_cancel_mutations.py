"""Typed barrier controls mutate only one test child, never checkout source."""
import inspect
import sys
import textwrap

import pytest

from project_mai_tai.oms.eh_fresh_price import EhFreshPriceMixin


MUTATIONS = {
    "Q1": ("_nfq2_observe", "if self._nfq2_segment_cancel_barrier(event):", "if False:",
           "test_typed_opportunity_barrier_retires_local_reclaim_and_invalidates_queue"),
    "Q2": ("_nfq2_segment_cancel_barrier",
           'and md.get("clearwait_opportunity_id") == md.get("fanout_segment_id")', 'and True',
           'test_invalid_or_other_scope_barrier_does_not_retire_reclaim[opportunity]'),
    "Q3": ("_nfq2_retire_local_segment_hold", 'hold.phase not in {"held", "queued"}',
           'hold.phase not in {"held", "queued", "dispatching"}',
           'test_typed_barrier_never_retires_dispatch_or_uncertainty[dispatching]'),
    "Q4": ("_nfq2_retire_local_segment_hold", 'if related_order is not None:', 'if False:',
           'test_any_related_order_is_not_proven_local_even_with_terminal_label'),
    "Q5": ("_nfq2_retire_local_segment_hold", 'or intent.status != "held"', 'or False',
           'test_pre_wire_proof_is_required_for_segment_retirement[intent_status]'),
    "Q6": ("_nfq2_retire_local_segment_hold", 'or before.dispatch is not None', 'or False',
           'test_pre_wire_proof_is_required_for_segment_retirement[durable_dispatch]'),
    "Q7": ("_nfq2_retire_local_segment_hold",
           'if not self._nfq2_transition(session, before, after, reason="v2_cancel_segment_barrier"):',
           'if not (self._nfq2_save(session, after) or True):',
           'test_barrier_cas_mismatch_never_erases_won_dispatch'),
    "Q8": ("_nfq2_retire_local_segment_hold", 'before = EhHold(TradeIntentEvent.model_validate(p["event"]), row.id, UUID(p["intent_id"]),\n'
           '                        p["phase"], p["token"], deepcopy(p.get("dispatch")))',
           'before = self._nfq2_copy(hold)',
           'test_barrier_rereads_durable_queued_shadow_and_prevents_restart_resend'),
}


def main():
    name = sys.argv[1]
    method, old, new, test = MUTATIONS[name]
    original = getattr(EhFreshPriceMixin, method)
    source = textwrap.dedent(inspect.getsource(original))
    assert source.count(old) == 1, (name, source.count(old))
    source = source.replace(old, new)
    if name == "Q3":
        durable = 'before.phase not in {"held", "queued"}'
        assert source.count(durable) == 1
        source = source.replace(durable, 'before.phase not in {"held", "queued", "dispatching"}')
    namespace = dict(original.__globals__)
    exec(compile("from __future__ import annotations\n" + source,
                 f"<nfq2-cancel-mutation-{name}>", "exec"), namespace)
    setattr(EhFreshPriceMixin, method, namespace[method])
    rc = pytest.main(["-p", "no:cacheprovider", "-q", "--tb=short", "tests/unit/test_nfq2_segment_cancel.py::" + test])
    print(f"CANCEL_MUTATION {name} pytest_rc={rc} {'RED' if rc == 1 else 'NOT_RED'}")
    return 0 if rc == 1 else 1


if __name__ == "__main__":
    raise SystemExit(main())
