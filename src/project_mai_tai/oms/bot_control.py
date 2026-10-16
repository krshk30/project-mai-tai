"""KILLSW1 durable command primitives; callers must run these off the event loop.

No method in this module sends orders. Each method owns its transaction, so a
returned STOP receipt means the latch has committed, not that inventory is flat.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.orm import Session, sessionmaker

from project_mai_tai.db.models import (
    BotControlAttempt,
    BotControlCommand,
    BotControlState,
    BrokerAccount,
    BrokerOrder,
    Fill,
    Strategy,
    TradeIntent,
)
from project_mai_tai.oms.bot_control_scope import controlled_live_scope, is_controlled_strategy

_LOGGER = logging.getLogger(__name__)


class ControlConflict(ValueError):
    """The caller's generation, identity or settlement proof is not authoritative."""


class UnseededScope(ControlConflict):
    """An absent latch is UNKNOWN, never implicit permission to send."""


@dataclass(frozen=True)
class ControlSnapshot:
    strategy_id: UUID
    generation: int
    revision: int
    desired_state: str
    active_command_id: UUID | None

    @property
    def admission_open(self) -> bool:
        return self.desired_state == "RUN" and self.active_command_id is None


@dataclass(frozen=True)
class CommandReceipt:
    command_id: UUID
    generation: int
    action: str
    phase: str
    replayed: bool


@dataclass(frozen=True)
class EntryPermit:
    order_id: UUID
    strategy_id: UUID
    generation: int
    revision: int
    recorded_at: datetime


@dataclass(frozen=True)
class StopLease:
    command_id: UUID
    strategy_id: UUID
    generation: int
    executor_id: str
    fence: int
    account_ids: tuple[UUID, ...] = ()
    scope_complete: bool = True


def _aware_utc(stamp: datetime) -> datetime:
    return stamp if stamp.tzinfo is not None else stamp.replace(tzinfo=UTC)


class AdmissionLatch:
    """Tick-only immutable snapshot reads; absence/failed hydration stays closed."""

    def __init__(self):
        self._states: dict[UUID, ControlSnapshot] = {}

    def replace(self, states: tuple[ControlSnapshot, ...]) -> None:
        updated = {}
        for state in states:
            previous = self._states.get(state.strategy_id)
            if previous is not None and (previous.generation, previous.revision) > (
                state.generation,
                state.revision,
            ):
                state = previous
            updated[state.strategy_id] = state
        self._states = updated

    def invalidate(self, strategy_id: UUID) -> None:
        self._states = {key: value for key, value in self._states.items() if key != strategy_id}

    def permits(self, strategy_id: UUID, generation: int) -> bool:
        state = self._states.get(strategy_id)
        return state is not None and state.generation == generation and state.admission_open


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


def _snapshot(state: BotControlState) -> ControlSnapshot:
    return ControlSnapshot(
        state.strategy_id,
        state.generation,
        state.revision,
        state.desired_state,
        state.active_command_id,
    )


def _validate_state(session: Session, state: BotControlState) -> None:
    if state.generation == 0:
        if state.desired_state != "RUN" or state.active_command_id is not None:
            raise ControlConflict("invalid_seeded_state")
        return
    command = session.scalar(
        select(BotControlCommand).where(
            BotControlCommand.strategy_id == state.strategy_id,
            BotControlCommand.execution_mode == state.execution_mode,
            BotControlCommand.generation == state.generation,
        )
    )
    if (
        command is None
        or command.account_scope_hash != _digest(command.account_scope)
        or state.desired_state != ("STOP" if command.action == "STOP" else "RUN")
    ):
        raise ControlConflict("control_history_inconsistent")
    if state.active_command_id is not None:
        if (
            state.active_command_id != command.command_id
            or not command.active
            or command.action != "STOP"
        ):
            raise ControlConflict("active_command_scope_inconsistent")
    elif command.active or command.phase != "SETTLED":
        raise ControlConflict("command_not_proven_settled")


class BotControlStore:
    def __init__(self, sessions: sessionmaker[Session]):
        self._sessions = sessions

    @staticmethod
    def _strategy(session: Session, strategy_id: UUID) -> Strategy:
        strategy = session.get(Strategy, strategy_id)
        if strategy is None or strategy.execution_mode != "live":
            raise ControlConflict("not_a_live_strategy")
        if not is_controlled_strategy(strategy.code):
            raise ControlConflict("not_a_controlled_live_strategy")
        return strategy

    @staticmethod
    def _lock(session: Session, strategy_id: UUID) -> BotControlState:
        BotControlStore._strategy(session, strategy_id)
        state = session.scalar(
            select(BotControlState)
            .where(
                BotControlState.strategy_id == strategy_id,
                BotControlState.execution_mode == "live",
            )
            .with_for_update()
        )
        if state is None:
            raise UnseededScope("scope_not_seeded")
        _validate_state(session, state)
        return state

    def seed(self, strategy_id: UUID) -> ControlSnapshot:
        """Explicit provisioning only; never call as a startup/admission fallback."""
        with self._sessions.begin() as session:
            self._strategy(session, strategy_id)
            state = session.get(BotControlState, (strategy_id, "live"))
            if state is None:
                state = BotControlState(
                    strategy_id=strategy_id,
                    execution_mode="live",
                    generation=0,
                    revision=0,
                    desired_state="RUN",
                )
                session.add(state)
                session.flush()
            _validate_state(session, state)
            result = _snapshot(state)
        return result

    def hydrate(self, strategy_id: UUID) -> ControlSnapshot:
        with self._sessions() as session:
            self._strategy(session, strategy_id)
            state = session.get(BotControlState, (strategy_id, "live"))
            if state is None:
                raise UnseededScope("scope_not_seeded")
            _validate_state(session, state)
            return _snapshot(state)

    def describe(self, strategy_id: UUID) -> tuple[ControlSnapshot, str]:
        with self._sessions() as session:
            self._strategy(session, strategy_id)
            state = session.get(BotControlState, (strategy_id, "live"))
            if state is None:
                raise UnseededScope("scope_not_seeded")
            _validate_state(session, state)
            if state.generation == 0:
                phase = "RUN"
            else:
                command = session.scalar(select(BotControlCommand).where(
                    BotControlCommand.strategy_id == strategy_id,
                    BotControlCommand.execution_mode == "live",
                    BotControlCommand.generation == state.generation,
                ))
                phase = command.phase
            return _snapshot(state), phase

    def claim_stop(self, command_id: UUID, executor_id: str, *, now: datetime) -> StopLease | None:
        if now.tzinfo is None or not executor_id or len(executor_id) > 128:
            raise ControlConflict("invalid_executor_lease")
        with self._sessions.begin() as session:
            command = session.get(BotControlCommand, command_id)
            if command is None:
                raise ControlConflict("unknown_command")
            state = self._lock(session, command.strategy_id)
            session.refresh(command)
            if (
                state.desired_state != "STOP"
                or state.active_command_id != command_id
                or command.generation != state.generation
                or not command.active
            ):
                raise ControlConflict("not_an_active_stop")
            if command.lease_fence is None and (
                command.executor_instance_id is not None or command.lease_until is not None
            ):
                raise ControlConflict("lease_fence_unknown")
            if command.lease_until is not None and _aware_utc(command.lease_until) > now:
                if command.executor_instance_id != executor_id:
                    return None
                fence = command.lease_fence
                if fence is None:
                    raise ControlConflict("lease_fence_unknown")
            else:
                fence = 1 if command.lease_fence is None else command.lease_fence + 1
            command.executor_instance_id = executor_id
            command.lease_fence = fence
            command.lease_until = now + timedelta(seconds=30)
            command.revision += 1
            result = StopLease(
                command_id, state.strategy_id, state.generation, executor_id, fence,
                tuple(UUID(value) for value in command.account_scope["account_ids"]),
                command.account_scope.get("complete") is True,
            )
        return result

    def _leased(self, session: Session, lease: StopLease, now: datetime) -> BotControlCommand:
        if now.tzinfo is None:
            raise ControlConflict("lease_time_unknown")
        state = self._lock(session, lease.strategy_id)
        command = session.get(BotControlCommand, lease.command_id)
        if (
            command is None
            or not command.active
            or state.active_command_id != lease.command_id
            or state.desired_state != "STOP"
            or state.generation != lease.generation
            or command.generation != lease.generation
            or command.executor_instance_id != lease.executor_id
            or command.lease_fence != lease.fence
            or command.lease_until is None
            or _aware_utc(command.lease_until) <= now
        ):
            raise ControlConflict("stop_executor_fenced")
        return command

    def renew_stop(self, lease: StopLease, *, now: datetime) -> None:
        with self._sessions.begin() as session:
            command = self._leased(session, lease, now)
            command.lease_until = now + timedelta(seconds=30)

    def set_stop_phase(self, lease: StopLease, phase: str, *, now: datetime) -> None:
        if phase not in {
            "PENDING_CENSUS",
            "CANCELLING_ENTRIES",
            "RECONCILING_PROTECTION",
            "EXIT_WORKING",
            "BLOCKED_UNKNOWN",
            "BLOCKED_SESSION",
            "BLOCKED_SCOPE",
        }:
            raise ControlConflict("invalid_stop_phase")
        with self._sessions.begin() as session:
            command = self._leased(session, lease, now)
            changed = command.phase != phase
            command.phase = phase
            command.revision += int(changed)
        if changed:
            _LOGGER.info(
                "[KILLSW1-COMMAND] command=%s generation=%s phase=%s",
                lease.command_id,
                lease.generation,
                phase,
            )

    def prove_unsent_entry(
        self,
        *,
        order_id: UUID,
        strategy_id: UUID,
        account_id: UUID,
        symbol: str,
    ) -> bool:
        """STOP/state + order locks serialize this proof against a pre-wire permit."""
        with self._sessions.begin() as session:
            state = self._lock(session, strategy_id)
            if state.desired_state != "STOP":
                raise ControlConflict("unsent_proof_requires_stop")
            order = session.scalar(
                select(BrokerOrder).where(BrokerOrder.id == order_id).with_for_update()
            )
            if (
                order is None
                or order.strategy_id != strategy_id
                or order.broker_account_id != account_id
                or order.symbol != symbol
                or order.side != "buy"
                or order.status != "pending"
                or order.broker_order_id is not None
                or order.submitted_at is not None
                or "bot_control_generation" not in (order.payload or {})
                or "bot_control_permit" in (order.payload or {})
                or session.scalar(select(Fill.id).where(Fill.order_id == order_id).limit(1))
                is not None
            ):
                return False
            order.status = "aborted"
            order.payload = {
                **order.payload,
                "bot_control_proven_not_sent": {
                    "generation": state.generation,
                    "command_id": str(state.active_command_id),
                    "proof": "controlled_entry_without_wire_permit_under_stop_lock",
                },
            }
        return True

    def request(
        self,
        *,
        strategy_id: UUID,
        command_id: UUID,
        request_id: UUID,
        expected_generation: int,
        action: str,
        account_ids: tuple[UUID, ...],
        actor_reference: str,
        reason: str,
        scope_complete: bool = True,
    ) -> CommandReceipt:
        if (
            action not in {"STOP", "ON"}
            or type(expected_generation) is not int
            or not 0 <= expected_generation < 2**63 - 1
        ):
            raise ControlConflict("invalid_action_or_generation")
        if not actor_reference or len(actor_reference) > 128 or not reason or len(reason) > 256:
            raise ControlConflict("invalid_actor_or_reason")
        if (
            type(scope_complete) is not bool
            or (not account_ids and scope_complete)
            or len(set(account_ids)) != len(account_ids)
            or (action == "ON" and not scope_complete)
        ):
            raise ControlConflict("explicit_unique_account_scope_required")
        account_scope = {
            "account_ids": sorted(str(value) for value in account_ids),
            "complete": scope_complete,
        }
        payload_hash = _digest(
            {
                "command_id": str(command_id),
                "strategy_id": str(strategy_id),
                "execution_mode": "live",
                "expected_generation": expected_generation,
                "action": action,
                "account_scope": account_scope,
                "actor_reference": actor_reference,
                "reason": reason,
            }
        )
        with self._sessions.begin() as session:
            state = self._lock(session, strategy_id)
            existing = session.scalar(
                select(BotControlCommand).where(BotControlCommand.request_id == request_id)
            )
            if existing is not None:
                if existing.request_hash != payload_hash:
                    raise ControlConflict("request_identity_conflict")
                result = CommandReceipt(
                    existing.command_id, existing.generation, existing.action, existing.phase, True
                )
            else:
                if session.get(BotControlCommand, command_id) is not None:
                    raise ControlConflict("command_identity_conflict")
                if state.generation != expected_generation:
                    raise ControlConflict("stale_generation")
                if state.active_command_id is not None:
                    raise ControlConflict("command_unsettled")
                accounts = session.scalars(
                    select(BrokerAccount).where(BrokerAccount.id.in_(account_ids))
                ).all()
                strategy = self._strategy(session, strategy_id)
                if len(accounts) != len(account_ids) or any(
                    not controlled_live_scope(strategy.code, strategy.execution_mode, account.name)
                    or account.provider not in {"webull", "schwab"}
                    for account in accounts
                ):
                    raise ControlConflict("account_scope_not_live")
                if action == "ON" and state.desired_state != "STOP":
                    raise ControlConflict("on_requires_settled_stop")
                if action == "ON":
                    stopped = session.scalar(
                        select(BotControlCommand).where(
                            BotControlCommand.strategy_id == strategy_id,
                            BotControlCommand.execution_mode == "live",
                            BotControlCommand.generation == state.generation,
                        )
                    )
                    if (
                        stopped is None
                        or stopped.action != "STOP"
                        or stopped.phase != "SETTLED"
                        or stopped.active
                        or stopped.account_scope_hash != _digest(account_scope)
                    ):
                        raise ControlConflict("on_requires_same_settled_scope")
                generation = state.generation + 1
                command = BotControlCommand(
                    command_id=command_id,
                    strategy_id=strategy_id,
                    execution_mode="live",
                    generation=generation,
                    expected_generation=expected_generation,
                    revision=0,
                    action=action,
                    request_id=request_id,
                    request_hash=payload_hash,
                    account_scope=account_scope,
                    account_scope_hash=_digest(account_scope),
                    actor_reference=actor_reference,
                    reason=reason,
                    phase=("PENDING_CENSUS" if scope_complete else "BLOCKED_SCOPE")
                    if action == "STOP"
                    else "SETTLED",
                    active=action == "STOP",
                    inventory_census={},
                    completed_at=None if action == "STOP" else datetime.now(UTC),
                )
                session.add(command)
                session.flush()
                # Revision CAS also rejects stale writers on engines without row locks.
                changed = session.execute(
                    update(BotControlState)
                    .where(
                        BotControlState.strategy_id == strategy_id,
                        BotControlState.execution_mode == "live",
                        BotControlState.revision == state.revision,
                    )
                    .values(
                        generation=generation,
                        revision=state.revision + 1,
                        desired_state="STOP" if action == "STOP" else "RUN",
                        active_command_id=command_id if action == "STOP" else None,
                        updated_at=datetime.now(UTC),
                    )
                    .execution_options(synchronize_session=False)
                )
                if changed.rowcount != 1:
                    raise ControlConflict("concurrent_state_change")
                result = CommandReceipt(command_id, generation, action, command.phase, False)
        _LOGGER.info(
            "[KILLSW1-COMMAND] strategy=%s generation=%s action=%s phase=%s replayed=%s",
            strategy_id,
            result.generation,
            result.action,
            result.phase,
            result.replayed,
        )
        return result

    def authorize_entry(
        self,
        *,
        order_id: UUID,
        strategy_id: UUID,
        expected_generation: int,
        decided_at: datetime | None = None,
        account_id: UUID | None = None,
        symbol: str | None = None,
    ) -> EntryPermit:
        """Commit a single-wire permit on the existing order before broker I/O.

        The caller must reconcile an ambiguous wire with this same identity, not
        call this again. A permit committed before STOP is MAY_HAVE_SENT; STOP
        must census/reconcile it even if the caller has not reached the socket.
        """
        with self._sessions.begin() as session:
            state = self._lock(session, strategy_id)
            if state.generation != expected_generation or not _snapshot(state).admission_open:
                raise ControlConflict("entry_fenced")
            if state.generation > 0:
                on = session.scalar(
                    select(BotControlCommand).where(
                        BotControlCommand.strategy_id == strategy_id,
                        BotControlCommand.execution_mode == "live",
                        BotControlCommand.generation == state.generation,
                    )
                )
                accepted = on.accepted_at
                if accepted.tzinfo is None:
                    accepted = accepted.replace(tzinfo=UTC)
                if (
                    decided_at is None
                    or decided_at.tzinfo is None
                    or not accepted <= decided_at <= datetime.now(UTC)
                ):
                    raise ControlConflict("entry_decision_predates_on")
            order = session.scalar(
                select(BrokerOrder).where(BrokerOrder.id == order_id).with_for_update()
            )
            if (
                order is None
                or order.strategy_id != strategy_id
                or (account_id is not None and order.broker_account_id != account_id)
                or (symbol is not None and order.symbol != symbol)
                or order.side.lower() != "buy"
                or order.quantity <= 0
                or order.status != "pending"
                or order.broker_order_id is not None
                or order.submitted_at is not None
            ):
                raise ControlConflict("entry_order_not_unsent_owned_buy")
            account = session.get(BrokerAccount, order.broker_account_id)
            strategy = self._strategy(session, strategy_id)
            if (
                account is None
                or not controlled_live_scope(strategy.code, strategy.execution_mode, account.name)
                or not account.is_active
                or account.provider not in {"webull", "schwab"}
            ):
                raise ControlConflict("entry_account_not_live")
            payload = dict(order.payload or {})
            if "bot_control_permit" in payload:
                raise ControlConflict("entry_dispatch_already_uncertain")
            revision = state.revision + 1
            changed = session.execute(
                update(BotControlState)
                .where(
                    BotControlState.strategy_id == strategy_id,
                    BotControlState.execution_mode == "live",
                    BotControlState.revision == state.revision,
                )
                .values(revision=revision, updated_at=datetime.now(UTC))
                .execution_options(synchronize_session=False)
            )
            if changed.rowcount != 1:
                raise ControlConflict("concurrent_state_change")
            recorded_at = datetime.now(UTC)
            payload["bot_control_permit"] = json.dumps(
                {
                    "strategy_id": str(strategy_id),
                    "client_order_id": order.client_order_id,
                    "generation": state.generation,
                    "revision": revision,
                    "status": "MAY_HAVE_SENT",
                    "recorded_at": recorded_at.isoformat(),
                },
                sort_keys=True,
            )
            order.payload = payload
            result = EntryPermit(order.id, strategy_id, state.generation, revision, recorded_at)
        _LOGGER.info(
            "[KILLSW1-PERMIT] strategy=%s order=%s generation=%s revision=%s status=MAY_HAVE_SENT",
            strategy_id,
            order_id,
            result.generation,
            result.revision,
        )
        return result

    def authorize_replacement(
        self,
        *,
        order_id: UUID,
        intent_id: UUID,
        strategy_id: UUID,
        account_id: UUID,
        symbol: str,
        old_broker_id: str,
        expected_generation: int,
        decided_at: datetime,
    ) -> EntryPermit:
        """Replacement identity is its durable intent, never a freshly invented retry ID."""
        with self._sessions.begin() as session:
            state = self._lock(session, strategy_id)
            if state.generation != expected_generation or not _snapshot(state).admission_open:
                raise ControlConflict("replacement_fenced")
            if decided_at.tzinfo is None or decided_at > datetime.now(UTC):
                raise ControlConflict("replacement_decision_time_unknown")
            if state.generation > 0:
                on = session.scalar(
                    select(BotControlCommand).where(
                        BotControlCommand.strategy_id == strategy_id,
                        BotControlCommand.execution_mode == "live",
                        BotControlCommand.generation == state.generation,
                    )
                )
                accepted = on.accepted_at
                if accepted.tzinfo is None:
                    accepted = accepted.replace(tzinfo=UTC)
                if decided_at < accepted:
                    raise ControlConflict("replacement_decision_predates_on")
            order = session.scalar(
                select(BrokerOrder).where(BrokerOrder.id == order_id).with_for_update()
            )
            intent = session.scalar(
                select(TradeIntent).where(TradeIntent.id == intent_id).with_for_update()
            )
            if (
                order is None
                or intent is None
                or order.strategy_id != strategy_id
                or order.broker_account_id != account_id
                or order.symbol != symbol
                or order.side != "buy"
                or order.quantity <= 0
                or not old_broker_id
                or order.broker_order_id != old_broker_id
                or order.status not in {"accepted", "working", "open"}
                or intent.strategy_id != strategy_id
                or intent.broker_account_id != account_id
                or intent.symbol != symbol
                or intent.side != "buy"
            ):
                raise ControlConflict("replacement_owned_identity_unknown")
            account = session.get(BrokerAccount, account_id)
            strategy = self._strategy(session, strategy_id)
            if (
                account is None
                or not controlled_live_scope(strategy.code, strategy.execution_mode, account.name)
                or not account.is_active
                or account.provider not in {"schwab", "webull"}
            ):
                raise ControlConflict("replacement_account_not_live")
            if (
                session.scalar(select(Fill.id).where(Fill.order_id == order_id).limit(1))
                is not None
            ):
                raise ControlConflict("replacement_parent_has_fill")
            payload = dict(intent.payload or {})
            if "bot_control_permit" in payload:
                raise ControlConflict("replacement_dispatch_already_uncertain")
            revision, recorded_at = state.revision + 1, datetime.now(UTC)
            changed = session.execute(
                update(BotControlState)
                .where(
                    BotControlState.strategy_id == strategy_id,
                    BotControlState.execution_mode == "live",
                    BotControlState.revision == state.revision,
                )
                .values(revision=revision, updated_at=recorded_at)
                .execution_options(synchronize_session=False)
            )
            if changed.rowcount != 1:
                raise ControlConflict("concurrent_state_change")
            payload["bot_control_permit"] = json.dumps(
                {
                    "strategy_id": str(strategy_id),
                    "generation": state.generation,
                    "revision": revision,
                    "status": "MAY_HAVE_SENT",
                    "recorded_at": recorded_at.isoformat(),
                    "parent_order_id": str(order_id),
                    "client_order_id": order.client_order_id,
                },
                sort_keys=True,
            )
            intent.payload = payload
            result = EntryPermit(order_id, strategy_id, state.generation, revision, recorded_at)
        return result

    def settle_stop(
        self,
        command_id: UUID,
        *,
        census: dict[str, object],
        lease: StopLease | None = None,
    ) -> ControlSnapshot:
        """Trusted executor only: every scoped account must have authoritative flat proof.

        This is not an HTTP-supplied assertion. The later inventory executor must
        supply fresh owned-order/position evidence, not net-account quantities.
        """
        with self._sessions.begin() as session:
            command = session.get(BotControlCommand, command_id)
            if command is None:
                raise ControlConflict("unknown_command")
            state = self._lock(session, command.strategy_id)
            if lease is not None:
                if lease.command_id != command_id:
                    raise ControlConflict("stop_executor_fenced")
                self._leased(session, lease, datetime.now(UTC))
            elif command.executor_instance_id is not None:
                raise ControlConflict("settlement_executor_unknown")
            if (
                command.action != "STOP"
                or not command.active
                or state.active_command_id != command_id
                or state.generation != command.generation
            ):
                raise ControlConflict("command_not_active_stop")
            if command.account_scope.get("complete") is not True:
                raise ControlConflict("inventory_scope_unknown")
            if set(census) != set(command.account_scope["account_ids"]):
                raise ControlConflict("incomplete_account_census")
            for evidence in census.values():
                if not isinstance(evidence, dict) or not (
                    evidence.get("complete") is True
                    and evidence.get("owned_quantity") == 0
                    and not isinstance(evidence.get("owned_quantity"), bool)
                    and evidence.get("working_orders") == 0
                    and not isinstance(evidence.get("working_orders"), bool)
                    and evidence.get("in_flight") == 0
                    and not isinstance(evidence.get("in_flight"), bool)
                    and isinstance(evidence.get("evidence_hash"), str)
                    and len(evidence["evidence_hash"]) == 64
                ):
                    raise ControlConflict("inventory_not_proven_settled")
                try:
                    observed = datetime.fromisoformat(str(evidence["observed_at"]))
                except (KeyError, TypeError, ValueError) as exc:
                    raise ControlConflict("inventory_observation_time_unknown") from exc
                accepted = command.accepted_at
                if accepted.tzinfo is None:
                    accepted = accepted.replace(tzinfo=UTC)
                now = datetime.now(UTC)
                if (
                    observed.tzinfo is None
                    or not accepted <= observed <= now
                    or (now - observed).total_seconds() > 2
                ):
                    raise ControlConflict("inventory_observation_not_fresh")
            pending_order = session.scalar(
                select(BrokerOrder.id)
                .where(
                    BrokerOrder.strategy_id == command.strategy_id,
                    BrokerOrder.broker_account_id.in_(
                        UUID(value) for value in command.account_scope["account_ids"]
                    ),
                    BrokerOrder.status.not_in(
                        ("filled", "cancelled", "canceled", "rejected", "aborted", "expired")
                    ),
                )
                .limit(1)
            )
            if pending_order is not None:
                raise ControlConflict("owned_order_not_terminal")
            unresolved = session.scalar(
                select(BotControlAttempt.attempt_id)
                .where(
                    BotControlAttempt.strategy_id == command.strategy_id,
                    BotControlAttempt.execution_mode == "live",
                    BotControlAttempt.status.not_in(("TERMINAL", "PROVEN_NOT_SENT")),
                )
                .limit(1)
            )
            if unresolved is not None:
                raise ControlConflict("unresolved_attempt")
            command.inventory_census = census
            command.phase = "SETTLED"
            command.active = False
            command.revision += 1
            command.completed_at = datetime.now(UTC)
            state.active_command_id = None
            state.revision += 1
            # Settlement never turns admission back on: only a new ON generation does.
            session.flush()
            result = _snapshot(state)
        _LOGGER.info(
            "[KILLSW1-COMMAND] strategy=%s command=%s generation=%s phase=SETTLED admission=STOP",
            result.strategy_id,
            command_id,
            result.generation,
        )
        return result
