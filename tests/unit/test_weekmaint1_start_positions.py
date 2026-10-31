"""10:0x authored start-only collector controls; existing adapter leaves only."""
import asyncio
from copy import deepcopy
from datetime import timedelta
from types import SimpleNamespace

import pytest

from ops.weekmaint import collector, policy
from tests.unit.test_weekmaint1_policy import ACCOUNTS, NOW, snapshot
from tests.unit.test_weekmaint1_review_fixes import make_backend as make_backend  # noqa: F401


class ExistingAdapter:
    def __init__(self, *, failure=None):
        self.calls = []
        self.failure = failure

    async def list_account_positions(self, account):
        self.calls.append(account)
        if account == self.failure:
            raise TimeoutError("controlled existing positions read failed")
        return [SimpleNamespace(symbol="MANUAL", broker_account_name=account, quantity="1000")]

    def positions_wire_read_age_seconds(self, account):
        return 2


def test_exact_one_existing_positions_call_each_and_manual_shares_ignored_no_order_reader():
    adapter = ExistingAdapter()
    item = collector.ExistingReadCollector(adapter, ACCOUNTS, clock=lambda: NOW)
    evidence = asyncio.run(item.collect([], {"live_accounts": ACCOUNTS.copy()}))
    assert adapter.calls == list(ACCOUNTS)
    policy.broker_gate(ACCOUNTS, evidence, NOW)
    for account in ACCOUNTS:
        assert evidence[account]["positions"]["rows"] == [{"symbol": "MANUAL", "quantity": "1000",
            "bot_owned_quantity": "0", "ownership_complete": True}]
    webull = next(account for account, provider in ACCOUNTS.items() if provider == "webull")
    assert evidence[webull]["positions"]["read_at"] == (NOW - timedelta(seconds=2)).isoformat()
    with pytest.raises(policy.Refused, match="already_attempted"):
        asyncio.run(item.collect([], {"live_accounts": ACCOUNTS.copy()}))
    assert adapter.calls == list(ACCOUNTS)


@pytest.mark.parametrize("account", ACCOUNTS)
def test_any_start_read_failure_refuses_and_cannot_retry(account):
    adapter = ExistingAdapter(failure=account)
    item = collector.ExistingReadCollector(adapter, ACCOUNTS, clock=lambda: NOW)
    with pytest.raises(TimeoutError):
        asyncio.run(item.collect([], {"live_accounts": ACCOUNTS.copy()}))
    calls = list(adapter.calls)
    with pytest.raises(policy.Refused, match="already_attempted"):
        asyncio.run(item.collect([], {"live_accounts": ACCOUNTS.copy()}))
    assert adapter.calls == calls


def test_failed_initial_backend_collection_is_not_retried_poststop(make_backend):
    item = make_backend()
    facts = snapshot()
    item.database_inventory = lambda: ([], deepcopy(facts))
    item.read_writer_inventory = lambda: {}
    calls = []
    class FailedCollector:
        async def collect(self, rows, current):
            calls.append("attempt")
            raise TimeoutError("controlled collector failure")
    item.collector = FailedCollector()
    with pytest.raises(TimeoutError):
        item.read_snapshot()
    with pytest.raises(policy.Refused, match="failed_no_retry"):
        item.read_snapshot()
    assert calls == ["attempt"]


def test_changed_ownership_never_refreshes_original_wire_evidence(make_backend):
    item = make_backend()
    facts = snapshot()
    rows = []
    item.database_inventory = lambda: (deepcopy(rows), deepcopy(facts))
    item.read_writer_inventory = lambda: {}
    calls = []
    class GoodCollector:
        async def collect(self, current_rows, current):
            calls.append("attempt")
            return deepcopy(facts["broker_evidence"])
    item.collector = GoodCollector()
    item.read_snapshot()
    rows.append({"table": "virtual_positions", "account": next(iter(ACCOUNTS)),
                 "row": {"id": "controlled-owned-row", "symbol": "MANUAL", "quantity": "1"}})
    with pytest.raises(policy.Refused, match="ownership_changed"):
        item.read_snapshot()
    assert calls == ["attempt"]
