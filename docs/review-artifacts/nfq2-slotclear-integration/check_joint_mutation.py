"""Conflict-resolution controls mutate only this child process."""
import inspect
import sys
import textwrap

import pytest

from project_mai_tai.oms.service import OmsRiskService
from project_mai_tai.strategy_core.schwab_1m_v2 import SchwabV2Strategy

controls = {
    "C1": (OmsRiskService, "_apply_v2_eh_reactive_entry",
           'if md.get("slotclear_first") == "true" and not self._nfq2_held_dispatch(event):',
           "if False:", "test_joint_slotclear_fresh_reactive_never_borrows_held_cap"),
    "C2": (OmsRiskService, "_apply_v2_eh_reactive_entry",
           'if md.get("slotclear_first") == "true" and not self._nfq2_held_dispatch(event):',
           'if md.get("slotclear_first") == "true":',
           "test_joint_slotclear_real_nfq_dispatch_retains_held_cap_and_single_claim"),
    "C3": (SchwabV2Strategy, "_cw_v2_quote",
           "self._dual_broker_fanout_enabled or fresh_first", "self._dual_broker_fanout_enabled",
           "test_joint_fresh_buy_identity_and_dedup_with_nfq_enabled[False-False]"),
}
cls, method, old, new, test = controls[sys.argv[1]]
original = getattr(cls, method)
source = textwrap.dedent(inspect.getsource(original))
assert source.count(old) == 1
namespace = dict(original.__globals__)
exec(compile("from __future__ import annotations\n" + source.replace(old, new),
             f"<joint-mutation-{sys.argv[1]}>", "exec"), namespace)
setattr(cls, method, namespace[method])
rc = pytest.main(["tests/unit/test_nfq2_slotclear_composition.py::" + test, "-q", "--tb=short"])
print(f"JOINT_MUTATION {sys.argv[1]} pytest_rc={rc}")
raise SystemExit(0 if rc == 1 else 1)
