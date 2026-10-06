"""WEBULL429 recorded case identities plus explicitly synthetic query controls."""

from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from project_mai_tai.broker_adapters.webull import WebullAccountConfig
from project_mai_tai.broker_adapters.webull_order_reads import (
    QueryBudget,
    QueryBudgetUnavailable,
    TerminalProofStore,
    TodayOrderReader,
)
from project_mai_tai.db.models import DashboardSnapshot
from project_mai_tai.settings import Settings
from tests.unit.test_webull_adapter import (
    _FakeClient,
    _HttpResp,
    _Resp,
    _ServerException,
    _adapter,
    _order,
    _TODAY_ORDERS_FILLED_FIXTURE,
)
from tests.unit.test_webull_adapter import fake_sdk as _sdk_fixture

fake_sdk = _sdk_fixture


class Clock:
    now = 100.0

    def __call__(self):
        return self.now


def primary(client, clock=None, store=None):
    clock = clock or Clock()
    adapter = _adapter(client)
    adapter._list_primary_enabled = True
    adapter._query_budget = QueryBudget(clock)
    adapter._today_reader = TodayOrderReader(adapter._query_budget, 15, clock)
    adapter._terminal_read_lock = threading.Lock()
    adapter._terminal_inflight = set()
    adapter._terminal_proof_store = store
    return adapter


def row(coid="owned", status="SUBMITTED", *, qty="0", price=None):
    item = {"order_status": status, "filled_qty": qty, "symbol": "AIFA", "side": "BUY"}
    if price is not None:
        item.update(filled_price=price, last_filled_time="2026-10-06 15:11:00.000+0000")
    return {"client_order_id": coid, "order_id": f"broker-{coid}", "items": [item]}


def listed(*rows, more=False):
    return {"orders": list(rows), "hasNext": more}


def read(adapter, coid="owned"):
    return adapter._fetch_order_blocking(
        adapter.accounts_by_name["live:orb"], _order(client_order_id=coid, symbol="AIFA")
    )


@pytest.fixture
def proof_store(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'proof.db'}")
    DashboardSnapshot.__table__.create(engine)
    yield TerminalProofStore(sessionmaker(engine))
    engine.dispose()


def test_default_on_and_rollback_switch():
    assert Settings(_env_file=None).webull_list_primary_reads_enabled
    assert not Settings(
        _env_file=None, webull_list_primary_reads_enabled=False
    ).webull_list_primary_reads_enabled


def test_working_orders_share_list_no_detail(fake_sdk):
    client = _FakeClient({"today": listed(row("a"), row("b"))})
    adapter = primary(client)
    assert read(adapter, "a").event_type == "accepted"
    assert read(adapter, "b").event_type == "accepted"
    assert client.calls == {"today": 1}


@pytest.mark.parametrize(
    "bad", [listed(), listed(row("foreign")), {"orders": None}, {"orders": [], "hasNext": "false"}]
)
def test_aifa_111109_unreadable_or_missing_is_unknown_not_flat(fake_sdk, bad):
    client = _FakeClient({"today": bad})
    client.raises["detail"] = _ServerException(
        "TOO_MANY_REQUESTS", "Too many requests", 429, "d22655f2-bdaf-4a47-a3cd-63469eee7589"
    )
    adapter = primary(client)
    # Case identity is recorded; list bodies are synthetic missing/error variants.
    import asyncio

    result = asyncio.run(
        adapter.fetch_order_update(
            _order(client_order_id="schwab_1m_v2-AIFA-open-621d8b9aeb95", symbol="AIFA")
        )
    )
    assert result is None
    assert client.calls == {"today": 1}


@pytest.mark.parametrize(
    "mutation", ["account", "client", "qty", "price", "time", "broker", "status", "symbol"]
)
def test_conflicting_identity_and_unusable_fill_do_not_release(fake_sdk, mutation):
    raw = row(status="FILLED", qty="1", price="1.11")
    if mutation == "account":
        raw["account_id"] = "FOREIGN"
    elif mutation == "client":
        raw["items"][0]["client_order_id"] = "FOREIGN"
    elif mutation == "qty":
        raw["items"][0]["filled_qty"] = "NaN"
    elif mutation == "price":
        raw["items"][0]["filled_price"] = "Infinity"
    elif mutation == "time":
        raw["items"][0].pop("last_filled_time")
    elif mutation == "broker":
        raw.pop("order_id")
    elif mutation == "status":
        raw["items"][0]["order_status"] = "UNREADABLE"
    elif mutation == "symbol":
        raw["items"][0]["symbol"] = "FOREIGN"
    client = _FakeClient({"today": listed(raw), "detail": raw})
    adapter = primary(client)
    try:
        result = read(adapter)
    except ValueError:
        result = None
    assert result is None


def test_partial_fill_is_accounted_without_terminal_probe(fake_sdk):
    client = _FakeClient({"today": listed(row(status="PARTIAL_FILLED", qty="1", price="1.11"))})
    report = read(primary(client))
    assert report.event_type == "partially_filled"
    assert report.filled_quantity == 1
    assert client.calls == {"today": 1}


@pytest.mark.parametrize("status", ["CANCELLED", "REJECTED"])
def test_terminal_with_partial_execution_keeps_fill_accounting_and_ownership(fake_sdk, status):
    raw = row(status=status, qty="1", price="1.11")
    client = _FakeClient({"today": listed(raw), "detail": raw})
    report = read(primary(client))
    assert report.event_type == "partially_filled"
    assert report.filled_quantity == 1
    assert report.broker_fill_id == "broker-owned:1"
    assert report.metadata["webull_terminal_with_partial_fill"] == status


def test_recorded_sep21_execution_survives_optional_detail_429(fake_sdk):
    raw = json.loads(_TODAY_ORDERS_FILLED_FIXTURE.read_text())["row"]
    client = _FakeClient({"today": listed(raw)})
    client.raises["detail"] = _ServerException("TOO_MANY_REQUESTS", "Too many requests", 429)
    adapter = primary(client)
    adapter.accounts_by_name["live:orb"] = WebullAccountConfig(raw["account_id"])
    report = adapter._fetch_order_blocking(
        adapter.accounts_by_name["live:orb"],
        _order(
            client_order_id=raw["client_order_id"], symbol=raw["items"][0]["symbol"], side="sell"
        ),
    )
    assert report.event_type == "filled"
    assert report.filled_quantity == Decimal("1")
    assert report.fill_price == Decimal("1.11")
    assert report.reported_at.isoformat() == "2026-09-21T14:17:44.527000+00:00"
    assert client.calls == {"today": 1, "detail": 1}
    assert "_webull_terminal_proof" not in report.metadata


def test_repeated_429_is_retryable_per_version_not_forever_attempted(fake_sdk):
    clock = Clock()
    raw = row(status="FILLED", qty="1", price="1.11")
    client = _FakeClient({"today": listed(raw), "detail": raw})
    client.raises["detail"] = _ServerException("TOO_MANY_REQUESTS", "Too many requests", 429)
    adapter = primary(client, clock)
    assert read(adapter).event_type == "filled"
    assert read(adapter).event_type == "filled"
    assert client.calls["detail"] == 1
    clock.now += 2.01
    # Same scan is not re-aged: until its next cycle there is no fresh row.
    assert read(adapter) is None
    clock.now += 13
    assert read(adapter).event_type == "filled"
    assert client.calls["detail"] == 2


@pytest.mark.parametrize("crash", ["before_commit", "after_commit"])
def test_terminal_proof_crash_retry_and_durable_idempotency(fake_sdk, proof_store, crash):
    raw = row(status="FILLED", qty="1", price="1.11")
    client = _FakeClient({"today": listed(raw), "detail": raw})
    original_save = proof_store.save

    def interrupted(*args):
        if crash == "after_commit":
            original_save(*args)
        raise RuntimeError("simulated process death")

    proof_store.save = interrupted
    first = primary(client, store=proof_store)
    with pytest.raises(RuntimeError):
        read(first)
    proof_store.save = original_save
    restarted = primary(client, store=proof_store)
    second = read(restarted)
    assert second.event_type == "filled"
    assert client.calls["detail"] == (2 if crash == "before_commit" else 1)
    again = read(primary(client, store=proof_store))
    assert again.broker_fill_id == second.broker_fill_id
    assert client.calls["detail"] == (2 if crash == "before_commit" else 1)
    with proof_store.session_factory() as session:
        assert session.scalar(select(func.count()).select_from(DashboardSnapshot)) == 1


def test_one_concurrent_terminal_attempt_per_version(fake_sdk, proof_store):
    entered, release = threading.Event(), threading.Event()
    raw = row(status="FILLED", qty="1", price="1.11")

    class Client(_FakeClient):
        def get_response(self, request):
            if request._kind == "detail":
                entered.set()
                assert release.wait(5)
            return super().get_response(request)

    client = Client({"today": listed(raw), "detail": raw})
    adapter = primary(client, store=proof_store)
    with ThreadPoolExecutor(2) as pool:
        first = pool.submit(read, adapter)
        assert entered.wait(5)
        second = pool.submit(read, adapter)
        try:
            assert second.result(2).event_type == "filled"
        finally:
            release.set()
        assert first.result(5).event_type == "filled"
    assert client.calls["detail"] == 1


def test_first_filled_target_does_not_request_sibling(fake_sdk):
    base = "schwab_1m_v2-OLOX-protect-0d727181a900"
    raw = row(base + "T", "FILLED", qty="205", price="1.53")
    raw["items"][0]["symbol"] = "OLOX"
    raw["items"][0]["side"] = "SELL"
    client = _FakeClient({"today": listed(raw), "detail": raw})
    result = primary(client)._exit_fill_blocking(WebullAccountConfig("ACC1"), "OLOX", base)
    assert result["quantity"] == 205
    assert client.calls == {"today": 1, "detail": 1}


def test_missing_oco_children_raise_unknown(fake_sdk):
    client = _FakeClient({"today": listed()})
    with pytest.raises(ValueError, match="UNKNOWN"):
        primary(client)._exit_fill_blocking(WebullAccountConfig("ACC1"), "AIFA", "owned")


@pytest.mark.parametrize(
    "coid,request_id",
    [
        ("schwab_1m_v2-OLOX-open-0eb124adbbf8", "055003ae-2940-4f55-afe9-7be5287081d5"),
        ("schwab_1m_v2-OLOX-open-e2c9f966071d", "aa10272c-2bda-4ad2-936a-b5f90bae0752"),
    ],
)
@pytest.mark.asyncio
async def test_recorded_olox_strict_unknowns_never_use_list_proof(fake_sdk, coid, request_id):
    raw = row(coid, "CANCELLED")
    client = _FakeClient({"today": listed(raw)})
    client.raises["detail"] = _ServerException(
        "TOO_MANY_REQUESTS", "Too many requests", 429, request_id
    )
    adapter = primary(client)
    result = await adapter.read_atr_resting_buy_after_cancel(
        _order(
            client_order_id=coid,
            symbol="OLOX",
            strategy_code="schwab_1m_v2",
            intent_type="cancel",
            metadata={
                "resting_entry_cancel": "true",
                "atr_reprice_identity": "webull_client_order_id",
            },
        )
    )
    assert result.outcome == "unknown"
    assert not result.can_replace
    assert client.calls == {"detail": 1}


@pytest.mark.asyncio
async def test_eod_requires_fresh_both_child_details_not_cached_list(fake_sdk):
    base = "schwab_1m_v2-AIFA-protect-owned"

    class Client(_FakeClient):
        def get_response(self, request):
            if request._kind == "detail" and request.values["client_order_id"] == base + "S":
                raise _ServerException("TOO_MANY_REQUESTS", "Too many requests", 429)
            return super().get_response(request)

    client = Client(
        {
            "detail": {"items": [{"order_status": "CANCELLED"}]},
            "today": listed(row(base + "T", "CANCELLED"), row(base + "S", "CANCELLED")),
        }
    )
    result = await primary(client).confirm_exit_pair_terminal(
        broker_account_name="live:orb", symbol="AIFA", base_client_order_id=base
    )
    assert result.outcome == "unanswerable"
    assert "today" not in client.calls


def test_two_pages_are_two_http_requests_and_later_target_is_found(fake_sdk):
    seen = []

    class Client(_FakeClient):
        def get_response(self, request):
            seen.append(request.values.get("last_client_order_id", ""))
            return _Resp(listed(row("a"), more=True) if len(seen) == 1 else listed(row()))

    adapter = primary(Client({}))
    assert read(adapter).event_type == "accepted"
    assert seen == ["", "a"]


def test_fair_continuation_and_per_page_age():
    clock = Clock()
    budget = QueryBudget(clock)
    reader = TodayOrderReader(budget, 15, clock)
    calls = []

    def page(cursor):
        calls.append(cursor)
        value = str(len(calls))
        return 200, listed(row(value), more=True)

    assert reader.read("ACC1", "later", page) is None
    assert len(calls) == 2
    assert reader.read("ACC2", "later", page) is None  # queues ACC2 behind used budget
    clock.now += 15
    assert reader.read("ACC1", "later", page) is None  # fair turn belongs to ACC2
    assert len(calls) == 2
    assert reader.read("ACC2", "later", page) is None
    assert len(calls) == 3
    clock.now += 2.01
    assert reader.read("ACC2", "3", page) is None  # page is stale, not freshly re-stamped
    clock.now += 12.99
    reader.read("ACC1", "later", page)
    assert calls[3] == "2"  # retains the ACC1 traversal, rather than restarting page one


@pytest.mark.parametrize("failure", ["page20", "malformed", "http", "cursor", "duplicate"])
def test_incomplete_scans_never_manufacture_absence(failure):
    clock = Clock()
    reader = TodayOrderReader(QueryBudget(clock), 15, clock)
    count = 0

    def page(cursor):
        nonlocal count
        count += 1
        if count == 2 and failure == "malformed":
            return 200, {"orders": [None]}
        if count == 2 and failure == "http":
            return 500, listed(row())
        if count == 2 and failure == "cursor":
            return 200, listed(row("1"), more=True)
        if count == 2 and failure == "duplicate":
            return 200, listed(row("1", "FILLED", qty="1", price="1.11"), more=True)
        return 200, listed(row(str(count)), more=True)

    if failure != "page20":
        with pytest.raises(ValueError):
            reader.read("ACC1", "missing", page)
    else:
        for _ in range(10):
            assert reader.read("ACC1", "missing", page) is None
            clock.now += 15
        assert count == 20
    scan = reader.scans["ACC1"]
    assert scan.failed and not scan.complete


def test_query_ceiling_counts_failures_and_reserves_strict_permit():
    clock = Clock()
    budget = QueryBudget(clock)
    budget.claim("detail", "ordinary")
    with pytest.raises(QueryBudgetUnavailable):
        budget.claim("detail", "other")
    budget.claim("detail", "RPG", strict=True)
    with pytest.raises(QueryBudgetUnavailable):
        budget.claim("detail", "EOD", strict=True)
    clock.now += 2
    budget.claim("detail", "other")
    budget.claim("list-today", "ACC1")
    budget.claim("list-today", "ACC2")
    with pytest.raises(QueryBudgetUnavailable):
        budget.claim("list-today", "ACC1")


def test_returned_http429_keeps_fresh_fill_without_completing_proof(fake_sdk):
    raw = row(status="FILLED", qty="1", price="1.11")

    class Client(_FakeClient):
        def get_response(self, request):
            if request._kind == "detail":
                return _HttpResp(429, {"error_code": "TOO_MANY_REQUESTS"})
            return super().get_response(request)

    report = read(primary(Client({"today": listed(raw)})))
    assert report.event_type == "filled"
    assert "_webull_terminal_proof" not in report.metadata


def test_aifa_actual_client_working_row_avoids_observed_detail_failure(fake_sdk):
    coid = "schwab_1m_v2-AIFA-open-621d8b9aeb95"
    client = _FakeClient({"today": listed(row(coid))})
    client.raises["detail"] = _ServerException("TOO_MANY_REQUESTS", "Too many requests", 429)
    assert read(primary(client), coid).event_type == "accepted"
    assert client.calls == {"today": 1}


@pytest.mark.parametrize("status", ["FILLED", "CANCELLED"])
def test_missing_cumulative_fill_and_conflicting_status_remain_unknown(fake_sdk, status):
    raw = row(status=status, qty="1" if status == "FILLED" else "0", price="1.11")
    raw["items"][0].pop("filled_qty")
    assert read(primary(_FakeClient({"today": listed(raw)}))) is None
    raw = row(status=status, qty="1" if status == "FILLED" else "0", price="1.11")
    raw["items"][0]["status"] = "SUBMITTED"
    assert read(primary(_FakeClient({"today": listed(raw)}))) is None


def test_terminal_versions_ignore_numeric_format_and_nonexecution_fields(fake_sdk, proof_store):
    raw = row(status="FILLED", qty="1.00", price="1.1100")
    detail = row(status="FILLED", qty="1", price="1.11")
    client = _FakeClient({"today": listed(raw), "detail": detail})
    assert read(primary(client, store=proof_store)).event_type == "filled"
    raw["updated_at"] = "new nonexecution value"
    assert read(primary(client, store=proof_store)).event_type == "filled"
    assert client.calls["detail"] == 1


def test_shared_adapters_and_aliases_coalesce_one_real_account_scan(fake_sdk):
    from project_mai_tai.broker_adapters.webull import WebullBrokerAdapter

    settings = Settings(
        _env_file=None,
        webull_app_key="webull429-coalescing-unit-key",
        oms_broker_sync_interval_seconds=15,
    )
    account = WebullAccountConfig("shared-real-account")
    client = _FakeClient({"today": listed(row())})
    first = WebullBrokerAdapter(settings, accounts_by_name={"live:orb": account}, client=client)
    second = WebullBrokerAdapter(settings, accounts_by_name={"alias": account}, client=client)
    assert first._today_order_detail_blocking(account, "owned") is not None
    assert second._today_order_detail_blocking(account, "owned") is not None
    assert client.calls == {"today": 1}
    assert first._query_budget is second._query_budget
    assert first._terminal_inflight is second._terminal_inflight


@pytest.mark.asyncio
async def test_fresh_both_child_eod_confirmation_can_release(fake_sdk):
    client = _FakeClient({"detail": {"items": [{"order_status": "CANCELLED"}]}})
    result = await primary(client).confirm_exit_pair_terminal(
        broker_account_name="live:orb", symbol="AIFA", base_client_order_id="owned"
    )
    assert result.outcome == "released"
    assert len(result.reports) == 2
    assert client.calls == {"detail": 2}


@pytest.mark.parametrize("key,value", [
    ("filledQty", "2"), ("filledQty", "bad"),
    ("avgFillPrice", "1.12"), ("avgFillPrice", "NaN"),
    ("lastFilledTime", "2026-10-06 15:12:00.000+0000"),
    ("orderStatus", "NOT_A_BROKER_STATUS"),
])
def test_conflicting_execution_aliases_are_unknown(fake_sdk, key, value):
    raw = row(status="FILLED", qty="1", price="1.11")
    raw["items"][0][key] = value
    client = _FakeClient({"today": listed(raw), "detail": raw})
    assert read(primary(client)) is None
    assert "detail" not in client.calls


def test_equivalent_execution_aliases_remain_usable(fake_sdk):
    raw = row(status="FILLED", qty="1", price="1.11")
    raw["items"][0].update(filledQty="1.00", avgFillPrice="1.1100",
                          lastFilledTime="2026-10-06T15:11:00+00:00", orderStatus="FILLED")
    assert read(primary(_FakeClient({"today": listed(raw), "detail": raw}))).event_type == "filled"


def test_unknown_status_alias_cannot_hide_behind_working_status(fake_sdk):
    raw = row()
    raw["items"][0]["orderStatus"] = "NOT_A_BROKER_STATUS"
    assert read(primary(_FakeClient({"today": listed(raw)}))) is None


def test_list_not_found_never_claims_readable_children(fake_sdk):
    clock = Clock()

    class Client(_FakeClient):
        def get_response(self, request):
            clock.now += 16  # A slow error must not become two readable absent children.
            raise _ServerException("ORDER_NOT_FOUND", "Order not found", 404)

    adapter = primary(Client({}), clock)
    with pytest.raises(_ServerException):
        adapter._exit_fill_blocking(adapter.accounts_by_name["live:orb"], "AIFA", "owned")


def test_native_decoder_missing_supported_fill_fields_is_unknown(fake_sdk):
    adapter = primary(_FakeClient({}))
    raw = row(status="FILLED", qty="1", price="1.11")
    raw["items"][0]["filled_quantity"] = raw["items"][0].pop("filled_qty")
    # Isolate the native decoder boundary: a sibling working read cannot erase
    # an unusable FILLED child, even if ordinary validation accepted an alias.
    adapter._ordinary_order_body = lambda *args, **kwargs: (200, raw)
    with pytest.raises(ValueError, match="UNKNOWN"):
        adapter._exit_fill_blocking(adapter.accounts_by_name["live:orb"], "AIFA", "owned")
