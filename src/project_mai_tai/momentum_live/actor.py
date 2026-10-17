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
OBSERVATION_POLL_NS = 1_000_000_000


@dataclass(frozen=True)
class OwnedObservation:
    """Authoritative own-order observation; never an account-wide quantity."""

    quantity: Decimal
    exited: Decimal
    first_fill_ns: int | None
    entry_terminal: bool
    outstanding_exit: bool
    position: OwnedPosition | None
    owner_provider: str | None = None  # Optional logging evidence, never execution authority.


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
    stop_requested: Callable[[], bool] = lambda: False
    abandon_unsent: Callable[[str], Awaitable[bool]] | None = None
    diagnostic_account: Callable[[], str] | None = None


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
        self._quantity_mismatch_since: dict[str, int | None] = {}
        self._quantity_mismatch_states: dict[str, str] = {}

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
        self._quantity_mismatch_since = {identity: stamp for identity, stamp in self._quantity_mismatch_since.items()
                                         if identity in active}
        self._quantity_mismatch_states = {identity: state for identity, state in self._quantity_mismatch_states.items()
                                          if identity in active}
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
        self._quantity_mismatch_since.clear()
        self._quantity_mismatch_states.clear()

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
        transient_quantity_mismatch = False
        try:
            async with self.state_lock:
                episode = self.book.episodes[identity]
                if episode.state not in ACTIVE_STATES:
                    return
                if episode.state == "claimed" and episode.first_submit_ns is None and episode.filled == 0:
                    if (self.ports.stop_requested() and self.ports.abandon_unsent is not None
                            and await self.ports.abandon_unsent(identity)):
                        self.book.reconcile(identity, cumulative_filled=Decimal(0),
                            cumulative_exited=Decimal(0), now_ns=self.ports.clock_ns(), terminal_entry=True)
                        await self.ports.save(episode_payload(self.book, identity))
                        self._refresh_owners()
                    return  # Quote-pending reservation; no broker order to poll yet.
                now_ns = self.ports.clock_ns()
                cached = self._observations.get(identity)
                refreshed = cached is None or not 0 <= now_ns-cached[0] < OBSERVATION_POLL_NS
                if cached is not None and 0 <= now_ns-cached[0] < OBSERVATION_POLL_NS:
                    observation = cached[1]
                else:
                    observation = await self.ports.observe(identity)
                    self._observations[identity] = (self.ports.clock_ns(), observation)
                if observation.position is not None:
                    position = observation.position
                    if (position.episode_id != identity or position.symbol != episode.symbol
                            or position.strategy != STRATEGY_CODE or position.quantity != observation.quantity
                            or position.exited != observation.exited):
                        if self.restored:
                            try:
                                values = (position.quantity, position.exited, observation.quantity, observation.exited)
                                transient_quantity_mismatch = (
                                    position.episode_id == identity and position.symbol == episode.symbol
                                    and position.strategy == STRATEGY_CODE
                                    and observation.owner_provider == "schwab"
                                    and self.ports.diagnostic_account is not None
                                    and position.account == self.ports.diagnostic_account()
                                    and isinstance(position.entry_coid, str) and bool(position.entry_coid)
                                    and isinstance(position.entry_order_id, str) and bool(position.entry_order_id)
                                    and all(isinstance(value, Decimal) and value.is_finite() and value >= 0
                                            for value in values)
                                    and position.exited <= position.quantity
                                    and observation.exited <= observation.quantity)
                            except Exception:
                                # Optional diagnostics cannot replace this original Refusal.
                                transient_quantity_mismatch = False
                        raise Refusal("owned_observation_identity_mismatch")
                elif observation.quantity > observation.exited:
                    raise Refusal("held_position_identity_UNKNOWN")
                # Actual recovery, not changed quantities/state/clocks, ends continuity.
                self._quantity_mismatch_since.pop(identity, None)
                self._quantity_mismatch_states.pop(identity, None)
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
                forced_cancel = (self.ports.stop_requested() and not observation.entry_terminal
                                 and not episode.cancel_requested)
                if forced_cancel:
                    episode.cancel_requested = True
                    episode.state = "cancel_requested"
                if forced_cancel or self.book.cancel_due(identity, now_ns=self.ports.clock_ns()):
                    # Durable latch before any cancel wire, including unknown ACK.
                    await self.ports.save(episode_payload(self.book, identity))
                    await self.ports.cancel_entry_once(identity)
                if observation.position is not None and episode.remaining > 0:
                    if self.ports.stop_requested():
                        if observation.entry_terminal:
                            await self.ports.exit_owned(observation.position, "KILLSW1_STOP")
                        self._refresh_owners()
                        return
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
            in_grace = False
            if transient_quantity_mismatch and self.restored:
                try:
                    stamp = self.ports.clock_ns()
                    observed_at = self._observations[identity][0]
                    valid_clock = (type(stamp) is int and type(now_ns) is int and type(observed_at) is int
                        and stamp >= max(1, now_ns, observed_at)
                        and 0 <= stamp-observed_at < OBSERVATION_POLL_NS)
                except Exception:
                    valid_clock = False
                if identity not in self._quantity_mismatch_since and len(self._quantity_mismatch_since) < 2:
                    # Unknown first time cannot become a new grace period later
                    # without an actual matching observation or retirement.
                    self._quantity_mismatch_since[identity] = stamp if valid_clock else None
                first = self._quantity_mismatch_since.get(identity)
                in_grace = valid_clock and type(first) is int and 0 <= stamp-first <= 2*OBSERVATION_POLL_NS
                if in_grace and self._quantity_mismatch_states.get(identity) != episode.state:
                    try:
                        logger.info("[MOMLIVE1-OWNED] episode=%s reconciliation=UNKNOWN "
                            "reason=owned_observation_identity_mismatch comparison=quantity_exited "
                            "state=%s grace_ms=%.3f", identity, episode.state, (stamp-first)/1_000_000)
                    except Exception:
                        in_grace = False  # Retain the original ERROR/refusal path.
                    else:
                        self._quantity_mismatch_states[identity] = episode.state
            if not in_grace:
                logger.exception("[MOMLIVE1-OWNED] episode=%s reconciliation=UNKNOWN", identity)
            if not self.restored:
                raise
