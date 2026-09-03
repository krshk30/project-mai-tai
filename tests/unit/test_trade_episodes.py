from __future__ import annotations

from project_mai_tai.trade_episodes import coalesce_completed_trade_cycles
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
                "broker_account_name": "live:orb",
                "broker_provider": "webull",
                "symbol": "CHPT",
                "side": "sell",
                "quantity": "1",
                "price": "7.80",
                "filled_at": "2026-09-03 10:12:00 AM ET",
            },
            {
                "broker_account_name": "live:schwab_1m_v2",
                "broker_provider": "schwab",
                "symbol": "CHPT",
                "side": "sell",
                "quantity": "2",
                "price": "7.89",
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
                "entry_slot": "reclaim",
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
