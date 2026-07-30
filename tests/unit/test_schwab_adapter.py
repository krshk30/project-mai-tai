from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from project_mai_tai.broker_adapters.schwab import SchwabBrokerAdapter
from project_mai_tai.broker_adapters.protocols import OrderRequest
from project_mai_tai.settings import Settings


@pytest.mark.asyncio
async def test_schwab_adapter_submits_and_polls_filled_market_order(monkeypatch) -> None:
    adapter = SchwabBrokerAdapter(
        Settings(
            oms_adapter="schwab",
            schwab_access_token="token-123",
            schwab_account_hash="hash-123",
        )
    )
    responses = iter(
        [
            (
                201,
                {
                    "Location": "https://api.schwabapi.com/trader/v1/accounts/hash-123/orders/987654321"
                },
                {},
            ),
            (
                200,
                {},
                {
                    "orderId": 987654321,
                    "status": "FILLED",
                    "quantity": "10",
                    "filledQuantity": "10",
                    "enteredTime": "2026-03-28T14:00:00Z",
                    "closeTime": "2026-03-28T14:00:02Z",
                    "orderActivityCollection": [
                        {
                            "executionLegs": [
                                {
                                    "price": "2.55",
                                    "quantity": "10",
                                    "time": "2026-03-28T14:00:02Z",
                                }
                            ]
                        }
                    ],
                },
            ),
        ]
    )

    async def fake_authorized_request_json(method: str, path: str, *, body=None):
        if method == "POST":
            assert path == "/trader/v1/accounts/hash-123/orders"
            assert body == {
                "session": "NORMAL",
                "duration": "DAY",
                "orderType": "MARKET",
                "orderStrategyType": "SINGLE",
                "orderLegCollection": [
                    {
                        "instruction": "BUY",
                        "quantity": 10.0,
                        "instrument": {"symbol": "UGRO", "assetType": "EQUITY"},
                    }
                ],
            }
        else:
            assert method == "GET"
            assert path == "/trader/v1/accounts/hash-123/orders/987654321"
        return next(responses)

    async def fake_sleep(_delay: float) -> None:
        return None

    monkeypatch.setattr(adapter, "_authorized_request_json", fake_authorized_request_json)
    monkeypatch.setattr(adapter, "_sleep", fake_sleep)

    reports = await adapter.submit_order(
        OrderRequest(
            client_order_id="macd_30s-UGRO-open-abc123",
            broker_account_name="paper:macd_30s",
            strategy_code="macd_30s",
            symbol="UGRO",
            side="buy",
            intent_type="open",
            quantity=Decimal("10"),
            reason="ENTRY_P1_MACD_CROSS",
            metadata={},
        )
    )

    assert [report.event_type for report in reports] == ["accepted", "filled"]
    assert reports[-1].broker_order_id == "987654321"
    assert reports[-1].fill_price == Decimal("2.55")
    assert reports[-1].filled_quantity == Decimal("10")


@pytest.mark.asyncio
async def test_schwab_adapter_cancels_order_by_broker_order_id(monkeypatch) -> None:
    adapter = SchwabBrokerAdapter(
        Settings(
            oms_adapter="schwab",
            schwab_access_token="token-123",
            schwab_account_hash="hash-123",
        )
    )
    responses = iter(
        [
            (200, {}, {}),
            (
                200,
                {},
                {
                    "orderId": 987654321,
                    "status": "CANCELED",
                    "quantity": "10",
                    "filledQuantity": "0",
                    "enteredTime": "2026-03-28T14:00:00Z",
                    "closeTime": "2026-03-28T14:00:03Z",
                },
            ),
        ]
    )

    async def fake_authorized_request_json(method: str, path: str, *, body=None):
        del body
        if method == "DELETE":
            assert path == "/trader/v1/accounts/hash-123/orders/987654321"
        else:
            assert method == "GET"
            assert path == "/trader/v1/accounts/hash-123/orders/987654321"
        return next(responses)

    monkeypatch.setattr(adapter, "_authorized_request_json", fake_authorized_request_json)

    reports = await adapter.submit_order(
        OrderRequest(
            client_order_id="macd_30s-UGRO-open-abc123",
            broker_account_name="paper:macd_30s",
            strategy_code="macd_30s",
            symbol="UGRO",
            side="buy",
            intent_type="cancel",
            quantity=Decimal("10"),
            reason="USER_CANCEL",
            metadata={"broker_order_id": "987654321"},
        )
    )

    assert len(reports) == 1
    assert reports[0].event_type == "cancelled"
    assert reports[0].broker_order_id == "987654321"


@pytest.mark.asyncio
async def test_schwab_adapter_returns_stop_guard_acceptance_immediately(monkeypatch) -> None:
    adapter = SchwabBrokerAdapter(
        Settings(
            oms_adapter="schwab",
            schwab_access_token="token-123",
            schwab_account_hash="hash-123",
        )
    )

    async def fake_authorized_request_json(method: str, path: str, *, body=None):
        assert method == "POST"
        assert path == "/trader/v1/accounts/hash-123/orders"
        assert body == {
            "session": "AM",
            "duration": "DAY",
            "orderType": "LIMIT",
            "price": 8.79,
            "orderStrategyType": "SINGLE",
            "orderLegCollection": [
                {
                    "instruction": "SELL",
                    "quantity": 10.0,
                    "instrument": {"symbol": "CLNN", "assetType": "EQUITY"},
                }
            ],
        }
        return (
            201,
            {
                "Location": "https://api.schwabapi.com/trader/v1/accounts/hash-123/orders/987654321"
            },
            {},
        )

    async def fail_wait_for_terminal_order(*args, **kwargs):
        raise AssertionError("stop-guard sell orders should not wait for terminal status")

    monkeypatch.setattr(adapter, "_authorized_request_json", fake_authorized_request_json)
    monkeypatch.setattr(adapter, "_wait_for_terminal_order", fail_wait_for_terminal_order)

    reports = await adapter.submit_order(
        OrderRequest(
            client_order_id="macd_30s-CLNN-close-abc123",
            broker_account_name="paper:macd_30s",
            strategy_code="macd_30s",
            symbol="CLNN",
            side="sell",
            intent_type="close",
            quantity=Decimal("10"),
            reason="HARD_STOP",
            metadata={
                "stop_guard": "true",
                "session": "AM",
                "order_type": "limit",
                "time_in_force": "day",
                "extended_hours": "true",
                "limit_price": "8.79",
            },
        )
    )

    assert [report.event_type for report in reports] == ["accepted"]
    assert reports[0].broker_order_id == "987654321"


@pytest.mark.asyncio
async def test_schwab_adapter_lists_account_positions(monkeypatch) -> None:
    adapter = SchwabBrokerAdapter(
        Settings(
            oms_adapter="schwab",
            schwab_access_token="token-123",
            schwab_account_hash="hash-123",
        )
    )

    async def fake_authorized_request_json(method: str, path: str, *, body=None):
        del body
        assert method == "GET"
        assert path == "/trader/v1/accounts/hash-123?fields=positions"
        return (
            200,
            {},
            {
                "securitiesAccount": {
                    "positions": [
                        {
                            "instrument": {"symbol": "UGRO", "assetType": "EQUITY"},
                            "longQuantity": "10",
                            "shortQuantity": "0",
                            "averagePrice": "2.55",
                            "marketValue": "25.50",
                            "tradeDate": "2026-03-28T14:00:00Z",
                        }
                    ]
                }
            },
        )

    monkeypatch.setattr(adapter, "_authorized_request_json", fake_authorized_request_json)

    positions = await adapter.list_account_positions("paper:macd_30s")

    assert len(positions) == 1
    assert positions[0].symbol == "UGRO"
    assert positions[0].quantity == Decimal("10")
    assert positions[0].average_price == Decimal("2.55")


@pytest.mark.asyncio
async def test_schwab_adapter_refreshes_and_persists_token_store(
    monkeypatch,
    tmp_path: Path,
) -> None:
    token_store_path = tmp_path / "test-schwab-token-store.json"
    token_store_path.write_text(
        json.dumps(
            {
                "refresh_token": "refresh-old",
                "expires_at": "2026-03-28T13:00:00+00:00",
            }
        ),
        encoding="utf-8",
    )
    adapter = SchwabBrokerAdapter(
        Settings(
            oms_adapter="schwab",
            schwab_client_id="client-id",
            schwab_client_secret="client-secret",
            schwab_token_store_path=str(token_store_path),
            schwab_account_hash="hash-123",
            # DECLARE the adapter-refresh-grant mode this test exercises (Rule 0), rather than
            # inheriting the settings.py default, which is now the safe `False` (pure reader).
            schwab_adapter_token_refresh_enabled=True,
        )
    )

    async def fake_token_request_json(*, form_data):
        assert form_data == {
            "grant_type": "refresh_token",
            "refresh_token": "refresh-old",
        }
        return (
            200,
            {},
            {
                "access_token": "access-new",
                "refresh_token": "refresh-new",
                "expires_in": 1800,
                "token_type": "Bearer",
                "scope": "readonly",
            },
        )

    monkeypatch.setattr(adapter, "_token_request_json", fake_token_request_json)

    access_token = await adapter._get_access_token()

    assert access_token == "access-new"
    persisted = json.loads(token_store_path.read_text(encoding="utf-8"))
    assert persisted["access_token"] == "access-new"
    assert persisted["refresh_token"] == "refresh-new"
    assert persisted["token_type"] == "Bearer"


# ------------------------------------------- OCO exit-fill capture (2026-07-27)
# Since the native OCO went live (2026-07-22) NO exit fill has been recorded: the exit executes on
# a broker-created child SELL leg the OMS never placed, so nothing books a fill for it. The
# operator's completed-trades table and P&L have been blank for five days
# (07-20: 3 sell fills · 07-21: 5 · 07-22: 1 · 07-23: 0 · 07-27: 0).

def _oco_adapter():
    return SchwabBrokerAdapter(
        Settings(oms_adapter="schwab", schwab_access_token="t", schwab_account_hash="hash-123")
    )


def _bracket_order(*, exit_status="FILLED", exit_price="5.92", close_time="2026-07-27T17:00:27+0000",
                   cancelled_sibling=True):
    """The live shape: TRIGGER(entry) -> OCO -> two SELL SINGLE children.

    ⛔ Callers must pass `entry_broker_order_id="9000"` (this order's id). Since 2026-07-29 the
    matcher walks ONLY our own entry's subtree and FAILS CLOSED without that proof — symbol-only
    matching is what let it book the operator's hand-placed trade.
    """
    sibling = {
        "orderId": 9002, "status": "CANCELED",
        "closeTime": close_time,
        "orderLegCollection": [{"instruction": "SELL",
                                "instrument": {"symbol": "FIEE"}}],
        # ⛔ THE TRAP: a CANCELED leg still carries an execution, priced 0.0
        "orderActivityCollection": [{"executionLegs": [{"quantity": 2.0, "price": 0.0}]}],
    }
    winner = {
        "orderId": 9001, "status": exit_status,
        "closeTime": close_time,
        "orderLegCollection": [{"instruction": "SELL", "instrument": {"symbol": "FIEE"}}],
        "orderActivityCollection": [
            {"executionLegs": [{"quantity": 2.0, "price": float(exit_price)}]}
        ],
    }
    children = [winner] + ([sibling] if cancelled_sibling else [])
    return {
        "orderId": 9000, "status": "FILLED",
        "closeTime": "2026-07-27T17:00:19+0000",
        "orderLegCollection": [{"instruction": "BUY", "instrument": {"symbol": "FIEE"}}],
        "orderActivityCollection": [{"executionLegs": [{"quantity": 2.0, "price": 5.84}]}],
        "childOrderStrategies": [
            {"orderId": 8999, "status": "FILLED", "orderStrategyType": "OCO",
             "orderLegCollection": [], "childOrderStrategies": children}
        ],
    }


def _patch_orders(monkeypatch, adapter, orders):
    async def fake(method, path, *, body=None):
        del body, method, path
        return (200, {}, orders)
    monkeypatch.setattr(adapter, "_authorized_request_json", fake)


@pytest.mark.asyncio
async def test_oco_exit_fill_reads_the_filled_child_leg(monkeypatch) -> None:
    """THE FIX: the exit price the completed-trades table needs, off the broker's own record."""
    adapter = _oco_adapter()
    _patch_orders(monkeypatch, adapter, [_bracket_order()])
    got = await adapter.fetch_oco_exit_fill(
        "paper:macd_30s", "FIEE", resolved_within_seconds=10**9,
        entry_broker_order_id="9000"
    )
    assert got is not None
    assert got["quantity"] == Decimal("2.0")
    assert got["price"] == Decimal("5.92")          # NOT the 0.0 cancelled sibling
    assert got["broker_order_id"] == "9001"


@pytest.mark.asyncio
async def test_a_cancelled_sibling_priced_zero_is_never_booked(monkeypatch) -> None:
    """⛔ THE DANGEROUS CASE. Every resolved bracket has a CANCELED sibling carrying
    `qty=2.0@0.0`. Booking it would write a $0 exit and report a -100% trade."""
    adapter = _oco_adapter()
    # only the cancelled, zero-priced leg exists -> there is NO real exit
    order = _bracket_order(exit_status="CANCELED", exit_price="0.0", cancelled_sibling=False)
    _patch_orders(monkeypatch, adapter, [order])
    assert await adapter.fetch_oco_exit_fill(
        "paper:macd_30s", "FIEE", resolved_within_seconds=10**9,
        entry_broker_order_id="9000"
    ) is None


@pytest.mark.asyncio
async def test_partial_executions_are_size_weighted(monkeypatch) -> None:
    """A market exit fills in slices (live FIEE: 15 slices). The exit price must be the
    size-weighted average, not the last slice."""
    adapter = _oco_adapter()
    order = _bracket_order()
    order["childOrderStrategies"][0]["childOrderStrategies"][0]["orderActivityCollection"] = [
        {"executionLegs": [{"quantity": 1.0, "price": 6.00}, {"quantity": 3.0, "price": 5.00}]}
    ]
    _patch_orders(monkeypatch, adapter, [order])
    got = await adapter.fetch_oco_exit_fill(
        "paper:macd_30s", "FIEE", resolved_within_seconds=10**9,
        entry_broker_order_id="9000"
    )
    assert got["quantity"] == Decimal("4.0")
    assert got["price"] == Decimal("5.25")          # (6*1 + 5*3)/4, not 5.00


@pytest.mark.asyncio
async def test_a_bracket_that_expired_without_filling_yields_nothing(monkeypatch) -> None:
    """No filled SELL = the position is STILL HELD; returning an exit would close a live row."""
    adapter = _oco_adapter()
    order = _bracket_order(exit_status="CANCELED", exit_price="0.0")
    _patch_orders(monkeypatch, adapter, [order])
    assert await adapter.fetch_oco_exit_fill(
        "paper:macd_30s", "FIEE", resolved_within_seconds=10**9,
        entry_broker_order_id="9000"
    ) is None


@pytest.mark.asyncio
async def test_an_unknown_account_returns_none_not_a_false_negative(monkeypatch) -> None:
    """Guard on the tests themselves: an unconfigured account short-circuits to None BEFORE the
    order tree is read. Three of these tests originally 'passed' for exactly that reason."""
    adapter = _oco_adapter()
    _patch_orders(monkeypatch, adapter, [_bracket_order()])
    assert await adapter.fetch_oco_exit_fill(
        "live:does-not-exist", "FIEE", resolved_within_seconds=10**9
    ) is None
    # ...while the SAME payload on a known account DOES yield the exit
    assert await adapter.fetch_oco_exit_fill(
        "paper:macd_30s", "FIEE", resolved_within_seconds=10**9,
        entry_broker_order_id="9000"
    ) is not None


@pytest.mark.asyncio
async def test_a_stale_exit_outside_the_recency_window_is_ignored(monkeypatch) -> None:
    """Guards against pairing today's entry with an EARLIER bracket's exit on the same symbol."""
    adapter = _oco_adapter()
    _patch_orders(monkeypatch, adapter, [_bracket_order(close_time="2020-01-01T00:00:00+0000")])
    assert await adapter.fetch_oco_exit_fill(
        "paper:macd_30s", "FIEE", resolved_within_seconds=60
    ) is None


# The two filters below are DEFENCE IN DEPTH and mask each other: a cancelled sibling is excluded
# by BOTH status and price, so a test using the realistic payload cannot tell which one is doing
# the work. These two isolate each filter so neither can silently rot.

@pytest.mark.asyncio
async def test_status_filter_alone_rejects_a_cancelled_leg_with_a_REAL_price(monkeypatch) -> None:
    """Only a CANCELED SELL exists, and it carries a plausible non-zero price. Status must
    exclude it on its own -- otherwise a cancelled bracket books a fake exit and closes a
    position that is still HELD."""
    adapter = _oco_adapter()
    order = _bracket_order(exit_status="CANCELED", exit_price="5.00", cancelled_sibling=False)
    _patch_orders(monkeypatch, adapter, [order])
    assert await adapter.fetch_oco_exit_fill(
        "paper:macd_30s", "FIEE", resolved_within_seconds=10**9,
        entry_broker_order_id="9000"
    ) is None


@pytest.mark.asyncio
async def test_price_filter_alone_rejects_a_FILLED_leg_priced_zero(monkeypatch) -> None:
    """A FILLED SELL whose only execution is priced 0.0. Price must exclude it on its own --
    booking it would write a $0 exit and report the trade as -100%."""
    adapter = _oco_adapter()
    order = _bracket_order(exit_status="FILLED", exit_price="0.0", cancelled_sibling=False)
    _patch_orders(monkeypatch, adapter, [order])
    assert await adapter.fetch_oco_exit_fill(
        "paper:macd_30s", "FIEE", resolved_within_seconds=10**9,
        entry_broker_order_id="9000"
    ) is None
