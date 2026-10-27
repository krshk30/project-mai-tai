"""Native-shaped LOCAL fixtures, not proof of a production endpoint's coverage."""
from copy import deepcopy

import pytest

from project_mai_tai.brokerdecides1_native import schwab_book, webull_book

SCOPE = "all_native_working_including_previous_sessions"


def schwab(**changes):
    args = dict(account="live:schwab", native_account="123",
        account_body={"securitiesAccount": {"accountNumber": "123", "positions": []}},
        orders=[], wire_started_ms=1000, wire_completed_ms=1100,
        scope=SCOPE, complete=True)
    args.update(changes)
    return schwab_book(**args)


def order(status="WORKING", order_id="order-one"):
    return {"accountNumber": "123", "orderId": order_id, "status": status,
        "orderLegCollection": [{"instruction": "SELL", "instrument": {"symbol": "WORX"}}]}


def webull(**changes):
    args = dict(account="live:webull", native_account="wb123",
        position_pages=[{"account_id": "wb123", "holdings": [], "has_next": False}],
        order_pages=[{"orders": [], "hasNext": False, "pageSize": 0}],
        wire_started_ms=1000, wire_completed_ms=1100, scope=SCOPE, complete=True,
        order_request_account="wb123")
    args.update(changes)
    return webull_book(**args)


@pytest.mark.parametrize("reader", [schwab, webull])
@pytest.mark.parametrize("scope", ["today_orders", "local_oms_orders", "", None])
def test_today_or_local_rows_never_prove_all_working(reader, scope):
    with pytest.raises(ValueError, match="coverage_UNKNOWN"):
        reader(scope=scope)


@pytest.mark.parametrize("reader", [schwab, webull])
def test_positive_complete_contract_flat_shape(reader):
    value = reader()
    assert value.complete and not value.working and not value.positions


def test_schwab_gross_long_and_short_cannot_net_to_flat():
    body = {"securitiesAccount": {"accountNumber": "123", "positions": [
        {"instrument": {"symbol": "WORX"}, "longQuantity": 2, "shortQuantity": 2}]}}
    assert schwab(account_body=body).positions == frozenset({"WORX"})


@pytest.mark.parametrize("status", ["WORKING", "PENDING_CANCEL", "UNKNOWN_VENDOR_STATUS"])
def test_any_unproven_terminal_status_and_sell_blocks(status):
    assert schwab(orders=[order(status)]).working == frozenset({"WORX"})


def test_filled_parent_does_not_hide_working_child():
    parent = order("FILLED")
    parent["childOrderStrategies"] = [order("WORKING", "child")]
    assert schwab(orders=[parent]).working == frozenset({"WORX"})


@pytest.mark.parametrize("change", [
    {"accountNumber": "foreign"}, {"orderId": None},
    {"orderLegCollection": []}, {"status": ""},
])
def test_schwab_malformed_order_is_unknown_not_dropped(change):
    row = order()
    row.update(change)
    with pytest.raises(ValueError):
        schwab(orders=[row])


def test_schwab_missing_positions_is_not_empty():
    with pytest.raises(ValueError):
        schwab(account_body={"securitiesAccount": {"accountNumber": "123"}})


def test_duplicate_schwab_order_is_unknown():
    with pytest.raises(ValueError):
        schwab(orders=[order(), order()])


@pytest.mark.parametrize("changes", [
    {"has_next": True}, {"has_next": "false"}, {"account_id": "foreign"},
    {"hasNext": True}, {"orders": None},
])
def test_webull_incomplete_foreign_or_conflicting_page_holds(changes):
    page = {"account_id": "wb123", "orders": [], "has_next": False}
    page.update(changes)
    with pytest.raises(ValueError):
        webull(order_pages=[page])


def test_webull_missing_continuation_never_implies_final():
    with pytest.raises(ValueError):
        webull(order_pages=[{"account_id": "wb123", "orders": []}])


def test_webull_old_manual_working_order_without_our_client_id_blocks():
    page = {"account_id": "wb123", "has_next": False, "orders": [
        {"order_id": "native-manual", "status": "WORKING", "items": [{"symbol": "WORX"}]}]}
    assert webull(order_pages=[page], order_request_account=None).working == frozenset({"WORX"})
    assert not webull(order_pages=[page], order_request_account=None).complete
    assert webull(order_pages=[page], order_request_account=None).reason == "Webull_nonempty_item_model_UNKNOWN"
    duplicate = deepcopy(page)
    duplicate["orders"] *= 2
    with pytest.raises(ValueError):
        webull(order_pages=[duplicate], order_request_account=None)


def test_webull_short_manual_position_blocks():
    page = {"account_id": "wb123", "holdings": [{"symbol": "WORX", "quantity": -3}],
        "has_next": False}
    assert webull(position_pages=[page]).positions == frozenset({"WORX"})
