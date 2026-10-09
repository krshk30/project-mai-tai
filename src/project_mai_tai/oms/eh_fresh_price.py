"""NFQ2: account-scoped EH price waits; only the serial intent lane submits."""
from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from time import monotonic
from uuid import UUID, uuid4

from sqlalchemy import and_, or_, select, update

from project_mai_tai.broker_adapters.atr_buy_readback import AtrBuyReadback
from project_mai_tai.broker_adapters.protocols import OrderRequest
from project_mai_tai.db.models import BrokerAccount, BrokerOrder, BrokerOrderEvent, DashboardSnapshot, Fill, Strategy, TradeIntent
from project_mai_tai.events import TradeIntentEvent, stream_name
from project_mai_tai.fanout_identity import fanout_slot_id
from project_mai_tai.fanout_outcome_consumer import append_outcome, broker_outcome
from project_mai_tai.fanout_segment_store import SNAPSHOT_TYPE as SEGMENT_SNAPSHOT, current_session_anchor
from project_mai_tai.oms.mirror_fresh_price import mirror_reading
from project_mai_tai.oms.atr_reprice_handoff import local_rpg_abort_proof, replacement_has_fills, replacement_unknown_fill_report
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
    dispatch: dict | None = None


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

    @staticmethod
    def _nfq2_bound_identity(event):
        md = event.payload.metadata
        try:
            return md.get("fanout_slot_id") == fanout_slot_id(strategy_code=event.payload.strategy_code,
                symbol=event.payload.symbol, segment_id=md.get("fanout_segment_id"), slot=md.get("fanout_slot", ""))
        except (TypeError, ValueError):
            return False

    def _nfq2_segment_current(self, session, event):
        latest = session.scalar(select(DashboardSnapshot).where(
            DashboardSnapshot.snapshot_type == SEGMENT_SNAPSHOT,
            DashboardSnapshot.payload["symbol"].as_string() == event.payload.symbol,
        ).order_by(DashboardSnapshot.created_at.desc(), DashboardSnapshot.id.desc()).limit(1))
        proof = latest.payload if latest is not None and isinstance(latest.payload, dict) else {}
        return (proof.get("strategy_code") == "schwab_1m_v2"
                and proof.get("session_anchor") == current_session_anchor(self._nfq_now()).isoformat()
                and proof.get("active") is True
                and str(proof.get("fanout_segment_id")) == event.payload.metadata.get("fanout_segment_id"))

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
                "phase": hold.phase, "token": hold.token, "dispatch": deepcopy(hold.dispatch)}

    @staticmethod
    def _nfq2_copy(hold, *, phase=None, token=None):
        return EhHold(hold.event.model_copy(deep=True), hold.row_id, hold.intent_id,
                      hold.phase if phase is None else phase, hold.token if token is None else token,
                      deepcopy(hold.dispatch))

    @staticmethod
    def _nfq2_same(current, expected):
        return bool(current is not None and current.row_id == expected.row_id
                    and current.intent_id == expected.intent_id
                    and current.event.event_id == expected.event.event_id
                    and current.phase == expected.phase and current.token == expected.token
                    and current.dispatch == expected.dispatch)

    def _nfq2_transition(self, session, before, after, *, reason=None, outcome="rejected_client_abort"):
        # Atomic durable CAS: an off-loop write must never resurrect a cancelled/replaced hold.
        result = session.execute(update(DashboardSnapshot).where(
            DashboardSnapshot.id == before.row_id,
            DashboardSnapshot.payload["phase"].as_string() == before.phase,
            DashboardSnapshot.payload["token"].as_string() == before.token,
            DashboardSnapshot.payload["intent_id"].as_string() == str(before.intent_id),
            DashboardSnapshot.payload["event"]["event_id"].as_string() == str(before.event.event_id),
            DashboardSnapshot.payload["dispatch"]["generation"].as_string() ==
                (before.dispatch or {}).get("generation"),
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
        orders = session.scalars(select(BrokerOrder).join(BrokerAccount).join(Strategy).where(
            BrokerAccount.name == event.payload.broker_account_name,
            Strategy.code == "schwab_1m_v2",
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
                return "dispatch_uncertain" if hold.phase == "uncertain" else "existing_buy"
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
        if not self._nfq2_bound_identity(event):
            return
        with self.session_factory() as session:
            if event.payload.intent_type == "open" and not self._nfq2_segment_current(session, event):
                return
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
                    generation = new.metadata.get("nfq2_generation")
                    if generation and generation != old.metadata.get("nfq2_generation"):
                        continue
                    if self._nfq2_segment_cancel_barrier(event):
                        self._nfq2_retire_local_segment_hold(session, hold)
                        continue
                    if slot and slot != old.metadata.get("fanout_slot_id"):
                        continue
                elif event.event_id == hold.event.event_id:
                    continue
                self._nfq2_retire(session, hold, "v2_cancel" if new.intent_type == "cancel" else "v2_replaced")
            session.commit()

    def _nfq2_segment_cancel_barrier(self, event):
        md = event.payload.metadata
        try:
            UUID(md.get("clearwait_removal_token", ""))
        except (TypeError, ValueError, AttributeError):
            return False
        return (event.payload.intent_type == "cancel" and event.payload.side == "buy"
                and md.get("clearwait_buy_only") == "true" and md.get("fanout_slot") == "resting"
                and md.get("clearwait_opportunity_id") == md.get("fanout_segment_id")
                and self._nfq2_bound_identity(event))

    def _nfq2_retire_local_segment_hold(self, session, hold):
        """A typed opportunity barrier can revoke local waits, never wire/uncertainty."""
        if hold.phase not in {"held", "queued"} or hold.dispatch is not None or not self._nfq2_bound_identity(hold.event):
            return False
        row = session.scalar(select(DashboardSnapshot).where(DashboardSnapshot.id == hold.row_id).with_for_update())
        if row is None or row.snapshot_type != SNAPSHOT_TYPE:
            return False
        try:
            p = row.payload
            before = EhHold(TradeIntentEvent.model_validate(p["event"]), row.id, UUID(p["intent_id"]),
                            p["phase"], p["token"], deepcopy(p.get("dispatch")))
        except (KeyError, TypeError, ValueError):
            return False
        if (before.phase not in {"held", "queued"} or before.dispatch is not None
                or before.intent_id != hold.intent_id
                or before.event.model_dump(mode="json") != hold.event.model_dump(mode="json")):
            return False
        intent = session.get(TradeIntent, before.intent_id)
        if intent is None or intent.status != "held" or not self._nfq2_intent_matches(session, intent, before.event):
            return False
        md = before.event.payload.metadata
        related_order = session.scalar(select(BrokerOrder.id).where(or_(
            BrokerOrder.intent_id == intent.id,
            BrokerOrder.client_order_id == md.get("fanout_attempt_id", ""),
            BrokerOrder.payload["nfq2_hold_id"].as_string() == str(before.row_id),
            and_(BrokerOrder.strategy_id == intent.strategy_id, BrokerOrder.broker_account_id == intent.broker_account_id,
                 BrokerOrder.symbol == intent.symbol, BrokerOrder.side == "buy",
                 BrokerOrder.payload["fanout_slot_id"].as_string() == md["fanout_slot_id"],
                 BrokerOrder.payload["fanout_segment_id"].as_string() == md["fanout_segment_id"]),
        )).limit(1))
        if related_order is not None:
            return False
        after = self._nfq2_copy(before, phase="retired", token="")
        if not self._nfq2_transition(session, before, after, reason="v2_cancel_segment_barrier"):
            return False
        hold.phase, hold.token = after.phase, after.token
        self._nfq2_holds.pop(self._nfq2_key(hold.event), None)
        return True

    @staticmethod
    def _nfq2_intent_matches(session, intent, event):
        account = session.get(BrokerAccount, intent.broker_account_id)
        strategy = session.get(Strategy, intent.strategy_id)
        return bool(account and strategy and account.name == event.payload.broker_account_name
                    and strategy.code == event.payload.strategy_code == "schwab_1m_v2"
                    and intent.symbol == event.payload.symbol and intent.side == event.payload.side == "buy"
                    and intent.intent_type == event.payload.intent_type == "open"
                    and intent.quantity == event.payload.quantity
                    and (intent.payload or {}).get("event_id") == str(event.event_id))

    def _nfq2_refuse_pre_wire(self, session, event, intent, reason):
        owned = session.scalar(select(BrokerOrder.id).where(BrokerOrder.intent_id == intent.id).limit(1))
        if (intent.status in {"pending", "created"} and owned is None
                and self._nfq2_intent_matches(session, intent, event)):
            self.store.mark_intent_refused(intent, origin="client_abort", code=PREFIX + reason)
        self.logger.warning("[OMS-NFQ2] %s symbol=%s account=%s intent=%s owned=%s", reason,
                            event.payload.symbol, event.payload.broker_account_name, intent.id, owned is not None)

    def _nfq2_pre_submit(self, session, event, intent):
        uncertain = next((h for h in self._nfq2_holds.values() if h.phase == "uncertain"
                          and event.payload.strategy_code == "schwab_1m_v2"
                          and event.payload.intent_type == "open" and event.payload.side == "buy"
                          and h.event.payload.symbol == event.payload.symbol
                          and h.event.payload.broker_account_name == event.payload.broker_account_name), None)
        if uncertain is not None:
            self._nfq2_refuse_pre_wire(session, event, intent, "dispatch_uncertain")
            self._nfq2_feedback(session, uncertain.event, "could_not_tell", "dispatch_uncertain")
            return True
        if not self._nfq2_applies(event):
            return False
        if not self._nfq2_bound_identity(event) or not self._nfq2_segment_current(session, event):
            # Drain only this exact pre-wire intent; never infer identity or retire another owner.
            self._nfq2_refuse_pre_wire(session, event, intent, "legacy_identity_unproven")
            return True
        reading = self._nfq2_reading(event.payload.symbol)
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
            hold.dispatch = None
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
                              "uncertain" if p["phase"] in {"dispatching", "uncertain"} else "held",
                              dispatch=deepcopy(p.get("dispatch")))
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
        busy = self.__dict__.setdefault("_eh_price_hold_inflight", set())
        matches = [(key, hold) for key, hold in self._nfq2_holds.items()
                   if key not in busy and hold.event.payload.symbol == symbol and hold.phase == "held"]
        if not matches:
            return
        reading = self._nfq2_reading(symbol)
        if not reading.fresh:
            return
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
                if (reason == "dispatch_uncertain" and before.phase == "uncertain"
                        and key not in busy and self._nfq2_same(self._nfq2_holds.get(key), before)):
                    await self._recover_nfq2_uncertain(key, before)
                    continue
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

    def _nfq2_recovery_order(self, session, hold, *, lock=False, allow_fills=False):
        """Bind a read to exact durable intent/attempt evidence, never an order-age/status label."""
        if hold.dispatch is not None and not isinstance(hold.dispatch, dict):
            return None
        dispatch = hold.dispatch or {}
        if dispatch.get("fill_seen"):
            return None
        md, payload = hold.event.payload.metadata, hold.event.payload
        client = dispatch.get("client_order_id") or md.get("fanout_attempt_id")
        if not client:
            return None
        statement = select(BrokerOrder).where(BrokerOrder.client_order_id == client)
        order = session.scalar(statement.with_for_update() if lock else statement)
        if order is None:
            return None
        intent = session.get(TradeIntent, order.intent_id) if order.intent_id else None
        account, strategy = session.get(BrokerAccount, order.broker_account_id), session.get(Strategy, order.strategy_id)
        if intent is None or account is None or strategy is None:
            return None
        imd, omd = (intent.payload or {}).get("metadata", {}), order.payload or {}
        if not isinstance(imd, dict) or not isinstance(omd, dict):
            return None
        event_id, generation = (intent.payload or {}).get("event_id"), imd.get("nfq2_retry_token")
        try:
            dispatched = hold.event.model_copy(deep=True)
            dispatched.event_id = UUID(event_id)
        except (TypeError, ValueError, AttributeError):
            return None
        identity = {"event_id": event_id, "client_order_id": client,
                    "generation": generation, "hold_id": str(hold.row_id)}
        if (not generation or imd.get("nfq2_hold_id") != str(hold.row_id)
                or self._build_client_order_id(dispatched) != client
                or dispatch and any(dispatch.get(key) != value for key, value in identity.items())
                or strategy.code != payload.strategy_code or strategy.code != "schwab_1m_v2"
                or account.name != payload.broker_account_name or order.symbol != payload.symbol
                or order.side != payload.side or order.side != "buy" or order.quantity != payload.quantity
                or not order.quantity.is_finite() or order.quantity <= 0
                or intent.strategy_id != order.strategy_id or intent.broker_account_id != order.broker_account_id
                or intent.symbol != order.symbol or intent.side != "buy" or intent.intent_type != "open"
                or intent.quantity != order.quantity or omd.get("fanout_attempt_id") != client
                or omd.get("nfq2_retry_token") != generation or omd.get("nfq2_hold_id") != str(hold.row_id)
                or any(not md.get(key) or imd.get(key) != md[key] or omd.get(key) != md[key]
                       for key in ("fanout_slot_id", "fanout_segment_id"))
                or imd.get("nfq2_generation", "") != md.get("nfq2_generation", "")
                or omd.get("nfq2_generation", "") != md.get("nfq2_generation", "")
                or (not allow_fills and replacement_has_fills(session, order))
                or replacement_unknown_fill_report(session, order)):
            return None
        local = local_rpg_abort_proof(session, order) is not None if order.status == "aborted" else False
        if not local and not order.broker_order_id and account.provider != "webull":
            return None
        return {"order_id": str(order.id), "intent_id": str(intent.id), "dispatch": identity,
                "client_order_id": client, "broker_order_id": order.broker_order_id or "",
                "broker_account_name": account.name, "strategy_code": strategy.code,
                "symbol": order.symbol, "quantity": str(order.quantity), "local_no_wire": local,
                "order_type": order.order_type, "time_in_force": order.time_in_force,
                "metadata": {key: md.get(key, "") for key in
                             ("fanout_slot_id", "fanout_segment_id", "nfq2_generation")}}

    def _nfq2_commit_recovery(self, session, before, identity, readback):
        # Re-read after network yield, lock the exact parent, and CAS the same hold generation.
        current = self._nfq2_recovery_order(session, before, lock=True)
        if current != identity or self._nfq2_reason(session, before) != "dispatch_uncertain":
            return None
        after = self._nfq2_copy(before)
        after.dispatch = deepcopy(identity["dispatch"])
        if readback is not None and readback.outcome == "fills":
            filled = readback.cumulative_filled
            if filled is None or not filled.is_finite() or not 0 < filled <= before.event.payload.quantity:
                return None
            after.dispatch["fill_seen"] = str(filled)
            return after if self._nfq2_transition(session, before, after) else None
        empty = bool(readback is not None and isinstance(readback.cumulative_filled, Decimal)
                     and readback.cumulative_filled.is_finite() and readback.cumulative_filled == Decimal(0)
                     and (identity["broker_order_id"] or readback.broker_order_id)
                     and (readback.can_replace and readback.broker_status in {"CANCELED", "CANCELLED"}
                          or readback.outcome == "rejected_empty"
                          and readback.broker_status == "REJECTED"))
        if not identity["local_no_wire"] and not empty:
            return None
        after.phase, after.token = "retired", ""
        after.dispatch["recovery"] = {"source": "client_audit" if identity["local_no_wire"] else "broker_readback",
            "order_id": identity["order_id"],
            "broker_order_id": identity["broker_order_id"] or (readback.broker_order_id if readback else ""),
            "status": "aborted" if identity["local_no_wire"] else readback.broker_status,
            "filled_quantity": "0", "observed_at": self._nfq_now().isoformat()}
        if not self._nfq2_transition(session, before, after, reason="dispatch_proven_clear"):
            return None
        if not identity["local_no_wire"]:
            order = session.get(BrokerOrder, UUID(identity["order_id"]))
            order.status = "rejected" if readback.outcome == "rejected_empty" else "cancelled"
            order.broker_order_id = identity["broker_order_id"] or readback.broker_order_id
            dispatched_intent = session.get(TradeIntent, order.intent_id)
            dispatched_intent.status = order.status
            session.add(BrokerOrderEvent(order_id=order.id, event_type=order.status, event_source="broker",
                event_at=self._nfq_now(), payload={"client_order_id": order.client_order_id,
                    "broker_order_id": order.broker_order_id, "filled_quantity": "0",
                    "metadata": {**identity["metadata"], "nfq2_recovery_generation": identity["dispatch"]["generation"],
                                 "nfq2_recovery_hold_id": str(before.row_id)},
                    "reason": "nfq2_exact_terminal_zero_readback"}))
        return after

    async def _recover_nfq2_uncertain(self, key, before):
        busy = self.__dict__.setdefault("_eh_price_hold_inflight", set())
        if key in busy or not self._nfq2_same(self._nfq2_holds.get(key), before):
            return
        busy.add(key)
        try:
            identity = await self._run_db(lambda session: self._nfq2_recovery_order(session, before), commit=False)
            if identity is None:
                return
            readback = None
            if not identity["local_no_wire"]:
                reader = getattr(self.broker_adapter, "read_atr_resting_buy_after_cancel", None)
                if reader is None:
                    return
                request = OrderRequest(client_order_id=identity["client_order_id"],
                    broker_account_name=identity["broker_account_name"], strategy_code=identity["strategy_code"],
                    symbol=identity["symbol"], side="buy", intent_type="cancel", quantity=Decimal(identity["quantity"]),
                    reason="nfq2_read_only_recovery", order_type=identity["order_type"],
                    time_in_force=identity["time_in_force"], metadata={**identity["metadata"],
                        "broker_order_id": identity["broker_order_id"], "resting_entry_cancel": "true",
                        "atr_reprice_identity": "webull_client_order_id", "nfq2_recovery_read_only": "true"})
                readback = await asyncio.wait_for(reader(request), timeout=2.0)
                if (not isinstance(readback, AtrBuyReadback)
                        or readback.broker_order_id and identity["broker_order_id"]
                        and readback.broker_order_id != identity["broker_order_id"]):
                    return
            if not self._nfq2_same(self._nfq2_holds.get(key), before):
                return
            after = await self._run_db(lambda session: self._nfq2_commit_recovery(session, before, identity, readback))
            current = self._nfq2_holds.get(key)
            if after is not None and self._nfq2_same(current, before):
                current.phase, current.token, current.dispatch = after.phase, after.token, after.dispatch
                if after.phase == "retired":
                    self._nfq2_holds.pop(key, None)
        except Exception:
            self.logger.exception("[OMS-NFQ2] recovery_unproven; exact owner remains blocking")
        finally:
            busy.discard(key)

    def _claim_nfq2_retry(self, event):
        md = event.payload.metadata
        token = md.get("nfq2_retry_token")
        if not token:
            return True
        hold = self._nfq2_holds.get(self._nfq2_key(event))
        if (hold is None or hold.phase != "queued" or hold.token != token
                or str(hold.row_id) != md.get("nfq2_hold_id")
                or hold.event.payload.metadata.get("fanout_segment_id") != md.get("fanout_segment_id")
                or any(getattr(event.payload, key) != getattr(hold.event.payload, key) for key in
                       ("strategy_code", "broker_account_name", "symbol", "side", "intent_type", "quantity"))
                or md.get("nfq2_generation", "") != hold.event.payload.metadata.get("nfq2_generation", "")):
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
            before = self._nfq2_copy(hold)
            after = self._nfq2_copy(hold, phase="dispatching")
            predecessor = hold.event.payload.metadata.get("fanout_attempt_id", "")
            event.payload.metadata["fanout_predecessor_attempt_id"] = predecessor
            event.payload.metadata["fanout_attempt_id"] = self._build_client_order_id(event)
            after.event.payload.metadata["fanout_attempt_id"] = event.payload.metadata["fanout_attempt_id"]
            after.dispatch = {"event_id": str(event.event_id), "client_order_id": self._build_client_order_id(event),
                              "generation": token, "hold_id": str(hold.row_id)}
            if not self._nfq2_transition(session, before, after):
                return False
            self._nfq2_feedback(session, event, "queued", "retry_claimed")
            session.commit()
            hold.phase, hold.event, hold.dispatch = after.phase, after.event, after.dispatch
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
                refusals = session.scalars(select(TradeIntent).where(
                    TradeIntent.payload["event_id"].as_string() == str(event.event_id))).all()
                prewire = [row for row in refusals if row.status == "rejected"
                    and (row.payload or {}).get("refusal_origin") == "client_abort"
                    and (row.payload or {}).get("refusal_code") == PREFIX + "legacy_identity_unproven"
                    and (row.payload or {}).get("metadata", {}).get("nfq2_retry_token") == hold.token
                    and (row.payload or {}).get("metadata", {}).get("nfq2_hold_id") == str(hold.row_id)
                    and self._nfq2_intent_matches(session, row, event)
                    and all((row.payload or {}).get("metadata", {}).get(key) == event.payload.metadata.get(key)
                            for key in ("fanout_slot_id", "fanout_segment_id", "nfq2_generation"))
                    and session.scalar(select(BrokerOrder.id).where(BrokerOrder.intent_id == row.id).limit(1)) is None]
                if order is None and len(prewire) == 1:
                    self._nfq2_finish_transition(session, hold, "retired", reason="legacy_identity_unproven")
                    session.commit()
                    return
                identity = self._nfq2_recovery_order(session, hold, allow_fills=True)
                wire = session.scalar(select(BrokerOrderEvent.id).where(
                    BrokerOrderEvent.order_id == order.id, BrokerOrderEvent.event_source == "broker",
                    BrokerOrderEvent.event_type.in_({"accepted", "filled", "partially_filled"}),
                    BrokerOrderEvent.payload["client_order_id"].as_string() == coid,
                    BrokerOrderEvent.payload["broker_order_id"].as_string() == order.broker_order_id,
                ).limit(1)) if order is not None and order.broker_order_id else None
                if identity is not None and (identity["local_no_wire"] or wire is not None
                                              or replacement_has_fills(session, order)):
                    origin = "client" if identity["local_no_wire"] else "broker"
                    outcome = broker_outcome(order.status, origin)
                    if outcome in {"filled", "partially_filled", "submitted", "working", "rejected_client_abort"}:
                        self._nfq2_finish_transition(session, hold, "retired", reason="pipeline_completed", outcome=outcome)
                        session.commit()
                        return
            if hold.phase == "dispatching":
                self._nfq2_finish_transition(session, hold, "uncertain")
            session.commit()

    def _nfq2_finish_transition(self, session, hold, phase, *, reason=None, outcome="rejected_client_abort"):
        after = self._nfq2_copy(hold, phase=phase, token="")
        if not self._nfq2_transition(session, hold, after, reason=reason, outcome=outcome):
            # A failed durable CAS is not permission to overwrite another generation.
            hold.phase, hold.token = "uncertain", ""
            return
        hold.phase, hold.token = after.phase, after.token
        if phase == "retired":
            self._nfq2_holds.pop(self._nfq2_key(hold.event), None)
