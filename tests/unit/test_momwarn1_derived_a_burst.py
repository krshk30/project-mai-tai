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


async def _measure_phase(templates, amount, *, with_A):
    svc, provider, redis, warning_redis = service()  # Actual approved default A queue8192.
    await svc.apply_subscription_event(event("v2", ["WFF"]))
    await svc.apply_subscription_event(event("momentum-warning", ["NEW"]))
    await svc._warning_monitor_once()
    assert svc._warning_active and svc._warning_queue.maxsize == 8192
    assert amount <= svc._warning_queue.maxsize
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
    warning_write_count = 0
    max_A_completion_lag_ms = 0
    original_warning_xadd = warning_redis.xadd
    async def blocked_A(*args, **kwargs):
        nonlocal warning_write_count, max_A_completion_lag_ms
        write = dict(stream=args[0], options=kwargs)
        if write not in warning_writes:
            warning_writes.append(write)
        warning_write_count += 1
        received_ns = svc._warning_inflight.received_monotonic_ns
        entered.set()
        await release.wait()
        result = await original_warning_xadd(*args, **kwargs)
        max_A_completion_lag_ms = max(max_A_completion_lag_ms,
                                     (time.monotonic_ns()-received_ns)/1e6)
        return result
    warning_redis.xadd = blocked_A
    turns = []
    async def ticker():
        while not stop.is_set():
            turns.append(time.monotonic_ns())
            await asyncio.sleep(0)
    # Controlled bursts/SDK callback availability, not measured provider frame ordering.
    # Templates project one real tape print to valid A shape, not qualifying vendor OHLC.
    rows = [templates[index % len(templates)] for index in range(amount)] if with_A else []
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
    if not with_A:
        frames.append("[]")
    async def feed():
        for frame in frames:
            await provider._process_messages(frame)  # Actual bounded raw decode/SDK normalize.
        if not with_A:
            # No A writer exists in baseline; its empty parser completion is the
            # controlled ready boundary, not a simulated A write or subscriber ACK.
            entered.set()
    svc._warning_publish_task = asyncio.create_task(svc._warning_publish_loop(stop))
    live = asyncio.create_task(svc._stream_publish_loop(stop))
    pulse = asyncio.create_task(ticker())
    started = time.monotonic_ns()
    flood = asyncio.create_task(feed())
    try:
        await asyncio.wait_for(entered.wait(), timeout=1)
        # CONTROLLED watched callback already available at the first A-writer boundary;
        # this proves callback/queue progress, not tail-of-frame or historical TQ delivery.
        boundary_ns = time.monotonic_ns()
        callbacks_while_parser_active = 0
        delivered = 0
        # Identical64 callbacks/yields in both phases, independent of parser duration.
        # Do not truncate the baseline merely because its empty frame is already done.
        for _ in range(64):
            callbacks_while_parser_active += int(not flood.done())
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
        async def drain_watched():
            while pending["T"] or pending["Q"]:
                await asyncio.sleep(0)
        await asyncio.wait_for(drain_watched(), timeout=1)
        blocked_counters = svc.warning_metrics()
        assert blocked_counters["queue_drops"] == blocked_counters["dropped_total"] == 0
        assert blocked_counters["lag_ms"] < 2000
        release.set()
        async def drain_A():
            while svc._warning_published < (amount if with_A else 0):
                assert svc._warning_revert_reason is None
                await asyncio.sleep(0)
        await asyncio.wait_for(drain_A(), timeout=2)
        latency_ms = (max(first_published.values())-started)/1e6
        assert any(row[1].get("event_type") == "trade_tick" for row in redis.entries)
        stall_ms = max((b-a for a, b in zip(turns, turns[1:])), default=0)/1e6
        quantiles = {}
        for kind in ("T", "Q"):
            values = sorted(row["latency_ms"] for row in samples[kind])
            assert len(values) == delivered == 64
            quantiles[kind] = dict(n=len(values), p50_ms=values[math.ceil(.5*len(values))-1],
                                  p95_ms=values[math.ceil(.95*len(values))-1],
                                  p99_ms=values[math.ceil(.99*len(values))-1], max_ms=values[-1])
            assert values[-1] < 50
        assert latency_ms < 50 and stall_ms < 50
        counters = svc.warning_metrics()
        expected_A = amount if with_A else 0
        assert svc._warning_seq == counters["A_received"] == expected_A
        assert svc._warning_queue.qsize() == 0
        assert counters["queue_drops"] == counters["dropped_total"] == 0
        assert counters["A_published"] == expected_A
        assert counters["max_publish_lag_ms"] < 2000 and max_A_completion_lag_ms < 2000
        if with_A:
            assert callbacks_while_parser_active > 0
            assert warning_write_count == amount
            assert warning_writes and all(row["stream"].endswith(":momentum-warning-bars") for row in warning_writes)
            assert all("maxlen" in row["options"] for row in warning_writes)
        else:
            assert svc._warning_drop_count == counters["queue_drops"] == 0
            assert svc._warning_queue.qsize() == 0 and not warning_writes
        assert counters["dropped_total"] == svc._warning_drop_count
        assert provider._subscriptions == {"WFF"} and provider._warning_trade_symbols == {"NEW"}
        assert not svc._warning_revert_reason
        assert not any(row[1].get("event_type") in {"aggregate", "live_bar"} for row in redis.entries)
        return dict(
            phase="DERIVED_A_BURST" if with_A else "MATCHED_NO_A_BASELINE",
            A_rows=expected_A,
            raw_frames=len(frames), watched_TQ_ms=latency_ms, loop_stall_ms=stall_ms,
            first_TQ_since_controlled_ready_ms=(max(first_published.values())-boundary_ns)/1e6,
            callback_to_Redis_completion_quantiles=quantiles, TQ_observations=samples,
            callbacks_while_A_parser_active=callbacks_while_parser_active if with_A else 0,
            queue_bound=8192, A_dropped_total=svc._warning_drop_count,
            A_blocked_counters=blocked_counters, A_counters=counters,
            A_max_received_to_write_completion_lag_ms=max_A_completion_lag_ms,
            A_stream_writes=warning_writes, A_stream_write_count=warning_write_count,
            callback_arrival=("CONTROLLED_available_at_first_A_writer_boundary_NOT_provider_order"
                              if with_A else "CONTROLLED_after_empty_frame_parser_completion"),
        )
    finally:
        stop.set()
        release.set()
        if not flood.done():
            flood.cancel()
        await asyncio.gather(flood, live, svc._warning_publish_task, pulse, return_exceptions=True)


@pytest.mark.asyncio
async def test_derived_three_times_max_observed_peak_actual_gateway_TQ_isolated(
    derived_input, monkeypatch, record_property,
):
    days, source_sha = derived_input
    maximum = max(row["peak_symbols_per_second"] for row in days)
    amount = 3*maximum
    assert amount <= 8192  # Approved queue accommodates this observed envelope.
    templates = [row["derived_A_shape_template"] for row in days]
    # Control the legal PM clock only; all performance clocks remain native.
    from datetime import UTC, datetime
    clock = SimpleNamespace(now=lambda _: datetime(2026, 10, 12, 12, 51, 21, tzinfo=UTC))
    monkeypatch.setattr(gateway, "datetime", clock)
    monkeypatch.setattr(massive_provider, "datetime", clock)
    baseline = await _measure_phase(templates, amount, with_A=False)
    one_x = await _measure_phase(templates, maximum, with_A=True)
    burst = await _measure_phase(templates, amount, with_A=True)
    delta = {
        phase: {
            kind: {metric: measured["callback_to_Redis_completion_quantiles"][kind][metric]
                   - baseline["callback_to_Redis_completion_quantiles"][kind][metric]
                   for metric in ("p50_ms", "p95_ms", "p99_ms", "max_ms")}
            for kind in ("T", "Q")
        }
        for phase, measured in (("1x", one_x), ("3x", burst))
    }
    record_property("DERIVED_watched_TQ_observed_ms", burst["watched_TQ_ms"])
    record_property("DERIVED_loop_stall_ms", burst["loop_stall_ms"])
    record_property("DERIVED_A_rows", amount)
    print("MOMWARN_DERIVED_PAIRED="+json.dumps(dict(
        evidence_kind="DERIVED_3X_MAX_OBSERVED_TAPE_SYMBOL_SECOND_ENVELOPE_PAIRED_CONTROL",
        source_summary_sha256=source_sha, max_observed_peak=maximum,
        order=["NO_A_BASELINE", "DERIVED_A_1X", "DERIVED_A_3X"],
        baseline=baseline, one_x=one_x, burst=burst,
        burst_minus_baseline_ms=delta,
        loop_stall_delta_ms=burst["loop_stall_ms"]-baseline["loop_stall_ms"],
        model_scope="Same64 controlled T/Q callbacks, actual callback-to-publish; NOT frame-tail delivery",
        latency_equivalence="NOT_ESTABLISHED: descriptive single pair, no approved equivalence margin",
        transport_latency="UNMEASURED", provider_WS_capture=False,
    ), sort_keys=True))
