"""Off-loop durable entry fence and immutable per-scope admission snapshots."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime
from hashlib import sha256
from uuid import UUID

from sqlalchemy import and_, literal, or_, select, union_all

from project_mai_tai.db.models import BotControlAttempt, BrokerAccount, BrokerOrder, OmsManagedPosition, Strategy, VirtualPosition
from project_mai_tai.oms.bot_control import BotControlStore, ControlConflict
from project_mai_tai.oms.bot_control_executor import StopExecutor
from project_mai_tai.oms.bot_control_journal import CleanupJournal
from project_mai_tai.oms.bot_control_scope import (
    CONTROLLED_STRATEGY_CODES, controlled_live_scope, is_controlled_strategy,
    logical_account_mode,
)

_LOGGER = logging.getLogger(__name__)


class BotControlRuntime:
    def __init__(self, sessions):
        self.sessions = sessions
        self.store = BotControlStore(sessions)
        self.journal = CleanupJournal(self.store)
        self.executor = StopExecutor(self.store, self.journal)
        self.states = {}
        self.account_modes = {}
        self._last_unknown = None
        self.boot_managed_ids = None
        self.boot_orb_entry_ids = None
        self._refresh_lock = asyncio.Lock()
        self._boot_unknown_logged = False

    def admission_open(self, code: str) -> bool:
        if not is_controlled_strategy(code):
            return True
        if ((code == "schwab_1m_v2" and self.boot_managed_ids is None)
                or (code == "orb_schwab" and self.boot_orb_entry_ids is None)):
            return False
        state = self.states.get(code)
        return state is not None and state.admission_open

    def stop_requested(self, code: str) -> bool:
        if not is_controlled_strategy(code):
            return False
        state = self.states.get(code)
        return state is not None and state.desired_state == "STOP"

    def is_live_account(self, name: str) -> bool:
        # UNKNOWN is not a paper exemption. Existing known paper accounts are
        # outside this live-only control even if a live scope cannot hydrate.
        return self.account_modes.get(name) not in {"paper", "shadow"}

    def controls(self, code: str, account_name: str) -> bool:
        return is_controlled_strategy(code) and self.is_live_account(account_name)

    async def refresh(self):
        async with self._refresh_lock:
            await self._refresh_once()

    async def _refresh_once(self):
        include_boot = self.boot_managed_ids is None or self.boot_orb_entry_ids is None
        def load():
            states, unknown = {}, []
            with self.sessions() as session:
                accounts = {
                    row.name: logical_account_mode(row.name)
                    for row in session.scalars(select(BrokerAccount)).all()
                }
                strategies = session.scalars(
                    select(Strategy).where(Strategy.execution_mode == "live",
                                           Strategy.code.in_(CONTROLLED_STRATEGY_CODES))
                ).all()
                identities = [(strategy.code, strategy.id) for strategy in strategies]
                boot, boot_orb = None, None
                if include_boot:
                    # One statement gives both sets the SAME database snapshot.
                    # Capture every old BUY identity for a held ORB account/symbol;
                    # a future entry UUID must not inherit its predecessor's block.
                    snapshot = union_all(
                        select(literal("atr"), OmsManagedPosition.id,
                               OmsManagedPosition.current_quantity, BrokerAccount.name,
                               BrokerAccount.provider, Strategy.execution_mode)
                        .select_from(OmsManagedPosition)
                        .outerjoin(BrokerAccount, BrokerAccount.name == OmsManagedPosition.broker_account_name)
                        .outerjoin(Strategy, Strategy.code == OmsManagedPosition.strategy_code)
                        .where(
                        OmsManagedPosition.strategy_code == "schwab_1m_v2",
                        OmsManagedPosition.status == "open", or_(
                            OmsManagedPosition.current_quantity != 0,
                            OmsManagedPosition.current_quantity.is_(None))),
                        select(literal("orb"), BrokerOrder.id, VirtualPosition.quantity,
                               BrokerAccount.name, BrokerAccount.provider,
                               Strategy.execution_mode).select_from(VirtualPosition)
                        .join(Strategy, Strategy.id == VirtualPosition.strategy_id)
                        .outerjoin(BrokerAccount, BrokerAccount.id == VirtualPosition.broker_account_id)
                        .outerjoin(BrokerOrder, and_(
                            BrokerOrder.strategy_id == VirtualPosition.strategy_id,
                            BrokerOrder.broker_account_id == VirtualPosition.broker_account_id,
                            BrokerOrder.symbol == VirtualPosition.symbol, BrokerOrder.side == "buy"))
                        .where(Strategy.code == "orb_schwab", Strategy.execution_mode == "live",
                               or_(VirtualPosition.quantity != 0, VirtualPosition.quantity.is_(None))),
                    )
                    managed_ids, orb_ids, boot_unknown = set(), set(), False
                    for kind, identity, quantity, account_name, provider, execution_mode in session.execute(snapshot):
                        if logical_account_mode(account_name) in {"paper", "shadow"}:
                            continue
                        code = "schwab_1m_v2" if kind == "atr" else "orb_schwab"
                        if (identity is None or not controlled_live_scope(code, execution_mode, account_name)
                                or provider not in {"schwab", "webull"} or quantity is None
                                or (hasattr(quantity, "is_finite") and not quantity.is_finite())
                                or quantity <= 0):
                            boot_unknown = True
                            continue
                        (managed_ids if kind == "atr" else orb_ids).add(str(identity))
                    if not boot_unknown:
                        boot, boot_orb = frozenset(managed_ids), frozenset(orb_ids)
            for code, identity in identities:
                try:
                    states[code] = self.store.hydrate(identity)
                except Exception as exc:
                    unknown.append((code, type(exc).__name__))
            return states, tuple(unknown), accounts, boot, boot_orb

        try:
            states, unknown, accounts, boot, boot_orb = await asyncio.to_thread(load)
            self.account_modes = accounts
            if include_boot:
                self.boot_managed_ids, self.boot_orb_entry_ids = boot, boot_orb
                if boot is None and not self._boot_unknown_logged:
                    _LOGGER.critical("[KILLSW1-BOOT-INVENTORY] ATR_ORB_STOP=BLOCKED owned_ids=UNKNOWN MOM=UNCHANGED")
                    self._boot_unknown_logged = True
        except Exception as exc:
            states, unknown = {}, (("ALL", type(exc).__name__),)
        self.states = states
        if unknown != self._last_unknown:
            if unknown:
                _LOGGER.error(
                    "[KILLSW1-HYDRATION] admission=CLOSED affected=%s exits=UNAFFECTED", unknown
                )
            else:
                _LOGGER.info("[KILLSW1-HYDRATION] scopes=%s status=READY", len(states))
            self._last_unknown = unknown

    async def run(self, stop):
        try:
            while not stop.is_set():
                self.executor.schedule(self.states)
                try:
                    await asyncio.wait_for(stop.wait(), timeout=0.5)
                except TimeoutError:
                    await self.refresh()
        finally:
            await self.executor.close()

    def stamp(self, metadata: dict, code: str, decided_at: datetime) -> None:
        if not is_controlled_strategy(code):
            return
        state = self.states.get(code)
        if not self.admission_open(code):
            raise ControlConflict("entry_scope_not_running")
        if decided_at.tzinfo is None:
            raise ControlConflict("entry_decision_time_unknown")
        metadata.setdefault("bot_control_generation", str(state.generation))
        metadata.setdefault("bot_control_decided_at", decided_at.isoformat())

    async def cleanup_lease(self, code):
        state = self.states.get(code)
        if state is None or state.desired_state != "STOP" or state.active_command_id is None:
            raise ControlConflict("cleanup_active_stop_unknown")
        lease = await asyncio.to_thread(
            self.store.claim_stop, state.active_command_id, self.executor.identity,
            now=datetime.now(UTC),
        )
        if lease is None:
            raise ControlConflict("cleanup_owned_by_other_executor")
        return lease

    @staticmethod
    def cleanup_binding(request, *, children=()):
        return sha256(json.dumps({
            "strategy": request.strategy_code, "account": request.broker_account_name,
            "symbol": request.symbol, "coid": request.client_order_id,
            "episode": request.metadata.get("episode_id"),
            "children": list(children),
        }, sort_keys=True).encode()).hexdigest()

    async def _cleanup_order(self, request):
        def read():
            with self.sessions() as session:
                row = session.scalar(select(BrokerOrder).where(
                    BrokerOrder.client_order_id == request.client_order_id,
                ))
                if row is None:
                    raise ControlConflict("cleanup_order_unprepared")
                strategy, account = session.get(Strategy, row.strategy_id), session.get(BrokerAccount, row.broker_account_id)
                if (strategy is None or account is None
                        or not controlled_live_scope(strategy.code, strategy.execution_mode, account.name)
                        or account.provider not in {"schwab", "webull"}
                        or strategy.code != request.strategy_code
                        or account.name != request.broker_account_name or row.symbol != request.symbol
                        or row.side != request.side):
                    raise ControlConflict("cleanup_order_identity_changed")
                return row.id
        return await asyncio.to_thread(read)

    async def begin_cleanup(self, request, operation, *, children=()):
        lease = await self.cleanup_lease(request.strategy_code)
        identity = await self._cleanup_order(request)
        prepared = await asyncio.to_thread(
            self.journal.prepare, lease, order_id=identity, operation=operation,
            sequence_no=0, owner_hash=self.cleanup_binding(request, children=children),
            owner_revision=lease.generation, now=datetime.now(UTC),
        )
        if prepared.status != "PREPARED":
            return False  # Existing owner must read the original identity, never resend.
        await asyncio.to_thread(
            self.journal.dispatch, lease, prepared.attempt_id, now=datetime.now(UTC),
        )
        return True

    async def record_cleanup_read(self, request, reports):
        if not self.stop_requested(request.strategy_code) or request.intent_type == "cancel":
            return
        lease = await self.cleanup_lease(request.strategy_code)
        order_id = await self._cleanup_order(request)

        def pending():
            with self.sessions() as session:
                return tuple(session.scalars(select(BotControlAttempt.attempt_id).where(
                    BotControlAttempt.command_id == lease.command_id,
                    BotControlAttempt.target_order_id == order_id,
                    BotControlAttempt.operation.in_(("CANCEL_ENTRY", "SUBMIT_EXIT", "ADOPT_EXISTING_EXIT")),
                    BotControlAttempt.status.not_in(("TERMINAL", "PROVEN_NOT_SENT")),
                )).all())

        attempts = await asyncio.to_thread(pending)
        if (not attempts and request.side == "sell" and request.metadata.get("exit_reason")
                and any(report.origin == "broker" and report.event_type in {"accepted", "partially_filled"}
                        and report.client_order_id == request.client_order_id for report in reports)):
            adopted = await asyncio.to_thread(
                self.journal.prepare, lease, order_id=order_id, operation="ADOPT_EXISTING_EXIT",
                sequence_no=0, owner_hash=self.cleanup_binding(request),
                owner_revision=lease.generation, now=datetime.now(UTC),
            )
            attempts = (adopted.attempt_id,)
        for report in reports:
            if (report.origin != "broker" or report.client_order_id != request.client_order_id
                    or not report.broker_order_id
                    or report.event_type not in {"accepted", "partially_filled", "filled", "cancelled", "rejected", "expired"}):
                continue
            for identity in attempts:
                await asyncio.to_thread(
                    self.journal.reconcile, lease, identity,
                    broker_order_id=report.broker_order_id, client_order_id=request.client_order_id,
                    status=report.event_type, filled_quantity=report.filled_quantity,
                    observed_at=datetime.now(UTC), now=datetime.now(UTC),
                )
            # One cumulative read per callback, not duplicate terminal transitions.
            if attempts:
                break

    async def record_cleanup_tree(self, request, read):
        lease = await self.cleanup_lease(request.strategy_code)
        order_id = await self._cleanup_order(request)

        def pending():
            with self.sessions() as session:
                return session.scalar(select(BotControlAttempt.attempt_id).where(
                    BotControlAttempt.command_id == lease.command_id,
                    BotControlAttempt.target_order_id == order_id,
                    BotControlAttempt.operation == "CANCEL_PROTECTION",
                    BotControlAttempt.status.not_in(("TERMINAL", "PROVEN_NOT_SENT")),
                ))

        identity = await asyncio.to_thread(pending)
        if identity is None:
            return
        await asyncio.to_thread(
            self.journal.reconcile_tree, lease, identity,
            owner_hash=self.cleanup_binding(request, children=read.child_ids),
            entry_broker_id=read.entry_order_id, child_ids=read.child_ids,
            cumulative_exited=read.cumulative_exited, terminal=read.terminal, working=read.working,
            observed_at=datetime.now(UTC), now=datetime.now(UTC),
        )

    async def authorize(self, request, old_broker_id: str | None = None):
        if not self.controls(request.strategy_code, request.broker_account_name):
            return None
        if not self.admission_open(request.strategy_code):
            raise ControlConflict("entry_scope_not_running")
        generation = request.metadata.get("bot_control_generation", "")
        if not isinstance(generation, str) or not generation.isdecimal():
            raise ControlConflict("entry_generation_unsealed")
        try:
            decided = datetime.fromisoformat(request.metadata["bot_control_decided_at"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ControlConflict("entry_decision_time_unknown") from exc

        def permit():
            with self.sessions() as session:
                strategy = session.scalar(
                    select(Strategy).where(Strategy.code == request.strategy_code)
                )
                account = session.scalar(
                    select(BrokerAccount).where(BrokerAccount.name == request.broker_account_name)
                )
                order = session.scalar(
                    select(BrokerOrder).where(
                        BrokerOrder.client_order_id == request.client_order_id
                    )
                )
                if (
                    strategy is None
                    or account is None
                    or not controlled_live_scope(strategy.code, strategy.execution_mode, account.name)
                    or order is None
                    or order.strategy_id != strategy.id
                    or order.broker_account_id != account.id
                    or order.symbol != request.symbol
                    or order.quantity != request.quantity
                ):
                    raise ControlConflict("entry_durable_identity_unprepared")
                order_id, strategy_id, account_id = order.id, strategy.id, account.id
            if old_broker_id is not None:
                try:
                    intent_id = UUID(request.metadata["bot_control_replace_intent_id"])
                except (KeyError, ValueError, TypeError) as exc:
                    raise ControlConflict("replacement_identity_unprepared") from exc
                return self.store.authorize_replacement(
                    order_id=order_id,
                    intent_id=intent_id,
                    strategy_id=strategy_id,
                    account_id=account_id,
                    symbol=request.symbol,
                    old_broker_id=old_broker_id,
                    expected_generation=int(generation),
                    decided_at=decided,
                )
            return self.store.authorize_entry(
                order_id=order_id,
                strategy_id=strategy_id,
                expected_generation=int(generation),
                decided_at=decided,
                account_id=account_id,
                symbol=request.symbol,
            )

        result = await asyncio.to_thread(permit)
        # Existing report writers may replace the order's flat metadata. Carry
        # the already-committed marker forward instead of losing its provenance.
        request.metadata["bot_control_permit"] = json.dumps(
            {
                "strategy_id": str(result.strategy_id),
                "client_order_id": request.client_order_id,
                "generation": result.generation,
                "revision": result.revision,
                "status": "MAY_HAVE_SENT",
                "recorded_at": result.recorded_at.isoformat(),
            },
            sort_keys=True,
        )
        return result


class ControlledBrokerAdapter:
    """All existing non-entry adapter methods retain their original behavior."""

    def __init__(self, delegate, control: BotControlRuntime):
        self.delegate, self.control = delegate, control

    def __getattr__(self, name):
        return getattr(self.delegate, name)

    async def submit_order(self, request):
        if (is_controlled_strategy(request.strategy_code) and request.side == "buy"
                and request.intent_type in {"open", "scale"}):
            try:
                await self.control.authorize(request)
            except Exception as exc:
                reason = str(exc) if isinstance(exc, ControlConflict) else "control_db_UNKNOWN"
                _LOGGER.warning(
                    "[KILLSW1-ENTRY-FENCED] strategy=%s symbol=%s reason=%s",
                    request.strategy_code,
                    request.symbol,
                    reason,
                )
                # Do not overwrite a prior working/uncertain broker order with
                # a synthetic terminal event merely because THIS call was fenced.
                raise ControlConflict(f"KILLSW1:{reason}") from exc
        return await self.delegate.submit_order(request)

    async def replace_bracket_order(self, request, old_broker_id):
        if is_controlled_strategy(request.strategy_code) and request.side == "buy":
            try:
                await self.control.authorize(request, old_broker_id)
            except Exception as exc:
                reason = str(exc) if isinstance(exc, ControlConflict) else "control_db_UNKNOWN"
                raise ControlConflict(f"KILLSW1:{reason}") from exc
        return await self.delegate.replace_bracket_order(request, old_broker_id)
