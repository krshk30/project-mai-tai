"""DERIVED rate stress on actual gateway code, never recorded WS arrival proof."""

import asyncio
from collections import deque
import hashlib
import json
import math
import os
from pathlib import Path
import time
from types import SimpleNamespace

import pytest

from project_mai_tai.market_data import gateway, massive_provider
from tests.unit.test_momwarn1_gateway import event, service

DAYS = {"2026-09-18", "2026-09-24", "2026-10-02", "2026-10-05", "2026-10-06",
        "2026-10-07", "2026-10-08", "2026-10-09", "2026-10-12"}


@pytest.fixture
def derived_input():
    path = os.environ.get("MOMWARN_DERIVED_SUMMARY")
    if not path:
        pytest.skip("derived nine-day receipt not selected; no synthetic denominator")
    raw = Path(path).read_bytes()
    if len(raw) > 1024*1024:
        raise ValueError("derived summary byte bound")
    days = json.loads(raw)
    assert len(days) == 9 and {row["day"] for row in days} == DAYS
    for row in days:
        assert row["evidence_kind"] == "DERIVED_ALL_TAPE_PRINT_SYMBOL_SECOND_LOAD"
        assert row["symbols_downloaded"] > 0 and row["peak_symbols_per_second"] > 0
        encoded = Path(row["output_path"]).read_bytes()
        assert hashlib.sha256(encoded).hexdigest() == row["output_sha256"]
        counts = json.loads(encoded)
        assert max(counts["counts_per_second"]) == row["peak_symbols_per_second"]
        assert counts["seconds"] == len(counts["counts_per_second"])
        assert row["partial_observed_lower_bound"] == (row["day"] == "2026-10-12")
    return days, hashlib.sha256(raw).hexdigest()


@pytest.mark.asyncio
async def test_derived_three_times_max_observed_peak_actual_gateway_TQ_isolated(
    derived_input, monkeypatch, record_property,
):
    days, source_sha = derived_input
    maximum = max(row["peak_symbols_per_second"] for row in days)
    amount = 3*maximum
    assert amount <= 100000  # Test-memory bound, not a claimed provider capacity.
    templates = [row["derived_A_shape_template"] for row in days]
    # Control the legal PM clock only; all performance clocks remain native.
    from datetime import UTC, datetime
    clock = SimpleNamespace(now=lambda _: datetime(2026, 10, 12, 12, 51, 21, tzinfo=UTC))
    monkeypatch.setattr(gateway, "datetime", clock)
    monkeypatch.setattr(massive_provider, "datetime", clock)
    svc, provider, redis, warning_redis = service()  # Actual default bounded A queue4096.
    await svc.apply_subscription_event(event("v2", ["WFF"]))
    await svc.apply_subscription_event(event("momentum-warning", ["NEW"]))
    await svc._warning_monitor_once()
    assert svc._warning_active and svc._warning_queue.maxsize == 4096
    provider._on_trade = svc._enqueue_trade
    provider._on_quote = lambda row: svc._enqueue_live(svc._quote_queue, row)
    provider._on_agg = lambda _: pytest.fail("A wildcard reached general live bars")
    pending = {"T": deque(), "Q": deque()}
    samples = {"T": [], "Q": []}
    first_published = {}
    for kind, name in (("T", "_publish_trade_tick_safely"), ("Q", "_publish_quote_tick_safely")):
        original = getattr(svc, name)
        async def measured(record, kind=kind, original=original):
            await original(record)
            observed = time.monotonic_ns()
            callback = pending[kind].popleft()
            samples[kind].append(dict(callback_monotonic_ns=callback, published_monotonic_ns=observed,
                                      latency_ms=(observed-callback)/1e6))
            first_published.setdefault(kind, observed)
        setattr(svc, name, measured)
    entered, release, stop = asyncio.Event(), asyncio.Event(), asyncio.Event()
    warning_writes = []
    original_warning_xadd = warning_redis.xadd
    async def blocked_A(*args, **kwargs):
        warning_writes.append(dict(stream=args[0], options=kwargs))
        entered.set()
        await release.wait()
        return await original_warning_xadd(*args, **kwargs)
    warning_redis.xadd = blocked_A
    svc._warning_publish_task = asyncio.create_task(svc._warning_publish_loop(stop))
    live = asyncio.create_task(svc._stream_publish_loop(stop))
    turns = []
    async def ticker():
        while not stop.is_set():
            turns.append(time.monotonic_ns())
            await asyncio.sleep(0)
    pulse = asyncio.create_task(ticker())
    # Controlled bursts/SDK callback availability, not measured provider frame ordering.
    # Templates project one real tape print to valid A shape, not qualifying vendor OHLC.
    rows = [templates[index % len(templates)] for index in range(amount)]
    frames = []
    current, size = [], 2
    for row in rows:
        encoded = json.dumps(row, separators=(",", ":"))
        if size+len(encoded)+1 > 1024**2:
            frames.append("["+",".join(current)+"]")
            current, size = [], 2
        current.append(encoded)
        size += len(encoded)+1
    if current:
        frames.append("["+",".join(current)+"]")
    async def feed():
        for frame in frames:
            await provider._process_messages(frame)  # Actual bounded raw decode/SDK normalize.
    started = time.monotonic_ns()
    flood = asyncio.create_task(feed())
    try:
        await asyncio.wait_for(entered.wait(), timeout=1)
        # CONTROLLED watched callback already available at the first A-writer boundary;
        # this proves callback/queue progress, not tail-of-frame or historical TQ delivery.
        delivered = 0
        while not flood.done() and delivered < 64:
            pending["T"].append(time.monotonic_ns())
            pending["Q"].append(time.monotonic_ns())
            provider._handle_messages([
                SimpleNamespace(ev="T", symbol="WFF", price=13.35, size=360,
                                sip_timestamp=1791809480927834426, conditions=[12, 14, 41], exchange=8),
                SimpleNamespace(ev="Q", symbol="WFF", bid_price=13, ask_price=14),
            ])
            delivered += 1
            await asyncio.sleep(0)
        await flood
        while pending["T"] or pending["Q"]:
            await asyncio.sleep(0)
        latency_ms = (max(first_published.values())-started)/1e6
        assert any(row[1].get("event_type") == "trade_tick" for row in redis.entries)
        stall_ms = max((b-a for a, b in zip(turns, turns[1:])), default=0)/1e6
        quantiles = {}
        for kind in ("T", "Q"):
            values = sorted(row["latency_ms"] for row in samples[kind])
            assert len(values) == delivered and len(values) >= 2
            quantiles[kind] = dict(n=len(values), p50_ms=values[math.ceil(.5*len(values))-1],
                                  p95_ms=values[math.ceil(.95*len(values))-1],
                                  p99_ms=values[math.ceil(.99*len(values))-1], max_ms=values[-1])
            assert values[-1] < 50
        record_property("DERIVED_watched_TQ_observed_ms", latency_ms)
        record_property("DERIVED_loop_stall_ms", stall_ms)
        record_property("DERIVED_A_rows", amount)
        assert latency_ms < 50 and stall_ms < 50
        assert svc._warning_seq == amount and svc._warning_queue.qsize() <= 4096
        assert svc._warning_drop_count > 0  # Actual default4096 overflows at this derived burst.
        assert warning_writes and all(row["stream"].endswith(":momentum-warning-bars") for row in warning_writes)
        assert all("maxlen" in row["options"] for row in warning_writes)
        counters = svc.warning_metrics()
        assert counters["A_received"] == amount and counters["queue_drops"] > 0
        assert counters["dropped_total"] == svc._warning_drop_count
        assert provider._subscriptions == {"WFF"} and provider._warning_trade_symbols == {"NEW"}
        assert not svc._warning_revert_reason
        assert not any(row[1].get("event_type") in {"aggregate", "live_bar"} for row in redis.entries)
        print("MOMWARN_DERIVED_BURST="+json.dumps(dict(
            evidence_kind="DERIVED_3X_MAX_OBSERVED_TAPE_SYMBOL_SECOND_ENVELOPE",
            source_summary_sha256=source_sha, max_observed_peak=maximum, A_rows=amount,
            raw_frames=len(frames), watched_TQ_ms=latency_ms, loop_stall_ms=stall_ms,
            callback_to_Redis_completion_quantiles=quantiles, TQ_observations=samples,
            queue_bound=4096, A_dropped_total=svc._warning_drop_count,
            A_counters=counters, A_stream_writes=warning_writes,
            callback_arrival="CONTROLLED_available_at_first_A_writer_boundary_NOT_provider_order",
            transport_latency="UNMEASURED", provider_WS_capture=False,
        ), sort_keys=True))
    finally:
        stop.set()
        release.set()
        if not flood.done():
            flood.cancel()
        await asyncio.gather(flood, live, svc._warning_publish_task, pulse, return_exceptions=True)
