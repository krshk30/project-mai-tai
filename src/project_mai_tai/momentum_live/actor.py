from __future__ import annotations

import asyncio
from dataclasses import dataclass
from decimal import Decimal
import json
import logging
from typing import Awaitable, Callable

from project_mai_tai.momentum_live.admission import ACTIVE_STATES, EpisodeLedger, Refusal, STRATEGY_CODE, Signal
from project_mai_tai.momentum_live.execution import LiveEntryExecutor
from project_mai_tai.momentum_live.lifecycle import EpisodeWorkQueue, OwnedPosition, QuoteDispatch
from project_mai_tai.momentum_live.owner_proof import episode_payload

logger = logging.getLogger("momentum-live")


@dataclass(frozen=True)
class OwnedObservation:
    """Authoritative own-order observation; never an account-wide quantity."""

    quantity: Decimal
    exited: Decimal
    first_fill_ns: int | None
    entry_terminal: bool
    outstanding_exit: bool
    position: OwnedPosition | None


@dataclass
class ActorPorts:
    enabled: Callable[[], bool]
    clock_ns: Callable[[], int]
    observe: Callable[[str], Awaitable[OwnedObservation]]
    save: Callable[[dict], Awaitable[None]]
    cancel_entry_once: Callable[[str], Awaitable[None]]
    protect_owned: Callable[[OwnedPosition], Awaitable[None]]
    exit_owned: Callable[[OwnedPosition, str], Awaitable[None]]
    quote: Callable[[str], dict | None]


class LiveActor:
    """One entry lane and two coalescing owned workers, independent of OMS ticks."""

    def __init__(self, book: EpisodeLedger, entry: LiveEntryExecutor, ports: ActorPorts,
                 *, state_lock: asyncio.Lock):
        self.book = book
        self.entry = entry
        self.ports = ports
        self.state_lock = state_lock
        self.inbox: asyncio.Queue = asyncio.Queue(maxsize=64)
        self.work = EpisodeWorkQueue(self._service_owned)
        self.dispatch = QuoteDispatch(self.work)
        self.restored = False
        self.tasks: list[asyncio.Task] = []
        self._observations: dict[str, tuple[int, OwnedObservation]] = {}

    def offer_intent(self, event) -> bool:
        if not self.restored or event.payload.strategy_code != STRATEGY_CODE:
            return False
        if event.payload.intent_type == "open" and not self.ports.enabled():
            return False
        if event.payload.intent_type not in {"open", "close", "cancel"}:
            return False
        try:
            self.inbox.put_nowait(event)
        except asyncio.QueueFull:
            logger.warning("[MOMLIVE1-REFUSED] symbol=%s reason=oms_offer_full", event.payload.symbol)
            return False
        return True

    def _refresh_owners(self) -> None:
        active = {identity for identities in self.book._active_by_symbol.values() for identity in identities}
        self.work.owners = active
        self.work.dirty.intersection_update(active)
        self._observations = {identity: row for identity, row in self._observations.items() if identity in active}
        self.dispatch.by_symbol = {symbol: tuple(sorted(identities))
                                   for symbol, identities in self.book._active_by_symbol.items()}
        self.dispatch.quote_waiters = {symbol: waiter for symbol, waiter in self.dispatch.quote_waiters.items()
                                       if symbol in self.dispatch.by_symbol}

    async def restore_before_serving(self) -> None:
        if not self.book.ready:
            raise Refusal("ownership_restore_pending")
        self._refresh_owners()
        # No dispatch/resubmit on restart. Reconcile and protect each durable owner
        # before accepting signals or publishing a restored producer receipt.
        for identity in sorted(self.work.owners):
            await self._service_owned(identity)
        self._refresh_owners()
        self.restored = True

    async def start(self) -> None:
        await self.restore_before_serving()
        self.tasks = [asyncio.create_task(self._entries()), asyncio.create_task(self._cadence()),
                      asyncio.create_task(self.work.run()), asyncio.create_task(self.work.run())]

    async def close(self) -> None:
        self.restored = False
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        self.tasks = []

    async def _entries(self) -> None:
        while True:
            event = await self.inbox.get()
            try:
                if event.payload.intent_type == "open":
                    if not self.ports.enabled():
                        continue
                    raw = json.loads(event.payload.metadata["momlive_signal"])
                    raw["signal_price"] = Decimal(raw["signal_price"])
                    signal = Signal(**raw)
                    if signal.country_route is None or signal.country_evidence_json is None:
                        raise Refusal("producer_country_decision_unsealed")
                    if signal.symbol != event.payload.symbol:
                        raise Refusal("handoff_symbol_identity_mismatch")
                    # Pure claim operations are atomic on this event loop; never
                    # hold the owner lock while waiting for a targeted quote.
                    identity, _, _ = await self.entry.admit(signal)
                    self._refresh_owners()
                    if identity in self.work.owners:
                        self.work.offer(identity)
                else:
                    identity = event.payload.metadata.get("episode_id")
                    episode = self.book.episodes.get(identity)
                    if (episode is None or episode.symbol != event.payload.symbol
                            or episode.state not in ACTIVE_STATES):
                        raise Refusal("owned_action_episode_unknown")
                    # Explicit cancel/close only schedules the original owner; it
                    # cannot become a generic account/symbol order.
                    self.work.offer(identity)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("[MOMLIVE1-REFUSED] symbol=%s reason=%s", event.payload.symbol,
                               str(exc) if isinstance(exc, Refusal) else "entry_observation_UNKNOWN")
                self._refresh_owners()
            finally:
                self.inbox.task_done()

    async def _cadence(self) -> None:
        while True:
            for identity in tuple(self.work.owners):
                self.work.offer(identity)
            await asyncio.sleep(.1)

    async def _service_owned(self, identity: str) -> None:
        try:
            async with self.state_lock:
                episode = self.book.episodes[identity]
                if episode.state not in ACTIVE_STATES:
                    return
                if episode.state == "claimed" and episode.first_submit_ns is None and episode.filled == 0:
                    return  # Quote-pending reservation; no broker order to poll yet.
                now_ns = self.ports.clock_ns()
                cached = self._observations.get(identity)
                refreshed = cached is None or not 0 <= now_ns-cached[0] < 1_000_000_000
                if cached is not None and 0 <= now_ns-cached[0] < 1_000_000_000:
                    observation = cached[1]
                else:
                    observation = await self.ports.observe(identity)
                    self._observations[identity] = (self.ports.clock_ns(), observation)
                if observation.position is not None:
                    position = observation.position
                    if (position.episode_id != identity or position.symbol != episode.symbol
                            or position.strategy != STRATEGY_CODE or position.quantity != observation.quantity
                            or position.exited != observation.exited):
                        raise Refusal("owned_observation_identity_mismatch")
                elif observation.quantity > observation.exited:
                    raise Refusal("held_position_identity_UNKNOWN")
                if refreshed:
                    self.book.reconcile(identity, cumulative_filled=observation.quantity,
                        cumulative_exited=observation.exited, first_fill_ns=observation.first_fill_ns,
                        now_ns=self.ports.clock_ns(), terminal_entry=observation.entry_terminal,
                        outstanding_exit=observation.outstanding_exit)
                    await self.ports.save(episode_payload(self.book, identity))
                if refreshed and observation.position is not None:
                    observation.position.first_fill_ns = episode.first_fill_ns
                    observation.position.entry_send_ns = episode.first_submit_ns
                    # Partial fills also receive protection; cancel cannot strand
                    # the filled slice while its remaining entry is still working.
                    await self.ports.protect_owned(observation.position)
                    if (observation.position.exit_clock_ns is None
                            or not 0 < observation.position.exit_clock_ns <= self.ports.clock_ns()):
                        raise Refusal("owned_time_exit_anchor_UNKNOWN")
                if self.book.cancel_due(identity, now_ns=self.ports.clock_ns()):
                    # Durable latch before any cancel wire, including unknown ACK.
                    await self.ports.save(episode_payload(self.book, identity))
                    await self.ports.cancel_entry_once(identity)
                if observation.position is not None and episode.remaining > 0:
                    quote = self.ports.quote(episode.symbol) or {}
                    reason = observation.position.exit_reason(quote.get("bid"),
                        quote_ns=quote.get("observed_ns"), now_ns=self.ports.clock_ns())
                    if reason is not None:
                        await self.ports.exit_owned(observation.position, reason)
                self._refresh_owners()
        except asyncio.CancelledError:
            raise
        except Exception:
            # Keep exact exposure and both workers alive; uncertainty never frees
            # a reservation, resubmits an entry, or switches to foreign quantities.
            logger.exception("[MOMLIVE1-OWNED] episode=%s reconciliation=UNKNOWN", identity)
            if not self.restored:
                raise
