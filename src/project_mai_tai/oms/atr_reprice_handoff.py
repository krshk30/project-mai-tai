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


def old_buy_proven_clear(job: dict) -> bool:
    return bool((job.get("local_no_wire") or job.get("cleared_at") is not None)
                and not job.get("no_rebuy"))


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
        if job["phase"] not in {"placed", "submitting", "submit_unknown"} or not job.get("replacement"):
            return job
        with self.session_factory() as session:
            order = session.scalar(select(BrokerOrder).where(
                BrokerOrder.client_order_id == job["replacement"]["client_order_id"]))
            if order is None or order.status == "pending":
                return job
            filled = session.scalar(select(Fill.id).where(Fill.order_id == order.id).limit(1))
            if filled is not None or order.status in {"filled", "partially_filled"}:
                updates = dict(phase="filled", replacement_filled=True, reason="replacement_fill_accounted")
            elif order.status == "aborted":
                if (order.payload or {}).get("rpg_handoff_token") != str(token):
                    return job
                proof = local_rpg_abort_proof(session, order, job=job)
                if proof is None:
                    return job
                updates = dict(phase="refused", reason="replacement_refused",
                               replacement_reasons=[proof[0]], completed_at=proof[1])
            elif order.status == "rejected" and (proof := broker_rpg_rejection_proof(session, order, job)):
                updates = dict(phase="refused", reason="replacement_refused",
                               replacement_reasons=[proof[0]], completed_at=proof[1])
            elif order.status in {"cancelled", "canceled", "expired", "rejected"}:
                updates = dict(phase="refused", reason="replacement_terminal_accounted")
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
        if updates["phase"] == job["phase"] and "replacement_wire_prices" not in updates:
            return job
        return self.change(token, job["revision"], **updates) or self.read(token)

    def change(self, token: UUID, revision: int, **changes: object) -> dict | None:
        with self.session_factory() as session:
            row = session.scalar(select(DashboardSnapshot).where(
                DashboardSnapshot.id == token,
                DashboardSnapshot.snapshot_type == SNAPSHOT_TYPE,
            ).with_for_update())
            if row is None:
                raise ValueError("reprice ownership unreadable")
            if row.payload["revision"] != revision:
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
