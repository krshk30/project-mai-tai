from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
from typing import Awaitable, Callable

from project_mai_tai.broker_adapters.protocols import OrderRequest
from project_mai_tai.momentum_live.admission import Refusal, STRATEGY_CODE
from project_mai_tai.momentum_live.lifecycle import OwnedPosition


@dataclass
class ProtectionHandle:
    episode_id: str
    account: str
    symbol: str
    entry_order_id: str
    base_coid: str
    quantity: Decimal
    session: str
    child_ids: tuple[str, ...] = ()
    status: str = "unsent"
    cumulative_exited: Decimal = Decimal(0)


@dataclass(frozen=True)
class ProtectionRead:
    episode_id: str
    account: str
    entry_order_id: str
    base_coid: str
    child_ids: tuple[str, ...]
    working: bool
    terminal: bool
    cumulative_exited: Decimal


@dataclass
class ProtectionPorts:
    save: Callable[[ProtectionHandle], Awaitable[None]]
    prepare: Callable[[OrderRequest], Awaitable[None]]
    submit: Callable[[OrderRequest], Awaitable[list]]
    read: Callable[[ProtectionHandle], Awaitable[ProtectionRead]]
    cancel: Callable[[ProtectionHandle], Awaitable[None]]


@dataclass
class NativeProtection:
    """Address one owned protection tree/pair; never cancel by account+symbol."""

    ports: ProtectionPorts
    handles: dict[str, ProtectionHandle] = field(default_factory=dict)
    generations: dict[str, int] = field(default_factory=dict)

    @staticmethod
    def _check(position: OwnedPosition, handle: ProtectionHandle) -> None:
        if (position.strategy != STRATEGY_CODE or handle.episode_id != position.episode_id
                or handle.account != position.account or handle.symbol != position.symbol
                or handle.entry_order_id != position.entry_order_id or not position.entry_order_id
                or not position.entry_coid or handle.quantity <= 0):
            raise Refusal("native_protection_owner_mismatch")

    async def _observe(self, position: OwnedPosition, handle: ProtectionHandle) -> ProtectionRead:
        self._check(position, handle)
        read = await self.ports.read(handle)
        if (read.episode_id != handle.episode_id or read.account != handle.account
                or read.entry_order_id != handle.entry_order_id or read.base_coid != handle.base_coid
                or len(read.child_ids) != len(set(read.child_ids))
                or not read.cumulative_exited.is_finite()
                or read.cumulative_exited < handle.cumulative_exited
                or read.cumulative_exited > position.quantity
                or (read.working and read.terminal)
                or (read.working and not read.child_ids)):
            raise Refusal("native_protection_read_unknown")
        if handle.child_ids and read.child_ids != handle.child_ids:
            raise Refusal("native_protection_child_identity_changed")
        handle.child_ids = read.child_ids
        handle.cumulative_exited = read.cumulative_exited
        position.exited = max(position.exited, read.cumulative_exited)
        position.child_ids = handle.child_ids
        position.native_confirmed = read.working and handle.session == "NORMAL"
        position.reserved = min(handle.quantity, position.remaining) if read.working else Decimal(0)
        position.release_unknown = not (read.working or read.terminal)
        if read.terminal:
            handle.status = "terminal"
        elif read.working:
            if handle.status != "cancel_requested":
                handle.status = "working"
        await self.ports.save(handle)
        return read

    async def ensure(self, position: OwnedPosition, *, broker: str, session: str) -> None:
        if position.remaining <= 0:
            if position.episode_id in self.handles:
                await self.release_for_exit(position)
            return
        if broker not in {"schwab", "webull"} or session not in {"NORMAL", "AM", "PM"}:
            raise Refusal("native_session_or_provider_unknown")
        handle = self.handles.get(position.episode_id)
        if handle is not None:
            read = await self._observe(position, handle)
            if not read.terminal:
                # Resize/handover must release exact prior children first. Never
                # put a second pair over a partial fill or ambiguous initial ACK.
                if not read.working or (handle.quantity == position.remaining and handle.session == session):
                    return
                if not await self.release_for_exit(position):
                    return
            if position.remaining <= 0:
                return
            if handle.quantity == position.remaining and handle.session == session:
                # A terminal rejected/expired/cancelled pair is not permission
                # to repeatedly send the same protection on every quote.
                return
        quantity = position.remaining
        generation = self.generations.get(position.episode_id, 0) + 1
        self.generations[position.episode_id] = generation
        base = f"ml1-{position.symbol}-p-{position.episode_id.replace('-', '')[:20]}-{generation}"
        if len(base) > 38:
            raise Refusal("native_protection_identity_capacity")
        tick = Decimal(".0001") if position.entry_price < 1 else Decimal(".01")
        target = (position.entry_price*Decimal("1.05")/tick).to_integral_value(rounding=ROUND_CEILING)*tick
        stop = (position.entry_price*Decimal(".85")/tick).to_integral_value(rounding=ROUND_FLOOR)*tick
        if stop <= 0:
            raise Refusal("native_stop_tick_unrepresentable")
        metadata = {"episode_id": position.episode_id, "momlive_strategy": STRATEGY_CODE,
            "momlive_one_shot": "true", "entry_broker_order_id": position.entry_order_id,
            "limit_price": str(target), "reference_price": str(target), "session": session,
            "extended_hours": "false" if session == "NORMAL" else "true",
            "market_session": "RTH" if session == "NORMAL" else "PRE" if session == "AM" else "POST"}
        if session == "NORMAL":
            metadata.update(bracket_target_price=str(target), bracket_stop_price=str(stop))
            metadata["exit_only_oco" if broker == "schwab" else "webull_exit_only_pair"] = "true"
        request = OrderRequest(base, position.account, STRATEGY_CODE, position.symbol,
            "sell", "close", quantity, "MOMLIVE1_OWNED_PROTECTION", metadata, "limit", "day")
        handle = ProtectionHandle(position.episode_id, position.account, position.symbol,
            position.entry_order_id, base, quantity, session)
        self.handles[position.episode_id] = handle
        await self.ports.prepare(request)
        # Durable send latch before the broker call. Unknown ACK cannot authorize
        # a new generation; only an authoritative terminal own-child read can.
        handle.status = "submitted_unknown"
        await self.ports.save(handle)
        position.release_unknown = True
        await self.ports.submit(request)
        await self._observe(position, handle)

    async def release_for_exit(self, position: OwnedPosition) -> bool:
        handle = self.handles.get(position.episode_id)
        if handle is None:
            if position.child_ids or position.reserved or position.release_unknown:
                raise Refusal("native_protection_handle_unknown")
            return True
        read = await self._observe(position, handle)
        if read.terminal:
            position.native_confirmed = False
            position.child_ids = ()
            position.reserved = Decimal(0)
            position.release_unknown = False
            return True
        if handle.status != "cancel_requested":
            handle.status = "cancel_requested"
            await self.ports.save(handle)
            await self.ports.cancel(handle)
        # Cancel ACK is not proof. The fresh read may reveal a simultaneous fill.
        read = await self._observe(position, handle)
        if not read.terminal:
            position.release_unknown = True
            return False
        position.native_confirmed = False
        position.child_ids = ()
        position.reserved = Decimal(0)
        position.release_unknown = False
        return True
