"""NFQ2: account-scoped EH price waits; only the serial intent lane submits."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from time import monotonic
from uuid import UUID, uuid4

from sqlalchemy import select, update

from project_mai_tai.db.models import BrokerAccount, BrokerOrder, DashboardSnapshot, Fill, TradeIntent
from project_mai_tai.events import TradeIntentEvent, stream_name
from project_mai_tai.fanout_outcome_consumer import append_outcome, broker_outcome
from project_mai_tai.fanout_segment_store import SNAPSHOT_TYPE as SEGMENT_SNAPSHOT, current_session_anchor
from project_mai_tai.oms.mirror_fresh_price import mirror_reading
from project_mai_tai.oms.atr_reprice_handoff import local_rpg_abort_proof
from project_mai_tai.strategy_core.entry_gate import within_entry_window


SNAPSHOT_TYPE = "oms_v2_eh_price_hold"
PREFIX = "v2_eh_nfq2:"


@dataclass
class EhHold:
    event: TradeIntentEvent
    row_id: UUID
    intent_id: UUID
    phase: str = "held"
    token: str = ""


class EhFreshPriceMixin:
    @property
    def _nfq2_holds(self):
        return self.__dict__.setdefault("_eh_price_holds", {})

    def _nfq2_enabled(self):
        return bool(getattr(self.settings, "oms_v2_eh_fresh_price_enabled", False))

    @staticmethod
    def _nfq2_key(event):
        return (event.payload.broker_account_name, event.payload.metadata.get("fanout_slot_id", ""))

    def _nfq2_applies(self, event):
        return (self._nfq2_enabled() or self._nfq2_held_dispatch(event)) and (
            self._v2_eh_reactive_entry_applies(event) or self._v2_eh_resting_entry_applies(event))

    def _nfq2_reading(self, symbol):
        # A last trade is not an executable ask. NFQ1's trade fallback is intentionally NOT used.
        return mirror_reading(self.__dict__.get("_latest_quotes_by_symbol", {}), {},
                              symbol, self._nfq_now(), 10000)

    def _nfq2_save(self, session, hold):
        row = session.get(DashboardSnapshot, hold.row_id)
        if row is None:
            row = DashboardSnapshot(id=hold.row_id, snapshot_type=SNAPSHOT_TYPE)
            session.add(row)
        row.payload = self._nfq2_payload(hold)
        session.flush()

    @staticmethod
    def _nfq2_payload(hold):
        return {"event": hold.event.model_dump(mode="json"), "intent_id": str(hold.intent_id),
                "phase": hold.phase, "token": hold.token}

    @staticmethod
    def _nfq2_copy(hold, *, phase=None, token=None):
        return EhHold(hold.event.model_copy(deep=True), hold.row_id, hold.intent_id,
                      hold.phase if phase is None else phase, hold.token if token is None else token)

    @staticmethod
    def _nfq2_same(current, expected):
        return bool(current is not None and current.row_id == expected.row_id
                    and current.intent_id == expected.intent_id
                    and current.event.event_id == expected.event.event_id
                    and current.phase == expected.phase and current.token == expected.token)

    def _nfq2_transition(self, session, before, after, *, reason=None, outcome="rejected_client_abort"):
        # Atomic durable CAS: an off-loop write must never resurrect a cancelled/replaced hold.
        result = session.execute(update(DashboardSnapshot).where(
            DashboardSnapshot.id == before.row_id,
            DashboardSnapshot.payload["phase"].as_string() == before.phase,
            DashboardSnapshot.payload["token"].as_string() == before.token,
            DashboardSnapshot.payload["intent_id"].as_string() == str(before.intent_id),
            DashboardSnapshot.payload["event"]["event_id"].as_string() == str(before.event.event_id),
        ).values(payload=self._nfq2_payload(after)), execution_options={"synchronize_session": False})
        if result.rowcount != 1:
            return False
        if reason:
            self._nfq2_retirement_feedback(session, after, reason, outcome=outcome)
        return True

    def _nfq2_feedback(self, session, event, outcome, reason):
        append_outcome(session, metadata={**event.payload.metadata, "nfq2_feedback": "true"},
                       symbol=event.payload.symbol, outcome=outcome, reason=PREFIX + reason,
                       evidence_id=f"nfq2:{event.event_id}:{outcome}:{reason}", event_source="client",
                       broker_account_name=event.payload.broker_account_name)
        reading = self._nfq2_reading(event.payload.symbol)
        self.logger.info("[OMS-NFQ2] symbol=%s account=%s slot=%s outcome=%s reason=%s "
                         "ask=%s cache_age_ms=%s source=%s timestamp=%s",
                         event.payload.symbol, event.payload.broker_account_name,
                         event.payload.metadata.get("fanout_slot_id"), outcome, reason,
                         reading.price, reading.age_ms, reading.source, reading.observed_at)

    def _nfq2_retire(self, session, hold, reason, *, outcome="rejected_client_abort"):
        hold.phase, hold.token = "retired", ""
        self._nfq2_save(session, hold)
        self._nfq2_retirement_feedback(session, hold, reason, outcome=outcome)
        self._nfq2_holds.pop(self._nfq2_key(hold.event), None)

    def _nfq2_retirement_feedback(self, session, hold, reason, *, outcome):
        intent = session.get(TradeIntent, hold.intent_id)
        if intent is not None and intent.status == "held" and outcome == "rejected_client_abort":
            self.store.mark_intent_refused(intent, origin="client_abort", code=PREFIX + reason)
        elif intent is not None and intent.status == "held":
            intent.status = "superseded"
        self._nfq2_feedback(session, hold.event, outcome, reason)

    def _nfq2_reason(self, session, hold):
        event = hold.event
        md = event.payload.metadata
        now = self._nfq_now()
        orders = session.scalars(select(BrokerOrder).join(BrokerAccount).where(
            BrokerAccount.name == event.payload.broker_account_name,
            BrokerOrder.symbol == event.payload.symbol, BrokerOrder.side == "buy",
            BrokerOrder.payload["fanout_slot_id"].as_string() == md.get("fanout_slot_id"),
        )).all()
        for order in orders:
            if str((order.payload or {}).get("fanout_segment_id")) != md.get("fanout_segment_id"):
                continue
            if session.scalar(select(Fill.id).where(Fill.order_id == order.id).limit(1)) is not None:
                return "own_slot_filled"
            if order.status in {"filled", "partially_filled"}:
                return "own_slot_filled"
            if order.status == "aborted" and local_rpg_abort_proof(session, order) is None:
                return "dispatch_uncertain"
            if order.status not in {"cancelled", "canceled", "rejected", "aborted", "expired"}:
                return "existing_buy"
        if hold.phase == "uncertain":
            return "dispatch_uncertain"
        if (not within_entry_window(now, self.settings)
                or event.produced_at.date() != now.date()):
            return "entry_window_ended"
        if not (self._v2_eh_reactive_entry_applies(event) or self._v2_eh_resting_entry_applies(event)):
            return "eh_path_closed"
        latest = session.scalar(select(DashboardSnapshot).where(
            DashboardSnapshot.snapshot_type == SEGMENT_SNAPSHOT,
            DashboardSnapshot.payload["symbol"].as_string() == event.payload.symbol,
        ).order_by(DashboardSnapshot.created_at.desc(), DashboardSnapshot.id.desc()).limit(1))
        proof = latest.payload if latest is not None else {}
        if (proof.get("strategy_code") != "schwab_1m_v2"
                or proof.get("session_anchor") != current_session_anchor(now).isoformat()
                or proof.get("active") is not True
                or str(proof.get("fanout_segment_id")) != md.get("fanout_segment_id")):
            return "segment_not_current"
        return None

    def _nfq2_observe(self, event):
        if not self._nfq2_holds or event.payload.strategy_code != "schwab_1m_v2":
            return
        with self.session_factory() as session:
            for hold in list(self._nfq2_holds.values()):
                old, new = hold.event.payload, event.payload
                if (old.symbol != new.symbol or old.broker_account_name != new.broker_account_name
                        or new.metadata.get("nfq2_retry_token")):
                    continue
                if new.intent_type not in {"open", "cancel"} or hold.phase == "uncertain":
                    continue
                if new.intent_type == "cancel":
                    segment = new.metadata.get("fanout_segment_id")
                    slot = new.metadata.get("fanout_slot_id")
                    if segment and segment != old.metadata.get("fanout_segment_id"):
                        continue
                    if slot and slot != old.metadata.get("fanout_slot_id"):
                        continue
                    generation = new.metadata.get("nfq2_generation")
                    if generation and generation != old.metadata.get("nfq2_generation"):
                        continue
                elif event.event_id == hold.event.event_id:
                    continue
                self._nfq2_retire(session, hold, "v2_cancel" if new.intent_type == "cancel" else "v2_replaced")
            session.commit()

    def _nfq2_pre_submit(self, session, event, intent):
        if not self._nfq2_applies(event):
            return False
        md = event.payload.metadata
        if not md.get("fanout_slot_id") or not md.get("fanout_segment_id"):
            # Legacy unbound entries cannot be retained safely; their existing pricer owns refusal.
            return False
        reading = self._nfq2_reading(event.payload.symbol)
        uncertain = next((h for h in self._nfq2_holds.values() if h.phase == "uncertain"
                          and h.event.payload.symbol == event.payload.symbol
                          and h.event.payload.broker_account_name == event.payload.broker_account_name), None)
        if uncertain is not None:
            self.store.mark_intent_refused(intent, origin="client_abort", code=PREFIX + "dispatch_uncertain")
            self._nfq2_feedback(session, uncertain.event, "could_not_tell", "dispatch_uncertain")
            return True
        hold = self._nfq2_holds.get(self._nfq2_key(event))
        if reading.fresh:
            return False
        key = self._nfq2_key(event)
        if hold is None:
            hold = EhHold(event.model_copy(deep=True), uuid4(), intent.id)
            self._nfq2_holds[key] = hold
        else:
            prior = session.get(TradeIntent, hold.intent_id)
            if prior is not None and prior.id != intent.id and prior.status == "held":
                prior.status = "superseded"
            hold.event = event.model_copy(deep=True)
            hold.intent_id = intent.id
            hold.phase, hold.token = "held", ""
        intent.status = "held"
        self._nfq2_save(session, hold)
        self._nfq2_feedback(session, event, "held_no_fresh_quote", "held")
        return True

    def _restore_nfq2_holds(self):
        with self.session_factory() as session:
            rows = session.scalars(select(DashboardSnapshot).where(
                DashboardSnapshot.snapshot_type == SNAPSHOT_TYPE,
                DashboardSnapshot.payload["phase"].as_string().in_(["held", "queued", "dispatching", "uncertain"]),
            )).all()
            for row in rows:
                p = row.payload
                hold = EhHold(TradeIntentEvent.model_validate(p["event"]), row.id, UUID(p["intent_id"]),
                              "uncertain" if p["phase"] in {"dispatching", "uncertain"} else "held")
                self._nfq2_holds[self._nfq2_key(hold.event)] = hold
                reason = self._nfq2_reason(session, hold)
                if reason == "dispatch_uncertain":
                    hold.phase = "uncertain"
                    self._nfq2_save(session, hold)
                elif reason:
                    outcome = "filled" if reason == "own_slot_filled" else "working" if reason == "existing_buy" else "rejected_client_abort"
                    self._nfq2_retire(session, hold, reason, outcome=outcome)
                else:
                    self._nfq2_save(session, hold)
                    self._nfq2_feedback(session, hold.event, "held_no_fresh_quote", "restart_" + hold.phase)
            session.commit()

    async def _evaluate_nfq2_holds(self, symbol=None):
        if not self._nfq2_holds:
            return
        if symbol is None:
            await self._sweep_nfq2_holds()
            return
        symbol = symbol.upper()
        # Reject unrelated/stale ticks entirely in memory, before copying anything or opening a session.
        matches = [(key, hold) for key, hold in self._nfq2_holds.items()
                   if hold.event.payload.symbol == symbol and hold.phase == "held"]
        if not matches:
            return
        reading = self._nfq2_reading(symbol)
        if not reading.fresh:
            return
        busy = self.__dict__.setdefault("_eh_price_hold_inflight", set())
        for key, hold in matches:
            if key in busy or self._nfq2_holds.get(key) is not hold or hold.phase != "held":
                continue
            reason = None
            try:
                trigger = Decimal(str(hold.event.payload.metadata.get("resting_level")
                                      or hold.event.payload.metadata["entry_price"]))
                if not trigger.is_finite() or trigger <= 0:
                    raise ValueError("invalid trigger")
                if reading.price > trigger * Decimal("1.01"):
                    reason = "ask_past_held_cap"
            except (KeyError, ValueError, InvalidOperation):
                reason = "invalid_trigger"
            before = self._nfq2_copy(hold)
            after = self._nfq2_copy(hold, phase="retired" if reason else "queued",
                                    token="" if reason else str(uuid4()))
            busy.add(key)
            try:
                changed = await self._run_db(lambda session: self._nfq2_transition(
                    session, before, after, reason=reason))
                current = self._nfq2_holds.get(key)
                if not self._nfq2_same(current, before):
                    continue
                if not changed:
                    current.phase, current.token = "uncertain", ""
                    self.logger.warning("[OMS-NFQ2] durable_cas_mismatch account=%s slot=%s", *key)
                    continue
                current.phase, current.token = after.phase, after.token
                if reason:
                    self._nfq2_holds.pop(key, None)
                    continue
                retry = TradeIntentEvent(source_service="oms-risk", produced_at=self._nfq_now(),
                                         payload=current.event.payload.model_copy(deep=True))
                retry.payload.metadata.update(nfq2_retry_token=current.token, nfq2_hold_id=str(current.row_id))
                try:
                    await self.redis.xadd(stream_name(self.settings.redis_stream_prefix, "strategy-intents"),
                                         {"data": retry.model_dump_json()},
                                         maxlen=self.settings.redis_strategy_intent_stream_maxlen, approximate=True)
                except Exception:
                    uncertain = self._nfq2_copy(after, phase="uncertain", token="")
                    await self._run_db(lambda session: self._nfq2_transition(session, after, uncertain))
                    if self._nfq2_same(self._nfq2_holds.get(key), after):
                        current.phase, current.token = "uncertain", ""
                    self.logger.exception("[OMS-NFQ2] enqueue_uncertain; token invalidated")
            except Exception:
                self.logger.exception("[OMS-NFQ2] transaction_failed; no new enqueue")
            finally:
                busy.discard(key)

    async def _sweep_nfq2_holds(self):
        now = monotonic()
        cadence = max(1.0, float(self.settings.oms_broker_sync_interval_seconds))
        if now - self.__dict__.get("_eh_price_hold_last_sweep", float("-inf")) < cadence:
            return
        self.__dict__["_eh_price_hold_last_sweep"] = now
        busy = self.__dict__.setdefault("_eh_price_hold_inflight", set())
        snapshots = [(key, self._nfq2_copy(hold)) for key, hold in self._nfq2_holds.items() if key not in busy]
        if not snapshots:
            return
        try:
            verdicts = await self._run_db(lambda session: [
                (key, before, self._nfq2_reason(session, before)) for key, before in snapshots], commit=False)
            for key, before, reason in verdicts:
                if (not reason or key in busy or not self._nfq2_same(self._nfq2_holds.get(key), before)
                        or reason == "dispatch_uncertain" and before.phase == "uncertain"):
                    continue
                after = self._nfq2_copy(before, phase="uncertain" if reason == "dispatch_uncertain" else "retired",
                                        token="")
                outcome = "filled" if reason == "own_slot_filled" else "working" if reason == "existing_buy" else "rejected_client_abort"
                busy.add(key)
                try:
                    changed = await self._run_db(lambda session: self._nfq2_transition(
                        session, before, after, reason=None if after.phase == "uncertain" else reason,
                        outcome=outcome))
                    current = self._nfq2_holds.get(key)
                    if changed and self._nfq2_same(current, before):
                        current.phase, current.token = after.phase, after.token
                        if after.phase == "retired":
                            self._nfq2_holds.pop(key, None)
                finally:
                    busy.discard(key)
        except Exception:
            self.logger.exception("[OMS-NFQ2] periodic_proof_failed; holds remain blocking")

    def _claim_nfq2_retry(self, event):
        md = event.payload.metadata
        token = md.get("nfq2_retry_token")
        if not token:
            return True
        hold = self._nfq2_holds.get(self._nfq2_key(event))
        if (hold is None or hold.phase != "queued" or hold.token != token
                or str(hold.row_id) != md.get("nfq2_hold_id")
                or hold.event.payload.metadata.get("fanout_segment_id") != md.get("fanout_segment_id")):
            return False
        with self.session_factory() as session:
            reason = self._nfq2_reason(session, hold)
            if reason:
                if reason == "dispatch_uncertain":
                    hold.phase, hold.token = "uncertain", ""
                    self._nfq2_save(session, hold)
                else:
                    outcome = "filled" if reason == "own_slot_filled" else "working" if reason == "existing_buy" else "rejected_client_abort"
                    self._nfq2_retire(session, hold, reason, outcome=outcome)
                session.commit()
                return False
            hold.phase = "dispatching"
            predecessor = hold.event.payload.metadata.get("fanout_attempt_id", "")
            event.payload.metadata["fanout_predecessor_attempt_id"] = predecessor
            event.payload.metadata["fanout_attempt_id"] = self._build_client_order_id(event)
            hold.event.payload.metadata["fanout_attempt_id"] = event.payload.metadata["fanout_attempt_id"]
            self._nfq2_save(session, hold)
            self._nfq2_feedback(session, event, "queued", "retry_claimed")
            session.commit()
        # Held pricing is trusted only after the serial CAS, never from an incoming metadata claim.
        return True

    def _nfq2_held_dispatch(self, event):
        hold = self._nfq2_holds.get(self._nfq2_key(event))
        return bool(hold is not None and hold.phase == "dispatching"
                    and hold.token == event.payload.metadata.get("nfq2_retry_token")
                    and str(hold.row_id) == event.payload.metadata.get("nfq2_hold_id"))

    def _finish_nfq2_retry(self, event, *, completed):
        if not event.payload.metadata.get("nfq2_retry_token"):
            return
        hold = self._nfq2_holds.get(self._nfq2_key(event))
        if (hold is None or hold.phase != "dispatching"
                or hold.token != event.payload.metadata.get("nfq2_retry_token")):
            return
        with self.session_factory() as session:
            if completed:
                coid = self._build_client_order_id(event)
                order = session.scalar(select(BrokerOrder).where(BrokerOrder.client_order_id == coid))
                origin = "client" if order is not None and local_rpg_abort_proof(session, order) else "broker"
                outcome = broker_outcome(order.status, origin) if order is not None else "rejected_client_abort"
                self._nfq2_retire(session, hold, "pipeline_completed", outcome=outcome)
            else:
                hold.phase, hold.token = "uncertain", ""
                self._nfq2_save(session, hold)
            session.commit()
