"""MIRRORHOLD1: retain an exact RTH mirror opportunity, not broker permission."""
from __future__ import annotations

import asyncio
import hashlib
import json
from copy import deepcopy
from datetime import UTC, datetime
from threading import RLock
from decimal import Decimal
from uuid import NAMESPACE_URL, uuid4, uuid5

from sqlalchemy import String, and_, cast, event as sqlalchemy_event, func, not_, or_, select, update
from sqlalchemy.dialects.postgresql import JSONB

from project_mai_tai.broker_adapters.protocols import OrderRequest
from project_mai_tai.broker_adapters.webull import WebullBrokerAdapter
from project_mai_tai.db.models import BrokerAccount, BrokerOrder, BrokerOrderEvent, DashboardSnapshot, Fill, Strategy, TradeIntent
from project_mai_tai.events import TradeIntentEvent, stream_name
from project_mai_tai.fanout_segment_store import SNAPSHOT_TYPE as SEGMENT_SNAPSHOT, current_session_anchor
from project_mai_tai.oms.mirror_fresh_price import HELD_REASON, SNAPSHOT_TYPE as NFQ_SNAPSHOT


SNAPSHOT_TYPE = "oms_webull_mirror_retained_hold"
MAX_WIRE_SUBMISSIONS = 4  # One initial submission, then at most three resubmissions.
FENCED_PHASES = {"dispatching", "uncertain"}


def identity(event):
    p, md = event.payload, event.payload.metadata
    return [p.broker_account_name, p.strategy_code, p.symbol.upper(),
            md["fanout_segment_id"], md["fanout_slot_id"]]


def row_id(event):
    return uuid5(NAMESPACE_URL, "mirrorhold1:" + json.dumps(identity(event)))


def price_generation(event):
    md = event.payload.metadata
    # Authorization nonces/event IDs are deliberately absent. Quantity remains an
    # independent dispatch binding and cannot change under a queued price claim.
    prices = [str(Decimal(md[k]).normalize()) for k in ("stop_price", "limit_price")]
    if any(not Decimal(p).is_finite() or Decimal(p) <= 0 for p in prices):
        raise ValueError("invalid mirror prices")
    request = OrderRequest("canonical-only-no-dispatch", event.payload.broker_account_name,
        event.payload.strategy_code, event.payload.symbol, "buy", "open", event.payload.quantity,
        "canonical-only", metadata=md)
    limit, stop, _, refusal = WebullBrokerAdapter._prepare_single_leg_prices(
        request=request, order_type="STOP_LIMIT", stop_price=Decimal(prices[0]), limit_price=Decimal(prices[1]))
    if refusal:
        raise ValueError(refusal)
    prices = [str(stop.normalize()), str(limit.normalize())]
    return hashlib.sha256(json.dumps([identity(event), prices]).encode()).hexdigest()


class MirrorRetainedHoldMixin:
    def _mirrorhold_cache(self):
        return self.__dict__.setdefault("_mirrorhold_by_symbol", {})

    def _mirrorhold_cache_lock(self):
        return self.__dict__.setdefault("_mirrorhold_cache_mutex", RLock())

    def _mirrorhold_cache_after_commit(self, session, key, data):
        # Publish only committed revisions. A rolled-back queue claim must never
        # hide a held owner from the tick consumer.
        pending_key = "hotfix1_mirrorhold_committed_cache"
        if pending_key not in session.info:
            session.info[pending_key] = {}
            def publish(_session):
                pending = dict(_session.info[pending_key])
                _session.info[pending_key].clear()
                with self._mirrorhold_cache_lock():
                    revisions = self.__dict__.setdefault("_mirrorhold_cache_revisions", {})
                    for owner, snapshot in pending.items():
                        if revisions.get(owner, -1) > snapshot["revision"]:
                            continue
                        revisions[owner] = snapshot["revision"]
                        owners = self.__dict__.setdefault("_mirrorhold_durable_owner_ids", set())
                        fences = self.__dict__.setdefault("_mirrorhold_fenced_owner_ids", set())
                        if snapshot["phase"] in {"held", "queued"}:
                            owners.add(owner)
                        else:
                            owners.discard(owner)
                        if snapshot["phase"] in FENCED_PHASES or snapshot.get("dispatch_unresolved"):
                            fences.add(owner)
                        else:
                            fences.discard(owner)
                        symbol = snapshot["identity"][2].upper()
                        bucket = self._mirrorhold_cache().setdefault(symbol, {})
                        if snapshot["phase"] in {"held", "queued"}:
                            bucket[owner] = snapshot
                        else:
                            bucket.pop(owner, None)
                            # A terminal owner must not become a legacy deferred
                            # actor when OFF removes it from retained admission.
                            event = TradeIntentEvent.model_validate(snapshot["event"])
                            slot = event.payload.metadata["fanout_slot_id"]
                            projections = self.__dict__.get("_webull_mirror_deferred_by_slot", {})
                            existing = projections.get(slot)
                            if (existing is not None and row_id(existing.event) == owner
                                    and existing.event.produced_at <= event.produced_at):
                                projections.pop(slot, None)
                        if not bucket:
                            self._mirrorhold_cache().pop(symbol, None)
            def rollback(_session):
                _session.info[pending_key].clear()
            sqlalchemy_event.listen(session, "after_commit", publish)
            sqlalchemy_event.listen(session, "after_rollback", rollback)
        session.info[pending_key][key] = deepcopy(data)

    def _mirrorhold_new_enabled(self):
        return bool(getattr(self.settings, "oms_v2_webull_mirror_retained_hold_enabled", False))

    def _mirrorhold_owner_ids(self):
        # Uncertain wires remain serial admission fences, never tick eligibility.
        return (self.__dict__.get("_mirrorhold_durable_owner_ids", set())
                | self.__dict__.get("_mirrorhold_fenced_owner_ids", set()))

    def _mirrorhold_enabled(self):
        return self._mirrorhold_new_enabled() or bool(self._mirrorhold_owner_ids())

    def _mirrorhold_scope(self, event):
        return (self._is_webull_resting_mirror_event(event) and
                (self._mirrorhold_new_enabled() or row_id(event) in self._mirrorhold_owner_ids()))

    def _mirrorhold_admit(self, event):
        if (event.payload.side == "buy" and event.payload.intent_type in {"open", "scale"}
                and self._mirrorhold_enabled()):
            with self.session_factory() as session:
                rows = session.scalars(select(DashboardSnapshot).where(
                    DashboardSnapshot.snapshot_type == SNAPSHOT_TYPE,
                    DashboardSnapshot.payload["identity"][0].as_string() == event.payload.broker_account_name,
                    DashboardSnapshot.payload["identity"][2].as_string() == event.payload.symbol.upper(),
                )).all()
                unresolved = [r for r in rows if r.payload.get("dispatch_unresolved", r.payload["phase"] in FENCED_PHASES)]
                for row in unresolved:
                    if not self._mirrorhold_reconcile_terminal(session, row):
                        return False
                if unresolved:
                    session.commit()
        if not self._mirrorhold_scope(event):
            return not (self._mirrorhold_new_enabled() and event.payload.metadata.get("nfq_retry_token"))
        if event.payload.metadata.get("nfq_retry_token"):
            return False  # A transferred NFQ token is never authority for this owner.
        with self.session_factory() as session:
            row = self._mirrorhold_read(session, event)
            if row is None:
                return self._mirrorhold_new_enabled() and not event.payload.metadata.get("mirrorhold_token")
            data = row.payload
            if data["phase"] in FENCED_PHASES | {"retired", "filled", "capped"}:
                return False
            if event.payload.metadata.get("mirrorhold_token"):
                return self._mirrorhold_token_matches(data, event)
            old = TradeIntentEvent.model_validate(data["event"])
            if event.produced_at < old.produced_at:
                return False
            return self._build_client_order_id(event) not in data["wire_clients"]

    def _mirrorhold_finish(self, event):
        if not self._mirrorhold_scope(event) or not event.payload.metadata.get("mirrorhold_token"):
            return
        with self.session_factory() as session:
            row = self._mirrorhold_read(session, event)
            if row is None:
                return
            data = row.payload
            if data["phase"] == "queued" and data["token"] == event.payload.metadata["mirrorhold_token"]:
                data = self._mirrorhold_write(session, row, {**data, "phase": "held", "token": "",
                    "reason": "serial_no_dispatch"})
                self._mirrorhold_project(data)
                session.commit()

    def _mirrorhold_read(self, session, event):
        return session.scalar(select(DashboardSnapshot).where(
            DashboardSnapshot.id == row_id(event), DashboardSnapshot.snapshot_type == SNAPSHOT_TYPE,
        ).with_for_update())

    def _mirrorhold_write(self, session, row, data):
        previous = row.payload
        data = {**data, "revision": previous["revision"] + 1}
        changed = session.execute(update(DashboardSnapshot).where(
            DashboardSnapshot.id == row.id,
            DashboardSnapshot.payload["revision"].as_integer() == previous["revision"],
        ).values(payload=data), execution_options={"synchronize_session": False}).rowcount
        if changed != 1:
            raise RuntimeError("mirrorhold1 stale revision; no dispatch")
        session.expire(row, ["payload"])
        self._mirrorhold_cache_after_commit(session, row.id, data)
        return data

    def _mirrorhold_log(self, data, reason):
        self.logger.info("[OMS-MIRRORHOLD1] account=%s symbol=%s segment=%s slot=%s "
                         "phase=%s wires=%s resubmissions=%s reason=%s",
                         data["identity"][0], data["identity"][2], data["identity"][3],
                         data["identity"][4], data["phase"], data["wire_submissions"],
                         max(0, data["wire_submissions"] - 1), reason)

    def _mirrorhold_project(self, data):
        from project_mai_tai.oms.service import _DeferredWebullRestingMirror
        event = TradeIntentEvent.model_validate(data["event"])
        slot = event.payload.metadata["fanout_slot_id"]
        projections = self.__dict__.setdefault("_webull_mirror_deferred_by_slot", {})
        if data["phase"] not in {"held", "queued"}:
            existing = projections.get(slot)
            if existing is not None and existing.event.payload.broker_account_name == data["identity"][0]:
                projections.pop(slot, None)
            return
        projections[slot] = _DeferredWebullRestingMirror(
            event=event, symbol=event.payload.symbol.upper(), segment_id=data["identity"][3],
            slot_id=slot, stop_price=Decimal(event.payload.metadata["stop_price"]),
            attempts=0, queued=data["phase"] == "queued", local_no_wire=data["local_no_wire"],
        )

    def _mirrorhold_upsert(self, session, event, *, no_wire=False):
        row = self._mirrorhold_read(session, event)
        if row is None:
            if not self._mirrorhold_new_enabled():
                raise RuntimeError("mirrorhold1 admission OFF; no new owner")
            clients, uncertain = self._mirrorhold_prior_wires(session, event)
            row = DashboardSnapshot(id=row_id(event), snapshot_type=SNAPSHOT_TYPE, payload={
                "revision": 0, "identity": identity(event), "event": event.model_dump(mode="json"),
                "price_generation": price_generation(event), "phase": "uncertain" if uncertain else "held", "token": "",
                "wire_submissions": len(clients), "wire_clients": clients, "dispatch_client": "",
                "dispatch_unresolved": uncertain,
                "local_no_wire": no_wire and not uncertain, "reason": "prior_wire_unproven" if uncertain else "new_opportunity",
            })
            session.add(row)
            session.flush()
            self._mirrorhold_cache_after_commit(session, row.id, row.payload)
            return row
        data = row.payload
        if data["phase"] in FENCED_PHASES or data["phase"] in {"retired", "filled", "capped"}:
            return row
        if event.payload.metadata.get("mirrorhold_token"):
            return row
        # A new price/authorization promotes the payload, never the wire budget.
        # Accepted orders still require the ordinary old-clear/duplicate guards.
        self._mirrorhold_write(session, row, {**data, "event": event.model_dump(mode="json"),
            "price_generation": price_generation(event), "token": "", "phase": "held",
            "local_no_wire": no_wire, "reason": "current_intent"})
        return row

    def _mirrorhold_audits(self, session, order):
        intent = session.get(TradeIntent, order.intent_id) if order.intent_id else None
        if (intent is None or intent.strategy_id != order.strategy_id
                or intent.broker_account_id != order.broker_account_id
                or intent.symbol != order.symbol or intent.side != order.side
                or intent.intent_type != "open" or intent.quantity != order.quantity):
            return []
        return [audit for audit in session.scalars(select(BrokerOrderEvent).where(BrokerOrderEvent.order_id == order.id))
                if (audit.payload or {}).get("client_order_id") == order.client_order_id]

    def _mirrorhold_clear_order(self, session, order):
        if session.scalar(select(Fill.id).where(Fill.order_id == order.id).limit(1)):
            return False
        if order.status == "aborted":
            # T43's shared exact proof is optional on this c21 baseline. Missing
            # or unproven aborted evidence remains inflight, never generally terminal.
            from project_mai_tai.oms import atr_reprice_handoff
            proof = getattr(atr_reprice_handoff, "local_rpg_abort_proof", None)
            return proof is not None and proof(session, order) is not None
        for audit in self._mirrorhold_audits(session, order):
            md = (audit.payload or {}).get("metadata", {})
            if any(md.get(k) != (order.payload or {}).get(k)
                   for k in ("fanout_slot_id", "fanout_segment_id")):
                continue
            if any(k in md and md[k] != (order.payload or {}).get(k) for k in (
                    "rpg_handoff_token", "rpg_resting_generation", "mirrorhold_dispatch_generation")):
                continue
            if audit.event_type == order.status and audit.event_source == "broker" and order.status in {
                "rejected", "cancelled", "canceled", "expired",
            }:
                return True
            if (order.status == "rejected" and audit.event_type == "rejected" and audit.event_source == "client"
                    and md.get("webull_local_no_wire") == "true" and not md.get("webull_wire_submitted_at_utc")):
                return True
        return False

    def _mirrorhold_proven_no_wire(self, session, order):
        if order.status == "aborted":
            return self._mirrorhold_clear_order(session, order)
        for audit in self._mirrorhold_audits(session, order):
            md = (audit.payload or {}).get("metadata", {})
            if (order.status != "rejected" or audit.event_type != "rejected" or audit.event_source != "client"
                    or md.get("webull_wire_submitted_at_utc") or not self._mirrorhold_clear_order(session, order)):
                continue
            if md.get("webull_local_no_wire") == "true":
                return True
        return False

    def _mirrorhold_reconcile_terminal(self, session, row):
        data = row.payload
        event = TradeIntentEvent.model_validate(data["event"])
        order = session.scalar(select(BrokerOrder).where(BrokerOrder.client_order_id == data["dispatch_client"]))
        if order is None:
            return False
        account = session.get(BrokerAccount, order.broker_account_id)
        strategy = session.get(Strategy, order.strategy_id)
        intent = session.get(TradeIntent, order.intent_id) if order.intent_id else None
        if (account is None or strategy is None or intent is None or account.name != data["identity"][0]
                or strategy.code != data["identity"][1] or order.symbol != data["identity"][2]
                or order.side != "buy" or intent.intent_type != "open"
                or intent.broker_account_id != order.broker_account_id or intent.strategy_id != order.strategy_id
                or intent.payload.get("event_id") != str(event.event_id)
                or intent.quantity != order.quantity or order.quantity != event.payload.quantity
                or (order.payload or {}).get("mirrorhold_id") != str(row.id)
                or (order.payload or {}).get("mirrorhold_dispatch_generation") != data["price_generation"]
                or any((order.payload or {}).get(k) != event.payload.metadata.get(k) for k in (
                    "fanout_segment_id", "fanout_slot_id", "rpg_handoff_token", "rpg_resting_generation"))
                or not self._mirrorhold_clear_order(session, order)):
            return False
        clients, uncertain = self._mirrorhold_prior_wires(session, event)
        if uncertain:
            return False  # Terminal-zero does not prove an unknown historical wire budget.
        clients = list(dict.fromkeys([*data["wire_clients"], *clients]))
        no_wire = self._mirrorhold_proven_no_wire(session, order)
        phase = "retired" if data["phase"] == "retired" else "held" if no_wire else "blocked"
        data = self._mirrorhold_write(session, row, {**data, "phase": phase, "token": "",
            "wire_clients": clients, "wire_submissions": len(clients), "dispatch_unresolved": False,
            "local_no_wire": no_wire, "reason": "exact_terminal_audit_accounted"})
        self._mirrorhold_project(data)
        self._mirrorhold_log(data, data["reason"])
        return True

    def _mirrorhold_prior_wires(self, session, event):
        account = session.scalar(select(BrokerAccount).where(BrokerAccount.name == event.payload.broker_account_name))
        if account is None:
            return [], False
        orders = session.scalars(select(BrokerOrder).where(
            BrokerOrder.broker_account_id == account.id, BrokerOrder.symbol == event.payload.symbol,
            BrokerOrder.side == "buy", BrokerOrder.payload["fanout_slot_id"].as_string() == identity(event)[4],
            BrokerOrder.payload["fanout_segment_id"].as_string() == identity(event)[3],
        )).all()
        clients, uncertain = [], False
        for order in orders:
            audits = self._mirrorhold_audits(session, order)
            wire = (bool(order.broker_order_id)
                    or any((a.payload or {}).get("metadata", {}).get("webull_wire_submitted_at_utc")
                           or (a.event_source == "broker" and a.event_type in {"accepted", "filled", "partially_filled"})
                           for a in audits))
            if wire:
                clients.append(order.client_order_id)
            elif not self._mirrorhold_proven_no_wire(session, order):
                uncertain = True
        return list(dict.fromkeys(clients)), uncertain

    def _mirrorhold_segment_readable(self, metadata):
        segment = metadata.get("fanout_segment_id") if isinstance(metadata, dict) else None
        return (type(segment) in {str, int} and str(segment).isascii()
                and str(segment).isdigit() and bool(str(segment).lstrip("0")))

    def _mirrorhold_timestamp_readable(self, session, timestamp):
        return (isinstance(timestamp, datetime) and
                (timestamp.utcoffset() is not None or session.get_bind().dialect.name == "sqlite"))

    def _mirrorhold_session_orders(self, session, account, event):
        anchor = current_session_anchor(self._nfq_now())
        segment = BrokerOrder.payload["fanout_segment_id"].as_string()
        if session.get_bind().dialect.name == "postgresql":
            payload = cast(BrokerOrder.payload, JSONB)
            object_type = func.jsonb_typeof(payload)
            segment_type = func.jsonb_typeof(payload["fanout_segment_id"])
            valid_types = {"string", "number"}

            def has_key(key):
                return payload.has_key(key)
        else:
            object_type = func.json_type(BrokerOrder.payload)
            segment_type = func.json_type(BrokerOrder.payload, "$.fanout_segment_id")
            valid_types = {"text", "integer"}

            def has_key(key):
                return func.json_type(BrokerOrder.payload, "$." + key).is_not(None)
        segment_key = has_key("fanout_segment_id")
        unknown_identity = or_(
            object_type.is_(None), object_type != "object",
            and_(segment_key, or_(segment.is_(None), segment_type.not_in(valid_types),
                                 not_(cast(segment, String).regexp_match(r"^[0-9]+$")),
                                 cast(segment, String).regexp_match(r"^0+$"))),
            and_(not_(segment_key), or_(has_key("fanout_slot_id"), has_key("mirrorhold_id"))),
        )
        # PostgreSQL compares typed instants; SQLite tests store UTC naive.
        # Keep working and ambiguous rows without inferring expiry from DAY/GTC.
        return session.scalars(select(BrokerOrder).where(
            BrokerOrder.broker_account_id == account.id, BrokerOrder.symbol == event.payload.symbol,
            BrokerOrder.side == "buy",
            or_(BrokerOrder.submitted_at >= anchor, BrokerOrder.submitted_at.is_(None),
                cast(segment, String) == identity(event)[3], BrokerOrder.status.is_(None),
                BrokerOrder.status.not_in({"filled", "rejected", "cancelled", "canceled", "expired"}),
                unknown_identity),
        )).all()

    def _mirrorhold_unrelated_legacy_fill(self, session, order, event):
        md = order.payload
        if (order.status != "filled" or not isinstance(md, dict)
                or any(k in md for k in ("fanout_segment_id", "fanout_slot_id", "mirrorhold_id"))
                or not self._mirrorhold_timestamp_readable(session, order.submitted_at)):
            return False
        timestamp = order.submitted_at
        if timestamp.utcoffset() is None:  # SQLite's UTC storage only; validated above.
            timestamp = timestamp.replace(tzinfo=UTC)
        segment_started = datetime.fromtimestamp(int(identity(event)[3]) / 1000, UTC)
        return (timestamp < segment_started and bool(session.scalar(
            select(Fill.id).where(Fill.order_id == order.id, Fill.filled_at < segment_started).limit(1))))

    def _mirrorhold_gate(self, session, event, *, ignore_client="", session_bound=False, scan_receipt=None):
        if not self._nfq_window_open(event):
            return "resting_window_ended"
        latest = session.scalar(select(DashboardSnapshot).where(
            DashboardSnapshot.snapshot_type == SEGMENT_SNAPSHOT,
            DashboardSnapshot.payload["symbol"].as_string() == event.payload.symbol,
        ).order_by(DashboardSnapshot.created_at.desc(), DashboardSnapshot.id.desc()).limit(1))
        evidence = latest.payload if latest is not None else {}
        if (evidence.get("strategy_code") != event.payload.strategy_code
                or type(evidence.get("active")) is not bool
                or evidence.get("session_anchor") != current_session_anchor(self._nfq_now()).isoformat()):
            return "segment_identity_unproven"
        if not evidence["active"] or str(evidence.get("fanout_segment_id")) != identity(event)[3]:
            return "segment_ended"
        account = session.scalar(select(BrokerAccount).where(BrokerAccount.name == event.payload.broker_account_name))
        if account is None:
            return None
        # Only this account can settle this leg; sibling rejection/acceptance/fill
        # is not mirror retirement evidence. Other live buys still block dispatch.
        if session_bound:
            orders = self._mirrorhold_session_orders(session, account, event)
            if scan_receipt is not None:
                scan_receipt["orders_considered"] = len(orders)
            # Exact slot/segment fill proof wins over unrelated ambiguity.
            for order in orders:
                md = order.payload
                if (self._mirrorhold_segment_readable(md)
                        and str(md["fanout_segment_id"]) == identity(event)[3]
                        and md.get("fanout_slot_id") == identity(event)[4]
                        and (order.status in {"filled", "partially_filled"} or session.scalar(
                            select(Fill.id).where(Fill.order_id == order.id).limit(1)))):
                    return "mirror_filled"
        else:
            orders = session.scalars(select(BrokerOrder).where(
                BrokerOrder.broker_account_id == account.id, BrokerOrder.symbol == event.payload.symbol,
                BrokerOrder.side == "buy",
            )).all()
        for order in orders:
            if session_bound:
                if not self._mirrorhold_timestamp_readable(session, order.submitted_at):
                    return "dispatch_uncertain"
                if not self._mirrorhold_segment_readable(order.payload):
                    if self._mirrorhold_unrelated_legacy_fill(session, order, event):
                        continue
                    return "dispatch_uncertain"
                if (str(order.payload["fanout_segment_id"]) == identity(event)[3]
                        and (not isinstance(order.payload.get("fanout_slot_id"), str)
                             or not order.payload["fanout_slot_id"].strip())):
                    return "dispatch_uncertain"
            md = order.payload or {}
            same_slot = (md.get("fanout_slot_id") == identity(event)[4]
                         and str(md.get("fanout_segment_id")) == identity(event)[3])
            if same_slot and (order.status in {"filled", "partially_filled"} or session.scalar(
                    select(Fill.id).where(Fill.order_id == order.id).limit(1))):
                return "mirror_filled"
            if order.client_order_id == ignore_client:
                continue
            if order.status in {"rejected", "cancelled", "canceled", "expired", "aborted"}:
                if not self._mirrorhold_clear_order(session, order):
                    return "dispatch_uncertain"
            elif order.status != "filled":
                return "dispatch_uncertain" if order.status == "pending" else "duplicate_buy"
        return None

    def _mirrorhold_observe_intent(self, event):
        if not self._mirrorhold_enabled():
            return False
        p, md = event.payload, event.payload.metadata
        owned = False
        with self.session_factory() as session:
            if self._mirrorhold_scope(event):
                owned = True
                row = self._mirrorhold_upsert(session, event)
                self._mirrorhold_project(row.payload)
            elif (p.intent_type == "cancel" and p.strategy_code == "schwab_1m_v2"
                  and md.get("fanout_source") == "rth_resting_mirror"):
                rows = session.scalars(select(DashboardSnapshot).where(
                    DashboardSnapshot.snapshot_type == SNAPSHOT_TYPE)).all()
                for row in rows:
                    data = row.payload
                    old = TradeIntentEvent.model_validate(data["event"])
                    old_md = old.payload.metadata
                    if (data["identity"][:3] != [p.broker_account_name, p.strategy_code, p.symbol.upper()]
                            or event.produced_at < old.produced_at
                            or any(md.get(k) and md[k] != old_md.get(k) for k in (
                                "fanout_segment_id", "fanout_slot_id", "fanout_attempt_id",
                                "rpg_resting_generation", "webull_mirror_generation_id"))):
                        continue
                    owned = True
                    if md.get("reason") == "reprice" or md.get("atr_reprice") == "true":
                        # This is an ownership transfer, not the end of the opportunity.
                        continue
                    data = self._mirrorhold_write(session, row, {**data, "phase": "retired",
                        "token": "", "reason": "v2_terminal_cancel"})
                    self._mirrorhold_project(data)
                    self._mirrorhold_log(data, data["reason"])
            session.commit()
        return owned

    def _mirrorhold_precheck(self, session, event):
        row = self._mirrorhold_upsert(session, event, no_wire=True)
        data = row.payload
        reading = self._mirror_reading(event.payload.symbol)
        outside = (reading.fresh and reading.price < Decimal(event.payload.metadata["stop_price"]) * Decimal("0.92"))
        if outside:
            if data["phase"] not in FENCED_PHASES | {"retired", "filled", "capped"}:
                data = self._mirrorhold_write(session, row, {**data, "phase": "held", "token": "",
                    "local_no_wire": True, "reason": "outside_8pct"})
            self._mirrorhold_project(data)
            self._mirrorhold_log(data, "outside_8pct_no_submission")
        return outside

    def _mirrorhold_dispatch(self, session, event):
        if not self._mirrorhold_scope(event):
            return None
        scan_receipt = {"orders_considered": 0}

        def refuse(reason):
            self.logger.warning("[OMS-MIRRORHOLD1] account=%s symbol=%s phase=refused reason=%s orders_considered=%s",
                                event.payload.broker_account_name, event.payload.symbol, reason,
                                scan_receipt["orders_considered"])
            return reason

        if self.__dict__.get("_symbol_tick_work_closing", False):
            return refuse("mirrorhold_shutdown")
        row = self._mirrorhold_read(session, event)
        if row is None:
            return refuse("mirrorhold_owner_missing")
        data, md = row.payload, event.payload.metadata
        if data["phase"] not in {"held", "queued"}:
            return refuse("mirrorhold_owner_" + data["phase"])
        if data["wire_submissions"] >= MAX_WIRE_SUBMISSIONS:
            self._mirrorhold_write(session, row, {**data, "phase": "capped", "token": "",
                                                   "reason": "actual_submission_cap"})
            return refuse("mirrorhold_actual_submission_cap")
        if (data["price_generation"] != price_generation(event)
                or Decimal(data["event"]["payload"]["quantity"]) != event.payload.quantity
                or (md.get("mirrorhold_token") and (data["phase"] != "queued"
                    or data["token"] != md["mirrorhold_token"]))):
            return refuse("mirrorhold_stale_generation_or_quantity")
        client = self._build_client_order_id(event)
        if client in data["wire_clients"]:
            return refuse("mirrorhold_duplicate_client")
        reason = self._mirrorhold_gate(session, event, session_bound=True, scan_receipt=scan_receipt)
        if reason:
            if reason == "mirror_filled":
                data = self._mirrorhold_write(session, row, {**data, "phase": "retired", "token": "",
                    "reason": reason})
                self._mirrorhold_project(data)
            return refuse("mirrorhold_" + reason)
        reading = self._mirror_reading(event.payload.symbol)
        if not reading.fresh or reading.price < Decimal(md["stop_price"]) * Decimal("0.92"):
            return refuse("mirrorhold_fresh_in_band_required")
        md["mirrorhold_id"] = str(row.id)
        md["mirrorhold_dispatch_generation"] = data["price_generation"]
        data = self._mirrorhold_write(session, row, {**data, "phase": "dispatching",
            "event": event.model_dump(mode="json"), "dispatch_client": client,
            "local_no_wire": False, "dispatch_unresolved": True, "reason": "reserved_before_adapter"})
        self._mirrorhold_project(data)
        return None

    def _mirrorhold_release_before_wire(self, session, event, order, reason):
        if not self._mirrorhold_scope(event):
            return
        row = self._mirrorhold_read(session, event)
        data = row.payload
        if (data["phase"] != "dispatching" or data["dispatch_client"] != order.client_order_id
                or data["dispatch_client"] != self._build_client_order_id(event)
                or data["price_generation"] != event.payload.metadata.get("mirrorhold_dispatch_generation")
                or order.quantity != event.payload.quantity):
            raise RuntimeError("mirrorhold1 pre-wire abort identity changed")
        from project_mai_tai.broker_adapters.protocols import ExecutionReport
        md = {**event.payload.metadata, "webull_local_no_wire": "true"}
        report = ExecutionReport("rejected", order.client_order_id, symbol=order.symbol, side="buy",
                                 quantity=order.quantity, origin="client", reason=reason, metadata=md)
        self._append_order_event_isolated(session, order=order, report=report, payload={
            "client_order_id": order.client_order_id, "metadata": md, "reason": reason})
        data = self._mirrorhold_write(session, row, {**data, "phase": "held", "token": "",
            "dispatch_unresolved": False, "local_no_wire": True, "reason": "callback_refused_before_adapter"})
        self._mirrorhold_project(data)

    def _mirrorhold_reports(self, session, event, reports):
        if not self._mirrorhold_scope(event):
            return
        row = self._mirrorhold_read(session, event)
        if row is None:
            return
        data = row.payload
        client = data["dispatch_client"]
        if not client or event.payload.metadata.get("mirrorhold_dispatch_generation") != data["price_generation"]:
            return
        if (event.payload.quantity != Decimal(data["event"]["payload"]["quantity"])
                or self._build_client_order_id(TradeIntentEvent.model_validate(data["event"])) != client):
            return
        exact = [r for r in reports if r.client_order_id == client and r.symbol == event.payload.symbol
                 and r.side == "buy" and r.intent_type == "open" and r.quantity == event.payload.quantity]
        if not exact:
            return
        wired = any(r.metadata.get("webull_wire_submitted_at_utc") or
                    (r.origin == "broker" and (r.broker_order_id or r.event_type in {"accepted", "filled", "partially_filled"}))
                    for r in exact)
        clients = list(data["wire_clients"])
        if wired and client not in clients:
            clients.append(client)
        data = {**data, "wire_clients": clients, "wire_submissions": len(clients)}
        if any((r.event_type in {"filled", "partially_filled"} or r.filled_quantity > 0)
               and r.origin == "broker" for r in exact):
            phase, reason = "filled", "mirror_fill"
        elif any(r.event_type == "accepted" and r.origin == "broker" for r in exact):
            phase, reason = "accepted", "mirror_accepted"
        elif client in clients and all(r.event_type == "rejected" and r.origin == "broker" and
                 r.metadata.get("webull_error_code") == "ORDER_RISK_RULE_PRICE_AGGRESSIVE" for r in exact):
            phase, reason = ("capped" if len(clients) >= MAX_WIRE_SUBMISSIONS else "held"), "PRICE_AGGRESSIVE"
        elif all(r.event_type == "rejected" and r.origin == "client" and
                 r.metadata.get("webull_local_no_wire") == "true" and not
                 r.metadata.get("webull_wire_submitted_at_utc") for r in exact):
            phase, reason = "held", "freshness_no_wire"
        elif client in clients and all(r.event_type in {"cancelled", "expired"} and r.origin == "broker"
                 and r.filled_quantity == 0 for r in exact):
            phase, reason = "blocked", "broker_terminal_clear"
        elif client in clients and all(r.event_type == "rejected" and r.origin == "broker" for r in exact):
            phase, reason = "blocked", "broker_terminal_refusal"
        else:
            phase, reason = "uncertain", "dispatch_evidence_unproven"
        unresolved = phase == "uncertain"
        if data["phase"] == "retired" and phase != "filled":
            phase, reason = "retired", data["reason"]
        data = self._mirrorhold_write(session, row, {**data, "phase": phase, "token": "",
            "local_no_wire": phase == "held" and all(r.origin == "client" and
                r.metadata.get("webull_local_no_wire") == "true" and not r.metadata.get("webull_wire_submitted_at_utc")
                for r in exact), "dispatch_unresolved": unresolved, "reason": reason})
        if unresolved:
            order = session.scalar(select(BrokerOrder).where(BrokerOrder.client_order_id == client))
            if order is not None:
                # Preserve the raw refusal/audit, but an unconfirmed POST is not a
                # terminal intent. Existing inflight/strict-flat readers must count it.
                order.status = "pending"
                intent = session.get(TradeIntent, order.intent_id) if order.intent_id else None
                if intent is not None:
                    intent.status = "submitted"
        self._mirrorhold_project(data)
        self._mirrorhold_log(data, reason)

    def _mirrorhold_claim(self, event):
        token = event.payload.metadata.get("mirrorhold_token")
        if not token:
            return not self._is_webull_mirror_deferred_resubmit(event)
        with self.session_factory() as session:
            row = self._mirrorhold_read(session, event)
            if row is None:
                return False
            data = row.payload
            return self._mirrorhold_token_matches(data, event)

    def _mirrorhold_price_budget_exhausted(self, job):
        if not self._mirrorhold_enabled():
            return job.get("attempt", 0) >= self._WEBULL_MIRROR_RESUBMIT_MAX_ATTEMPTS
        raw = job.get("authorization", {}).get("event")
        if not raw:
            return True
        event = TradeIntentEvent.model_validate(raw)
        if not self._mirrorhold_scope(event):
            return job.get("attempt", 0) >= self._WEBULL_MIRROR_RESUBMIT_MAX_ATTEMPTS
        with self.session_factory() as session:
            row = self._mirrorhold_read(session, event)
            return row is None or row.payload["wire_submissions"] >= MAX_WIRE_SUBMISSIONS

    def _mirrorhold_token_matches(self, data, event):
        return (data["phase"] == "queued" and data["token"] == event.payload.metadata.get("mirrorhold_token")
                and data["price_generation"] == price_generation(event)
                and data["identity"] == identity(event)
                and Decimal(data["event"]["payload"]["quantity"]) == event.payload.quantity)

    def _mirrorhold_schedule(self, symbol):
        if not self._mirrorhold_new_enabled():
            return
        if symbol is not None:
            symbol = symbol.upper()
            with self._mirrorhold_cache_lock():
                snapshots = tuple(self._mirrorhold_cache().get(symbol, {}).items())
            reading = self._mirror_reading(symbol)
            keys = [key for key, data in snapshots
                    if data["phase"] == "held"
                    and not data["event"]["payload"]["metadata"].get("rpg_handoff_token")
                    and reading.fresh
                    and reading.price >= Decimal(data["event"]["payload"]["metadata"]["stop_price"]) * Decimal("0.92")
                    and data["wire_submissions"] < MAX_WIRE_SUBMISSIONS
                    and (key, data["revision"]) not in self.__dict__.get("_mirrorhold_tick_attempts", set())]
            if not keys:
                return
            attempts = self.__dict__.setdefault("_mirrorhold_tick_attempts", set())
            if self._schedule_symbol_tick_work(("mirrorhold", symbol),
                    lambda: self._mirrorhold_evaluate_off_loop(symbol, keys)):
                attempts.update((key, data["revision"]) for key, data in snapshots if key in keys)
            return

    async def _mirrorhold_evaluate(self, symbol=None):
        if not self._mirrorhold_enabled():
            return
        busy = self.__dict__.setdefault("_mirrorhold_evaluating", set())
        if symbol in busy:
            return
        busy.add(symbol)
        try:
            await self._mirrorhold_evaluate_off_loop(symbol, None)
        finally:
            busy.discard(symbol)
            if symbol is None:
                self.__dict__.setdefault("_mirrorhold_tick_attempts", set()).clear()

    async def _mirrorhold_evaluate_off_loop(self, symbol, keys):
        queued = await asyncio.to_thread(self._mirrorhold_prepare_queue, symbol, keys)
        for event, token, retry in queued:
            if self.__dict__.get("_symbol_tick_work_closing", False):
                await asyncio.to_thread(self._mirrorhold_invalidate_enqueue, event, token)
                continue
            try:
                await self.redis.xadd(stream_name(self.settings.redis_stream_prefix, "strategy-intents"),
                    {"data": retry.model_dump_json()}, maxlen=self.settings.redis_strategy_intent_stream_maxlen,
                    approximate=True)
            except (Exception, asyncio.CancelledError) as exc:
                await asyncio.to_thread(self._mirrorhold_invalidate_enqueue, event, token)
                if isinstance(exc, asyncio.CancelledError):
                    raise
                self.logger.exception("[OMS-MIRRORHOLD1] enqueue unconfirmed; durable claim fenced")

    def _mirrorhold_invalidate_enqueue(self, event, token):
        with self.session_factory() as session:
            row = self._mirrorhold_read(session, event)
            if row is not None and row.payload["phase"] == "queued" and row.payload["token"] == token:
                data = self._mirrorhold_write(session, row, {**row.payload, "phase": "held",
                    "token": "", "reason": "enqueue_token_invalidated"})
                self._mirrorhold_project(data)
            session.commit()

    def _mirrorhold_prepare_queue(self, symbol, keys):
        queued = []
        with self.session_factory() as session:
            query = select(DashboardSnapshot).where(
                DashboardSnapshot.snapshot_type == SNAPSHOT_TYPE,
            )
            if keys is not None:
                query = query.where(DashboardSnapshot.id.in_(keys))
            else:
                query = query.where(DashboardSnapshot.payload["phase"].as_string().in_(["held", "queued"]))
            rows = session.scalars(query.with_for_update()).all()
            for row in rows:
                data = row.payload
                event = TradeIntentEvent.model_validate(data["event"])
                if symbol is not None and event.payload.symbol != symbol.upper():
                    continue
                reason = self._mirrorhold_gate(session, event)
                if reason in {"resting_window_ended", "segment_ended", "mirror_filled"}:
                    data = self._mirrorhold_write(session, row, {**data, "phase": "retired",
                        "token": "", "reason": reason})
                    self._mirrorhold_project(data)
                    continue
                if reason or data["phase"] != "held" or symbol is None:
                    continue
                self._mirrorhold_project(data)
                # RPG alone owns its authorization callback and old-ticket proof.
                # A retained opportunity never strips a token to bypass that owner.
                if event.payload.metadata.get("rpg_handoff_token"):
                    continue
                reading = self._mirror_reading(event.payload.symbol)
                if not reading.fresh or reading.price < Decimal(event.payload.metadata["stop_price"]) * Decimal("0.92"):
                    continue
                if data["wire_submissions"] >= MAX_WIRE_SUBMISSIONS:
                    continue
                retry = TradeIntentEvent(source_service="oms-risk", produced_at=self._nfq_now(),
                                         payload=event.payload.model_copy(deep=True))
                for key in ("nfq_retry_token", "nfq_hold_id", "webull_deferred_resubmit_attempt"):
                    retry.payload.metadata.pop(key, None)
                token = str(uuid4())
                retry.payload.metadata.update(mirrorhold_token=token, webull_deferred_resubmit="true")
                data = self._mirrorhold_write(session, row, {**data, "phase": "queued", "token": token,
                    "reason": "serial_queue_reserved"})
                self._mirrorhold_project(data)
                queued.append((event, token, retry))
            session.commit()
        return queued

    def _restore_mirrorhold(self):
        with self.session_factory() as session:
            # Transfer only proven pre-dispatch NFQ ownership, invalidating old tokens.
            # An uncertain predecessor is a fence, never a fresh opportunity.
            predecessors = session.scalars(select(DashboardSnapshot).where(
                DashboardSnapshot.snapshot_type == NFQ_SNAPSHOT,
                DashboardSnapshot.payload["phase"].as_string().in_(["held", "queued", "dispatching", "uncertain"]),
            ).with_for_update()).all() if self._mirrorhold_new_enabled() else []
            for old in predecessors:
                event = TradeIntentEvent.model_validate(old.payload["event"])
                if not self._mirrorhold_scope(event):
                    continue
                row = self._mirrorhold_upsert(session, event, no_wire=True)
                if old.payload["phase"] in {"dispatching", "uncertain"}:
                    client = self._build_client_order_id(event)
                    data = {**row.payload, "phase": "uncertain", "dispatch_client": client,
                            "local_no_wire": False, "dispatch_unresolved": True,
                            "nfq_predecessor_hold_id": str(old.id), "reason": "nfq_dispatch_unproven"}
                    order = session.scalar(select(BrokerOrder).where(BrokerOrder.client_order_id == client))
                    intent = session.get(TradeIntent, order.intent_id) if order is not None and order.intent_id else None
                    account = session.get(BrokerAccount, order.broker_account_id) if order is not None else None
                    strategy = session.get(Strategy, order.strategy_id) if order is not None else None
                    md = event.payload.metadata
                    if (intent is not None and account is not None and strategy is not None
                            and account.name == event.payload.broker_account_name
                            and strategy.code == event.payload.strategy_code
                            and intent.broker_account_id == order.broker_account_id
                            and intent.strategy_id == order.strategy_id
                            and order.symbol == event.payload.symbol and order.side == "buy"
                            and intent.intent_type == "open" and intent.quantity == order.quantity == event.payload.quantity
                            and intent.payload.get("event_id") == str(event.event_id)
                            and md.get("nfq_hold_id") == str(old.id) and md.get("nfq_retry_token")
                            and all((order.payload or {}).get(k) == md.get(k) for k in (
                                "nfq_hold_id", "nfq_retry_token", "fanout_segment_id", "fanout_slot_id",
                                "rpg_resting_generation", "webull_mirror_generation_id"))):
                        # This is a local owner link, not a claim that the venue echoed it.
                        link = {"mirrorhold_id": str(row.id), "mirrorhold_dispatch_generation": data["price_generation"]}
                        order.payload = {**order.payload, **link}
                        event.payload.metadata.update(link)
                        data["event"] = event.model_dump(mode="json")
                    self._mirrorhold_write(session, row, data)
                old.payload = {**old.payload, "phase": "retired", "token": "",
                               "reason": "mirrorhold1_ownership_transfer"}
                projection = self.__dict__.get("_nfq_price_holds", {}).get(event.payload.metadata["fanout_slot_id"])
                if projection is not None and projection.event.payload.broker_account_name == event.payload.broker_account_name:
                    self._nfq_price_holds.pop(event.payload.metadata["fanout_slot_id"], None)
            rows = session.scalars(select(DashboardSnapshot).where(
                DashboardSnapshot.snapshot_type == SNAPSHOT_TYPE,
                DashboardSnapshot.payload["phase"].as_string().in_(["held", "queued"]))).all()
            self._mirrorhold_durable_owner_ids = {row.id for row in rows}
            for row in rows:
                data = row.payload
                if data["phase"] == "queued":
                    data = self._mirrorhold_write(session, row, {**data, "phase": "held", "token": "",
                        "reason": "restart_queue_invalidated"})
                if data["phase"] == "held":
                    event = TradeIntentEvent.model_validate(data["event"])
                    reason = self._mirrorhold_gate(session, event)
                    if reason in {"resting_window_ended", "segment_ended", "mirror_filled"}:
                        data = self._mirrorhold_write(session, row, {**data, "phase": "retired", "token": "",
                            "reason": reason})
                self._mirrorhold_project(data)
                self._mirrorhold_cache_after_commit(session, row.id, data)
            # OFF must not discard proof of an uncertain wire. Restore these
            # only as serial fences; they never enter the held/tick index.
            fences = session.scalars(select(DashboardSnapshot).where(
                DashboardSnapshot.snapshot_type == SNAPSHOT_TYPE,
                (DashboardSnapshot.payload["phase"].as_string().in_(FENCED_PHASES)
                 | DashboardSnapshot.payload["dispatch_unresolved"].as_boolean().is_(True)),
            )).all()
            self._mirrorhold_fenced_owner_ids = {row.id for row in fences}
            for row in fences:
                data = row.payload
                if data["phase"] == "dispatching":
                    data = self._mirrorhold_write(session, row, {**data, "phase": "uncertain",
                        "reason": "restart_dispatch_uncertain"})
                self._mirrorhold_cache_after_commit(session, row.id, data)
            session.commit()

    def _nfq_enabled(self):
        return not self._mirrorhold_new_enabled() and super()._nfq_enabled()

    def _nfq_observe_reports(self, session, event, reports):
        if self._mirrorhold_scope(event):
            return  # OFF stops new admission, not ownership of an existing hold.
        super()._nfq_observe_reports(session, event, reports)

    def _nfq_pre_submit(self, session, event):
        if not self._mirrorhold_scope(event):
            return super()._nfq_pre_submit(session, event)
        row = self._mirrorhold_upsert(session, event, no_wire=True)
        if not self._nfq_window_open(event):
            self._mirrorhold_write(session, row, {**row.payload, "phase": "retired", "token": "",
                                                   "reason": "resting_window_ended"})
            return "mirrorhold_resting_window_ended"
        if not self._mirror_reading(event.payload.symbol).fresh:
            self._mirrorhold_project(row.payload)
            self._nfq_outcome(session, event, "held_no_fresh_quote", HELD_REASON)
            self._nfq_log(event, "held", "no_fresh_quote")
            return HELD_REASON
        return None

    async def _evaluate_nfq_holds(self, symbol=None):
        if symbol is None:
            if self._mirrorhold_enabled():
                await self._mirrorhold_evaluate()
        if self._mirrorhold_new_enabled():
            return
        await super()._evaluate_nfq_holds(symbol)
