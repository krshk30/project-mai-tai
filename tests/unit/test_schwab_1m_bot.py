from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from websockets.exceptions import ConnectionClosedOK
from websockets.frames import Close

from project_mai_tai.db.models import StrategyBarHistory
from project_mai_tai.services.control_plane import (
    _build_bot_account_rows,
    _build_bot_account_summary,
    _build_bot_position_rows,
)
from project_mai_tai.market_data.models import LiveBarRecord, TradeTickRecord
from project_mai_tai.market_data.schwab_streamer import SchwabStreamerClient
from project_mai_tai.market_data.schwab_streamer import SchwabStreamerCredentials
from project_mai_tai.market_data.schwab_tick_archive import (
    SchwabTickArchive,
    load_recorded_live_bars,
    load_recorded_trades,
)
from project_mai_tai.services.strategy_engine_app import StrategyEngineService
from project_mai_tai.market_data.schwab_tick_archive import load_aggregated_trade_bars
from project_mai_tai.services.strategy_engine_app import StrategyEngineState
from project_mai_tai.settings import Settings
from tests.unit.test_strategy_engine_service import (
    FakeRedis,
    build_test_session_factory,
    fixed_now,
    make_test_settings,
    seed_trending_bars,
)


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


def test_load_recorded_trades_returns_sorted_trade_ticks(tmp_path: Path) -> None:
    archive = SchwabTickArchive(tmp_path)
    trade_times = [
        datetime(2026, 4, 27, 20, 0, 45, tzinfo=UTC),
        datetime(2026, 4, 27, 20, 0, 15, tzinfo=UTC),
        datetime(2026, 4, 27, 20, 1, 5, tzinfo=UTC),
    ]
    for index, trade_time in enumerate(trade_times):
        archive.record_trade(
            TradeTickRecord(
                symbol="YAAS",
                price=1.10 + (index * 0.05),
                size=100 + index,
                timestamp_ns=int(trade_time.timestamp() * 1_000_000_000),
            ),
            recorded_at_ns=int(trade_time.timestamp() * 1_000_000_000),
        )
    archive.close()

    records = load_recorded_trades(
        tmp_path,
        symbol="YAAS",
        day="2026-04-27",
    )

    assert [int(record.timestamp_ns or 0) for record in records] == sorted(
        int(trade_time.timestamp() * 1_000_000_000) for trade_time in trade_times
    )


def test_schwab_1m_uses_schwab_history_targets_not_generic_hydration() -> None:
    state = StrategyEngineState(
        settings=Settings(
            strategy_macd_30s_enabled=False,
            strategy_polygon_30s_enabled=False,
            strategy_macd_1m_enabled=False,
            strategy_schwab_1m_enabled=True,
        )
    )

    runtime = state.bots["schwab_1m"]
    runtime.set_watchlist(["YAAS"])

    assert state.market_data_hydration_pairs(["YAAS"]) == set()
    assert state.schwab_native_history_targets(["YAAS"]) == [("schwab_1m", "YAAS", 60)]
    assert "schwab_1m" in state.schwab_stream_strategy_codes()


def test_schwab_1m_uses_completed_bar_entries_and_shorter_cooldown() -> None:
    state = StrategyEngineState(
        settings=Settings(
            strategy_macd_30s_enabled=True,
            strategy_polygon_30s_enabled=True,
            strategy_macd_1m_enabled=False,
            strategy_schwab_1m_enabled=True,
        )
    )

    schwab_30s = state.bots["macd_30s"].definition.trading_config
    polygon_30s = state.bots["polygon_30s"].definition.trading_config
    schwab_1m = state.bots["schwab_1m"].definition.trading_config

    assert schwab_30s.entry_intrabar_enabled is False
    assert polygon_30s.entry_intrabar_enabled is False
    assert schwab_1m.entry_intrabar_enabled is False
    assert schwab_30s.p4_prev_bar_entry_enabled is True
    assert polygon_30s.p4_prev_bar_entry_enabled is False
    assert schwab_1m.p4_prev_bar_entry_enabled is False
    assert schwab_30s.schwab_native_warmup_bars_required == 35
    assert polygon_30s.schwab_native_warmup_bars_required == 35
    assert schwab_1m.schwab_native_warmup_bars_required == 35
    assert schwab_30s.cooldown_bars == 10
    assert polygon_30s.cooldown_bars == 10
    assert schwab_1m.cooldown_bars == 5
    assert schwab_30s.p1_min_vol_ratio == 1.25
    assert polygon_30s.p1_min_vol_ratio == 1.25
    assert schwab_1m.p1_min_vol_ratio == 1.25
    assert schwab_30s.p1_min_volume_abs == 7500
    assert polygon_30s.p1_min_volume_abs == 7500
    assert schwab_1m.p1_min_volume_abs == 7500
    assert schwab_30s.p1_min_dollar_volume_abs == 25_000
    assert polygon_30s.p1_min_dollar_volume_abs == 25_000
    assert schwab_1m.p1_min_dollar_volume_abs == 25_000
    assert schwab_1m.p4_enabled is True
    assert schwab_1m.p4_body_pct == 4.0
    assert schwab_1m.p4_range_pct == 999.0
    assert schwab_1m.p4_close_top_pct == 20.0
    assert schwab_1m.p4_vol_mult20 == 2.0
    assert schwab_1m.p4_breakout_lookback == 1
    assert schwab_1m.p4_max_ema9_dist_pct == 3.5
    assert schwab_30s.p3_entry_stoch_k_cap == 80.0
    assert schwab_30s.p3_min_volume_abs == 10_000
    assert schwab_30s.p3_min_dollar_volume_abs == 35_000
    assert schwab_30s.p3_min_vol_ratio == 1.50
    assert schwab_30s.p3_hard_stop_pause_minutes == 30
    assert schwab_30s.p3_max_bars_since_macd_cross == 4
    assert schwab_30s.p3_max_recent_runup_pct == 8.0
    assert schwab_30s.p3_recent_runup_lookback_bars == 8
    assert polygon_30s.p3_entry_stoch_k_cap is None
    assert polygon_30s.p3_min_volume_abs is None
    assert polygon_30s.p3_min_dollar_volume_abs is None
    assert polygon_30s.p3_min_vol_ratio is None
    assert polygon_30s.p3_hard_stop_pause_minutes == 0
    assert polygon_30s.p3_max_bars_since_macd_cross is None
    assert polygon_30s.p3_max_recent_runup_pct is None
    assert polygon_30s.p3_recent_runup_lookback_bars == 0
    assert schwab_1m.p3_min_volume_abs == 20_000
    assert schwab_1m.p3_min_dollar_volume_abs == 70_000
    assert schwab_1m.p3_min_vol_ratio == 1.50
    assert schwab_1m.surge_rate == -0.001
    assert schwab_1m.p3_hard_stop_pause_minutes == 30
    assert schwab_1m.p3_max_bars_since_macd_cross == 2
    assert schwab_1m.p3_max_recent_runup_pct == 8.0
    assert schwab_1m.p3_recent_runup_lookback_bars == 4
    assert state.bots["schwab_1m"].use_live_aggregate_bars is True
    assert state.bots["schwab_1m"].live_aggregate_fallback_enabled is True
    assert state.bots["schwab_1m"].live_aggregate_bars_are_final is True


def test_schwab_native_variants_keep_intended_live_execution_tuning() -> None:
    state = StrategyEngineState(
        settings=Settings(
            strategy_macd_30s_enabled=True,
            strategy_polygon_30s_enabled=True,
            strategy_macd_1m_enabled=False,
            strategy_schwab_1m_enabled=True,
        )
    )

    schwab_30s = state.bots["macd_30s"].definition.trading_config
    polygon_30s = state.bots["polygon_30s"].definition.trading_config
    schwab_1m = state.bots["schwab_1m"].definition.trading_config

    assert schwab_30s.schwab_native_use_chop_regime is True
    assert schwab_30s.p3_allow_momentum_override is False
    assert schwab_30s.p3_entry_stoch_k_cap == 80.0
    assert schwab_30s.p3_min_volume_abs == 10_000
    assert schwab_30s.p3_min_dollar_volume_abs == 35_000
    assert schwab_30s.p3_min_vol_ratio == 1.50
    assert schwab_30s.p3_hard_stop_pause_minutes == 30
    assert schwab_30s.p3_max_bars_since_macd_cross == 4
    assert schwab_30s.p3_max_recent_runup_pct == 8.0
    assert schwab_30s.p3_recent_runup_lookback_bars == 8

    assert polygon_30s.p3_allow_momentum_override is True
    assert polygon_30s.p3_entry_stoch_k_cap is None
    assert polygon_30s.p3_min_volume_abs is None
    assert polygon_30s.p3_min_dollar_volume_abs is None
    assert polygon_30s.p3_min_vol_ratio is None
    assert polygon_30s.p3_hard_stop_pause_minutes == 0
    assert polygon_30s.p3_max_bars_since_macd_cross is None
    assert polygon_30s.p3_max_recent_runup_pct is None
    assert polygon_30s.p3_recent_runup_lookback_bars == 0

    assert schwab_1m.schwab_native_use_chop_regime is True
    assert schwab_1m.p3_allow_momentum_override is False
    assert schwab_1m.p3_min_score == 6
    assert schwab_1m.p3_entry_stoch_k_cap == 80.0
    assert schwab_1m.p3_min_volume_abs == 20_000
    assert schwab_1m.p3_min_dollar_volume_abs == 70_000
    assert schwab_1m.p3_min_vol_ratio == 1.50
    assert schwab_1m.p3_max_ema9_dist_pct == 2.0
    assert schwab_1m.p3_hard_stop_pause_minutes == 30
    assert schwab_1m.p3_max_bars_since_macd_cross == 2
    assert schwab_1m.p3_max_recent_runup_pct == 8.0
    assert schwab_1m.p3_recent_runup_lookback_bars == 4
    assert schwab_30s.p1_min_vol_ratio == 1.25
    assert schwab_1m.p1_min_vol_ratio == 1.25
    assert schwab_30s.p1_min_volume_abs == 7500
    assert schwab_1m.p1_min_volume_abs == 7500
    assert schwab_30s.p1_min_dollar_volume_abs == 25_000
    assert schwab_1m.p1_min_dollar_volume_abs == 25_000
    assert schwab_1m.cooldown_bars == 5
    assert schwab_1m.entry_intrabar_enabled is False
    assert schwab_30s.p4_prev_bar_entry_enabled is True
    assert polygon_30s.p4_prev_bar_entry_enabled is False
    assert schwab_1m.p4_prev_bar_entry_enabled is False
    assert schwab_1m.p4_enabled is True


def test_schwab_1m_needs_history_seed_until_required_bars_loaded() -> None:
    state = StrategyEngineState(
        settings=Settings(
            strategy_macd_30s_enabled=False,
            strategy_polygon_30s_enabled=False,
            strategy_macd_1m_enabled=False,
            strategy_schwab_1m_enabled=True,
        )
    )

    runtime = state.bots["schwab_1m"]
    symbol = "GCTK"

    runtime._history_seed_attempted.add(symbol)

    assert runtime.needs_history_seed(symbol) is True

    runtime.seed_bars(symbol, seed_trending_bars(count=55, interval_secs=60))

    assert runtime.needs_history_seed(symbol) is False


def test_schwab_1m_lazy_seed_retries_while_still_under_required_bars() -> None:
    state = StrategyEngineState(
        settings=Settings(
            strategy_macd_30s_enabled=False,
            strategy_polygon_30s_enabled=False,
            strategy_macd_1m_enabled=False,
            strategy_schwab_1m_enabled=True,
        )
    )

    runtime = state.bots["schwab_1m"]
    symbol = "GCTK"
    runtime._history_seed_attempted.add(symbol)

    class _ScalarResult:
        def all(self):
            return []

    class _Session:
        def __init__(self, counter: dict[str, int]) -> None:
            self._counter = counter

        def scalars(self, *_args, **_kwargs):
            self._counter["calls"] += 1
            return _ScalarResult()

    class _SessionFactory:
        def __init__(self) -> None:
            self.counter = {"calls": 0}

        def __call__(self):
            return self

        def __enter__(self):
            return _Session(self.counter)

        def __exit__(self, exc_type, exc, tb):
            return False

    factory = _SessionFactory()
    runtime.session_factory = factory

    runtime._ensure_history_seeded(symbol)
    first_call_count = factory.counter["calls"]
    runtime._ensure_history_seeded(symbol)

    assert first_call_count > 0
    assert factory.counter["calls"] > first_call_count


def test_schwab_1m_no_first_tick_does_not_halt_flat_symbol() -> None:
    service = StrategyEngineService(
        settings=Settings(
            redis_url="redis://localhost:6379/15",
            strategy_macd_30s_enabled=False,
            strategy_polygon_30s_enabled=False,
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


def test_schwab_1m_runtime_does_not_emit_intrabar_open_on_trade_tick(monkeypatch) -> None:
    service = StrategyEngineService(
        settings=make_test_settings(
            redis_stream_prefix="test",
            strategy_macd_30s_enabled=False,
            strategy_polygon_30s_enabled=False,
            strategy_macd_1m_enabled=False,
            strategy_schwab_1m_enabled=True,
            strategy_schwab_1m_broker_provider="schwab",
        ),
        redis_client=FakeRedis(),
        now_provider=fixed_now,
    )
    runtime = service.state.bots["schwab_1m"]
    runtime.set_watchlist(["CMND"])
    runtime.seed_bars("CMND", seed_trending_bars(count=55, interval_secs=60))

    captured: dict[str, int | float] = {}

    def fake_calculate(_bars):
        return {
            "price": 1.35,
            "bar_timestamp": 1_700_001_620.0,
        }

    def fake_check_entry(symbol, indicators, bar_index, position_tracker):
        del position_tracker
        captured["bar_index"] = bar_index
        captured["price"] = indicators["price"]
        return {
            "action": "BUY",
            "ticker": symbol,
            "path": "P3_SURGE",
            "price": indicators["price"],
            "score": 5,
            "score_details": "intrabar",
        }

    monkeypatch.setattr(runtime.indicator_engine, "calculate", fake_calculate)
    monkeypatch.setattr(runtime.entry_engine, "check_entry", fake_check_entry)
    monkeypatch.setattr(
        runtime.entry_engine,
        "pop_last_decision",
        lambda _symbol: {"status": "signal", "reason": "P3_SURGE", "path": "P3_SURGE", "score": "5"},
    )

    intents = runtime.handle_trade_tick(
        "CMND",
        price=1.36,
        size=100,
        timestamp_ns=1_700_001_651_000_000_000,
        cumulative_volume=50_000,
    )

    assert intents == []
    assert captured == {}
    assert "CMND" not in runtime.pending_open_symbols
    assert runtime.definition.trading_config.cooldown_bars == 5
    assert runtime.definition.trading_config.entry_intrabar_enabled is False


def test_schwab_streamer_extracts_chart_equity_bar() -> None:
    quotes, trades, bars = SchwabStreamerClient._extract_records(
        {
            "data": [
                {
                    "service": "CHART_EQUITY",
                    "content": [
                        {
                            "key": "SNBR",
                            "1": "42",
                            "2": "3.31",
                            "3": "3.45",
                            "4": "3.29",
                            "5": "3.39",
                            "6": "6611",
                            "7": "1777410960000",
                            "8": "20260428",
                        }
                    ],
                }
            ]
        }
    )

    assert quotes == []
    assert trades == []
    assert len(bars) == 1
    assert bars[0].symbol == "SNBR"
    assert bars[0].interval_secs == 60
    assert bars[0].open == 3.31
    assert bars[0].high == 3.45
    assert bars[0].low == 3.29
    assert bars[0].close == 3.39
    assert bars[0].volume == 6611
    assert bars[0].timestamp == 1_777_410_960.0


def test_schwab_streamer_extracts_timesale_equity_trade() -> None:
    quotes, trades, bars = SchwabStreamerClient._extract_records(
        {
            "data": [
                {
                    "service": "TIMESALE_EQUITY",
                    "content": [
                        {
                            "key": "SNBR",
                            "1": "1777410960123",
                            "2": "3.39",
                            "3": "600",
                            "4": "12345",
                        }
                    ],
                }
            ]
        }
    )

    assert quotes == []
    assert len(trades) == 1
    assert bars == []
    assert trades[0].symbol == "SNBR"
    assert trades[0].price == 3.39
    assert trades[0].size == 600
    assert trades[0].timestamp_ns == 1_777_410_960_123_000_000
    assert trades[0].cumulative_volume is None


def test_schwab_streamer_skips_levelone_trade_when_symbol_uses_timesale() -> None:
    quotes, trades, bars = SchwabStreamerClient._extract_records(
        {
            "data": [
                {
                    "service": "LEVELONE_EQUITIES",
                    "content": [
                        {
                            "key": "SNBR",
                            "1": "3.38",
                            "2": "3.40",
                            "3": "3.39",
                            "4": "10",
                            "5": "12",
                            "8": "6611",
                            "9": "100",
                            "35": "1777410960000",
                        }
                    ],
                }
            ]
        },
        timesale_symbols={"SNBR"},
    )

    assert len(quotes) == 1
    assert trades == []
    assert bars == []


def test_schwab_streamer_uses_subs_for_initial_chart_equity_subscription() -> None:
    client = SchwabStreamerClient(Settings())
    client._credentials = SchwabStreamerCredentials(
        socket_url="wss://example.test",
        customer_id="cust",
        correl_id="corr",
        channel="chan",
        function_id="func",
    )
    client._desired_chart_symbols = {"SNBR"}
    sent_payloads: list[dict[str, object]] = []

    class _FakeWebSocket:
        async def send(self, message: str) -> None:
            sent_payloads.append(json.loads(message))

    client._ws = _FakeWebSocket()

    asyncio.run(client._apply_subscription_delta())

    assert sent_payloads
    request = sent_payloads[0]["requests"][0]
    assert request["service"] == "CHART_EQUITY"
    assert request["command"] == "SUBS"
    assert request["parameters"]["keys"] == "SNBR"


def test_schwab_streamer_uses_subs_for_initial_timesale_equity_subscription() -> None:
    client = SchwabStreamerClient(Settings())
    client._credentials = SchwabStreamerCredentials(
        socket_url="wss://example.test",
        customer_id="cust",
        correl_id="corr",
        channel="chan",
        function_id="func",
    )
    client._desired_symbols = {"SNBR"}
    client._desired_timesale_symbols = {"SNBR"}
    sent_payloads: list[dict[str, object]] = []

    class _FakeWebSocket:
        async def send(self, message: str) -> None:
            sent_payloads.append(json.loads(message))

    client._ws = _FakeWebSocket()

    asyncio.run(client._apply_subscription_delta())

    assert len(sent_payloads) == 2
    timesale_request = sent_payloads[1]["requests"][0]
    assert timesale_request["service"] == "TIMESALE_EQUITY"
    assert timesale_request["command"] == "SUBS"
    assert timesale_request["parameters"]["keys"] == "SNBR"
    assert timesale_request["parameters"]["fields"] == SchwabStreamerClient.TIMESALE_EQUITY_FIELDS


def test_schwab_streamer_falls_back_to_levelone_trade_when_timesale_is_rejected() -> None:
    client = SchwabStreamerClient(Settings())
    client._desired_timesale_symbols = {"SNBR"}
    client._subscribed_timesale_symbols = {"SNBR"}
    quotes_seen = []
    trades_seen = []
    client._on_quote = quotes_seen.append
    client._on_trade = trades_seen.append

    async def _run() -> None:
        payload = {
            "response": [
                {
                    "service": "TIMESALE_EQUITY",
                    "command": "SUBS",
                    "content": [{"code": "11", "msg": "Service not available or temporary down."}],
                }
            ],
            "data": [
                {
                    "service": "LEVELONE_EQUITIES",
                    "content": [
                        {
                            "key": "SNBR",
                            "1": "3.38",
                            "2": "3.40",
                            "3": "3.39",
                            "4": "10",
                            "5": "12",
                            "8": "6611",
                            "9": "100",
                            "35": "1777410960000",
                        }
                    ],
                }
            ],
        }
        await client._handle_message(json.dumps(payload))

    asyncio.run(_run())

    assert len(quotes_seen) == 1
    assert len(trades_seen) == 1
    assert trades_seen[0].symbol == "SNBR"
    assert trades_seen[0].price == 3.39
    assert client._timesale_service_available is False
    assert client._subscribed_timesale_symbols == set()


def test_schwab_streamer_skips_timesale_subscription_when_service_is_unavailable() -> None:
    client = SchwabStreamerClient(Settings())
    client._credentials = SchwabStreamerCredentials(
        socket_url="wss://example.test",
        customer_id="cust",
        correl_id="corr",
        channel="chan",
        function_id="func",
    )
    client._desired_symbols = {"SNBR"}
    client._desired_timesale_symbols = {"SNBR"}
    client._timesale_service_available = False
    sent_payloads: list[dict[str, object]] = []

    class _FakeWebSocket:
        async def send(self, message: str) -> None:
            sent_payloads.append(json.loads(message))

    client._ws = _FakeWebSocket()

    asyncio.run(client._apply_subscription_delta())

    assert len(sent_payloads) == 1
    request = sent_payloads[0]["requests"][0]
    assert request["service"] == "LEVELONE_EQUITIES"
    assert request["command"] == "ADD"


def test_schwab_streamer_sync_subscriptions_swallows_clean_socket_close() -> None:
    client = SchwabStreamerClient(Settings())
    client._connected = True

    async def _raise_closed(*args, **kwargs):
        del args, kwargs
        raise ConnectionClosedOK(Close(1000, "OK"), Close(1000, "OK"), True)

    client._apply_subscription_delta = _raise_closed  # type: ignore[method-assign]

    asyncio.run(client.sync_subscriptions(["SNBR"]))

    assert client.connected is False
    assert client.last_error == ""


def test_schwab_tick_archive_records_and_loads_live_bars(tmp_path: Path) -> None:
    archive = SchwabTickArchive(tmp_path)
    archive.record_live_bar(
        LiveBarRecord(
            symbol="SNBR",
            interval_secs=60,
            open=3.31,
            high=3.45,
            low=3.29,
            close=3.39,
            volume=6611,
            timestamp=datetime(2026, 4, 28, 21, 16, tzinfo=UTC).timestamp(),
            trade_count=3,
        )
    )
    archive.record_live_bar(
        LiveBarRecord(
            symbol="SNBR",
            interval_secs=60,
            open=3.39,
            high=3.42,
            low=3.36,
            close=3.37,
            volume=2100,
            timestamp=datetime(2026, 4, 28, 21, 17, tzinfo=UTC).timestamp(),
            trade_count=2,
        )
    )
    archive.close()

    bars = load_recorded_live_bars(
        tmp_path,
        symbol="SNBR",
        day="2026-04-28",
        interval_secs=60,
    )

    assert len(bars) == 2
    assert bars[0].open == 3.31
    assert bars[0].close == 3.39
    assert bars[0].volume == 6611
    assert bars[0].trade_count == 3
    assert bars[1].close == 3.37


def test_schwab_1m_history_loader_prefers_recorded_live_bars(tmp_path: Path, monkeypatch) -> None:
    from project_mai_tai.services import strategy_engine_app as strategy_engine_module

    archive = SchwabTickArchive(tmp_path)
    archive.record_live_bar(
        LiveBarRecord(
            symbol="SNBR",
            interval_secs=60,
            open=3.31,
            high=3.45,
            low=3.29,
            close=3.39,
            volume=6611,
            timestamp=datetime(2026, 4, 28, 21, 16, tzinfo=UTC).timestamp(),
            trade_count=3,
        )
    )
    archive.record_live_bar(
        LiveBarRecord(
            symbol="SNBR",
            interval_secs=60,
            open=3.39,
            high=3.42,
            low=3.36,
            close=3.37,
            volume=2100,
            timestamp=datetime(2026, 4, 28, 21, 17, tzinfo=UTC).timestamp(),
            trade_count=2,
        )
    )
    archive.close()

    service = StrategyEngineService(
        settings=make_test_settings(
            redis_stream_prefix="test",
            strategy_macd_30s_enabled=False,
            strategy_polygon_30s_enabled=False,
            strategy_macd_1m_enabled=False,
            strategy_schwab_1m_enabled=True,
            schwab_tick_archive_enabled=True,
            schwab_tick_archive_root=str(tmp_path),
        ),
        redis_client=FakeRedis(),
        now_provider=lambda: datetime(2026, 4, 28, 21, 18, tzinfo=UTC),
    )

    async def _fake_fetch(*_args, **_kwargs):
        return []

    service._schwab_quote_poll_adapter.fetch_historical_bars = _fake_fetch
    monkeypatch.setattr(
        strategy_engine_module,
        "utcnow",
        lambda: datetime(2026, 4, 28, 21, 18, tzinfo=UTC),
    )

    bars = asyncio.run(
        service._load_schwab_history_bars(
            symbol="SNBR",
            interval_secs=60,
            required_bars=50,
        )
    )

    assert len(bars) == 2
    assert bars[0]["open"] == 3.31
    assert bars[0]["close"] == 3.39
    assert bars[1]["close"] == 3.37


def test_schwab_1m_history_loader_expands_beyond_current_session_when_warmup_is_short(monkeypatch) -> None:
    from project_mai_tai.services import strategy_engine_app as strategy_engine_module

    now = datetime(2026, 4, 30, 11, 20, tzinfo=UTC)
    service = StrategyEngineService(
        settings=make_test_settings(
            redis_stream_prefix="test",
            strategy_macd_30s_enabled=False,
            strategy_polygon_30s_enabled=False,
            strategy_macd_1m_enabled=False,
            strategy_schwab_1m_enabled=True,
        ),
        redis_client=FakeRedis(),
        now_provider=lambda: now,
    )

    call_starts: list[datetime] = []
    session_start = datetime(2026, 4, 30, 8, 0, tzinfo=UTC)
    short_bars = seed_trending_bars(count=18, interval_secs=60, start_timestamp=session_start.timestamp())
    broad_bars = seed_trending_bars(
        count=60,
        interval_secs=60,
        start_timestamp=(session_start - timedelta(days=1, minutes=10)).timestamp(),
    )

    async def _fake_fetch(symbol, *, interval_minutes, start_at, end_at, need_extended_hours_data):
        del symbol, interval_minutes, end_at, need_extended_hours_data
        call_starts.append(start_at)
        return short_bars if start_at == session_start else broad_bars

    service._schwab_quote_poll_adapter.fetch_historical_bars = _fake_fetch
    monkeypatch.setattr(strategy_engine_module, "utcnow", lambda: now)

    bars = asyncio.run(
        service._load_schwab_history_bars(
            symbol="FATN",
            interval_secs=60,
            required_bars=50,
        )
    )

    assert len(call_starts) == 2
    assert call_starts[0] == session_start
    assert call_starts[1] < session_start
    assert len(bars) == 60
    assert bars[-1]["timestamp"] == broad_bars[-1]["timestamp"]


def test_schwab_1m_history_loader_merges_persisted_older_bars_when_live_history_is_short(monkeypatch) -> None:
    from project_mai_tai.services import strategy_engine_app as strategy_engine_module

    now = datetime(2026, 4, 30, 11, 20, tzinfo=UTC)
    session_factory = build_test_session_factory()
    service = StrategyEngineService(
        settings=make_test_settings(
            redis_stream_prefix="test",
            strategy_macd_30s_enabled=False,
            strategy_polygon_30s_enabled=False,
            strategy_macd_1m_enabled=False,
            strategy_schwab_1m_enabled=True,
            dashboard_snapshot_persistence_enabled=False,
            strategy_history_persistence_enabled=False,
        ),
        redis_client=FakeRedis(),
        session_factory=session_factory,
        now_provider=lambda: now,
    )

    older_start = datetime(2026, 4, 29, 19, 0, tzinfo=UTC)
    with session_factory() as session:
        for index in range(40):
            bar_time = older_start + timedelta(minutes=index)
            close = 2.0 + index * 0.01
            session.add(
                StrategyBarHistory(
                    strategy_code="schwab_1m",
                    symbol="FATN",
                    interval_secs=60,
                    bar_time=bar_time,
                    open_price=close - 0.01,
                    high_price=close + 0.02,
                    low_price=close - 0.02,
                    close_price=close,
                    volume=10_000 + index,
                    trade_count=1,
                    position_state="flat",
                    position_quantity=0,
                    decision_status="evaluated",
                    decision_reason="seed",
                    decision_path="",
                    decision_score="",
                    decision_score_details="",
                    indicators_json={},
                )
            )
        session.commit()

    session_start = datetime(2026, 4, 30, 8, 0, tzinfo=UTC)
    short_bars = seed_trending_bars(count=18, interval_secs=60, start_timestamp=session_start.timestamp())

    async def _fake_fetch(symbol, *, interval_minutes, start_at, end_at, need_extended_hours_data):
        del symbol, interval_minutes, start_at, end_at, need_extended_hours_data
        return short_bars

    service._schwab_quote_poll_adapter.fetch_historical_bars = _fake_fetch
    monkeypatch.setattr(strategy_engine_module, "utcnow", lambda: now)

    bars = asyncio.run(
        service._load_schwab_history_bars(
            symbol="FATN",
            interval_secs=60,
            required_bars=50,
        )
    )

    assert len(bars) >= 50
    assert bars[0]["timestamp"] < session_start.timestamp()
    assert bars[-1]["timestamp"] == short_bars[-1]["timestamp"]


def test_schwab_1m_restore_prefers_fuller_archived_history_when_db_session_slice_is_short(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 4, 30, 13, 5, tzinfo=UTC)
    session_factory = build_test_session_factory()
    service = StrategyEngineService(
        settings=make_test_settings(
            redis_stream_prefix="test",
            strategy_macd_30s_enabled=False,
            strategy_polygon_30s_enabled=False,
            strategy_macd_1m_enabled=False,
            strategy_schwab_1m_enabled=True,
            schwab_tick_archive_enabled=True,
            schwab_tick_archive_root=str(tmp_path),
            dashboard_snapshot_persistence_enabled=False,
            strategy_history_persistence_enabled=False,
        ),
        redis_client=FakeRedis(),
        session_factory=session_factory,
        now_provider=lambda: now,
    )

    runtime = service.state.bots["schwab_1m"]
    runtime.set_watchlist(["FATN"])

    session_start = datetime(2026, 4, 30, 8, 0, tzinfo=UTC)
    archive = SchwabTickArchive(tmp_path)
    for index in range(60):
        bar_time = session_start + timedelta(minutes=index)
        base = 3.00 + (index * 0.01)
        archive.record_live_bar(
            LiveBarRecord(
                symbol="FATN",
                interval_secs=60,
                open=base,
                high=base + 0.03,
                low=base - 0.02,
                close=base + 0.01,
                volume=10_000 + index,
                timestamp=bar_time.timestamp(),
                trade_count=3,
            )
        )
    archive.close()

    with session_factory() as session:
        for index in range(35):
            bar_time = session_start + timedelta(minutes=25 + index)
            base = Decimal("3.00") + (Decimal("0.01") * Decimal(25 + index))
            session.add(
                StrategyBarHistory(
                    strategy_code="schwab_1m",
                    symbol="FATN",
                    interval_secs=60,
                    bar_time=bar_time,
                    open_price=base,
                    high_price=base + Decimal("0.03"),
                    low_price=base - Decimal("0.02"),
                    close_price=base + Decimal("0.01"),
                    volume=10_000 + index,
                    trade_count=3,
                    position_state="flat",
                    position_quantity=0,
                    decision_status="evaluated",
                    decision_reason="seed",
                    decision_path="",
                    decision_score="",
                    decision_score_details="",
                    indicators_json={},
                )
            )
        session.commit()

    service._restore_runtime_bar_history_from_database()

    builder = runtime.builder_manager.get_builder("FATN")
    assert builder is not None
    assert builder.get_bar_count() > 35
    assert builder.bars[0].timestamp == session_start.timestamp()
    assert any(bar.timestamp == (session_start + timedelta(minutes=5)).timestamp() for bar in builder.bars)
    assert "FATN" in runtime.last_indicators


@pytest.mark.parametrize(
    ("bot_code", "interval_secs", "settings_overrides"),
    [
        (
            "schwab_1m",
            60,
            {
                "strategy_macd_30s_enabled": False,
                "strategy_polygon_30s_enabled": False,
                "strategy_macd_1m_enabled": False,
                "strategy_schwab_1m_enabled": True,
            },
        ),
        (
            "macd_30s",
            30,
            {
                "strategy_macd_30s_enabled": True,
                "strategy_polygon_30s_enabled": False,
                "strategy_macd_1m_enabled": False,
                "strategy_schwab_1m_enabled": False,
            },
        ),
    ],
)
def test_schwab_native_bots_use_trade_based_extended_vwap_after_hours(
    tmp_path: Path,
    bot_code: str,
    interval_secs: int,
    settings_overrides: dict[str, object],
) -> None:
    now = datetime(2026, 4, 30, 20, 16, tzinfo=UTC)
    service = StrategyEngineService(
        settings=make_test_settings(
            redis_stream_prefix="test",
            schwab_tick_archive_enabled=True,
            schwab_tick_archive_root=str(tmp_path),
            dashboard_snapshot_persistence_enabled=False,
            strategy_history_persistence_enabled=False,
            **settings_overrides,
        ),
        redis_client=FakeRedis(),
        now_provider=lambda: now,
    )

    runtime = service.state.bots[bot_code]
    runtime.set_watchlist(["VSME"])

    session_start = datetime(2026, 4, 30, 20, 0, tzinfo=UTC)
    archive = SchwabTickArchive(tmp_path)
    bars: list[dict[str, float | int]] = []
    closes: list[float] = []
    for index in range(60):
        bar_time = session_start + timedelta(seconds=interval_secs * index)
        close_price = 1.00 + (index * 0.01)
        closes.append(close_price)
        bars.append(
            {
                "open": close_price - 0.01,
                "high": close_price + 0.02,
                "low": close_price - 0.02,
                "close": close_price,
                "volume": 100,
                "timestamp": bar_time.timestamp(),
                "trade_count": 1,
            }
        )
        trade_time = bar_time + timedelta(seconds=max(1, interval_secs // 2))
        archive.record_trade(
            TradeTickRecord(
                symbol="VSME",
                price=close_price,
                size=100,
                timestamp_ns=int(trade_time.timestamp() * 1_000_000_000),
            ),
            recorded_at_ns=int(trade_time.timestamp() * 1_000_000_000),
        )
    archive.close()

    runtime.seed_bars("VSME", bars)

    indicators = runtime.last_indicators["VSME"]
    expected_previous_vwap = sum(closes[:-1]) / len(closes[:-1])
    expected_current_vwap = sum(closes) / len(closes)

    assert float(indicators["extended_vwap"]) == pytest.approx(expected_current_vwap)
    assert float(indicators["vwap"]) == pytest.approx(expected_current_vwap)
    assert float(indicators["decision_vwap"]) == pytest.approx(expected_current_vwap)
    assert float(indicators["selected_vwap"]) == pytest.approx(expected_current_vwap)
    assert bool(indicators["price_above_vwap"]) is True
    assert bool(indicators["price_cross_above_vwap"]) is False
    assert float(indicators["vwap_dist_pct"]) == pytest.approx(
        ((closes[-1] - expected_current_vwap) / expected_current_vwap) * 100.0
    )
    assert expected_previous_vwap < expected_current_vwap


def test_polygon_30s_bot_does_not_mix_schwab_trade_vwap_after_hours(tmp_path: Path) -> None:
    now = datetime(2026, 4, 30, 20, 16, tzinfo=UTC)
    service = StrategyEngineService(
        settings=make_test_settings(
            redis_stream_prefix="test",
            schwab_tick_archive_enabled=True,
            schwab_tick_archive_root=str(tmp_path),
            dashboard_snapshot_persistence_enabled=False,
            strategy_history_persistence_enabled=False,
            strategy_macd_30s_enabled=False,
            strategy_polygon_30s_enabled=True,
            strategy_schwab_1m_enabled=False,
            strategy_macd_1m_enabled=False,
        ),
        redis_client=FakeRedis(),
        now_provider=lambda: now,
    )

    runtime = service.state.bots["polygon_30s"]
    runtime.set_watchlist(["VSME"])
    bars = [
        {
            "open": 1.00 + (index * 0.01) - 0.01,
            "high": 1.00 + (index * 0.01) + 0.02,
            "low": 1.00 + (index * 0.01) - 0.02,
            "close": 1.00 + (index * 0.01),
            "volume": 100,
            "timestamp": (datetime(2026, 4, 30, 20, 0, tzinfo=UTC) + timedelta(seconds=30 * index)).timestamp(),
            "trade_count": 1,
        }
        for index in range(60)
    ]

    runtime.seed_bars("VSME", bars)

    indicators = runtime.last_indicators["VSME"]
    assert float(indicators["extended_vwap"]) == pytest.approx(float(indicators["selected_vwap"]))


@pytest.mark.asyncio
async def test_schwab_1m_history_refresh_replays_missing_completed_bar(monkeypatch) -> None:
    from project_mai_tai.services import strategy_engine_app as strategy_engine_module

    now = datetime(2026, 4, 30, 10, 31, 28, tzinfo=UTC)
    start_at = datetime(2026, 4, 30, 9, 35, tzinfo=UTC).timestamp()
    service = StrategyEngineService(
        settings=make_test_settings(
            redis_stream_prefix="test",
            strategy_macd_30s_enabled=False,
            strategy_polygon_30s_enabled=False,
            strategy_macd_1m_enabled=False,
            strategy_schwab_1m_enabled=True,
            strategy_schwab_1m_broker_provider="schwab",
            dashboard_snapshot_persistence_enabled=False,
            strategy_history_persistence_enabled=False,
        ),
        redis_client=FakeRedis(),
        now_provider=lambda: now,
    )
    runtime = service.state.bots["schwab_1m"]
    runtime.set_watchlist(["SNBR"])
    runtime.seed_bars(
        "SNBR",
        seed_trending_bars(
            count=55,
            interval_secs=60,
            start_timestamp=start_at,
        ),
    )

    async def _fake_fetch(*_args, **_kwargs):
        return seed_trending_bars(
            count=56,
            interval_secs=60,
            start_timestamp=start_at,
        )

    service._schwab_quote_poll_adapter.fetch_historical_bars = _fake_fetch
    monkeypatch.setattr(strategy_engine_module, "utcnow", lambda: now)

    intents, refreshed = await service._refresh_stale_schwab_1m_history()

    assert intents == 0
    assert refreshed == 1
    assert runtime.builder_manager.get_builder("SNBR").get_bars_as_dicts()[-1]["timestamp"] == datetime(
        2026,
        4,
        30,
        10,
        30,
        tzinfo=UTC,
    ).timestamp()
    assert runtime.summary()["recent_decisions"][0]["last_bar_at"] == "2026-04-30T06:30:00-04:00"


@pytest.mark.asyncio
async def test_schwab_1m_history_refresh_ignores_prior_session_bars(monkeypatch) -> None:
    from project_mai_tai.services import strategy_engine_app as strategy_engine_module

    now = datetime(2026, 5, 1, 10, 31, 28, tzinfo=UTC)
    service = StrategyEngineService(
        settings=make_test_settings(
            redis_stream_prefix="test",
            strategy_macd_30s_enabled=False,
            strategy_polygon_30s_enabled=False,
            strategy_macd_1m_enabled=False,
            strategy_schwab_1m_enabled=True,
            strategy_schwab_1m_broker_provider="schwab",
            dashboard_snapshot_persistence_enabled=False,
            strategy_history_persistence_enabled=False,
        ),
        redis_client=FakeRedis(),
        now_provider=lambda: now,
    )
    runtime = service.state.bots["schwab_1m"]
    runtime.set_watchlist(["SNBR"])

    previous_session_bar = datetime(2026, 4, 30, 19, 58, tzinfo=UTC).timestamp()
    current_session_bar = datetime(2026, 5, 1, 10, 30, tzinfo=UTC).timestamp()

    async def _fake_fetch(*_args, **_kwargs):
        return [
            {
                "timestamp": previous_session_bar,
                "open": 10.0,
                "high": 10.1,
                "low": 9.9,
                "close": 10.05,
                "volume": 1_000,
                "trade_count": 10,
            },
            {
                "timestamp": current_session_bar,
                "open": 10.1,
                "high": 10.2,
                "low": 10.0,
                "close": 10.15,
                "volume": 1_200,
                "trade_count": 12,
            },
        ]

    service._schwab_quote_poll_adapter.fetch_historical_bars = _fake_fetch
    monkeypatch.setattr(strategy_engine_module, "utcnow", lambda: now)

    intents, refreshed = await service._refresh_stale_schwab_1m_history()

    assert intents == 0
    assert refreshed == 1
    bars = runtime.builder_manager.get_builder("SNBR").get_bars_as_dicts()
    assert len(bars) == 1
    assert bars[-1]["timestamp"] == current_session_bar


def test_schwab_1m_runtime_emits_signal_immediately_from_final_live_bar(monkeypatch) -> None:
    service = StrategyEngineService(
        settings=make_test_settings(
            redis_stream_prefix="test",
            strategy_macd_30s_enabled=False,
            strategy_polygon_30s_enabled=False,
            strategy_macd_1m_enabled=False,
            strategy_schwab_1m_enabled=True,
            strategy_schwab_1m_broker_provider="schwab",
        ),
        redis_client=FakeRedis(),
        now_provider=fixed_now,
    )
    runtime = service.state.bots["schwab_1m"]
    runtime.set_watchlist(["SNBR"])
    runtime.latest_quotes["SNBR"] = {"bid": 3.38, "ask": 3.39}
    runtime.seed_bars("SNBR", seed_trending_bars(count=55, interval_secs=60))

    captured: dict[str, int | float] = {}

    def fake_calculate(_bars):
        return {
            "price": 3.39,
            "bar_timestamp": 1_777_410_960.0,
        }

    def fake_check_entry(symbol, indicators, bar_index, position_tracker):
        del position_tracker
        captured["bar_index"] = bar_index
        captured["price"] = indicators["price"]
        return {
            "action": "BUY",
            "ticker": symbol,
            "path": "P1_CROSS",
            "price": indicators["price"],
            "score": 6,
            "score_details": "live_chart_bar",
        }

    monkeypatch.setattr(runtime.indicator_engine, "calculate", fake_calculate)
    monkeypatch.setattr(runtime.entry_engine, "check_entry", fake_check_entry)
    monkeypatch.setattr(
        runtime.entry_engine,
        "pop_last_decision",
        lambda _symbol: {"status": "signal", "reason": "P1_CROSS", "path": "P1_CROSS", "score": "6"},
    )

    intents = runtime.handle_live_bar(
        symbol="SNBR",
        open_price=3.31,
        high_price=3.45,
        low_price=3.29,
        close_price=3.39,
        volume=6611,
        timestamp=1_777_410_960.0,
        trade_count=1,
    )

    assert len(intents) == 1
    assert captured["bar_index"] == 56
    assert captured["price"] == 3.39
    assert intents[0].payload.symbol == "SNBR"
    assert intents[0].payload.reason == "ENTRY_P1_CROSS"


def test_schwab_1m_final_live_bar_uses_accumulated_trade_tick_count(monkeypatch) -> None:
    service = StrategyEngineService(
        settings=make_test_settings(
            redis_stream_prefix="test",
            strategy_macd_30s_enabled=False,
            strategy_polygon_30s_enabled=False,
            strategy_macd_1m_enabled=False,
            strategy_schwab_1m_enabled=True,
            strategy_schwab_1m_broker_provider="schwab",
        ),
        redis_client=FakeRedis(),
        now_provider=fixed_now,
    )
    runtime = service.state.bots["schwab_1m"]
    runtime.set_watchlist(["SNBR"])
    runtime.latest_quotes["SNBR"] = {"bid": 3.38, "ask": 3.39}
    runtime.seed_bars("SNBR", seed_trending_bars(count=55, interval_secs=60))

    # Force ticks down the live-aggregate short-circuit so the accumulator runs.
    monkeypatch.setattr(runtime, "_should_fallback_to_trade_ticks", lambda _symbol: False)
    monkeypatch.setattr(runtime, "_evaluate_intrabar_entry_from_trade_tick", lambda *_args, **_kwargs: [])

    bucket_start_seconds = 1_777_410_960.0
    for offset_ns in (
        int(0.10 * 1_000_000_000),
        int(15.5 * 1_000_000_000),
        int(45.2 * 1_000_000_000),
        int(58.9 * 1_000_000_000),
    ):
        runtime.handle_trade_tick(
            "SNBR",
            price=3.39,
            size=100,
            timestamp_ns=int(bucket_start_seconds * 1_000_000_000) + offset_ns,
            cumulative_volume=50_000,
        )

    runtime.handle_live_bar(
        symbol="SNBR",
        open_price=3.31,
        high_price=3.45,
        low_price=3.29,
        close_price=3.39,
        volume=6611,
        timestamp=bucket_start_seconds,
        trade_count=1,
    )

    persisted = runtime.builder_manager.get_builder("SNBR").bars[-1]
    assert persisted.trade_count == 4
    assert "SNBR" not in runtime._live_aggregate_trade_tick_counts


def test_schwab_1m_trade_tick_count_is_recorded_even_when_fallback_path_handles_tick(monkeypatch) -> None:
    # Regression: between CHART_EQUITY bars the live-aggregate freshness window
    # expires and ticks route through the native-builder fallback path. The
    # accumulator must still capture them so the eventual CHART bar gets a
    # real trade_count instead of falling back to the streamer default of 1.
    service = StrategyEngineService(
        settings=make_test_settings(
            redis_stream_prefix="test",
            strategy_macd_30s_enabled=False,
            strategy_polygon_30s_enabled=False,
            strategy_macd_1m_enabled=False,
            strategy_schwab_1m_enabled=True,
            strategy_schwab_1m_broker_provider="schwab",
        ),
        redis_client=FakeRedis(),
        now_provider=fixed_now,
    )
    runtime = service.state.bots["schwab_1m"]
    runtime.set_watchlist(["SNBR"])
    runtime.latest_quotes["SNBR"] = {"bid": 3.38, "ask": 3.39}
    runtime.seed_bars("SNBR", seed_trending_bars(count=55, interval_secs=60))

    # Pretend the live-aggregate stream has gone stale so ticks take the native
    # builder fallback path rather than the live-aggregate short-circuit. Stub
    # the builder + completed-bar evaluation so we can isolate just the counter.
    monkeypatch.setattr(runtime, "_should_fallback_to_trade_ticks", lambda _symbol: True)
    monkeypatch.setattr(runtime.builder_manager, "on_trade", lambda *_a, **_kw: [])
    monkeypatch.setattr(runtime, "_evaluate_intrabar_entry", lambda *_a, **_kw: [])

    bucket_start_seconds = 1_777_410_960.0
    for offset_ns in (
        int(0.10 * 1_000_000_000),
        int(20.0 * 1_000_000_000),
        int(40.0 * 1_000_000_000),
    ):
        runtime.handle_trade_tick(
            "SNBR",
            price=3.39,
            size=100,
            timestamp_ns=int(bucket_start_seconds * 1_000_000_000) + offset_ns,
            cumulative_volume=50_000,
        )

    assert runtime._live_aggregate_trade_tick_counts.get("SNBR", {}).get(bucket_start_seconds) == 3


def test_schwab_1m_final_live_bar_falls_back_when_no_ticks_seen() -> None:
    service = StrategyEngineService(
        settings=make_test_settings(
            redis_stream_prefix="test",
            strategy_macd_30s_enabled=False,
            strategy_polygon_30s_enabled=False,
            strategy_macd_1m_enabled=False,
            strategy_schwab_1m_enabled=True,
            strategy_schwab_1m_broker_provider="schwab",
        ),
        redis_client=FakeRedis(),
        now_provider=fixed_now,
    )
    runtime = service.state.bots["schwab_1m"]
    runtime.set_watchlist(["SNBR"])
    runtime.latest_quotes["SNBR"] = {"bid": 3.38, "ask": 3.39}
    runtime.seed_bars("SNBR", seed_trending_bars(count=55, interval_secs=60))

    runtime.handle_live_bar(
        symbol="SNBR",
        open_price=3.31,
        high_price=3.45,
        low_price=3.29,
        close_price=3.39,
        volume=6611,
        timestamp=1_777_410_960.0,
        trade_count=1,
    )

    persisted = runtime.builder_manager.get_builder("SNBR").bars[-1]
    assert persisted.trade_count == 1


def test_flat_symbol_schwab_resubscribe_interval_is_backed_off() -> None:
    service = StrategyEngineService(
        settings=Settings(
            redis_url="redis://localhost:6379/15",
            strategy_macd_30s_enabled=True,
            strategy_polygon_30s_enabled=False,
            strategy_macd_1m_enabled=False,
            strategy_schwab_1m_enabled=False,
        )
    )

    assert service._schwab_symbol_resubscribe_interval_seconds(has_open_position=True) == 5.0
    assert service._schwab_symbol_resubscribe_interval_seconds(has_open_position=False) == 45.0


def test_data_health_summary_is_degraded_for_flat_warning_symbol() -> None:
    state = StrategyEngineState(
        settings=Settings(
            strategy_macd_30s_enabled=True,
            strategy_polygon_30s_enabled=False,
            strategy_macd_1m_enabled=False,
            strategy_schwab_1m_enabled=False,
        )
    )

    runtime = state.bots["macd_30s"]
    runtime.apply_data_warning("ENVB", reason="quiet symbol")

    summary = runtime.data_health_summary()

    assert summary["status"] == "degraded"
    assert summary["halted_symbols"] == []
    assert summary["warning_symbols"] == ["ENVB"]
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
