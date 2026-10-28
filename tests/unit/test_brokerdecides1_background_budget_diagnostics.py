"""Six existing refusal operands, controlled clock/state, no HTTP/provider calls."""
from collections import deque
import logging
from types import SimpleNamespace

import pytest

from project_mai_tai import brokerdecides1_collect as collector
from project_mai_tai.broker_adapters.webull_order_reads import QueryBudget, QueryBudgetUnavailable


@pytest.mark.asyncio
@pytest.mark.parametrize("condition,expected", [
    ("foreground", "foreground"),
    ("terminal", "terminal_inflight"),
    ("pending", "ordinary_pending"),
    ("attempt", "recent_ordinary_attempt_500ms"),
    ("backoff", "backoff"),
    ("interval", "background_interval_10000ms"),
])
async def test_first_existing_budget_operand_is_logged_without_permit_or_private_data(
        monkeypatch, caplog, condition, expected):
    clock_reads = []

    def clock():
        clock_reads.append(100.0)
        return 100.0

    budget = QueryBudget(clock=clock)
    priority = ("foreground", "terminal", "pending", "attempt", "backoff", "interval")
    active = priority[priority.index(condition):]
    # Later operands are also true: the code must name only the FIRST operand,
    # not claim that the other refusal conditions were false.
    budget.foreground_reads = int("foreground" in active)
    budget.terminal_inflight = {("private-account", "private-order", "private-token")} if "terminal" in active else set()
    budget.waiting = {"detail": deque(["private-queue-owner"])} if "pending" in active else {}
    budget.attempts = {"list-open": deque([99.5])} if "attempt" in active else {}
    budget.blocked_until = {"list-open": 101.0} if "backoff" in active else {}
    budget.background_at = {"list-open": 90.0}
    original_attempts = {key: tuple(value) for key, value in budget.attempts.items()}
    failures = []

    def blocking(adapter, account):
        collector._diagnostic_stage("orders", 1)
        try:
            budget.claim_background("list-open", "private-account")
        except QueryBudgetUnavailable as exc:
            failures.append(exc)
            raise
        raise AssertionError("refused background query must not reach HTTP")

    monkeypatch.setattr(collector, "_webull_blocking", blocking)
    with caplog.at_level(logging.WARNING, logger=collector.__name__):
        with pytest.raises(ValueError, match="^Webull_native_collection_UNKNOWN$"):
            await collector.read_webull(SimpleNamespace(), "live:orb")
    failure, = failures
    assert type(failure) is QueryBudgetUnavailable
    assert failure.args == ("list-open background budget unavailable",)
    assert failure.background_refusal_code == expected
    assert clock_reads == [100.0]  # No second clock read for the diagnostic.
    assert budget.background_at == {"list-open": 90.0}  # No permit/interval stamp.
    assert {key: tuple(value) for key, value in budget.attempts.items()} == {
        "list-open": original_attempts.get("list-open", ())}
    records = [record for record in caplog.records
        if record.getMessage().startswith("[BROKERDECIDES1-NATIVE-FAIL]")]
    record, = records
    text = record.getMessage()
    assert f"msg=list-open background budget unavailable;condition={expected} " in text
    assert "stage=orders page=1 http_status=UNKNOWN" in text
    assert record.exc_info is None
    assert all(secret not in text for secret in (
        "private-account", "private-order", "private-token", "private-queue-owner"))
