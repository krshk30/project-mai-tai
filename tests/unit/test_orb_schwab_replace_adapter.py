from datetime import UTC, datetime
from decimal import Decimal

import pytest

from project_mai_tai.broker_adapters.protocols import OrderRequest
from project_mai_tai.broker_adapters.schwab import SchwabBrokerAdapter
from project_mai_tai.orb_schwab_order_route import build_orb_schwab_reprice_intent
from project_mai_tai.settings import Settings


def _setup():
    settings = Settings(
        oms_adapter="schwab",
        schwab_access_token="test-token",
        schwab_account_hash="test-hash",
        strategy_schwab_1m_v2_account_name="live:schwab_1m_v2",
        strategy_schwab_1m_v2_broker_provider="schwab",
        strategy_schwab_1m_v2_go_live_enabled=True,
        orb_live_schwab_orders_enabled=True,
    )
    adapter = SchwabBrokerAdapter(settings)
    event = build_orb_schwab_reprice_intent(settings, "CLRO", Decimal("5.60"))
    request = OrderRequest(
        client_order_id="orb_schwab-CLRO-open-first",
        broker_account_name="live:schwab_1m_v2",
        strategy_code="orb_schwab",
        symbol="CLRO",
        side="buy",
        intent_type="open",
        quantity=Decimal("2"),
        reason="reprice",
        metadata=event.payload.metadata,
    )
    return adapter, request


@pytest.mark.asyncio
async def test_replace_uses_one_put_and_confirms_entire_bracket(monkeypatch) -> None:
    adapter, request = _setup()
    desired = adapter._build_bracket_payload(request)
    old = {**desired, "stopPrice": 5.50, "price": 5.52, "status": "WORKING", "quantity": 2}
    new = {**desired, "status": "WORKING", "quantity": 2, "filledQuantity": 0}
    calls = []

    async def fetch(_account, order_id):
        return old if order_id == "old" else new

    async def request_json(method, path, body=None):
        calls.append((method, path, body))
        return 201, {"Location": "/trader/v1/accounts/test-hash/orders/new"}, {}

    monkeypatch.setattr(adapter, "_fetch_order", fetch)
    monkeypatch.setattr(adapter, "_authorized_request_json", request_json)
    report = await adapter.replace_bracket_order(request, "old")
    assert report is not None and report.event_type == "accepted"
    assert report.broker_order_id == "new"
    assert len(calls) == 1 and calls[0][0] == "PUT"
    assert calls[0][2] == desired


@pytest.mark.asyncio
async def test_unknown_replace_never_claims_success(monkeypatch) -> None:
    adapter, request = _setup()
    old = {
        **adapter._build_bracket_payload(request),
        "status": "WORKING",
        "quantity": 2,
        "filledQuantity": 0,
    }

    async def fetch(_account, order_id):
        return old if order_id == "old" else None

    async def request_json(_method, _path, body=None):
        return 201, {"Location": "/trader/v1/accounts/test-hash/orders/new"}, {}

    monkeypatch.setattr(adapter, "_fetch_order", fetch)
    monkeypatch.setattr(adapter, "_authorized_request_json", request_json)
    report = await adapter.replace_bracket_order(request, "old")
    assert report.broker_order_id == "new"
    assert report.metadata["orb_replace_confirmation"] == "unknown"
    assert report.filled_quantity == 0 and report.broker_fill_id is None


def test_orb_fill_time_comes_from_execution_legs_not_parent_entered_time():
    adapter, request = _setup()
    order = {"quantity": 2, "filledQuantity": 2, "enteredTime": "2026-09-29T13:28:00Z",
             "orderActivityCollection": [{"executionLegs": [
                 {"time": "2026-09-29T13:30:10Z", "quantity": 1, "price": 5.6},
                 {"time": "2026-09-29T13:30:11Z", "quantity": 1, "price": 5.6},
             ]}]}
    report = adapter._execution_report_from_order(request=request, order=order,
                                                  event_type="filled", broker_order_id="parent")
    assert report.metadata["orb_entry_fill_time_source"] == "execution_leg"
    assert report.metadata["orb_entry_first_fill_at"] == "2026-09-29T13:30:10+00:00"
    assert report.reported_at == datetime(2026, 9, 29, 13, 28, tzinfo=UTC)  # other consumers unchanged


def test_orb_missing_execution_time_is_unknown_not_order_entered_time():
    adapter, request = _setup()
    report = adapter._execution_report_from_order(
        request=request, order={"quantity": 2, "filledQuantity": 2, "enteredTime": "2026-09-29T13:28:00Z"},
        event_type="filled", broker_order_id="parent")
    assert report.metadata["orb_entry_fill_time_source"] == "UNKNOWN"
    assert report.metadata["orb_entry_first_fill_at"] == ""


def test_other_strategy_fill_metadata_is_unchanged():
    from dataclasses import replace

    adapter, request = _setup()
    request = replace(request, strategy_code="schwab_1m_v2")
    report = adapter._execution_report_from_order(request=request,
        order={"quantity": 2, "filledQuantity": 2}, event_type="filled", broker_order_id="parent")
    assert report.metadata == request.metadata


@pytest.mark.asyncio
@pytest.mark.parametrize("problem", ["read_raises", "partial", "wrong_price", "wrong_shape"])
async def test_accepted_put_retains_new_id_on_every_confirmation_problem(monkeypatch, problem):
    adapter, request = _setup()
    old = {**adapter._build_bracket_payload(request), "status": "WORKING", "quantity": 2, "filledQuantity": 0}
    async def fetch(_account, order_id):
        if order_id == "old":
            return old
        if problem == "read_raises":
            raise RuntimeError("GET unavailable after accepted PUT")
        if problem == "partial":
            return {**old, "filledQuantity": 1}
        if problem == "wrong_price":
            return {**old, "price": "100"}
        return {**old, "childOrderStrategies": []}
    async def put(*_args, **_kwargs):
        return 201, {"Location": "/trader/v1/accounts/test-hash/orders/new"}, {}
    monkeypatch.setattr(adapter, "_fetch_order", fetch)
    monkeypatch.setattr(adapter, "_authorized_request_json", put)
    report = await adapter.replace_bracket_order(request, "old")
    assert report.broker_order_id == "new"
    assert report.metadata["orb_replace_confirmation"] == "unknown"
    assert report.filled_quantity == 0 and report.broker_fill_id is None
