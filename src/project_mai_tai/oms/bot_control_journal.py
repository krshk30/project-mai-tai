"""Lease-fenced cleanup identities. This journal never performs broker I/O."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from hashlib import sha256
from uuid import UUID, uuid5

from sqlalchemy import select

from project_mai_tai.db.models import (
    BotControlAttempt,
    BrokerAccount,
    BrokerOrder,
    OmsManagedPosition,
    Strategy,
)
from project_mai_tai.oms.bot_control import (
    BotControlStore,
    ControlConflict,
    StopLease,
    _aware_utc,
)

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class CleanupAttempt:
    attempt_id: UUID
    order_id: UUID
    operation: str
    status: str
    client_order_id: str
    revision: int


def _view(row: BotControlAttempt) -> CleanupAttempt:
    return CleanupAttempt(
        row.attempt_id,
        row.target_order_id or row.entry_order_id,
        row.operation,
        row.status,
        row.client_order_id,
        row.revision,
    )


class CleanupJournal:
    """Internal executor API, not an operator-supplied ownership assertion.

    Preparation binds an existing owned broker-order row. Ownership/availability
    census and broker-specific protection release must precede preparation;
    preparing a journal row alone is never permission to sell.
    """

    def __init__(self, store: BotControlStore):
        self.store = store

    @staticmethod
    def _native_parent(session, lease: StopLease, row: BotControlAttempt) -> BrokerOrder:
        entry = session.get(BrokerOrder, row.entry_order_id)
        if (
            entry is None
            or not entry.broker_order_id
            or entry.side != "buy"
            or entry.strategy_id != lease.strategy_id
            or entry.broker_account_id != row.account_id
            or entry.symbol != row.symbol
        ):
            raise ControlConflict("native_parent_identity_changed")
        if row.managed_row_id is not None:
            managed = session.get(OmsManagedPosition, row.managed_row_id)
            strategy = session.get(Strategy, lease.strategy_id)
            account = session.get(BrokerAccount, row.account_id)
            if (
                managed is None
                or strategy is None
                or account is None
                or managed.strategy_code != strategy.code
                or managed.broker_account_name != account.name
                or managed.symbol != row.symbol
                or managed.entry_order_id != entry.id
                or managed.entry_client_order_id != entry.client_order_id
            ):
                raise ControlConflict("native_managed_binding_changed")
        expected_hash = sha256(
            f"{lease.strategy_id}:{entry.id}:{entry.broker_order_id}:"
            f"{entry.client_order_id}:{row.managed_row_id}:{row.binding_key}".encode()
        ).hexdigest()
        if row.owner_evidence_hash != expected_hash:
            raise ControlConflict("native_parent_identity_changed")
        return entry

    def prepare_native(
        self,
        lease: StopLease,
        *,
        entry_id: UUID,
        managed_id: UUID | None,
        binding_key: str,
        now: datetime,
    ) -> CleanupAttempt:
        """Bind the existing exit owner's exact parent/tree, not account-wide orders."""
        if not binding_key or len(binding_key) > 256:
            raise ControlConflict("native_tree_binding_unknown")
        identity = uuid5(lease.command_id, f"native:{entry_id}:{managed_id}:{binding_key}")
        with self.store._sessions.begin() as session:
            command = self.store._leased(session, lease, now)
            entry = session.get(BrokerOrder, entry_id)
            if (
                entry is None
                or entry.strategy_id != lease.strategy_id
                or entry.side != "buy"
                or not entry.broker_order_id
                or str(entry.broker_account_id) not in command.account_scope["account_ids"]
            ):
                raise ControlConflict("native_parent_not_owned")
            if managed_id is not None:
                managed = session.get(OmsManagedPosition, managed_id)
                strategy = session.get(Strategy, lease.strategy_id)
                account = session.get(BrokerAccount, entry.broker_account_id)
                if (
                    managed is None
                    or strategy is None
                    or account is None
                    or managed.strategy_code != strategy.code
                    or managed.broker_account_name != account.name
                    or managed.symbol != entry.symbol
                    or managed.entry_order_id != entry.id
                    or managed.entry_client_order_id != entry.client_order_id
                ):
                    raise ControlConflict("native_managed_binding_changed")
            owner_hash = sha256(
                f"{lease.strategy_id}:{entry.id}:{entry.broker_order_id}:"
                f"{entry.client_order_id}:{managed_id}:{binding_key}".encode()
            ).hexdigest()
            row = session.get(BotControlAttempt, identity)
            if row is not None:
                if row.owner_evidence_hash != owner_hash:
                    raise ControlConflict("native_parent_identity_changed")
                return _view(row)
            row = BotControlAttempt(
                attempt_id=identity,
                command_id=lease.command_id,
                strategy_id=lease.strategy_id,
                execution_mode="live",
                generation=lease.generation,
                account_id=entry.broker_account_id,
                symbol=entry.symbol,
                binding_kind="managed_native_tree" if managed_id is not None else "orb_native_tree",
                binding_key=binding_key,
                parent_intent_id=entry.intent_id,
                entry_order_id=entry.id,
                managed_row_id=managed_id,
                sequence_no=0,
                operation="CANCEL_PROTECTION",
                status="PREPARED",
                client_order_id=f"ks1-native-{identity}",
                requested_quantity=None,
                owner_evidence_revision=lease.generation,
                owner_evidence_hash=owner_hash,
                executor_instance_id=lease.executor_id,
                lease_fence=lease.fence,
                revision=0,
                prepared_at=now,
            )
            session.add(row)
            session.flush()
            result = _view(row)
        self._log(result)
        return result

    def record_native_outcome(
        self, lease: StopLease, attempt_id: UUID, *, outcome: str, now: datetime
    ) -> CleanupAttempt:
        """Only the existing exact-parent reader's positive terminal result releases."""
        if outcome not in {
            "released",
            "resolved_by_fill",
            "reserved",
            "unanswerable",
            "unsupported",
        }:
            raise ControlConflict("native_release_outcome_unknown")
        with self.store._sessions.begin() as session:
            self.store._leased(session, lease, now)
            row = self._owned(session, lease, attempt_id)
            if row.binding_kind not in {"managed_native_tree", "orb_native_tree"}:
                raise ControlConflict("native_attempt_binding_unknown")
            if row.status == "TERMINAL":
                return _view(row)
            if row.dispatch_intent_at is None:
                raise ControlConflict("native_release_not_dispatched")
            observed_at = _aware_utc(now)
            if observed_at < _aware_utc(row.dispatch_intent_at):
                raise ControlConflict("native_release_before_dispatch")
            terminal = outcome in {"released", "resolved_by_fill"}
            next_status = "TERMINAL" if terminal else "UNKNOWN"
            changed = row.status != next_status
            entry = self._native_parent(session, lease, row)
            row.status = next_status
            row.terminal_evidence = {
                "source": "existing_owned_parent_release_reader",
                "entry_order_id": str(entry.id),
                "entry_broker_order_id": entry.broker_order_id,
                "binding": row.binding_key,
                "outcome": outcome,
                "observed_at": now.isoformat(),
            }
            row.last_authoritative_read_at = now
            row.actual_call_completed_at = now
            row.revision += 1
            result = _view(row)
        if changed:
            self._log(result)
        return result

    def prepare(
        self,
        lease: StopLease,
        *,
        order_id: UUID,
        operation: str,
        sequence_no: int,
        owner_hash: str,
        owner_revision: int,
        now: datetime,
    ) -> CleanupAttempt:
        if (
            operation
            not in {"CANCEL_ENTRY", "CANCEL_PROTECTION", "SUBMIT_EXIT", "ADOPT_EXISTING_EXIT"}
            or type(sequence_no) is not int
            or sequence_no < 0
            or type(owner_revision) is not int
            or owner_revision < 0
            or not isinstance(owner_hash, str)
            or len(owner_hash) != 64
            or any(value not in "0123456789abcdef" for value in owner_hash)
        ):
            raise ControlConflict("cleanup_identity_unknown")
        identity = uuid5(lease.command_id, f"{order_id}:{operation}:{sequence_no}")
        with self.store._sessions.begin() as session:
            command = self.store._leased(session, lease, now)
            order = session.scalar(
                select(BrokerOrder).where(BrokerOrder.id == order_id).with_for_update()
            )
            if (
                order is None
                or order.strategy_id != lease.strategy_id
                or str(order.broker_account_id) not in command.account_scope["account_ids"]
                or not order.symbol
                or not order.client_order_id
                or not order.quantity.is_finite()
                or order.quantity <= 0
                or (operation == "CANCEL_ENTRY" and order.side != "buy")
                or (operation != "CANCEL_ENTRY" and order.side != "sell")
            ):
                raise ControlConflict("cleanup_order_not_owned")
            previous = session.get(BotControlAttempt, identity)
            if previous is not None:
                if (
                    previous.owner_evidence_hash != owner_hash
                    or previous.owner_evidence_revision != owner_revision
                    or previous.requested_quantity != order.quantity
                ):
                    raise ControlConflict("cleanup_identity_conflict")
                return _view(previous)
            if operation == "SUBMIT_EXIT":
                if (
                    order.status != "pending"
                    or order.broker_order_id is not None
                    or order.submitted_at is not None
                ):
                    raise ControlConflict("cleanup_exit_not_unsent")
            elif (
                order.status not in {"accepted", "working", "open", "partially_filled"}
                or not order.broker_order_id
            ):
                raise ControlConflict("cleanup_order_not_working")
            if operation in {"SUBMIT_EXIT", "ADOPT_EXISTING_EXIT"}:
                unresolved = session.scalar(
                    select(BotControlAttempt.attempt_id)
                    .where(
                        BotControlAttempt.strategy_id == lease.strategy_id,
                        BotControlAttempt.account_id == order.broker_account_id,
                        BotControlAttempt.symbol == order.symbol,
                        BotControlAttempt.operation.in_(("SUBMIT_EXIT", "ADOPT_EXISTING_EXIT")),
                        BotControlAttempt.status.not_in(("TERMINAL", "PROVEN_NOT_SENT")),
                    )
                    .limit(1)
                )
                if unresolved is not None:
                    raise ControlConflict("cleanup_exit_already_unresolved")
            row = BotControlAttempt(
                attempt_id=identity,
                command_id=lease.command_id,
                strategy_id=lease.strategy_id,
                execution_mode="live",
                generation=lease.generation,
                account_id=order.broker_account_id,
                symbol=order.symbol,
                binding_kind="broker_order",
                binding_key=str(order_id),
                target_order_id=order_id,
                existing_order_id=order_id if operation == "ADOPT_EXISTING_EXIT" else None,
                sequence_no=sequence_no,
                operation=operation,
                status="WORKING" if operation == "ADOPT_EXISTING_EXIT" else "PREPARED",
                client_order_id=(
                    order.client_order_id
                    if operation in {"SUBMIT_EXIT", "ADOPT_EXISTING_EXIT"}
                    else f"killsw1-{identity}"
                ),
                requested_quantity=order.quantity,
                owner_evidence_hash=owner_hash,
                owner_evidence_revision=owner_revision,
                executor_instance_id=lease.executor_id,
                lease_fence=lease.fence,
                revision=0,
                prepared_at=now,
            )
            session.add(row)
            session.flush()
            result = _view(row)
        self._log(result)
        return result

    def dispatch(self, lease: StopLease, attempt_id: UUID, *, now: datetime) -> CleanupAttempt:
        """Commit MAY_HAVE_SENT before exactly one call; replay always refuses.

        After a crash, reconciliation uses the original order identity, even if
        the crash happened after this commit but before reaching the socket.
        """
        with self.store._sessions.begin() as session:
            self.store._leased(session, lease, now)
            row = self._owned(session, lease, attempt_id)
            if (
                row.status != "PREPARED"
                or row.dispatch_intent_at is not None
                or row.executor_instance_id != lease.executor_id
                or row.lease_fence != lease.fence
            ):
                raise ControlConflict("cleanup_dispatch_uncertain_or_fenced")
            if row.binding_kind in {"managed_native_tree", "orb_native_tree"}:
                self._native_parent(session, lease, row)
            row.status, row.dispatch_intent_at = "MAY_HAVE_SENT", now
            row.revision += 1
            result = _view(row)
        self._log(result)
        return result

    def prove_prepared_not_sent(
        self,
        lease: StopLease,
        attempt_id: UUID,
        *,
        now: datetime,
    ) -> CleanupAttempt:
        """Recover only a pre-dispatch intent; never infer an order's terminality."""
        with self.store._sessions.begin() as session:
            self.store._leased(session, lease, now)
            row = self._owned(session, lease, attempt_id)
            if row.status != "PREPARED" or row.dispatch_intent_at is not None:
                raise ControlConflict("cleanup_dispatch_already_uncertain")
            row.status = "PROVEN_NOT_SENT"
            row.terminal_evidence = {
                "proof": "no_dispatch_intent_under_current_lease_lock",
                "observed_at": now.isoformat(),
            }
            row.executor_instance_id, row.lease_fence = lease.executor_id, lease.fence
            row.revision += 1
            result = _view(row)
        self._log(result)
        return result

    @staticmethod
    def _log(result: CleanupAttempt) -> None:
        _LOGGER.info(
            "[KILLSW1-ATTEMPT] attempt=%s order=%s operation=%s status=%s revision=%s",
            result.attempt_id,
            result.order_id,
            result.operation,
            result.status,
            result.revision,
        )

    @staticmethod
    def _owned(session, lease: StopLease, identity: UUID) -> BotControlAttempt:
        row = session.scalar(
            select(BotControlAttempt)
            .where(
                BotControlAttempt.attempt_id == identity,
            )
            .with_for_update()
        )
        if (
            row is None
            or row.command_id != lease.command_id
            or row.strategy_id != lease.strategy_id
            or row.generation != lease.generation
        ):
            raise ControlConflict("cleanup_attempt_not_owned")
        return row

    def reconcile(
        self,
        lease: StopLease,
        attempt_id: UUID,
        *,
        broker_order_id: str,
        client_order_id: str,
        status: str,
        filled_quantity: Decimal,
        observed_at: datetime,
        now: datetime,
    ) -> CleanupAttempt:
        """Only an exact, fresh authoritative read can resolve an uncertain call.

        A missing-order response is not proof of non-submission. The caller must
        retain UNKNOWN and query the same identity; it must not create a new one.
        """
        if (
            observed_at.tzinfo is None
            or now.tzinfo is None
            or not 0 <= (now - observed_at).total_seconds() <= 2
            or not isinstance(filled_quantity, Decimal)
            or not filled_quantity.is_finite()
            or filled_quantity < 0
            or status
            not in {
                "accepted",
                "working",
                "open",
                "partially_filled",
                "filled",
                "cancelled",
                "canceled",
                "expired",
                "rejected",
            }
        ):
            raise ControlConflict("cleanup_read_unknown")
        with self.store._sessions.begin() as session:
            self.store._leased(session, lease, now)
            row = self._owned(session, lease, attempt_id)
            order = session.get(BrokerOrder, row.target_order_id)
            if (
                order is None
                or not broker_order_id
                or order.broker_order_id != broker_order_id
                or order.client_order_id != client_order_id
                or row.status in {"TERMINAL", "PROVEN_NOT_SENT"}
                or observed_at < _aware_utc(row.prepared_at)
                or (
                    row.last_authoritative_read_at is not None
                    and observed_at < _aware_utc(row.last_authoritative_read_at)
                )
                or filled_quantity > row.requested_quantity
                or (status == "filled" and filled_quantity != row.requested_quantity)
            ):
                raise ControlConflict("cleanup_read_identity_conflict")
            previous_fill = (row.terminal_evidence or {}).get("filled_quantity")
            if previous_fill is not None and filled_quantity < Decimal(previous_fill):
                raise ControlConflict("cleanup_fill_would_regress")
            terminal = status in {"filled", "cancelled", "canceled", "expired", "rejected"}
            updated_status = "TERMINAL" if terminal else "WORKING"
            changed = row.status != updated_status or previous_fill != str(filled_quantity)
            row.status = updated_status
            row.terminal_evidence = {
                "broker_order_id": broker_order_id,
                "client_order_id": client_order_id,
                "status": status,
                "filled_quantity": str(filled_quantity),
                "observed_at": observed_at.isoformat(),
                "source": "authoritative_order_read",
            }
            row.last_authoritative_read_at = observed_at
            row.executor_instance_id, row.lease_fence = lease.executor_id, lease.fence
            row.revision += 1
            result = _view(row)
        if changed:
            self._log(result)
        return result

    def reconcile_tree(
        self,
        lease: StopLease,
        attempt_id: UUID,
        *,
        owner_hash: str,
        entry_broker_id: str,
        child_ids: tuple[str, ...],
        cumulative_exited: Decimal,
        terminal: bool,
        working: bool,
        observed_at: datetime,
        now: datetime,
    ) -> CleanupAttempt:
        """Receive the existing owner's validated native-tree observation.

        It is not a cancel acknowledgment. The native reader validates the full
        bound tree and records its actual child fills through the existing fill
        writer before invoking this audit. No journal quantity changes accounting.
        """
        if (
            observed_at.tzinfo is None
            or now.tzinfo is None
            or not 0 <= (now - observed_at).total_seconds() <= 2
            or not child_ids
            or not all(isinstance(value, str) and value for value in child_ids)
            or len(set(child_ids)) != len(child_ids)
            or type(terminal) is not bool
            or type(working) is not bool
            or (terminal and working)
            or not isinstance(cumulative_exited, Decimal)
            or not cumulative_exited.is_finite()
            or cumulative_exited < 0
        ):
            raise ControlConflict("cleanup_tree_read_unknown")
        with self.store._sessions.begin() as session:
            self.store._leased(session, lease, now)
            row = self._owned(session, lease, attempt_id)
            order = session.get(BrokerOrder, row.target_order_id)
            if (
                row.operation != "CANCEL_PROTECTION"
                or row.owner_evidence_hash != owner_hash
                or row.status in {"TERMINAL", "PROVEN_NOT_SENT"}
                or order is None
                or order.strategy_id != lease.strategy_id
                or (order.payload or {}).get("entry_broker_order_id") != entry_broker_id
                or not entry_broker_id
                or observed_at < _aware_utc(row.prepared_at)
                or (
                    row.last_authoritative_read_at is not None
                    and observed_at < _aware_utc(row.last_authoritative_read_at)
                )
            ):
                raise ControlConflict("cleanup_tree_binding_changed")
            previous = row.terminal_evidence or {}
            if (
                previous.get("child_ids") is not None
                and previous["child_ids"] != list(child_ids)
                or previous.get("cumulative_exited") is not None
                and cumulative_exited < Decimal(previous["cumulative_exited"])
            ):
                raise ControlConflict("cleanup_tree_execution_regressed")
            next_status = "TERMINAL" if terminal else "WORKING" if working else "UNKNOWN"
            changed = row.status != next_status
            row.status = next_status
            row.terminal_evidence = {
                "source": "validated_owned_native_tree",
                "entry_broker_order_id": entry_broker_id,
                "child_ids": list(child_ids),
                "cumulative_exited": str(cumulative_exited),
                "terminal": terminal,
                "working": working,
                "observed_at": observed_at.isoformat(),
            }
            row.last_authoritative_read_at = observed_at
            row.executor_instance_id, row.lease_fence = lease.executor_id, lease.fence
            row.revision += 1
            result = _view(row)
        if changed:
            self._log(result)
        return result
