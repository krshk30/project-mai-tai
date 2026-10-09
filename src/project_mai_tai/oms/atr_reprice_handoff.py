"""Durable per-order cancel/read/replace coordinator for ATR resting BUYs.

This module does not bypass the OMS intent lane. Its replacement and fill callbacks
must use that lane and the original BUY's normal fill accounting respectively.
Cancellation acknowledgements are deliberately not clearance evidence.
"""
from __future__ import annotations

import asyncio
from contextlib import nullcontext
from dataclasses import dataclass, replace
from datetime import UTC
from decimal import Decimal, InvalidOperation
from typing import Awaitable, Callable, Literal
from uuid import UUID, uuid5, NAMESPACE_URL

from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from project_mai_tai.broker_adapters.atr_buy_readback import AtrBuyReadback, scoped_request
from project_mai_tai.broker_adapters.protocols import ExecutionReport, OrderRequest
from project_mai_tai.db.models import (
    BrokerAccount, BrokerOrder, BrokerOrderEvent, DashboardSnapshot, Fill, Strategy, TradeIntent,
)
from project_mai_tai.strategy_core.v2_entry_sizing import proven_resting_pair


SNAPSHOT_TYPE = "atr_reprice_handoff"
READ_INTERVAL_SECONDS = 1.0
READ_TIMEOUT_SECONDS = 2.0
MAX_READS = 30
PREWIRE_ABORT_CODES = {"rpg_stale_strategy_authorization", "rpg_current_price_size_or_identity_changed",
                      "rpg_strategy_reauthorization_unreadable", "rpg_old_buy_still_owned"}
PREWIRE_NO_WIRE_CODES = PREWIRE_ABORT_CODES | {
    "webull_mirror_precheck_deferred", "webull_mirror_no_fresh_quote_held", "schwab_ineligible_cached",
}


def old_buy_proven_clear(job: dict) -> bool:
    return bool((job.get("local_no_wire") or job.get("cleared_at") is not None)
                and not job.get("no_rebuy"))


def replacement_terminal_zero(job: dict) -> bool:
    """Validate identity-bearing journal evidence, never a phase/status marker."""
    report, request = job.get("replacement_terminal_report", {}), job.get("replacement", {})
    try:
        quantity = Decimal(request.get("quantity", "0"))
        zero = Decimal(report.get("filled_quantity", "NaN"))
        reported_quantity = Decimal(report.get("quantity", "NaN"))
    except (TypeError, ValueError, InvalidOperation):
        return False
    return bool(request and quantity.is_finite() and quantity > 0
        and zero.is_finite() and zero == 0 and reported_quantity.is_finite() and reported_quantity == quantity
        and not job.get("replacement_filled") and not job.get("no_rebuy")
        and report.get("source") in {"broker", "client_audit"}
        and report.get("status") in {"cancelled", "canceled", "rejected", "aborted", "expired"}
        and (report.get("order_id") or (report.get("source") == "client_audit"
             and report.get("intent_id") and report.get("refusal_code") in PREWIRE_NO_WIRE_CODES))
        and (report.get("broker_order_id") or report.get("status") in {"aborted", "rejected"})
        and all(report.get(key) == request.get(key) for key in
                ("client_order_id", "broker_account_name", "strategy_code", "symbol", "side"))
        and all(request.get(key) == job.get("old", {}).get(key) for key in
                ("broker_account_name", "strategy_code", "symbol", "side"))
        and request.get("side") == "buy"
        and request.get("metadata", {}).get("fanout_segment_id") == str(job.get("segment_id"))
        and request.get("metadata", {}).get("cw_entry_slot") == job.get("slot")
        and all(request.get("metadata", {}).get(key) and
                report.get("metadata", {}).get(key) == request["metadata"][key] for key in
                ("rpg_resting_generation", "fanout_segment_id", "cw_entry_slot")))


def replacement_needs_reconciliation(job: dict) -> bool:
    return bool(job.get("replacement") and not job.get("replacement_filled") and not job.get("no_rebuy")
        and job["phase"] in {"placed", "submitting", "submit_unknown", "refused", "expired"})


def rpg_buy_owned(job: dict, *, include_placed: bool = True,
                  slot: str | None = None, segment_id: int | None = None) -> bool:
    """Shared OMS/v2 policy. Known fills consume their opportunity, not a later flip."""
    if job.get("no_rebuy") or job.get("replacement_filled") or job["phase"] == "filled":
        return ((segment_id is None or segment_id == job["segment_id"])
                and (slot is None or slot == job["slot"]))
    if job["phase"] == "placed":
        return include_placed
    if job["phase"] not in {"expired", "refused"}:
        return True
    return bool(not old_buy_proven_clear(job) or
                (job.get("replacement") and not replacement_terminal_zero(job)))


def replacement_order_matches(session, order: BrokerOrder, request: dict, *, require_generation=True) -> bool:
    account = session.get(BrokerAccount, order.broker_account_id)
    strategy = session.get(Strategy, order.strategy_id)
    try:
        quantity = Decimal(request.get("quantity", "0"))
    except (TypeError, ValueError, InvalidOperation):
        return False
    return bool(account and strategy and quantity.is_finite() and quantity > 0
        and account.name == request.get("broker_account_name")
        and strategy.code == request.get("strategy_code") == "schwab_1m_v2"
        and order.client_order_id == request.get("client_order_id")
        and order.symbol == request.get("symbol") and order.side == request.get("side") == "buy"
        and order.quantity == quantity and all(
                (key == "rpg_resting_generation" and not require_generation
                 and not request.get("metadata", {}).get(key) and not (order.payload or {}).get(key))
                or (request.get("metadata", {}).get(key)
                    and (order.payload or {}).get(key) == request["metadata"][key]) for key in
                ("rpg_resting_generation", "fanout_segment_id", "cw_entry_slot")))


def _replacement_terminal_report(order, request, *, source, status, at):
    return {**{key: request[key] for key in
               ("client_order_id", "broker_account_name", "strategy_code", "symbol", "side", "quantity")},
            "order_id": str(order.id), "broker_order_id": order.broker_order_id or "",
            "metadata": {key: request["metadata"][key] for key in
                         ("rpg_resting_generation", "fanout_segment_id", "cw_entry_slot")},
            "source": source, "status": status, "filled_quantity": "0", "reported_at": at}


def replacement_has_fills(session, order):
    if (order.status in {"filled", "partially_filled"}
            or session.scalar(select(Fill.id).where(Fill.order_id == order.id).limit(1)) is not None):
        return True
    for event in session.scalars(select(BrokerOrderEvent).where(
            BrokerOrderEvent.order_id == order.id, BrokerOrderEvent.event_source == "broker")):
        if event.event_type in {"filled", "partially_filled"}:
            return True
        payload = event.payload or {}
        for value in (payload.get("filled_quantity"), payload.get("metadata", {}).get("rpg_terminal_filled_quantity")):
            try:
                quantity = Decimal(value) if value is not None else Decimal(0)
            except (TypeError, ValueError, InvalidOperation):
                continue
            if quantity.is_finite() and quantity > 0:
                return True
    return False


def replacement_unknown_fill_report(session, order):
    """Malformed quantity evidence blocks zero proof without inventing a fill."""
    for event in session.scalars(select(BrokerOrderEvent).where(
            BrokerOrderEvent.order_id == order.id, BrokerOrderEvent.event_source == "broker")):
        payload = event.payload or {}
        for value in (payload.get("filled_quantity"), payload.get("metadata", {}).get("rpg_terminal_filled_quantity")):
            if value is None:
                continue
            try:
                quantity = Decimal(value)
            except (TypeError, ValueError, InvalidOperation):
                return True
            if not quantity.is_finite() or quantity < 0:
                return True
    return False


def legacy_prewire_abort_report(session, job, *, intent_id=None):
    """Exact durable pre-wire audit from the legacy OMS rejection path."""
    request = job.get("replacement", {})
    event_id = request.get("metadata", {}).get("rpg_event_id")
    try:
        client = f"{request['strategy_code']}-{request['symbol']}-open-{UUID(event_id).hex[:12]}"
        quantity = Decimal(request["quantity"])
    except (KeyError, TypeError, ValueError, InvalidOperation):
        return None
    if (client != request.get("client_order_id") or not old_buy_proven_clear(job)
            or not quantity.is_finite() or quantity <= 0):
        return None
    statement = select(TradeIntent).where(TradeIntent.payload["event_id"].as_string() == event_id)
    if intent_id is not None:
        statement = statement.where(TradeIntent.id == UUID(intent_id)).with_for_update()
    intent = session.scalar(statement)
    if intent is None:
        return None
    payload = intent.payload or {}
    md = payload.get("metadata", {})
    account, strategy = session.get(BrokerAccount, intent.broker_account_id), session.get(Strategy, intent.strategy_id)
    if (intent.status != "rejected" or intent.intent_type != "open" or intent.side != "buy"
            or intent.symbol != request.get("symbol") or intent.quantity != quantity
            or not account or account.name != request.get("broker_account_name")
            or not strategy or strategy.code != request.get("strategy_code") or strategy.code != "schwab_1m_v2"
            or not ((payload.get("refusal_origin") == "client_abort"
                     and payload.get("refusal_code") in PREWIRE_ABORT_CODES)
                    or (payload.get("refusal_origin") == "skipped_before_submit"
                        and payload.get("refusal_code") == "webull_mirror_precheck_deferred"))
            or any(not request.get("metadata", {}).get(key) or md.get(key) != request["metadata"][key]
                   for key in ("rpg_handoff_token", "rpg_resting_generation", "fanout_segment_id", "cw_entry_slot"))
            or session.scalar(select(BrokerOrder.id).where(BrokerOrder.client_order_id == client).limit(1)) is not None):
        return None
    at = intent.updated_at.replace(tzinfo=UTC) if intent.updated_at.tzinfo is None else intent.updated_at
    return {**{key: request[key] for key in
               ("client_order_id", "broker_account_name", "strategy_code", "symbol", "side", "quantity")},
            "order_id": "", "intent_id": str(intent.id), "broker_order_id": "",
            "metadata": {key: request["metadata"][key] for key in
                         ("rpg_resting_generation", "fanout_segment_id", "cw_entry_slot")},
            "source": "client_audit", "status": "aborted" if payload["refusal_origin"] == "client_abort" else "rejected",
            "filled_quantity": "0",
            "refusal_code": payload["refusal_code"], "reported_at": at.timestamp()}


def local_rpg_abort_proof(session, order: BrokerOrder, *, job: dict | None = None):
    """Read-only exact no-wire evidence, shared by dispatch and position readers."""
    md = order.payload or {}
    account = session.get(BrokerAccount, order.broker_account_id)
    strategy = session.get(Strategy, order.strategy_id)
    intent = session.get(TradeIntent, order.intent_id)
    audit = session.scalar(select(BrokerOrderEvent).where(
        BrokerOrderEvent.order_id == order.id,
        BrokerOrderEvent.event_type == "aborted",
        BrokerOrderEvent.event_source == "client",
    ).order_by(BrokerOrderEvent.event_at.desc()).limit(1))
    filled = session.scalar(select(Fill.id).where(Fill.order_id == order.id).limit(1))
    code = md.get("refusal_code")
    if (order.status != "aborted" or order.broker_order_id or filled is not None
            or account is None or strategy is None or strategy.code != "schwab_1m_v2"
            or order.side != "buy"
            or md.get("refusal_origin") != "client_abort" or not code
            or intent is None or intent.status != "aborted"
            or intent.strategy_id != order.strategy_id or intent.broker_account_id != order.broker_account_id
            or intent.symbol != order.symbol or intent.quantity != order.quantity
            or intent.side != "buy" or intent.intent_type != "open"
            or (intent.payload or {}).get("refusal_origin") != "client_abort"
            or (intent.payload or {}).get("refusal_code") != code
            or md.get("rpg_local_abort_no_wire") != "true"
            or not md.get("rpg_abort_event_id")
            or md.get("rpg_abort_event_id") != (intent.payload or {}).get("event_id")
            or audit is None or (audit.payload or {}).get("reason") != code
            or any((audit.payload or {}).get("metadata", {}).get(key) != md.get(key) for key in
                   ("rpg_local_abort_no_wire", "rpg_abort_event_id", "refusal_origin", "refusal_code"))):
        return None
    if not md.get("rpg_handoff_token"):
        # This NEW ordinary intent was refused before wire. It proves nothing
        # about the separately owned old order, whose ticket still blocks it.
        if code != "rpg_old_buy_still_owned" or job is not None:
            return None
    else:
        try:
            token = UUID(md["rpg_handoff_token"])
            if job is None:
                row = session.get(DashboardSnapshot, token)
                if row is None or row.snapshot_type != SNAPSHOT_TYPE:
                    return None
                job = row.payload
            replacement = job.get("replacement", {})
            quantity = Decimal(replacement.get("quantity", "0"))
            if not quantity.is_finite() or quantity <= 0:
                return None
        except (ValueError, TypeError, InvalidOperation, AttributeError):
            return None
        if (not old_buy_proven_clear(job)
                or account.name != replacement.get("broker_account_name")
                or strategy.code != replacement.get("strategy_code")
                or order.symbol != replacement.get("symbol") or order.quantity != quantity
                or order.client_order_id != replacement.get("client_order_id")
                or md.get("rpg_handoff_token") != replacement.get("metadata", {}).get("rpg_handoff_token")
                or any(md.get(key) != replacement.get("metadata", {}).get(key) for key in
                       ("rpg_resting_generation", "fanout_segment_id", "cw_entry_slot"))):
            return None
    at = audit.event_at.replace(tzinfo=UTC) if audit.event_at.tzinfo is None else audit.event_at
    return code, at.timestamp()


def broker_rpg_rejection_proof(session, order: BrokerOrder, job: dict):
    """A persisted venue refusal, not a cancel/expiry or status-only assertion."""
    md = order.payload or {}
    replacement = job.get("replacement", {})
    account = session.get(BrokerAccount, order.broker_account_id)
    strategy = session.get(Strategy, order.strategy_id)
    intent = session.get(TradeIntent, order.intent_id)
    audit = session.scalar(select(BrokerOrderEvent).where(
        BrokerOrderEvent.order_id == order.id,
        BrokerOrderEvent.event_type == "rejected",
        BrokerOrderEvent.event_source == "broker",
    ).order_by(BrokerOrderEvent.event_at.desc()).limit(1))
    try:
        quantity = Decimal(replacement.get("quantity", "0"))
        if not quantity.is_finite() or quantity <= 0:
            return None
    except (TypeError, ValueError, InvalidOperation):
        return None
    code = (intent.payload or {}).get("refusal_code") if intent is not None else None
    if (order.status != "rejected" or not old_buy_proven_clear(job)
            or session.scalar(select(Fill.id).where(Fill.order_id == order.id).limit(1)) is not None
            or account is None or account.name != replacement.get("broker_account_name")
            or strategy is None or strategy.code != "schwab_1m_v2"
            or strategy.code != replacement.get("strategy_code")
            or order.symbol != replacement.get("symbol") or order.quantity != quantity
            or order.side != "buy" or order.client_order_id != replacement.get("client_order_id")
            or intent is None or intent.status != "rejected" or intent.intent_type != "open"
            or intent.strategy_id != order.strategy_id or intent.broker_account_id != order.broker_account_id
            or intent.symbol != order.symbol or intent.side != "buy" or intent.quantity != order.quantity
            or (intent.payload or {}).get("refusal_origin") != "broker_reject" or not code
            or md.get("reject_reason") != code
            or any(not md.get(key) or md.get(key) != replacement.get("metadata", {}).get(key) for key in
                   ("rpg_handoff_token", "rpg_resting_generation", "fanout_segment_id", "cw_entry_slot"))
            or audit is None or (audit.payload or {}).get("client_order_id") != order.client_order_id
            or (audit.payload or {}).get("reason") != code):
        return None
    at = audit.event_at.replace(tzinfo=UTC) if audit.event_at.tzinfo is None else audit.event_at
    return code, at.timestamp()


def cancelled_rpg_replacement_proof(session, order: BrokerOrder, job: dict):
    """An exact successor ticket's recorded terminal-zero read, not an order status."""
    replacement = job.get("replacement", {})
    account = session.get(BrokerAccount, order.broker_account_id)
    strategy = session.get(Strategy, order.strategy_id)
    try:
        quantity = Decimal(replacement.get("quantity", "0"))
        if not quantity.is_finite() or quantity <= 0:
            return None
    except (TypeError, ValueError, InvalidOperation):
        return None
    if (order.status not in {"cancelled", "canceled", "expired"} or not old_buy_proven_clear(job)
            or account is None or account.name != replacement.get("broker_account_name")
            or strategy is None or strategy.code != replacement.get("strategy_code")
            or strategy.code != "schwab_1m_v2" or order.side != "buy"
            or not order.broker_order_id
            or order.symbol != replacement.get("symbol") or order.quantity != quantity
            or order.client_order_id != replacement.get("client_order_id")
            or session.scalar(select(Fill.id).where(Fill.order_id == order.id).limit(1)) is not None):
        return None
    keys = ("rpg_resting_generation", "fanout_segment_id", "cw_entry_slot")
    md = replacement.get("metadata", {})
    if any(not md.get(key) or (order.payload or {}).get(key) != md[key] for key in keys):
        return None
    for event in session.scalars(select(BrokerOrderEvent).where(
            BrokerOrderEvent.order_id == order.id,
            BrokerOrderEvent.event_source == "broker",
            BrokerOrderEvent.event_type.in_({"cancelled", "canceled", "expired"}))):
        payload = event.payload or {}
        evidence = payload.get("metadata", {})
        if (payload.get("client_order_id") == order.client_order_id
                and payload.get("broker_order_id") == order.broker_order_id
                and evidence.get("rpg_terminal_filled_quantity") == "0"
                and evidence.get("atr_reprice_terminal_cancel") == "true"
                and all(evidence.get(key) == md[key] for key in keys)):
            at = event.event_at.replace(tzinfo=UTC) if event.event_at.tzinfo is None else event.event_at
            return at.timestamp()
    if order.status == "expired":
        return None
    successors = session.scalars(select(DashboardSnapshot).where(
        DashboardSnapshot.snapshot_type == SNAPSHOT_TYPE,
        DashboardSnapshot.payload["original_order_id"].as_string() == str(order.id)))
    for row in successors:
        proof, old = row.payload, row.payload.get("old", {})
        try:
            old_quantity = Decimal(old.get("quantity", "0"))
        except (TypeError, ValueError, InvalidOperation):
            continue
        if (old_buy_proven_clear(proof) and proof.get("cleared_at") is not None
                and proof.get("clear_recorded") is True
                and proof.get("broker_status") in {"CANCELED", "CANCELLED"}
                and old.get("client_order_id") == order.client_order_id
                and old.get("broker_account_name") == account.name
                and old.get("strategy_code") == strategy.code and old.get("symbol") == order.symbol
                and old.get("side") == "buy" and old.get("intent_type") == "cancel"
                and str(old.get("metadata", {}).get("broker_order_id", "")) == str(order.broker_order_id or "")
                and all(old.get("metadata", {}).get(key) == md[key] for key in keys)
                and old_quantity.is_finite() and old_quantity == quantity):
            return proof["cleared_at"]
    return None


def _request_dict(request: OrderRequest) -> dict:
    return {
        "client_order_id": request.client_order_id,
        "broker_account_name": request.broker_account_name,
        "strategy_code": request.strategy_code,
        "symbol": request.symbol,
        "side": request.side,
        "intent_type": request.intent_type,
        "quantity": str(request.quantity),
        "reason": request.reason,
        "metadata": dict(request.metadata),
        "order_type": request.order_type,
        "time_in_force": request.time_in_force,
    }


def _request(payload: dict) -> OrderRequest:
    return OrderRequest(**{**payload, "quantity": Decimal(payload["quantity"])})


@dataclass(frozen=True)
class ReplacementDecision:
    verdict: Literal["ready", "wait", "expired"]
    reason: str
    request: OrderRequest | None = None


class HandoffJournal:
    """One durable identity per old order; CAS before every broker side effect.

    Postgres row locks serialize competing workers. A crash in ``submitting`` is
    UNKNOWN, not permission to send again. The original client id remains owned.
    """

    def __init__(self, session_factory: sessionmaker):
        self.session_factory = session_factory

    def prepare(self, old: OrderRequest, *, slot: str, segment_id: int, now: float) -> UUID:
        if not scoped_request(old, allow_webull_client_identity=True) or slot not in {"first", "reclaim"} or segment_id <= 0:
            raise ValueError("reprice requires a scoped old BUY, slot and segment")
        token = uuid5(NAMESPACE_URL, f"atr-reprice:{old.broker_account_name}:{old.client_order_id}")
        payload = {
            "revision": 0, "phase": "prepared", "old": _request_dict(old),
            "slot": slot, "segment_id": segment_id, "created_at": now,
            "next_read_at": now, "reads": 0, "reason": "prepared_before_cancel",
        }
        with self.session_factory() as session:
            existing = session.get(DashboardSnapshot, token)
            if existing is not None:
                prior = existing.payload
                if (existing.snapshot_type != SNAPSHOT_TYPE or prior["old"] != payload["old"]
                        or prior["slot"] != slot or prior["segment_id"] != segment_id):
                    raise ValueError("old order already belongs to a different reprice")
                return token
            session.add(DashboardSnapshot(id=token, snapshot_type=SNAPSHOT_TYPE, payload=payload))
            session.commit()
        return token

    def read(self, token: UUID, *, session=None) -> dict:
        with (nullcontext(session) if session is not None else self.session_factory()) as session:
            row = session.get(DashboardSnapshot, token, populate_existing=True)
            if row is None or row.snapshot_type != SNAPSHOT_TYPE:
                raise ValueError("reprice ownership unreadable")
            return dict(row.payload)

    def prepare_local(self, old: OrderRequest, *, slot: str, segment_id: int,
                      now: float, phase: str, reason: str, session, **evidence) -> UUID:
        """Persist no-wire evidence or an unresolved target, never fake a broker read."""
        if phase not in {"clear", "held_unknown"}:
            raise ValueError("invalid local ownership phase")
        token = uuid5(NAMESPACE_URL, f"atr-reprice:{old.broker_account_name}:{old.client_order_id}")
        if session.get(DashboardSnapshot, token) is None:
            session.add(DashboardSnapshot(id=token, snapshot_type=SNAPSHOT_TYPE, payload={
                "revision": 0, "phase": phase, "old": _request_dict(old),
                "slot": slot, "segment_id": segment_id, "created_at": now,
                "next_read_at": now, "reads": 0, "reason": reason, **evidence,
            }))
            session.flush()
        return token

    def jobs(self, *, session=None) -> list[tuple[UUID, dict]]:
        with (nullcontext(session) if session is not None else self.session_factory()) as session:
            return [(row.id, dict(row.payload)) for row in session.scalars(
                select(DashboardSnapshot).where(DashboardSnapshot.snapshot_type == SNAPSHOT_TYPE)
            )]

    def reconcile_feedback(self, token: UUID, job: dict, *, include_wire_prices: bool = False) -> dict:
        """Use committed OMS accounting to avoid restoring a filled/dead resting latch.

        This never sends or repeats an order. A pre-wire pending row cannot prove
        acceptance, whereas a committed broker report can resolve submit-unknown.
        """
        if not replacement_needs_reconciliation(job):
            return job
        with self.session_factory() as session:
            order = session.scalar(select(BrokerOrder).where(
                BrokerOrder.client_order_id == job["replacement"]["client_order_id"]))
            if order is None:
                proof = legacy_prewire_abort_report(session, job)
                if proof is None:
                    return job
                if (job["phase"] == "refused" and job.get("reason") == "replacement_refused"
                        and job.get("replacement_terminal_report") == proof):
                    return job
                return self.change(token, job["revision"], _expected_replacement=job["replacement"],
                    _expected_intent=proof["intent_id"], phase="refused", reason="replacement_refused",
                    replacement_terminal_report=proof) or self.read(token)
            if not replacement_order_matches(session, order, job["replacement"]):
                return job
            guard = (str(order.id), order.broker_order_id)
            if replacement_has_fills(session, order):
                updates = dict(phase="filled", replacement_filled=True, no_rebuy=True,
                               reason="replacement_fill_accounted")
            elif replacement_unknown_fill_report(session, order):
                return job
            elif order.status == "aborted":
                if (order.payload or {}).get("rpg_handoff_token") != str(token):
                    return job
                proof = local_rpg_abort_proof(session, order, job=job)
                if proof is None:
                    return job
                updates = dict(phase="refused", reason="replacement_refused",
                               replacement_reasons=[proof[0]], completed_at=proof[1],
                               replacement_terminal_report=_replacement_terminal_report(order, job["replacement"],
                                   source="client_audit", status="aborted", at=proof[1]))
            elif order.status == "rejected" and (proof := broker_rpg_rejection_proof(session, order, job)):
                updates = dict(phase="refused", reason="replacement_refused",
                               replacement_reasons=[proof[0]], completed_at=proof[1],
                               replacement_terminal_report=_replacement_terminal_report(order, job["replacement"],
                                   source="broker", status="rejected", at=proof[1]))
            elif order.status in {"cancelled", "canceled", "expired"}:
                at = cancelled_rpg_replacement_proof(session, order, job)
                if at is None:
                    return job
                updates = dict(phase="refused", reason="replacement_terminal_accounted",
                               replacement_terminal_report=_replacement_terminal_report(order, job["replacement"],
                                   source="broker", status=order.status, at=at))
            elif order.status in {"cancelled", "canceled", "expired", "rejected"}:
                return job  # A status alone cannot retire ownership of a possibly sent BUY.
            elif order.broker_order_id and order.status in {"accepted", "working", "open"}:
                updates = dict(phase="placed", reason="replacement_accepted_accounted")
                if include_wire_prices:
                    md = order.payload or {}
                    if md.get("fanout_leg") == "webull":
                        report = session.scalar(select(BrokerOrderEvent).where(
                            BrokerOrderEvent.order_id == order.id,
                            BrokerOrderEvent.event_type == "accepted",
                            BrokerOrderEvent.event_source == "broker",
                        ).order_by(BrokerOrderEvent.event_at.desc()).limit(1))
                        wire_md = (report.payload or {}).get("metadata", {}) if report else {}
                        stop = wire_md.get("webull_wire_stop_price") or wire_md.get(
                            "webull_resting_mirror_original_stop_price")
                        limit = wire_md.get("webull_wire_limit_price")
                    else:
                        # Schwab consumes these formatted request values without another price round.
                        stop, limit = md.get("stop_price"), md.get("limit_price")
                    if stop is not None and limit is not None:
                        wire = {"stop_price": str(stop), "limit_price": str(limit)}
                        if proven_resting_pair(wire) and wire != job.get("replacement_wire_prices"):
                            updates["replacement_wire_prices"] = wire
            else:
                return job
        if updates["phase"] == job["phase"] and all(job.get(key) == value for key, value in updates.items()):
            return job
        return self.change(token, job["revision"], _expected_replacement=job["replacement"],
                           _expected_order=guard, **updates) or self.read(token)

    def change(self, token: UUID, revision: int, *, _expected_replacement: dict | None = None,
               _expected_order: tuple | None = None, _expected_intent: str | None = None,
               **changes: object) -> dict | None:
        with self.session_factory() as session:
            row = session.scalar(select(DashboardSnapshot).where(
                DashboardSnapshot.id == token,
                DashboardSnapshot.snapshot_type == SNAPSHOT_TYPE,
            ).with_for_update())
            if row is None:
                raise ValueError("reprice ownership unreadable")
            if row.payload["revision"] != revision:
                return None
            if _expected_replacement is not None and row.payload.get("replacement") != _expected_replacement:
                return None
            if _expected_intent is not None:
                report = legacy_prewire_abort_report(session, row.payload, intent_id=_expected_intent)
                if report is None or report != changes.get("replacement_terminal_report"):
                    return None
            if _expected_order is not None:
                order = session.scalar(select(BrokerOrder).where(
                    BrokerOrder.id == UUID(_expected_order[0])).with_for_update())
                if (order is None or order.broker_order_id != _expected_order[1]
                        or not replacement_order_matches(session, order, _expected_replacement)):
                    return None
                if changes.get("replacement_terminal_report"):
                    if replacement_has_fills(session, order) or replacement_unknown_fill_report(session, order):
                        return None
                    proof_job = dict(row.payload)
                    report = changes["replacement_terminal_report"]
                    if report["status"] in {"cancelled", "canceled", "expired"}:
                        valid = cancelled_rpg_replacement_proof(session, order, proof_job)
                    elif report["status"] == "aborted":
                        valid = local_rpg_abort_proof(session, order, job=proof_job)
                    elif report["status"] == "rejected":
                        valid = broker_rpg_rejection_proof(session, order, proof_job)
                    else:
                        valid = None
                    if valid is None:
                        return None
            row.payload = {**row.payload, **changes, "revision": revision + 1}
            result = dict(row.payload)
            session.commit()
            return result


class AtrRepriceHandoff:
    """One advance per scheduled turn; independent journal/lock for each leg.

    The initial turn cancels and immediately reads. Later turns read at most once
    per second, at most thirty times total, with two seconds per read. Exhaustion
    retains ownership and forbids a new BUY. No native replace or place-first.
    """

    def __init__(
        self, *, journal: HandoffJournal, adapter: object,
        prepare_replacement: Callable[[dict], Awaitable[ReplacementDecision]],
        submit_replacement: Callable[[OrderRequest], Awaitable[list[ExecutionReport]]],
        record_fill: Callable[[OrderRequest, ExecutionReport], Awaitable[bool]],
        now: Callable[[], float],
    ):
        self.journal = journal
        self.adapter = adapter
        self.prepare_replacement = prepare_replacement
        self.submit_replacement = submit_replacement
        self.record_fill = record_fill
        self.now = now
        self._locks: dict[UUID, asyncio.Lock] = {}

    async def advance(self, token: UUID) -> dict:
        async with self._locks.setdefault(token, asyncio.Lock()):
            return await self._advance(token)

    async def _advance(self, token: UUID) -> dict:
        job = self.journal.read(token)
        old = _request(job["old"])
        if job["phase"] == "prepared":
            job = self.journal.change(token, job["revision"], phase="waiting",
                                      cancel_started_at=self.now(), reason="cancel_started")
            if job is None:
                return self.journal.read(token)
            try:
                # An ACK, rejection, exception or timeout cannot authorize replacement.
                await asyncio.wait_for(self.adapter.submit_order(old), READ_TIMEOUT_SECONDS)
            except Exception:
                pass

        if job["phase"] in {"waiting", "fills_waiting"}:
            if self.now() < job["next_read_at"]:
                return job
            if job["reads"] >= MAX_READS:
                return self.journal.change(token, job["revision"], phase="held_unknown",
                                           reason="readback_budget_exhausted") or self.journal.read(token)
            job = self.journal.change(token, job["revision"], reads=job["reads"] + 1,
                                      next_read_at=self.now() + READ_INTERVAL_SECONDS)
            if job is None:
                return self.journal.read(token)
            try:
                readback = await asyncio.wait_for(
                    self.adapter.read_atr_resting_buy_after_cancel(old), READ_TIMEOUT_SECONDS
                )
                if not isinstance(readback, AtrBuyReadback):
                    readback = AtrBuyReadback("unknown", "invalid_readback_type")
            except Exception as exc:
                readback = AtrBuyReadback("unknown", f"readback_raised:{type(exc).__name__}")
            # A different worker may have advanced the persisted record during the read.
            current = self.journal.read(token)
            if current["revision"] != job["revision"]:
                return current
            observed = {"reason": readback.reason, "readback_at": self.now(),
                        "broker_status": readback.broker_status}
            if readback.broker_order_id and not old.metadata.get("broker_order_id"):
                # Webull accepts with only the client id. Its exact fresh detail
                # binds the broker id before accounting or any replacement.
                old = replace(old, metadata={**old.metadata, "broker_order_id": readback.broker_order_id})
                observed["old"] = _request_dict(old)
            if readback.outcome == "fills":
                cumulative = readback.cumulative_filled
                if cumulative is None or not cumulative.is_finite() or not 0 < cumulative <= old.quantity:
                    return self.journal.change(token, job["revision"],
                        reason="invalid_positive_fill_evidence") or self.journal.read(token)
                job = self.journal.change(token, job["revision"], **observed,
                    phase="fills_waiting", filled_quantity=str(cumulative), no_rebuy=True)
                if job is None:
                    return self.journal.read(token)
                price = readback.fill_price
                if price is None or not price.is_finite() or price <= 0:
                    return job  # Still owned. Never invent a fill price to make the proof green.
                report = ExecutionReport(
                    event_type="filled" if cumulative == old.quantity else "partially_filled",
                    client_order_id=old.client_order_id,
                    broker_order_id=old.metadata["broker_order_id"],
                    symbol=old.symbol, side="buy", intent_type="open", quantity=old.quantity,
                    filled_quantity=cumulative, fill_price=price, origin="broker",
                    metadata={**old.metadata, "atr_reprice_readback": str(token),
                              "atr_reprice_terminal_cancel": str(readback.terminal_cancel).lower()},
                    reason="atr_reprice_cancel_raced_fill",
                )
                try:
                    recorded = await self.record_fill(old, report)
                except Exception:
                    recorded = False
                if not recorded:
                    return self.journal.change(token, job["revision"],
                        reason="fill_accounting_not_confirmed") or self.journal.read(token)
                return self.journal.change(token, job["revision"],
                    phase="filled" if readback.terminal_cancel or cumulative == old.quantity else "fills_waiting",
                    recorded_filled_quantity=str(cumulative), reason="fill_recorded_no_rebuy",
                ) or self.journal.read(token)
            if job.get("no_rebuy"):
                # Cumulative fills cannot subsequently become zero and mint another full BUY.
                return self.journal.change(token, job["revision"],
                    reason="fill_evidence_retained_no_rebuy") or self.journal.read(token)
            if not readback.can_replace:
                return self.journal.change(token, job["revision"], **observed) or self.journal.read(token)
            job = self.journal.change(token, job["revision"], **observed,
                                      phase="clear", cleared_at=self.now())
            if job is None:
                return self.journal.read(token)

        if job["phase"] != "clear":
            return job
        # The strategy must evaluate current bars/quotes/segment/window, not a saved price.
        try:
            decision = await self.prepare_replacement(job)
        except Exception as exc:
            decision = ReplacementDecision("wait", f"strategy_unreadable:{type(exc).__name__}")
        if decision.verdict != "ready":
            return self.journal.change(token, job["revision"], reason=decision.reason,
                phase="expired" if decision.verdict == "expired" else "clear") or self.journal.read(token)
        replacement = decision.request
        if not self._valid_replacement(old, replacement):
            return self.journal.change(token, job["revision"],
                reason="replacement_scope_invalid") or self.journal.read(token)
        job = self.journal.change(token, job["revision"], phase="submitting",
                                  replacement=_request_dict(replacement), submit_started_at=self.now())
        if job is None:
            return self.journal.read(token)
        try:
            reports = await self.submit_replacement(replacement)
        except Exception as exc:
            return self.journal.change(token, job["revision"], phase="submit_unknown",
                reason=f"replacement_raised:{type(exc).__name__}") or self.journal.read(token)
        attributable = [report for report in reports if report.client_order_id == replacement.client_order_id]
        accepted = any(report.event_type in {"accepted", "filled", "partially_filled"}
                       and report.origin == "broker" for report in attributable)
        rejected = bool(attributable) and all(
            report.event_type == "rejected" or (report.event_type == "aborted" and report.origin == "client")
            for report in attributable
        )
        deferred = rejected and any(report.metadata.get("rpg_price_wait_owner") == "true"
                                    for report in attributable)
        # Current-gate feedback can advance the revision during the awaited submit.
        current = self.journal.read(token)
        if current["phase"] != "submitting" or current.get("replacement") != job["replacement"]:
            return current
        job = current
        return self.journal.change(token, job["revision"],
            phase="placed" if accepted else "price_wait" if deferred else "refused" if rejected else "submit_unknown",
            reason="replacement_accepted" if accepted else "replacement_refused" if rejected else "replacement_unproven",
            completed_at=self.now(),
            replacement_filled=any(r.event_type in {"filled", "partially_filled"} for r in attributable),
            replacement_reasons=[report.reason for report in attributable],
        ) or self.journal.read(token)

    @staticmethod
    def _valid_replacement(old: OrderRequest, replacement: OrderRequest | None) -> bool:
        return bool(
            replacement is not None and replacement.intent_type == "open"
            and replacement.side == "buy" and replacement.strategy_code == old.strategy_code
            and replacement.symbol == old.symbol
            and replacement.broker_account_name == old.broker_account_name
            and replacement.client_order_id and replacement.client_order_id != old.client_order_id
            and replacement.quantity.is_finite() and replacement.quantity > 0
            and replacement.metadata.get("resting_entry") == "true"
        )
