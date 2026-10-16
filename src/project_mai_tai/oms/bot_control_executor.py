"""Per-scope cleanup scheduling; adapters retain their exact owned exit paths."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import uuid4

from project_mai_tai.oms.bot_control import BotControlStore, ControlConflict, StopLease
from project_mai_tai.oms.bot_control_journal import CleanupJournal

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class StopProgress:
    phase: str
    # Only a fresh, complete authoritative census may be submitted to settlement.
    # An empty dict is not a census for an unknown account scope.
    census: dict[str, object] | None = None


CleanupDriver = Callable[[StopLease, CleanupJournal], Awaitable[StopProgress]]


class StopExecutor:
    def __init__(self, store: BotControlStore, journal: CleanupJournal):
        self.store, self.journal = store, journal
        self.identity = f"killsw1-{uuid4()}"
        self.drivers: dict[str, CleanupDriver] = {}
        self.tasks = {}
        self.errors = {}

    def register(self, strategy: str, driver: CleanupDriver) -> None:
        if strategy in self.drivers:
            raise ControlConflict("cleanup_scope_already_registered")
        self.drivers[strategy] = driver

    def schedule(self, states) -> None:
        for code, state in states.items():
            command = state.active_command_id
            if state.desired_state != "STOP" or command is None:
                continue
            previous = self.tasks.get(command)
            if previous is None or previous.done():
                if previous is not None and not previous.cancelled():
                    # Consume the task exception even if a prior pass failed
                    # before a lease was acquired. One scope never stalls others.
                    previous.exception()
                self.tasks[command] = asyncio.create_task(self._drive(code, command))
        for identity, task in tuple(self.tasks.items()):
            if task.done() and all(
                state.active_command_id != identity for state in states.values()
            ):
                if not task.cancelled():
                    task.exception()
                del self.tasks[identity]
                self.errors.pop(identity, None)

    async def _renew(self, lease, lost):
        while True:
            await asyncio.sleep(10)
            try:
                await asyncio.to_thread(self.store.renew_stop, lease, now=datetime.now(UTC))
            except Exception:
                lost.set()
                return

    async def _drive(self, code, command):
        renewal = None
        lease = None
        try:
            lease = await asyncio.to_thread(
                self.store.claim_stop,
                command,
                self.identity,
                now=datetime.now(UTC),
            )
            if lease is None:
                return
            lost = asyncio.Event()
            renewal = asyncio.create_task(self._renew(lease, lost))
            if not lease.scope_complete:
                await asyncio.to_thread(
                    self.store.set_stop_phase, lease, "BLOCKED_SCOPE", now=datetime.now(UTC),
                )
                return
            driver = self.drivers.get(code)
            if driver is None:
                await asyncio.to_thread(
                    self.store.set_stop_phase,
                    lease,
                    "BLOCKED_UNKNOWN",
                    now=datetime.now(UTC),
                )
                return
            progress = await driver(lease, self.journal)
            if lost.is_set():
                raise ControlConflict("cleanup_lease_lost_during_observation")
            if progress.phase == "SETTLED":
                if progress.census is None:
                    raise ControlConflict("cleanup_settlement_census_unknown")
                await asyncio.to_thread(
                    self.store.settle_stop,
                    command,
                    census=progress.census,
                    lease=lease,
                )
            else:
                await asyncio.to_thread(
                    self.store.set_stop_phase,
                    lease,
                    progress.phase,
                    now=datetime.now(UTC),
                )
            self.errors.pop(command, None)
        except asyncio.CancelledError:
            # Dispatch intent survives cancellation/shutdown. Never synthesize a
            # terminal report or free an owned reservation from task cancellation.
            raise
        except Exception as exc:
            reason = str(exc) if isinstance(exc, ControlConflict) else type(exc).__name__
            if self.errors.get(command) != reason:
                _LOGGER.error(
                    "[KILLSW1-CLEANUP] scope=%s command=%s state=UNKNOWN reason=%s",
                    code,
                    command,
                    reason,
                )
                self.errors[command] = reason
            if lease is not None:
                try:
                    await asyncio.to_thread(
                        self.store.set_stop_phase,
                        lease,
                        "BLOCKED_UNKNOWN",
                        now=datetime.now(UTC),
                    )
                except Exception:
                    pass  # A stale executor must not overwrite a successor's phase.
        finally:
            if renewal is not None:
                renewal.cancel()
                await asyncio.gather(renewal, return_exceptions=True)

    async def close(self):
        for task in self.tasks.values():
            task.cancel()
        await asyncio.gather(*self.tasks.values(), return_exceptions=True)
        self.tasks.clear()
