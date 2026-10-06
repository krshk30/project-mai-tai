"""Exact-episode, fail-closed evidence for scanner removal of an unfilled BUY."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Callable, Mapping, Sequence

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, sessionmaker

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
from project_mai_tai.oms.atr_reprice_handoff import old_buy_proven_clear

SNAPSHOT_TYPE = "v2_removed_wait"
DISPATCH_SNAPSHOT_TYPE = "v2_wait_dispatch"
SETTLE_MS = 15_000
ROW_LIMIT = 2048
TERMINAL = frozenset({"cancelled", "canceled", "rejected", "expired"})


@dataclass(frozen=True)
class RemovedWait:
    symbol: str
    opportunity_id: int
    token: str
    requested_at_ms: int
    account_names: tuple[str, ...]

    def payload(self, *, active: bool) -> dict:
        return {
            "schema_version": 1,
            "strategy_code": "schwab_1m_v2",
            "symbol": self.symbol,
            "opportunity_id": str(self.opportunity_id),
            "token": self.token,
            "requested_at_ms": str(self.requested_at_ms),
            "account_names": list(self.account_names),
            "active": active,
        }


@dataclass(frozen=True)
class RemovedWaitProof:
    request: RemovedWait
    observed_at_ms: int
    clear: bool
    reason: str


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

    def record(self, request: RemovedWait, active: bool) -> None:
        with self.session_factory() as session:
            session.add(
                DashboardSnapshot(
                    snapshot_type=SNAPSHOT_TYPE, payload=request.payload(active=active)
                )
            )
            session.commit()

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
            )
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
