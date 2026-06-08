"""SPOF Workstream A — strategy-engine main-loop resilience.

Covers the fix for the 2026-06-03 (dead Schwab token) and 2026-06-07
(streamer-side RuntimeError) zombie outages: an uncaught exception from a
Schwab-touching loop step must NOT end the main loop. It must be contained
(Layer 1) or backstopped (Layer 2), surfaced via the dedicated main_loop_health
heartbeat field, and escalate to degraded-persistent after N consecutive
failures — while CancelledError still propagates so shutdown works.

New file (additive to the regression baseline; does not touch any existing
test module).
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from importlib import import_module

import pytest

from project_mai_tai.services.strategy_engine_app import StrategyEngineService


class _FakeRedis:
    def __init__(self) -> None:
        self.entries: list[tuple[str, str]] = []

    async def xadd(self, stream: str, fields: dict[str, str], **kwargs) -> str:
        del kwargs
        self.entries.append((stream, fields["data"]))
        return "1-0"

    async def xrevrange(self, stream: str, count: int | None = None, **kwargs):
        del stream, count, kwargs
        return []

    async def aclose(self) -> None:
        return None


def _make_service(**settings_kwargs) -> StrategyEngineService:
    base = {
        "scanner_feed_retention_enabled": False,
        "strategy_seeded_snapshot_max_age_seconds": 0.0,
    }
    base.update(settings_kwargs)
    settings = import_module("project_mai_tai.settings").Settings(**base)
    return StrategyEngineService(settings=settings, redis_client=_FakeRedis())


def _neutralize_loop(service: StrategyEngineService, *, heartbeat_calls: list[str]) -> None:
    """Patch every loop-body collaborator to a trivial default so a single
    ``_run_main_loop_iteration`` call is driveable in isolation. Individual
    tests then make ONE step raise."""

    async def _aint(*a, **k):
        return 0

    async def _atuple(*a, **k):
        return (0, 0)

    async def _anone(*a, **k):
        return None

    service._read_stream_group = _aint  # type: ignore[method-assign]
    service._drain_market_data_stream = _aint  # type: ignore[method-assign]
    service._drain_schwab_stream_queues = _atuple  # type: ignore[method-assign]
    service._monitor_schwab_symbol_health = _aint  # type: ignore[method-assign]
    service._refresh_stale_schwab_1m_history = _atuple  # type: ignore[method-assign]
    service._immediate_schwab_1m_history_refresh = _aint  # type: ignore[method-assign]
    service._sync_subscription_targets = _anone  # type: ignore[method-assign]
    service._publish_strategy_state_snapshot = _anone  # type: ignore[method-assign]
    service._sync_runtime_data_health_incidents = lambda *a, **k: None  # type: ignore[method-assign]
    service._reconcile_runtime_state_from_database = lambda *a, **k: False  # type: ignore[method-assign]
    service.state.flush_completed_bars = lambda: ([], 0)  # type: ignore[method-assign]
    service.state.monitor_completed_bar_flow = lambda *a, **k: 0  # type: ignore[method-assign]
    service.state._roll_scanner_session_if_needed = lambda: False  # type: ignore[method-assign]

    async def _hb(status: str) -> None:
        heartbeat_calls.append(status)

    service._publish_heartbeat = _hb  # type: ignore[method-assign]


async def _run_one_iteration(service: StrategyEngineService) -> None:
    """Drive one iteration with reconcile skipped and the heartbeat forced due."""
    now = datetime.now(UTC)
    await service._run_main_loop_iteration(
        stream_block_ms=1,
        heartbeat_interval_secs=1,
        last_runtime_db_reconcile_at=now,
        last_heartbeat_at=now - timedelta(seconds=3600),
    )


# --------------------------------------------------------------------------
# Layer 1 — _bounded_loop_step
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_bounded_loop_step_contains_exception_and_returns_default() -> None:
    service = _make_service()

    async def boom():
        raise RuntimeError("boom")

    result = await service._bounded_loop_step("alpha", boom(), default=(0, 0))

    assert result == (0, 0)
    assert service._main_loop_step_consecutive_failures["alpha"] == 1
    assert service._main_loop_step_failure_totals["alpha"] == 1
    assert service._main_loop_exception_total == 1
    assert service._main_loop_health == "recovering"


@pytest.mark.asyncio
async def test_bounded_loop_step_success_returns_result_and_resets_streak() -> None:
    service = _make_service()

    async def boom():
        raise RuntimeError("boom")

    async def ok():
        return 42

    await service._bounded_loop_step("alpha", boom(), default=0)
    assert service._main_loop_step_consecutive_failures["alpha"] == 1

    result = await service._bounded_loop_step("alpha", ok(), default=0)
    assert result == 42
    assert service._main_loop_step_consecutive_failures["alpha"] == 0
    assert service._main_loop_health == "healthy"


@pytest.mark.asyncio
async def test_bounded_loop_step_propagates_cancellederror() -> None:
    """CancelledError MUST propagate (shutdown depends on it) and must NOT be
    counted as a contained failure."""
    service = _make_service()

    async def cancels():
        raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        await service._bounded_loop_step("alpha", cancels(), default=0)

    assert "alpha" not in service._main_loop_step_consecutive_failures
    assert service._main_loop_exception_total == 0
    assert service._main_loop_health == "healthy"


@pytest.mark.asyncio
async def test_bounded_loop_step_timeout_is_contained() -> None:
    service = _make_service()

    async def hangs():
        await asyncio.sleep(10)
        return 1

    result = await service._bounded_loop_step("alpha", hangs(), default=-1, timeout=0.05)
    assert result == -1
    assert service._main_loop_step_consecutive_failures["alpha"] == 1


# --------------------------------------------------------------------------
# Escalation + surfacing
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_persistent_failures_escalate_then_recover() -> None:
    service = _make_service(strategy_main_loop_persistent_failure_threshold=3)
    assert service._main_loop_health == "healthy"

    service._record_main_loop_step_failure("alpha")
    assert service._main_loop_health == "recovering"
    service._record_main_loop_step_failure("alpha")
    assert service._main_loop_health == "recovering"
    service._record_main_loop_step_failure("alpha")
    assert service._main_loop_health == "degraded-persistent"

    service._record_main_loop_step_success("alpha")
    assert service._main_loop_health == "healthy"


@pytest.mark.asyncio
async def test_heartbeat_carries_main_loop_health_fields() -> None:
    service = _make_service()
    service._record_main_loop_step_failure("schwab_1m_stale_refresh")

    await service._publish_heartbeat("healthy")

    payload = json.loads(service.redis.entries[-1][1])
    details = payload["payload"]["details"]
    assert details["main_loop_health"] == "recovering"
    assert details["main_loop_exceptions_total"] == "1"
    assert "schwab_1m_stale_refresh" in details["main_loop_failing_steps"]
    # The dedicated field carries severity; top-level status Literal is not overloaded.
    assert payload["payload"]["status"] in {"starting", "healthy", "degraded", "stopping"}


# --------------------------------------------------------------------------
# Iteration-level reproductions of the two real outages
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_iteration_survives_dead_token_history_refresh() -> None:
    """2026-06-03 reproduction: _refresh_stale_schwab_1m_history raises the
    dead-token RuntimeError. The iteration must NOT raise (no zombie), the
    failure is recorded, and the heartbeat still fires."""
    service = _make_service()
    heartbeat_calls: list[str] = []
    _neutralize_loop(service, heartbeat_calls=heartbeat_calls)

    async def dead_token(*a, **k):
        raise RuntimeError("failed refreshing Schwab token: unsupported_token_type: 400")

    service._refresh_stale_schwab_1m_history = dead_token  # type: ignore[method-assign]

    await _run_one_iteration(service)  # must not raise

    assert service._main_loop_step_consecutive_failures["schwab_1m_stale_refresh"] == 1
    assert heartbeat_calls == ["healthy"], "heartbeat must still fire despite the Schwab failure"


@pytest.mark.asyncio
async def test_iteration_survives_streamer_chart_stale_runtimeerror() -> None:
    """2026-06-07 reproduction: a streamer-side RuntimeError surfaced via the
    stream-queue drain must be contained, not zombify the loop."""
    service = _make_service()
    heartbeat_calls: list[str] = []
    _neutralize_loop(service, heartbeat_calls=heartbeat_calls)

    async def chart_stale(*a, **k):
        raise RuntimeError("Schwab CHART_EQUITY channel stale while websocket remained connected")

    service._drain_schwab_stream_queues = chart_stale  # type: ignore[method-assign]

    await _run_one_iteration(service)  # must not raise

    assert service._main_loop_step_consecutive_failures["schwab_stream_drain"] == 1
    assert heartbeat_calls == ["healthy"]


# --------------------------------------------------------------------------
# Controlled fault-injection hook (the post-deploy survival test)
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_fault_injection_raises_then_self_clears() -> None:
    service = _make_service(
        strategy_main_loop_fault_injection_count=2,
        strategy_schwab_1m_enabled=False,
    )
    assert service._main_loop_fault_injection_remaining == 2

    for _ in range(2):
        with pytest.raises(RuntimeError, match="FAULT-INJECTION"):
            await service._refresh_stale_schwab_1m_history()

    assert service._main_loop_fault_injection_remaining == 0
    # Injection exhausted: with schwab_1m disabled the method early-returns cleanly.
    assert await service._refresh_stale_schwab_1m_history() == (0, 0)
