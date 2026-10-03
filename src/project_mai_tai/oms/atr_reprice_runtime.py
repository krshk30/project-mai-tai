"""OMS runtime binding for the durable ATR cancel/readback handoff.

Only the serial intent consumer advances a coordinator or places a replacement.
The retry task merely wakes that consumer; v2 commits current-gate authorization.
"""
from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, time
from decimal import Decimal, InvalidOperation
import json
from uuid import UUID, uuid5, NAMESPACE_URL
from zoneinfo import ZoneInfo

from sqlalchemy import select

from project_mai_tai.broker_adapters.protocols import ExecutionReport, OrderRequest
from project_mai_tai.db.models import BrokerAccount, BrokerOrder, DashboardSnapshot, Fill, TradeIntent
from project_mai_tai.events import TradeIntentEvent, TradeIntentPayload
from project_mai_tai.oms.atr_reprice_handoff import (
    AtrRepriceHandoff, HandoffJournal, ReplacementDecision, SNAPSHOT_TYPE, _request,
)


ACTIVE_PHASES = {"prepared", "waiting", "fills_waiting", "clear", "held_unknown",
                 "submitting", "submit_unknown", "price_wait"}


def reprice_window_open(now: datetime) -> bool:
    et = now.astimezone(ZoneInfo("America/New_York"))
    return et.weekday() < 5 and time(9, 30) <= et.time() < time(15, 45)


class AtrRepriceRuntimeMixin:
    def _rpg_owns_old_order(self, session, order, account):
        if session is None:
            return False
        token = uuid5(NAMESPACE_URL, f"atr-reprice:{account}:{order.client_order_id}")
        row = session.get(DashboardSnapshot, token, populate_existing=True)
        return row is not None and row.snapshot_type == SNAPSHOT_TYPE

    def _rpg_external_retry(self, event):
        token = event.payload.metadata.get("rpg_handoff_token")
        if (event.payload.intent_type == "open" and token
                and self.__dict__.get("_rpg_dispatch_token") != token):
            self.logger.info("[OMS-RPG1] token=%s ignored=1 reason=handoff_owns_retry", token)
            return True
        return False

    def _rpg_now(self):
        from project_mai_tai.oms.service import utcnow
        return utcnow()

    def _rpg_journal(self):
        return HandoffJournal(self.session_factory)

    def _rpg_controller(self):
        controller = self.__dict__.get("_atr_reprice_controller")
        if controller is None:
            controller = AtrRepriceHandoff(
                journal=self._rpg_journal(), adapter=self.broker_adapter,
                prepare_replacement=self._rpg_prepare_replacement,
                submit_replacement=self._rpg_submit_replacement,
                record_fill=self._rpg_record_fill, now=lambda: self._rpg_now().timestamp(),
            )
            self._atr_reprice_controller = controller
        return controller

    async def _rpg_begin_cancel(self, event):
        md = event.payload.metadata
        journal = self._rpg_journal()
        # Replay of the cancel envelope resumes its ticket, never picks a newer order.
        for token, job in journal.jobs():
            if job.get("cancel_event_id") == str(event.event_id):
                await self._rpg_advance(token)
                return []
        hold = self.__dict__.get("_nfq_price_holds", {}).get(md.get("fanout_slot_id"))
        local = (hold is not None and hold.phase in {"held", "queued"}
                 and hold.event.payload.symbol == event.payload.symbol
                 and hold.event.payload.broker_account_name == event.payload.broker_account_name
                 and hold.event.payload.metadata.get("fanout_segment_id") == md.get("fanout_segment_id")
                 and (not md.get("rpg_resting_generation") or
                      md["rpg_resting_generation"] == hold.event.payload.metadata.get("rpg_resting_generation"))
                 and (not md.get("webull_mirror_generation_id") or
                      md["webull_mirror_generation_id"] == hold.event.payload.metadata.get("webull_mirror_generation_id")))
        with self.session_factory() as session:
            account = session.scalar(select(BrokerAccount).where(
                BrokerAccount.name == event.payload.broker_account_name))
            candidates = [] if account is None else list(session.scalars(select(BrokerOrder).where(
                BrokerOrder.broker_account_id == account.id,
                BrokerOrder.symbol == event.payload.symbol, BrokerOrder.side == "buy",
            ).order_by(BrokerOrder.submitted_at.desc())))
            candidates = [order for order in candidates
                if (order.payload or {}).get("resting_entry") == "true"
                and str((order.payload or {}).get("fanout_segment_id")) == md.get("fanout_segment_id")
                and (order.payload or {}).get("cw_entry_slot", "first") == md.get("cw_entry_slot")
                and (not md.get("rpg_resting_generation") or
                     (order.payload or {}).get("rpg_resting_generation") == md["rpg_resting_generation"])]
            if not md.get("rpg_resting_generation"):
                # Legacy restored rests have no generation tag. Require the old
                # stop too, and never let historical cancelled rows hide two live buys.
                try:
                    expected = Decimal(md["rpg_old_stop_price"])
                    candidates = [order for order in candidates if
                        Decimal(str((order.payload or {}).get("stop_price", "NaN"))) == expected]
                except (KeyError, InvalidOperation):
                    candidates = []
                live = [order for order in candidates if order.status not in
                        {"cancelled", "canceled", "rejected", "expired"}]
                candidates = live or candidates[:1]
            target = candidates[0] if candidates else None
            client_identity = self.settings.provider_for_account(event.payload.broker_account_name) == "webull"
            if target is None or (not target.broker_order_id and not client_identity) or len(candidates) > 1:
                local = local and not candidates
                opening = hold.event if local else event
                old = OrderRequest(client_order_id=self._build_client_order_id(opening),
                    broker_account_name=event.payload.broker_account_name,
                    strategy_code="schwab_1m_v2", symbol=event.payload.symbol, side="buy",
                    intent_type="cancel", quantity=opening.payload.quantity, reason=event.payload.reason,
                    metadata={**opening.payload.metadata, **md}, order_type="STOP_LIMIT", time_in_force="day")
                token = journal.prepare_local(old, slot=md["cw_entry_slot"],
                    segment_id=int(md["fanout_segment_id"]), now=self._rpg_now().timestamp(),
                    phase="clear" if local else "held_unknown",
                    reason="nfq_proven_no_wire" if local else "exact_old_order_unproven", session=session,
                    local_no_wire=bool(local), cancel_event_id=str(event.event_id))
                if local:
                    self._nfq_retire(session, hold, "v2_cancel_intent")
                session.commit()
                self.logger.warning("[OMS-RPG1] symbol=%s reason=%s", event.payload.symbol,
                                    "nfq_proven_no_wire" if local else "exact_old_order_unproven")
                return []
            old_md = {**dict(target.payload or {}), **md,
                      "broker_order_id": target.broker_order_id or "",
                      "target_client_order_id": target.client_order_id}
            if client_identity:
                old_md["atr_reprice_identity"] = "webull_client_order_id"
            old = OrderRequest(client_order_id=target.client_order_id,
                broker_account_name=event.payload.broker_account_name,
                strategy_code="schwab_1m_v2", symbol=target.symbol, side="buy", intent_type="cancel",
                quantity=target.quantity, reason=event.payload.reason, metadata=old_md,
                order_type=target.order_type, time_in_force=target.time_in_force)
            original_order_id = str(target.id)
        prior_token = uuid5(NAMESPACE_URL, f"atr-reprice:{old.broker_account_name}:{old.client_order_id}")
        if any(token == prior_token for token, _ in journal.jobs()):
            await self._rpg_advance(prior_token)
            return []
        token = journal.prepare(old, slot=md["cw_entry_slot"],
                                segment_id=int(md["fanout_segment_id"]), now=self._rpg_now().timestamp())
        job = journal.read(token)
        if not job.get("cancel_event_id"):
            journal.change(token, job["revision"], cancel_event_id=str(event.event_id),
                           original_order_id=original_order_id)
        await self._rpg_advance(token)
        return []

    async def _rpg_advance(self, token):
        journal = self._rpg_journal()
        job = journal.read(token)
        if job["phase"] == "price_wait":
            auth = job.get("authorization", {})
            if (auth.get("verdict") == "wait" or
                    self._rpg_now().timestamp() < job.get("completed_at", 0) + 1.0 or
                    not 0 <= self._rpg_now().timestamp() - auth.get("at", 0) <= 1):
                return job
            if not self._rpg_retire_price_wait(job):
                return job  # An uncertain dispatch is not a no-wire price wait.
            price_refusal = any("PRICE_AGGRESSIVE" in (reason or "")
                                for reason in job.get("replacement_reasons", []))
            if price_refusal and job.get("attempt", 0) >= self._WEBULL_MIRROR_RESUBMIT_MAX_ATTEMPTS:
                return journal.change(token, job["revision"], phase="refused", reason="price_retry_budget_exhausted")
            job = journal.change(token, job["revision"], phase="clear",
                                 attempt=job.get("attempt", 0) + 1)
            if job is None:
                return journal.read(token)
        job = await self._rpg_controller().advance(token)
        self.logger.info("[OMS-RPG1] token=%s phase=%s reason=%s reads=%s",
                         token, job["phase"], job["reason"], job["reads"])
        return job

    def _rpg_retire_price_wait(self, job):
        request = job["replacement"]
        md = request["metadata"]
        slot = md.get("fanout_slot_id")
        hold = self.__dict__.get("_nfq_price_holds", {}).get(slot)
        deferred = self.__dict__.get("_webull_mirror_deferred_by_slot", {}).get(slot)
        if hold is not None and hold.event.payload.metadata.get("rpg_handoff_token") == md["rpg_handoff_token"]:
            if hold.phase not in {"held", "queued"}:
                return False
            with self.session_factory() as session:
                self._nfq_retire(session, hold, "v2_cancel_intent")
                session.commit()
            return True
        if deferred is not None and deferred.event.payload.metadata.get("rpg_handoff_token") == md["rpg_handoff_token"]:
            self._forget_webull_mirror_deferred(slot, reason="rpg_reauthorizes_price_wait")
            return True
        # A retired no-wire hold (expiry, segment change) must end, not resurrect.
        return job.get("authorization", {}).get("verdict") == "expired"

    async def _rpg_prepare_replacement(self, job):
        if job.get("cleared_at") and not job.get("clear_recorded"):
            old = _request(job["old"])
            # Terminal-zero belongs to the original OPEN and precedes the new attempt.
            recorded = await self._rpg_record_fill(old, ExecutionReport(
                "cancelled", old.client_order_id, broker_order_id=old.metadata["broker_order_id"],
                symbol=old.symbol, side="buy", intent_type="open", quantity=old.quantity,
                origin="broker", reason="atr_reprice_terminal_cancel_explicit_zero"))
            if not recorded:
                return ReplacementDecision("wait", "cancel_accounting_not_confirmed")
            token = uuid5(NAMESPACE_URL, f"atr-reprice:{old.broker_account_name}:{old.client_order_id}")
            self._rpg_journal().change(token, job["revision"], clear_recorded=True)
            return ReplacementDecision("wait", "terminal_zero_recorded")
        if not reprice_window_open(self._rpg_now()):
            return ReplacementDecision("expired", "window_1545")
        auth = job.get("authorization", {})
        if not 0 <= self._rpg_now().timestamp() - auth.get("at", 0) <= 1.0:
            return ReplacementDecision("wait", "current_strategy_authorization_required")
        if auth.get("verdict") != "ready":
            return ReplacementDecision(auth.get("verdict", "wait"), auth.get("reason", "gates_unknown"))
        event = TradeIntentEvent.model_validate(auth["event"])
        token = UUID(event.payload.metadata["rpg_handoff_token"])
        with self.session_factory() as session:
            if job.get("local_no_wire"):
                old = None
            else:
                old = session.get(BrokerOrder, UUID(job["original_order_id"]))
            if old is None and not job.get("local_no_wire"):
                return ReplacementDecision("wait", "original_order_missing")
            if old is not None and session.scalar(select(Fill.id).where(Fill.order_id == old.id).limit(1)):
                return ReplacementDecision("expired", "original_fill_no_rebuy")
        event.event_id = uuid5(NAMESPACE_URL, f"rpg-replacement:{token}:{job.get('attempt', 0)}")
        event.payload.metadata["rpg_event_id"] = str(event.event_id)
        event.payload.metadata["rpg_resting_generation"] = f"{token}:{job.get('attempt', 0)}"
        event.payload.metadata["webull_deferred_resubmit_attempt"] = str(job.get("attempt", 0))
        return ReplacementDecision("ready", "current_strategy_gates", OrderRequest(
            client_order_id=self._build_client_order_id(event),
            broker_account_name=event.payload.broker_account_name,
            strategy_code=event.payload.strategy_code, symbol=event.payload.symbol,
            side="buy", intent_type="open", quantity=event.payload.quantity,
            reason=event.payload.reason, metadata=event.payload.metadata,
            order_type=event.payload.metadata["order_type"], time_in_force="day"))

    async def _rpg_submit_replacement(self, request):
        token = request.metadata["rpg_handoff_token"]
        event = TradeIntentEvent(event_id=UUID(request.metadata["rpg_event_id"]),
            produced_at=self._rpg_now(), source_service="schwab-1m-v2",
            payload=TradeIntentPayload(strategy_code=request.strategy_code,
                broker_account_name=request.broker_account_name, symbol=request.symbol,
                side="buy", intent_type="open", quantity=request.quantity,
                reason=request.reason, metadata=dict(request.metadata)))
        self._rpg_dispatch_token = token
        try:
            events = await self.process_trade_intent(event)
        finally:
            self._rpg_dispatch_token = None
        slot = request.metadata.get("fanout_slot_id")
        owner = (self.__dict__.get("_nfq_price_holds", {}).get(slot)
                 or self.__dict__.get("_webull_mirror_deferred_by_slot", {}).get(slot))
        waiting = (owner is not None and owner.event.payload.metadata.get("rpg_handoff_token") == token)
        return [ExecutionReport(event_type=e.payload.status,
            client_order_id=request.client_order_id, broker_order_id=e.payload.broker_order_id,
            symbol=request.symbol, side="buy", intent_type="open", quantity=request.quantity,
            filled_quantity=e.payload.filled_quantity, fill_price=e.payload.fill_price,
            reason=e.payload.reason, metadata={**e.payload.metadata,
                **({"rpg_price_wait_owner": "true"} if waiting else {})},
            origin="broker" if e.payload.status in {"accepted", "filled", "partially_filled"} else "client")
            for e in events]

    async def _rpg_record_fill(self, old, report):
        with self.session_factory() as session:
            order = session.scalar(select(BrokerOrder).where(BrokerOrder.client_order_id == old.client_order_id))
            if order is None:
                return False
            intent = session.get(TradeIntent, order.intent_id)
            opening = replace(old, intent_type="open", metadata=dict(order.payload or {}))
            event = TradeIntentEvent(event_id=UUID(intent.payload["event_id"]),
                source_service="schwab-1m-v2", payload=TradeIntentPayload(
                    strategy_code="schwab_1m_v2", broker_account_name=old.broker_account_name,
                    symbol=old.symbol, side="buy", intent_type="open", quantity=old.quantity,
                    reason=intent.reason, metadata=dict(opening.metadata)))
            events = await self._record_order_reports(session=session, intent=intent,
                strategy_id=order.strategy_id, broker_account_id=order.broker_account_id,
                intent_event=event, request=opening, reports=[report])
            if (report.event_type == "partially_filled" and
                    report.metadata.get("atr_reprice_terminal_cancel") == "true"):
                order.status = "cancelled"  # Fill stays booked; the unfilled remainder is dead.
            session.commit()
        for event in events:
            await self._publish_order_event(event)
        return True

    def _rpg_open_refusal(self, event, *, session=None):
        if event.payload.strategy_code != "schwab_1m_v2" or event.payload.intent_type != "open":
            return None
        token = event.payload.metadata.get("rpg_handoff_token")
        if token:
            job = self._rpg_journal().read(UUID(token), session=session)
            if (job["phase"] != "submitting"
                    or self.__dict__.get("_rpg_dispatch_token") != token
                    or not reprice_window_open(self._rpg_now())):
                return "rpg_unclaimed_or_expired_replacement"
            auth = job.get("authorization", {})
            if (auth.get("verdict") != "ready" or
                    not 0 <= self._rpg_now().timestamp() - auth.get("at", 0) <= 1.0):
                return "rpg_stale_strategy_authorization"
            current = auth.get("event", {}).get("payload", {})
            current_md = current.get("metadata", {})
            if (Decimal(str(current.get("quantity", 0))) != event.payload.quantity or any(
                    current_md.get(key) != event.payload.metadata.get(key) for key in
                    ("stop_price", "limit_price", "cw_flip_level", "cw_entry_slot", "fanout_segment_id", "fanout_slot_id"))):
                return "rpg_current_price_size_or_identity_changed"
            replacement = job.get("replacement", {})
            if (replacement.get("client_order_id") != self._build_client_order_id(event)
                    or job["old"]["broker_account_name"] != event.payload.broker_account_name
                    or job["old"]["symbol"] != event.payload.symbol
                    or str(job["segment_id"]) != event.payload.metadata.get("fanout_segment_id")
                    or job["slot"] != event.payload.metadata.get("cw_entry_slot")):
                return "rpg_replacement_identity_changed"
            if session is not None and job.get("original_order_id") and session.scalar(
                    select(Fill.id).where(Fill.order_id == UUID(job["original_order_id"])).limit(1)):
                return "rpg_original_filled_no_rebuy"
            return None
        for _, job in self._rpg_journal().jobs(session=session):
            old = job["old"]
            if (job["phase"] in ACTIVE_PHASES and old["symbol"] == event.payload.symbol
                    and old["broker_account_name"] == event.payload.broker_account_name):
                return "rpg_old_buy_still_owned"
        return None

    async def _run_rpg_retry_loop(self, stop_event):
        while not stop_event.is_set():
            try:
                for token, job in self._rpg_journal().jobs():
                    if job["phase"] in {"prepared", "waiting", "fills_waiting", "clear", "price_wait"}:
                        await self.redis.xadd(f"{self.settings.redis_stream_prefix}:strategy-intents",
                            {"data": json.dumps({"event_type": "atr_reprice_tick", "token": str(token)})},
                            maxlen=self.settings.redis_strategy_intent_stream_maxlen, approximate=True)
            except Exception:
                self.logger.exception("[OMS-RPG1] retry wakeup failed; durable ownership retained")
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=1.0)
            except TimeoutError:
                pass
