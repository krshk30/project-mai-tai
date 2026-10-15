from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Awaitable, Callable

from project_mai_tai.momentum_live.admission import Refusal, STRATEGY_CODE


@dataclass
class OwnedPosition:
    episode_id: str
    account: str
    symbol: str
    entry_coid: str
    entry_order_id: str
    quantity: Decimal
    entry_price: Decimal
    first_fill_ns: int | None
    exited: Decimal = Decimal(0)
    reserved: Decimal = Decimal(0)
    child_ids: tuple[str, ...] = ()
    native_confirmed: bool = False
    release_unknown: bool = False
    strategy: str = STRATEGY_CODE
    entry_send_ns: int | None = None
    pending_exit_reason: str | None = None

    @property
    def exit_clock_source(self) -> str:
        return "first_execution" if self.first_fill_ns is not None else "send" if self.entry_send_ns is not None else "UNKNOWN"

    @property
    def exit_clock_ns(self) -> int | None:
        return self.first_fill_ns if self.first_fill_ns is not None else self.entry_send_ns

    @property
    def remaining(self) -> Decimal:
        return max(Decimal(0), self.quantity-self.exited)

    def exit_reason(self, bid: Decimal | None, *, quote_ns: int | None, now_ns: int) -> str | None:
        if (bid is None or not bid.is_finite() or bid <= 0 or quote_ns is None
                or not 0 <= now_ns-quote_ns <= 2_000_000_000 or self.remaining <= 0):
            return None
        if self.pending_exit_reason is not None:
            return self.pending_exit_reason
        if self.exit_clock_ns is not None and now_ns-self.exit_clock_ns >= 300_000_000_000:
            return "time_300s"
        if not self.native_confirmed:
            if bid >= self.entry_price*Decimal("1.05"):
                return "target_5pct"
            if bid <= self.entry_price*Decimal(".85"):
                return "hard_stop_15pct"
        return None

    def claim_exit_quantity(self, *, broker_available: Decimal | None,
                            foreign_reserved: Decimal | None) -> Decimal:
        if (self.strategy != STRATEGY_CODE or not self.entry_order_id or not self.entry_coid
                or self.release_unknown or broker_available is None or foreign_reserved is None
                or not broker_available.is_finite() or not foreign_reserved.is_finite()
                or foreign_reserved < 0 or self.reserved < 0):
            raise Refusal("owned_exit_quantity_unknown")
        quantity = max(Decimal(0), min(self.remaining-self.reserved, broker_available-foreign_reserved))
        self.reserved += quantity
        return quantity

    def release_confirmation(self, *, episode_id: str, entry_order_id: str,
                             requested_child_ids: tuple[str, ...], terminal: bool,
                             cumulative_own_exit: Decimal) -> None:
        if (episode_id != self.episode_id or entry_order_id != self.entry_order_id
                or requested_child_ids != self.child_ids or cumulative_own_exit < self.exited
                or cumulative_own_exit > self.quantity):
            raise Refusal("protection_release_owner_mismatch")
        self.exited = cumulative_own_exit
        self.release_unknown = not terminal
        if terminal:
            self.native_confirmed = False
            self.child_ids = ()


class EpisodeWorkQueue:
    """At most two dirty owners plus two workers; callbacks only coalesce keys."""

    def __init__(self, worker: Callable[[str], Awaitable[None]]):
        self.worker = worker
        self.queue: asyncio.Queue[str] = asyncio.Queue(maxsize=2)
        self.owners: set[str] = set()
        self.pending: set[str] = set()
        self.running: set[str] = set()
        self.dirty: set[str] = set()
        self.high_water = 0
        self.offers = 0

    def add_owner(self, episode_id: str) -> None:
        if episode_id not in self.owners and len(self.owners) >= 2:
            raise Refusal("episode_worker_capacity")
        self.owners.add(episode_id)

    def offer(self, episode_id: str) -> bool:
        if episode_id not in self.owners:
            return False
        self.offers += 1
        if episode_id in self.running:
            self.dirty.add(episode_id)
        elif episode_id not in self.pending:
            try:
                self.queue.put_nowait(episode_id)
            except asyncio.QueueFull:
                # Closed owners can still occupy queued slots until a worker
                # drains them. Never raise from a quote callback; cadence retries.
                return False
            self.pending.add(episode_id)
            self.high_water = max(self.high_water, self.queue.qsize())
        return True

    async def run(self) -> None:
        while True:
            episode_id = await self.queue.get()
            self.pending.discard(episode_id)
            self.running.add(episode_id)
            try:
                await self.worker(episode_id)
            finally:
                self.running.discard(episode_id)
                self.queue.task_done()
                if episode_id in self.dirty:
                    self.dirty.discard(episode_id)
                    self.offer(episode_id)


@dataclass
class QuoteDispatch:
    work: EpisodeWorkQueue
    by_symbol: dict[str, tuple[str, ...]] = field(default_factory=dict)
    quote_waiters: dict[str, asyncio.Event] = field(default_factory=dict)

    def on_quote(self, symbol: str) -> None:
        waiter = self.quote_waiters.get(symbol)
        if waiter is not None:
            waiter.set()
        for episode_id in self.by_symbol.get(symbol, ()):
            self.work.offer(episode_id)
