from __future__ import annotations

from datetime import UTC, datetime
from dataclasses import replace
import logging
import json
from decimal import Decimal, InvalidOperation
from uuid import UUID, uuid5, NAMESPACE_URL

from sqlalchemy import or_, select, text

from project_mai_tai.broker_adapters.protocols import OrderRequest
from project_mai_tai.broker_adapters.momentum_order_read import MomentumEntryLookup
from project_mai_tai.broker_adapters.webull import WebullBrokerAdapter
from project_mai_tai.db.models import AccountPosition, BotControlAttempt, BotControlState, BrokerAccount, BrokerOrder, BrokerOrderEvent, Fill, OmsManagedPosition, Strategy, TradeIntent, VirtualPosition
from project_mai_tai.events import TradeIntentEvent, TradeIntentPayload
from project_mai_tai.fanout_identity import merge_broker_order_payload
from project_mai_tai.momentum_live.admission import ACTIVE_STATES, Refusal, Route, STRATEGY_CODE
from project_mai_tai.momentum_live.entry_reprice import UnsentEntryConflict, validate_entry_reprice


class LivePersistence:
    """Synchronous DB units only; caller runs these through OMS._run_db."""

    def __init__(self, store):
        self.store = store

    @staticmethod
    def fill_value(row) -> Decimal:
        value = (row.payload or {}).get("momlive_execution_value_exact")
        return Decimal(value) if value is not None else row.quantity*row.price

    def restore(self, session, day_start: datetime) -> list[dict]:
        rows = session.scalars(select(TradeIntent).join(Strategy, Strategy.id == TradeIntent.strategy_id)
            .where(Strategy.code == STRATEGY_CODE,
                   TradeIntent.payload["momlive_parent"].as_boolean().is_(True),
                   or_(TradeIntent.created_at >= day_start,
                       TradeIntent.payload["momlive_episode"]["state"].as_string().in_(ACTIVE_STATES)))
            .order_by(TradeIntent.created_at).limit(4097)).all()
        if len(rows) >= 4097:
            raise Refusal("durable_episode_history_full")
        return [row.payload["momlive_episode"] for row in rows]

    def restore_bundle(self, session, day_start: datetime) -> dict:
        rows = self.restore(session, day_start)
        runtime, bindings = {}, {}
        for row in rows:
            parent = session.get(TradeIntent, UUID(row["episode_id"]))
            account = session.get(BrokerAccount, parent.broker_account_id)
            if not account.name.startswith("live:") or account.provider not in {"schwab", "webull"}:
                raise Refusal("restored_live_account_binding_UNKNOWN")
            bindings[row["episode_id"]] = account.name
            runtime[row["episode_id"]] = dict(parent.payload.get("momlive_runtime") or {})
        return {"episodes": rows, "bindings": bindings, "runtime": runtime}

    def prove_empty_owned_state(self, session, *, episode_ids=()) -> None:
        strategy = session.scalar(select(Strategy).where(Strategy.code == STRATEGY_CODE))
        if strategy is None:
            if episode_ids:
                raise Refusal("empty_owner_proof_episode_missing")
            return
        held = session.scalar(select(VirtualPosition.id).where(VirtualPosition.strategy_id == strategy.id,
                                                               VirtualPosition.quantity != 0).limit(1))
        managed = session.scalar(select(OmsManagedPosition.id).where(
            OmsManagedPosition.strategy_code == STRATEGY_CODE, OmsManagedPosition.status == "open",
            OmsManagedPosition.current_quantity != 0).limit(1))
        working = session.scalar(select(BrokerOrder.id).where(BrokerOrder.strategy_id == strategy.id,
            BrokerOrder.status.not_in(("filled", "cancelled", "rejected", "expired", "aborted"))).limit(1))
        if held is not None or managed is not None or working is not None:
            raise Refusal("empty_owner_cleanup_exposure_UNKNOWN")
        parents = session.scalars(select(TradeIntent).where(TradeIntent.strategy_id == strategy.id,
            TradeIntent.payload["momlive_parent"].as_boolean().is_(True)).limit(4097)).all()
        if len(parents) > 4096:
            raise Refusal("empty_owner_cleanup_history_UNKNOWN")
        validated = set()
        for parent in parents:
            row = (parent.payload or {}).get("momlive_episode")
            if (not isinstance(row, dict) or row.get("episode_id") != str(parent.id)
                    or row.get("state") not in {"closed", "terminal_no_fill"}):
                raise Refusal("empty_owner_cleanup_book_UNKNOWN")
            snapshot = self.inspect_episode(session, str(parent.id))
            if (snapshot["quantity"] != snapshot["exited"] or snapshot["outstanding_exit"]
                    or Decimal(str(row["filled"])) != snapshot["quantity"]
                    or Decimal(str(row["exited"])) != snapshot["exited"]):
                raise Refusal("empty_owner_cleanup_fill_balance_UNKNOWN")
            validated.add(str(parent.id))
        if not set(episode_ids).issubset(validated):
            raise Refusal("empty_owner_proof_episode_missing")

    def inspect_episode(self, session, episode_id: str) -> dict:
        parent = session.get(TradeIntent, UUID(episode_id))
        if parent is None or session.get(Strategy, parent.strategy_id).code != STRATEGY_CODE:
            raise Refusal("episode_inspection_owner_UNKNOWN")
        order_query = select(BrokerOrder).where(
            BrokerOrder.strategy_id == parent.strategy_id,
            BrokerOrder.payload["episode_id"].as_string() == episode_id)
        runtime = dict(parent.payload.get("momlive_runtime") or {})
        retained_ids = {runtime.get("soft_exit_coid"), (runtime.get("protection") or {}).get("base_coid")}
        retained_ids.update((runtime.get("protection") or {}).get("child_ids") or ())
        orders, stamps, order_count = [], [], 0
        terminal = {"filled", "cancelled", "rejected", "expired", "aborted"}
        # Terminal exit attempts can grow while held. Stream history in bounded
        # chunks; retain only entry/current handles/unresolved orders for reads.
        for order in session.scalars(order_query.execution_options(yield_per=64)):
            order_count += 1
            if order.side == "buy" and (order.payload or {}).get("momlive_first_fill_ns"):
                stamps.append(int(order.payload["momlive_first_fill_ns"]))
            if (order.side == "buy" or order.status not in terminal or order.client_order_id in retained_ids
                    or not runtime.get("soft_exit_coid") and (order.payload or {}).get("exit_reason")):
                orders.append(order)
                if len(orders) > 64 or len(stamps) > 64:
                    raise Refusal("episode_unresolved_order_history_limit_UNKNOWN")
        quantity = exited = value = Decimal(0)
        fill_query = select(Fill).where(Fill.order_id.in_(order_query.with_only_columns(BrokerOrder.id)))
        for fill in session.scalars(fill_query.execution_options(yield_per=64)):
            if fill.strategy_id != parent.strategy_id:
                raise Refusal("episode_own_fill_balance_UNKNOWN")
            if fill.side == "buy":
                quantity += fill.quantity
                value += self.fill_value(fill)
            elif fill.side == "sell":
                exited += fill.quantity
            else:
                raise Refusal("episode_own_fill_side_UNKNOWN")
        if exited > quantity:
            raise Refusal("episode_own_fill_balance_UNKNOWN")
        requests = []
        entry_terminal = bool(orders)
        outstanding_exit = False
        for order in orders:
            account = session.get(BrokerAccount, order.broker_account_id)
            intent = session.get(TradeIntent, order.intent_id)
            metadata = dict(order.payload or {})
            if order.broker_order_id:
                metadata["broker_order_id"] = order.broker_order_id
            request = OrderRequest(order.client_order_id, account.name, STRATEGY_CODE, order.symbol,
                order.side, intent.intent_type, order.quantity, intent.reason, metadata,
                order.order_type, order.time_in_force)
            requests.append({"request": request, "status": order.status,
                             "broker_order_id": order.broker_order_id, "submitted_at": order.submitted_at})
            if order.side == "buy" and intent.intent_type == "open":
                entry_terminal &= order.status in {"filled", "cancelled", "rejected", "expired", "aborted"}
            if order.side == "sell" and order.status not in {"filled", "cancelled", "rejected", "expired", "aborted"}:
                outstanding_exit = True
        return {"quantity": quantity, "exited": exited, "price": value/quantity if quantity else None,
            "first_fill_ns": min(stamps) if stamps else None,
            "entry_terminal": entry_terminal, "outstanding_exit": outstanding_exit,
            "closed_ns": (parent.payload.get("momlive_episode") or {}).get("closed_ns"),
            "orders": requests, "order_count": order_count, "runtime": runtime}

    def abandon_unsent_exit(self, session, request) -> None:
        order = session.scalar(select(BrokerOrder).where(BrokerOrder.client_order_id == request.client_order_id))
        if (order is None or session.get(Strategy, order.strategy_id).code != STRATEGY_CODE
                or order.side != "sell" or order.status != "pending" or order.broker_order_id
                or (order.payload or {}).get("episode_id") != request.metadata["episode_id"]
                or session.scalar(select(Fill.id).where(Fill.order_id == order.id).limit(1)) is not None):
            raise Refusal("unsent_exit_identity_or_execution_UNKNOWN")
        order.status = "aborted"
        order.payload = {**order.payload, "momlive_exit_never_dispatched": True}

    def _unsent_entry(self, session, request, *, local_permit=None):
        """Lock and prove the current prewire version, never classify a lost ACK."""
        episode_id = request.metadata["episode_id"]
        parent = session.scalar(select(TradeIntent).where(
            TradeIntent.id == UUID(episode_id)).with_for_update())
        order = session.scalar(select(BrokerOrder).where(
            BrokerOrder.client_order_id == request.client_order_id).with_for_update())
        if parent is None or order is None:
            raise Refusal("prewire_settlement_identity_UNKNOWN")
        payload = dict(parent.payload or {})
        episode = payload.get("momlive_episode") or {}
        metadata = dict(order.payload or {})
        account = session.get(BrokerAccount, parent.broker_account_id)
        if (session.get(Strategy, parent.strategy_id).code != STRATEGY_CODE
                or payload.get("momlive_parent") is not True
                or episode.get("episode_id") != episode_id or episode.get("symbol") != request.symbol
                or episode.get("state") not in {"claimed", "submitting"}
                or "first_submit_ns" not in episode
                or episode.get("state") == "claimed" and episode["first_submit_ns"] is not None
                or episode.get("first_fill_ns") is not None or episode.get("closed_ns") is not None
                or Decimal(str(episode.get("filled", "NaN"))) != 0
                or Decimal(str(episode.get("exited", "NaN"))) != 0
                or parent.status != "pending" or parent.side != "buy" or parent.intent_type != "open"
                or parent.symbol != request.symbol or request.strategy_code != STRATEGY_CODE
                or request.side != "buy" or request.intent_type != "open"
                or account is None or account.name != request.broker_account_name
                or account.provider not in {"schwab", "webull"} or not account.name.startswith("live:")
                or order.intent_id != parent.id or order.strategy_id != parent.strategy_id
                or order.broker_account_id != parent.broker_account_id
                or order.symbol != parent.symbol or order.side != "buy" or order.quantity != request.quantity
                or order.order_type != request.order_type or order.time_in_force != request.time_in_force
                or order.status != "pending" or order.broker_order_id or order.submitted_at is not None
                or metadata.get("episode_id") != episode_id
                or metadata.get("momlive_strategy") != STRATEGY_CODE
                or metadata.get("limit_price") != request.metadata.get("limit_price")
                or (local_permit is None and ("bot_control_permit" in metadata or "bot_control_permit" in request.metadata)
                    or local_permit is not None and (
                        not isinstance(local_permit, str) or not local_permit
                        or metadata.get("bot_control_permit") != local_permit
                        or request.metadata.get("bot_control_permit") != local_permit))
                or "momcancel1_ack_identity" in metadata
                or "momlive_terminal_broker_event" in metadata):
            raise Refusal("prewire_settlement_dispatch_or_owner_UNKNOWN")
        snapshot = self.inspect_episode(session, episode_id)
        if (snapshot["order_count"] != 1 or snapshot["quantity"] or snapshot["exited"]
                or snapshot["outstanding_exit"]
                or session.scalar(select(BrokerOrderEvent.id).where(
                    BrokerOrderEvent.order_id == order.id).limit(1)) is not None
                or session.scalar(select(VirtualPosition.id).where(
                    VirtualPosition.strategy_id == parent.strategy_id,
                    VirtualPosition.broker_account_id == parent.broker_account_id,
                    VirtualPosition.symbol == parent.symbol, VirtualPosition.quantity != 0).limit(1)) is not None
                or session.scalar(select(OmsManagedPosition.id).where(
                    OmsManagedPosition.strategy_code == STRATEGY_CODE,
                    OmsManagedPosition.broker_account_name == account.name,
                    OmsManagedPosition.symbol == parent.symbol,
                    OmsManagedPosition.current_quantity != 0).limit(1)) is not None):
            raise Refusal("prewire_settlement_execution_or_report_UNKNOWN")
        return parent, order, account, payload, episode, metadata

    def reprice_unsent_entry(self, session, request, replacement, episode=None) -> None:
        try:
            self._reprice_unsent_entry(session, request, replacement, episode)
        except Refusal as exc:
            raise UnsentEntryConflict(str(exc)) from exc

    def _reprice_unsent_entry(self, session, request, replacement, episode=None) -> None:
        parent, order, account, payload, previous_episode, metadata = self._unsent_entry(session, request)
        validate_entry_reprice(request, replacement, Route(account.provider))
        if (parent.quantity != request.quantity
                or previous_episode.get("state") == "submitting" and (
                    type(previous_episode.get("first_submit_ns")) is not int
                    or previous_episode["first_submit_ns"] <= 0 or previous_episode.get("route") != account.provider)
                or {key: value for key, value in metadata.items() if key != "bot_control_prepared_at"}
                != request.metadata):
            raise Refusal("mombuffer1_unsent_preimage_changed")
        if self.foreign_owner(session, parent.strategy_id, parent.broker_account_id, parent.symbol):
            raise Refusal("foreign_account_symbol_owner")
        if episode is not None:
            first_submit = episode.get("first_submit_ns")
            expected = {**previous_episode, "state": "submitting", "route": account.provider,
                        "first_submit_ns": first_submit}
            if (previous_episode.get("state") != "claimed"
                    or previous_episode.get("first_submit_ns") is not None
                    or type(first_submit) is not int or first_submit <= 0 or episode != expected):
                raise Refusal("mombuffer1_submission_owner_changed")
        order.quantity = parent.quantity = replacement.quantity
        order.payload = merge_broker_order_payload(metadata, replacement.metadata)
        parent.payload = {**payload, "metadata": dict(replacement.metadata),
                          "momlive_episode": dict(episode if episode is not None else previous_episode)}
        session.flush()

    def refuse_unsent_entry(self, session, request, reason: str, context: dict) -> None:
        """Live pre-submit call-stack proof only; never a legacy/boot classifier."""
        parent, order, _, payload, episode, metadata = self._unsent_entry(session, request)
        # Commit all durable terminals together before the executor frees RAM.
        receipt = {"reason": "refused_before_wire", "refusal": reason,
                   "context": {key: str(value) for key, value in context.items()}}
        order.status = "aborted"
        order.payload = {**metadata, "momlive_entry_never_dispatched": True,
                         "reject_reason": "refused_before_wire", "momstuck2_refusal": receipt}
        parent.status = "cancelled"
        parent.payload = {**payload, "momlive_episode": {**episode, "state": "terminal_no_fill"},
                          "momstuck2_refusal": receipt}

    def refuse_local_entry(self, session, request, reason, context, *, permit, expected_episode):
        """Only the live runtime's consumed exact no-dispatch receipt calls this.

        Not a boot classifier, not generic exception/report settlement. Preserve
        permit audit; never permit this path after any native transport attempt.
        """
        parent, order, account, payload, episode, metadata = self._unsent_entry(
            session, request, local_permit=permit)
        if (parent.quantity != request.quantity
                or not isinstance(expected_episode, dict) or episode != expected_episode
                or episode.get("state") != "submitting" or episode.get("route") != account.provider
                or type(episode.get("first_submit_ns")) is not int or episode["first_submit_ns"] <= 0
                or {k: v for k, v in metadata.items() if k != "bot_control_prepared_at"} != request.metadata):
            raise Refusal("mombuffer1_local_no_dispatch_preimage_changed")
        receipt = {"source": "exact_live_native_boundary_before_first_transport", "reason": reason,
                   "permit_retained": permit is not None,
                   "context": {key: str(value) for key, value in context.items()}}
        order.status = "aborted"
        order.payload = {**metadata, "momlive_entry_never_dispatched": True,
                         "mombuffer1_no_dispatch": receipt}
        parent.status = "cancelled"
        parent.payload = {**payload, "momlive_episode": {**episode, "state": "terminal_no_fill"},
                          "mombuffer1_no_dispatch": receipt}

    def mark_refused_pre_submit(self, session, request, reason):
        """Record this lexical refusal only on its freshly prepared first attempt."""
        identity = request.metadata["episode_id"]
        parent = session.scalar(select(TradeIntent).where(
            TradeIntent.id == UUID(identity)).with_for_update())
        order = session.scalar(select(BrokerOrder).where(
            BrokerOrder.client_order_id == request.client_order_id).with_for_update())
        attempt = request.metadata.get("momlive_submit_attempt_id")
        seal = dict(attempt_id=attempt, episode_id=identity, client_order_id=request.client_order_id, count=1)
        if (parent is None or order is None or not isinstance(attempt, str)
                or str(UUID(attempt)) != attempt or request.metadata.get("momlive_submit_attempt_count") != "1"
                or (parent.payload or {}).get("momlive_entry_attempt") != seal
                or order.intent_id != parent.id or order.strategy_id != parent.strategy_id
                or session.get(Strategy, parent.strategy_id).code != STRATEGY_CODE
                or session.get(BrokerAccount, order.broker_account_id).name != request.broker_account_name
                or order.broker_account_id != parent.broker_account_id or order.symbol != request.symbol
                or order.side != "buy" or order.status != "pending" or parent.status != "pending"
                or request.side != "buy" or request.intent_type != "open"
                or order.broker_order_id or order.submitted_at is not None
                or (order.payload or {}).get("momlive_submit_attempt_id") != attempt
                or (order.payload or {}).get("momlive_submit_attempt_count") != "1"
                or any(key in (order.payload or {}) or key in request.metadata for key in (
                    "bot_control_permit", "momcancel1_ack_identity", "momlive_terminal_broker_event"))
                or session.scalar(select(BrokerOrderEvent.id).where(
                    BrokerOrderEvent.order_id == order.id).limit(1)) is not None
                or session.scalar(select(BotControlAttempt.attempt_id).where(or_(
                    BotControlAttempt.target_order_id == order.id,
                    BotControlAttempt.entry_order_id == order.id,
                    BotControlAttempt.existing_order_id == order.id,
                    BotControlAttempt.client_order_id == order.client_order_id)).limit(1)) is not None
                or session.scalar(select(Fill.id).where(Fill.order_id == order.id).limit(1)) is not None):
            raise Refusal("momcancel1_refusal_marker_prior_attempt_or_dispatch_UNKNOWN")
        marker = {**seal, "refused_pre_submit": True, "reason": reason}
        order.payload = {**order.payload, "refused_pre_submit": True,
                         "momlive_pre_submit_refusal": json.dumps(marker, sort_keys=True)}
        episode = (parent.payload or {}).get("momlive_episode") or {}
        if episode.get("state") == "claimed" and episode.get("first_submit_ns") is None:
            # First refresh can refuse before begin_submit. Keep this order
            # visible to MOMCANCEL1, including after a restart, without a send.
            parent.payload = {**parent.payload, "momlive_episode": {
                **episode, "state": "cancel_requested", "cancel_requested": True}}
        session.flush()

    def retire_broker_absent_entry(self, session, request, lookup, *, now_ns, sync_pass):
        """Two locked own rows only; an UNKNOWN lookup can never retire a slot."""
        if (type(lookup) is not MomentumEntryLookup or lookup.kind != "absent"
                or lookup.report is not None or lookup.sync_pass != sync_pass
                or type(sync_pass) is not int or sync_pass <= 0
                or (lookup.account, lookup.client_order_id, lookup.symbol) != (
                    request.broker_account_name, request.client_order_id, request.symbol)
                or not 0 <= now_ns - lookup.observed_ns <= 2_000_000_000
                or lookup.provider not in {"webull", "schwab"}
                or lookup.reason not in {"refused_pre_submit_proven", "webull_exact_client_id_ORDER_NOT_FOUND"}):
            raise Refusal("momcancel1_complete_broker_absence_UNKNOWN")
        identity = request.metadata["episode_id"]
        parent = session.scalar(select(TradeIntent).where(
            TradeIntent.id == UUID(identity)).with_for_update())
        order = session.scalar(select(BrokerOrder).where(
            BrokerOrder.client_order_id == request.client_order_id).with_for_update())
        if parent is None or order is None:
            raise Refusal("momcancel1_absent_entry_identity_UNKNOWN")
        account = session.get(BrokerAccount, parent.broker_account_id)
        strategy = session.get(Strategy, parent.strategy_id)
        payload, metadata = dict(parent.payload or {}), dict(order.payload or {})
        episode = payload.get("momlive_episode") or {}
        try:
            marker = json.loads(metadata.get("momlive_pre_submit_refusal", "null"))
        except (TypeError, ValueError):
            marker = None
        seal = payload.get("momlive_entry_attempt")
        if (not isinstance(marker, dict) or metadata.get("refused_pre_submit") is not True
                or marker.get("refused_pre_submit") is not True or not marker.get("reason")
                or not isinstance(seal, dict) or type(seal.get("count")) is not int or seal.get("count") != 1
                or seal != {key: marker.get(key) for key in ("attempt_id", "episode_id", "client_order_id", "count")}
                or seal.get("episode_id") != identity or seal.get("client_order_id") != request.client_order_id
                or seal.get("attempt_id") != metadata.get("momlive_submit_attempt_id")
                or metadata.get("momlive_submit_attempt_count") != "1"
                or "bot_control_permit" in metadata):
            raise Refusal("momcancel1_refused_pre_submit_proof_UNKNOWN")
        if (strategy is None or strategy.code != STRATEGY_CODE
                or request.strategy_code != STRATEGY_CODE or payload.get("momlive_parent") is not True
                or account is None or account.name != lookup.account or account.provider != lookup.provider
                or request.side != "buy" or request.intent_type != "open"
                or parent.side != "buy" or parent.intent_type != "open" or parent.status != "pending"
                or parent.symbol != request.symbol or parent.quantity != request.quantity
                or episode.get("episode_id") != identity or episode.get("symbol") != request.symbol
                or episode.get("state") not in {"submitting", "cancel_requested", "unknown"}
                or episode.get("first_fill_ns") is not None or episode.get("closed_ns") is not None
                or Decimal(str(episode.get("filled", "NaN"))) != 0
                or Decimal(str(episode.get("exited", "NaN"))) != 0
                or order.intent_id != parent.id or order.strategy_id != parent.strategy_id
                or order.broker_account_id != parent.broker_account_id or order.symbol != request.symbol
                or order.side != "buy" or order.status != "pending" or order.quantity != request.quantity
                or order.order_type != request.order_type or order.time_in_force != request.time_in_force
                or order.broker_order_id or order.submitted_at is not None
                or metadata != request.metadata or metadata.get("episode_id") != identity
                or metadata.get("momlive_strategy") != STRATEGY_CODE
                or metadata.get("momlive_one_shot") != "true"
                or request.client_order_id != "ml1-" + request.symbol + "-e-" + identity.replace("-", "")[:20]
                or "momcancel1_ack_identity" in metadata or "momlive_terminal_broker_event" in metadata):
            raise Refusal("momcancel1_absent_entry_preimage_changed")
        snapshot = self.inspect_episode(session, identity)
        if (snapshot["order_count"] != 1 or snapshot["quantity"] or snapshot["exited"]
                or snapshot["outstanding_exit"]
                or session.scalar(select(BotControlAttempt.attempt_id).where(or_(
                    BotControlAttempt.target_order_id == order.id,
                    BotControlAttempt.entry_order_id == order.id,
                    BotControlAttempt.existing_order_id == order.id,
                    BotControlAttempt.client_order_id == order.client_order_id)).limit(1)) is not None
                or session.scalar(select(BrokerOrderEvent.id).where(
                    BrokerOrderEvent.order_id == order.id).limit(1)) is not None
                or session.scalar(select(Fill.id).where(Fill.order_id == order.id).limit(1)) is not None
                or session.scalar(select(VirtualPosition.id).where(
                    VirtualPosition.strategy_id == parent.strategy_id,
                    VirtualPosition.broker_account_id == parent.broker_account_id,
                    VirtualPosition.symbol == parent.symbol, VirtualPosition.quantity != 0).limit(1)) is not None
                or session.scalar(select(OmsManagedPosition.id).where(
                    OmsManagedPosition.strategy_code == STRATEGY_CODE,
                    OmsManagedPosition.broker_account_name == account.name,
                    OmsManagedPosition.symbol == parent.symbol,
                    OmsManagedPosition.current_quantity != 0).limit(1)) is not None):
            raise Refusal("momcancel1_absent_entry_execution_UNKNOWN")
        receipt = dict(provider=lookup.provider, account=lookup.account,
            client_order_id=lookup.client_order_id, reason=lookup.reason,
            observed_ns=lookup.observed_ns, sync_pass=lookup.sync_pass)
        order.status = "aborted"
        order.payload = {**metadata, "momlive_entry_never_dispatched": True,
                         "momcancel1_broker_absent": receipt}
        parent.status = "cancelled"
        parent.payload = {**payload, "momlive_episode": {
            **episode, "state": "terminal_no_fill", "closed_ns": now_ns},
            "momcancel1_broker_absent": receipt}
        session.flush()
        return now_ns

    def abandon_unsent_entry(self, session, episode_id: str) -> bool:
        """Client-only proof for a claimed reservation that never reached a wire permit."""
        strategy_id = session.scalar(select(Strategy.id).where(Strategy.code == STRATEGY_CODE))
        if strategy_id is None:
            return False
        control = session.scalar(select(BotControlState).where(
            BotControlState.strategy_id == strategy_id,
            BotControlState.execution_mode == "live",
        ).with_for_update())
        if control is None or control.desired_state != "STOP" or control.active_command_id is None:
            return False
        parent = session.scalar(select(TradeIntent).where(
            TradeIntent.id == UUID(episode_id)).with_for_update())
        if parent is None or session.get(Strategy, parent.strategy_id).code != STRATEGY_CODE:
            raise Refusal("KILLSW1:unsent_episode_owner_unknown")
        payload = dict(parent.payload or {})
        episode = payload.get("momlive_episode") or {}
        if (payload.get("momlive_parent") is not True or episode.get("episode_id") != episode_id
                or "first_submit_ns" not in episode or episode["first_submit_ns"] is not None
                or episode.get("state") != "claimed"
                or Decimal(str(episode.get("filled", "NaN"))) != 0
                or Decimal(str(episode.get("exited", "NaN"))) != 0):
            raise Refusal("KILLSW1:unsent_episode_state_unknown")
        orders = session.scalars(select(BrokerOrder).where(
            BrokerOrder.intent_id == parent.id).with_for_update().limit(33)).all()
        if len(orders) > 32:
            raise Refusal("KILLSW1:unsent_order_count_unknown")
        for order in orders:
            metadata = order.payload or {}
            if (order.strategy_id != parent.strategy_id or order.broker_account_id != parent.broker_account_id
                    or order.symbol != parent.symbol or order.side != "buy" or order.status != "pending"
                    or order.broker_order_id or order.submitted_at is not None
                    or metadata.get("episode_id") != episode_id
                    or "bot_control_generation" not in metadata or "bot_control_permit" in metadata
                    or session.scalar(select(Fill.id).where(Fill.order_id == order.id).limit(1)) is not None):
                raise Refusal("KILLSW1:unsent_order_identity_or_dispatch_unknown")
        for order in orders:
            order.status = "aborted"
            order.payload = {**order.payload, "momlive_entry_never_dispatched": True}
        payload["momlive_episode"] = {**episode, "state": "terminal_no_fill"}
        payload["momlive_c6_unsent_proof"] = True
        parent.payload, parent.status = payload, "cancelled"
        return True

    def claim(self, session, *, episode: dict, account: str, provider: str, signal: dict) -> bool:
        if session.get_bind().dialect.name == "postgresql":
            session.execute(text("SELECT pg_advisory_xact_lock(hashtext(:key))"),
                            {"key": "momentum_warn_live:capacity"})
            session.execute(text("SELECT pg_advisory_xact_lock(hashtext(:key))"),
                            {"key": f"orb-v2:{account}:{episode['symbol']}"})
        identity = UUID(episode["episode_id"])
        if session.get(TradeIntent, identity) is not None:
            return False
        strategy = self.store.ensure_strategy(session, STRATEGY_CODE, name="Momentum LIVE",
                                               execution_mode="live", metadata_json={"account_name": account})
        broker = self.store.ensure_broker_account(session, account, provider=provider, environment="live")
        if self.foreign_owner(session, strategy.id, broker.id, episode["symbol"]):
            raise Refusal("foreign_account_symbol_owner")
        active = session.scalars(select(TradeIntent).where(TradeIntent.strategy_id == strategy.id,
            TradeIntent.payload["momlive_parent"].as_boolean().is_(True),
            TradeIntent.payload["momlive_episode"]["state"].as_string().in_(ACTIVE_STATES)).limit(2)).all()
        if len(active) >= 2:
            raise Refusal("durable_two_reservations_full")
        session.add(TradeIntent(id=identity, strategy_id=strategy.id, broker_account_id=broker.id,
            symbol=episode["symbol"], side="buy", intent_type="open", quantity=Decimal(1),
            reason="MOMLIVE1_DETECTED", status="pending",
            payload={"event_id": str(identity), "source_service": "momentum-paper", "momlive_parent": True,
                     "momlive_episode": episode, "momlive_signal": signal, "metadata": {"episode_id": str(identity)}}))
        session.flush()
        return True

    @staticmethod
    def foreign_owner(session, strategy_id, account_id, symbol) -> bool:
        pending = session.scalar(select(Strategy.code).join(
            TradeIntent, TradeIntent.strategy_id == Strategy.id).where(
            TradeIntent.broker_account_id == account_id, TradeIntent.symbol == symbol,
            Strategy.id != strategy_id, TradeIntent.side == "buy",
            TradeIntent.intent_type.in_(("open", "scale")),
            TradeIntent.status.not_in(("filled", "cancelled", "canceled", "rejected", "expired",
                                       "aborted", "completed", "superseded")),
        ).limit(1))
        if pending is not None:
            account = session.get(BrokerAccount, account_id)
            logging.getLogger(__name__).warning(
                "[ROUTE-OWNER-CONFLICT] account=%s symbol=%s incoming=%s foreign=%s",
                account.name if account is not None else "UNKNOWN", symbol, STRATEGY_CODE, pending,
            )
            return True
        held = session.scalar(select(VirtualPosition.id).where(
            VirtualPosition.broker_account_id == account_id, VirtualPosition.symbol == symbol,
            VirtualPosition.strategy_id != strategy_id, VirtualPosition.quantity != 0).limit(1))
        working = session.scalar(select(BrokerOrder.id).where(
            BrokerOrder.broker_account_id == account_id, BrokerOrder.symbol == symbol,
            BrokerOrder.strategy_id != strategy_id,
            BrokerOrder.status.in_(("pending", "submitted", "accepted", "partially_filled"))).limit(1))
        account = session.get(BrokerAccount, account_id)
        managed = session.scalar(select(OmsManagedPosition.id).where(
            OmsManagedPosition.broker_account_name == account.name, OmsManagedPosition.symbol == symbol,
            OmsManagedPosition.strategy_code != STRATEGY_CODE, OmsManagedPosition.status == "open",
            OmsManagedPosition.current_quantity != 0).limit(1))
        net = session.scalar(select(AccountPosition.quantity).where(
            AccountPosition.broker_account_id == account_id, AccountPosition.symbol == symbol))
        own = session.scalar(select(VirtualPosition.quantity).where(
            VirtualPosition.broker_account_id == account_id, VirtualPosition.symbol == symbol,
            VirtualPosition.strategy_id == strategy_id))
        manual = net is not None and net != (own or Decimal(0))
        return held is not None or working is not None or managed is not None or manual

    def save(self, session, episode: dict, *, metadata: dict | None = None) -> None:
        row = session.scalar(select(TradeIntent).where(
            TradeIntent.id == UUID(episode["episode_id"])).with_for_update())
        if row is None or session.get(Strategy, row.strategy_id).code != STRATEGY_CODE:
            raise Refusal("durable_episode_owner_changed")
        payload = {**row.payload, "momlive_episode": episode}
        if (row.payload.get("momcancel1_broker_absent")
                and (row.payload.get("momlive_episode") or {}).get("state") == "terminal_no_fill"):
            if episode["state"] != "terminal_no_fill" or Decimal(str(episode["filled"])) != 0:
                raise Refusal("momcancel1_settled_owner_regression_refused")
            episode = {**episode, "closed_ns": row.payload["momlive_episode"]["closed_ns"]}
            payload["momlive_episode"] = episode
        if metadata is not None:
            payload["momlive_runtime"] = dict(metadata)
        row.payload = payload
        row.status = "filled" if episode["state"] == "closed" else (
            "cancelled" if episode["state"] == "terminal_no_fill" else "pending")

    def prepare_order(self, session, episode_id: str, request, *, provider: str) -> None:
        controlled_entry = request.side == "buy" and "bot_control_generation" in request.metadata
        sealed_entry = request.side == "buy" and "momlive_submit_attempt_id" in request.metadata
        parent = (session.scalar(select(TradeIntent).where(TradeIntent.id == UUID(episode_id)).with_for_update())
                  if controlled_entry or sealed_entry else session.get(TradeIntent, UUID(episode_id)))
        if parent is None or session.get(Strategy, parent.strategy_id).code != STRATEGY_CODE:
            raise Refusal("order_episode_owner_invalid")
        if controlled_entry and (parent.payload or {}).get("momlive_episode", {}).get("state") != "claimed":
            raise Refusal("KILLSW1:entry_claim_no_longer_admissible")
        broker = self.store.ensure_broker_account(session, request.broker_account_name,
                                                  provider=provider, environment="live")
        if request.side == "buy" and self.foreign_owner(session, parent.strategy_id, broker.id, request.symbol):
            raise Refusal("foreign_account_symbol_owner")
        if request.side == "sell":
            own = self.store.get_virtual_position(session, strategy_id=parent.strategy_id,
                broker_account_id=broker.id, symbol=request.symbol)
            if own is None or not 0 < request.quantity <= own.quantity:
                raise Refusal("exit_exceeds_strategy_owned_quantity")
        if session.scalar(select(BrokerOrder.id).where(BrokerOrder.client_order_id == request.client_order_id)):
            raise Refusal("order_already_durable_no_resubmit")
        if sealed_entry:
            episode = (parent.payload or {}).get("momlive_episode") or {}
            if (episode.get("state") != "claimed" or episode.get("first_submit_ns") is not None
                    or "momlive_entry_attempt" in (parent.payload or {})
                    or request.metadata.get("momlive_submit_attempt_count") != "1"
                    or session.scalar(select(BrokerOrder.id).where(
                        BrokerOrder.intent_id == parent.id).limit(1)) is not None):
                raise Refusal("momcancel1_first_attempt_not_proven")
            attempt = request.metadata["momlive_submit_attempt_id"]
            if str(UUID(attempt)) != attempt:
                raise Refusal("momcancel1_attempt_identity_invalid")
            parent.payload = {**parent.payload, "momlive_entry_attempt": dict(
                attempt_id=attempt, episode_id=episode_id, client_order_id=request.client_order_id, count=1)}
        self._insert_order(session, parent, broker, request, episode_id)

    def record_terminal_native_child(self, session, episode_id, request, report):
        """Persist an existing zero-fill terminal receipt, never authorize a sell."""
        parent = session.get(TradeIntent, UUID(episode_id))
        if parent is None or session.get(Strategy, parent.strategy_id).code != STRATEGY_CODE:
            raise Refusal("terminal_child_episode_owner_UNKNOWN")
        protection = ((parent.payload or {}).get("momlive_runtime") or {}).get("protection") or {}
        base = protection.get("base_coid") or ""
        expected_children = {WebullBrokerAdapter._combo_leg_coid(base, role) for role in ("T", "S")}
        root = session.scalar(select(BrokerOrder).where(
            BrokerOrder.client_order_id == protection.get("base_coid")))
        broker = session.get(BrokerAccount, parent.broker_account_id)
        root_intent = session.get(TradeIntent, root.intent_id) if root is not None else None
        try:
            protection_quantity = Decimal(str(protection.get("quantity")))
        except (InvalidOperation, ValueError):
            raise Refusal("terminal_child_protection_quantity_UNKNOWN") from None
        if ((parent.payload or {}).get("momlive_parent") is not True
                or ((parent.payload or {}).get("momlive_episode") or {}).get("episode_id") != episode_id
                or broker is None or broker.provider != "webull"
                or protection.get("session") != "NORMAL"
                or protection.get("episode_id") != episode_id
                or protection.get("account") != broker.name
                or not base or set(protection.get("child_ids") or ()) != expected_children
                or protection.get("symbol") != parent.symbol
                or not protection_quantity.is_finite() or protection_quantity <= 0
                or request.quantity != protection_quantity
                or request.client_order_id not in (protection.get("child_ids") or ())
                or request.client_order_id == protection.get("base_coid")
                or request.broker_account_name != broker.name
                or request.strategy_code != STRATEGY_CODE
                or request.symbol != parent.symbol or request.side != "sell"
                or request.intent_type != "close" or request.quantity <= 0
                or request.metadata.get("momlive_native_child") != "true"
                or request.metadata.get("momlive_strategy") != STRATEGY_CODE
                or request.metadata.get("episode_id") != episode_id
                or root is None or root.strategy_id != parent.strategy_id
                or root.broker_account_id != broker.id or root.symbol != parent.symbol
                or root.side != "sell" or root.quantity != request.quantity
                or root_intent is None or root_intent.intent_type != "close"
                or root_intent.strategy_id != parent.strategy_id
                or root_intent.broker_account_id != broker.id
                or root_intent.side != "sell" or root_intent.symbol != parent.symbol
                or ((root_intent.payload or {}).get("metadata") or {}).get("episode_id") != episode_id
                or not root.broker_order_id
                or (root.payload or {}).get("episode_id") != episode_id
                or report.origin != "broker"
                or report.event_type not in {"cancelled", "rejected", "expired"}
                or report.filled_quantity != 0 or not report.broker_order_id
                or report.broker_order_id == root.broker_order_id
                or report.client_order_id != request.client_order_id
                or report.symbol != request.symbol or report.side != "sell"
                or report.intent_type != "close"):
            raise Refusal("terminal_child_receipt_identity_UNKNOWN")
        if session.scalar(select(BrokerOrder.id).where(BrokerOrder.client_order_id == request.client_order_id)):
            raise Refusal("terminal_child_already_durable")
        snapshot = self.inspect_episode(session, episode_id)
        if not 0 < request.quantity <= snapshot["quantity"]:
            raise Refusal("terminal_child_entry_execution_UNKNOWN")
        self._insert_order(session, parent, broker, request, episode_id)
        return self.record(session, request, [report])

    def _insert_order(self, session, parent, broker, request, episode_id):
        intent = parent
        if request.side == "sell" or broker.id != parent.broker_account_id:
            event = TradeIntentEvent(event_id=uuid5(NAMESPACE_URL, request.client_order_id), source_service="oms",
                payload=TradeIntentPayload(strategy_code=STRATEGY_CODE, broker_account_name=request.broker_account_name,
                    symbol=request.symbol, side=request.side, quantity=request.quantity,
                    intent_type=request.intent_type, reason=request.reason, metadata=request.metadata))
            intent = self.store.create_trade_intent(session, strategy=session.get(Strategy, parent.strategy_id),
                                                  broker_account=broker, event=event)
        order = self.store.get_or_create_order(session, intent=intent, strategy_id=parent.strategy_id,
            broker_account_id=broker.id, client_order_id=request.client_order_id, symbol=request.symbol,
            side=request.side, quantity=request.quantity,
            metadata={**request.metadata, "episode_id": episode_id, "momlive_strategy": STRATEGY_CODE},
            status="pending", order_type=request.order_type, time_in_force=request.time_in_force)
        if (request.side == "buy" and request.intent_type == "open"
                or "bot_control_generation" in request.metadata
                or request.metadata.get("exit_reason") == "KILLSW1_STOP"):
            # This branch creates a NEW prewire row (prepare_order refuses an
            # existing coid). The generic factory's creation timestamp is not a
            # submission receipt; unknown/existing rows are never cleared here.
            order.payload = {**order.payload, "bot_control_prepared_at": order.submitted_at.isoformat()}
            order.submitted_at = None
        intent.quantity = request.quantity

    def record(self, session, request, reports) -> dict:
        order = session.scalar(select(BrokerOrder).where(BrokerOrder.client_order_id == request.client_order_id))
        if (order is None or session.get(Strategy, order.strategy_id).code != STRATEGY_CODE
                or session.get(BrokerAccount, order.broker_account_id).name != request.broker_account_name
                or order.symbol != request.symbol or order.side != request.side):
            raise Refusal("report_order_owner_invalid")
        for report in reports:
            if report.client_order_id != request.client_order_id:
                raise Refusal("report_client_order_identity_mismatch")
            if report.filled_quantity > 0 and report.origin != "broker":
                raise Refusal("fill_broker_provenance_unknown")
            entry_ack = (request.side == "buy" and request.intent_type == "open"
                         and report.event_type == "accepted"
                         and report.metadata.get("momcancel1_place_ack") == "true")
            if entry_ack:
                metadata = dict(order.payload or {})
                unknown = (report.metadata.get("momcancel1_ack_identity") != "VALID"
                           or report.origin != "broker" or report.symbol != request.symbol
                           or report.side != request.side or report.intent_type != request.intent_type
                           or report.quantity != request.quantity
                           or not report.broker_order_id
                           or (order.broker_order_id and order.broker_order_id != report.broker_order_id))
                if unknown:
                    # POST already happened. Keep the reservation and existing ID;
                    # an ambiguous ACK never authorizes a new entry or a cancel.
                    order.payload = {**metadata, "momcancel1_ack_identity": "UNKNOWN"}
                    continue
            preserve = (report.event_type in {"rejected", "cancelled", "expired", "aborted"} and report.origin != "broker"
                        or order.status == "filled" and report.event_type != "filled"
                        or entry_ack and order.status in {"cancelled", "rejected", "expired", "aborted"})
            metadata = {**order.payload, **request.metadata}
            if (order.payload or {}).get("momcancel1_ack_identity") == "UNKNOWN":
                metadata["momcancel1_ack_identity"] = "UNKNOWN"
            if entry_ack:
                metadata.setdefault("momcancel1_ack_identity", "VALID")
            if report.origin == "broker" and not preserve:
                metadata["momlive_terminal_broker_event"] = (
                    report.event_type if report.event_type in {"filled", "cancelled", "rejected", "expired"} else "")
            if (report.filled_quantity > 0
                    and report.metadata.get("momlive_execution_time_source") == "broker_execution_time"):
                if report.reported_at.tzinfo is None:
                    raise Refusal("trusted_fill_time_timezone_UNKNOWN")
                stamp = int(report.reported_at.timestamp()*1e9)
                if not 0 < stamp <= int(datetime.now(UTC).timestamp()*1e9):
                    raise Refusal("trusted_fill_time_invalid_or_future")
                key = "momlive_first_fill_ns" if request.side == "buy" else "momlive_last_exit_fill_ns"
                prior_stamp = metadata.get(key)
                reducer = min if request.side == "buy" else max
                metadata[key] = str(reducer(stamp, int(prior_stamp))) if prior_stamp else str(stamp)
            self.store.update_order_from_report(order, report=report, metadata=metadata, preserve_status=preserve)
            prior = session.scalars(select(Fill).where(Fill.order_id == order.id)).all()
            prior_quantity = sum((row.quantity for row in prior), Decimal(0))
            prior_value = sum((self.fill_value(row) for row in prior), Decimal(0))
            fill_report = report
            if report.filled_quantity > 0 and report.event_type not in {"filled", "partially_filled"}:
                # A terminal cancellation can carry real partial executions. Keep
                # its order status above, but book the owned cumulative fill delta.
                fill_report = replace(report, event_type="partially_filled")
            exact_delta_value = None
            if prior_quantity > 0 and report.filled_quantity > prior_quantity and report.fill_price is not None:
                # Adapters report cumulative quantity and cumulative VWAP; preserve
                # actual cumulative cash when recording only the new execution delta.
                delta_price = (report.filled_quantity*report.fill_price-prior_value) / (report.filled_quantity-prior_quantity)
                if delta_price <= 0:
                    raise Refusal("cumulative_fill_value_invalid")
                fill_report = replace(fill_report, fill_price=delta_price)
            if report.filled_quantity > prior_quantity and report.fill_price is not None:
                exact_delta_value = report.filled_quantity*report.fill_price-prior_value
            fill = self.store.record_fill_if_needed(session, order=order, strategy_id=order.strategy_id,
                broker_account_id=order.broker_account_id, report=fill_report,
                payload={"metadata": metadata, "client_order_id": order.client_order_id,
                         "momlive_fill_source": "broker",
                         "momlive_execution_value_exact": str(exact_delta_value) if exact_delta_value is not None else None})
            if fill is not None:
                self.store.apply_fill_to_positions(session, strategy_id=order.strategy_id,
                    broker_account_id=order.broker_account_id, symbol=order.symbol,
                    side=order.side, quantity=fill.quantity, price=fill.price, reported_at=fill.filled_at)
        fills = session.scalars(select(Fill).where(Fill.order_id == order.id)).all()
        quantity = sum((row.quantity for row in fills), Decimal(0))
        value = sum((self.fill_value(row) for row in fills), Decimal(0))
        stamp = min((row.filled_at.replace(tzinfo=UTC) if row.filled_at.tzinfo is None else row.filled_at
                     for row in fills), default=None)
        return {"quantity": quantity, "price": value / quantity if quantity else None,
                "first_fill_ns": int(stamp.timestamp()*1e9) if stamp else None,
                "broker_order_id": order.broker_order_id,
                "entry_ack_identity_unknown": (order.payload or {}).get("momcancel1_ack_identity") == "UNKNOWN",
                "status": order.status}
