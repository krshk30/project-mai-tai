"""FALSEFLIP2: off-loop owned-episode reads; no broker operation or entry override."""

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import BigInteger, cast, func, or_, select

from project_mai_tai.db.models import (
    AccountPosition, BrokerAccount, BrokerOrder, BrokerOrderEvent, DashboardSnapshot, Fill,
    OmsManagedPosition, Strategy, SystemIncident, TradeIntent, VirtualPosition,
)
from project_mai_tai.falseflip1 import Classification
from project_mai_tai.positions_read_receipt import load_receipts

TERMINAL = {"filled", "cancelled", "canceled", "rejected", "aborted", "expired", "replaced"}
MAX_AGE_MS = 15000


@dataclass(frozen=True)
class FalseFlipReleaseProof:
    symbol: str
    opportunity_id: int
    observed_at_ms: int
    complete: bool
    reason: str
    row_bindings: frozenset[tuple[str, str, str]] = frozenset()
    all_filled: bool = True


def terminal_report_proven(session, order, *, as_of_ms):
    event = session.scalar(select(BrokerOrderEvent).where(
        BrokerOrderEvent.order_id == order.id,
        BrokerOrderEvent.event_type == order.status,
        BrokerOrderEvent.event_source == "broker",
    ).order_by(BrokerOrderEvent.event_at.desc()).limit(1))
    payload = event.payload if event else {}
    metadata = (payload or {}).get("metadata", {})
    outcome = metadata.get("cancel_outcome") if isinstance(metadata, dict) else None
    return bool(event and (payload or {}).get("client_order_id") == order.client_order_id
                and 0 < at_ms(event.event_at) <= as_of_ms
                and outcome not in {"could_not_tell", "not_confirmed", "unconfirmed", "accepted"})


def at_ms(value):
    if value is None:
        return 0
    return int((value.replace(tzinfo=UTC) if value.tzinfo is None else value).timestamp() * 1000)


def read_release_proofs(session, rows, account_names, *, now=None):
    """Broker read times come from OMS receipts, never Fill/AccountPosition.updated_at."""
    now = now or datetime.now(UTC)
    observed = at_ms(now)
    groups = {}
    for row in rows:
        try:
            classification = Classification.from_payload(row.entry_classification or {})
        except (KeyError, TypeError, ValueError):
            continue
        if classification.kind == "FALSE_FLIP":
            key = (row.symbol, classification.identity.opportunity_id)
            groups.setdefault(key, []).append((row, classification))
    if not groups:
        return {}
    if len(groups) > 256:
        raise ValueError("false episode read exceeds bound")
    receipts = load_receipts(session)
    accounts = session.scalars(select(BrokerAccount).where(BrokerAccount.name.in_(account_names))).all()
    account_map = {a.name: a.id for a in accounts}
    strategy = session.scalar(select(Strategy).where(Strategy.code == "schwab_1m_v2"))
    proofs = {}
    for (symbol, opportunity), values in groups.items():
        reason, evidence_at, all_filled = "fully_filled_closed_both_reads", observed, True
        bindings = frozenset((row.broker_account_name, str(row.id), str(row.entry_order_id))
                             for row, _ in values)
        if (strategy is None or len(set(account_names)) != 2
                or set(account_map) != set(account_names)):
            reason = "accounts_unknown"
        else:
            for name in account_names:
                receipt = receipts.get(name, {})
                read_at = at_ms(receipt.get("read_at"))
                if (receipt.get("writer") != "oms.sync_broker_positions"
                        or not 0 <= observed - read_at <= MAX_AGE_MS or read_at <= 0):
                    reason = "broker_position_read_stale_or_unknown"
                    break
                evidence_at = min(evidence_at, read_at)
            if reason == "fully_filled_closed_both_reads":
                if (session.scalar(select(VirtualPosition.id).where(
                        VirtualPosition.strategy_id == strategy.id,
                        VirtualPosition.broker_account_id.in_(account_map.values()),
                        VirtualPosition.symbol == symbol, VirtualPosition.quantity != 0).limit(1))
                        or session.scalar(select(OmsManagedPosition.id).where(
                            OmsManagedPosition.strategy_code == strategy.code,
                            OmsManagedPosition.broker_account_name.in_(account_names),
                            OmsManagedPosition.symbol == symbol,
                            or_(OmsManagedPosition.status == "open", OmsManagedPosition.current_quantity != 0)
                        ).limit(1))):
                    reason = "own_position_still_open"
                elif (session.scalar(select(BrokerOrder.id).where(
                        BrokerOrder.strategy_id == strategy.id,
                        BrokerOrder.broker_account_id.in_(account_map.values()),
                        BrokerOrder.symbol == symbol, BrokerOrder.side == "buy",
                        or_(BrokerOrder.status.is_(None), BrokerOrder.status.notin_(TERMINAL))).limit(1))
                        or session.scalar(select(TradeIntent.id).where(
                            TradeIntent.strategy_id == strategy.id,
                            TradeIntent.broker_account_id.in_(account_map.values()),
                            TradeIntent.symbol == symbol, TradeIntent.side == "buy",
                            TradeIntent.intent_type == "open",
                            or_(TradeIntent.status.is_(None), TradeIntent.status.notin_(TERMINAL | {"completed"}))
                        ).limit(1))):
                    reason = "working_or_unknown_buy_or_intent"
            for row, classification in values:
                if reason != "fully_filled_closed_both_reads":
                    break
                identity = classification.identity
                try:
                    order_id = UUID(identity.entry_order_id)
                except (ValueError, TypeError, AttributeError):
                    reason = "entry_binding_unknown"
                    break
                order = session.get(BrokerOrder, order_id)
                if (order is None or row.status != "closed" or row.current_quantity != 0
                        or row.strategy_code != "schwab_1m_v2"
                        or row.broker_account_name != identity.account or row.symbol != identity.symbol
                        or str(row.id) != identity.managed_row_id
                        or row.entry_order_id != order.id or row.entry_client_order_id != identity.entry_client_order_id
                        or order.client_order_id != identity.entry_client_order_id
                        or order.strategy_id != strategy.id
                        or order.broker_account_id != account_map.get(identity.account)
                        or order.symbol != symbol or order.side != "buy"
                        or str((order.payload or {}).get("fanout_segment_id")) != str(opportunity)):
                    reason = "entry_binding_unknown"
                    break
                filled = session.scalar(select(func.sum(Fill.quantity)).where(
                    Fill.order_id == order.id, Fill.side == "buy")) or Decimal(0)
                if filled <= 0 or filled > order.quantity or filled != row.original_quantity:
                    reason = "entry_execution_quantity_unknown"
                    break
                if order.status != "filled" or filled != order.quantity:
                    if (order.status not in {"cancelled", "canceled", "rejected"}
                            or not terminal_report_proven(session, order, as_of_ms=evidence_at)):
                        reason = "partial_entry_not_terminal"
                        break
                    all_filled = False
                closes = session.execute(select(Fill.quantity, Fill.filled_at)
                    .join(BrokerOrder, BrokerOrder.id == Fill.order_id)
                    .where(Fill.strategy_id == strategy.id, Fill.broker_account_id == order.broker_account_id,
                           Fill.symbol == symbol, Fill.side == "sell",
                           BrokerOrder.payload["confirmation_managed_row_id"].as_string() == str(row.id))).all()
                sold = sum((qty for qty, _ in closes), Decimal(0))
                close_at = max((at_ms(t) for _, t in closes), default=at_ms(row.updated_at))
                if evidence_at < close_at:
                    reason = "broker_read_precedes_close"
                    break
                total = session.scalar(select(AccountPosition.quantity).where(
                    AccountPosition.broker_account_id == order.broker_account_id,
                    AccountPosition.symbol == symbol)) or Decimal(0)
                # Manual/ORB lots do not become v2 ownership. If the shared account
                # still holds shares, require exact owned sell attribution instead.
                if total != 0 and sold != filled:
                    reason = "shared_holding_own_exit_unproven"
                    break
            if reason == "fully_filled_closed_both_reads":
                siblings = session.scalars(select(BrokerOrder).where(
                    BrokerOrder.strategy_id == strategy.id,
                    BrokerOrder.broker_account_id.in_(account_map.values()),
                    BrokerOrder.symbol == symbol, BrokerOrder.side == "buy",
                    BrokerOrder.payload["fanout_segment_id"].as_string() == str(opportunity)
                ).order_by(BrokerOrder.submitted_at, BrokerOrder.id)).all()
                latest = {order.broker_account_id: order for order in siblings}
                classified_ids = {UUID(c.identity.entry_order_id) for _, c in values}
                for order in latest.values():
                    if order.id in classified_ids:
                        continue
                    if (order.status in {"cancelled", "canceled", "rejected"}
                            and terminal_report_proven(session, order, as_of_ms=evidence_at)):
                        all_filled = False
                    else:
                        reason = "unfilled_sibling_requires_cancel_receipt"
                        break
        complete = reason == "fully_filled_closed_both_reads"
        if complete and not all_filled:
            reason = "closed_with_terminal_sibling_and_both_reads"
        proofs[(symbol, opportunity)] = FalseFlipReleaseProof(
            symbol, opportunity, evidence_at, complete, reason, bindings, all_filled)
    return proofs


def persist_owner_transition(factory, record, accounts, valid, *, request=None,
                             previous=None, now=None):
    """Atomic native snapshots; failed/superseded writes never grant memory permission."""
    from project_mai_tai.fanout_segment_store import current_session_anchor
    from project_mai_tai.falseflip1_runtime import BUDGET_TYPE, FalseBudget
    from project_mai_tai.v2_removed_wait import DISPATCH_SNAPSHOT_TYPE

    now = now or datetime.now(UTC)
    old_opportunity = previous[1] if previous is not None else record.opportunity_id
    with factory() as session:
        if session.bind.dialect.name == "postgresql":
            from sqlalchemy import text

            session.execute(text("SET LOCAL lock_timeout = '250ms'"))
            session.execute(text("SET LOCAL statement_timeout = '2s'"))
            session.execute(select(func.pg_advisory_xact_lock(func.hashtext("v2_removed_wait:" + record.symbol))))
        if not valid():
            raise ValueError("false episode memory changed")
        rows = session.scalars(select(OmsManagedPosition).where(
            OmsManagedPosition.strategy_code == "schwab_1m_v2",
            OmsManagedPosition.symbol == record.symbol,
            OmsManagedPosition.broker_account_name.in_(accounts),
            OmsManagedPosition.entry_time >= current_session_anchor(now),
        ).limit(257)).all()
        if len(rows) > 256:
            raise ValueError("false episode write bound exceeded")
        proof = read_release_proofs(session, rows, accounts, now=now).get((record.symbol, old_opportunity))
        if proof is None or not proof.complete:
            raise ValueError("false episode broker/entry proof changed")
        budget_row = session.scalar(select(DashboardSnapshot).where(
            DashboardSnapshot.snapshot_type == BUDGET_TYPE,
            DashboardSnapshot.payload["symbol"].as_string() == record.symbol,
            cast(DashboardSnapshot.payload["segment"].as_string(), BigInteger) == record.retry_segment_id,
            DashboardSnapshot.created_at >= current_session_anchor(now),
        ).order_by(DashboardSnapshot.payload["revision"].as_integer().desc()).limit(1))
        if budget_row is None:
            raise ValueError("false episode budget missing")
        budget = FalseBudget.read(budget_row.payload)
        settled = budget.filled_closed if proof.all_filled else budget.cancelled
        if old_opportunity not in settled or old_opportunity in budget.drafted:
            raise ValueError("false episode settlement marker changed")
        already_written = []
        for kind, key in (("v2_fanout_segment_identity", "fanout_segment_id"),
                          ("v2_flip_entry_ownership", "opportunity_id")):
            latest = session.scalar(select(DashboardSnapshot).where(
                DashboardSnapshot.snapshot_type == kind,
                DashboardSnapshot.payload["symbol"].as_string() == record.symbol,
                DashboardSnapshot.created_at >= current_session_anchor(now),
            ).order_by(DashboardSnapshot.created_at.desc(), DashboardSnapshot.id.desc())
              .limit(1).with_for_update())
            active = previous is not None
            reason = "first_rest_working" if active else "false_flip2_closed_episode"
            same = bool(latest and str(latest.payload.get(key)) == str(record.opportunity_id)
                        and latest.payload.get("active") is active and latest.payload.get("reason") == reason)
            if same and kind == "v2_flip_entry_ownership" and active:
                same = latest.payload == record.as_payload(active=active, reason=reason)
            already_written.append(same)
            if not same and (latest is None or str(latest.payload.get(key)) != str(old_opportunity)
                             or latest.payload.get("active") is not (previous is None)):
                raise ValueError("durable owner/opportunity changed")
        if all(already_written):
            return  # A worker commit with a delayed acknowledgement is safe to retry.
        if any(already_written):
            raise ValueError("owner/opportunity publication was not atomic")
        active = previous is not None
        reason = "first_rest_working" if active else "false_flip2_closed_episode"
        session.add_all([
            DashboardSnapshot(snapshot_type="v2_fanout_segment_identity", created_at=now, payload={
                "schema_version": 1, "strategy_code": "schwab_1m_v2", "symbol": record.symbol,
                "fanout_segment_id": str(record.opportunity_id), "active": active, "reason": reason,
                "session_anchor": current_session_anchor(now).isoformat()}),
            DashboardSnapshot(snapshot_type="v2_flip_entry_ownership", created_at=now,
                              payload=record.as_payload(active=active, reason=reason)),
        ])
        if request is not None:
            if request.purpose != "false_flip_restore" or request.opportunity_id != old_opportunity:
                raise ValueError("not this false restore request")
            latest = session.scalar(select(DashboardSnapshot).where(
                DashboardSnapshot.snapshot_type == "v2_removed_wait",
                DashboardSnapshot.payload["symbol"].as_string() == record.symbol,
            ).order_by(DashboardSnapshot.created_at.desc(), DashboardSnapshot.id.desc())
              .limit(1).with_for_update())
            if latest is None or latest.payload != request.payload(active=True):
                raise ValueError("removal request superseded")
            session.add(DashboardSnapshot(snapshot_type="v2_removed_wait",
                                          payload=request.payload(active=False), created_at=now))
        if active:
            session.add(DashboardSnapshot(snapshot_type=DISPATCH_SNAPSHOT_TYPE, created_at=now, payload={
                "schema_version": 1, "strategy_code": "schwab_1m_v2", "symbol": record.symbol,
                "opportunity_id": str(record.opportunity_id), "account_names": list(accounts),
                "kind": "begin", "attempt_token": "", "account_name": ""}))
        session.flush()
        if not valid():
            raise ValueError("false episode superseded before commit")
        session.commit()


def unanswered_incidents(factory, requests, *, now=None):
    now = now or datetime.now(UTC)
    with factory() as session:
        if session.bind.dialect.name == "postgresql":
            from sqlalchemy import text

            session.execute(text("SET LOCAL lock_timeout = '250ms'"))
            session.execute(text("SET LOCAL statement_timeout = '2s'"))
        for request in requests:
            if request.purpose != "false_flip_restore" or at_ms(now) - request.requested_at_ms < 30000:
                continue
            title = f"FALSEFLIP2 sibling cancellation unconfirmed: {request.symbol} {request.token}"
            if session.scalar(select(SystemIncident.id).where(SystemIncident.title == title)) is None:
                session.add(SystemIncident(service_name="schwab-1m-v2", severity="critical", status="open",
                    title=title, payload={"source": "FALSEFLIP2", "symbol": request.symbol,
                        "opportunity_id": request.opportunity_id, "request_token": request.token,
                        "action": "keep_blocked", "same_slot": "resting"}))
        session.commit()
