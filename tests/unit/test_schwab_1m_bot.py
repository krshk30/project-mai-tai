from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

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
