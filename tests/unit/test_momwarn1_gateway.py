from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime
import json
from pathlib import Path
import time
from types import SimpleNamespace

from massive.websocket.models import EquityAgg
import pytest
from websockets.exceptions import ConnectionClosedError
from websockets.frames import Close

from project_mai_tai.events import MarketDataSubscriptionEvent, MarketDataSubscriptionPayload
from project_mai_tai.market_data import gateway, massive_provider
from project_mai_tai.market_data.gateway import MarketDataGatewayService
from project_mai_tai.market_data.massive_provider import MassiveTradeStream
from project_mai_tai.market_data.models import LiveBarRecord, QuoteTickRecord, TradeTickRecord
from project_mai_tai.market_data.momentum_warning_watchdog import (
    WarningHealth, WarningHealthReader, WarningWatchdog,
)
from project_mai_tai.settings import Settings
from project_mai_tai.strategy_core.time_utils import is_fillable_et_session
from test_market_data_gateway import FakeRedis, FakeSnapshotProvider, FakeReferenceCache


class FakeWS:
    def __init__(self):
        self.added = []
        self.removed = []

    def subscribe(self, *channels):
        self.added.extend(channels)

    def unsubscribe(self, *channels):
        self.removed.extend(channels)


@pytest.fixture(autouse=True)
def pm_clock(monkeypatch):
    clock = SimpleNamespace(now=lambda _: datetime(2026, 10, 12, 12, 51, 21, tzinfo=UTC))
    monkeypatch.setattr(gateway, "datetime", clock)
    monkeypatch.setattr(massive_provider, "datetime", clock)


def aggregate(**changes):
    # Documentation-shaped synthetic SDK event, never recorded WS peak evidence.
    fields = dict(ev="A", sym="WFF", o=13.1, h=13.4, l=13.0, c=13.35,
                  v=240, s=1791809480000, e=1791809481000)
    fields.update(changes)
    return EquityAgg.from_dict(fields)


def stream():
    provider = MassiveTradeStream("offline-test")
    provider._ws = FakeWS()
    provider._connected = True
    return provider


def service(**settings):
    provider = stream()
    redis = FakeRedis()
    warning_redis = FakeRedis()
    result = MarketDataGatewayService(
        Settings(_env_file=None, redis_stream_prefix="test", market_data_warmup_enabled=True,
                 market_data_momentum_warning_enabled=True, **settings), redis,
        trade_stream=provider, snapshot_provider=FakeSnapshotProvider(),
        reference_cache=FakeReferenceCache(), warning_redis_client=warning_redis,
        warning_health_reader=SimpleNamespace(read=healthy),
    )
    return result, provider, redis, warning_redis


def healthy():
    return WarningHealth(time.time_ns(), 1, 1, 0, 1, 4 * 1024**3)


async def arm_warning_with_three_clean_reads(svc):
    """Initial/new-day setup uses actual monitor reads, never policy pre-priming."""
    for index in range(3):
        await svc._warning_monitor_once()
        assert svc._warning_revert_reason is None
        assert svc._warning_active is (index == 2)


def event(owner, symbols, mode="replace"):
    return MarketDataSubscriptionEvent(source_service="offline-test", payload=MarketDataSubscriptionPayload(
        consumer_name=owner, symbols=symbols, mode=mode,
    ))


@pytest.mark.asyncio
async def test_initial_arm_helper_uses_three_actual_health_reads_not_constructor_priming():
    svc, provider, _, _ = service()
    reads = []
    def read():
        reads.append(time.time_ns())
        return healthy()
    svc._warning_health_reader = SimpleNamespace(read=read)
    assert not svc._warning_active and not reads and not provider._ws.added
    await arm_warning_with_three_clean_reads(svc)
    assert len(reads) == 3
    assert provider._ws.added == ["A.*"] and svc._warning_generation == 1


@pytest.mark.asyncio
async def test_warning_owner_T_only_cap_and_other_owner_union():
    svc, provider, redis, _ = service()
    await svc.apply_subscription_event(event("v2", ["WFF"]))
    normal_warmups = len(redis.entries)
    await svc.apply_subscription_event(event("momentum-warning", ["WFF", "NEW"]))
    assert len(redis.entries) == normal_warmups
    assert provider._subscriptions == {"WFF"}
    assert provider._warning_trade_symbols == {"NEW", "WFF"}
    assert "T.NEW" in provider._ws.added
    assert "Q.NEW" not in provider._ws.added
    await svc.apply_subscription_event(event("v2", [], "replace"))
    assert "Q.WFF" in provider._ws.removed
    assert "T.WFF" not in provider._ws.removed
    await svc.apply_subscription_event(event("orb", ["NEW"]))
    assert any(row[1]["event_type"] == "historical_bars" for row in redis.entries[normal_warmups:])
    await svc.apply_subscription_event(event("momentum-warning", []))
    assert "T.WFF" in provider._ws.removed
    assert "T.NEW" not in provider._ws.removed
    assert svc.active_symbols() == {"NEW"}
    with pytest.raises(ValueError, match="cap"):
        await svc.apply_subscription_event(event("momentum-warning", [f"X{i}" for i in range(17)]))
    assert svc.active_symbols() == {"NEW"}


@pytest.mark.asyncio
@pytest.mark.parametrize("symbol", ["*", "T.*", "Q.*", "A.*", "X,Y", "X Y"])
async def test_no_TQ_wildcard_even_in_saved_owner_request(symbol):
    svc, provider, redis, _ = service()
    with pytest.raises(ValueError):
        await svc.apply_subscription_event(event("momentum-warning", [symbol]))
    assert not provider._ws.added
    assert not redis.hashes


@pytest.mark.asyncio
@pytest.mark.parametrize("symbol", ["BRK.B", "BRK.A", "BF.B"])
async def test_default_OFF_normal_owners_accept_and_restore_stock_class_tickers(symbol):
    from massive import WebSocketClient
    provider = stream()
    redis = FakeRedis()
    settings = Settings(_env_file=None, redis_stream_prefix="test", market_data_warmup_enabled=False)
    assert not settings.market_data_momentum_warning_enabled
    svc = MarketDataGatewayService(settings, redis, snapshot_provider=FakeSnapshotProvider(),
        trade_stream=provider, reference_cache=FakeReferenceCache())
    await svc.apply_subscription_event(event("v2", [symbol.lower()]))
    assert svc.active_symbols() == {symbol}
    assert set(provider._ws.added) == {f"T.{symbol}", f"Q.{symbol}"}
    assert WebSocketClient._parse_subscription(f"T.{symbol}") == ["T", symbol]
    assert not provider._warning_trade_symbols
    restored_provider = stream()
    restored = MarketDataGatewayService(settings, redis, snapshot_provider=FakeSnapshotProvider(),
        trade_stream=restored_provider, reference_cache=FakeReferenceCache())
    await restored._restore_subscription_state()
    await restored._sync_provider_owners(restored._desired_symbols_by_consumer)
    assert restored.active_symbols() == {symbol}
    assert set(restored_provider._ws.added) == {f"T.{symbol}", f"Q.{symbol}"}


@pytest.mark.asyncio
@pytest.mark.parametrize("symbol", ["*", "T.*", "WFF,Q.*", "WFF,Q.NEW", "WFF T.NEW", "WFF\nQ.NEW"])
async def test_default_OFF_normal_owner_rejects_channel_injection_before_persistence(symbol):
    provider = stream()
    redis = FakeRedis()
    svc = MarketDataGatewayService(
        Settings(_env_file=None, market_data_warmup_enabled=False), redis,
        snapshot_provider=FakeSnapshotProvider(), trade_stream=provider,
        reference_cache=FakeReferenceCache(),
    )
    assert not svc.settings.market_data_momentum_warning_enabled
    with pytest.raises(ValueError):
        await svc.apply_subscription_event(event("v2", [symbol]))
    assert not redis.hashes and not provider._ws.added
    assert not svc.active_symbols()


@pytest.mark.asyncio
async def test_restore_warning_owner_never_warms_or_adds_Q():
    svc, _, redis, _ = service()
    await svc.apply_subscription_event(event("v2", ["WFF"]))
    await svc.apply_subscription_event(event("momentum-warning", ["WFF", "NEW"]))
    restarted, provider, _, _ = service()
    restarted.redis = redis
    await restarted._restore_subscription_state()
    await restarted._sync_provider_owners(restarted._desired_symbols_by_consumer)
    assert provider._subscriptions == {"WFF"}
    assert provider._warning_trade_symbols == {"WFF", "NEW"}
    assert set(provider._ws.added) == {"T.WFF", "Q.WFF", "T.NEW"}
    await restarted.apply_subscription_event(event("momentum-warning", []))
    assert "T.WFF" not in provider._ws.removed


@pytest.mark.asyncio
async def test_warning_off_restores_only_other_owners():
    svc, _, redis, _ = service()
    await svc.apply_subscription_event(event("v2", ["WFF"]))
    await svc.apply_subscription_event(event("momentum-warning", ["NEW"]))
    provider = stream()
    restarted = MarketDataGatewayService(Settings(_env_file=None, redis_stream_prefix="test"), redis,
        snapshot_provider=FakeSnapshotProvider(), trade_stream=provider, reference_cache=FakeReferenceCache())
    await restarted._restore_subscription_state()
    await restarted._sync_provider_owners(restarted._desired_symbols_by_consumer)
    assert restarted.active_symbols() == {"WFF"}
    assert not provider._warning_trade_symbols
    assert not provider._warning_aggregates_enabled


@pytest.mark.asyncio
async def test_A_exact_symbol_filter_separated_no_general_bars_or_TQ_firehose():
    provider = stream()
    bars, trades, quotes, warnings = [], [], [], []
    provider._on_agg, provider._on_trade, provider._on_quote = bars.append, trades.append, quotes.append
    provider.configure_momentum_warning(warnings.append, lambda _: None)
    await provider.sync_subscriptions(["WFF"])
    assert await provider.set_momentum_warning_aggregates(True)
    provider._handle_messages([aggregate(), aggregate(sym="UNWATCHED"),
        SimpleNamespace(ev="T", symbol="UNWATCHED", price=13),
        SimpleNamespace(ev="Q", symbol="UNWATCHED", bid_price=13, ask_price=14),
        SimpleNamespace(ev="T", symbol="WFF", price=13, size=1, timestamp=123, conditions=[12]),
        SimpleNamespace(ev="Q", symbol="WFF", bid_price=13, ask_price=14)])
    assert len(warnings) == 2
    assert len(trades) == len(quotes) == 1
    assert not bars
    assert warnings[0].start_ms == 1791809480000
    assert warnings[0].end_ms == 1791809481000
    assert warnings[0].provider_kind == "massive_ws_a"
    assert warnings[0].received_monotonic_ns > 0
    assert not hasattr(warnings[0], "sip_low_at")
    assert trades[0].conditions == ("12",)
    assert "A.*" in provider._ws.added
    assert "T.*" not in provider._ws.added and "Q.*" not in provider._ws.added
    provider._revert_warning("offline_control")
    provider._handle_messages([aggregate()])
    assert not bars


@pytest.mark.parametrize("changes", [dict(e=None), dict(s=None), dict(s=1), dict(e=1791809480000),
    dict(e=1791809482000), dict(o=float("nan")), dict(l=14), dict(v=-1),
    dict(s=1791809480000.5), dict(e=True), dict(e=1791809480001),
    dict(e=1791809480500), dict(e=1791809480998), dict(e=1791809480999),
    dict(e=1791809481001)])
def test_invalid_aggregate_never_fabricates_endpoints(changes):
    assert MassiveTradeStream._normalize_warning_aggregate(aggregate(**changes), "WFF") is None


@pytest.mark.asyncio
async def test_documented_exclusive_1000_bucket_preserves_raw_end_to_Redis():
    svc, provider, _, redis = service()
    await arm_warning_with_three_clean_reads(svc)
    message = aggregate(s=1610144868000, e=1610144869000)
    provider._handle_messages([message])
    stop = asyncio.Event()
    publisher = asyncio.create_task(svc._warning_publish_loop(stop))
    while not redis.entries:
        await asyncio.sleep(0)
    stop.set()
    await publisher
    payload = redis.entries[0][1]
    assert payload["start_ms"] == 1610144868000
    assert payload["end_ms"] == 1610144869000
    assert payload["end_ms"] - payload["start_ms"] == 1000
    assert not any(key in payload for key in ("sip_low_at", "sip_timestamp", "timestamp_ns"))


@pytest.mark.asyncio
async def test_A_dropoldest_only_and_contract_v1_strict_stream_bound():
    svc, provider, redis, warning_redis = service(market_data_momentum_warning_queue_maxsize=2,
                                                market_data_momentum_warning_stream_maxlen=3)
    await arm_warning_with_three_clean_reads(svc)
    normal_trade = TradeTickRecord("WFF", 13, 1)
    svc._trade_queue.put_nowait(normal_trade)
    for _ in range(5):
        provider._handle_messages([aggregate()])
    assert svc._warning_queue.qsize() == 2
    assert svc._warning_drop_count == 3
    counters = svc.warning_metrics()
    assert counters["queue_drops"] == counters["dropped_total"] == 3
    # Overflow alone drops oldest A; it is not a newly approved safety latch.
    assert svc._warning_active and svc._warning_revert_reason is None
    assert provider._ws.removed == []
    assert svc._trade_queue.get_nowait() == normal_trade
    stop = asyncio.Event()
    task = asyncio.create_task(svc._warning_publish_loop(stop))
    while len(warning_redis.entries) < 2:
        await asyncio.sleep(0)
    stop.set()
    await task
    assert not redis.entries
    stream_name, payload, kwargs = warning_redis.entries[0]
    assert stream_name == "test:momentum-warning-bars"
    assert payload["version"] == 1 and payload["event_type"] == "aggregate"
    assert payload["sequence"] == 4 and payload["dropped_total"] == 3
    assert payload["generation"] == 1 and payload["gateway_instance_id"]
    assert payload["published_utc_ns"] >= payload["received_utc_ns"]
    assert kwargs == dict(maxlen=3, approximate=False)


@pytest.mark.parametrize("iso,allowed", [
    ("2026-10-12T07:59:59+00:00", False), ("2026-10-12T08:00:00+00:00", True),
    ("2026-10-12T13:29:59+00:00", True), ("2026-10-12T13:30:00+00:00", False),
    ("2026-10-10T12:00:00+00:00", False), ("2026-11-26T12:00:00+00:00", False),
    ("2026-12-24T09:00:00+00:00", True), ("2026-12-24T14:30:00+00:00", False),
])
def test_pm_trading_days_and_DST(iso, allowed):
    assert is_fillable_et_session(datetime.fromisoformat(iso), 4, 9, end_minute=30) is allowed


@pytest.mark.asyncio
async def test_outside_window_unsubscribes_A_and_preserves_watched_owners(monkeypatch):
    svc, provider, _, _ = service()
    await svc.apply_subscription_event(event("v2", ["WFF"]))
    await arm_warning_with_three_clean_reads(svc)
    monkeypatch.setattr(massive_provider, "datetime", SimpleNamespace(now=lambda _: datetime(2026, 10, 12, 22, 0, tzinfo=UTC)))
    provider._handle_messages([aggregate()])
    assert provider._ws.removed == ["A.*"]
    assert provider._subscriptions == {"WFF"}
    assert svc._warning_revert_reason is None
    assert not provider._warning_failed


@pytest.mark.asyncio
@pytest.mark.parametrize("close_path", ["publisher", "provider", "monitor"])
async def test_daily_1800_close_drains_late_A_and_resumes_next_trading_04(monkeypatch, caplog, close_path):
    def set_clock(iso):
        fixed = SimpleNamespace(now=lambda _: datetime.fromisoformat(iso))
        monkeypatch.setattr(gateway, "datetime", fixed)
        monkeypatch.setattr(massive_provider, "datetime", fixed)
    set_clock("2026-10-12T21:59:59+00:00")
    svc, provider, _, redis = service()
    await svc.apply_subscription_event(event("v2", ["WFF"]))
    await svc.apply_subscription_event(event("momentum-warning", ["NEW"]))
    owners = {key: set(value) for key, value in svc._desired_symbols_by_consumer.items()}
    svc._trade_queue.put_nowait(TradeTickRecord("WFF", 13, 1))
    svc._quote_queue.put_nowait(QuoteTickRecord("WFF", 13, 14))
    await arm_warning_with_three_clean_reads(svc)
    provider._handle_messages([aggregate(), aggregate()])
    assert svc._warning_generation == 1
    # Stale pending A at the normal boundary is a discard, not a safety trip.
    seq, row = svc._warning_queue.get_nowait()
    svc._warning_queue.put_nowait((seq, replace(row, received_monotonic_ns=time.monotonic_ns()-3_000_000_000)))
    stop = asyncio.Event()
    publisher = None
    caplog.clear()
    set_clock("2026-10-12T22:00:00+00:00")
    if close_path == "publisher":
        publisher = asyncio.create_task(svc._warning_publish_loop(stop))
        while provider._warning_aggregates_enabled:
            await asyncio.sleep(0)
    elif close_path == "provider":
        provider._handle_messages([aggregate()])
    else:
        await svc._warning_monitor_once()
    provider._handle_messages([aggregate()])
    assert svc._warning_queue.empty() and svc._warning_drop_count == 2
    assert not svc._warning_active
    assert svc._warning_revert_reason is None and not provider._warning_failed
    assert svc.warning_metrics()["watchdog_revert_ns"] is None
    assert svc._trade_queue.qsize() == svc._quote_queue.qsize() == 1
    assert svc._bar_queue.empty()
    assert svc._desired_symbols_by_consumer == owners
    assert provider._subscriptions == {"WFF"} and provider._warning_trade_symbols == {"NEW"}
    assert provider._ws.removed == ["A.*"]
    set_clock("2026-10-13T08:00:00+00:00")
    await arm_warning_with_three_clean_reads(svc)
    assert svc._warning_active and svc._warning_generation == 2
    start_ms = int(datetime.fromisoformat("2026-10-13T08:00:00+00:00").timestamp() * 1000)
    provider._handle_messages([aggregate(s=start_ms, e=start_ms+1000)])
    if publisher is None:
        publisher = asyncio.create_task(svc._warning_publish_loop(stop))
    while not redis.entries:
        await asyncio.sleep(0)
    stop.set()
    await publisher
    assert redis.entries[0][1]["generation"] == 2
    assert redis.entries[0][1]["start_ms"] == start_ms
    assert provider._ws.added.count("A.*") == 2
    assert not any(record.exc_info or record.levelname in {"ERROR", "CRITICAL"}
                   or "AUTO-REVERT" in record.message for record in caplog.records)


@pytest.mark.asyncio
async def test_normal_close_cancels_only_inflight_A_write_writer_resumes_next_04(monkeypatch):
    svc, provider, _, redis = service()
    await arm_warning_with_three_clean_reads(svc)
    provider._handle_messages([aggregate()])
    entered, cancelled = asyncio.Event(), asyncio.Event()
    original_xadd = redis.xadd
    async def blocked(*args, **kwargs):
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise
    redis.xadd = blocked
    stop = asyncio.Event()
    publisher = asyncio.create_task(svc._warning_publish_loop(stop))
    await entered.wait()
    closed_clock = SimpleNamespace(now=lambda _: datetime(2026, 10, 12, 22, 0, tzinfo=UTC))
    monkeypatch.setattr(gateway, "datetime", closed_clock)
    monkeypatch.setattr(massive_provider, "datetime", closed_clock)
    await svc._warning_monitor_once()
    await cancelled.wait()
    while svc._warning_inflight is not None:
        await asyncio.sleep(0)
    assert not publisher.done()
    assert svc._warning_drop_count == 1
    assert svc._warning_revert_reason is None and not provider._warning_failed
    redis.xadd = original_xadd
    next_clock = SimpleNamespace(now=lambda _: datetime(2026, 10, 13, 8, 0, tzinfo=UTC))
    monkeypatch.setattr(gateway, "datetime", next_clock)
    monkeypatch.setattr(massive_provider, "datetime", next_clock)
    await arm_warning_with_three_clean_reads(svc)
    provider._handle_messages([aggregate()])
    while not redis.entries:
        await asyncio.sleep(0)
    stop.set()
    await publisher
    assert redis.entries[0][1]["generation"] == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["provider", "watchdog"])
async def test_safety_failure_remains_sticky_across_normal_close_and_next_04(monkeypatch, failure):
    svc, provider, _, _ = service()
    await arm_warning_with_three_clean_reads(svc)
    if failure == "provider":
        provider._revert_warning("invalid_aggregate")
    else:
        await svc._revert_warning("load")
    reason = svc._warning_revert_reason
    for clock in (datetime(2026, 10, 12, 22, 0, tzinfo=UTC), datetime(2026, 10, 13, 8, 0, tzinfo=UTC)):
        fixed = SimpleNamespace(now=lambda _: clock)
        monkeypatch.setattr(gateway, "datetime", fixed)
        monkeypatch.setattr(massive_provider, "datetime", fixed)
        await svc._warning_monitor_once()
    assert svc._warning_revert_reason == reason
    assert not svc._warning_active
    assert provider._ws.added.count("A.*") == 1
    if failure == "provider":
        assert provider._warning_failed


@pytest.mark.asyncio
async def test_next_window_reprimes_logs_and_sync_baseline_after_inactive_night(health_reader, monkeypatch):
    reader, path, directory = health_reader
    svc, provider, _, _ = service()
    # Legacy fixture is a log/reset component control, not actual live source setup.
    svc._warning_health_reader = SimpleNamespace(read=reader.read, reset_window=reader.reset_window)
    await arm_warning_with_three_clean_reads(svc)
    assert svc._warning_active and reader._logs
    closed = SimpleNamespace(now=lambda _: datetime(2026, 10, 12, 22, 0, tzinfo=UTC))
    monkeypatch.setattr(gateway, "datetime", closed)
    monkeypatch.setattr(massive_provider, "datetime", closed)
    await svc._warning_monitor_once()
    assert not reader._logs
    for name in reader.LOG_NAMES:
        (directory / name).write_text("ERROR during inactive night\n")
    path.write_text(json.dumps(dict(schema_version=1, observed_wall_ns=time.time_ns(),
        oms_sync_seq=100, oms_sync_seconds=1, v2_bar_lateness_seconds=0)))
    next_window = SimpleNamespace(now=lambda _: datetime(2026, 10, 13, 8, 0, tzinfo=UTC))
    monkeypatch.setattr(gateway, "datetime", next_window)
    monkeypatch.setattr(massive_provider, "datetime", next_window)
    await arm_warning_with_three_clean_reads(svc)
    assert svc._warning_active and svc._warning_generation == 2
    assert svc._warning_revert_reason is None and not provider._warning_failed


@pytest.mark.asyncio
async def test_closed_weekend_does_not_reenable_until_next_trading_04(monkeypatch):
    svc, provider, _, _ = service()
    await arm_warning_with_three_clean_reads(svc)
    for stamp in ("2026-10-16T22:00:00+00:00", "2026-10-17T08:00:00+00:00", "2026-10-18T08:00:00+00:00"):
        fixed = SimpleNamespace(now=lambda _: datetime.fromisoformat(stamp))
        monkeypatch.setattr(gateway, "datetime", fixed)
        monkeypatch.setattr(massive_provider, "datetime", fixed)
        await svc._warning_monitor_once()
        assert not svc._warning_active and svc._warning_revert_reason is None
    monday = SimpleNamespace(now=lambda _: datetime(2026, 10, 19, 8, 0, tzinfo=UTC))
    monkeypatch.setattr(gateway, "datetime", monday)
    monkeypatch.setattr(massive_provider, "datetime", monday)
    await arm_warning_with_three_clean_reads(svc)
    assert svc._warning_active and provider._ws.added.count("A.*") == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("policy", [False, True])
async def test_disconnect_or_1008_pauses_A_until_replacement_connection(policy):
    svc, provider, _, _ = service()
    await svc.apply_subscription_event(event("v2", ["WFF"]))
    await arm_warning_with_three_clean_reads(svc)
    first_ws = provider._ws
    exc = ConnectionClosedError(Close(1008, "rejected"), None, None) if policy else ConnectionResetError("offline disconnect")
    class BrokenWS(FakeWS):
        async def connect(self, _processor):
            provider._running = False
            raise exc
    ws = BrokenWS()
    provider._build_client = lambda: ws
    provider._running = True
    await provider._run_loop()
    assert "A.*" in ws.removed
    assert svc._warning_revert_reason is None
    assert svc._pauses.reason == ("policy_1008" if policy else "disconnect")
    assert not provider._warning_failed and not svc._warning_active
    assert provider._subscriptions == {"WFF"}
    assert set(ws.added) == {"T.WFF", "Q.WFF"}
    provider._ws, provider._connected = first_ws, True
    for index in range(3):
        await svc._warning_monitor_once()
        assert svc._warning_active is (index == 2)


@pytest.mark.asyncio
async def test_fair_live_loop_continuous_quote_and_bar_backlogs_cannot_starve_trades():
    svc, _, _, _ = service(market_data_publish_round_size=2)
    seen = []
    stop = asyncio.Event()
    trade = TradeTickRecord("WFF", 13, 1)
    quote = QuoteTickRecord("WFF", 13, 14)
    bar = LiveBarRecord("WFF", 60, 13, 14, 12, 13, 1, 123)
    async def trade_publish(_):
        seen.append("T")
        if seen.count("T") == 3:
            stop.set()
    async def refill_quote(_):
        seen.append("Q")
        svc._quote_queue.put_nowait(quote)
        if len(seen) > 30:
            raise AssertionError("unbounded quote drain starved trades")
    async def refill_bar(_):
        seen.append("B")
        svc._bar_queue.put_nowait(bar)
        if len(seen) > 30:
            raise AssertionError("unbounded bar drain starved trades")
    svc._publish_trade_tick_safely = trade_publish
    svc._publish_quote_tick_safely = refill_quote
    svc._publish_live_bar_safely = refill_bar
    for _ in range(3):
        svc._trade_queue.put_nowait(trade)
    svc._quote_queue.put_nowait(quote)
    svc._bar_queue.put_nowait(bar)
    await asyncio.wait_for(svc._stream_publish_loop(stop), timeout=1)
    assert seen[:9] == ["T", "Q", "B"] * 3


@pytest.mark.asyncio
async def test_huge_synthetic_SDK_batch_yields_and_does_not_wait_on_A_Redis(record_property):
    svc, provider, redis, warning_redis = service(market_data_momentum_warning_queue_maxsize=16)
    await svc.apply_subscription_event(event("v2", ["WFF"]))
    await arm_warning_with_three_clean_reads(svc)
    provider._on_trade = svc._enqueue_trade
    provider._on_quote = lambda r: svc._enqueue_live(svc._quote_queue, r)
    entered = asyncio.Event()
    release = asyncio.Event()
    async def blocked_A(*args, **kwargs):
        entered.set()
        await release.wait()
        return '1-0'  # A completed Redis XADD supplies an acknowledgement ID.
    warning_redis.xadd = blocked_A
    stop = asyncio.Event()
    svc._warning_publish_task = asyncio.create_task(svc._warning_publish_loop(stop))
    live = asyncio.create_task(svc._stream_publish_loop(stop))
    turns = []
    async def ticker():
        while not stop.is_set():
            turns.append(time.monotonic_ns())
            await asyncio.sleep(0)
    clock_task = asyncio.create_task(ticker())
    started = time.monotonic_ns()
    # 12,000 synthetic rows: a fairness control, NOT 3x a recorded/known peak.
    flood = asyncio.create_task(provider._process_messages([aggregate()] * 12000))
    await entered.wait()
    provider._handle_messages([SimpleNamespace(ev="T", symbol="WFF", price=13, size=1),
                              SimpleNamespace(ev="Q", symbol="WFF", bid_price=13, ask_price=14)])
    while not any(p[1].get("event_type") == "quote_tick" for p in redis.entries):
        await asyncio.sleep(0)
    elapsed_ms = (time.monotonic_ns() - started) / 1e6
    record_property("synthetic_watched_TQ_observed_ms", elapsed_ms)
    record_property("synthetic_A_rows", 12000)
    assert any(p[1].get("event_type") == "trade_tick" for p in redis.entries)
    await flood
    assert elapsed_ms < 50
    assert len(turns) >= 12000 // 64
    assert svc._warning_queue.qsize() <= 16
    assert svc._warning_drop_count > 0
    stop.set()
    release.set()
    await asyncio.gather(live, svc._warning_publish_task, clock_task)
    assert not svc._warning_revert_reason


@pytest.mark.parametrize("field,value,reason", [
    ("load_1m", 6.1, "load"), ("ram_available_bytes", 3 * 1024**3 - 1, "ram"),
    ("v2_bar_lateness_seconds", 5.1, "v2_bar_late"), ("new_live_error", True, "live_error"),
    ("observed_wall_ns", 1, "monitor_stale"), ("load_1m", float("nan"), "monitor_invalid"),
])
def test_watchdog_triggers_are_latched(field, value, reason):
    watchdog = WarningWatchdog()
    assert watchdog.check(replace(healthy(), **{field: value}), publish_lag_seconds=0) == reason
    assert watchdog.check(healthy(), publish_lag_seconds=0) == reason


def test_two_distinct_slow_syncs_duplicate_sample_not_second_sync_and_boundary_values():
    watchdog = WarningWatchdog()
    health = replace(healthy(), oms_sync_seconds=5.1)
    assert watchdog.check(health, publish_lag_seconds=2) is None
    assert watchdog.check(health, publish_lag_seconds=0) is None
    assert watchdog.check(replace(health, oms_sync_seq=2), publish_lag_seconds=0) == "oms_sync_slow_twice"
    watchdog = WarningWatchdog()
    assert watchdog.check(replace(healthy(), load_1m=6, ram_available_bytes=3 * 1024**3,
        v2_bar_lateness_seconds=5, oms_sync_seconds=5), publish_lag_seconds=2) is None
    assert watchdog.check(healthy(), publish_lag_seconds=2.01) == "publish_lag"


def test_two_slow_syncs_revert_even_with_fast_sync_between_and_missing_sync_fails_closed():
    watchdog = WarningWatchdog()
    assert watchdog.check(replace(healthy(), oms_sync_seconds=6), publish_lag_seconds=0) is None
    assert watchdog.check(replace(healthy(), oms_sync_seq=2), publish_lag_seconds=0) is None
    assert watchdog.check(replace(healthy(), oms_sync_seq=3, oms_sync_seconds=6), publish_lag_seconds=0) == "oms_sync_slow_twice"
    watchdog = WarningWatchdog()
    assert watchdog.check(healthy(), publish_lag_seconds=0) is None
    assert watchdog.check(replace(healthy(), oms_sync_seq=3), publish_lag_seconds=0) == "monitor_sync_gap"


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["unreadable", "timeout", "load", "bar", "errors", "lag", "sync"])
async def test_gateway_A_only_noise_pauses_three_clean_resume_and_harm_stays_sticky(failure):
    svc, provider, _, _ = service(market_data_momentum_warning_monitor_timeout_seconds=0.01)
    await svc.apply_subscription_event(event("v2", ["WFF"]))
    await svc.apply_subscription_event(event("momentum-warning", ["NEW"]))
    await arm_warning_with_three_clean_reads(svc)
    provider._handle_messages([aggregate()])
    def read():
        if failure == "unreadable":
            raise OSError("offline unreadable")
        if failure == "timeout":
            time.sleep(0.03)
        return replace(healthy(), **{
            "load": dict(load_1m=7), "bar": dict(v2_bar_lateness_seconds=6),
            "errors": dict(new_live_error=True), "sync": dict(oms_sync_seq=2, oms_sync_seconds=6),
        }.get(failure, {}))
    svc._warning_health_reader = SimpleNamespace(read=read)
    if failure == "lag":
        seq, record = svc._warning_queue.get_nowait()
        svc._warning_queue.put_nowait((seq, replace(record, received_monotonic_ns=time.monotonic_ns()-3_000_000_000)))
    await svc._warning_monitor_once()
    if failure == "sync":
        svc._warning_health_reader = SimpleNamespace(read=lambda: replace(healthy(), oms_sync_seq=3, oms_sync_seconds=6))
        await svc._warning_monitor_once()
    noise = failure in {"unreadable", "timeout", "bar", "lag"}
    if noise:
        assert svc._warning_revert_reason is None and not provider._warning_failed
        assert svc._pauses.metrics()["state"] == "paused"
    else:
        assert svc._warning_revert_reason
    assert provider._ws.removed == ["A.*"]
    assert provider._subscriptions == {"WFF"}
    assert provider._warning_trade_symbols == {"NEW"}
    assert svc._warning_queue.empty()
    assert svc.warning_metrics()["queue_drops"] == 0
    assert svc.warning_metrics()["dropped_total"] >= 1
    if failure == "timeout":
        # Main retains ONE timed-out read until it completes. Completing its
        # controlled task here is not a clean policy/monitor observation.
        pending = getattr(svc, "_warning_health_task", None)
        if pending is not None:
            await asyncio.wait_for(asyncio.shield(pending), timeout=1)
    svc._warning_health_reader = SimpleNamespace(read=healthy)
    await svc._warning_monitor_once()
    assert not svc._warning_active
    if noise:
        await svc._warning_monitor_once()
        assert not svc._warning_active
        await svc._warning_monitor_once()
        assert svc._warning_active and svc._warning_revert_reason is None
        assert provider._ws.added.count("A.*") == 2
        assert provider._subscriptions == {"WFF"}
        assert provider._warning_trade_symbols == {"NEW"}
    else:
        await svc._warning_monitor_once()
        await svc._warning_monitor_once()
        assert not svc._warning_active and svc._warning_revert_reason
        assert provider._ws.added.count("A.*") == 1


@pytest.mark.asyncio
async def test_no_fictional_subscription_confirmation_metrics_and_trade_conditions_preserved():
    svc, _, _, _ = service()
    await svc.apply_subscription_event(event("momentum-warning", ["WFF"]))
    assert svc.warning_metrics()["warning_trade_coverage"]["WFF"]["status"] == "requested"
    svc._enqueue_trade(TradeTickRecord("WFF", 13, 1, timestamp_ns=1791809480927000000,
                                     conditions=("12",), conditions_present=True))
    metric = svc.warning_metrics()
    coverage = metric["warning_trade_coverage"]["WFF"]
    assert coverage["status"] == "provider_event_observed"
    assert coverage["subscription_confirmed"] is None
    assert coverage["first_trade_sip_ns"] == 1791809480927000000
    assert metric["A_subscription_confirmed"] is None
    assert svc._trade_queue.get_nowait().conditions_present


def test_settings_default_off_and_bounds():
    defaults = Settings(_env_file=None)
    assert not defaults.market_data_momentum_warning_enabled
    assert defaults.market_data_momentum_warning_queue_maxsize == 8192
    for key, value in dict(market_data_momentum_warning_queue_maxsize=0,
                          market_data_momentum_warning_owner_cap=17,
                          market_data_publish_round_size=1000).items():
        with pytest.raises(ValueError):
            Settings(_env_file=None, **{key: value})


@pytest.fixture
def health_reader(tmp_path, monkeypatch):
    path = tmp_path / "health.json"
    path.write_text(json.dumps(dict(schema_version=1, observed_wall_ns=time.time_ns(),
        oms_sync_seq=1, oms_sync_seconds=1, v2_bar_lateness_seconds=0)))
    for name in WarningHealthReader.LOG_NAMES:
        (tmp_path / name).write_text("old ERROR before A activation\n")
    meminfo = tmp_path / "meminfo"
    meminfo.write_text("MemAvailable: 4194304 kB\n")
    reader = WarningHealthReader(str(path), str(tmp_path))
    original_open = reader._open_regular
    reader._open_regular = lambda source: original_open(meminfo if source == Path("/proc/meminfo") else source)
    monkeypatch.setattr("os.getloadavg", lambda: (1, 1, 1))
    return reader, path, tmp_path


def test_bounded_reader_tracks_only_new_live_errors_even_split_across_reads(health_reader):
    reader, _, directory = health_reader
    assert not reader.read().new_live_error
    log = directory / "oms.log"
    with log.open("a") as out:
        out.write("2026 INFO healthy\n2026 ERR")
    assert not reader.read().new_live_error
    with log.open("a") as out:
        out.write("OR live sync failed\n")
    health = reader.read()
    assert health.new_live_error
    assert health.ram_available_bytes == 4 * 1024**3
    assert not reader.read().new_live_error


@pytest.mark.asyncio
async def test_retired_paper_log_absence_does_not_disable_A_but_live_ORB_error_does(health_reader):
    reader, _, directory = health_reader
    assert set(reader.LOG_NAMES) == {
        "oms.log", "strategy.log", "schwab-1m-v2.log", "orb-schwab.log",
    }
    assert not (directory / "orb.log").exists()
    svc, provider, _, _ = service()
    # This isolates the existing log-scope component, not the new live-source setup.
    svc._warning_health_reader = SimpleNamespace(read=reader.read)
    await arm_warning_with_three_clean_reads(svc)
    assert svc._warning_active
    assert provider._ws.added == ["A.*"]
    (directory / "orb.log").write_text("ERROR retired paper observer\n")
    await svc._warning_monitor_once()
    assert svc._warning_active
    assert svc._warning_revert_reason is None
    with (directory / "orb-schwab.log").open("a") as output:
        output.write("ERROR live ORB\n")
    await svc._warning_monitor_once()
    assert svc._warning_revert_reason == "live_error"
    assert provider._ws.removed == ["A.*"]


@pytest.mark.parametrize("name", ["oms.log", "strategy.log", "schwab-1m-v2.log", "orb-schwab.log"])
def test_each_required_live_log_still_fails_closed_when_missing(health_reader, name):
    reader, _, directory = health_reader
    (directory / name).unlink()
    with pytest.raises(FileNotFoundError):
        reader.read()


@pytest.mark.parametrize("failure", ["health_missing", "health_large", "missing_duration", "invalid_nan",
    "log_missing", "log_truncated", "log_rotated", "log_overflow", "not_regular", "no_RAM"])
def test_unreadable_or_unbounded_monitor_evidence_is_unknown(health_reader, failure):
    reader, path, directory = health_reader
    reader.read()
    log = directory / "strategy.log"
    if failure == "health_missing":
        path.unlink()
    elif failure == "health_large":
        path.write_text(" " * (reader.MAX_HEALTH_BYTES + 1))
    elif failure in {"missing_duration", "invalid_nan"}:
        payload = json.loads(path.read_text())
        if failure == "missing_duration":
            payload.pop("oms_sync_seconds")
        else:
            payload["v2_bar_lateness_seconds"] = float("nan")
        path.write_text(json.dumps(payload))
    elif failure == "log_missing":
        log.unlink()
    elif failure == "log_truncated":
        log.write_text("")
    elif failure == "log_rotated":
        log.rename(directory / "rotated.log")
        log.write_text("new log\n")
    elif failure == "log_overflow":
        with log.open("a") as out:
            out.write("X" * (reader.MAX_LOG_BYTES + 1))
    elif failure == "not_regular":
        log.unlink()
        log.mkdir()
    else:
        (directory / "meminfo").write_text("MemFree: 99999 kB\n")
    if failure == "log_truncated":
        assert not reader.read().new_live_error
        assert reader.log_observability()["coverage_status"] == "GAP_UNOBSERVED_COPYTRUNCATE"
        with log.open("a") as out:
            out.write("ERROR retained after approved rebaseline\n")
        assert reader.read().new_live_error
    else:
        with pytest.raises((ValueError, OSError)):
            reader.read()


@pytest.mark.asyncio
async def test_slow_offloop_monitor_does_not_stall_live_publishing():
    import threading
    svc, _, redis, _ = service()
    entered, release = threading.Event(), threading.Event()
    def read():
        entered.set()
        release.wait(timeout=0.2)
        return healthy()
    svc._warning_health_reader = SimpleNamespace(read=read)
    monitor = asyncio.create_task(svc._warning_monitor_once())
    while not entered.is_set():
        await asyncio.sleep(0)
    svc._enqueue_trade(TradeTickRecord("WFF", 13, 1))
    stop = asyncio.Event()
    live = asyncio.create_task(svc._stream_publish_loop(stop))
    while not redis.entries:
        await asyncio.sleep(0)
    assert not monitor.done()
    release.set()
    await monitor
    stop.set()
    await live


@pytest.mark.asyncio
async def test_pending_A_write_cancelled_on_revert_and_live_quote_survives():
    svc, provider, redis, warning_redis = service()
    await arm_warning_with_three_clean_reads(svc)
    provider._handle_messages([aggregate()])
    entered = asyncio.Event()
    cancelled = asyncio.Event()
    async def hung(*args, **kwargs):
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise
    warning_redis.xadd = hung
    stop = asyncio.Event()
    svc._warning_publish_task = asyncio.create_task(svc._warning_publish_loop(stop))
    await entered.wait()
    await svc._revert_warning("live_error")
    await asyncio.gather(svc._warning_publish_task, return_exceptions=True)
    assert cancelled.is_set()
    assert provider._ws.removed == ["A.*"]
    assert not warning_redis.entries
    svc._quote_queue.put_nowait(QuoteTickRecord("WFF", 13, 14))
    live = asyncio.create_task(svc._stream_publish_loop(stop))
    while not redis.entries:
        await asyncio.sleep(0)
    stop.set()
    await live
    assert redis.entries[0][1]["event_type"] == "quote_tick"


@pytest.mark.asyncio
async def test_normal_A_live_path_unchanged_with_warning_disabled():
    provider = stream()
    bars = []
    provider._on_agg = bars.append
    await provider.sync_subscriptions(["WFF"])
    provider._handle_messages([aggregate()])
    assert len(bars) == 1
    assert bars[0].interval_secs == 1


@pytest.mark.asyncio
async def test_metrics_strict_separate_stream_and_new_instance_uuid():
    svc, _, _, redis = service()
    other, _, _, _ = service()
    metric = svc.warning_metrics()
    await svc._warning_publisher.publish_momentum_warning_metric(metric)
    name, payload, kwargs = redis.entries[0]
    assert name == "test:momentum-warning-metrics"
    assert payload["event_type"] == "gateway_sample"
    assert payload["version"] == 1
    assert metric["gateway_instance_id"] != other.warning_metrics()["gateway_instance_id"]
    assert kwargs == dict(maxlen=1000, approximate=False)


@pytest.mark.asyncio
async def test_subscription_update_retries_failed_wire_request_without_losing_diff():
    provider = stream()
    ws = provider._ws
    real_subscribe = ws.subscribe
    def broken(*_):
        raise RuntimeError("offline subscribe error")
    ws.subscribe = broken
    with pytest.raises(RuntimeError):
        await provider.sync_subscriptions(["WFF"])
    assert provider._subscriptions == set()
    ws.subscribe = real_subscribe
    await provider.sync_subscriptions(["WFF"])
    assert set(ws.added) == {"T.WFF", "Q.WFF"}


@pytest.mark.asyncio
async def test_legacy_explicit_A_bar_route_not_repurposed_by_warning_wildcard():
    provider = stream()
    provider._provider_aggregate_subscriptions_enabled = True
    bars = []
    provider._on_agg = bars.append
    provider.configure_momentum_warning(lambda _: None, lambda _: None)
    await provider.sync_subscriptions(["WFF"])
    with pytest.raises(ValueError, match="legacy"):
        await provider.set_momentum_warning_aggregates(True)
    provider._handle_messages([aggregate()])
    assert len(bars) == 1
    assert "A.*" not in provider._ws.added


def test_warning_sdk_disables_hidden_reconnect_only_when_configured(monkeypatch):
    import massive
    monkeypatch.setattr(massive, "WebSocketClient", lambda **options: options)
    provider = MassiveTradeStream("offline-test")
    assert "max_reconnects" not in provider._build_client()
    provider.configure_momentum_warning(lambda _: None, lambda _: None)
    assert provider._build_client()["max_reconnects"] == 0
    assert provider._build_client()["raw"] is True


@pytest.mark.asyncio
async def test_no_A_request_before_existing_SDK_socket_present():
    provider = stream()
    provider._ws.websocket = None
    provider.configure_momentum_warning(lambda _: None, lambda _: None)
    assert not await provider.set_momentum_warning_aggregates(True)
    assert not provider._ws.added


@pytest.mark.asyncio
async def test_real_SDK_fake_transport_disconnect_escapes_without_internal_reconnect(monkeypatch):
    import massive.websocket as sdk
    provider = MassiveTradeStream("offline-test")
    provider.configure_momentum_warning(lambda _: None, lambda _: None)
    ws = provider._build_client()
    ws.url = "ws://offline-fake"
    error = ConnectionClosedError(Close(1008, "offline rejection"), None, None)
    connects = []
    class Transport:
        count = 0
        async def send(self, _):
            pass
        async def recv(self):
            self.count += 1
            if self.count == 1:
                return '[{"ev":"status","status":"connected"}]'
            if self.count == 2:
                return '[{"ev":"status","status":"auth_success"}]'
            raise error
    async def fake_connect(*args, **kwargs):
        connects.append(args)
        yield Transport()
        raise AssertionError("SDK tried hidden reconnect")
    monkeypatch.setattr(sdk, "connect", fake_connect)
    with pytest.raises(ConnectionClosedError):
        await ws.connect(provider._process_messages)
    assert len(connects) == 1


@pytest.mark.asyncio
async def test_raw_SDK_normalization_is_chunked_and_preserves_real_trade_provenance():
    svc, provider, _, _ = service(market_data_momentum_warning_queue_maxsize=8)
    await provider.sync_subscriptions(["WFF"])
    provider._on_trade = svc._enqueue_trade
    provider._on_quote = lambda r: svc._enqueue_live(svc._quote_queue, r)
    await arm_warning_with_three_clean_reads(svc)
    fields = dict(ev="A", sym="WFF", o=13.1, h=13.4, l=13, c=13.35,
                  v=240, s=1791809480000, e=1791809481000)
    records = [fields] * 4096 + [dict(ev="T", sym="WFF", p=13.4, s=100,
        t=1791809480928000000, c=[12]), dict(ev="Q", sym="WFF", bp=13.4, ap=13.45)]
    other_turns = []
    finished = asyncio.Event()
    async def other_work():
        while not finished.is_set():
            other_turns.append(time.monotonic_ns())
            await asyncio.sleep(0)
    ticker = asyncio.create_task(other_work())
    await provider._process_messages(json.dumps(records))
    finished.set()
    await ticker
    assert len(other_turns) >= 4096 // 64
    assert svc._warning_queue.qsize() == 8
    trade = svc._trade_queue.get_nowait()
    assert trade.timestamp_ns == 1791809480928000000
    assert trade.conditions == ("12",) and trade.conditions_present
    assert svc._quote_queue.qsize() == 1
    await provider._process_messages('[{"ev":"status","status":"not_authorized"}]')
    assert svc._warning_revert_reason == "provider_error"
    assert provider._ws.removed == ["A.*"]
