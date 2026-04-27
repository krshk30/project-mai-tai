from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from project_mai_tai.services.control_plane import (
    _build_bot_account_rows,
    _build_bot_account_summary,
    _build_bot_position_rows,
)
from project_mai_tai.services.strategy_engine_app import StrategyEngineService
from project_mai_tai.market_data.schwab_tick_archive import load_aggregated_trade_bars
from project_mai_tai.services.strategy_engine_app import StrategyEngineState
from project_mai_tai.settings import Settings


def _write_trade(path: Path, *, timestamp_ns: int, price: float, size: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "event_type": "trade",
        "symbol": path.stem,
        "timestamp_ns": timestamp_ns,
        "recorded_at_ns": timestamp_ns,
        "price": price,
        "size": size,
        "conditions": [],
    }
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload))
        handle.write("\n")


def test_load_aggregated_trade_bars_builds_one_minute_ohlcv(tmp_path: Path) -> None:
    archive_file = tmp_path / "2026-04-27" / "YAAS.jsonl"
    _write_trade(
        archive_file,
        timestamp_ns=int(datetime(2026, 4, 27, 11, 0, 5, tzinfo=UTC).timestamp() * 1_000_000_000),
        price=1.10,
        size=100,
    )
    _write_trade(
        archive_file,
        timestamp_ns=int(datetime(2026, 4, 27, 11, 0, 25, tzinfo=UTC).timestamp() * 1_000_000_000),
        price=1.30,
        size=150,
    )
    _write_trade(
        archive_file,
        timestamp_ns=int(datetime(2026, 4, 27, 11, 0, 55, tzinfo=UTC).timestamp() * 1_000_000_000),
        price=1.20,
        size=50,
    )

    bars = load_aggregated_trade_bars(
        tmp_path,
        symbol="YAAS",
        day="2026-04-27",
        interval_secs=60,
    )

    assert len(bars) == 1
    bar = bars[0]
    assert bar.open == 1.10
    assert bar.high == 1.30
    assert bar.low == 1.10
    assert bar.close == 1.20
    assert bar.volume == 300
    assert bar.trade_count == 3


def test_schwab_1m_uses_schwab_history_targets_not_generic_hydration() -> None:
    state = StrategyEngineState(
        settings=Settings(
            strategy_macd_30s_enabled=False,
            strategy_webull_30s_enabled=False,
            strategy_macd_1m_enabled=False,
            strategy_schwab_1m_enabled=True,
        )
    )

    runtime = state.bots["schwab_1m"]
    runtime.set_watchlist(["YAAS"])

    assert state.market_data_hydration_pairs(["YAAS"]) == set()
    assert state.schwab_native_history_targets(["YAAS"]) == [("schwab_1m", "YAAS", 60)]
    assert "schwab_1m" in state.schwab_stream_strategy_codes()


def test_schwab_1m_no_first_tick_does_not_halt_flat_symbol() -> None:
    service = StrategyEngineService(
        settings=Settings(
            redis_url="redis://localhost:6379/15",
            strategy_macd_30s_enabled=False,
            strategy_webull_30s_enabled=False,
            strategy_macd_1m_enabled=False,
            strategy_schwab_1m_enabled=True,
        )
    )

    now = datetime(2026, 4, 27, 19, 0, 0, tzinfo=UTC)
    service._schwab_symbol_active_first_seen_at["YAAS"] = now - timedelta(hours=4)

    assert (
        service._is_schwab_symbol_data_halt_stale(
            "YAAS",
            now,
            strategy_codes=("schwab_1m",),
            has_open_position=False,
        )
        is False
    )
    assert (
        service._is_schwab_symbol_data_halt_stale(
            "YAAS",
            now,
            strategy_codes=("schwab_1m",),
            has_open_position=True,
        )
        is True
    )


def test_flat_symbol_schwab_resubscribe_interval_is_backed_off() -> None:
    service = StrategyEngineService(
        settings=Settings(
            redis_url="redis://localhost:6379/15",
            strategy_macd_30s_enabled=True,
            strategy_webull_30s_enabled=False,
            strategy_macd_1m_enabled=False,
            strategy_schwab_1m_enabled=False,
        )
    )

    assert service._schwab_symbol_resubscribe_interval_seconds(has_open_position=True) == 5.0
    assert service._schwab_symbol_resubscribe_interval_seconds(has_open_position=False) == 45.0


def test_data_health_summary_is_degraded_for_flat_halted_symbol() -> None:
    state = StrategyEngineState(
        settings=Settings(
            strategy_macd_30s_enabled=True,
            strategy_webull_30s_enabled=False,
            strategy_macd_1m_enabled=False,
            strategy_schwab_1m_enabled=False,
        )
    )

    runtime = state.bots["macd_30s"]
    runtime.apply_data_halt("ENVB", reason="quiet symbol")

    summary = runtime.data_health_summary()

    assert summary["status"] == "degraded"
    assert summary["halted_symbols"] == ["ENVB"]
    assert summary["open_position_halted_symbols"] == []


def test_bot_ui_hides_account_only_positions_from_strategy_views() -> None:
    data = {
        "account_positions": [
            {
                "broker_account_name": "paper:schwab_1m",
                "symbol": "CANF",
                "quantity": "100",
                "average_price": "2.50",
                "market_value": "250.00",
                "updated_at": "2026-04-27 03:47:09 PM ET",
            }
        ],
        "virtual_positions": [],
    }
    bot = {
        "strategy_code": "schwab_1m",
        "account_name": "paper:schwab_1m",
        "positions": [],
        "runtime_kind": "macd",
    }

    summary = _build_bot_account_summary(data, bot)
    assert summary["account_position_count"] == 0
    assert summary["non_strategy_symbol_count"] == 1
    assert summary["non_strategy_symbols"] == ["CANF"]

    assert "No broker-account positions" in _build_bot_account_rows(data, bot)
    assert "No open positions" in _build_bot_position_rows(data, bot)
