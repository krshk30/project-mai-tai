"""NFQ1: durable, price-gated Webull mirror work, owned by the serial intent lane."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from sqlalchemy import select

from project_mai_tai.db.models import BrokerAccount, BrokerOrder, DashboardSnapshot, Fill
from project_mai_tai.events import TradeIntentEvent, stream_name
from project_mai_tai.fanout_outcome_consumer import (
    NFQ_GAVE_UP_PREFIX, NFQ_RETIRED_PREFIX, TERMINAL_RELEASE_OUTCOMES, append_outcome, broker_outcome,
)
from project_mai_tai.fanout_segment_store import (
    SNAPSHOT_TYPE as SEGMENT_SNAPSHOT, current_session_anchor,
)
from project_mai_tai.oms.atr_reprice_handoff import local_rpg_abort_proof
from project_mai_tai.strategy_core.entry_gate import resolve_entry_window, within_entry_window


SNAPSHOT_TYPE = "oms_webull_mirror_price_hold"
HELD_REASON = "webull_mirror_no_fresh_quote_held"
GAVE_UP_PREFIX = NFQ_GAVE_UP_PREFIX
RETIRED_PREFIX = NFQ_RETIRED_PREFIX
EASTERN = ZoneInfo("America/New_York")


@dataclass(frozen=True)
class MirrorReading:
    price: Decimal | None
    source: str
    observed_at: datetime | None
    age_ms: float | None
    fresh: bool


def mirror_reading(quotes, trades, symbol: str, now: datetime, max_age_ms: int) -> MirrorReading:
    """Prefer a usable ask to a last trade; retain stale evidence for decision logging."""
    readings = []
    for source, book, field in (("ask", quotes, "ask"), ("last", trades, "price")):
        row = book.get(symbol.upper(), {})
        observed = row.get("received_at")
        try:
            price = Decimal(str(row.get(field, "")))
        except (InvalidOperation, ValueError, TypeError):
            continue
        if not price.is_finite() or price <= 0:
            continue
        age = ((now - observed).total_seconds() * 1000
               if isinstance(observed, datetime) and observed.tzinfo is not None else None)
        reading = MirrorReading(price, source, observed, age,
                                age is not None and 0 <= age <= max(0, max_age_ms))
        if reading.fresh:
            return reading
        readings.append(reading)
    return readings[0] if readings else MirrorReading(None, "none", None, None, False)


@dataclass
class PriceHold:
    event: TradeIntentEvent
    row_id: UUID
    phase: str = "held"
    token: str = ""


class MirrorFreshPriceMixin:
    """Service hooks share the caller's transaction for holds, reports, and outcomes."""

    @property
    def _nfq_holds(self) -> dict[str, PriceHold]:
        return self.__dict__.setdefault("_nfq_price_holds", {})

    def _nfq_enabled(self) -> bool:
        return bool(getattr(self.settings, "oms_v2_webull_mirror_fresh_price_enabled", True))

    def _mirror_max_age_ms(self) -> int:
        return int(getattr(self.settings, "oms_v2_webull_mirror_quote_max_age_ms", 10000))

    def _nfq_now(self) -> datetime:
        from project_mai_tai.oms.service import utcnow
        return utcnow()

    def _mirror_reading(self, symbol: str) -> MirrorReading:
        return mirror_reading(
            self.__dict__.get("_latest_quotes_by_symbol", {}),
            self.__dict__.get("_latest_trades_by_symbol", {}),
            symbol, self._nfq_now(), self._mirror_max_age_ms(),
        )

    def _nfq_window_open(self, event: TradeIntentEvent) -> bool:
        from project_mai_tai.oms.service import _is_regular_market_session
        now = self._nfq_now().astimezone(EASTERN)
        return (within_entry_window(now, self.settings) and _is_regular_market_session(now)
                and event.produced_at.astimezone(EASTERN).date() == now.date())

    def _nfq_entry_deadline(self) -> datetime:
        _, _, end_hour, end_minute = resolve_entry_window(self.settings)
        now = self._nfq_now().astimezone(EASTERN)
        # A NORMAL-session mirror cannot outlive RTH even if the configured v2
        # entry window extends into EH. v2's resting-session split has the same bound.
        return min(now.replace(hour=end_hour, minute=end_minute, second=0, microsecond=0),
                   now.replace(hour=16, minute=0, second=0, microsecond=0))

    def _nfq_log_release(self, event, reason: str, *, report=None) -> None:
        decision = "gave_up" if reason.startswith(GAVE_UP_PREFIX) else "retired"
        reason = reason.removeprefix(GAVE_UP_PREFIX).removeprefix(RETIRED_PREFIX)
        self._nfq_log(event, decision, reason, report=report)

    def _nfq_log(self, event, decision: str, reason: str, *, report=None) -> None:
        reading = self._mirror_reading(event.payload.symbol)
        md = event.payload.metadata
        wire = report.metadata if report is not None else {}
        self.logger.info(
            "[OMS-NFQ1] sym=%s segment=%s slot_id=%s attempt=%s decision=%s reason=%s "
            "cache_age_ms=%s cache_source=%s cache_timestamp=%s "
            "cache_clock=producer_timestamp oms_consumption_age=unknown "
            "wire_age_ms=%s wire_source=%s",
            event.payload.symbol, md.get("fanout_segment_id", ""),
            md.get("fanout_slot_id", ""), md.get("fanout_attempt_id", ""), decision, reason,
            "unknown" if reading.age_ms is None else f"{reading.age_ms:.3f}", reading.source,
            reading.observed_at.isoformat() if reading.observed_at else "missing",
            wire.get("webull_shape_market_age_ms_at_wire", "unknown"),
            wire.get("webull_shape_market_source", md.get("webull_shape_market_source", "none")),
        )

    def _nfq_save(self, session, hold: PriceHold) -> None:
        row = session.get(DashboardSnapshot, hold.row_id)
        if row is None:
            row = DashboardSnapshot(id=hold.row_id, snapshot_type=SNAPSHOT_TYPE)
            session.add(row)
        row.payload = {"event": hold.event.model_dump(mode="json"),
                       "phase": hold.phase, "token": hold.token}
        session.flush()

    def _nfq_outcome(self, session, event, outcome: str, reason: str) -> str:
        row = append_outcome(
            session, metadata=event.payload.metadata, symbol=event.payload.symbol,
            outcome=outcome, reason=reason, event_source="client",
            evidence_id=f"nfq1:{event.event_id}:{outcome}:{reason}",
            broker_account_name=event.payload.broker_account_name,
        )
        return str(row.payload["reason"]) if row is not None else reason

    def _nfq_retire(self, session, hold: PriceHold, reason: str, *, gave_up=True) -> None:
        slot = hold.event.payload.metadata["fanout_slot_id"]
        if self._nfq_holds.get(slot) is not hold:
            return
        hold.phase = "retired"
        self._nfq_save(session, hold)
        self._nfq_holds.pop(slot, None)
        if gave_up:
            if reason == "duplicate_buy":
                # Retire this retry, but preserve v2's ability to cancel the actual live leg.
                reason = self._nfq_outcome(session, hold.event, "working", "nfq1_existing_webull_buy")
            else:
                ordinary = reason in {"v2_cancel_intent", "v2_replaced_slot_or_attempt", "segment_ended", "slot_filled"}
                reason = self._nfq_outcome(
                    session, hold.event, "rejected_client_abort",
                    (RETIRED_PREFIX if ordinary else GAVE_UP_PREFIX) + reason,
                )
            self._nfq_log_release(hold.event, reason)

    def _nfq_hold(self, session, event, reason: str, *, report=None) -> None:
        event.payload.metadata.setdefault("webull_mirror_generation_id", str(event.event_id))
        slot = event.payload.metadata["fanout_slot_id"]
        hold = self._nfq_holds.get(slot)
        if hold is None:
            hold = PriceHold(event.model_copy(deep=True), uuid4())
            self._nfq_holds[slot] = hold
        else:
            hold.event = event.model_copy(deep=True)
            hold.phase = "held"
            hold.token = ""
        self._nfq_save(session, hold)
        self._forget_webull_mirror_deferred(slot, reason="nfq1_owns_price_wait")
        self._nfq_outcome(session, event, "held_no_fresh_quote", HELD_REASON)
        self._nfq_log(event, "held", reason, report=report)

    def _nfq_pre_submit(self, session, event) -> str | None:
        if not self._nfq_enabled() or not self._is_webull_resting_mirror_event(event):
            return None
        if not self._nfq_window_open(event):
            reason = GAVE_UP_PREFIX + "resting_window_ended"
            hold = self._nfq_holds.get(event.payload.metadata["fanout_slot_id"])
            if hold is not None and hold.event.event_id == event.event_id:
                self._nfq_retire(session, hold, "resting_window_ended")
            else:
                self._nfq_outcome(session, event, "rejected_client_abort", reason)
                self._nfq_log(event, "gave_up", "resting_window_ended")
            return reason
        if not self._mirror_reading(event.payload.symbol).fresh:
            self._nfq_hold(session, event, "no_fresh_quote")
            return HELD_REASON
        return None

    def _nfq_observe_intent(self, event) -> None:
        if not self._nfq_enabled() or not self._nfq_holds:
            return
        md = event.payload.metadata
        if event.payload.strategy_code != "schwab_1m_v2":
            return
        with self.session_factory() as session:
            for hold in list(self._nfq_holds.values()):
                old = hold.event.payload
                if old.symbol != event.payload.symbol:
                    continue
                same_segment = md.get("fanout_segment_id") == old.metadata.get("fanout_segment_id")
                same_slot = md.get("fanout_slot_id") == old.metadata.get("fanout_slot_id")
                if event.payload.intent_type == "cancel":
                    if event.payload.broker_account_name != old.broker_account_name:
                        continue
                    if str(md.get("fanout_source", "")) != "rth_resting_mirror":
                        continue
                    # Bound cancels must not retire a newer generation. Unbound legacy cancels
                    # still manage the entire account/symbol mirror, matching the broker path.
                    if md.get("fanout_slot_id") and not (same_segment and same_slot):
                        continue
                    generation = md.get("webull_mirror_generation_id")
                    if generation and generation != old.metadata.get("webull_mirror_generation_id"):
                        continue
                    attempt = md.get("fanout_attempt_id") or md.get("fanout_predecessor_attempt_id")
                    if attempt and attempt != old.metadata.get("fanout_attempt_id"):
                        continue
                    self._nfq_retire(session, hold, "v2_cancel_intent")
                elif self._resting_fanout_pair_key(event) is not None:
                    if md.get("nfq_retry_token"):
                        continue
                    replaced = (not same_segment or not same_slot or (
                        self._is_webull_resting_mirror_event(event)
                        and event.event_id != hold.event.event_id))
                    if replaced:
                        self._nfq_retire(session, hold, "v2_replaced_slot_or_attempt")
            session.commit()

    def _nfq_observe_reports(self, session, event, reports) -> None:
        if not self._nfq_enabled() or not reports:
            return
        md = event.payload.metadata
        slot = md.get("fanout_slot_id", "")
        hold = self._nfq_holds.get(slot)
        same_segment = hold is not None and md.get("fanout_segment_id") == hold.event.payload.metadata.get("fanout_segment_id")
        if same_segment and any(r.event_type in {"filled", "partially_filled"} for r in reports):
            is_mirror = self._is_webull_resting_mirror_event(event)
            self._nfq_retire(session, hold, "slot_filled", gave_up=not is_mirror)
            if is_mirror:
                self._nfq_log(event, "sent", "slot_filled", report=reports[-1])
            return
        if not self._is_webull_resting_mirror_event(event):
            return
        if hold is not None and (not same_segment or md.get("fanout_attempt_id") != hold.event.payload.metadata.get("fanout_attempt_id")):
            return
        for report in reports:
            shape = report.metadata.get("webull_resting_mirror_shape")
            if (report.event_type == "rejected" and report.origin == "client"
                    and shape == "abandoned_no_fresh_quote"):
                # The adapter's structured no-wire result covers a stamp expiring after dispatch.
                # Do not grade broker text as this proof, and do not spend PA1's broker retry budget.
                self._nfq_hold(session, event, "stamp_expired_at_wire", report=report)
                return
            if shape == "abandoned_window_ended":
                if hold is not None:
                    self._nfq_retire(session, hold, "resting_window_ended")
                else:
                    self._nfq_outcome(session, event, "rejected_client_abort", GAVE_UP_PREFIX + "resting_window_ended")
                    self._nfq_log(event, "gave_up", "resting_window_ended", report=report)
                return
            if report.event_type == "rejected" and shape == "abandoned_ask_past_band":
                if hold is None:
                    self._nfq_outcome(session, event, "rejected_client_abort", GAVE_UP_PREFIX + "ASK_PAST_BAND")
                    self._nfq_log(event, "gave_up", "ASK_PAST_BAND", report=report)
                else:
                    self._nfq_retire(session, hold, "ASK_PAST_BAND")
                return
        if hold is not None:
            # Normal ledger outcomes now own the leg (including PA1/429); never retain an NFQ
            # retry alongside another owner or after an uncertain broker dispatch.
            self._nfq_retire(session, hold, "broker_pipeline_completed", gave_up=False)
        if reports:
            report = reports[-1]
            if report.event_type in {"rejected", "aborted", "cancelled", "expired"}:
                if slot in self.__dict__.get("_webull_mirror_deferred_by_slot", {}):
                    self._nfq_outcome(session, event, "held_no_fresh_quote", "nfq1_PRICE_AGGRESSIVE_wait")
                    self._nfq_log(event, "held", "PRICE_AGGRESSIVE", report=report)
                else:
                    reason = report.reason or report.metadata.get("webull_error_code") or "broker_pipeline_refused"
                    outcome = broker_outcome(report.event_type, report.origin)
                    terminal = outcome in TERMINAL_RELEASE_OUTCOMES
                    reason = self._nfq_outcome(
                        session, event, outcome, reason,
                    )
                    if terminal:
                        self._nfq_log_release(event, reason, report=report)
                    else:
                        self._nfq_log(event, "held", reason, report=report)
            else:
                self._nfq_log(event, "sent", report.event_type, report=report)

    def _nfq_retirement_reason(self, session, hold) -> str | None:
        event = hold.event
        md = event.payload.metadata
        reason = None if self._nfq_window_open(event) else "resting_window_ended"
        latest = session.scalar(select(DashboardSnapshot).where(
            DashboardSnapshot.snapshot_type == SEGMENT_SNAPSHOT,
            DashboardSnapshot.payload["symbol"].as_string() == event.payload.symbol,
        ).order_by(DashboardSnapshot.created_at.desc(), DashboardSnapshot.id.desc()).limit(1))
        if latest is None:
            reason = reason or "segment_identity_unproven"
        evidence = latest.payload if latest is not None else {}
        if (not isinstance(evidence, dict)
                or evidence.get("strategy_code") != "schwab_1m_v2"
                or type(evidence.get("active")) is not bool
                or evidence.get("session_anchor") != current_session_anchor(self._nfq_now()).isoformat()):
            reason = reason or "segment_identity_unproven"
        elif (evidence["active"] is not True or
                str(evidence.get("fanout_segment_id")) != md["fanout_segment_id"]):
            reason = reason or "segment_ended"
        # A durable fill on either leg or a live Webull buy always outranks a held retry,
        # including after restart. This adds no broker read or 429 load.
        orders = session.scalars(select(BrokerOrder).where(
            BrokerOrder.symbol == event.payload.symbol, BrokerOrder.side == "buy",
            BrokerOrder.payload["fanout_slot_id"].as_string() == md["fanout_slot_id"],
        )).all()
        for order in orders:
            if str((order.payload or {}).get("fanout_segment_id")) != md["fanout_segment_id"]:
                continue
            filled = session.scalar(select(Fill.id).where(Fill.order_id == order.id).limit(1))
            if filled is not None or order.status in {"filled", "partially_filled"}:
                return "slot_filled"
            if order.status == "aborted":
                if local_rpg_abort_proof(session, order) is None:
                    return "dispatch_uncertain"
                continue
            account = session.get(BrokerAccount, order.broker_account_id)
            if account is not None and account.name == event.payload.broker_account_name and order.status not in {
                "rejected", "aborted", "cancelled", "canceled", "expired", "filled",
            }:
                if order.status == "pending" and (order.payload or {}).get("nfq_retry_token"):
                    return "dispatch_uncertain"
                return "duplicate_buy"
        return reason

    def _restore_nfq_holds(self) -> None:
        if not self._nfq_enabled():
            return
        with self.session_factory() as session:
            rows = session.scalars(select(DashboardSnapshot).where(
                DashboardSnapshot.snapshot_type == SNAPSHOT_TYPE,
                DashboardSnapshot.payload["phase"].as_string().in_(["held", "queued", "dispatching", "uncertain"]),
            )).all()
            for row in rows:
                hold = PriceHold(TradeIntentEvent.model_validate(row.payload["event"]), row.id)
                self._nfq_holds[hold.event.payload.metadata["fanout_slot_id"]] = hold
                reason = self._nfq_retirement_reason(session, hold)
                if reason == "dispatch_uncertain":
                    hold.phase = "uncertain"
                    reason = None
                if reason:
                    self._nfq_retire(session, hold, reason)
                else:
                    # Invalidate queued tokens from the old process; fresh ticks mint a new token.
                    if row.payload["phase"] in {"dispatching", "uncertain"}:
                        hold.phase = "uncertain"
                    self._nfq_save(session, hold)
                    self._nfq_outcome(session, hold.event, "held_no_fresh_quote", HELD_REASON)
                    self._nfq_log(hold.event, "held", "restart_dispatch_uncertain" if hold.phase == "uncertain" else "oms_restart_restored")
            session.commit()

    async def _evaluate_nfq_holds(self, symbol: str | None = None) -> None:
        if not self._nfq_enabled() or not self._nfq_holds:
            return
        before = deepcopy(self._nfq_holds)
        try:
            queued = self._prepare_nfq_queues(symbol)
        except Exception:
            # No await or broker call occurs during preparation. A failed transaction must not
            # remove ownership, terminate the control loop, or skip quote-driven exits.
            self.__dict__["_nfq_price_holds"] = before
            self.logger.exception("[OMS-NFQ1] state_transaction_failed; no enqueue")
            for hold in before.values():
                self._nfq_log(hold.event, "held", "state_transaction_failed")
            return
        for hold, retry in queued:
            try:
                await self.redis.xadd(
                    stream_name(self.settings.redis_stream_prefix, "strategy-intents"),
                    {"data": retry.model_dump_json()},
                    maxlen=self.settings.redis_strategy_intent_stream_maxlen, approximate=True,
                )
            except Exception:
                # Invalidate before persistence: even a lost acknowledgement plus a DB outage
                # cannot admit the uncertain stream copy in this process.
                hold.token = ""
                hold.phase = "enqueue_uncertain"
                try:
                    with self.session_factory() as session:
                        self._nfq_retire(session, hold, "serial_enqueue_unconfirmed")
                        session.commit()
                except Exception:
                    hold.phase = "enqueue_uncertain"
                    self._nfq_holds[hold.event.payload.metadata["fanout_slot_id"]] = hold
                    self.logger.exception("[OMS-NFQ1] enqueue retirement persistence failed; token invalid")
                self.logger.exception("[OMS-NFQ1] serial enqueue unconfirmed; token retired")

    def _prepare_nfq_queues(self, symbol: str | None):
        # This short local transaction owns the state change before the first await. Only the
        # serial consumer can dispatch. A repeated tick cannot queue another generation.
        queued = []
        with self.session_factory() as session:
            for hold in list(self._nfq_holds.values()):
                if symbol is not None and hold.event.payload.symbol != symbol.upper():
                    continue
                if hold.phase == "enqueue_uncertain":
                    self._nfq_retire(session, hold, "serial_enqueue_unconfirmed")
                    continue
                reason = self._nfq_retirement_reason(session, hold)
                if reason == "dispatch_uncertain":
                    if hold.phase != "uncertain":
                        hold.phase = "uncertain"
                        self._nfq_save(session, hold)
                        self._nfq_outcome(session, hold.event, "held_no_fresh_quote", "nfq1_dispatch_uncertain")
                        self._nfq_log(hold.event, "held", "dispatch_uncertain")
                    continue
                if reason:
                    self._nfq_retire(session, hold, reason)
                    continue
                if symbol is None or hold.phase != "held":
                    continue
                if not self._mirror_reading(hold.event.payload.symbol).fresh:
                    self._nfq_log(hold.event, "held", "no_fresh_quote")
                    continue
                retry = TradeIntentEvent(source_service="oms-risk", produced_at=self._nfq_now(),
                                         payload=hold.event.payload.model_copy(deep=True))
                hold.token = str(uuid4())
                retry.payload.metadata["nfq_retry_token"] = hold.token
                retry.payload.metadata["nfq_hold_id"] = str(hold.row_id)
                # Preserve PA1 attempt metadata so switching through a price hold cannot reset it.
                retry.payload.metadata.pop("webull_deferred_resubmit", None)
                hold.phase = "queued"
                self._nfq_save(session, hold)
                queued.append((hold, retry))
            session.commit()
        return queued

    def _claim_nfq_retry(self, event) -> bool:
        token = event.payload.metadata.get("nfq_retry_token")
        if not token:
            return True
        slot = event.payload.metadata.get("fanout_slot_id", "")
        hold = self._nfq_holds.get(slot)
        if (hold is None or hold.phase != "queued" or hold.token != token
                or str(hold.row_id) != event.payload.metadata.get("nfq_hold_id")
                or hold.event.payload.metadata.get("fanout_segment_id") != event.payload.metadata.get("fanout_segment_id")
                or hold.event.payload.metadata.get("fanout_attempt_id") != event.payload.metadata.get("fanout_attempt_id")):
            self._nfq_log(event, "ignored", "stale_serial_token")
            return False
        with self.session_factory() as session:
            reason = self._nfq_retirement_reason(session, hold)
            if reason:
                self._nfq_retire(session, hold, reason)
                session.commit()
                return False
            hold.phase = "dispatching"
            event.payload.metadata["fanout_predecessor_attempt_id"] = hold.event.payload.metadata.get("fanout_attempt_id", "")
            hold.event = event.model_copy(deep=True)
            # process_trade_intent binds this same id before writing its intent.
            hold.event.payload.metadata["fanout_attempt_id"] = self._build_client_order_id(event)
            self._nfq_save(session, hold)
            session.commit()
        return True

    def _finish_nfq_retry(self, event, *, completed: bool = True) -> None:
        if not event.payload.metadata.get("nfq_retry_token"):
            return
        hold = self._nfq_holds.get(event.payload.metadata.get("fanout_slot_id", ""))
        if not completed:
            # Report handling mutates memory in the intent transaction. If that transaction
            # failed after acceptance, the committed pre-wire claim/order remain authoritative.
            with self.session_factory() as session:
                row = session.get(DashboardSnapshot, UUID(event.payload.metadata["nfq_hold_id"]))
                if row is None or row.payload["phase"] not in {"dispatching", "uncertain"}:
                    return
                hold = PriceHold(TradeIntentEvent.model_validate(row.payload["event"]), row.id,
                                 phase="uncertain", token="")
                self._nfq_holds[hold.event.payload.metadata["fanout_slot_id"]] = hold
                self._nfq_save(session, hold)
                self._nfq_outcome(session, hold.event, "held_no_fresh_quote", "nfq1_dispatch_uncertain")
                self._nfq_log(hold.event, "held", "dispatch_uncertain")
                session.commit()
            return
        if hold is not None and hold.phase == "dispatching" and hold.token == event.payload.metadata["nfq_retry_token"]:
            with self.session_factory() as session:
                reason = self._nfq_retirement_reason(session, hold)
                if reason == "dispatch_uncertain":
                    hold.phase = "uncertain"
                    self._nfq_save(session, hold)
                    self._nfq_outcome(session, hold.event, "held_no_fresh_quote", "nfq1_dispatch_uncertain")
                    self._nfq_log(hold.event, "held", "dispatch_uncertain")
                else:
                    self._nfq_retire(session, hold, "serial_pipeline_ended")
                session.commit()
