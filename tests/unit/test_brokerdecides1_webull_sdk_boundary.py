"""Actual collector + pinned SDK request, PRIVATE ApiRequest/client/response fixtures.

No SDK HTTP client, real account or provider is constructed. Native response
shapes/positions SDK source remain unproved. Worker authors, main alone executes.
"""
from copy import deepcopy
import hashlib
import importlib.util
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace

import pytest

from project_mai_tai import brokerdecides1_collect as collector

SDK_FIXTURE = Path(__file__).parents[1] / "fixtures" / "l12_sdk_open_request_2_0_11.py"


class PrivateApiRequest:
    def __init__(self, path, *, version, method, query_params):
        self.path, self.version, self.method = path, version, method
        self.query = dict(query_params)
        self.connect_timeout = self.read_timeout = None

    def add_query_param(self, key, value):
        self.query[key] = value

    def set_connect_timeout(self, value):
        self.connect_timeout = value

    def set_read_timeout(self, value):
        self.read_timeout = value


class PrivatePositionsRequest(PrivateApiRequest):
    """Protocol fixture ONLY: installed positions SDK source is not in packet."""
    def __init__(self):
        super().__init__("positions-fixture-not-a-provider-path", version="private", method="GET", query_params={})

    def set_account_id(self, value):
        self.add_query_param("account_id", value)

    def set_page_size(self, value):
        self.add_query_param("page_size", value)

    def set_last_instrument_id(self, value):
        self.add_query_param("last_instrument_id", value)


def private_sdk(monkeypatch):
    assert hashlib.sha256(SDK_FIXTURE.read_bytes()).hexdigest() == collector.SDK_OPEN_REQUEST_SHA
    for name in ("webull", "webull.core", "webull.trade", "webull.trade.request"):
        package = ModuleType(name)
        package.__path__ = []
        monkeypatch.setitem(sys.modules, name, package)
    base = ModuleType("webull.core.request")
    base.ApiRequest = PrivateApiRequest
    monkeypatch.setitem(sys.modules, base.__name__, base)
    name = "webull.trade.request.get_open_orders_request"
    spec = importlib.util.spec_from_file_location(name, SDK_FIXTURE)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, module)
    spec.loader.exec_module(module)
    positions = ModuleType("webull.trade.request.get_account_positions_request")
    positions.AccountPositionsRequest = PrivatePositionsRequest
    monkeypatch.setitem(sys.modules, positions.__name__, positions)
    return module.OpenOrdersListRequest, positions


def adapter_fixture(monkeypatch, *, fault="none", pagination=False):
    open_request, positions_module = private_sdk(monkeypatch)
    requests, permits = [], []
    stamps = iter((1000, 1100))
    monkeypatch.setattr(collector, "stamp", lambda: next(stamps))

    class Budget:
        def claim_background(self, endpoint, account):
            permits.append((endpoint, account))
            if fault == "budget":
                raise RuntimeError("private no permit")

    class Client:
        def get_response(self, request):
            requests.append(request)
            is_order = isinstance(request, open_request)
            cursor = request.query.get("last_client_order_id" if is_order else "last_instrument_id")
            if is_order:
                body = {"hasNext": False, "pageSize": 0, "orders": []}
                if fault == "no_flag":
                    body.pop("hasNext")
                elif fault == "foreign":
                    body["account_id"] = "foreign"
                elif fault == "cursor_cycle":
                    body["hasNext"] = True
                    body["orders"] = [{"client_order_id": "same"}]
                elif fault == "body_none":
                    body = None
            else:
                body = {"account_id": "private-wb", "has_next": pagination and not cursor,
                    "holdings": [{"symbol": "SHORT", "quantity": -1, "instrument_id": "pos-two"}]
                    if cursor else [{"symbol": "HOLD", "quantity": 2, "instrument_id": "pos-one"}]}
                if fault == "position_account":
                    body["account_id"] = "foreign"
                elif fault == "no_qty":
                    body["holdings"][0].pop("quantity")
                elif fault == "no_positions":
                    body.pop("holdings")
            response = SimpleNamespace(body=deepcopy(body))
            if fault != "no_status":
                response.status_code = 503 if fault == "status" else 200
            return response

    client = Client()
    adapter = SimpleNamespace(accounts_by_name={"live:webull": SimpleNamespace(account_id="private-wb")},
        _query_budget=Budget(), _get_client=lambda: client, _body=lambda response: response.body)
    return adapter, requests, permits, positions_module


def test_actual_collector_uses_pinned_open_sdk_empty_envelope_and_private_positions_pages(monkeypatch):
    adapter, requests, permits, positions = adapter_fixture(monkeypatch, pagination=True)
    book = collector._webull_blocking(adapter, "live:webull")
    assert book.complete and not book.working
    assert book.positions == frozenset({"HOLD", "SHORT"})
    assert book.wire_started_ms == 1000 and book.wire_completed_ms == 1100
    assert len(requests) == 3
    first = requests[0]
    assert first.path == "/trade/orders/list-open"
    assert first.method == "GET" and first.version == "v2"
    assert first.query == {"account_id": "private-wb", "page_size": 100}
    assert requests[1].query == {"account_id": "private-wb", "page_size": 50}
    assert requests[2].query == {**requests[1].query, "last_instrument_id": "pos-one"}
    assert all(request.connect_timeout == request.read_timeout == 2 for request in requests)
    assert permits == [("list-open", "private-wb")] + [("positions", "private-wb")] * 2


def test_pinned_sdk_cursor_setter_contract_does_not_claim_nonempty_collection(monkeypatch):
    request_type, positions = private_sdk(monkeypatch)
    request = request_type()
    request.set_account_id("private-wb")
    request.set_page_size(100)
    request.set_last_client_order_id("open-cursor")
    assert request.path == "/trade/orders/list-open"
    assert request.query == {"account_id": "private-wb", "page_size": 100,
                             "last_client_order_id": "open-cursor"}


@pytest.mark.parametrize("fault", [
    "status", "no_status", "no_flag", "foreign", "cursor_cycle", "body_none",
    "position_account", "no_qty", "no_positions",
])
def test_actual_collector_never_converts_protocol_failure_to_empty_book(monkeypatch, fault):
    adapter, requests, permits, positions = adapter_fixture(monkeypatch, fault=fault)
    with pytest.raises(ValueError):
        collector._webull_blocking(adapter, "live:webull")
    assert len(requests) <= 2  # No HTTP retry, alternate endpoint or today-only fallback.


def test_actual_collector_respects_existing_query_budget_without_http(monkeypatch):
    adapter, requests, permits, positions = adapter_fixture(monkeypatch, fault="budget")
    with pytest.raises(RuntimeError, match="no permit"):
        collector._webull_blocking(adapter, "live:webull")
    assert requests == [] and permits == [("list-open", "private-wb")]


def test_missing_positions_sdk_cursor_support_is_unknown_not_first_page_success(monkeypatch):
    adapter, requests, permits, positions = adapter_fixture(monkeypatch, pagination=True)

    class MissingCursor(PrivatePositionsRequest):
        set_last_instrument_id = None

    positions.AccountPositionsRequest = MissingCursor
    with pytest.raises(TypeError):
        collector._webull_blocking(adapter, "live:webull")
    assert len(requests) == 2  # No guessed replacement cursor or repeated first positions page.


@pytest.mark.asyncio
async def test_async_collector_error_has_no_raw_private_diagnostic(monkeypatch):
    def unreadable(*args):
        raise ValueError("SYNTHETIC_PRIVATE_DIAGNOSTIC")

    monkeypatch.setattr(collector, "_webull_blocking", unreadable)
    with pytest.raises(ValueError, match="^Webull_native_collection_UNKNOWN$") as caught:
        await collector.read_webull(SimpleNamespace(), "live:webull")
    assert "SYNTHETIC_PRIVATE_DIAGNOSTIC" not in str(caught.value)


@pytest.mark.parametrize("shape", ["flat", "nonempty_pages", "null_echo", "foreign_echo",
    "conflicting_echo", "true_empty", "missing_flag", "nonbool_flag", "conflicting_flags",
    "missing_holdings", "null_holdings", "error_envelope", "http_error"])
def test_positions_omitted_echo_uses_own_request_without_body_injection(monkeypatch, shape):
    # False/true below are CONTROLLED variants: diagnostic retained only flag TYPE.
    adapter, requests, permits, positions = adapter_fixture(monkeypatch, pagination=True)
    client = adapter._get_client()
    original = client.get_response
    bodies = []

    def response(request):
        result = original(request)
        if not isinstance(request, positions.AccountPositionsRequest):
            return result
        body = result.body
        body.pop("account_id")
        if shape != "nonempty_pages":
            body.update(holdings=[], has_next=False)
        if shape == "null_echo":
            body["account_id"] = None
        elif shape == "foreign_echo":
            body["account_id"] = "foreign"
        elif shape == "conflicting_echo":
            body.update(account_id="private-wb", accountId="foreign")
        elif shape == "true_empty":
            body["has_next"] = True
        elif shape == "missing_flag":
            body.pop("has_next")
        elif shape == "nonbool_flag":
            body["has_next"] = 0
        elif shape == "conflicting_flags":
            body["hasNext"] = True
        elif shape == "missing_holdings":
            body.pop("holdings")
        elif shape == "null_holdings":
            body["holdings"] = None
        elif shape == "error_envelope":
            body["error"] = "CONTROLLED error"
        elif shape == "http_error":
            result.status_code = 503
        bodies.append((body, deepcopy(body)))
        return result

    client.get_response = response
    if shape not in ("flat", "nonempty_pages"):
        with pytest.raises(ValueError):
            collector._webull_blocking(adapter, "live:webull")
        assert len(requests) == 2
    else:
        book = collector._webull_blocking(adapter, "live:webull")
        assert book.complete and book.working == frozenset()
        assert book.positions == (frozenset({"HOLD", "SHORT"})
            if shape == "nonempty_pages" else frozenset())
        assert len(requests) == (3 if shape == "nonempty_pages" else 2)
        assert all(request.query["account_id"] == "private-wb" for request in requests)
        assert book.wire_started_ms == 1000 and book.wire_completed_ms == 1100
    assert all(body == before for body, before in bodies)
