"""Shared loop-resilience primitives for the isolated schwab_1m_v2 bot.

SPOF Workstream A (v2 follow-up). Lives in its own module so BOTH
`services/schwab_1m_v2_bot.py` and `market_data/schwab_v2_rest_client.py` can
import it without a circular dependency (the rest client cannot import the bot).

Design: `docs/schwab-1m-v2-loop-resilience-design.md`.

- `LoopHealthTracker` — per-task consecutive-failure accounting with escalation
  to `degraded-persistent` after N consecutive failures, a loud one-shot log on
  the escalation edge, and a `details()` dict for the heartbeat (a dedicated
  `loop_health` field — NOT folded into the shared status Literal).
- `run_resilient_loop` — the per-task backstop: runs a loop's iteration body so
  an unanticipated `Exception` can never silently kill the task. `CancelledError`
  is re-raised so shutdown still works.
- `sleep_or_stop` — interruptible idle wait shared by the loops.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable


async def sleep_or_stop(stop_event: asyncio.Event, seconds: float) -> None:
    """Wait up to `seconds`, returning early if `stop_event` is set."""
    if seconds <= 0:
        return
    try:
        await asyncio.wait_for(stop_event.wait(), timeout=seconds)
    except asyncio.TimeoutError:
        return


class LoopHealthTracker:
    """Tracks per-task loop failures and derives an overall `loop_health`.

    healthy            — no task is currently failing.
    recovering         — at least one task has 1..N-1 consecutive failures.
    degraded-persistent — some task has >= N consecutive failures (loud).
    """

    def __init__(
        self,
        *,
        persistent_failure_threshold: int,
        logger: logging.Logger,
    ) -> None:
        self._threshold = max(1, int(persistent_failure_threshold))
        self._logger = logger
        self._consecutive: dict[str, int] = {}
        self._totals: dict[str, int] = {}
        self._exception_total = 0
        self._health = "healthy"
        self._dead_tasks: set[str] = set()

    @property
    def health(self) -> str:
        return self._health

    @property
    def exception_total(self) -> int:
        return self._exception_total

    def consecutive(self, name: str) -> int:
        return self._consecutive.get(name, 0)

    def record_failure(self, name: str) -> None:
        self._consecutive[name] = self._consecutive.get(name, 0) + 1
        self._totals[name] = self._totals.get(name, 0) + 1
        self._exception_total += 1
        self._refresh()

    def record_success(self, name: str) -> None:
        if self._consecutive.get(name):
            self._consecutive[name] = 0
            self._refresh()
        # A task that produces a clean iteration is alive again.
        self._dead_tasks.discard(name)

    def mark_task_died(self, name: str, *, exc: BaseException | None = None) -> None:
        """Liveness supervision: a task ended unexpectedly (nothing awaited it).
        Force `degraded-persistent` and log loudly — once per death (deduped)."""
        if name in self._dead_tasks:
            return
        self._dead_tasks.add(name)
        self._consecutive[name] = max(self._consecutive.get(name, 0), self._threshold)
        self._totals[name] = self._totals.get(name, 0) + 1
        self._exception_total += 1
        self._logger.error(
            "[V2-TASK-DIED] task=%s ended unexpectedly (exc=%r); v2 loop_health is now "
            "degraded-persistent — INVESTIGATE (a bot task is no longer running while the "
            "service heartbeat continues)",
            name,
            exc,
        )
        self._refresh()

    def _refresh(self) -> None:
        worst = max(self._consecutive.values(), default=0)
        if worst >= self._threshold:
            new_health = "degraded-persistent"
        elif worst >= 1:
            new_health = "recovering"
        else:
            new_health = "healthy"
        if new_health == "degraded-persistent" and self._health != "degraded-persistent":
            failing = sorted(
                name for name, n in self._consecutive.items() if n >= self._threshold
            )
            self._logger.error(
                "[V2-LOOP-DEGRADED-PERSISTENT] schwab_1m_v2 task(s) %s have %s+ consecutive "
                "failures; the loop is ALIVE and the service still heartbeats, but a bot loop "
                "is failing — INVESTIGATE",
                ",".join(failing) or "?",
                self._threshold,
            )
        self._health = new_health

    def details(self) -> dict[str, str]:
        """Heartbeat detail fields (dedicated `loop_health`, alongside data_flow)."""
        failing = sorted(name for name, n in self._consecutive.items() if n > 0)
        return {
            "loop_health": self._health,
            "loop_exceptions_total": str(self._exception_total),
            "loop_failing_tasks": ",".join(failing),
        }


async def run_resilient_loop(
    *,
    stop_event: asyncio.Event,
    tracker: LoopHealthTracker,
    name: str,
    iteration: Callable[[], Awaitable[None]],
    backoff_secs: float,
    logger: logging.Logger,
    idle: Callable[[], Awaitable[None]] | None = None,
) -> None:
    """Run `iteration` repeatedly until `stop_event`. ANY `Exception` is
    contained (recorded + logged + backoff + continue) so the task cannot die
    silently. `CancelledError` is re-raised so shutdown propagates.
    """
    while not stop_event.is_set():
        try:
            await iteration()
        except asyncio.CancelledError:
            raise
        except Exception:
            tracker.record_failure(name)
            logger.exception(
                "[V2-LOOP-RECOVERED] task=%s iteration raised; loop continues "
                "(loop_health=%s, consecutive=%s)",
                name,
                tracker.health,
                tracker.consecutive(name),
            )
            await sleep_or_stop(stop_event, backoff_secs)
            continue
        else:
            tracker.record_success(name)
        if idle is not None:
            await idle()
