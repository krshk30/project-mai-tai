"""Offline collector controls: no provider connection or live SDK execution."""
import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest

from project_mai_tai.brokerdecides1_collect import owned_read, read_schwab, schwab_windows, webull_pages
from project_mai_tai.settings import Settings


def test_reviewed_age_default_is_twenty_seconds():
    assert Settings().strategy_schwab_1m_v2_brokerdecides1_max_age_ms == 20_000


def test_webull_pagination_uses_actual_open_cursor_not_local_ids():
    calls = []

    def fetch(cursor):
        calls.append(cursor)
        return 200, {"account_id": "a", "has_next": not cursor, "orders": [
            {"order_id": "manual-native", "client_order_id": "cursor-one"}] if not cursor else []}

    assert len(webull_pages(fetch, account_id="a", field="orders", page_size=100,
        cursor_keys=("client_order_id", "clientOrderId"))) == 2
    assert calls == ["", "cursor-one"]


@pytest.mark.parametrize("fault", ["status", "account", "no_flag", "bad_flag", "no_cursor", "cycle"])
def test_webull_native_page_failure_never_becomes_empty(fault):
    def fetch(cursor):
        body = {"account_id": "a", "has_next": True, "orders": [{"client_order_id": "same"}]}
        if fault == "account":
            body["account_id"] = "foreign"
        elif fault == "no_flag":
            body.pop("has_next")
        elif fault == "bad_flag":
            body["has_next"] = "false"
        elif fault == "no_cursor":
            body["orders"] = []
        return (429 if fault == "status" else 200), body

    with pytest.raises(ValueError):
        webull_pages(fetch, account_id="a", field="orders", page_size=100,
                     cursor_keys=("client_order_id",))


@pytest.mark.asyncio
async def test_schwab_cap_splits_time_windows_and_dedupes_boundary():
    start, end = datetime(2026, 10, 1, tzinfo=UTC), datetime(2026, 10, 3, tzinfo=UTC)
    calls = []

    async def fetch(lower, upper, cap):
        calls.append((lower, upper))
        if lower == start and upper == end:
            return 200, [{"orderId": 1}, {"orderId": 2}]
        return 200, [{"orderId": 1}]

    assert await schwab_windows(fetch, start, end, max_results=2) == [{"orderId": 1}]
    assert len(calls) == 3


@pytest.mark.asyncio
async def test_schwab_indivisible_cap_holds():
    async def fetch(*args):
        return 200, [{"orderId": 1}]

    start = datetime(2026, 10, 1, tzinfo=UTC)
    with pytest.raises(ValueError, match="time_cap_UNKNOWN"):
        await schwab_windows(fetch, start, start + timedelta(milliseconds=1), max_results=1)


@pytest.mark.asyncio
async def test_schwab_actual_query_is_181_days_with_explicit_reviewed_scope():
    calls = []

    async def request(method, path):
        calls.append((method, path))
        if path.endswith("accountNumbers"):
            return 200, {}, [{"accountNumber": "123", "hashValue": "fixture-hash"}]
        if "fields=positions" in path:
            return 200, {}, {"securitiesAccount": {"accountNumber": "123", "positions": []}}
        return 200, {}, []

    adapter = SimpleNamespace(accounts_by_name={"live:schwab": SimpleNamespace(account_hash="fixture-hash")},
        _authorized_request_json=request)
    book = await read_schwab(adapter, "live:schwab")
    assert book.complete and "older_unfilled_GTC_accepted" in book.reason
    assert book.scope_policy == "schwab_reviewed_window_20261022_145124"
    assert book.window_days == 181
    assert len(calls) == 3 and all(method == "GET" for method, path in calls)
    assert "fromEnteredTime=" in calls[-1][1] and "maxResults=500" in calls[-1][1]
    assert "status=" not in calls[-1][1]  # All statuses, not a guessed finite subset.
    query = parse_qs(urlsplit(calls[-1][1]).query)
    assert datetime.fromisoformat(query["toEnteredTime"][0]) - datetime.fromisoformat(
        query["fromEnteredTime"][0]) == timedelta(days=181)


@pytest.mark.asyncio
async def test_outer_timeout_retains_one_native_invocation_no_copy():
    adapter = SimpleNamespace()
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def operation():
        calls.append(1)
        entered.set()
        await release.wait()
        return "done"

    caller = asyncio.create_task(owned_read(adapter, "live:wb", operation))
    await entered.wait()
    caller.cancel()
    with pytest.raises(asyncio.CancelledError):
        await caller
    with pytest.raises(ValueError, match="inflight_UNKNOWN"):
        await owned_read(adapter, "live:wb", operation)
    assert calls == [1]
    release.set()
    await adapter._brokerdecides_native_tasks["live:wb"]


@pytest.mark.asyncio
@pytest.mark.parametrize("shape", ["omitted", "nonempty", "null", "nonlist", "missing_identity",
    "null_identity", "foreign_identity", "http_error", "error_envelope"])
async def test_schwab_request_bound_positions_omission_contract(shape):
    calls = []
    body = {"securitiesAccount": {"accountNumber": "123"}}
    account = body["securitiesAccount"]
    if shape == "nonempty":
        account["positions"] = [{"instrument": {"symbol": "WORX"},
            "longQuantity": 2, "shortQuantity": 0}]
    elif shape in ("null", "nonlist"):
        account["positions"] = None if shape == "null" else {}
    elif shape == "missing_identity":
        account.pop("accountNumber")
    elif shape in ("null_identity", "foreign_identity"):
        account["accountNumber"] = None if shape == "null_identity" else "foreign"
    elif shape == "error_envelope":
        body["error"] = "CONTROLLED error"

    async def request(method, path):
        calls.append((method, path))
        if path.endswith("accountNumbers"):
            return 200, {}, [{"accountNumber": "123", "hashValue": "fixture-hash"}]
        if "fields=positions" in path:
            return (503 if shape == "http_error" else 200), {}, body
        return 200, {}, []

    adapter = SimpleNamespace(accounts_by_name={"live:schwab":
        SimpleNamespace(account_hash="fixture-hash")}, _authorized_request_json=request)
    if shape not in ("omitted", "nonempty"):
        with pytest.raises(ValueError):
            await read_schwab(adapter, "live:schwab")
        return
    book = await read_schwab(adapter, "live:schwab")
    assert book.complete and book.working == frozenset()
    assert book.positions == (frozenset({"WORX"}) if shape == "nonempty" else frozenset())
    assert book.window_days == 181 and len(calls) == 3
    if shape == "omitted":
        assert "positions" not in account
    else:
        assert len(account["positions"]) == 1
