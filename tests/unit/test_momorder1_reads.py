"""MOMORDER1 bounded detail reads against the real Webull mapper/query budget."""

from copy import deepcopy
from dataclasses import replace
from decimal import Decimal as D

import pytest

from project_mai_tai.broker_adapters.webull_order_reads import QueryBudget, QueryBudgetUnavailable
from project_mai_tai.momentum_live.admission import STRATEGY_CODE
from tests.unit.test_webull_adapter import _FakeClient, _ServerException, _order
from tests.unit.test_webull_adapter import fake_sdk as fake_sdk
from tests.unit.test_webull_list_primary import Clock, listed, primary


def request(*, fresh=True, side="buy"):
    return _order(client_order_id="ml1-AIXI-e-f9e48766f9ad581db014", symbol="AIXI",
        strategy_code=STRATEGY_CODE, side=side, quantity=D(31),
        intent_type="open" if side == "buy" else "close", reason="MOMLIVE1_DETECTED",
        metadata={"episode_id": "f9e48766-f9ad-581d-b014-7068e23d5c83",
            "momlive_strategy": STRATEGY_CODE, "momlive_one_shot": "true",
            "broker_order_id": "EJJSLL5VJ047EH7KV7K224SEV9", "limit_price": "3.12",
            **({"momlive_fresh_order_read": "true"} if fresh else {})})


def body(req, *, status="FILLED", filled="31", price="3.11"):
    return {"account_id": "ACC1", "client_order_id": req.client_order_id,
        "order_id": req.metadata["broker_order_id"],
        "items": [{"symbol": req.symbol, "side": req.side.upper(), "qty": str(req.quantity),
            "order_status": status, "filled_qty": filled, "filled_price": price,
            "last_filled_time": "2026-10-20 09:10:42.128+0000"}]}


def read(adapter, req):
    return adapter._fetch_order_blocking(adapter.accounts_by_name["live:orb"], req)


def test_stale_15_second_list_and_ordinary_starvation_cannot_hide_filled_entry(fake_sdk):
    clock, req = Clock(), request()
    client = _FakeClient({"today": listed(body(req, status="SUBMITTED", filled="0")), "detail": body(req)})
    adapter = primary(client, clock)
    assert read(adapter, request(fresh=False)).event_type == "accepted"
    clock.now += 2.1  # List evidence expired, but its configured 15-second cycle has not.
    adapter._query_budget.claim("detail", "ATR-ordinary-reader")
    result = read(adapter, req)
    assert result.event_type == "filled" and result.filled_quantity == 31
    assert result.fill_price == D("3.11")
    assert client.calls == {"today": 1, "detail": 1}
    assert len(adapter._query_budget.attempts["detail"]) == 2


def test_exhausted_strict_budget_is_unknown_without_cached_fill_or_extra_http(fake_sdk):
    clock, req = Clock(), request()
    client = _FakeClient({"today": listed(body(req)), "detail": body(req)})
    adapter = primary(client, clock)
    adapter._query_budget.claim("detail", "ATR-terminal", strict=True)
    adapter._query_budget.claim("detail", "ORB-terminal", strict=True)
    for _ in range(100):
        assert read(adapter, req) is None
    assert client.calls == {}
    assert len(adapter._query_budget.attempts["detail"]) == 2


@pytest.mark.parametrize("defect", ["account", "coid", "native_id", "symbol", "side", "quantity", "time", "status"])
def test_fresh_detail_requires_exact_owned_identity_and_usable_execution(fake_sdk, defect):
    req = request()
    raw = body(req)
    if defect in {"account", "coid", "native_id"}:
        raw[{"account": "account_id", "coid": "client_order_id", "native_id": "order_id"}[defect]] = "FOREIGN"
    else:
        key, value = {"symbol": ("symbol", "FOREIGN"), "side": ("side", "SELL"),
            "quantity": ("filled_qty", "32"), "time": ("last_filled_time", "unreadable"),
            "status": ("order_status", "UNREADABLE")}[defect]
        raw["items"][0][key] = value
    client = _FakeClient({"detail": raw, "today": listed(body(req))})
    assert read(primary(client), req) is None
    assert client.calls == {"detail": 1}


@pytest.mark.asyncio
async def test_429_http_attempts_count_toward_existing_two_per_two_second_ceiling(fake_sdk):
    clock, req = Clock(), request()
    client = _FakeClient({})
    client.raises["detail"] = _ServerException("TOO_MANY_REQUESTS", "controlled 429", 429)
    adapter = primary(client, clock)
    for _ in range(100):
        assert await adapter.fetch_order_update(req) is None
    assert client.calls == {"detail": 2}
    assert len(adapter._query_budget.attempts["detail"]) == 2
    clock.now += 2
    assert await adapter.fetch_order_update(req) is None
    assert client.calls == {"detail": 3}


def test_momentum_and_atr_orb_consumers_share_the_same_strict_ceiling(fake_sdk):
    clock, req = Clock(), request()
    budget = QueryBudget(clock)
    first_client, second_client = _FakeClient({"detail": body(req)}), _FakeClient({"detail": body(req)})
    first, second = primary(first_client, clock), primary(second_client, clock)
    first._query_budget = second._query_budget = budget
    budget.claim("detail", "ATR-terminal", strict=True)
    assert read(first, req).event_type == "filled"
    assert read(second, req) is None
    with pytest.raises(QueryBudgetUnavailable):
        budget.claim("detail", "ORB-terminal", strict=True)
    assert first_client.calls == {"detail": 1} and second_client.calls == {}
    clock.now += 2
    budget.claim("detail", "ORB-terminal", strict=True)
    assert read(second, req).event_type == "filled"
    with pytest.raises(QueryBudgetUnavailable):
        budget.claim("detail", "ATR-terminal", strict=True)


@pytest.mark.parametrize("strategy", ["schwab_1m_v2", "orb_schwab"])
def test_unmarked_atr_orb_ordinary_list_reads_remain_list_primary(fake_sdk, strategy):
    req = replace(request(fresh=False), strategy_code=strategy, metadata={"limit_price": "3.12"})
    raw = body(request(), status="SUBMITTED", filled="0")
    client = _FakeClient({"today": listed(raw), "detail": body(request())})
    adapter = primary(client)
    assert read(adapter, req).event_type == "accepted"
    assert client.calls == {"today": 1}


@pytest.mark.parametrize("filled,event", [("0", "cancelled"), ("7", "cancelled"), ("31", "filled")])
def test_fresh_cancel_read_keeps_partial_cancellation_distinct_from_full_fill(fake_sdk, filled, event):
    req = request()
    raw = body(req, status="FILLED" if event == "filled" else "CANCELLED", filled=filled)
    client = _FakeClient({"detail": deepcopy(raw)})
    result = read(primary(client), req)
    assert result.event_type == event and result.filled_quantity == D(filled)
    assert result.origin == "broker" and result.client_order_id == req.client_order_id
    assert client.calls == {"detail": 1}
