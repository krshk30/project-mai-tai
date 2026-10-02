"""Durable per-order cancel/read/replace coordinator for ATR resting BUYs.

This module does not bypass the OMS intent lane. Its replacement and fill callbacks
must use that lane and the original BUY's normal fill accounting respectively.
Cancellation acknowledgements are deliberately not clearance evidence.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from decimal import Decimal
from typing import Awaitable, Callable, Literal
from uuid import UUID, uuid5, NAMESPACE_URL

from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from project_mai_tai.broker_adapters.atr_buy_readback import AtrBuyReadback, scoped_request
from project_mai_tai.broker_adapters.protocols import ExecutionReport, OrderRequest
from project_mai_tai.db.models import DashboardSnapshot


SNAPSHOT_TYPE = "atr_reprice_handoff"
READ_INTERVAL_SECONDS = 1.0
READ_TIMEOUT_SECONDS = 2.0
MAX_READS = 30


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
        if not scoped_request(old) or slot not in {"first", "reclaim"} or segment_id <= 0:
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

    def read(self, token: UUID) -> dict:
        with self.session_factory() as session:
            row = session.get(DashboardSnapshot, token)
            if row is None or row.snapshot_type != SNAPSHOT_TYPE:
                raise ValueError("reprice ownership unreadable")
            return dict(row.payload)

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
                    metadata={**old.metadata, "atr_reprice_readback": str(token)},
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
        rejected = bool(attributable) and all(report.event_type == "rejected" for report in attributable)
        return self.journal.change(token, job["revision"],
            phase="placed" if accepted else "refused" if rejected else "submit_unknown",
            reason="replacement_accepted" if accepted else "replacement_refused" if rejected else "replacement_unproven",
            completed_at=self.now(),
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
