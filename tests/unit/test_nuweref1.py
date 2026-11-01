"""C12011 related cases; retained inputs are not native or five-second parity."""

import asyncio
from datetime import date, datetime
import hashlib
import inspect
import json
import logging
from pathlib import Path
from statistics import mean
from threading import Event, Thread
import time
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from project_mai_tai.market_data.massive_provider import MassiveSnapshotProvider
from project_mai_tai.services.strategy_engine_app import StrategyEngineService, StrategyEngineState
from project_mai_tai.settings import Settings
from project_mai_tai.strategy_core.config import MomentumAlertConfig, MomentumConfirmedConfig
from project_mai_tai.strategy_core.models import DaySnapshot, MarketSnapshot, MinuteSnapshot, ReferenceData
from project_mai_tai.strategy_core.momentum_alerts import MomentumAlertEngine
from project_mai_tai.strategy_core.momentum_confirmed import MomentumConfirmedScanner
from project_mai_tai.strategy_core import momentum_confirmed


NOW = datetime(2026, 10, 27, 11, 49, tzinfo=ZoneInfo("America/New_York"))
# Original provider70c26d16 artifact23/24 and accepted diagnostic a02307b9.
# These are real causal minute endpoints, not fabricated five-second snapshots.
NUWE_HISTORY = [
    (5.08, 3074269), (5.1899, 3545373), (4.9499, 3806222),
    (5.06, 4045578), (5.1, 4198774),
]
NUWE_PRIOR = {
    "2026-09-29": 16776.359186, "2026-09-30": 14082.001645,
    "2026-10-01": 25445.643873, "2026-10-02": 12055.959535,
    "2026-10-05": 19417.14557, "2026-10-06": 8044.875081,
    "2026-10-07": 14740.812863, "2026-10-08": 15589.179478,
    "2026-10-09": 17101.716097, "2026-10-12": 12187.2756,
    "2026-10-13": 19289.563043, "2026-10-14": 41837.369022,
    "2026-10-15": 10913.131418, "2026-10-16": 12454.301042,
    "2026-10-19": 7480.69352, "2026-10-20": 3196.759575,
    "2026-10-21": 10930.032097, "2026-10-22": 7518.000076,
    "2026-10-23": 132690.719949, "2026-10-26": 85457.922728,
}


class ReferenceRedis:
    """Controlled Redis SET NX/XX port only; contains no broker/native facts."""
    def __init__(self):
        self.values = {}
        self.calls = []

    async def set(self, key, value, *, nx=False, xx=False, ex=None):
        self.calls.append(("set", key, nx, xx, ex))
        assert ex == 172800
        if (nx and key in self.values) or (xx and key not in self.values):
            return False
        self.values[key] = value
        return True

    async def get(self, key):
        self.calls.append(("get", key))
        return self.values.get(key)


def snapshot(symbol="NUWE", *, price=5.36, volume=4727868):
    return MarketSnapshot(symbol, previous_close=3.0969,
                          day=DaySnapshot(close=price, volume=volume, high=price),
                          minute=MinuteSnapshot(close=price, accumulated_volume=volume))


def state(enabled):
    settings = Settings(
        _env_file=None, scanner_nuweref1_enabled=enabled, massive_api_key=None, taapi_secret=None,
        news_enabled=False,
        strategy_history_persistence_enabled=False, dashboard_snapshot_persistence_enabled=False,
        scanner_feed_retention_enabled=False, strategy_macd_30s_enabled=False,
        strategy_polygon_30s_enabled=False, strategy_schwab_1m_enabled=False,
        strategy_macd_1m_enabled=False, strategy_tos_enabled=False, strategy_runner_enabled=False,
        strategy_macd_30s_reclaim_enabled=False,
    )
    return StrategyEngineState(settings, now_provider=lambda: NOW)


def configure(target, redis, fetcher):
    target._nuweref1.redis = redis
    target._nuweref1.fetcher = fetcher


def diagnostic(reference, monkeypatch):
    engine = MomentumAlertEngine(MomentumAlertConfig(), scan_interval_secs=60, now_provider=lambda: NOW)
    engine.prefill_history([{"NUWE": point} for point in NUWE_HISTORY])
    scanner = MomentumConfirmedScanner(MomentumConfirmedConfig())
    monkeypatch.setattr(momentum_confirmed, "get_minutes_since_4am", lambda: 469)
    refs = {"NUWE": reference} if reference is not None else {}
    current = snapshot()
    return scanner.process_alerts(engine.check_alerts([current], refs), refs, {"NUWE": current})


@pytest.mark.asyncio
async def test_nuwe_real_inputs_on_recovers_off_does_not_confirm(monkeypatch):
    requests = []
    sdk = SimpleNamespace()

    def details(symbol, **kwargs):
        requests.append(("details", symbol, kwargs))
        return SimpleNamespace(share_class_shares_outstanding=4868247, weighted_shares_outstanding=405251)

    def grouped(**kwargs):
        requests.append(("grouped", kwargs))
        assert kwargs["adjusted"] is True and kwargs["include_otc"] is False
        return [SimpleNamespace(ticker="NUWE", volume=NUWE_PRIOR[kwargs["date"]])]

    sdk.get_ticker_details = details
    sdk.get_grouped_daily_aggs = grouped
    provider = MassiveSnapshotProvider("controlled-no-network")
    provider._client = sdk
    monkeypatch.setattr("project_mai_tai.market_data.massive_provider.time.sleep", lambda seconds: None)
    on, off = state(True), state(False)
    configure(on, ReferenceRedis(), provider.fetch_scanner_reference)
    on.process_snapshot_batch([snapshot()], {})
    await on._nuweref1.drain()
    on.process_snapshot_batch([snapshot()], {})
    off.process_snapshot_batch([snapshot()], {})
    assert on.reference_data["NUWE"].shares_outstanding == 4868247
    assert on.reference_data["NUWE"].avg_daily_volume == mean(NUWE_PRIOR.values())
    confirmed = diagnostic(on.reference_data["NUWE"], monkeypatch)
    assert len(confirmed) == 1
    assert confirmed[0]["confirmation_path"] == "PATH_C_EXTREME_MOVER"
    assert confirmed[0]["price"] == 5.36 and confirmed[0]["volume"] == 4727868
    assert diagnostic(off.reference_data.get("NUWE"), monkeypatch) == []
    assert len([item for item in requests if item[0] == "details"]) == 1
    assert len([item for item in requests if item[0] == "grouped"]) == 20
    await on._nuweref1.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["missing_shares", "exception"])
async def test_sofx_incomplete_stays_unscanned_one_log_no_retry(caplog, failure):
    calls = []
    provider = MassiveSnapshotProvider("controlled-no-network")
    def details(symbol, **kwargs):
        calls.append(symbol)
        if failure == "exception":
            raise RuntimeError("controlled_sdk_failure")
        return SimpleNamespace(share_class_shares_outstanding=None, weighted_shares_outstanding=None)
    provider._client = SimpleNamespace(get_ticker_details=details)
    target = state(True)
    redis = ReferenceRedis()
    configure(target, redis, provider.fetch_scanner_reference)
    current = snapshot("SOFX", price=7.22)
    first = target.process_snapshot_batch([current], {})
    await target._nuweref1.drain()
    second = target.process_snapshot_batch([current], {})
    await target._nuweref1.drain()
    assert calls == ["SOFX"]
    assert first["alerts"] == second["alerts"] == []
    assert first["newly_confirmed"] == second["newly_confirmed"] == []
    assert "SOFX" not in target.reference_data
    failures = [record for record in caplog.records if "[NUWEREF1-UNSCANNED]" in record.message]
    expected = "reference_incomplete" if failure == "missing_shares" else "fetch_or_store_failed"
    assert len(failures) == 1 and expected in failures[0].message
    await target._nuweref1.close()
    restarted = state(True)
    configure(restarted, redis, provider.fetch_scanner_reference)
    restarted.process_snapshot_batch([current], {})
    await restarted._nuweref1.drain()
    assert calls == ["SOFX"] and "SOFX" not in restarted.reference_data
    assert len([record for record in caplog.records if "[NUWEREF1-UNSCANNED]" in record.message]) == 1
    await restarted._nuweref1.close()


@pytest.mark.asyncio
async def test_data_present_para_shaped_on_off_decision_bytes_equal_zero_fetch(monkeypatch):
    calls = []
    redis = ReferenceRedis()
    on, off = state(True), state(False)
    configure(on, redis, lambda *args: calls.append(args))
    def unexpected(*args):
        raise AssertionError("data-present symbol entered missing-reference branch")
    monkeypatch.setattr(on._nuweref1, "observe", unexpected)
    reference = {"PARA": ReferenceData(4508967, 38031.80006715)}
    current = snapshot("PARA", price=3.50, volume=699136)
    for _ in range(3):
        left = on.process_snapshot_batch([current], reference)
        right = off.process_snapshot_batch([current], reference)
        assert json.dumps(left, sort_keys=True, default=str).encode() == json.dumps(right, sort_keys=True, default=str).encode()
    assert calls == [] and redis.calls == [] and on._nuweref1._tasks == set()
    assert on._nuweref1._attempted == set()
    await on._nuweref1.close()


@pytest.mark.asyncio
async def test_same_symbol_twice_one_attempt_and_restart_reuses_durable_result():
    calls = []
    redis = ReferenceRedis()
    def fetch(symbol, day):
        calls.append((symbol, day))
        return ReferenceData(4868247, mean(NUWE_PRIOR.values()))
    first = state(True)
    configure(first, redis, fetch)
    first.process_snapshot_batch([snapshot()], {})
    first.process_snapshot_batch([snapshot()], {})
    await first._nuweref1.drain()
    first.process_snapshot_batch([snapshot()], {})
    await first._nuweref1.close()
    restarted = state(True)
    configure(restarted, redis, fetch)
    restarted.process_snapshot_batch([snapshot()], {})
    await restarted._nuweref1.drain()
    restarted.process_snapshot_batch([snapshot()], {})
    assert calls == [("NUWE", date(2026, 10, 27))]
    assert restarted.reference_data["NUWE"] == first.reference_data["NUWE"]
    await restarted._nuweref1.close()


@pytest.mark.timing
@pytest.mark.asyncio
async def test_slow_five_second_fetch_does_not_delay_snapshot_handler():
    started = Event()
    finished = Event()
    calls = []
    def fetch(symbol, day):
        calls.append(symbol)
        started.set()
        time.sleep(5.0)
        finished.set()
        return ReferenceData(4868247, mean(NUWE_PRIOR.values()))
    target = state(True)
    configure(target, ReferenceRedis(), fetch)
    begin = time.perf_counter()
    target.process_snapshot_batch([snapshot()], {})
    assert time.perf_counter() - begin < 0.5
    assert await asyncio.to_thread(started.wait, 2.0)
    assert not finished.is_set()
    begin = time.perf_counter()
    target.process_snapshot_batch([snapshot()], {})
    assert time.perf_counter() - begin < 0.5
    assert not finished.is_set()
    await target._nuweref1.drain()
    assert finished.is_set() and calls == ["NUWE"]
    await target._nuweref1.close()


def test_own_flag_default_off_and_original_nightly_builder_unchanged():
    assert Settings.model_fields["scanner_nuweref1_enabled"].default is False
    path = Path(__file__).resolve().parents[2] / "src/project_mai_tai/market_data/reference_cache.py"
    assert hashlib.sha256(path.read_bytes()).hexdigest() == "11e781eb86c27b094b85d731c3ea514f85664d03852effacd8e00e3e15bfa2ef"


def test_off_snapshot_body_retains_exact_20dc_statements():
    # Remove ONLY independent default-off hooks and identity aliases, then compare
    # exact original nonblank statement bytes (not a production-derived oracle).
    source = inspect.getsource(StrategyEngineState.process_snapshot_batch)
    kept = []
    skipped_indent = None
    for line in source.splitlines():
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip())
        if skipped_indent is not None:
            if indent > skipped_indent:
                continue
            skipped_indent = None
        if line.strip() == 'if getattr(self.settings, "scanner_nuweref1_enabled", False):':
            skipped_indent = indent
            continue
        if line.strip() in {"scanner_snapshots = filtered_snapshots", "replay_alerts = list(self.recent_alerts)"}:
            continue
        # Independent session-day capture flag adds only these two operands
        # inside its existing default-off observation hook (61a260785).
        if line.strip() in {'session_day_enabled=getattr(',
                'self.settings, "scanner_confirmed_capture_session_day_enabled", False),'}:
            continue
        line = line.replace("scanner_snapshots", "filtered_snapshots")
        if line.strip() == "replay_alerts,":
            line = line.replace("replay_alerts,", "list(self.recent_alerts),")
        kept.append(line)
    canonical = ("\n".join(kept) + "\n").encode()
    assert hashlib.sha256(canonical).hexdigest() == "c2e87cc03dc3fff13dbe2f43c883f01a69191a86272fd22d3e2da8271d291cc8"
    assert not hasattr(state(False), "_nuweref1")


@pytest.mark.asyncio
async def test_no_reference_top_gainers_on_off_equal_before_fetch(monkeypatch):
    # Absent reference naturally yields no TOPGAINERS row. A partial reference
    # can supply RVOL without float; that existing TOPGAINERS behavior stays.
    for refs in ({}, {"NUWE": ReferenceData(0, 10000)}):
        on, off = state(True), state(False)
        calls, seen = [], []
        configure(on, ReferenceRedis(), lambda *args: calls.append(args))
        original_update = on.top_gainers_tracker.update

        def observe_original(snapshots, reference_data, **kwargs):
            seen.append([item.ticker for item in snapshots])
            return original_update(snapshots, reference_data, **kwargs)

        monkeypatch.setattr(on.top_gainers_tracker, "update", observe_original)
        current = snapshot("NUWE")
        current.todays_change_percent = 73.08
        # No yield: background work cannot have supplied any new reference yet.
        left = on.process_snapshot_batch([current], refs)
        right = off.process_snapshot_batch([current], refs)
        assert calls == [] and seen == [["NUWE"]]
        assert left["top_gainers"] == right["top_gainers"]
        if refs:
            assert any(row["ticker"] == "NUWE" for row in left["top_gainers"])
        else:
            assert left["top_gainers"] == []
        assert left["five_pillars"] == right["five_pillars"]
        assert left["alerts"] == right["alerts"]
        await on._nuweref1.close()


@pytest.mark.timing
@pytest.mark.asyncio
async def test_hung_sixty_second_readonly_fetch_shutdown_bounded_and_cancellable(caplog, monkeypatch):
    # A real daemon worker blocks for up to60s; fixture releases and joins it
    # after shutdown assertions so the test never leaks controlled work.
    for cancel_close in (False, True):
        started, release, finished = Event(), Event(), Event()
        workers = []

        def owned_thread(**kwargs):
            worker = Thread(**kwargs)
            workers.append(worker)
            return worker

        monkeypatch.setattr("project_mai_tai.services.scanner_reference_recovery.Thread", owned_thread)

        def fetch(symbol, day):
            started.set()
            try:
                release.wait(60.0)
                return ReferenceData(4868247, mean(NUWE_PRIOR.values()))
            finally:
                finished.set()

        target = state(True)
        configure(target, ReferenceRedis(), fetch)
        teardown = []

        async def heartbeat(status):
            teardown.append(status)

        async def stop_stream():
            teardown.append("stream_stopped")

        async def close_redis():
            teardown.append("redis_closed")

        controlled_service = SimpleNamespace(
            settings=SimpleNamespace(scanner_nuweref1_enabled=True, strategy_persist_offload_enabled=False),
            state=target, logger=logging.getLogger("nuweref1-shutdown-control"),
            _publish_heartbeat=heartbeat,
            _flush_pending_snapshots=lambda **kwargs: teardown.append("snapshots_flushed"),
            _schwab_stream_client=SimpleNamespace(stop=stop_stream),
            _schwab_tick_archive=SimpleNamespace(close=lambda: teardown.append("archive_closed")),
            redis=SimpleNamespace(aclose=close_redis),
        )
        try:
            target.process_snapshot_batch([snapshot()], {})
            assert await asyncio.to_thread(started.wait, 2.0)
            begin = time.perf_counter()
            closing = asyncio.create_task(
                target._nuweref1.close() if cancel_close
                else StrategyEngineService._shutdown_cleanup(controlled_service)
            )
            if cancel_close:
                await asyncio.sleep(0)
                closing.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await asyncio.wait_for(closing, timeout=1.0)
                assert time.perf_counter() - begin < 1.0
            else:
                await asyncio.wait_for(closing, timeout=19.0)
                assert time.perf_counter() - begin < 20.0
                await target._nuweref1.close()
                logs = [record for record in caplog.records
                        if "[NUWEREF1-SHUTDOWN-TIMEOUT]" in record.message]
                assert len(logs) == 1
                assert teardown == ["stopping", "snapshots_flushed", "stream_stopped", "archive_closed", "redis_closed"]
            assert not finished.is_set()
            assert "NUWE" not in target.reference_data
        finally:
            release.set()
            assert await asyncio.to_thread(finished.wait, 2.0)
            for worker in workers:
                await asyncio.to_thread(worker.join, 2.0)
                assert not worker.is_alive()
            await target._nuweref1.close()


@pytest.mark.asyncio
async def test_three_missing_names_share_twenty_adjusted_grouped_requests(monkeypatch):
    requests = []
    symbols = ("NUWE", "PARA", "TEST")

    def details(symbol, **kwargs):
        requests.append(("details", symbol))
        return SimpleNamespace(share_class_shares_outstanding=4868247)

    def grouped(**kwargs):
        requests.append(("grouped", kwargs["date"]))
        assert kwargs["adjusted"] is True and kwargs["include_otc"] is False
        assert kwargs["date"] < "2026-10-27"
        return [SimpleNamespace(ticker=symbol, volume=1000) for symbol in symbols]

    provider = MassiveSnapshotProvider("controlled-no-network")
    provider._client = SimpleNamespace(get_ticker_details=details, get_grouped_daily_aggs=grouped)
    monkeypatch.setattr("project_mai_tai.market_data.massive_provider.time.sleep", lambda seconds: None)
    target = state(True)
    configure(target, ReferenceRedis(), provider.fetch_scanner_reference)
    snapshots = [snapshot(symbol) for symbol in symbols]
    target.process_snapshot_batch(snapshots, {})
    await target._nuweref1.drain()
    target.process_snapshot_batch(snapshots, {})
    assert set(target.reference_data) == set(symbols)
    assert all(target.reference_data[symbol].avg_daily_volume == 1000 for symbol in symbols)
    assert [item[1] for item in requests if item[0] == "details"] == list(symbols)
    grouped_days = [item[1] for item in requests if item[0] == "grouped"]
    assert len(grouped_days) == len(set(grouped_days)) == 20
    target.process_snapshot_batch(snapshots, {})
    await target._nuweref1.drain()
    assert len(requests) == 23
    await target._nuweref1.close()
