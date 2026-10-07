"""Assertion mutations in memory; never modifies the tested source checkout."""
import inspect
import sys
import textwrap

import pytest

from project_mai_tai.strategy_core import schwab_1m_v2 as v2
from project_mai_tai import v2_removed_wait as wait

MUTATIONS = {
    "cancel_not_emitted": (v2.SchwabV2Strategy, "_cancel_retry_leftovers",
                          "self._queue_removed_wait_barriers(state, request)", "pass"),
    "soft_rest_not_disarmed": (v2.SchwabV2Strategy, "_cancel_retry_leftovers",
                               "state.cw_armed = False", "pass"),
    "receipt_grants_entry": (v2.SchwabV2Strategy, "apply_removed_wait_proofs",
                             'self._removed_wait_requests.pop(request.symbol, None)',
                             'self._removed_wait_requests.pop(request.symbol, None); self._clear_flip_owner_memory(state)'),
    "receipt_token_ignored": (v2.SchwabV2Strategy, "apply_removed_wait_proofs",
                             'self._removed_wait_requests.get(request.symbol) == request', 'True'),
    "receipt_age_ignored": (v2.SchwabV2Strategy, "apply_removed_wait_proofs",
                           '0 <= self._now_ms() - proof.observed_at_ms <= FLIP_OWNER_EVIDENCE_MAX_AGE_MS', 'True'),
    "scanner_proof_reused": (v2.SchwabV2Strategy, "apply_removed_wait_proofs",
                            'proof.reason == "retry_leftovers_cancelled_owner_kept"', 'True'),
    "retry_allowed_still_cancels": (v2.SchwabV2Strategy, "_retry_one_release_or_hold",
                                   'persisted and closes_in_segment < max_closes', 'False'),
    "unknown_cancel_accepted": (wait, "assess_removed_wait",
                                'intent.status not in {"cancelled", "canceled"} and not no_target', 'False'),
    "bound_close_proof_removed": (v2.SchwabV2Strategy, "_apply_flip_position_evidence",
                                  'if reason is not None:', 'if True:'),
    "mirror_barrier_removed": (v2.SchwabV2Strategy, "_queue_removed_wait_barriers",
                               'if self._dual_broker_fanout_enabled and self.settings.strategy_schwab_1m_v2_webull_account_name:',
                               'if False:'),
    "terminal_source_ignored": (wait, "assess_removed_wait",
                                'e.event_source == "broker"', 'True'),
    "terminal_status_ignored": (wait, "assess_removed_wait",
                                '("cancelled" if e.event_type == "canceled" else e.event_type) == status', 'True'),
    "unknown_terminal_outcome_accepted": (wait, "assess_removed_wait",
                                        'metadata(e).get("cancel_outcome") not in {',
                                        '"proven_terminal" not in {'),
}


def main():
    key = sys.argv[1]
    owner, name, old, new = MUTATIONS[key]
    original = getattr(owner, name)
    source = textwrap.dedent(inspect.getsource(original))
    assert old in source, key
    namespace = dict(original.__globals__)
    exec(compile(source.replace(old, new, 1), f"<retryleft1-mutation:{key}>", "exec"), namespace)
    setattr(owner, name, namespace[name])
    return pytest.main(["tests/unit/test_retryleft1.py", "-q", "--tb=short"])


if __name__ == "__main__":
    raise SystemExit(main())
