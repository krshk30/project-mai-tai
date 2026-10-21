"""Exact-episode, fail-closed evidence for scanner removal of an unfilled BUY."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Callable, Mapping, Sequence

from sqlalchemy import func, or_, select, text
from sqlalchemy.orm import Session, sessionmaker

from project_mai_tai.db.models import (
    BrokerAccount,
    BrokerOrder,
    BrokerOrderEvent,
    DashboardSnapshot,
    Fill,
    Strategy,
    SystemIncident,
    TradeIntent,
)
from project_mai_tai.fanout_identity import fanout_slot_id
from project_mai_tai.fanout_segment_store import current_session_anchor
from project_mai_tai.oms.atr_reprice_handoff import old_buy_proven_clear
from project_mai_tai.owned_inventory import owned_position_present

SNAPSHOT_TYPE = "v2_removed_wait"
DISPATCH_SNAPSHOT_TYPE = "v2_wait_dispatch"
SETTLE_MS = 15_000
ROW_LIMIT = 2048
TERMINAL = frozenset({"cancelled", "canceled", "rejected", "expired"})
ORDER_TERMINAL = TERMINAL | {"filled", "replaced"}
INTENT_TERMINAL = TERMINAL | {"filled", "aborted"}


class RemovedWaitPublicationChanged(ValueError):
    """The publication's supplied memory fence failed, not a storage failure."""


def previous_session_request(request: RemovedWait, now: datetime) -> bool:
    identity_ms = request.opportunity_id or request.requested_at_ms
    return 0 < identity_ms < int(current_session_anchor(now).timestamp() * 1000)


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


@dataclass(frozen=True)
class RemovedWaitClear:
    proof: RemovedWaitProof
    retirements: tuple[tuple[str, dict], ...]
    memory_fence: tuple
    validate: Callable[[], bool] | None = field(default=None, compare=False, repr=False)


def request_only_retirement(proof: RemovedWaitProof) -> bool:
    """End removal bookkeeping without granting an unfilled/owner-clear permission."""
    return (not proof.clear and proof.reason == "own_fill_stays_owned"
            and proof.request.purpose == "scanner_removal")



def assess_clear_rules(
    request: RemovedWait, *, now: datetime, accounts: Mapping[object, str],
    intents: Sequence[TradeIntent], orders: Sequence[BrokerOrder],
    filled_order_ids: set[object], order_events: Sequence[BrokerOrderEvent],
    snapshots: Sequence[DashboardSnapshot], has_position: bool, strategy_id: object,
) -> RemovedWaitProof:
    """L1 rules over a fresh shared-account snapshot; no dispatch journal prerequisite."""
    observed = int(now.timestamp() * 1000)
    cutoff = _utc(now) - timedelta(milliseconds=SETTLE_MS)

    def result(reason: str, clear: bool = False) -> RemovedWaitProof:
        return RemovedWaitProof(request, observed, clear, reason)

    def metadata(row: object) -> dict:
        p = getattr(row, "payload", None)
        if not isinstance(p, dict):
            return {}
        md = p.get("metadata", p)
        return md if isinstance(md, dict) else {}

    def own(row: object) -> bool:
        return (getattr(row, "strategy_id", None) == strategy_id
                and (request.opportunity_id == 0 or str(metadata(row).get("fanout_segment_id", ""))
                     == str(request.opportunity_id)))

    if (len(accounts) != 2 or len(set(accounts.values())) != 2 or not request.account_names
            or len(set(request.account_names)) != len(request.account_names)
            or not set(request.account_names) <= set(accounts.values())):
        return result("accounts_unreadable")
    requests = [r for r in snapshots if r.snapshot_type == SNAPSHOT_TYPE]
    if not requests or requests[-1].payload != request.payload(active=True):
        return result("active_request_changed")
    if request.purpose not in {"scanner_removal", "retry_exhausted", "false_flip_restore"}:
        return result("purpose_unreadable")
    if has_position:
        return result("position_stays_managed")
    prior = previous_session_request(request, now)
    broker_observed_ids = {event.order_id for event in order_events if event.event_source == "broker"}
    for order in orders:
        # A settled client refusal never placed a wire order; an id or any
        # broker-sourced event retains the existing terminal-evidence rules.
        client_terminal = (order.status in {"aborted", "rejected"}
                           and order.broker_order_id is None and order.id not in broker_observed_ids)
        if ((prior or str(order.side).lower() == "buy")
                and order.status not in ORDER_TERMINAL and not client_terminal):
            return result("working_order" if prior else "opening_order_not_terminal")
    if any(i.intent_type == "open"
           and i.status not in INTENT_TERMINAL for i in intents):
        return result("intent_not_terminal")
    if observed - request.requested_at_ms < SETTLE_MS:
        return result("request_settling")
    for row in (*orders, *intents):
        if not isinstance(row.updated_at, datetime) or _utc(row.updated_at) > cutoff:
            return result("terminal_row_settling")
    # Only the latest broker outcome controls a cancelled ticket. A previous
    # unknown cannot overrule a later confirmation; absence cannot confirm unknown.
    latest_events: dict[object, BrokerOrderEvent] = {}
    for event in order_events:
        if (event.event_source == "broker" and event.event_type in TERMINAL
                and (event.order_id not in latest_events
                     or _utc(event.event_at) > _utc(latest_events[event.order_id].event_at))):
            latest_events[event.order_id] = event
    own_intents = {i.id for i in intents if i.intent_type == "open" and own(i)}
    own_orders = [o for o in orders if own(o) or o.intent_id in own_intents]
    for order in orders:
        event = latest_events.get(order.id)
        if event and (metadata(event).get("cancel_outcome") in {"could_not_tell", "not_confirmed"}
                      or _utc(event.event_at) > cutoff):
            return result("cancel_unknown_or_refused")
    for intent in intents:
        if metadata(intent).get("cancel_outcome") in {"could_not_tell", "not_confirmed"}:
            return result("cancel_unknown_or_refused")
        if metadata(intent).get("clearwait_removal_token") != request.token:
            continue
        p = intent.payload or {}
        no_target = (intent.status == "rejected" and p.get("refusal_origin") == "skipped_before_submit"
                     and p.get("refusal_code") == "cancel_target_not_found")
        if (intent.intent_type != "cancel" or intent.status not in {"cancelled", "canceled"}
                and not no_target or metadata(intent).get("cancel_outcome") in {"could_not_tell", "not_confirmed"}):
            return result("cancel_unknown_or_refused")
    if prior:
        return result("previous_session_flat", True)
    if request.purpose == "scanner_removal":
        own_ids = {o.id for o in own_orders}
        if (any(o.id in filled_order_ids or o.status in {"filled", "partially_filled"} for o in own_orders)
                or any(e.order_id in own_ids and (e.event_type in {"filled", "partially_filled"}
                       or (e.payload or {}).get("broker_fill_id")) for e in order_events)):
            return result("own_fill_stays_owned")
        for snapshot in snapshots:
            p = snapshot.payload or {}
            if (snapshot.snapshot_type == "v2_flip_entry_ownership"
                    and str(p.get("opportunity_id")) == str(request.opportunity_id)
                    and (p.get("fill_accounts") or p.get("position_ids")
                         or p.get("phase") in {"provisional", "bound", "consumed", "awaiting_close"})):
                return result("filled_or_ambiguous_owner")
        return result("scanner_removal_flat", True)
    # B1 is a cancellation receipt only. Historical filled siblings remain owned;
    # a fill on the cancelled ticket itself disqualifies that ticket's receipt.
    for order in own_orders:
        if order.status == "filled":
            continue
        if order.id in filled_order_ids or any(
            e.order_id == order.id and (e.event_type in {"filled", "partially_filled"}
                                       or (e.payload or {}).get("broker_fill_id")) for e in order_events
        ):
            return result("own_fill_stays_owned")
        event = latest_events.get(order.id)
        if not event or event.event_type not in {"cancelled", "canceled", "rejected", "expired"}:
            return result("broker_terminal_unproven")
        if metadata(event).get("cancel_outcome") in {"could_not_tell", "not_confirmed"}:
            return result("cancel_unknown_or_refused")
    receipts = {accounts[i.broker_account_id] for i in intents
                if metadata(i).get("clearwait_removal_token") == request.token
                and str(metadata(i).get("clearwait_opportunity_id")) == str(request.opportunity_id)
                and metadata(i).get("clearwait_buy_only") == "true"
                and metadata(i).get("reason") == ("false_flip_restore" if request.purpose == "false_flip_restore"
                                                  else "retry_budget_exhausted")
                and own(i) and _utc(i.created_at).timestamp() * 1000 >= request.requested_at_ms}
    if receipts != set(request.account_names):
        return result("waiting_for_all_cancel_receipts")
    return result("false_flip_leftovers_cancelled_owner_kept" if request.purpose == "false_flip_restore"
                  else "retry_leftovers_cancelled_owner_kept", True)


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


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
                   and r.payload == request.payload(active=True) for r in snapshots):
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
                    if not settled(order) or not (terminal or no_wire):
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
            if intent.status not in {"cancelled", "canceled"} and not no_target:
                return result("cancel_unknown_or_refused")
            if not settled(intent):
                return result("cancel_receipt_settling")
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
                persisted = p == request.payload(active=True)
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
            if not settled(order):
                return result("terminal_order_settling")
            status = "cancelled" if order.status == "canceled" else order.status
            intent = intents_by_id.get(order.intent_id)
            pre_wire = (
                not order.broker_order_id
                and status == "rejected"
                and intent is not None
                and no_wire_intent(intent)
            )
            if (order.id, status) not in broker_terminal and not pre_wire:
                return result("broker_terminal_unproven")
        if exact(md) or (
            request.opportunity_id == 0
            and order.submitted_at
            and _utc(order.submitted_at).timestamp() * 1000 >= request.requested_at_ms
        ):
            if order.id in filled_order_ids or str(order.status).lower() == "filled":
                return result("own_fill_stays_owned")
            if not settled(order):
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
        if status not in {"cancelled", "canceled"} and not no_target:
            return result("cancel_unknown_or_refused")
        if not settled(intent):
            return result("cancel_receipt_settling")
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

    def record_dispatch(self, payload: dict) -> None:
        with self.session_factory() as session:
            session.add(DashboardSnapshot(snapshot_type=DISPATCH_SNAPSHOT_TYPE, payload=payload))
            session.commit()

    def record_clear_failure(self, request: RemovedWait, attempts: int, error_class: str) -> None:
        with self.session_factory() as session:
            if session.bind.dialect.name == "postgresql":
                session.execute(text("SET LOCAL lock_timeout = '250ms'"))
                session.execute(text("SET LOCAL statement_timeout = '2s'"))
            title = f"RWLOOP1 removal request retirement failed: {request.symbol} {request.token}"
            incident = session.scalar(select(SystemIncident).where(SystemIncident.title == title))
            payload = {"source": "RWLOOP1", "symbol": request.symbol, "request_token": request.token,
                       "opportunity_id": request.opportunity_id, "attempts": attempts,
                       "max_attempts": 5, "error_class": error_class[:80], "owner_kept": True}
            if incident is None:
                session.add(SystemIncident(service_name="schwab-1m-v2", severity="warning", status="open",
                                           title=title, payload=payload))
            else:
                incident.payload = payload
            session.commit()

    def record(self, request: RemovedWait, active: bool) -> None:
        with self.session_factory() as session:
            session.add(
                DashboardSnapshot(
                    snapshot_type=SNAPSHOT_TYPE, payload=request.payload(active=active)
                )
            )
            session.commit()

    def record_rules(self, request: RemovedWait, active: bool,
                     valid: Callable[[], bool] | None = None) -> None:
        with self.session_factory() as session:
            if session.bind.dialect.name == "postgresql":
                session.execute(text("SET LOCAL lock_timeout = '250ms'"))
                session.execute(text("SET LOCAL statement_timeout = '2s'"))
                session.execute(select(func.pg_advisory_xact_lock(
                    func.hashtext("v2_removed_wait:" + request.symbol))))
            if valid is not None and not valid():
                raise RemovedWaitPublicationChanged("removal publication changed")
            if not active:
                latest = session.scalar(select(DashboardSnapshot).where(
                    DashboardSnapshot.snapshot_type == SNAPSHOT_TYPE,
                    DashboardSnapshot.payload["symbol"].as_string() == request.symbol,
                ).order_by(DashboardSnapshot.created_at.desc(), DashboardSnapshot.id.desc())
                  .limit(1).with_for_update())
                if latest is None or latest.payload != request.payload(active=True):
                    raise ValueError("active removal request changed")
            session.add(DashboardSnapshot(snapshot_type=SNAPSHOT_TYPE, payload=request.payload(active=active)))
            session.flush()
            if valid is not None and not valid():
                raise RemovedWaitPublicationChanged("removal publication changed before commit")
            session.commit()

    def commit_clear(self, plan: RemovedWaitClear, valid: Callable[[], bool]) -> None:
        """Off-loop atomic retirement; a changed request or memory fence rolls back."""
        request = plan.proof.request
        with self.session_factory() as session:
            if session.bind.dialect.name == "postgresql":
                session.execute(text("SET LOCAL lock_timeout = '250ms'"))
                session.execute(text("SET LOCAL statement_timeout = '2s'"))
                session.execute(select(func.pg_advisory_xact_lock(
                    func.hashtext("v2_removed_wait:" + request.symbol))))
            latest = session.scalar(select(DashboardSnapshot).where(
                DashboardSnapshot.snapshot_type == SNAPSHOT_TYPE,
                DashboardSnapshot.payload["symbol"].as_string() == request.symbol,
            ).order_by(DashboardSnapshot.created_at.desc(), DashboardSnapshot.id.desc())
              .limit(1).with_for_update())
            if latest is None or latest.payload != request.payload(active=True) or not valid():
                raise ValueError("active removal request or memory fence changed")
            for snapshot_type, payload in plan.retirements:
                session.add(DashboardSnapshot(snapshot_type=snapshot_type, payload=payload))
            session.add(DashboardSnapshot(snapshot_type=SNAPSHOT_TYPE, payload=request.payload(active=False)))
            session.flush()
            if not valid():
                raise ValueError("removal fence changed before commit")
            session.commit()

    def clear_rule_proofs(
        self, requests: Sequence[RemovedWait], account_names: set[str], *, now: datetime | None = None,
    ) -> tuple[RemovedWaitProof, ...]:
        observed = now or datetime.now(UTC)
        results = []
        with self.session_factory() as session:
            if session.bind.dialect.name == "postgresql":
                session.connection(execution_options={"isolation_level": "REPEATABLE READ"})
                session.execute(text("SET TRANSACTION READ ONLY"))
            strategy_id = session.scalar(select(Strategy.id).where(Strategy.code == "schwab_1m_v2"))
            accounts = {a.id: a.name for a in session.scalars(select(BrokerAccount).where(
                BrokerAccount.name.in_(account_names))).all()}
            if strategy_id is None or len(accounts) != 2 or set(accounts.values()) != account_names:
                raise ValueError("required removal accounts unavailable")

            def bounded(query: object) -> list:
                rows = list(session.scalars(query.limit(ROW_LIMIT + 1)).all())
                if len(rows) > ROW_LIMIT:
                    raise ValueError("removal proof exceeded bounded evidence")
                return rows

            for request in requests:
                if not set(request.account_names) <= account_names:
                    raise ValueError("removal account configuration changed")
                anchor = datetime.fromtimestamp((request.opportunity_id or request.requested_at_ms) / 1000, UTC)
                # Opportunity zero owns this session's terminal history, not unrelated old fills.
                zero_op_session = current_session_anchor(anchor)
                cutoff = observed - timedelta(milliseconds=SETTLE_MS)
                intents = bounded(select(TradeIntent).where(
                    TradeIntent.broker_account_id.in_(accounts), TradeIntent.symbol == request.symbol,
                    or_(TradeIntent.status.is_(None), TradeIntent.status.not_in(INTENT_TERMINAL),
                        TradeIntent.updated_at >= cutoff, TradeIntent.created_at >= anchor,
                        TradeIntent.payload["cancel_outcome"].as_string().in_(("could_not_tell", "not_confirmed")),
                        TradeIntent.payload["metadata"]["cancel_outcome"].as_string().in_(
                            ("could_not_tell", "not_confirmed")))))
                orders = bounded(select(BrokerOrder).where(
                    BrokerOrder.broker_account_id.in_(accounts), BrokerOrder.symbol == request.symbol,
                    or_(BrokerOrder.status.is_(None), BrokerOrder.status.not_in(ORDER_TERMINAL),
                        BrokerOrder.updated_at >= cutoff, BrokerOrder.submitted_at >= anchor,
                        (BrokerOrder.strategy_id == strategy_id) & (request.opportunity_id == 0)
                            & (BrokerOrder.submitted_at >= zero_op_session),
                        BrokerOrder.id.in_(select(BrokerOrderEvent.order_id).where(
                            BrokerOrderEvent.event_source == "broker",
                            or_(BrokerOrderEvent.payload["cancel_outcome"].as_string().in_(
                                    ("could_not_tell", "not_confirmed")),
                                BrokerOrderEvent.payload["metadata"]["cancel_outcome"].as_string().in_(
                                    ("could_not_tell", "not_confirmed"))))),
                        (BrokerOrder.strategy_id == strategy_id) & (
                            BrokerOrder.payload["fanout_segment_id"].as_string() == str(request.opportunity_id)),
                        BrokerOrder.intent_id.in_([i.id for i in intents]))))
                events = bounded(select(BrokerOrderEvent).where(
                    BrokerOrderEvent.order_id.in_([o.id for o in orders])))
                fills = set(session.scalars(select(Fill.order_id).where(
                    Fill.order_id.in_([o.id for o in orders]))).all())
                latest_request = session.scalar(select(DashboardSnapshot).where(
                    DashboardSnapshot.snapshot_type == SNAPSHOT_TYPE,
                    DashboardSnapshot.payload["symbol"].as_string() == request.symbol,
                ).order_by(DashboardSnapshot.created_at.desc(), DashboardSnapshot.id.desc()).limit(1))
                snapshots = bounded(select(DashboardSnapshot).where(
                    DashboardSnapshot.snapshot_type == "v2_flip_entry_ownership",
                    DashboardSnapshot.payload["symbol"].as_string() == request.symbol,
                    DashboardSnapshot.created_at >= anchor))
                if latest_request is not None:
                    snapshots.append(latest_request)
                try:
                    has_position = owned_position_present(session, symbol=request.symbol, accounts=accounts,
                        strategy_id=strategy_id, now=observed)
                except (ValueError, TypeError, ArithmeticError):
                    results.append(RemovedWaitProof(request, int(observed.timestamp() * 1000), False,
                                                    "own_position_evidence_UNKNOWN"))
                    continue
                results.append(assess_clear_rules(request, now=observed, accounts=accounts,
                    intents=intents, orders=orders, filled_order_ids=fills, order_events=events,
                    snapshots=snapshots, has_position=has_position, strategy_id=strategy_id))
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
            accounts = {
                a.id: a.name
                for a in session.scalars(
                    select(BrokerAccount).where(BrokerAccount.name.in_(account_names))
                ).all()
            }
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
                try:
                    has_position = owned_position_present(session, symbol=request.symbol, accounts=accounts,
                        strategy_id=strategy_id, now=observed)
                except (ValueError, TypeError, ArithmeticError):
                    results.append(RemovedWaitProof(request, int(observed.timestamp() * 1000), False,
                                                    "own_position_evidence_UNKNOWN"))
                    continue
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
                    )
                )
        return tuple(results)


RemovalPersist = Callable[[RemovedWait, bool], None]
