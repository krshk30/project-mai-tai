"""Exact-episode, fail-closed evidence for scanner removal of an unfilled BUY."""

from __future__ import annotations

import math
import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from time import monotonic
from typing import Callable, Mapping, Sequence
from uuid import UUID

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session, sessionmaker

from project_mai_tai.cancel_terminal_proof import (
    CancelTerminalProof, CompleteWorkingBook, NeverSentScope, TerminalSubmissionWitness,
    UnboundCancelFences, UnboundCancelRequest,
    evaluate_cancel_terminal, evaluate_unbound_cancel_terminal,
)
from project_mai_tai.db.models import (
    AccountPosition,
    BrokerAccount,
    BrokerOrder,
    BrokerOrderEvent,
    DashboardSnapshot,
    Fill,
    OmsManagedPosition,
    Strategy,
    TradeIntent,
    VirtualPosition,
)
from project_mai_tai.fanout_identity import fanout_slot_id
from project_mai_tai.fanout_segment_store import current_session_anchor
from project_mai_tai.oms.atr_reprice_handoff import old_buy_proven_clear, replacement_terminal_zero
from project_mai_tai.oms.cancel_terminal import load_cancel_terminal_evidence, receipt_from_intent
from project_mai_tai.oms.buy_submission_journal import (
    BuyCoverageEpoch, PROTOCOL, close_never_sent_admission, close_terminal_buy_admission,
    lock_buy_scope, opportunity_start_ms,
)
from project_mai_tai.oms.unbound_cancel_book import load_unbound_request_working_books

SNAPSHOT_TYPE = "v2_removed_wait"
DISPATCH_SNAPSHOT_TYPE = "v2_wait_dispatch"
SETTLE_MS = 15_000
ROW_LIMIT = 2048
TERMINAL = frozenset({"cancelled", "canceled", "rejected", "expired"})


def configured_removed_wait_bindings(adapter, account_names: Sequence[str]) -> dict[str, tuple[str, str]]:
    """Expected identities come from configured adapters, never from book response content."""
    from project_mai_tai.broker_adapters.cancel_terminal import broker_binding
    from project_mai_tai.broker_adapters.schwab import SchwabBrokerAdapter
    from project_mai_tai.broker_adapters.webull import WebullBrokerAdapter

    result = {}
    for name in account_names:
        if not isinstance(name, str) or not name or name in result:
            raise ValueError("removal account names unproven")
        leaf, account_id = broker_binding(adapter, name)
        provider = ("schwab" if isinstance(leaf, SchwabBrokerAdapter) else
                    "webull" if isinstance(leaf, WebullBrokerAdapter) else "")
        if not provider or not isinstance(account_id, str) or not account_id.strip():
            raise ValueError("removal configured account unproven")
        result[name] = (provider, account_id)
    return result


@dataclass(frozen=True)
class RemovedWait:
    symbol: str
    opportunity_id: int
    token: str
    requested_at_ms: int
    account_names: tuple[str, ...]
    purpose: str = "scanner_removal"

    def payload(self, *, active: bool) -> dict:
        payload = {
            "schema_version": 1,
            "strategy_code": "schwab_1m_v2",
            "symbol": self.symbol,
            "opportunity_id": str(self.opportunity_id),
            "token": self.token,
            "requested_at_ms": str(self.requested_at_ms),
            "account_names": list(self.account_names),
            "active": active,
        }
        if self.purpose != "scanner_removal":
            payload["purpose"] = self.purpose
        return payload


@dataclass(frozen=True)
class RemovedWaitProof:
    request: RemovedWait
    observed_at_ms: int
    clear: bool
    reason: str
    closed_owned_rows: tuple[tuple[str, str], ...] = ()


def active_request_matches(payload: object, request: RemovedWait) -> bool:
    if not isinstance(payload, dict):
        return False
    plain = request.payload(active=True)
    if payload == plain:
        return True
    return (payload.get("reason") == "unbound_symbol_terminal"
            and payload.get("verdict") == "TERMINAL"
            and isinstance(payload.get("closed_owned_rows"), list)
            and type(payload.get("terminal_observed_at_ms")) is int
            and set(payload) == set(plain) | {"reason", "verdict", "closed_owned_rows", "terminal_observed_at_ms"}
            and all(payload.get(k) == v for k, v in plain.items()))


def prior_session_request(request: RemovedWait, now: datetime) -> bool:
    """Classify the positive opportunity, never request age or its rewrite time."""
    if request.opportunity_id <= 0:
        return False
    anchor = current_session_anchor(now)
    return current_session_anchor(datetime.fromtimestamp(request.opportunity_id / 1000, UTC)) < anchor


def rollover_retry_proven_terminal(row: DashboardSnapshot, account_names: set[str]) -> bool:
    """Mutable retry journal rows are current revisions, not historical presence."""
    p = row.payload
    if not isinstance(p, dict):
        return False
    rpg = row.snapshot_type == "atr_reprice_handoff"
    event = p.get("event")
    scoped = p.get("old") if rpg else event.get("payload") if isinstance(event, dict) else None
    if not isinstance(scoped, dict) or not scoped.get("broker_account_name"):
        return False
    if scoped["broker_account_name"] not in account_names:
        return True
    md = scoped.get("metadata")
    if (scoped.get("side") != "buy" or not scoped.get("strategy_code")
            or not isinstance(md, dict) or not str(md.get("fanout_segment_id", "")).isdigit()
            or int(md["fanout_segment_id"]) <= 0):
        return False
    if rpg:
        if p.get("phase") == "filled" and p.get("no_rebuy") is True:
            try:
                quantity = float(p.get("filled_quantity", "nan"))
            except (TypeError, ValueError):
                return False
            return bool(p.get("replacement_filled") is True
                        or (math.isfinite(quantity) and quantity > 0))
        cleared = p.get("cleared_at")
        typed_clear = p.get("local_no_wire") is True or (
            type(cleared) in {int, float} and math.isfinite(cleared) and cleared > 0)
        return bool(p.get("phase") in {"expired", "refused"} and typed_clear
            and old_buy_proven_clear(p)
            and (not p.get("replacement") or replacement_terminal_zero(p)))
    if row.snapshot_type == "oms_v2_eh_price_hold":
        return p.get("phase") == "retired" and p.get("token") == "" and p.get("dispatch") is None
    if row.snapshot_type == "oms_webull_mirror_price_hold":
        return p.get("phase") == "retired" and isinstance(p.get("token"), str)
    expected = [scoped["broker_account_name"], scoped["strategy_code"], scoped.get("symbol"),
                md["fanout_segment_id"], md.get("fanout_slot_id")]
    return bool(p.get("identity") == expected and md.get("fanout_slot_id")
        and p.get("phase") in {"retired", "filled", "capped"}
        and p.get("dispatch_unresolved") is False)


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def assess_removed_wait(
    request: RemovedWait,
    *,
    now: datetime,
    accounts: Mapping[object, str],
    intents: Sequence[TradeIntent],
    orders: Sequence[BrokerOrder],
    filled_order_ids: set[object],
    order_events: Sequence[BrokerOrderEvent],
    snapshots: Sequence[DashboardSnapshot],
    has_position: bool,
    cancel_terminal_proofs: Mapping[object, CancelTerminalProof | None] | None = None,
) -> RemovedWaitProof:
    """Absence alone is never a receipt. Every configured account must acknowledge removal."""

    def result(reason: str, clear: bool = False) -> RemovedWaitProof:
        return RemovedWaitProof(request, int(now.timestamp() * 1000), clear, reason)

    cutoff = _utc(now) - timedelta(milliseconds=SETTLE_MS)

    def settled(row: object) -> bool:
        at = getattr(row, "updated_at", None)
        return bool(isinstance(at, datetime) and _utc(at) <= cutoff)

    def metadata(row: object) -> dict:
        payload = getattr(row, "payload", None)
        if not isinstance(payload, dict):
            return {}
        md = payload.get("metadata", payload)
        return md if isinstance(md, dict) else {}

    def shared_ack(intent: TradeIntent) -> bool:
        proof = (cancel_terminal_proofs or {}).get(intent.id)
        return bool(proof and proof.terminal
                    and proof.scope.account_name == accounts.get(intent.broker_account_id)
                    and proof.scope.symbol == request.symbol
                    and proof.scope.event_id == (intent.payload or {}).get("event_id")
                    and proof.scope.client_order_id == metadata(intent).get("target_client_order_id"))

    def shared_target(order: BrokerOrder) -> bool:
        return any(p and p.terminal and p.scope.symbol == request.symbol
                   and p.scope.account_name == accounts.get(order.broker_account_id)
                   and p.scope.client_order_id == order.client_order_id
                   for p in (cancel_terminal_proofs or {}).values())

    def cancel_unknown(intent: TradeIntent) -> str | None:
        if cancel_terminal_proofs is not None:
            proof = cancel_terminal_proofs.get(intent.id)
            if not shared_ack(intent):
                return proof.reason if proof is not None and not proof.terminal else "cancel_identity_unknown"
        elif not settled(intent):
            return "cancel_receipt_settling"
        return None

    expected_slot = (
        fanout_slot_id(
            strategy_code="schwab_1m_v2",
            symbol=request.symbol,
            segment_id=request.opportunity_id,
            slot="resting",
        )
        if request.opportunity_id
        else ""
    )

    def exact(md: dict) -> bool:
        return bool(
            request.opportunity_id > 0
            and str(md.get("fanout_segment_id", "")) == str(request.opportunity_id)
            and md.get("fanout_slot") == "resting"
            and md.get("fanout_slot_id") == expected_slot
        )

    if (
        not accounts
        or len(set(accounts.values())) != len(accounts)
        or set(accounts.values()) != set(request.account_names)
        or not all(request.account_names)
        or len(set(request.account_names)) != len(request.account_names)
    ):
        return result("accounts_unreadable")

    if request.purpose in {"retry_exhausted", "false_flip_restore"}:
        # Cancellation-only receipt: historical fills stay owned. This proof must
        # never be used to retire the opportunity or grant another entry.
        if has_position:
            return result("position_stays_managed")
        if not any(r.snapshot_type == SNAPSHOT_TYPE
                   and active_request_matches(r.payload, request) for r in snapshots):
            return result("removal_not_durable")
        receipts: set[str] = set()
        own_intent_ids = {i.id for i in intents if i.intent_type == "open" and exact(metadata(i))}
        intent_by_id = {i.id: i for i in intents}
        for order in orders:
            if exact(metadata(order)) or order.intent_id in own_intent_ids:
                if order.status not in TERMINAL | {"filled", "aborted"}:
                    return result("opening_order_not_terminal")
                if order.status != "filled":
                    own = intent_by_id.get(order.intent_id)
                    p = own.payload if own is not None else {}
                    no_wire = (not order.broker_order_id and own is not None
                               and p.get("refusal_origin") in {"skipped_before_submit", "client_abort"}
                               and bool(p.get("refusal_code")))
                    status = "cancelled" if order.status == "canceled" else order.status
                    terminal = any(e.order_id == order.id and e.event_source == "broker"
                                   and ("cancelled" if e.event_type == "canceled" else e.event_type) == status
                                   and e.event_type in TERMINAL and _utc(e.event_at) <= cutoff
                                   and metadata(e).get("cancel_outcome") not in {
                                       "already_absent", "confirmed_after_accepted_request",
                                       "could_not_tell", "not_confirmed"}
                                   for e in order_events)
                    if not shared_target(order) and (not settled(order) or not (terminal or no_wire)):
                        return result("broker_terminal_unproven")
        for intent in intents:
            md = metadata(intent)
            if intent.intent_type == "open" and exact(md) and intent.status not in TERMINAL | {"filled", "aborted"}:
                return result("intent_not_terminal")
            if md.get("clearwait_removal_token") != request.token:
                continue
            if (intent.intent_type != "cancel" or not exact(md)
                    or str(md.get("clearwait_opportunity_id")) != str(request.opportunity_id)
                    or md.get("clearwait_buy_only") != "true"
                    or md.get("reason") != ("false_flip_restore" if request.purpose == "false_flip_restore"
                                            else "retry_budget_exhausted")
                    or _utc(intent.created_at).timestamp() * 1000 < request.requested_at_ms):
                return result("cancel_receipt_invalid")
            p = intent.payload or {}
            no_target = (intent.status == "rejected"
                         and p.get("refusal_origin") == "skipped_before_submit"
                         and p.get("refusal_code") == "cancel_target_not_found")
            if (intent.status not in TERMINAL or
                    (intent.status not in {"cancelled", "canceled"} and not no_target and not shared_ack(intent))):
                return result("cancel_unknown_or_refused")
            if unknown := cancel_unknown(intent):
                return result(unknown)
            account = accounts.get(intent.broker_account_id)
            if account is None:
                return result("cancel_account_unproven")
            receipts.add(account)
        if receipts != set(request.account_names):
            return result("waiting_for_all_cancel_receipts")
        return result("false_flip_leftovers_cancelled_owner_kept" if request.purpose == "false_flip_restore"
                      else "retry_leftovers_cancelled_owner_kept", True)
    if request.purpose != "scanner_removal":
        return result("purpose_unreadable")

    own_intent_ids = {i.id for i in intents if i.intent_type == "open" and exact(metadata(i))}
    if request.opportunity_id == 0 and any(
        o.id in filled_order_ids or str(o.status).lower() in {"filled", "partially_filled"}
        for o in orders
    ):
        return result("missing_opportunity_with_own_fill_history")
    if any(
        (exact(metadata(o)) or o.intent_id in own_intent_ids)
        and (o.id in filled_order_ids or str(o.status).lower() in {"filled", "partially_filled"})
        for o in orders
    ):
        return result("own_fill_stays_owned")
    own_order_ids = {o.id for o in orders if exact(metadata(o)) or o.intent_id in own_intent_ids}
    if any(
        e.order_id in own_order_ids
        and (e.event_type in {"filled", "partially_filled"}
             or (isinstance(e.payload, dict) and e.payload.get("broker_fill_id")))
        for e in order_events
    ):
        return result("fill_history_not_unfilled")
    if has_position:
        return result("position_stays_managed")
    persisted = False
    for row in snapshots:
        p = row.payload
        if not isinstance(p, dict):
            return result("snapshot_unreadable")
        if row.snapshot_type == SNAPSHOT_TYPE and p.get("symbol") == request.symbol:
            if p.get("token") == request.token:
                persisted = active_request_matches(p, request)
        if p.get("symbol") != request.symbol:
            continue
        same = str(p.get("opportunity_id", p.get("segment_id", ""))) == str(request.opportunity_id)
        if row.snapshot_type == "v2_flip_entry_ownership" and same:
            if (
                p.get("fill_accounts")
                or p.get("position_ids")
                or p.get("phase") in {"provisional", "bound", "consumed", "awaiting_close"}
                or "not_first_rest" in str(p.get("reason", ""))
            ):
                return result("filled_or_ambiguous_owner")
    if not persisted:
        return result("removal_not_durable")

    # Latest per-ticket state matters: a historical held row is not a current unknown,
    # and a cancelled order does not absolve a different live replacement ticket.
    latest_rpg: dict[str, dict] = {}
    latest_nfq: dict[str, dict] = {}
    for row in snapshots:
        p = row.payload
        if row.snapshot_type == "atr_reprice_handoff":
            old = p.get("old", {})
            md = old.get("metadata", {}) if isinstance(old, dict) else {}
            segment = p.get(
                "segment_id", md.get("fanout_segment_id") if isinstance(md, dict) else None
            )
            if (
                old.get("symbol") == request.symbol
                and (
                    request.opportunity_id == 0
                    or segment is None
                    or str(segment) == str(request.opportunity_id)
                )
                and (
                    old.get("strategy_code") != "schwab_1m_v2"
                    or not isinstance(md, dict)
                    or segment is None
                    or str(md.get("fanout_segment_id", "")) != str(segment)
                )
            ):
                return result("replacement_ticket_metadata_unknown")
            if (
                isinstance(md, dict)
                and old.get("symbol") == request.symbol
                and old.get("strategy_code") == "schwab_1m_v2"
                and (
                    request.opportunity_id == 0
                    or str(p.get("segment_id", md.get("fanout_segment_id", "")))
                    == str(request.opportunity_id)
                )
            ):
                latest_rpg[str(p.get("token", row.id))] = p
        elif row.snapshot_type == "oms_webull_mirror_price_hold":
            event_payload = p.get("event", {}).get("payload", {})
            md = event_payload.get("metadata", {})
            if event_payload.get("symbol") == request.symbol and (
                event_payload.get("strategy_code") is None
                or not isinstance(md, dict)
                or (
                    event_payload.get("strategy_code") == "schwab_1m_v2"
                    and not md.get("fanout_segment_id")
                )
            ):
                return result("mirror_retry_metadata_unknown")
            if (
                event_payload.get("symbol") == request.symbol
                and event_payload.get("strategy_code") == "schwab_1m_v2"
                and (
                    request.opportunity_id == 0
                    or str(md.get("fanout_segment_id", "")) == str(request.opportunity_id)
                )
            ):
                latest_nfq[str(row.id)] = p
    for p in latest_rpg.values():
        cleared_at = p.get("cleared_at")
        typed_clear = p.get("local_no_wire") is True or (
            type(cleared_at) in {int, float} and math.isfinite(cleared_at) and cleared_at > 0
        )
        if (
            p.get("phase") not in {"expired", "refused"}
            or not typed_clear
            or not old_buy_proven_clear(p)
        ):
            return result("replacement_ticket_not_terminal")
    if any(p.get("phase") != "retired" for p in latest_nfq.values()):
        return result("mirror_retry_not_retired")

    by_intent: dict[object, list[BrokerOrder]] = {}
    intents_by_id = {i.id: i for i in intents}
    broker_terminal = {
        (e.order_id, "cancelled" if e.event_type == "canceled" else e.event_type)
        for e in order_events
        if e.event_source == "broker" and e.event_type in TERMINAL and _utc(e.event_at) <= cutoff
        and metadata(e).get("cancel_outcome") not in {
            "already_absent", "confirmed_after_accepted_request", "could_not_tell", "not_confirmed"
        }
    }

    def no_wire_intent(intent: TradeIntent) -> bool:
        p = intent.payload or {}
        return bool(
            str(intent.status).lower() == "rejected"
            and (
                (p.get("refusal_origin") == "skipped_before_submit" and p.get("refusal_code"))
                or (
                    p.get("refusal_origin") == "client_abort"
                    and p.get("refusal_code") == "rpg_stale_strategy_authorization"
                )
            )
        )

    for order in orders:
        by_intent.setdefault(order.intent_id, []).append(order)
        md = metadata(order)
        if str(order.status).lower() not in TERMINAL | {"filled"}:
            return result("opening_order_not_terminal")
        if str(order.status).lower() in TERMINAL:
            if not settled(order) and not shared_target(order):
                return result("terminal_order_settling")
            status = "cancelled" if order.status == "canceled" else order.status
            intent = intents_by_id.get(order.intent_id)
            pre_wire = (
                not order.broker_order_id
                and status == "rejected"
                and intent is not None
                and no_wire_intent(intent)
            )
            if (order.id, status) not in broker_terminal and not pre_wire and not shared_target(order):
                return result("broker_terminal_unproven")
        if exact(md) or (
            request.opportunity_id == 0
            and order.submitted_at
            and _utc(order.submitted_at).timestamp() * 1000 >= request.requested_at_ms
        ):
            if order.id in filled_order_ids or str(order.status).lower() == "filled":
                return result("own_fill_stays_owned")
            if not settled(order) and not shared_target(order):
                return result("terminal_order_settling")

    receipts: set[str] = set()
    for intent in intents:
        md = metadata(intent)
        status = str(intent.status).lower()
        if status not in TERMINAL | {"filled"}:
            return result("intent_not_terminal")
        matching = exact(md)
        if matching and intent.intent_type == "open":
            related = by_intent.get(intent.id, [])
            if status == "filled" or any(
                o.id in filled_order_ids or str(o.status).lower() == "filled" for o in related
            ):
                return result("own_fill_stays_owned")
            if not settled(intent) or any(not settled(o) for o in related):
                return result("terminal_intent_settling")
            if not related:
                if not no_wire_intent(intent):
                    return result("no_order_is_not_no_wire")
        if intent.intent_type == "open" and not matching:
            created_ms = int(_utc(intent.created_at).timestamp() * 1000)
            if created_ms >= (request.opportunity_id or request.requested_at_ms):
                return result("opening_identity_unproven")
        if md.get("clearwait_removal_token") != request.token:
            continue
        if (
            intent.intent_type != "cancel"
            or str(md.get("clearwait_opportunity_id")) != str(request.opportunity_id)
            or md.get("clearwait_buy_only") != "true"
            or _utc(intent.created_at).timestamp() * 1000 < request.requested_at_ms
        ):
            return result("cancel_receipt_invalid")
        payload = intent.payload or {}
        no_target = (
            status == "rejected"
            and payload.get("refusal_origin") == "skipped_before_submit"
            and payload.get("refusal_code") == "cancel_target_not_found"
        )
        if status not in {"cancelled", "canceled"} and not no_target and not shared_ack(intent):
            return result("cancel_unknown_or_refused")
        if unknown := cancel_unknown(intent):
            return result(unknown)
        account = accounts.get(intent.broker_account_id)
        if account is None:
            return result("cancel_account_unproven")
        receipts.add(account)
    if receipts != set(accounts.values()):
        return result("waiting_for_all_cancel_receipts")

    # The journal precedes publication, independently of the OMS transaction.
    # Empty OMS rows or a prior cancelled generation cannot hide a later lost wire.
    journal = [
        r.payload for r in snapshots
        if r.snapshot_type == DISPATCH_SNAPSHOT_TYPE
        and r.payload.get("symbol") == request.symbol
        and str(r.payload.get("opportunity_id")) == str(request.opportunity_id)
    ]
    begins = [p for p in journal if p.get("kind") == "begin"]
    if request.opportunity_id == 0 or len(begins) != 1:
        return result("dispatch_history_unknown")
    expected_accounts = set(request.account_names)
    for p in journal:
        if (p.get("schema_version") != 1 or p.get("strategy_code") != "schwab_1m_v2"
                or p.get("kind") not in {"begin", "attempt"}
                or not isinstance(p.get("account_names"), list)
                or not all(isinstance(a, str) and a for a in p["account_names"])
                or len(p["account_names"]) != len(expected_accounts)
                or set(p["account_names"]) != expected_accounts):
            return result("dispatch_history_unknown")
    attempts = {p.get("attempt_token"): p for p in journal if p.get("kind") == "attempt"}
    if len(attempts) != len([p for p in journal if p.get("kind") == "attempt"]):
        return result("dispatch_history_unknown")
    for token, p in attempts.items():
        if not isinstance(token, str) or not token or p.get("account_name") not in expected_accounts:
            return result("dispatch_history_unknown")
        matched = [i for i in intents if i.intent_type == "open" and exact(metadata(i))
                   and metadata(i).get("clearwait_dispatch_token") == token
                   and accounts.get(i.broker_account_id) == p["account_name"]]
        if not matched:
            return result("dispatch_attempt_unproven")
    for intent in intents:
        if intent.intent_type != "open" or not exact(metadata(intent)):
            continue
        p = attempts.get(metadata(intent).get("clearwait_dispatch_token"))
        if p is None or p["account_name"] != accounts.get(intent.broker_account_id):
            return result("dispatch_history_unknown")
    for order in orders:
        if exact(metadata(order)) or order.intent_id in own_intent_ids:
            p = attempts.get(metadata(order).get("clearwait_dispatch_token"))
            intent = intents_by_id.get(order.intent_id)
            if (p is None or p["account_name"] != accounts.get(order.broker_account_id)
                    or intent is None or intent.id not in own_intent_ids
                    or metadata(intent).get("clearwait_dispatch_token") != p["attempt_token"]):
                return result("dispatch_history_unknown")
    return result("terminal_unfilled_removed_wait", True)


class RemovedWaitStore:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self.session_factory = session_factory

    @staticmethod
    def _lock_request(session: Session, symbol: str) -> None:
        # Append-only rows need a per-symbol lock, not just a lock on yesterday's row.
        if session.get_bind().dialect.name == "postgresql":
            session.execute(select(func.pg_advisory_xact_lock(
                func.hashtext("v2_removed_wait:" + symbol))))

    @staticmethod
    def _rollover_retry_unknown(session: Session, symbol: str, account_names: set[str]) -> bool:
        rows = session.scalars(select(DashboardSnapshot).where(
            DashboardSnapshot.snapshot_type.in_({"atr_reprice_handoff",
                "oms_webull_mirror_price_hold", "oms_webull_mirror_retained_hold", "oms_v2_eh_price_hold"}),
            or_(DashboardSnapshot.payload["symbol"].as_string() == symbol,
                DashboardSnapshot.payload["old"]["symbol"].as_string() == symbol,
                DashboardSnapshot.payload["event"]["payload"]["symbol"].as_string() == symbol),
        ).order_by(DashboardSnapshot.created_at, DashboardSnapshot.id).limit(ROW_LIMIT + 1)).all()
        if len(rows) > ROW_LIMIT:
            return True
        return any(not rollover_retry_proven_terminal(row, account_names) for row in rows)

    def record_dispatch(self, payload: dict) -> None:
        with self.session_factory() as session:
            session.add(DashboardSnapshot(snapshot_type=DISPATCH_SNAPSHOT_TYPE, payload=payload))
            session.commit()

    def record(self, request: RemovedWait, active: bool) -> None:
        with self.session_factory() as session:
            self._lock_request(session, request.symbol)
            payload = request.payload(active=active)
            if not active:
                latest = session.scalar(select(DashboardSnapshot).where(
                    DashboardSnapshot.snapshot_type == SNAPSHOT_TYPE,
                    DashboardSnapshot.payload["symbol"].as_string() == request.symbol,
                ).order_by(DashboardSnapshot.created_at.desc(), DashboardSnapshot.id.desc())
                  .limit(1).with_for_update())
                if latest is not None and latest.payload.get("reason") == "unbound_symbol_terminal":
                    if not active_request_matches(latest.payload, request):
                        raise ValueError("terminal witness active request changed")
                    payload = {**latest.payload, "active": False}
                elif latest is not None and not active_request_matches(latest.payload, request):
                    raise ValueError("active removal request changed")
            session.add(
                DashboardSnapshot(
                    snapshot_type=SNAPSHOT_TYPE, payload=payload
                )
            )
            session.commit()

    def retire_unbound(
        self, requests: Sequence[RemovedWait], account_names: set[str], *,
        books: Mapping[str, CompleteWorkingBook | None] | None = None,
        publication_closed: Mapping[RemovedWait, bool], now: datetime | None = None,
        expected_bindings: Mapping[str, tuple[str, str]] | None = None,
        publication_current: Callable[[RemovedWait], bool] | None = None,
        minimum_book_started_at_ms: int = 0,
    ) -> tuple[RemovedWaitProof, ...]:
        """Off-loop exact-request CAS; an empty DB is not a publication-drained witness."""
        observed = now or datetime.now(UTC)
        results = []
        for request in requests:
            with self.session_factory() as session:
                self._lock_request(session, request.symbol)
                accounts = session.scalars(select(BrokerAccount).where(
                    BrokerAccount.name.in_(account_names))).all()
                ids = {a.id: a.name for a in accounts}
                configured = (dict(expected_bindings) if expected_bindings is not None else
                              {a.name: (a.provider, a.external_account_id or "") for a in accounts})
                valid_bindings = all(isinstance(v, tuple) and len(v) == 2
                    and v[0] in {"schwab", "webull"} and isinstance(v[1], str) and bool(v[1].strip())
                    for v in configured.values())
                binding = {name: value[1] for name, value in configured.items()} if valid_bindings else {}
                exact_accounts = (valid_bindings
                    and len(accounts) == len(account_names) == len(request.account_names)
                    and set(configured) == account_names == set(request.account_names)
                    and all(a.provider == configured[a.name][0]
                        and (a.external_account_id is None or a.external_account_id == configured[a.name][1])
                        for a in accounts))
                if exact_accounts:
                    # Serialize guard reads and the final request CAS with every physical BUY.
                    for account_id in sorted(binding.values()):
                        lock_buy_scope(session, account_id, request.symbol)
                latest = session.scalar(select(DashboardSnapshot).where(
                    DashboardSnapshot.snapshot_type == SNAPSHOT_TYPE,
                    DashboardSnapshot.payload["symbol"].as_string() == request.symbol,
                ).order_by(DashboardSnapshot.created_at.desc(), DashboardSnapshot.id.desc())
                  .limit(1).with_for_update())
                intents = session.scalars(select(TradeIntent).where(
                    TradeIntent.broker_account_id.in_(ids), TradeIntent.symbol == request.symbol,
                    or_(TradeIntent.side == "buy", and_(TradeIntent.intent_type == "cancel",
                        or_(TradeIntent.side.is_(None), TradeIntent.side == ""))),
                    or_(TradeIntent.status.is_(None),
                        TradeIntent.status.not_in(TERMINAL | {"filled", "aborted"}),
                        TradeIntent.payload["metadata"]["clearwait_removal_token"].as_string()
                        == request.token),
                ).limit(ROW_LIMIT + 1)).all()
                orders = session.scalars(select(BrokerOrder).where(
                    BrokerOrder.broker_account_id.in_(ids), BrokerOrder.symbol == request.symbol,
                    or_(BrokerOrder.side == "buy", BrokerOrder.side.is_(None), BrokerOrder.side == ""),
                    or_(BrokerOrder.status.is_(None),
                        BrokerOrder.status.not_in(TERMINAL | {"filled", "aborted"})),
                ).limit(1)).all()
                episode_orders = select(BrokerOrder.id).where(
                    BrokerOrder.broker_account_id.in_(ids), BrokerOrder.symbol == request.symbol,
                    or_(BrokerOrder.payload["metadata"]["fanout_segment_id"].as_string()
                        == str(request.opportunity_id),
                        BrokerOrder.payload["fanout_segment_id"].as_string() == str(request.opportunity_id)),
                )
                managed = session.scalars(select(OmsManagedPosition).where(
                    OmsManagedPosition.broker_account_name.in_(account_names),
                    OmsManagedPosition.symbol == request.symbol,
                    OmsManagedPosition.strategy_code == "schwab_1m_v2",
                    or_(OmsManagedPosition.status != "closed", OmsManagedPosition.current_quantity != 0,
                        and_(request.opportunity_id > 0, OmsManagedPosition.entry_order_id.in_(episode_orders))),
                ).limit(ROW_LIMIT + 1)).all()
                episode_buys = session.scalars(select(BrokerOrder).where(
                    BrokerOrder.id.in_(episode_orders), BrokerOrder.side == "buy",
                ).limit(ROW_LIMIT + 1)).all()
                bounded = (len(intents) <= ROW_LIMIT and len(managed) <= ROW_LIMIT
                           and len(episode_buys) <= ROW_LIMIT)
                pending = (any(i.status not in TERMINAL | {"filled", "aborted"} for i in intents)
                           or self._rollover_retry_unknown(session, request.symbol, account_names))
                unanswered = any(i.intent_type == "cancel" and i.status not in TERMINAL
                                 for i in intents)
                bound_targets = {name: set() for name in request.account_names}
                for intent in intents:
                    md = (intent.payload or {}).get("metadata", {})
                    if md.get("clearwait_removal_token") == request.token and md.get("target_client_order_id"):
                        bound_targets[ids[intent.broker_account_id]].add(md["target_client_order_id"])
                rows_closed = all(m.status == "closed" and m.current_quantity == 0 for m in managed)
                filled_episode_ids = set(session.scalars(select(Fill.order_id).where(
                    Fill.order_id.in_([order.id for order in episode_buys]))))
                filled_owners_closed = all(any(m.entry_order_id == order.id
                    and m.broker_account_name == ids[order.broker_account_id]
                    and m.status == "closed" and m.current_quantity == 0 for m in managed)
                    for order in episode_buys if order.status == "filled" or order.id in filled_episode_ids)
                observed_ms = int(observed.timestamp() * 1000)
                start_ms = opportunity_start_ms(session, request.symbol,
                    str(request.opportunity_id), observed_ms) if request.opportunity_id > 0 else 0
                process_ids = {}
                for account in request.account_names:
                    receipts = [i for i in intents if ids.get(i.broker_account_id) == account
                        and i.intent_type == "cancel" and i.status in TERMINAL
                        and (i.payload or {}).get("source_service") == "schwab-1m-v2"
                        and (i.payload or {}).get("metadata", {}).get("clearwait_removal_token") == request.token]
                    stamps = set()
                    for receipt in receipts:
                        md = receipt.payload.get("metadata", {})
                        if (md.get("clearwait_opportunity_id") != str(request.opportunity_id)
                                or md.get("clearwait_buy_only") != "true"
                                or md.get("reason") != ("false_flip_restore" if request.purpose == "false_flip_restore"
                                    else "retry_budget_exhausted" if request.purpose == "retry_exhausted"
                                    else "watchlist-removed")
                                or (request.opportunity_id > 0
                                    and md.get("fanout_segment_id") != str(request.opportunity_id))
                                or int(_utc(receipt.created_at).timestamp() * 1000) < request.requested_at_ms
                                or int(_utc(receipt.updated_at).timestamp() * 1000) < request.requested_at_ms):
                            stamps.add(None)
                            continue
                        try:
                            stamps.add(str(UUID(md["buy_submission_process_id"])))
                        except (KeyError, ValueError, TypeError, AttributeError):
                            stamps.add(None)
                    if len(stamps) == 1 and None not in stamps:
                        process_ids[account] = stamps.pop()
                scope = UnboundCancelRequest(request.symbol, request.token, request.token,
                    str(request.opportunity_id), request.purpose, request.requested_at_ms, binding,
                    account_providers={a.name: a.provider for a in accounts},
                    session_key=current_session_anchor(observed).isoformat(),
                    opportunity_started_at_ms=start_ms, coverage_process_ids=process_ids)
                request_books = (books if books is not None else
                    load_unbound_request_working_books(session, intents, scope, now_ms=observed_ms))
                assessment_book_current = (type(minimum_book_started_at_ms) is int
                    and 0 <= minimum_book_started_at_ms <= observed_ms
                    and all(book is not None and book.started_at_ms >= minimum_book_started_at_ms
                        for name, book in request_books.items() if scope.account_providers.get(name) == "webull"))
                current = (latest is not None and active_request_matches(latest.payload, request)
                           and (publication_current is None or publication_current(request)))
                safe = (exact_accounts and bounded and not pending and not orders
                        and publication_closed.get(request) is True
                        and not unanswered and rows_closed and filled_owners_closed and current
                        and set(process_ids) == set(request.account_names) and start_ms > 0)
                never_sent = {}
                terminal_submissions = {}
                if safe and start_ms:
                    for name in sorted(process_ids):
                        admission_scope = NeverSentScope(
                            name, binding[name], request.symbol, str(request.opportunity_id), start_ms,
                            request.token, request.token, request.requested_at_ms,
                            UUID(process_ids[name]), scope.session_key)
                        witness = close_never_sent_admission(session, admission_scope, observed_at_ms=observed_ms)
                        if witness.never_sent:
                            never_sent[name] = witness
                        else:
                            terminal_submissions[name] = close_terminal_buy_admission(
                                session, admission_scope, observed_at_ms=observed_ms)
                terminal_ids = {name: set(w.client_order_ids) for name, w in terminal_submissions.items()
                    if isinstance(w, TerminalSubmissionWitness) and w.terminal is True
                    and w.reason == "broker_terminal_durable_admission_closed"}
                terminal_orders = session.scalars(select(BrokerOrder).where(
                    BrokerOrder.broker_account_id.in_(ids), BrokerOrder.symbol == request.symbol,
                    BrokerOrder.side == "buy", BrokerOrder.client_order_id.in_(
                        {coid for coids in terminal_ids.values() for coid in coids}),
                ).limit(ROW_LIMIT + 1)).all()
                terminal_orders_covered = all(coids == {order.client_order_id for order in terminal_orders
                    if ids[order.broker_account_id] == name} for name, coids in terminal_ids.items())
                terminal_owned = session.scalars(select(OmsManagedPosition).where(
                    OmsManagedPosition.broker_account_name.in_(account_names),
                    OmsManagedPosition.strategy_code == "schwab_1m_v2",
                    OmsManagedPosition.symbol == request.symbol,
                    OmsManagedPosition.entry_order_id.in_([order.id for order in terminal_orders]),
                ).limit(ROW_LIMIT + 1)).all()
                managed = list({row.id: row for row in (*managed, *terminal_owned)}.values())
                terminal_fills = set(session.scalars(select(Fill.order_id).where(
                    Fill.order_id.in_([order.id for order in terminal_orders]))))
                terminal_owners_closed = (all(row.status == "closed" and row.current_quantity == 0
                    for row in terminal_owned) and all(any(row.entry_order_id == order.id
                        and row.broker_account_name == ids[order.broker_account_id] for row in terminal_owned)
                    for order in terminal_orders if order.status == "filled" or order.id in terminal_fills))
                bounded = bounded and len(terminal_orders) <= ROW_LIMIT and len(managed) <= ROW_LIMIT
                episode_covered = all(order.client_order_id
                    and order.client_order_id in terminal_ids.get(ids[order.broker_account_id], set())
                    for order in episode_buys)
                targets_covered = all(targets <= terminal_ids.get(name, set())
                                      for name, targets in bound_targets.items())
                proof = evaluate_unbound_cancel_terminal(scope, request_books,
                    fences=UnboundCancelFences(scope,
                        exact_accounts and bounded and not pending and not orders
                        and publication_closed.get(request) is True
                        and set(process_ids) == set(request.account_names) and start_ms > 0
                        and assessment_book_current,
                        not unanswered and targets_covered and episode_covered and terminal_orders_covered,
                        rows_closed and filled_owners_closed and terminal_owners_closed,
                        current),
                    never_sent_witnesses=never_sent, terminal_submission_witnesses=terminal_submissions,
                    now_ms=observed_ms)
                witnesses = []
                for row in managed:
                    entry = session.get(BrokerOrder, row.entry_order_id) if row.entry_order_id else None
                    md = (entry.payload or {}).get("metadata", entry.payload or {}) if entry else {}
                    if (entry is not None and entry.symbol == request.symbol
                            and ids.get(entry.broker_account_id) == row.broker_account_name
                            and (str(md.get("fanout_segment_id")) == str(request.opportunity_id)
                                or entry.client_order_id in terminal_ids.get(row.broker_account_name, set()))
                            and request.opportunity_id > 0):
                        witnesses.append((row.broker_account_name, str(row.id)))
                witnesses = tuple(sorted(witnesses))
                if proof.terminal:
                    # The thread's memory fence and durable token must still match after proof work.
                    session.flush()
                    session.expire_all()
                    final = session.scalar(select(DashboardSnapshot).where(
                        DashboardSnapshot.snapshot_type == SNAPSHOT_TYPE,
                        DashboardSnapshot.payload["symbol"].as_string() == request.symbol,
                    ).order_by(DashboardSnapshot.created_at.desc(), DashboardSnapshot.id.desc()).limit(1))
                    if (final is None or not active_request_matches(final.payload, request)
                            or (publication_current is not None and not publication_current(request))):
                        proof = evaluate_unbound_cancel_terminal(scope, request_books,
                            fences=UnboundCancelFences(scope, False, False, False, False), now_ms=observed_ms)
                if proof.terminal:
                    book_observed_ms = min(book.started_at_ms for name, book in request_books.items()
                        if scope.account_providers.get(name) == "webull" and book is not None)
                    session.add(DashboardSnapshot(snapshot_type=SNAPSHOT_TYPE,
                        payload={**request.payload(active=request.purpose in {"retry_exhausted", "false_flip_restore"}), "verdict": "TERMINAL",
                            "reason": proof.reason, "closed_owned_rows": [list(w) for w in witnesses],
                            "terminal_observed_at_ms": book_observed_ms},
                        created_at=max(observed, _utc(latest.created_at) + timedelta(microseconds=1))))
                    session.commit()
                else:
                    session.rollback()
                results.append(RemovedWaitProof(request, book_observed_ms if proof.terminal else proof.observed_at_ms, proof.terminal,
                                                proof.reason, witnesses if proof.terminal else ()))
        return tuple(results)

    def request_assessment_receipts(self, request: RemovedWait, *,
        expected_bindings: Mapping[str, tuple[str, str]], now: datetime,
    ) -> tuple[dict, ...] | None:
        """Exact committed receipt revisions for an explicit non-trading OMS signal."""
        with self.session_factory() as session:
            latest = session.scalar(select(DashboardSnapshot).where(
                DashboardSnapshot.snapshot_type == SNAPSHOT_TYPE,
                DashboardSnapshot.payload["symbol"].as_string() == request.symbol,
            ).order_by(DashboardSnapshot.created_at.desc(), DashboardSnapshot.id.desc()).limit(1))
            if latest is None or not active_request_matches(latest.payload, request):
                return None
            start_ms = opportunity_start_ms(session, request.symbol, str(request.opportunity_id),
                                             int(now.timestamp() * 1000)) if request.opportunity_id > 0 else 0
            if not start_ms:
                return None
            accounts = session.scalars(select(BrokerAccount).where(
                BrokerAccount.name.in_(request.account_names))).all()
            if (len(accounts) != len(request.account_names)
                    or set(expected_bindings) != set(request.account_names)):
                return None
            receipts = []
            for account in sorted(accounts, key=lambda a: a.name):
                provider, account_id = expected_bindings[account.name]
                if (account.provider != provider or not account_id
                        or account.external_account_id not in {None, account_id}):
                    return None
                rows = session.scalars(select(TradeIntent).where(
                    TradeIntent.broker_account_id == account.id, TradeIntent.symbol == request.symbol,
                    TradeIntent.intent_type == "cancel",
                    TradeIntent.payload["metadata"]["clearwait_removal_token"].as_string() == request.token,
                ).order_by(TradeIntent.updated_at.desc(), TradeIntent.created_at.desc(),
                           TradeIntent.id.desc()).limit(ROW_LIMIT + 1)).all()
                if not rows or len(rows) > ROW_LIMIT:
                    return None
                row = rows[0]
                md = (row.payload or {}).get("metadata", {})
                reason = ("retry_budget_exhausted" if request.purpose == "retry_exhausted" else
                          "false_flip_restore" if request.purpose == "false_flip_restore" else "watchlist-removed")
                if (any(r.status not in TERMINAL for r in rows) or row.side != "buy"
                        or (row.payload or {}).get("source_service") != "schwab-1m-v2"
                        or md.get("clearwait_purpose") != request.purpose
                        or md.get("clearwait_opportunity_id") != str(request.opportunity_id)
                        or md.get("fanout_segment_id") != str(request.opportunity_id)
                        or md.get("clearwait_buy_only") != "true" or md.get("reason") != reason
                        or int(_utc(row.created_at).timestamp() * 1000) < request.requested_at_ms
                        or not request.requested_at_ms <= int(_utc(row.updated_at).timestamp() * 1000)
                            <= int(now.timestamp() * 1000)):
                    return None
                try:
                    event_id = str(UUID(row.payload["event_id"]))
                    process_id = str(UUID(md["buy_submission_process_id"]))
                except (KeyError, ValueError, TypeError, AttributeError):
                    return None
                coverage = session.get(BuyCoverageEpoch, (UUID(process_id), account_id))
                if (coverage is None or coverage.protocol != PROTOCOL
                        or coverage.account_name != account.name
                        or not 0 < coverage.started_at_ms <= start_ms <= request.requested_at_ms):
                    return None
                receipts.append({"intent_id": str(row.id), "event_id": event_id,
                    "account_name": account.name, "account_id": account_id, "provider": provider,
                    "created_at": _utc(row.created_at).isoformat(),
                    "updated_at": _utc(row.updated_at).isoformat(), "status": row.status,
                    "coverage_process_id": process_id})
            return tuple(receipts)

    def request_publication_closed(
        self, request: RemovedWait, *, last_publications: Mapping[str, Sequence[str]],
        no_dispatch: bool = False, now: datetime | None = None,
        include_revision: bool = False,
    ) -> bool | tuple[bool, str]:
        """Existing serial request barriers, not the OMS cursor or an empty intent table."""
        observed = now or datetime.now(UTC)
        publications = {event: account for account, events in last_publications.items() for event in events}
        if (not set(last_publications) <= set(request.account_names)
                or len(publications) != sum(map(len, last_publications.values()))):
            return False
        with self.session_factory() as session:
            accounts = session.scalars(select(BrokerAccount).where(
                BrokerAccount.name.in_(request.account_names))).all()
            names = {a.id: a.name for a in accounts}
            if len(accounts) != len(request.account_names) or set(names.values()) != set(request.account_names):
                return False
            rows = session.scalars(select(TradeIntent).where(
                TradeIntent.broker_account_id.in_(names), TradeIntent.symbol == request.symbol,
                TradeIntent.side == "buy",
                or_(TradeIntent.payload["metadata"]["clearwait_removal_token"].as_string() == request.token,
                    TradeIntent.payload["event_id"].as_string().in_(tuple(publications))),
            ).limit(ROW_LIMIT + 1)).all()
            if len(rows) > ROW_LIMIT:
                return False
            seen = set()
            receipts = set()
            for intent in rows:
                payload = intent.payload or {}
                md = payload.get("metadata", {})
                account = names[intent.broker_account_id]
                if (intent.status not in TERMINAL | {"filled", "aborted"}
                        or payload.get("source_service") != "schwab-1m-v2"):
                    return False
                event = payload.get("event_id")
                if event in publications:
                    if publications[event] != account:
                        return False
                    seen.add(event)
                if md.get("clearwait_removal_token") == request.token:
                    if (intent.intent_type != "cancel" or intent.status not in TERMINAL
                            or md.get("clearwait_buy_only") != "true"
                            or md.get("clearwait_opportunity_id") != str(request.opportunity_id)
                            or md.get("reason") != ("false_flip_restore" if request.purpose == "false_flip_restore"
                                else "retry_budget_exhausted" if request.purpose == "retry_exhausted"
                                else "watchlist-removed")
                            or (request.opportunity_id > 0 and md.get("fanout_segment_id") != str(request.opportunity_id))
                            or int(_utc(intent.created_at).timestamp() * 1000) < request.requested_at_ms
                            or int(_utc(intent.updated_at).timestamp() * 1000) < request.requested_at_ms):
                        return False
                    receipts.add(account)
            if seen != set(publications):
                return False
            if receipts == set(request.account_names):
                if include_revision:
                    # Journal writes preserve receipt time, so observe the actual payload revision.
                    exact = sorted((str(i.id), i.status, _utc(i.updated_at).isoformat(), i.payload)
                        for i in rows if (i.payload or {}).get("metadata", {}).get(
                            "clearwait_removal_token") == request.token)
                    revision = hashlib.sha256(json.dumps(exact, sort_keys=True,
                        separators=(",", ":")).encode("utf-8")).hexdigest()
                    return True, revision
                return True
            if not no_dispatch or publications or receipts or request.opportunity_id != 0:
                return False
            # A fresh zero-id raise may avoid spurious cancel publication, but historical
            # current-session opens/orders/dispatch journals are positive disqualifiers.
            anchor = current_session_anchor(observed)
            if session.scalar(select(TradeIntent.id).where(
                TradeIntent.broker_account_id.in_(names), TradeIntent.symbol == request.symbol,
                TradeIntent.side == "buy", TradeIntent.created_at >= anchor).limit(1)) is not None:
                return False
            if session.scalar(select(BrokerOrder.id).where(
                BrokerOrder.broker_account_id.in_(names), BrokerOrder.symbol == request.symbol,
                BrokerOrder.side == "buy",
                or_(BrokerOrder.submitted_at.is_(None), BrokerOrder.submitted_at >= anchor)).limit(1)) is not None:
                return False
            return session.scalar(select(DashboardSnapshot.id).where(
                DashboardSnapshot.snapshot_type == DISPATCH_SNAPSHOT_TYPE,
                DashboardSnapshot.payload["symbol"].as_string() == request.symbol,
                DashboardSnapshot.created_at >= anchor).limit(1)) is None

    def restore_terminal_proofs(self) -> tuple[RemovedWaitProof, ...]:
        """Restore only the latest typed witness, retaining its actual observation time."""
        with self.session_factory() as session:
            latest = select(DashboardSnapshot.id, func.row_number().over(
                partition_by=DashboardSnapshot.payload["symbol"].as_string(),
                order_by=(DashboardSnapshot.created_at.desc(), DashboardSnapshot.id.desc()),
            ).label("rank")).where(DashboardSnapshot.snapshot_type == SNAPSHOT_TYPE).subquery()
            rows = session.scalars(select(DashboardSnapshot).where(DashboardSnapshot.id.in_(
                select(latest.c.id).where(latest.c.rank == 1))).limit(ROW_LIMIT + 1)).all()
            if len(rows) > ROW_LIMIT:
                raise ValueError("terminal witness restore exceeded bounded evidence")
            results = []
            for row in rows:
                p = row.payload
                if not isinstance(p, dict) or p.get("reason") != "unbound_symbol_terminal":
                    continue
                raw = p.get("closed_owned_rows")
                if (p.get("verdict") != "TERMINAL" or p.get("schema_version") != 1
                        or p.get("strategy_code") != "schwab_1m_v2"
                        or not isinstance(p.get("active"), bool) or not isinstance(raw, list)
                        or not isinstance(p.get("account_names"), list)):
                    raise ValueError("terminal witness unreadable")
                request = RemovedWait(p["symbol"], int(p["opportunity_id"]), p["token"],
                    int(p["requested_at_ms"]), tuple(p["account_names"]), p.get("purpose", "scanner_removal"))
                witnesses = tuple(tuple(pair) for pair in raw)
                if (len(set(witnesses)) != len(witnesses) or any(len(pair) != 2
                        or pair[0] not in request.account_names or not isinstance(pair[1], str)
                        or not pair[1] for pair in witnesses)):
                    raise ValueError("terminal witness rows unreadable")
                observed_ms = p.get("terminal_observed_at_ms")
                if (type(observed_ms) is not int or observed_ms < request.requested_at_ms
                        or observed_ms > int(_utc(row.created_at).timestamp() * 1000)):
                    raise ValueError("terminal witness observation unreadable")
                results.append(RemovedWaitProof(request, observed_ms,
                    True, "unbound_symbol_terminal", witnesses))
        return tuple(results)

    def retire_prior_sessions(
        self, requests: Sequence[RemovedWait], account_names: set[str], *,
        now: datetime | None = None,
    ) -> tuple[RemovedWaitProof, ...]:
        """Off-loop, bounded CAS retirement. Never retire an unbound/zero identity.

        The caller holds the in-memory gate closed throughout this transaction.
        All-account symbol blockers intentionally ignore generation/strategy.
        Retry journals require typed terminal evidence, never absence by age.
        """
        observed = now or datetime.now(UTC)
        started = monotonic()
        prior = tuple(r for r in requests if prior_session_request(r, observed))
        try:
            canonical = {p.request: p for p in self.proofs(prior, account_names, now=observed)} if prior else {}
        except Exception:  # noqa: BLE001
            canonical = {}
        results = []
        for request in requests:
            reason = "same_session_or_unknown_opportunity"
            clear = False
            if prior_session_request(request, observed):
                with self.session_factory() as session:
                    self._lock_request(session, request.symbol)
                    accounts = session.scalars(select(BrokerAccount).where(
                        BrokerAccount.name.in_(account_names))).all()
                    ids = [a.id for a in accounts]
                    latest = session.scalar(select(DashboardSnapshot).where(
                        DashboardSnapshot.snapshot_type == SNAPSHOT_TYPE,
                        DashboardSnapshot.payload["symbol"].as_string() == request.symbol,
                    ).order_by(DashboardSnapshot.created_at.desc(), DashboardSnapshot.id.desc())
                      .limit(1).with_for_update())
                    if (not account_names or len(accounts) != len(account_names)
                            or {a.name for a in accounts} != account_names
                            or len(request.account_names) != len(account_names)
                            or set(request.account_names) != account_names):
                        reason = "account_binding_unknown"
                    elif latest is None or not active_request_matches(latest.payload, request):
                        reason = "active_request_changed"
                    elif session.scalar(select(BrokerOrder.id).where(
                        BrokerOrder.broker_account_id.in_(ids),
                        BrokerOrder.symbol == request.symbol,
                        or_(BrokerOrder.status.is_(None),
                            BrokerOrder.status.not_in(TERMINAL | {"filled"})),
                    ).limit(1)) is not None:
                        reason = "working_order"
                    elif session.scalar(select(OmsManagedPosition.id).where(
                        OmsManagedPosition.broker_account_name.in_(account_names),
                        OmsManagedPosition.symbol == request.symbol,
                        OmsManagedPosition.status == "open",
                    ).limit(1)) is not None:
                        reason = "open_managed_row"
                    elif session.scalar(select(TradeIntent.id).where(
                        TradeIntent.broker_account_id.in_(ids),
                        TradeIntent.symbol == request.symbol,
                        TradeIntent.side == "buy",
                        or_(TradeIntent.status.is_(None),
                            TradeIntent.status.not_in(TERMINAL | {"filled", "aborted"})),
                    ).limit(1)) is not None:
                        reason = "pending_buy_intent"
                    elif self._rollover_retry_unknown(session, request.symbol, account_names):
                        reason = "retry_proof_unknown"
                    elif request not in canonical or not canonical[request].clear:
                        # Empty OMS rows cannot prove an unpublished/queued cancel absent.
                        reason = canonical[request].reason if request in canonical else "canonical_proof_unknown"
                    elif (monotonic() - started) * 1000 > SETTLE_MS:
                        reason = "stale_proof"
                    else:
                        session.add(DashboardSnapshot(snapshot_type=SNAPSHOT_TYPE,
                            payload={**request.payload(active=False), "verdict": "CLEAR",
                                     "reason": "session_rollover"}, created_at=observed))
                        session.commit()
                        clear, reason = True, "session_rollover"
            results.append(RemovedWaitProof(request, int(observed.timestamp() * 1000), clear, reason))
        return tuple(results)

    def restore(self) -> dict[str, RemovedWait]:
        with self.session_factory() as session:
            latest_ids = (
                select(
                    DashboardSnapshot.id,
                    func.row_number()
                    .over(
                        partition_by=DashboardSnapshot.payload["symbol"].as_string(),
                        order_by=(DashboardSnapshot.created_at.desc(), DashboardSnapshot.id.desc()),
                    )
                    .label("rank"),
                )
                .where(DashboardSnapshot.snapshot_type == SNAPSHOT_TYPE)
                .subquery()
            )
            rows = session.scalars(
                select(DashboardSnapshot)
                .where(
                    DashboardSnapshot.id.in_(select(latest_ids.c.id).where(latest_ids.c.rank == 1)),
                )
                .order_by(DashboardSnapshot.created_at.desc(), DashboardSnapshot.id.desc())
                .limit(ROW_LIMIT + 1)
            ).all()
        if len(rows) > ROW_LIMIT:
            raise ValueError("removal restore exceeded bounded evidence")
        latest: dict[str, RemovedWait] = {}
        seen: set[str] = set()
        for row in rows:
            p = row.payload
            if (
                not isinstance(p, dict)
                or p.get("schema_version") != 1
                or p.get("strategy_code") != "schwab_1m_v2"
            ):
                raise ValueError("unreadable removal snapshot")
            symbol = str(p.get("symbol", "")).strip().upper()
            if not symbol or not isinstance(p.get("active"), bool):
                raise ValueError("invalid removal snapshot")
            if symbol in seen:
                continue
            seen.add(symbol)
            raw_accounts = p.get("account_names")
            if (
                not isinstance(raw_accounts, list)
                or not raw_accounts
                or not all(isinstance(a, str) and a for a in raw_accounts)
                or len(set(raw_accounts)) != len(raw_accounts)
            ):
                raise ValueError("invalid removal accounts")
            request = RemovedWait(
                symbol,
                int(p["opportunity_id"]),
                str(p["token"]),
                int(p["requested_at_ms"]),
                tuple(raw_accounts),
                str(p.get("purpose", "scanner_removal")),
            )
            if request.purpose not in {"scanner_removal", "retry_exhausted", "false_flip_restore"}:
                raise ValueError("invalid cancellation purpose")
            if request.opportunity_id < 0 or not request.token or request.requested_at_ms <= 0:
                raise ValueError("invalid removal identity")
            if p["active"]:
                latest[symbol] = request
        return latest

    def proofs(
        self,
        requests: Sequence[RemovedWait],
        account_names: set[str],
        *,
        now: datetime | None = None,
    ) -> tuple[RemovedWaitProof, ...]:
        observed = now or datetime.now(UTC)
        results: list[RemovedWaitProof] = []
        with self.session_factory() as session:
            strategy_id = session.scalar(select(Strategy.id).where(Strategy.code == "schwab_1m_v2"))
            account_rows = {a.id: a for a in session.scalars(
                select(BrokerAccount).where(BrokerAccount.name.in_(account_names))).all()}
            accounts = {ident: row.name for ident, row in account_rows.items()}
            if strategy_id is None or set(accounts.values()) != account_names:
                raise ValueError("required removal accounts unavailable")
            for request in requests:
                if set(request.account_names) != account_names:
                    raise ValueError("removal account configuration changed")
                anchor = min(
                    current_session_anchor(observed),
                    datetime.fromtimestamp(
                        (request.opportunity_id or request.requested_at_ms) / 1000, UTC
                    ),
                )

                def bounded(query: object) -> list:
                    rows = list(session.scalars(query.limit(ROW_LIMIT + 1)).all())
                    if len(rows) > ROW_LIMIT:
                        raise ValueError("removal proof exceeded bounded evidence")
                    return rows

                intents = bounded(
                    select(TradeIntent).where(
                        TradeIntent.strategy_id == strategy_id,
                        TradeIntent.symbol == request.symbol,
                        TradeIntent.side == "buy",
                        or_(
                            TradeIntent.created_at >= anchor,
                            TradeIntent.status.not_in(TERMINAL | {"filled"}),
                        ),
                    )
                )
                orders = bounded(
                    select(BrokerOrder).where(
                        BrokerOrder.strategy_id == strategy_id,
                        BrokerOrder.symbol == request.symbol,
                        BrokerOrder.side == "buy",
                        or_(
                            BrokerOrder.submitted_at >= anchor,
                            BrokerOrder.status.not_in(TERMINAL | {"filled"}),
                            BrokerOrder.intent_id.in_([i.id for i in intents]),
                        ),
                    )
                )
                fills = set(
                    session.scalars(
                        select(Fill.order_id).where(Fill.order_id.in_([o.id for o in orders]))
                    ).all()
                )
                order_events = bounded(
                    select(BrokerOrderEvent).where(
                        BrokerOrderEvent.order_id.in_([o.id for o in orders]),
                    )
                )
                snapshots = bounded(
                    select(DashboardSnapshot)
                    .where(
                        DashboardSnapshot.snapshot_type.in_(
                            [
                                SNAPSHOT_TYPE,
                                DISPATCH_SNAPSHOT_TYPE,
                                "v2_flip_entry_ownership",
                                "atr_reprice_handoff",
                                "oms_webull_mirror_price_hold",
                            ]
                        ),
                        DashboardSnapshot.created_at >= anchor,
                        DashboardSnapshot.payload["symbol"].as_string() == request.symbol,
                    )
                    .order_by(DashboardSnapshot.created_at, DashboardSnapshot.id)
                )
                # RPG puts the symbol inside old, not at the top level.
                snapshots += bounded(
                    select(DashboardSnapshot)
                    .where(
                        DashboardSnapshot.snapshot_type == "atr_reprice_handoff",
                        DashboardSnapshot.payload["old"]["symbol"].as_string() == request.symbol,
                        DashboardSnapshot.created_at >= anchor,
                    )
                    .order_by(DashboardSnapshot.created_at, DashboardSnapshot.id)
                )
                snapshots += bounded(
                    select(DashboardSnapshot)
                    .where(
                        DashboardSnapshot.snapshot_type == "oms_webull_mirror_price_hold",
                        DashboardSnapshot.payload["event"]["payload"]["symbol"].as_string()
                        == request.symbol,
                        DashboardSnapshot.created_at >= anchor,
                    )
                    .order_by(DashboardSnapshot.created_at, DashboardSnapshot.id)
                )
                position_queries = [
                    select(AccountPosition.id).where(
                        AccountPosition.symbol == request.symbol,
                        AccountPosition.broker_account_id.in_(accounts),
                        AccountPosition.quantity != 0,
                    ),
                    select(VirtualPosition.id).where(
                        VirtualPosition.symbol == request.symbol,
                        VirtualPosition.strategy_id == strategy_id,
                        VirtualPosition.quantity != 0,
                    ),
                    select(OmsManagedPosition.id).where(
                        OmsManagedPosition.symbol == request.symbol,
                        OmsManagedPosition.strategy_code == "schwab_1m_v2",
                        OmsManagedPosition.status == "open",
                    ),
                ]
                has_position = any(session.scalar(q.limit(1)) is not None for q in position_queries)
                evidence = load_cancel_terminal_evidence(session, intents)
                cancel_proofs = {}
                for intent in intents:
                    md = (intent.payload or {}).get("metadata", {})
                    if not isinstance(md, dict) or md.get("clearwait_removal_token") != request.token:
                        continue
                    account = account_rows.get(intent.broker_account_id)
                    receipt = receipt_from_intent(intent, account) if account is not None else None
                    cancel_proofs[intent.id] = evaluate_cancel_terminal(receipt,
                        evidence.get(receipt.scope.event_id), now_ms=int(observed.timestamp() * 1000)) if receipt else None
                results.append(
                    assess_removed_wait(
                        request,
                        now=observed,
                        accounts=accounts,
                        intents=intents,
                        orders=orders,
                        filled_order_ids=fills,
                        order_events=order_events,
                        snapshots=snapshots,
                        has_position=has_position,
                        cancel_terminal_proofs=cancel_proofs,
                    )
                )
        return tuple(results)


RemovalPersist = Callable[[RemovedWait, bool], None]
