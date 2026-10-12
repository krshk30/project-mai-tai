from copy import deepcopy

import pytest

from project_mai_tai.services.control_plane import _collect_completed_position_rows
from project_mai_tai.trade_episodes import collect_completed_trade_cycles


def _gixi():
    common = {"symbol": "GIXI", "quantity": "2", "strategy_code": "orb_schwab",
              "broker_account_name": "live:schwab_1m_v2"}
    orders = [
        {**common, "order_id": "1238915b-8746-4bd8-ab60-7b05b2f1c266", "side": "buy",
         "intent_type": "open", "status": "filled", "price": "",
         "reason": "ORB_FIXED_HIGH_SCHWAB_STOP_LIMIT",
         "updated_at": "2026-10-12 09:31:07 AM ET"},
        {**common, "order_id": "93c26df6-71db-4df6-addc-4b9f562f76d6", "side": "sell",
         "intent_type": "close", "status": "filled", "price": "",
         "reason": "BREAK_BAR_BODY_UNDER_45_PCT",
         "updated_at": "2026-10-12 09:31:07 AM ET"},
    ]
    fills = [
        {**common, "order_id": orders[0]["order_id"], "side": "buy", "price": "6.33",
         "filled_at": "2026-10-12 09:30:21 AM ET"},
        {**common, "order_id": orders[1]["order_id"], "side": "sell", "price": "6.2111",
         "filled_at": "2026-10-12 09:31:06 AM ET"},
    ]
    return orders, fills


def _cycles(orders, fills):
    return collect_completed_trade_cycles(
        strategy_code="orb_schwab", broker_account_name="live:schwab_1m_v2",
        recent_orders=orders, recent_fills=fills, closed_today=[],
    )


def test_recorded_gixi_delayed_order_updates_render_one_priced_position():
    orders, fills = _gixi()
    original = deepcopy((orders, fills))
    cycles = _cycles(orders, fills)
    assert len(cycles) == 1
    cycle = cycles[0]
    assert (cycle.quantity, cycle.entry_price, cycle.exit_price) == (2, 6.33, 6.2111)
    assert cycle.pnl == pytest.approx(-0.2378)
    assert cycle.summary == "Break Bar Body Under 45 Pct"
    assert (orders, fills) == original
    bot = {"strategy_code": "orb_schwab", "account_name": "live:schwab_1m_v2",
           "recent_orders": orders, "recent_fills": fills,
           "closed_today": [{"ticker": cycle.symbol, "quantity": cycle.quantity,
                             "entry_time": cycle.entry_time, "exit_time": cycle.exit_time,
                             "entry_price": cycle.entry_price, "exit_price": cycle.exit_price,
                             "exit_summary": cycle.summary}]}
    rows = _collect_completed_position_rows(bot, orders, fills)
    assert len(rows) == 1
    assert (rows[0]["entry_price"], rows[0]["exit_price"]) == ("6.3300", "6.2111")


def test_distinct_order_only_reentry_is_not_hidden_by_gixi_fills():
    orders, fills = _gixi()
    for index, price in enumerate(("6.40", "6.50")):
        orders.append({**orders[index], "order_id": f"later-{index}", "price": price,
                       "updated_at": f"2026-10-12 09:4{index}:00 AM ET"})
    cycles = _cycles(orders, fills)
    assert len(cycles) == 2
    assert {cycle.entry_price for cycle in cycles} == {6.33, 6.40}


@pytest.mark.parametrize("mismatch", ["order_id", "broker_account_name", "symbol", "side", "strategy_code"])
def test_identity_mismatch_cannot_remove_order_fallback(mismatch):
    orders, fills = _gixi()
    for fill in fills:
        fill[mismatch] = "different"
    assert any(cycle.entry_price == 0 for cycle in _cycles(orders, fills))


def test_partial_fill_does_not_claim_entire_order_is_priced():
    orders, fills = _gixi()
    for fill in fills:
        fill["quantity"] = "1"
    assert any(cycle.quantity == 2 for cycle in _cycles(orders, fills))


def test_unpriced_fill_cannot_hide_priced_order_fallback():
    orders, fills = _gixi()
    for order, fill, price in zip(orders, fills, ("6.33", "6.2111"), strict=True):
        order["price"] = price
        fill["price"] = "0"
    assert any(cycle.entry_price == 6.33 for cycle in _cycles(orders, fills))


def test_other_bot_reconstruction_is_unchanged():
    orders, fills = _gixi()
    for item in orders + fills:
        item["strategy_code"] = "schwab_1m_v2"
    cycles = collect_completed_trade_cycles(
        strategy_code="schwab_1m_v2", broker_account_name="live:schwab_1m_v2",
        recent_orders=orders, recent_fills=fills, closed_today=[],
    )
    assert len(cycles) == 2
