"""SPOF Workstream A (v2 follow-up) — schwab_1m_v2 loop resilience.

v2 runs independent asyncio tasks under a fire-and-forget run(), so its failure
mode is a SILENT dead task while the heartbeat task keeps publishing healthy.
This covers: per-task backstop containment, CancelledError propagation, the
escalation/self-clear of loop_health, the run() liveness supervisor catching a
dead task, the E1 callback reproduction (raise in _on_chart_bar), and the
fault-injection hook. New file — additive to the baseline.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from importlib import import_module

import pytest

from project_mai_tai.market_data.schwab_v2_loop_health import (
    LoopHealthTracker,
    run_resilient_loop,
)
from project_mai_tai.market_data.schwab_v2_rest_client import ChartBar, SchwabV2RestClient
from project_mai_tai.services.schwab_1m_v2_bot import SchwabV2BotService

_LOG = logging.getLogger("test_v2_loop_resilience")


def _settings(**kw):
    return import_module("project_mai_tai.settings").Settings(**kw)


def _now_ms() -> int:
    return int(datetime.now(UTC).timestamp() * 1000)


# --------------------------------------------------------------------------
# LoopHealthTracker
# --------------------------------------------------------------------------

def test_tracker_escalates_then_self_clears() -> None:
    t = LoopHealthTracker(persistent_failure_threshold=3, logger=_LOG)
    assert t.health == "healthy"
    t.record_failure("bar_loop")
    assert t.health == "recovering"
    t.record_failure("bar_loop")
    assert t.health == "recovering"
    t.record_failure("bar_loop")
    assert t.health == "degraded-persistent"
    t.record_success("bar_loop")
    assert t.health == "healthy"
    assert t.exception_total == 3


def test_tracker_details_dedicated_field() -> None:
    t = LoopHealthTracker(persistent_failure_threshold=2, logger=_LOG)
    t.record_failure("scanner")
    d = t.details()
    assert d["loop_health"] == "recovering"
    assert d["loop_exceptions_total"] == "1"
    assert "scanner" in d["loop_failing_tasks"]


def test_mark_task_died_escalates_and_dedupes() -> None:
    t = LoopHealthTracker(persistent_failure_threshold=3, logger=_LOG)
    t.mark_task_died("bar_loop", exc=RuntimeError("x"))
    assert t.health == "degraded-persistent"
    total = t.exception_total
    t.mark_task_died("bar_loop")  # already flagged → no double count
    assert t.exception_total == total


# --------------------------------------------------------------------------
# run_resilient_loop (the per-task backstop)
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_resilient_loop_contains_exception_and_continues() -> None:
    stop = asyncio.Event()
    t = LoopHealthTracker(persistent_failure_threshold=3, logger=_LOG)
    calls = {"n": 0}

    async def iteration() -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("boom")  # contained
        stop.set()  # second pass: end the loop cleanly

    await run_resilient_loop(
        stop_event=stop, tracker=t, name="x", iteration=iteration,
        backoff_secs=0.0, logger=_LOG,
    )
    assert calls["n"] >= 2  # loop survived the first raise and ran again
    assert t.exception_total == 1
    assert t.consecutive("x") == 0  # cleared by the clean pass


@pytest.mark.asyncio
async def test_resilient_loop_propagates_cancellederror() -> None:
    stop = asyncio.Event()
    t = LoopHealthTracker(persistent_failure_threshold=3, logger=_LOG)

    async def iteration() -> None:
        raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        await run_resilient_loop(
            stop_event=stop, tracker=t, name="x", iteration=iteration,
            backoff_secs=0.0, logger=_LOG,
        )
    assert t.exception_total == 0  # CancelledError is NOT a contained failure


# --------------------------------------------------------------------------
# E1 reproduction — raise in the _on_chart_bar callback
# --------------------------------------------------------------------------

async def _noop_quote(symbol, quote) -> None:  # noqa: ANN001
    return None


@pytest.mark.asyncio
async def test_bar_loop_pass_propagates_callback_exception() -> None:
    """E1: a raising _on_chart_bar must NOT be locally swallowed in the pass —
    it must propagate so the per-task backstop can contain it."""
    seen = {"cb": 0}

    async def on_bar(symbol, bar) -> None:  # noqa: ANN001
        seen["cb"] += 1
        raise RuntimeError("callback boom")

    client = SchwabV2RestClient(
        _settings(), on_chart_bar=on_bar, on_quote=_noop_quote,
        loop_health=LoopHealthTracker(persistent_failure_threshold=3, logger=_LOG),
    )
    client.set_desired_symbols({"AAA"})
    bar = ChartBar(symbol="AAA", open=1, high=1, low=1, close=1, volume=10, timestamp_ms=_now_ms())
    client._fetch_recent_closed_bars = lambda symbol, since: [bar]  # type: ignore[assignment]

    with pytest.raises(RuntimeError, match="callback boom"):
        await client._bar_loop_pass(0.0)
    assert seen["cb"] == 1


@pytest.mark.asyncio
async def test_bar_loop_backstop_contains_callback_exception() -> None:
    """E1 end-to-end: the bar loop SURVIVES a persistently-raising callback,
    records failures, escalates — and never exits on its own. Deterministic:
    the callback ends the loop on its 3rd raise (no polling race)."""
    tracker = LoopHealthTracker(persistent_failure_threshold=3, logger=_LOG)
    client = SchwabV2RestClient(
        _settings(strategy_schwab_1m_v2_loop_error_backoff_seconds=0.0),
        on_chart_bar=lambda s, b: None, on_quote=_noop_quote, loop_health=tracker,
    )
    seen = {"n": 0}

    async def on_bar(symbol, bar) -> None:  # noqa: ANN001
        seen["n"] += 1
        if seen["n"] >= 3:
            client._stop_event.set()  # let the loop exit after the 3rd failure
        raise RuntimeError("callback boom")

    client._on_chart_bar = on_bar  # type: ignore[assignment]
    client.set_desired_symbols({"AAA"})
    # Return a NEWER bar each poll so the callback fires every pass (the loop
    # only forwards bars strictly newer than the last seen timestamp).
    ts = {"v": _now_ms()}

    def _fetch(symbol, since):  # noqa: ANN001
        ts["v"] += 60_000
        return [ChartBar(symbol="AAA", open=1, high=1, low=1, close=1, volume=10, timestamp_ms=ts["v"])]

    client._fetch_recent_closed_bars = _fetch  # type: ignore[assignment]

    await asyncio.wait_for(client._bar_loop(), timeout=5.0)
    assert seen["n"] == 3  # loop stayed alive across the first two failures
    assert tracker.consecutive("bar_loop") == 3
    assert tracker.health == "degraded-persistent"


# --------------------------------------------------------------------------
# run() liveness supervisor — catches a silently dead task
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_liveness_loop_flags_dead_task() -> None:
    svc = SchwabV2BotService(
        settings=_settings(strategy_schwab_1m_v2_task_liveness_check_interval_seconds=0.02)
    )

    async def dies() -> None:
        raise RuntimeError("task died silently")

    dead = asyncio.create_task(dies())
    await asyncio.sleep(0.02)
    assert dead.done()

    svc._tasks = {"bar_loop": dead}
    liveness = asyncio.create_task(svc._task_liveness_loop())
    svc._tasks["liveness"] = liveness
    # The liveness check interval is floored to 1.0s, so wait past one cycle.
    await asyncio.sleep(1.2)
    svc._stop_event.set()
    await asyncio.wait_for(liveness, timeout=3.0)

    assert svc._loop_health.health == "degraded-persistent"


# --------------------------------------------------------------------------
# Fault-injection hook (the survival-test mechanism, on the E1 path)
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_fault_injection_raises_then_self_clears() -> None:
    svc = SchwabV2BotService(settings=_settings(strategy_schwab_1m_v2_loop_fault_injection_count=2))
    assert svc._loop_fault_injection_remaining == 2
    bar = ChartBar(symbol="AAA", open=1, high=1, low=1, close=1, volume=0, timestamp_ms=_now_ms())

    for _ in range(2):
        with pytest.raises(RuntimeError, match="FAULT-INJECTION"):
            await svc._handle_bar_from_rest("AAA", bar)

    assert svc._loop_fault_injection_remaining == 0
    # Injection exhausted: the next call no longer injects (proceeds normally;
    # volume=0 bar is not persisted, strategy.on_bar is internally guarded).
    await svc._handle_bar_from_rest("AAA", bar)  # must not raise
