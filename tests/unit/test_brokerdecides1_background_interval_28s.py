"""C11629 controlled self-interval change; no HTTP/provider/async runtime."""
from collections import deque

import pytest

from project_mai_tai.broker_adapters.webull_order_reads import (
    L12_BACKGROUND_MIN_INTERVAL_SECONDS, QueryBudget, QueryBudgetUnavailable,
)


def budget_at(now):
    reads = []

    def clock():
        reads.append(now)
        return now

    budget = QueryBudget(clock=clock)
    budget.background_at["list-open"] = 0.0
    return budget, reads


def test_background_claim_at_29_973_seconds_is_allowed_and_stamped_at_actual_claim():
    budget, reads = budget_at(29.973)
    assert L12_BACKGROUND_MIN_INTERVAL_SECONDS == 28.0
    assert budget.claim_background("list-open", "controlled-owner") is None
    assert reads == [29.973]
    assert budget.background_at == {"list-open": 29.973}
    assert list(budget.attempts["list-open"]) == []  # No ordinary permit consumed.
    assert budget.waiting == {} and budget.blocked_until == {}


def test_background_claim_at_27_9_seconds_retains_interval_refusal():
    budget, reads = budget_at(27.9)
    with pytest.raises(QueryBudgetUnavailable) as caught:
        budget.claim_background("list-open", "controlled-owner")
    assert caught.value.args == ("list-open background budget unavailable",)
    assert caught.value.background_refusal_code == "background_interval_27900ms"
    assert reads == [27.9] and budget.background_at == {"list-open": 0.0}


def test_ordinary_attempt_1_5_seconds_ago_still_refuses_background():
    budget, reads = budget_at(30.0)
    budget.attempts["list-open"] = deque([28.5])
    with pytest.raises(QueryBudgetUnavailable) as caught:
        budget.claim_background("list-open", "controlled-owner")
    assert caught.value.background_refusal_code == "recent_ordinary_attempt_1500ms"
    assert list(budget.attempts["list-open"]) == [28.5]
    assert reads == [30.0] and budget.background_at == {"list-open": 0.0}


def test_foreground_still_refuses_otherwise_eligible_background_claim():
    budget, reads = budget_at(29.973)
    with budget.foreground():
        with pytest.raises(QueryBudgetUnavailable) as caught:
            budget.claim_background("list-open", "controlled-owner")
        assert budget.foreground_reads == 1
    assert caught.value.background_refusal_code == "foreground"
    assert budget.foreground_reads == 0
    assert reads == [29.973] and budget.background_at == {"list-open": 0.0}
