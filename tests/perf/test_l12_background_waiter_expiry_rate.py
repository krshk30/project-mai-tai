"""Actual synchronous budget calls only; not native/v2/OMS loop qualification."""
from collections import deque
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys
from time import perf_counter_ns

import pytest

from project_mai_tai.broker_adapters.webull_order_reads import QueryBudget, QueryBudgetUnavailable


BASE_SHA = "102d1022b7fad52e40aecb79cd8dc313b91de48f3c9b116cff2a07c727383903"


@pytest.mark.timing
@pytest.mark.parametrize("waiters", [0, 64, 4096])
def test_actual_background_claim_rate_under_controlled_waiter_load(waiters, record_property):
    path = Path(os.environ["L12_BASELINE_SOURCE"])
    assert not path.is_symlink() and hashlib.sha256(path.read_bytes()).hexdigest() == BASE_SHA
    spec = importlib.util.spec_from_file_location("CONTROLLED_original_l12_budget", path)
    baseline = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = baseline
    spec.loader.exec_module(baseline)
    measured = []
    for label, cls, refused_type in (("BASE", baseline.QueryBudget, baseline.QueryBudgetUnavailable),
                                   ("FIX", QueryBudget, QueryBudgetUnavailable)):
        budget = cls(clock=lambda: 100.0)
        for index in range(waiters):
            endpoint = "detail" if index % 2 else "list-today"
            owner = "CONTROLLED-" + str(index)
            budget.waiting.setdefault(endpoint, deque()).append(owner)
            budget.last_wait[(endpoint, owner)] = 99.0
        durations, accepted, refused = [], 0, 0
        start = perf_counter_ns()
        while perf_counter_ns() - start < 1_000_000_000:
            before = perf_counter_ns()
            try:
                budget.claim_background("list-open", "CONTROLLED-L12")
                accepted += 1
            except refused_type:
                refused += 1
            durations.append(perf_counter_ns() - before)
        elapsed = (perf_counter_ns() - start) / 1_000_000_000
        durations.sort()
        assert elapsed >= 1.0 and durations and max(durations) < 50_000_000
        assert sum(map(len, budget.waiting.values())) == waiters
        assert len(budget.last_wait) == waiters
        assert accepted == (0 if waiters else 1) and refused == len(durations) - accepted
        assert not budget.attempts["list-open"]
        measured.append(dict(source=label, waiters=waiters, calls=len(durations), elapsed_seconds=elapsed,
            calls_per_second=len(durations) / elapsed, max_ms=max(durations) / 1_000_000,
            p99_ms=durations[min(len(durations)-1, 99*len(durations)//100)] / 1_000_000,
            accepted=accepted, refused=refused, ordinary_permits=0))
    record_property("actual_budget_rate", json.dumps(measured, sort_keys=True))
    print("L12_BACKGROUND_RATE=" + json.dumps(measured, sort_keys=True))
