"""C1 05:18: actual budget claims, CONTROLLED monotonic clock, no HTTP."""
from collections import deque
from copy import deepcopy

import pytest

from project_mai_tai.broker_adapters.webull_order_reads import QueryBudget, QueryBudgetUnavailable


@pytest.mark.parametrize("endpoint", ["detail", "list-today", "other-ordinary-endpoint"])
def test_refused_ordinary_owner_then_only_background_claims_expires_at_30(endpoint):
    now = [0.0]
    budget = QueryBudget(clock=lambda: now[0])
    budget.claim(endpoint, "first-attempt")
    if endpoint != "detail":
        budget.claim(endpoint, "second-attempt")
    with pytest.raises(QueryBudgetUnavailable):
        budget.claim(endpoint, "CONTROLLED-native-account:JDZG-entry-coid")
    queue = budget.waiting[endpoint]
    assert tuple(queue) == ("CONTROLLED-native-account:JDZG-entry-coid",)
    attempts = tuple(budget.attempts[endpoint])
    for at in (1.0, 10.0, 29.999):
        now[0] = at
        with pytest.raises(QueryBudgetUnavailable) as refused:
            budget.claim_background("list-open", "L12")
        assert refused.value.background_refusal_code == "ordinary_pending"
        assert tuple(queue) == ("CONTROLLED-native-account:JDZG-entry-coid",)
        assert budget.last_wait[(endpoint, queue[0])] == 0.0
        assert not budget.background_at
    now[0] = 30.0
    assert budget.claim_background("list-open", "L12") is None
    assert budget.waiting[endpoint] is queue and not queue
    assert (endpoint, "CONTROLLED-native-account:JDZG-entry-coid") not in budget.last_wait
    assert tuple(budget.attempts[endpoint]) == attempts  # Cleanup grants no ordinary permit.
    assert not budget.attempts["list-open"] and budget.background_at == {"list-open": 30.0}


def test_live_waiter_refresh_keeps_its_original_priority():
    now = [0.0]
    budget = QueryBudget(clock=lambda: now[0])
    budget.defer("detail", 60.0)
    with pytest.raises(QueryBudgetUnavailable):
        budget.claim("detail", "active-owner")
    now[0] = 29.0
    with pytest.raises(QueryBudgetUnavailable):
        budget.claim("detail", "active-owner")
    now[0] = 30.0
    with pytest.raises(QueryBudgetUnavailable) as refused:
        budget.claim_background("list-open", "L12")
    assert refused.value.background_refusal_code == "ordinary_pending"
    assert list(budget.waiting["detail"]) == ["active-owner"]
    assert budget.last_wait == {("detail", "active-owner"): 29.0}
    assert budget.blocked_until == {"detail": 60.0} and not budget.background_at


def test_expiry_across_every_endpoint_preserves_live_fifo_queue_identity_and_unrelated_state():
    budget = QueryBudget(clock=lambda: 100.0)
    expected = {}
    for endpoint in ("detail", "list-today", "unrelated-ordinary"):
        budget.waiting[endpoint] = deque(["old-a", "live-a", "old-b", "live-b"])
        budget.last_wait.update({(endpoint, "old-a"): 69.0, (endpoint, "old-b"): 70.0,
            (endpoint, "live-a"): 99.0, (endpoint, "live-b"): 71.0})
        expected[endpoint] = budget.waiting[endpoint]
    budget.last_wait[("not-queued", "unrelated")] = 1.0
    with pytest.raises(QueryBudgetUnavailable) as refused:
        budget.claim_background("positions", "L12")
    assert refused.value.background_refusal_code == "ordinary_pending"
    for endpoint, original in expected.items():
        assert budget.waiting[endpoint] is original and list(original) == ["live-a", "live-b"]
        assert (endpoint, "old-a") not in budget.last_wait and (endpoint, "old-b") not in budget.last_wait
    assert budget.last_wait[("not-queued", "unrelated")] == 1.0
    assert not budget.background_at and not budget.attempts["positions"]


@pytest.mark.parametrize("timestamp", [None, 101.0, float("nan")])
def test_missing_future_or_nan_wait_clock_keeps_existing_conservative_pending(timestamp):
    budget = QueryBudget(clock=lambda: 100.0)
    queue = budget.waiting["detail"] = deque(["unknown-clock"])
    if timestamp is not None:
        budget.last_wait[("detail", "unknown-clock")] = timestamp
    with pytest.raises(QueryBudgetUnavailable) as refused:
        budget.claim_background("list-open", "L12")
    assert refused.value.background_refusal_code == "ordinary_pending"
    assert budget.waiting["detail"] is queue and tuple(queue) == ("unknown-clock",)
    assert not budget.background_at


@pytest.mark.parametrize("condition,code", [
    ("foreground", "foreground"), ("terminal", "terminal_inflight"),
    ("attempt", "recent_ordinary_attempt_1000ms"), ("backoff", "backoff"),
    ("interval", "background_interval_10000ms"),
])
def test_expiring_an_abandoned_owner_cannot_override_other_original_refusal(condition, code):
    budget = QueryBudget(clock=lambda: 100.0)
    budget.waiting["detail"] = deque(["abandoned"])
    budget.last_wait[("detail", "abandoned")] = 0.0
    if condition == "foreground":
        budget.foreground_reads = 1
    elif condition == "terminal":
        budget.terminal_inflight.add(("account", "order", "proof"))
    elif condition == "attempt":
        budget.attempts["list-open"] = deque([99.0])
    elif condition == "backoff":
        budget.defer("list-open", 30.0)
    else:
        budget.background_at["list-open"] = 90.0
    attempts, backoff, background = deepcopy(budget.attempts), dict(budget.blocked_until), dict(budget.background_at)
    with pytest.raises(QueryBudgetUnavailable) as refused:
        budget.claim_background("list-open", "L12")
    assert refused.value.background_refusal_code == code
    assert not budget.waiting["detail"] and not budget.last_wait
    assert budget.attempts == {**attempts, "list-open": attempts.get("list-open", deque())}
    assert budget.blocked_until == backoff and budget.background_at == background


def test_cleanup_and_pending_check_use_the_same_single_clock_sample_and_lock():
    calls = []
    budget = None
    def clock():
        assert budget.lock.locked()
        calls.append(30.0)
        return 30.0
    budget = QueryBudget(clock=clock)
    budget.waiting["detail"] = deque(["abandoned"])
    budget.last_wait[("detail", "abandoned")] = 0.0
    budget.claim_background("positions", "L12")
    assert calls == [30.0] and not budget.waiting["detail"]
    assert budget.background_at == {"positions": 30.0}
