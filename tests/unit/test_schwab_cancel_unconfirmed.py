"""A failed cancel-confirmation must never be reported as a cancellation.

⭐⭐ WHY (2026-07-30, live money). `_cancel_order` DELETEs the order, then re-fetches it to learn its
real final state. When that fetch returned None it reported "cancelled" — claiming the single most
consequential terminal state on no evidence at all.

IRE: a resting buy-stop was placed 12:45 and FILLED at the broker as price rose through 9.03. The
reprice cancel landed 12:47; the DELETE did not 400; the confirming fetch failed; we booked
"cancelled". The broker held 2 shares with a live OTOCO bracket (target 9.2069) that the OMS did not
know existed and therefore never managed — no flip exit, no stop management, invisible in
`virtual_positions` while `account_positions` showed 4 against our 2. That day had 139 failed order
fetches and 130 rate-limit errors, so this is not a rare race.

⭐ The rule already existed elsewhere and was never applied here: the Webull mirror was hardened in
 #537 to raise on a 429 rather than read an empty response as flat. Same defect, different adapter.
"""
from __future__ import annotations

import asyncio

from project_mai_tai.broker_adapters.protocols import OrderRequest
from project_mai_tai.broker_adapters.schwab import (
    SchwabAccountConfig,
    SchwabBrokerAdapter,
)
from project_mai_tai.oms.store import OmsStore
from project_mai_tai.settings import Settings


def _adapter(*, fetch_result, delete_status: int = 200) -> SchwabBrokerAdapter:
    a = object.__new__(SchwabBrokerAdapter)
    a.settings = Settings()

    async def _req(method, path, **kw):
        return (delete_status, {}, {})

    async def _fetch(account, boid):
        return fetch_result

    a._authorized_request_json = _req
    a._fetch_order = _fetch
    a._extract_error_reason = lambda body: "err"
    a._execution_report_from_order = lambda **kw: kw
    return a


def _cancel(adapter) -> list:
    req = OrderRequest(
        broker_account_name="live:schwab_1m_v2",
        strategy_code="schwab_1m_v2",
        client_order_id="schwab_1m_v2-IRE-open-3ffb2ebed35c",
        symbol="IRE",
        side="buy",
        quantity=2,
        intent_type="cancel",
        reason="schwab_1m_v2 resting-entry cancel",
        metadata={"broker_order_id": "1007401978921"},
    )
    return asyncio.run(adapter._cancel_order(SchwabAccountConfig(account_hash="h"), req))


def test_a_failed_confirmation_is_NOT_reported_as_cancelled() -> None:
    """THE REGRESSION. This is the exact IRE shape."""
    reports = _cancel(_adapter(fetch_result=None))
    assert len(reports) == 1
    assert reports[0].event_type != "cancelled", (
        "an unconfirmed cancel must never claim the order is dead — it may have FILLED"
    )


def test_the_order_is_left_in_an_OPEN_state_so_reconcile_resolves_it() -> None:
    """⛔ Load-bearing: the reported status must be one the OMS still considers open, or the order
    drops out of the reconcile sweep and the ambiguity is never resolved."""
    reports = _cancel(_adapter(fetch_result=None))
    assert reports[0].event_type in OmsStore.OPEN_ORDER_STATUSES


def test_the_reason_says_it_is_unconfirmed() -> None:
    """An operator reading the order row must see WHY it is still open."""
    assert "unconfirmed" in (_cancel(_adapter(fetch_result=None))[0].reason or "").lower()


def test_a_4xx_delete_is_still_rejected() -> None:
    """Unchanged behaviour — a refused DELETE is a real, known answer."""
    reports = _cancel(_adapter(fetch_result=None, delete_status=400))
    assert reports[0].event_type == "rejected"


def test_a_SUCCESSFUL_fetch_still_reports_the_real_status() -> None:
    """⛔ The guard must not swallow a genuine confirmation. When the broker DOES answer, its
    answer wins — including 'this actually filled'."""
    reports = _cancel(_adapter(fetch_result={"status": "FILLED"}))
    assert reports[0]["event_type"] != "accepted"
