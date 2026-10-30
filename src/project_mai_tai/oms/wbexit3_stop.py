"""Focused Webull STOP retry identities under the EXISTING C6 lease/journal.

Pure DB checks: no provider access, order creation, fill writing, timer, lease
claim or wire permission. The original driver/managed owner/ceiling/journal still
owns dispatch. UNKNOWN prior calls always remain read-first, never a new SELL.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from uuid import uuid5

from sqlalchemy import select

from project_mai_tai.db.models import BotControlAttempt, BrokerAccount, BrokerOrder, Fill
from project_mai_tai.oms.bot_control import ControlConflict, _aware_utc
from project_mai_tai.oms import wbexit3

BOUND = 256
TERMINAL = frozenset({"filled", "cancelled", "canceled", "rejected", "expired"})
READ_KEY = "wbexit3_stop_actual_fetch"
READ_ATTEMPT_KEY = "wbexit3_stop_fetch_attempt"


def selected(service, account, now):
    return bool(wbexit3.enabled(service) and account.provider == "webull"
        and wbexit3.session(now) in {"PM", "AH"})


def _scope(service, db, lease, journal, order, now):
    if not wbexit3.enabled(service):
        raise ControlConflict("wbexit3_stop_disabled")
    # This current lease must be valid NOW; a prior terminal row is economic
    # evidence, never a transferable task/nonce or a request to reacquire a lease.
    journal.store._leased(db, lease, now)
    if order is None:
        raise ControlConflict("wbexit3_stop_original_order_missing")
    account = db.get(BrokerAccount, order.broker_account_id)
    if (account is None or account.provider != "webull" or account.id not in lease.account_ids
            or order.strategy_id != lease.strategy_id or order.side != "sell"
            or (order.payload or {}).get("killsw1_command") != str(lease.command_id)):
        raise ControlConflict("wbexit3_stop_original_owner_scope_UNKNOWN")
    return account


def retired(service, db, lease, journal, order, now):
    """Positive persisted READ + canonical fills; False means reread original ID.

    Stored cancelled/rejected status alone does not retire a SELL. A returned
    True still cannot size/prepare/send an order; original C6 proof does that.
    """
    _scope(service, db, lease, journal, order, now)
    attempts = db.scalars(select(BotControlAttempt).where(
        BotControlAttempt.command_id == lease.command_id, BotControlAttempt.target_order_id == order.id,
        BotControlAttempt.operation == "SUBMIT_EXIT").limit(2)).all()
    if len(attempts) != 1:
        raise ControlConflict("wbexit3_stop_prior_attempt_identity_UNKNOWN")
    attempt = attempts[0]
    if (attempt.strategy_id != lease.strategy_id or attempt.account_id != order.broker_account_id
            or attempt.generation != lease.generation or attempt.symbol != order.symbol
            or attempt.client_order_id != order.client_order_id or attempt.requested_quantity != order.quantity):
        raise ControlConflict("wbexit3_stop_prior_attempt_binding_changed")
    if attempt.status in {"PREPARED", "MAY_HAVE_SENT", "WORKING", "UNKNOWN"}:
        return False  # Existing _read_exit/unknown-ACK handling retains this exact order.
    # C6's audit also uses authoritative_order_read for a direct submit reply.
    # Only read_existing below writes this separate AFTER-fetch receipt.
    evidence = (order.payload or {}).get(READ_KEY)
    if evidence is None:
        return False
    audit = attempt.terminal_evidence or {}
    if (not isinstance(evidence, dict) or attempt.status != "TERMINAL" or order.status not in TERMINAL or not order.broker_order_id
            or evidence.get("source") != "fetch_order_update" or evidence.get("status") != order.status
            or evidence.get("broker_order_id") != order.broker_order_id
            or evidence.get("client_order_id") != order.client_order_id
            or evidence.get("attempt_id") != str(attempt.attempt_id)
            or evidence.get("command_id") != str(lease.command_id)
            or evidence.get("generation") != lease.generation
            or attempt.last_authoritative_read_at is None
            or audit.get("source") != "authoritative_order_read"
            or audit.get("broker_order_id") != order.broker_order_id
            or audit.get("client_order_id") != order.client_order_id
            or audit.get("status") not in TERMINAL):
        raise ControlConflict("wbexit3_stop_prior_terminal_read_UNKNOWN")
    try:
        stamp = datetime.fromisoformat(evidence["observed_at"])
        filled = Decimal(evidence["filled_quantity"])
        audit_filled = Decimal(audit["filled_quantity"])
    except (KeyError, ValueError, TypeError, ArithmeticError) as exc:
        raise ControlConflict("wbexit3_stop_prior_terminal_encoding_UNKNOWN") from exc
    if (stamp.tzinfo is None or not _aware_utc(attempt.prepared_at) <= stamp <= now
            or stamp < _aware_utc(attempt.last_authoritative_read_at)
            or not filled.is_finite() or not 0 <= filled <= order.quantity
            or not audit_filled.is_finite() or not 0 <= audit_filled <= filled
            or order.status == "filled" and filled != order.quantity):
        raise ControlConflict("wbexit3_stop_prior_terminal_quantity_or_clock_UNKNOWN")
    fills = db.scalars(select(Fill).where(Fill.order_id == order.id).limit(BOUND + 1)).all()
    if len(fills) > BOUND or any(f.strategy_id != order.strategy_id or f.broker_account_id != order.broker_account_id
           or f.symbol != order.symbol or f.side != "sell" or not f.broker_fill_id
           or not f.quantity.is_finite() or f.quantity <= 0 or not f.price.is_finite() or f.price <= 0 for f in fills):
        raise ControlConflict("wbexit3_stop_prior_execution_binding_UNKNOWN")
    if sum((f.quantity for f in fills), Decimal(0)) != filled:
        raise ControlConflict("wbexit3_stop_prior_execution_not_canonically_recorded")
    return True


async def read_existing(service, lease, journal, order_id):
    """Read the SAME STOP order under its CURRENT original C6 lease.

    No new order/nonce/lease is created. A later terminal read may add an actual
    execution through the existing canonical writer, never by journal arithmetic.
    The immutable terminal audit is retained; the explicit fetch receipt is extra
    evidence and can retire it only after matching canonical fills.
    """
    from project_mai_tai.broker_adapters.protocols import OrderRequest
    from project_mai_tai.db.models import TradeIntent
    from project_mai_tai.events import TradeIntentEvent, TradeIntentPayload
    from project_mai_tai.oms import bot_control_atr as atr
    from datetime import UTC

    def read(db):
        order = db.get(BrokerOrder, order_id)
        if order is None:
            raise ControlConflict("wbexit3_stop_existing_order_missing")
        account = _scope(service, db, lease, journal, order, atr.datetime.now(UTC))
        if not selected(service, account, atr.datetime.now(UTC)) or not order.broker_order_id:
            raise ControlConflict("wbexit3_stop_existing_native_identity_UNKNOWN")
        attempts = db.scalars(select(BotControlAttempt).where(
            BotControlAttempt.command_id == lease.command_id,
            BotControlAttempt.target_order_id == order.id,
            BotControlAttempt.operation == "SUBMIT_EXIT").limit(2)).all()
        intent = db.get(TradeIntent, order.intent_id)
        if (len(attempts) != 1 or intent is None or intent.strategy_id != lease.strategy_id
                or intent.broker_account_id != order.broker_account_id or intent.intent_type != "close"
                or intent.side != "sell" or intent.symbol != order.symbol or intent.quantity != order.quantity
                or intent.reason != atr.STOP_REASON
                or (intent.payload or {}).get("metadata", {}).get("killsw1_command") != str(lease.command_id)
                or attempts[0].status not in {"MAY_HAVE_SENT", "WORKING", "UNKNOWN", "TERMINAL"}
                or attempts[0].generation != lease.generation
                or attempts[0].strategy_id != lease.strategy_id or attempts[0].account_id != order.broker_account_id
                or attempts[0].symbol != order.symbol or attempts[0].requested_quantity != order.quantity
                or attempts[0].client_order_id != order.client_order_id):
            raise ControlConflict("wbexit3_stop_existing_attempt_UNKNOWN")
        now = atr.datetime.now(UTC)
        previous = (order.payload or {}).get(READ_ATTEMPT_KEY)
        count = 0
        if previous is not None:
            if (not isinstance(previous, dict) or previous.get("attempt_id") != str(attempts[0].attempt_id)
                    or previous.get("command_id") != str(lease.command_id)
                    or previous.get("generation") != lease.generation
                    or previous.get("client_order_id") != order.client_order_id
                    or previous.get("broker_order_id") != order.broker_order_id
                    or type(previous.get("count")) is not int or not 1 <= previous["count"] <= 1000000):
                raise ControlConflict("wbexit3_stop_read_retry_binding_UNKNOWN")
            if now < datetime.fromisoformat(previous["next_at"]):
                raise ControlConflict("wbexit3_stop_owned_read_backoff")
            count = previous["count"]
        count += 1
        order.payload = {**(order.payload or {}), READ_ATTEMPT_KEY: {
            "attempt_id": str(attempts[0].attempt_id), "command_id": str(lease.command_id),
            "generation": lease.generation, "client_order_id": order.client_order_id,
            "broker_order_id": order.broker_order_id, "count": count, "started_at": now.isoformat(),
            "next_at": (now + timedelta(seconds=min(60, 10 * 2 ** min(count - 1, 3)))).isoformat()}}
        return order, attempts[0].attempt_id, account.name, intent.id
    # Commit the owned READ reservation BEFORE the external API/budget await.
    order, attempt_id, account, intent_id = await wbexit3._joined(service._run_db(read, commit=True))
    # Internal JSON receipts are not broker/TradeIntent metadata (str->str).
    request_metadata = {key: value for key, value in (order.payload or {}).items()
                        if key not in {READ_KEY, READ_ATTEMPT_KEY}}
    request = OrderRequest(order.client_order_id, account, atr.STRATEGY, order.symbol, "sell", "close",
        order.quantity, atr.STOP_REASON, {**request_metadata, "broker_order_id": order.broker_order_id},
        order.order_type, order.time_in_force)
    read_started = atr.datetime.now(UTC)
    report = await wbexit3._joined(service.broker_adapter.fetch_order_update(request))
    observed_at = atr.datetime.now(UTC)
    if not 0 <= (observed_at - read_started).total_seconds() <= 2:
        raise ControlConflict("wbexit3_stop_actual_fetch_aged_UNKNOWN")
    event = TradeIntentEvent(source_service="oms-risk", payload=TradeIntentPayload(
        strategy_code=atr.STRATEGY, broker_account_name=account, symbol=order.symbol, side="sell",
        intent_type="close", quantity=order.quantity, reason=atr.STOP_REASON, metadata=dict(request.metadata)))
    with service.session_factory() as db:
        current = db.get(BrokerOrder, order.id)
        _scope(service, db, lease, journal, current, atr.datetime.now(UTC))
        attempt = db.get(BotControlAttempt, attempt_id)
        if (attempt is None or attempt.status not in {"MAY_HAVE_SENT", "WORKING", "UNKNOWN", "TERMINAL"}
                or attempt.target_order_id != order.id or attempt.generation != lease.generation):
            raise ControlConflict("wbexit3_stop_read_attempt_changed")
        atr._validated_sell(report, request, current)
        if (report.quantity != request.quantity or report.reported_at.tzinfo is None
                or report.reported_at > observed_at):
            raise ControlConflict("wbexit3_stop_exact_order_read_quantity_or_clock_UNKNOWN")
        if attempt.status == "TERMINAL" and report.event_type not in TERMINAL:
            raise ControlConflict("wbexit3_stop_terminal_order_read_regressed")
        events = await atr._canonical_sell_reports(service, db, current, db.get(TradeIntent, intent_id), event, request, report)
        _scope(service, db, lease, journal, current, atr.datetime.now(UTC))
        current.payload = {**(current.payload or {}), READ_ATTEMPT_KEY: order.payload[READ_ATTEMPT_KEY], READ_KEY: {
            "source": "fetch_order_update", "attempt_id": str(attempt_id), "command_id": str(lease.command_id),
            "generation": lease.generation, "broker_order_id": report.broker_order_id,
            "client_order_id": report.client_order_id, "status": report.event_type,
            "filled_quantity": str(report.filled_quantity), "observed_at": observed_at.isoformat()}}
        was_terminal = attempt.status == "TERMINAL"
        db.commit()
    if not was_terminal:
        import asyncio
        await wbexit3._joined(asyncio.to_thread(journal.reconcile, lease, attempt_id, broker_order_id=report.broker_order_id,
            client_order_id=report.client_order_id, status=report.event_type,
            filled_quantity=report.filled_quantity, observed_at=observed_at, now=atr.datetime.now(UTC)))
    for item in events:
        await service._publish_order_event(item)


def next_identity(service, db, lease, journal, row, entry, account, now):
    """One deterministic new order only after ALL original STOP exits retire.

    Keeps the original first-attempt coid and original journal operation/sequence
    contract. Later attempts have new BrokerOrder IDs; CleanupJournal's existing
    unresolved unique fence still rejects any competing/uncertain SELL.
    """
    original = f"ks1-atr-{lease.command_id.hex[:16]}-{row.id.hex[:16]}"
    if not selected(service, account, now):
        return original, 0
    orders = db.scalars(select(BrokerOrder).where(BrokerOrder.strategy_id == lease.strategy_id,
        BrokerOrder.broker_account_id == account.id, BrokerOrder.symbol == row.symbol, BrokerOrder.side == "sell",
        BrokerOrder.payload["killsw1_command"].as_string() == str(lease.command_id),
        BrokerOrder.payload["managed_row_id"].as_string() == str(row.id)).order_by(BrokerOrder.id).limit(BOUND + 1)).all()
    if len(orders) > BOUND:
        raise ControlConflict("wbexit3_stop_complete_retry_inventory_over_bound")
    if not orders:
        return original, 0
    sequences = set()
    latest = None
    for order in orders:
        if (order.payload or {}).get("entry_order_id") != str(entry.id) or not retired(service, db, lease, journal, order, now):
            raise ControlConflict("wbexit3_stop_prior_sell_READ_FIRST")
        raw_sequence = (order.payload or {}).get("wbexit3_stop_retry_sequence")
        if raw_sequence is None:
            sequence = 0
        elif (type(raw_sequence) is str and 1 <= len(raw_sequence) <= 3
                and raw_sequence.isascii() and raw_sequence.isdigit() and raw_sequence == str(int(raw_sequence))):
            sequence = int(raw_sequence)
        else:
            raise ControlConflict("wbexit3_stop_retry_sequence_UNKNOWN")
        if not 0 <= sequence <= BOUND or sequence in sequences:
            raise ControlConflict("wbexit3_stop_retry_sequence_UNKNOWN")
        expected = original if sequence == 0 else f"ks1-atr-{lease.command_id.hex[:15]}-{uuid5(lease.command_id, str(row.id) + ':retry:' + str(sequence)).hex[:16]}"
        if order.client_order_id != expected:
            raise ControlConflict("wbexit3_stop_prior_retry_coid_CHANGED")
        sequences.add(sequence)
        attempt = db.scalar(select(BotControlAttempt).where(BotControlAttempt.target_order_id == order.id,
            BotControlAttempt.command_id == lease.command_id, BotControlAttempt.operation == "SUBMIT_EXIT"))
        observed = datetime.fromisoformat(order.payload[READ_KEY]["observed_at"])
        latest = max(latest or observed, observed)
    if sequences != set(range(max(sequences) + 1)):
        raise ControlConflict("wbexit3_stop_retry_history_incomplete")
    sequence = max(sequences) + 1
    delay = min(60, 10 * 2 ** min(sequence - 1, 3))
    if now < latest + timedelta(seconds=delay):
        raise ControlConflict("wbexit3_stop_owned_retry_backoff")
    # Preserve the legacy first identity. Only new retries use the native <=40
    # bound; full command identity remains in the immutable journal/metadata.
    coid = f"ks1-atr-{lease.command_id.hex[:15]}-{uuid5(lease.command_id, str(row.id) + ':retry:' + str(sequence)).hex[:16]}"
    return coid, sequence
