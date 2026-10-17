"""Read-first retirement of old managed SELLs; never submit or apply old fills.

This is not a boot permission. Callers must retain the existing recovery block
until the complete restart/owner protocol has been qualified independently.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from hashlib import sha256
import json
from math import isfinite

from sqlalchemy import or_, select

from project_mai_tai.broker_adapters.protocols import OrderRequest
from project_mai_tai.db.models import (
    BrokerAccount, BrokerOrder, BrokerOrderEvent, Fill, OmsManagedPosition, Strategy,
    TradeIntent, VirtualPosition,
)
from project_mai_tai.fanout_segment_store import current_session_anchor
from project_mai_tai.oms.bot_control_scope import logical_account_mode
from project_mai_tai.oms.managed_sell_prewire import TERMINAL


def _archive(row):
    return {column.name: getattr(row, column.name) for column in row.__table__.columns}


def _encoded(value):
    return json.dumps(value, default=str, sort_keys=True, separators=(",", ":"))


def requires_read_only_recovery(order, intent, strategy_code, now, *, strategy=None, account=None):
    """Old/unknown prepared sellers may not enter generic fill/reprice paths."""
    payload = order.payload or {}
    managed = (payload.get("managed_sell_prepared_at")
               or strategy_code == "schwab_1m_v2"
               and str(payload.get("oms_v2_managed_exit", "")).lower() == "true")
    if (strategy_code not in {"schwab_1m_v2", "orb_schwab"} or order.side != "sell"
            or not managed):
        return False
    # Exempt only positively bound nonlive scope. Deployment environment is not
    # execution mode; missing/mismatched identities retain the old live fence.
    bound = (strategy is not None and account is not None and intent is not None
             and strategy.id == order.strategy_id and strategy.code == strategy_code
             and account.id == order.broker_account_id and intent.id == order.intent_id
             and intent.strategy_id == order.strategy_id
             and intent.broker_account_id == order.broker_account_id and intent.symbol == order.symbol)
    if bound:
        mode = logical_account_mode(account.name)
        if mode in {"paper", "shadow"} or (
                mode == "live" and strategy.execution_mode in {"paper", "shadow"}):
            return False
    age = order.submitted_at or (intent.created_at if intent is not None else None)
    # ORM timestamps are UTC in both supported stores, including naive SQLite.
    if age is not None and age.tzinfo is None:
        age = age.replace(tzinfo=UTC)
    return age is None or age < current_session_anchor(now)


def _flat_episode_proof(unit, order, report):
    """Positive unrelated broker holdings are not permission to claim they are ours."""
    from uuid import UUID
    payload = order.payload or {}
    if report.filled_quantity > 0 and (
            not report.broker_fill_id or not isinstance(report.fill_price, Decimal)
            or not report.fill_price.is_finite() or report.fill_price <= 0):
        raise ValueError("old_sell_terminal_execution_identity_UNKNOWN")
    identity = payload.get("entry_order_id") or payload.get("managed_sell_entry_order_id")
    if not identity:
        raise ValueError("old_sell_episode_binding_UNKNOWN")
    entry = unit.get(BrokerOrder, UUID(identity))
    if (entry is None or entry.strategy_id != order.strategy_id
            or entry.broker_account_id != order.broker_account_id or entry.symbol != order.symbol
            or entry.side != "buy" or entry.status != "filled" or not entry.broker_order_id):
        raise ValueError("old_sell_entry_binding_UNKNOWN")
    exits = unit.scalars(select(BrokerOrder).where(
        BrokerOrder.strategy_id == order.strategy_id, BrokerOrder.broker_account_id == order.broker_account_id,
        BrokerOrder.symbol == order.symbol, BrokerOrder.side == "sell",
        or_(BrokerOrder.id == order.id, BrokerOrder.intent_id == entry.intent_id,
            BrokerOrder.payload["entry_order_id"].as_string() == str(entry.id),
            BrokerOrder.payload["managed_sell_entry_order_id"].as_string() == str(entry.id)),
    ).limit(513)).all()
    if len(exits) > 512:
        raise ValueError("old_sell_episode_history_bound_UNKNOWN")

    def total(bound):
        fills = unit.scalars(select(Fill).where(Fill.order_id == bound.id)).all()
        if any(fill.strategy_id != order.strategy_id or fill.broker_account_id != order.broker_account_id
               or fill.symbol != order.symbol or fill.side != bound.side or not fill.broker_fill_id
               or not fill.quantity.is_finite() or fill.quantity <= 0
               or not fill.price.is_finite() or fill.price <= 0 for fill in fills):
            raise ValueError("old_sell_episode_fill_identity_UNKNOWN")
        quantity = sum((fill.quantity for fill in fills), Decimal(0))
        if not bound.quantity.is_finite() or quantity > bound.quantity:
            raise ValueError("old_sell_episode_execution_bounds_UNKNOWN")
        return quantity

    bought, sold = total(entry), Decimal(0)
    if bought <= 0 or bought != entry.quantity:
        raise ValueError("old_sell_entry_execution_incomplete")
    for exit_order in exits:
        if exit_order.id == order.id:
            if total(exit_order) > report.filled_quantity:
                raise ValueError("old_sell_terminal_execution_regressed")
            sold += report.filled_quantity
        else:
            if exit_order.status not in TERMINAL:
                raise ValueError("old_sell_episode_other_seller_UNKNOWN")
            amount = total(exit_order)
            if exit_order.status == "filled" and amount != exit_order.quantity:
                raise ValueError("old_sell_episode_exit_execution_incomplete")
            sold += amount
    if sold != bought:
        raise ValueError("old_sell_episode_not_proven_flat")
    return {"entry_order_id": str(entry.id), "bought": str(bought), "exited": str(sold),
            "source": "canonical_episode_fills_plus_exact_original_terminal_report"}


async def retire_previous_flat_sell(service, order_id):
    """Archive one exact old seller only after terminal + fresh flat wire proof.

    A missing broker ID remains UNKNOWN, including an interrupted initial POST.
    No fill writer or broker mutation is reachable through this function.
    """
    started = datetime.now(UTC)
    anchor = current_session_anchor(started)

    def load(unit):
        row = unit.get(BrokerOrder, order_id)
        if row is None:
            raise ValueError("old_sell_missing")
        strategy = unit.get(Strategy, row.strategy_id)
        account = unit.get(BrokerAccount, row.broker_account_id)
        intent = unit.get(TradeIntent, row.intent_id) if row.intent_id else None
        payload = row.payload or {}
        if (strategy is None or account is None or intent is None
                or strategy.code not in {"schwab_1m_v2", "orb_schwab"}
                or strategy.execution_mode != "live" or not account.name.startswith("live:")
                or account.provider not in {"schwab", "webull"}
                or row.side != "sell" or row.status in TERMINAL or not row.broker_order_id
                or intent.strategy_id != row.strategy_id or intent.broker_account_id != row.broker_account_id
                or intent.symbol != row.symbol or intent.side != "sell"
                or not (payload.get("managed_sell_prepared_at")
                        or strategy.code == "schwab_1m_v2"
                        and str(payload.get("oms_v2_managed_exit", "")).lower() == "true")):
            raise ValueError("old_sell_binding_UNKNOWN")
        age = row.submitted_at or intent.created_at
        # SQLite test storage is naive UTC; a naive production timestamp is not evidence.
        if age is not None and age.tzinfo is None and unit.bind.dialect.name == "sqlite":
            age = age.replace(tzinfo=UTC)
        if age is None or age.tzinfo is None or age >= anchor:
            raise ValueError("old_sell_not_proven_previous_session")
        original = {"order": _archive(row), "intent": _archive(intent)}
        fingerprint = sha256(_encoded(original).encode()).hexdigest()
        request = OrderRequest(row.client_order_id, account.name, strategy.code,
            row.symbol, "sell", "close", row.quantity, "O14_OLD_SELL_READ_ONLY",
            {**payload, "broker_order_id": row.broker_order_id}, row.order_type, row.time_in_force)
        return request, original, fingerprint, account.provider

    try:
        request, original, fingerprint, provider = await service._run_db(load, commit=False)
        report = await service.broker_adapter.fetch_order_update(request)
        terminal_read = datetime.now(UTC)
        if (report is None or report.origin != "broker"
                or report.event_type not in {"filled", "cancelled", "rejected"}
                or report.client_order_id != request.client_order_id
                or report.broker_order_id != request.metadata["broker_order_id"]
                or report.symbol != request.symbol or report.side != "sell"
                or report.quantity != request.quantity
                or not isinstance(report.filled_quantity, Decimal)
                or not report.filled_quantity.is_finite()
                or not Decimal(0) <= report.filled_quantity <= request.quantity
                or (report.event_type == "filled" and report.filled_quantity != request.quantity)):
            raise ValueError("old_sell_original_terminal_UNKNOWN")
        snapshots = await service.broker_adapter.list_account_positions(request.broker_account_name)
        if provider == "webull":
            # Never use the generic receipt helper's missing-cache fallback here.
            age = service.broker_adapter.positions_wire_read_age_seconds(request.broker_account_name)
            if isinstance(age, bool) or not isinstance(age, (int, float)) or not isfinite(age) or age < 0:
                raise ValueError("old_sell_webull_wire_age_UNKNOWN")
            now = datetime.now(UTC)
            read_at = now - timedelta(seconds=age)
        else:
            read_at = service._positions_wire_read_at(request.broker_account_name)
            # An uncached helper timestamps the completed read at invocation.
            # Compare against a clock sampled AFTER obtaining that evidence.
            now = datetime.now(UTC)
        if (not isinstance(read_at, datetime) or read_at.tzinfo is None
                or not terminal_read < read_at <= now
                or (now - read_at).total_seconds() > 30
                or current_session_anchor(now) != anchor):
            raise ValueError("old_sell_flat_wire_not_fresh")
        if not isinstance(snapshots, list):
            raise ValueError("old_sell_positions_incomplete")
        broker_quantity = Decimal(0)
        matched = False
        for snapshot in snapshots:
            if snapshot.broker_account_name != request.broker_account_name:
                raise ValueError("old_sell_foreign_position_read")
            if snapshot.symbol == request.symbol:
                if (matched or not isinstance(snapshot.quantity, Decimal)
                        or not snapshot.quantity.is_finite() or snapshot.quantity < 0):
                    raise ValueError("old_sell_broker_balance_UNKNOWN")
                matched, broker_quantity = True, snapshot.quantity

        def retire(unit):
            commit_clock = datetime.now(UTC)
            if (not 0 <= (commit_clock - read_at).total_seconds() <= 30
                    or current_session_anchor(commit_clock) != anchor):
                raise ValueError("old_sell_flat_wire_aged_before_archive")
            row = unit.scalar(select(BrokerOrder).where(BrokerOrder.id == order_id).with_for_update())
            intent = unit.get(TradeIntent, row.intent_id) if row is not None else None
            if row is None or intent is None or sha256(_encoded({
                    "order": _archive(row), "intent": _archive(intent)}).encode()).hexdigest() != fingerprint:
                raise ValueError("old_sell_changed_during_reads")
            positions = unit.scalars(select(VirtualPosition).where(
                VirtualPosition.strategy_id == row.strategy_id,
                VirtualPosition.broker_account_id == row.broker_account_id,
                VirtualPosition.symbol == row.symbol)).all()
            if len(positions) != 1 or positions[0].quantity != 0:
                raise ValueError("old_sell_owned_net_not_flat")
            if unit.scalar(select(OmsManagedPosition.id).where(
                    OmsManagedPosition.strategy_code == request.strategy_code,
                    OmsManagedPosition.broker_account_name == request.broker_account_name,
                    OmsManagedPosition.symbol == request.symbol,
                    OmsManagedPosition.current_quantity != 0).limit(1)) is not None:
                raise ValueError("old_sell_managed_net_not_flat")
            if unit.scalar(select(BrokerOrder.id).where(
                    BrokerOrder.id != row.id, BrokerOrder.broker_account_id == row.broker_account_id,
                    BrokerOrder.symbol == row.symbol,
                    or_(BrokerOrder.status.is_(None), BrokerOrder.status.not_in(TERMINAL))).limit(1)) is not None:
                raise ValueError("old_sell_other_order_unresolved")
            own_flat_proof = _flat_episode_proof(unit, row, report) if broker_quantity else {
                "source": "fresh_complete_account_flat_wire_read"}
            # Preserve the complete old rows before mutation in the append-only event.
            unit.add(BrokerOrderEvent(order_id=row.id, event_type="aborted", event_source="client",
                event_at=now, payload=json.loads(_encoded({
                    "reason": "o14_previous_session_retired", "original": original,
                    "original_sha256": fingerprint, "terminal_report": vars(report),
                    "terminal_read_at": terminal_read, "flat_wire_read_at": read_at,
                    "broker_quantity_untouched": broker_quantity, "owned_flat_proof": own_flat_proof,
                    "archive_only": True, "historical_fill_applied": False}))))
            row.status = "aborted"
            row.payload = {**(row.payload or {}), "o14_retired_at": now.isoformat(),
                           "o14_original_sha256": fingerprint}
            others = unit.scalar(select(BrokerOrder.id).where(
                BrokerOrder.intent_id == intent.id, BrokerOrder.id != row.id,
                or_(BrokerOrder.status.is_(None), BrokerOrder.status.not_in(TERMINAL))).limit(1))
            if others is None and intent.status not in TERMINAL:
                intent.status = "aborted"
            return True

        return await service._run_db(retire)
    except Exception as exc:
        service.logger.critical("[MANAGED-SELL-RETIREMENT-BLOCKED] order_id=%s reason=%s sell=0",
                                order_id, type(exc).__name__ + ":" + str(exc))
        return False
