"""Process-local source mutations; never edit runtime files or touch a broker."""
import inspect
import sys
import textwrap

import pytest

from project_mai_tai.broker_adapters.webull import WebullBrokerAdapter
from project_mai_tai.broker_adapters.webull_order_reads import QueryBudget, TodayOrderReader


mutations = {
    "detail_first_restored": (
        WebullBrokerAdapter, "_ordinary_order_body",
        'if not getattr(self, "_list_primary_enabled", False):', "if True:",
    ),
    "nested_foreign_client_adopted": (
        WebullBrokerAdapter, "_validated_list_execution",
        'for key in ("client_order_id", "clientOrderId"):', "for key in ():",
    ),
    "stale_page_served": (
        TodayOrderReader, "read",
        "if row is not None and 0 <= now - row[0] < fresh_seconds:", "if row is not None:",
    ),
    "budget_not_counted": (
        QueryBudget, "claim",
        "attempts.append(now)", "pass",
    ),
    "page20_claimed_complete": (
        TodayOrderReader, "read",
        "if scan.pages >= 20:", "if False:",
    ),
    "missing_child_claimed_readable": (
        WebullBrokerAdapter, "_exit_fill_blocking",
        "if unknown_child:", "if False:",
    ),
    "first_filled_lost": (
        WebullBrokerAdapter, "_exit_fill_blocking",
        "return {", "candidate = {",
    ),
    "durable_proof_ignored": (
        WebullBrokerAdapter, "_ordinary_order_body",
        "proof = store.load(account.account_id, client_id, version) if store else None", "proof = None",
    ),
    "forever_attempted": (
        WebullBrokerAdapter, "_ordinary_order_body",
        "self._terminal_inflight.discard(key)", "pass",
    ),
    "strict_read_uses_list": (
        WebullBrokerAdapter, "_atr_buy_order_detail",
        "response = self._budgeted_detail(account, detail, client_order_id, strict=True)",
        "return 200, self._today_order_detail_blocking(account, client_order_id)",
    ),
    "quantity_alias_conflict_accepted": (
        WebullBrokerAdapter, "_validated_list_execution",
        "value is None or not value.is_finite() or value != qty", "False",
    ),
    "price_alias_conflict_accepted": (
        WebullBrokerAdapter, "_validated_list_execution",
        "value is None or not value.is_finite() or value != price", "False",
    ),
    "unknown_status_alias_accepted": (
        WebullBrokerAdapter, "_validated_list_execution",
        "any(value not in known for value in status_aliases)", "False",
    ),
    "list_not_found_swallowed": (
        WebullBrokerAdapter, "_exit_fill_blocking",
        'self._is_order_not_found(exc) and not getattr(self, "_list_primary_enabled", False)',
        "self._is_order_not_found(exc)",
    ),
}
name = sys.argv[1]
owner, method, before, after = mutations[name]
original = getattr(owner, method)
source = textwrap.dedent(inspect.getsource(original))
assert source.count(before) == 1, (name, source.count(before))
namespace = dict(original.__globals__)
exec(compile(source.replace(before, after), original.__code__.co_filename, "exec"), namespace)
setattr(owner, method, namespace[method])
result = pytest.main(["-p", "no:cacheprovider", "tests/unit/test_webull_list_primary.py", "-q"])
print(f"MUTATION {name} pytest_rc={result} killed={int(result == 1)}")
raise SystemExit(0 if result == 1 else 1)
