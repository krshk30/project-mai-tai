from __future__ import annotations

from project_mai_tai.trade_episodes import parse_et_timestamp, coalesce_completed_trade_cycles
from project_mai_tai.trade_episodes import collect_completed_trade_cycles


def test_collect_completed_trade_cycles_prefers_fills() -> None:
    cycles = collect_completed_trade_cycles(
        strategy_code="macd_30s",
        broker_account_name="paper:macd_30s",
        recent_orders=[
            {
                "symbol": "IONZ",
                "side": "buy",
                "intent_type": "open",
                "quantity": "100",
                "price": "1.00",
                "status": "filled",
                "reason": "ENTRY_P1_CROSS",
                "path": "P1_CROSS",
                "updated_at": "2026-04-24 09:35:00 AM ET",
            },
            {
                "symbol": "IONZ",
                "side": "sell",
                "intent_type": "close",
                "quantity": "100",
                "price": "1.05",
                "status": "filled",
                "reason": "STOP_LOSS",
                "path": "",
                "updated_at": "2026-04-24 09:40:00 AM ET",
            },
        ],
        recent_fills=[
            {
                "symbol": "IONZ",
                "side": "buy",
                "quantity": "100",
                "price": "1.00",
                "filled_at": "2026-04-24 09:35:00 AM ET",
            },
            {
                "symbol": "IONZ",
                "side": "sell",
                "quantity": "100",
                "price": "1.20",
                "filled_at": "2026-04-24 09:40:00 AM ET",
            },
        ],
        closed_today=[],
    )

    assert len(cycles) == 1
    assert cycles[0].symbol == "IONZ"
    assert cycles[0].path == "P1_CROSS"
    assert cycles[0].entry_price == 1.0
    assert cycles[0].exit_price == 1.2
    assert round(cycles[0].pnl, 2) == 20.0


def test_collect_completed_trade_cycles_separates_account_and_strategy_keys() -> None:
    cycle_a = collect_completed_trade_cycles(
        strategy_code="macd_30s",
        broker_account_name="paper:macd_30s",
        recent_orders=[],
        recent_fills=[
            {
                "symbol": "SMX",
                "side": "buy",
                "quantity": "10",
                "price": "2.00",
                "filled_at": "2026-04-24 10:00:00 AM ET",
            },
            {
                "symbol": "SMX",
                "side": "sell",
                "quantity": "10",
                "price": "2.20",
                "filled_at": "2026-04-24 10:05:00 AM ET",
            },
        ],
        closed_today=[],
    )[0]
    cycle_b = collect_completed_trade_cycles(
        strategy_code="polygon_30s",
        broker_account_name="live:polygon_30s",
        recent_orders=[],
        recent_fills=[
            {
                "symbol": "SMX",
                "side": "buy",
                "quantity": "10",
                "price": "2.00",
                "filled_at": "2026-04-24 10:00:00 AM ET",
            },
            {
                "symbol": "SMX",
                "side": "sell",
                "quantity": "10",
                "price": "2.20",
                "filled_at": "2026-04-24 10:05:00 AM ET",
            },
        ],
        closed_today=[],
    )[0]

    assert cycle_a.symbol == cycle_b.symbol
    assert cycle_a.cycle_key != cycle_b.cycle_key


def test_collect_completed_trade_cycles_separates_brokers_in_one_event_stream() -> None:
    cycles = collect_completed_trade_cycles(
        strategy_code="schwab_1m_v2",
        broker_account_name="live:schwab_1m_v2",
        recent_orders=[],
        recent_fills=[
            {
                "broker_account_name": "live:schwab_1m_v2",
                "broker_provider": "schwab",
                "symbol": "CHPT",
                "side": "buy",
                "quantity": "2",
                "price": "7.73",
                "entry_slot": "first",
                "filled_at": "2026-09-03 10:06:10 AM ET",
            },
            {
                "broker_account_name": "live:orb",
                "broker_provider": "webull",
                "symbol": "CHPT",
                "side": "buy",
                "quantity": "1",
                "price": "7.72",
                "entry_slot": "first",
                "filled_at": "2026-09-03 10:06:11 AM ET",
            },
            {
                "broker_account_name": "live:schwab_1m_v2",
                "broker_provider": "schwab",
                "symbol": "CHPT",
                "side": "sell",
                "quantity": "2",
                "price": "7.89",
                "filled_at": "2026-09-03 10:12:00 AM ET",
            },
            {
                "broker_account_name": "live:orb",
                "broker_provider": "webull",
                "symbol": "CHPT",
                "side": "sell",
                "quantity": "1",
                "price": "7.80",
                "filled_at": "2026-09-03 10:15:36 AM ET",
            },
        ],
        closed_today=[],
    )

    assert len(cycles) == 2
    by_account = {cycle.broker_account_name: cycle for cycle in cycles}
    assert by_account["live:schwab_1m_v2"].quantity == 2
    assert by_account["live:schwab_1m_v2"].exit_price == 7.89
    assert by_account["live:schwab_1m_v2"].path == "Resting / Schwab"
    assert by_account["live:orb"].quantity == 1
    assert by_account["live:orb"].exit_price == 7.80
    assert by_account["live:orb"].path == "Resting / Webull"


def test_collect_completed_trade_cycles_labels_v2_reclaim_from_durable_slot() -> None:
    cycles = collect_completed_trade_cycles(
        strategy_code="schwab_1m_v2",
        broker_account_name="live:schwab_1m_v2",
        recent_orders=[],
        recent_fills=[
            {
                "broker_account_name": "live:schwab_1m_v2",
                "broker_provider": "schwab",
                "symbol": "CHPT",
                "side": "buy",
                "quantity": "2",
                "price": "7.90",
                "metadata": {"cw_entry_slot": "reclaim"},
                "filled_at": "2026-09-03 10:16:03 AM ET",
            },
            {
                "broker_account_name": "live:schwab_1m_v2",
                "broker_provider": "schwab",
                "symbol": "CHPT",
                "side": "sell",
                "quantity": "2",
                "price": "8.05",
                "filled_at": "2026-09-03 10:27:37 AM ET",
            },
        ],
        closed_today=[],
    )

    assert len(cycles) == 1
    assert cycles[0].path == "Reclaim / Schwab"


def test_collect_completed_trade_cycles_falls_back_to_filled_orders_when_needed() -> None:
    cycles = collect_completed_trade_cycles(
        strategy_code="polygon_30s",
        broker_account_name="live:polygon_30s",
        recent_orders=[
            {
                "symbol": "CAST",
                "side": "buy",
                "intent_type": "open",
                "quantity": "100",
                "price": "1.50",
                "status": "filled",
                "reason": "ENTRY_P1_CROSS",
                "path": "P1_CROSS",
                "updated_at": "2026-04-24 11:00:00 AM ET",
            },
            {
                "symbol": "CAST",
                "side": "sell",
                "intent_type": "close",
                "quantity": "100",
                "price": "1.65",
                "status": "filled",
                "reason": "TAKE_PROFIT",
                "path": "",
                "updated_at": "2026-04-24 11:06:00 AM ET",
            },
        ],
        recent_fills=[],
        closed_today=[],
    )

    assert len(cycles) == 1
    assert cycles[0].symbol == "CAST"
    assert cycles[0].entry_price == 1.5
    assert cycles[0].exit_price == 1.65


def test_collect_completed_trade_cycles_sanitizes_broker_payload_close_reason() -> None:
    cycles = collect_completed_trade_cycles(
        strategy_code="macd_30s",
        broker_account_name="paper:macd_30s",
        recent_orders=[
            {
                "symbol": "SST",
                "side": "buy",
                "intent_type": "open",
                "quantity": "10",
                "price": "4.09",
                "status": "filled",
                "reason": "ENTRY_P1_CROSS",
                "path": "P1_CROSS",
                "updated_at": "2026-05-01 07:04:02 AM ET",
            },
            {
                "symbol": "SST",
                "side": "sell",
                "intent_type": "close",
                "quantity": "10",
                "price": "4.04",
                "status": "filled",
                "reason": "{'Session': 'Am', 'Duration': 'Day', 'Ordertype': 'Limit', 'Orderlegcollection': []}",
                "path": "",
                "updated_at": "2026-05-01 07:05:45 AM ET",
            },
        ],
        recent_fills=[],
        closed_today=[],
    )

    assert len(cycles) == 1
    assert cycles[0].path == "P1_CROSS"
    assert cycles[0].summary == "Final Close"


def test_collect_completed_trade_cycles_recovers_reconciled_path_and_summary_from_matching_orders() -> None:
    cycles = collect_completed_trade_cycles(
        strategy_code="macd_30s",
        broker_account_name="paper:macd_30s",
        recent_orders=[
            {
                "symbol": "ATRA",
                "side": "buy",
                "intent_type": "open",
                "quantity": "10",
                "price": "7.65",
                "status": "filled",
                "reason": "ENTRY_P3_SURGE",
                "path": "",
                "metadata": {"path": "P3_SURGE"},
                "updated_at": "2026-05-07 10:39:37 AM ET",
            },
            {
                "symbol": "ATRA",
                "side": "sell",
                "intent_type": "close",
                "quantity": "10",
                "price": "7.54",
                "status": "filled",
                "reason": "HARD_STOP_NATIVE_BACKUP",
                "path": "",
                "updated_at": "2026-05-07 10:40:56 AM ET",
            },
        ],
        recent_fills=[],
        closed_today=[
            {
                "ticker": "ATRA",
                "path": "DB_RECONCILE",
                "quantity": 10,
                "entry_time": "2026-05-07 10:39:37 AM ET",
                "entry_price": 7.65,
                "exit_time": "2026-05-07 10:40:56 AM ET",
                "exit_price": 7.54,
                "pnl": -1.10,
                "pnl_pct": -1.4,
                "reason": "close",
            }
        ],
    )

    assert len(cycles) == 1
    assert cycles[0].path == "P3_SURGE"
    assert cycles[0].summary == "Hard Stop Native Backup"


def test_collect_completed_trade_cycles_marks_reconciled_rows_when_no_better_path_exists() -> None:
    cycles = collect_completed_trade_cycles(
        strategy_code="runner",
        broker_account_name="paper:runner",
        recent_orders=[],
        recent_fills=[],
        closed_today=[
            {
                "ticker": "RMSG",
                "path": "DB_RECONCILE",
                "quantity": 10,
                "entry_time": "2026-05-07 09:13:31 AM ET",
                "entry_price": 1.68,
                "exit_time": "2026-05-07 09:16:17 AM ET",
                "exit_price": 1.65,
                "pnl": -0.25,
                "pnl_pct": -1.5,
                "reason": "close",
            }
        ],
    )

    assert len(cycles) == 1
    assert cycles[0].path == "RECONCILED"
    assert cycles[0].summary == "Reconciled close"


def test_coalesce_completed_trade_cycles_merges_shadow_close_row_into_real_cycle() -> None:
    rows = [
        {
            "strategy_code": "schwab_1m",
            "broker_account_name": "paper:schwab_1m",
            "symbol": "UONE",
            "cycle_key": "real",
            "path": "P3_SURGE",
            "quantity": 10.0,
            "entry_time": "2026-05-01 01:11:09 PM ET",
            "entry_price": 0.0,
            "exit_time": "2026-05-01 01:12:02 PM ET",
            "exit_price": 0.0,
            "pnl": 0.0,
            "pnl_pct": 0.0,
            "summary": "Hard Stop Native Backup",
            "sort_time": "2026-05-01 01:12:02 PM ET",
        },
        {
            "strategy_code": "schwab_1m",
            "broker_account_name": "paper:schwab_1m",
            "symbol": "UONE",
            "cycle_key": "shadow",
            "path": "-",
            "quantity": 10.0,
            "entry_time": "2026-05-01 01:11:09 PM ET",
            "entry_price": 7.48,
            "exit_time": "2026-05-01 01:11:24 PM ET",
            "exit_price": 7.36,
            "pnl": -1.16,
            "pnl_pct": -1.6,
            "summary": "Close",
            "sort_time": "2026-05-01 01:11:24 PM ET",
        },
    ]

    merged = coalesce_completed_trade_cycles(rows)

    assert len(merged) == 1
    assert merged[0]["path"] == "P3_SURGE"
    assert merged[0]["entry_price"] == 7.48
    assert merged[0]["exit_price"] == 7.36
    assert merged[0]["pnl"] == -1.16
    assert merged[0]["summary"] == "Hard Stop Native Backup"
    assert merged[0]["exit_time"] == "2026-05-01 01:12:02 PM ET"


def _fill(side, qty, price, at, *, account="live:orb", intent, reason):
    return {
        "symbol": "BNC", "side": side, "quantity": qty, "price": price, "filled_at": at,
        "strategy_code": "schwab_1m_v2", "broker_account_name": account,
        "intent_type": intent, "reason": reason,
    }


def test_a_managed_row_inside_an_existing_cycle_is_not_a_second_position():
    """⛔ MEASURED IN PRODUCTION, BNC 2026-09-08. The fan-out leg's fill is stamped 09:50:51 and its
    managed row 09:51:04 — 13 seconds of settle lag. Deduping on the exact entry-time string missed,
    so the dashboard rendered the SAME Webull position twice: once truthfully at -$0.12, and once
    from the managed row with no prices and $+0.00 (+0.0%).
    """
    fills = [
        _fill("buy", 1, 5.25, "2026-09-08 09:50:51 AM ET", intent="open", reason="ENTRY_RESTING"),
        _fill("sell", 1, 5.1325, "2026-09-08 09:52:05 AM ET", intent="close", reason="Close"),
    ]
    closed_today = [{
        "ticker": "BNC", "broker_account_name": "live:orb",
        "entry_time": "2026-09-08T13:51:04.129000+00:00",
        "exit_time": "2026-09-08T13:52:18.677000+00:00",
        "original_quantity": 1, "entry_path": "Resting",
        "reason": "oms_v2_managed_exit:CONFIRMATION_EXIT",
    }]

    cycles = collect_completed_trade_cycles(
        strategy_code="schwab_1m_v2", broker_account_name="live:orb",
        recent_orders=[], recent_fills=fills, closed_today=closed_today,
    )

    assert len(cycles) == 1, [
        (c.entry_time, c.entry_price, c.exit_price, c.pnl) for c in cycles
    ]
    only = cycles[0]
    assert only.entry_price == 5.25
    assert only.exit_price == 5.1325
    assert only.pnl < 0, "the surviving row must be the priced one, not the $0.00 phantom"


def test_a_genuine_re_entry_after_the_close_is_still_its_own_position():
    """⛔ THE CONTROL THAT KEEPS THE FIX HONEST. BNC exited 11:02:21 and re-entered 11:03:08 on the
    same day — 47 seconds later. A tolerance-window dedupe would have swallowed it. The interval
    test must not: the second entry falls OUTSIDE the first cycle's window.
    """
    fills = [
        _fill("buy", 1, 4.83, "2026-09-08 10:59:14 AM ET", intent="open", reason="ENTRY_RESTING"),
        _fill("sell", 1, 4.93, "2026-09-08 11:02:21 AM ET", intent="close", reason="Close"),
    ]
    closed_today = [{
        "ticker": "BNC", "broker_account_name": "live:orb",
        "entry_time": "2026-09-08T15:03:08.480000+00:00",
        "exit_time": "2026-09-08T15:18:31.000000+00:00",
        "original_quantity": 1, "entry_path": "Reclaim",
        "reason": "oms_v2_managed_exit:CLOSE",
    }]

    cycles = collect_completed_trade_cycles(
        strategy_code="schwab_1m_v2", broker_account_name="live:orb",
        recent_orders=[], recent_fills=fills, closed_today=closed_today,
    )

    assert len(cycles) == 2, "a real re-entry was swallowed by the duplicate suppression"


def test_the_same_symbol_on_the_other_broker_is_never_suppressed():
    """The two fan-out legs are separate positions. Account is part of the identity."""
    fills = [
        _fill("buy", 1, 5.25, "2026-09-08 09:50:51 AM ET", intent="open", reason="ENTRY_RESTING"),
        _fill("sell", 1, 5.1325, "2026-09-08 09:52:05 AM ET", intent="close", reason="Close"),
    ]
    closed_today = [{
        "ticker": "BNC", "broker_account_name": "live:schwab_1m_v2",
        "entry_time": "2026-09-08T13:51:04.129000+00:00",
        "exit_time": "2026-09-08T13:57:24.000000+00:00",
        "original_quantity": 2, "entry_path": "Resting", "reason": "oms_v2_managed_exit:CLOSE",
    }]

    cycles = collect_completed_trade_cycles(
        strategy_code="schwab_1m_v2", broker_account_name="live:orb",
        recent_orders=[], recent_fills=fills, closed_today=closed_today,
    )

    assert len(cycles) == 2, "the Schwab leg was suppressed by the Webull leg's window"


def test_parse_et_timestamp_reads_the_iso_utc_shape_the_bot_actually_publishes():
    """⛔ THE FIXTURE-VS-PRODUCTION CONTROL. `closed_today` is built with `lot[2].isoformat()`, so
    every timestamp reaching the dedupe is ISO UTC with fractional seconds — not the display-ET
    string the dashboard's own rows carry. Before this, ISO fell through to datetime.min, so BOTH
    duplicate checks compared a real time against year 1 and never matched. The first version of
    this fix was written against display-ET fixtures and was therefore INERT in production.
    """
    parsed = parse_et_timestamp("2026-09-08T13:50:51.255000+00:00")

    assert parsed.year == 2026, "the live ISO shape fell through to datetime.min"
    assert (parsed.hour, parsed.minute, parsed.second) == (9, 50, 51), "not converted to ET"
    # display-ET must still parse, and the two forms must agree on the same instant
    display = parse_et_timestamp("2026-09-08 09:50:51 AM ET")
    assert abs((parsed - display).total_seconds()) < 1


def test_the_production_bnc_payload_yields_exactly_one_priced_cycle():
    """The live 2026-09-08 BNC/Webull leg, verbatim: fills in display-ET, closed_today in ISO UTC.

    On the previous head this returned TWO cycles — the priced one at -$0.1175 and a phantom at
    $0.00 — because the ISO timestamps could not be parsed.
    """
    fills = [
        _fill("buy", 1, 5.25, "2026-09-08 09:50:51 AM ET", intent="open", reason="ENTRY_RESTING"),
        _fill("sell", 1, 5.1325, "2026-09-08 09:52:05 AM ET", intent="close", reason="Close"),
    ]
    closed_today = [{
        "ticker": "BNC", "broker_account_name": "live:orb",
        "entry_time": "2026-09-08T13:51:04.129000+00:00",
        "exit_time": "2026-09-08T13:52:18.677000+00:00",
        "original_quantity": 1, "entry_path": "Resting",
        "reason": "oms_v2_managed_exit:CONFIRMATION_EXIT",
    }]

    cycles = collect_completed_trade_cycles(
        strategy_code="schwab_1m_v2", broker_account_name="live:orb",
        recent_orders=[], recent_fills=fills, closed_today=closed_today,
    )

    assert len(cycles) == 1, [(c.entry_time, c.entry_price, c.pnl) for c in cycles]
    assert cycles[0].entry_price == 5.25
    assert cycles[0].exit_price == 5.1325
    assert cycles[0].pnl < 0, "the surviving row must carry the real loss, not $0.00"


def test_a_filled_order_does_not_duplicate_a_position_the_fills_already_priced():
    """⛔ MEASURED LIVE, 2026-09-08, and the reason the dashboard showed every Webull leg twice.

    The collector reconstructs cycles from fills AND from filled orders. A broker_orders row carries
    NO fill price (payload_has_price=NO on all eight BNC/live:orb orders that day) and is stamped at
    `updated_at`, not the fill time — so the order copy renders `-` entry, `-` exit and $+0.00.
    Exact-timestamp dedupe missed because the stamps differ by ~14 seconds:
        fill  12:09:50  -$0.02
        order 12:10:04  $+0.00   <- open-e0c300989641 / close-8a0891514c98
    """
    fills = [
        {"symbol": "BNC", "side": "buy", "quantity": 1, "price": 5.20,
         "filled_at": "2026-09-08 12:09:50 PM ET", "strategy_code": "schwab_1m_v2",
         "broker_account_name": "live:orb", "intent_type": "open", "reason": "ENTRY"},
        {"symbol": "BNC", "side": "sell", "quantity": 1, "price": 5.185,
         "filled_at": "2026-09-08 12:11:07 PM ET", "strategy_code": "schwab_1m_v2",
         "broker_account_name": "live:orb", "intent_type": "close", "reason": "Close"},
    ]
    orders = [
        {"symbol": "BNC", "side": "buy", "quantity": 1, "status": "filled",
         "updated_at": "2026-09-08 12:10:04 PM ET", "strategy_code": "schwab_1m_v2",
         "broker_account_name": "live:orb", "intent_type": "open", "reason": "ENTRY"},
        {"symbol": "BNC", "side": "sell", "quantity": 1, "status": "filled",
         "updated_at": "2026-09-08 12:11:17 PM ET", "strategy_code": "schwab_1m_v2",
         "broker_account_name": "live:orb", "intent_type": "close",
         "reason": "oms_v2_managed_exit:CONFIRMATION_EXIT"},
    ]

    cycles = collect_completed_trade_cycles(
        strategy_code="schwab_1m_v2", broker_account_name="live:orb",
        recent_orders=orders, recent_fills=fills, closed_today=[],
    )

    assert len(cycles) == 1, [(c.entry_time, c.entry_price, c.pnl) for c in cycles]
    assert cycles[0].entry_price == 5.20, "the surviving row must be the PRICED one"
    assert cycles[0].pnl < 0, "the phantom, not the real loss, survived"


def test_an_order_only_position_is_still_reported_when_no_fill_exists():
    """⛔ THE CONTROL THAT KEEPS THE ORDER PASS ALIVE. It is a fallback for positions whose fills
    never arrived; suppressing it wholesale would silently drop those trades."""
    orders = [
        {"symbol": "ZZZ", "side": "buy", "quantity": 10, "price": "2.00", "status": "filled",
         "updated_at": "2026-09-08 10:00:00 AM ET", "intent_type": "open", "reason": "ENTRY_X",
         "path": "X", "broker_account_name": "live:orb"},
        {"symbol": "ZZZ", "side": "sell", "quantity": 10, "price": "2.20", "status": "filled",
         "updated_at": "2026-09-08 10:05:00 AM ET", "intent_type": "close", "reason": "Close",
         "broker_account_name": "live:orb"},
    ]

    cycles = collect_completed_trade_cycles(
        strategy_code="schwab_1m_v2", broker_account_name="live:orb",
        recent_orders=orders, recent_fills=[], closed_today=[],
    )

    assert len(cycles) == 1
    assert cycles[0].entry_price == 2.0, "an order-only position lost its prices"


def test_two_separate_positions_on_one_symbol_both_survive():
    """⛔ THE AXIS MY FIRST CONTROL SET MISSED. Suppressing on symbol+account alone left the suite
    green, so nothing proved the interval was doing the work. BNC traded FOUR times on live:orb on
    2026-09-08 — exiting 11:02:21 and re-entering 47 seconds later at 11:03:08. Both are real."""
    fills = [
        {"symbol": "BNC", "side": "buy", "quantity": 1, "price": 4.83,
         "filled_at": "2026-09-08 10:59:14 AM ET", "strategy_code": "schwab_1m_v2",
         "broker_account_name": "live:orb", "intent_type": "open", "reason": "ENTRY"},
        {"symbol": "BNC", "side": "sell", "quantity": 1, "price": 4.93,
         "filled_at": "2026-09-08 11:02:21 AM ET", "strategy_code": "schwab_1m_v2",
         "broker_account_name": "live:orb", "intent_type": "close", "reason": "Close"},
        {"symbol": "BNC", "side": "buy", "quantity": 1, "price": 5.20,
         "filled_at": "2026-09-08 12:09:50 PM ET", "strategy_code": "schwab_1m_v2",
         "broker_account_name": "live:orb", "intent_type": "open", "reason": "ENTRY"},
        {"symbol": "BNC", "side": "sell", "quantity": 1, "price": 5.185,
         "filled_at": "2026-09-08 12:11:07 PM ET", "strategy_code": "schwab_1m_v2",
         "broker_account_name": "live:orb", "intent_type": "close", "reason": "Close"},
    ]

    cycles = collect_completed_trade_cycles(
        strategy_code="schwab_1m_v2", broker_account_name="live:orb",
        recent_orders=[], recent_fills=fills, closed_today=[],
    )

    assert len(cycles) == 2, "a real second position on the same symbol was swallowed"
    assert {c.entry_price for c in cycles} == {4.83, 5.20}
