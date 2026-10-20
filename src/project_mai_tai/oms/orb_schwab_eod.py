"""One durable, broker-confirmed close attempt for a residual live ORB position."""

from __future__ import annotations

from dataclasses import dataclass
from asyncio import sleep
from datetime import datetime, time
from decimal import Decimal
from math import isfinite
from time import monotonic
from threading import Lock
from typing import TYPE_CHECKING, Callable
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from sqlalchemy import or_, select

from project_mai_tai.broker_adapters.protocols import OrderRequest
from project_mai_tai.db.models import (
    BrokerAccount, BrokerOrder, Fill, Strategy, SystemIncident, TradeIntent, VirtualPosition,
)
from project_mai_tai.events import TradeIntentEvent, TradeIntentPayload
from project_mai_tai.orb_schwab_exits import CONTEXT_KEY, confirmed_entry_time, exit_signal

if TYPE_CHECKING:
    from project_mai_tai.oms.service import OmsRiskService

_ET = ZoneInfo("America/New_York")
_STATE = "orb_schwab_eod_close"
_START = time(15, 55)
_DEADLINE = time(16)
STOP_REASON = "KILLSW1_STOP"


def pending_generic_sell(unit, entry) -> bool:
    """Only O14's exact internal reservation; never expire an exit-side fence."""
    return unit.scalar(select(BrokerOrder.id).join(
        Strategy, Strategy.id == BrokerOrder.strategy_id,
    ).where(
        Strategy.code == "orb_schwab", BrokerOrder.strategy_id == entry.strategy_id,
        BrokerOrder.broker_account_id == entry.broker_account_id,
        BrokerOrder.symbol == entry.symbol, BrokerOrder.side == "sell",
        BrokerOrder.payload["managed_sell_entry_order_id"].as_string() == str(entry.id),
        BrokerOrder.payload["managed_sell_prepared_at"].as_string().is_not(None),
        or_(BrokerOrder.status.is_(None), BrokerOrder.status.not_in(
            {"filled", "cancelled", "canceled", "rejected", "expired", "aborted"})),
    ).limit(1)) is not None


@dataclass(frozen=True)
class _Target:
    entry_id: UUID
    strategy_id: UUID
    account_id: UUID
    account: str
    symbol: str
    broker_id: str
    client_id: str
    quantity: Decimal
    entry_quantity: Decimal = Decimal("2")
    routed: bool = False


def _write_incident(session, target, reason, now):
    existing = session.scalar(
        select(SystemIncident).where(
            SystemIncident.service_name == "oms-risk",
            SystemIncident.payload["source"].as_string() == _STATE,
            SystemIncident.payload["entry_order_id"].as_string() == str(target.entry_id),
        )
    )
    if existing is not None:
        return False
    session.add(SystemIncident(
        service_name="oms-risk",
        severity="critical",
        status="open",
        title=f"ORB close needs attention: {target.symbol} on {target.account}",
        opened_at=now,
        payload={
            "source": _STATE,
            "entry_order_id": str(target.entry_id),
            "entry_broker_order_id": target.broker_id,
            "broker_account_name": target.account,
            "symbol": target.symbol,
            "reason": reason,
            "observed_at": now.isoformat(),
        },
    ))
    return True


async def _incident(service: OmsRiskService, target: _Target, reason: str, now: datetime) -> None:
    if not await service._run_db(lambda session: _write_incident(session, target, reason, now)):
        return
    service.logger.error(
        "[OMS-ORB-SCHWAB-EOD] symbol=%s parent=%s status=NEEDS_ATTENTION reason=%s",
        target.symbol, target.broker_id, reason,
    )


async def _finish(service: OmsRiskService, target: _Target, phase: str, now: datetime) -> None:
    def write(session):
        entry = session.scalar(
            select(BrokerOrder).where(BrokerOrder.id == target.entry_id).with_for_update()
        )
        if entry is None:
            return
        state = dict((entry.payload or {}).get(_STATE) or {})
        entry.payload = {**(entry.payload or {}), _STATE: {
            **state, "phase": phase, "updated_at": now.isoformat(),
        }}
    await service._run_db(write)


def _schwab_entry(session, target):
    entry = session.get(BrokerOrder, target.entry_id, populate_existing=True)
    if (entry is None or entry.side != "buy" or entry.strategy_id != target.strategy_id
            or entry.broker_account_id != target.account_id or entry.symbol != target.symbol
            or entry.broker_order_id != target.broker_id or not target.broker_id):
        raise ValueError("orb_close_owned_entry_CHANGED")
    # Unsent/terminal zero-fill attempts are not another inventory lot.
    filled = select(Fill.id).where(Fill.order_id == BrokerOrder.id,
        Fill.side == "buy", Fill.quantity > 0).exists()
    others = session.scalars(select(BrokerOrder).where(
        BrokerOrder.strategy_id == target.strategy_id, BrokerOrder.broker_account_id == target.account_id,
        BrokerOrder.symbol == target.symbol, BrokerOrder.side == "buy",
        BrokerOrder.id != target.entry_id,
        or_(BrokerOrder.status.in_({"filled", "partially_filled"}), filled),
        or_(BrokerOrder.submitted_at.is_(None),
            BrokerOrder.submitted_at >= entry.submitted_at if entry.submitted_at is not None else True),
    ).limit(1).execution_options(populate_existing=True)).all()
    if others:
        raise ValueError("orb_close_entry_chronology_UNKNOWN")
    return entry


async def _broker_quantity(
    service: OmsRiskService, target: _Target, *, after: float | None = None,
    legacy_eod_receipt: dict | None = None,
) -> Decimal:
    positions = await service.broker_adapter.list_account_positions(target.account)
    if after is not None:
        # A successful cached list is not a wire receipt. Sample before reading
        # the adapter's age so the inferred lower bound is conservative.
        sampled = monotonic()
        receipt_age = getattr(service.broker_adapter, "positions_wire_read_age_seconds", None)
        age = receipt_age(target.account) if callable(receipt_age) else None
        if isinstance(age, bool) or not isinstance(age, (int, float)) or not isfinite(age) or age < 0:
            raise ValueError("orb_close_positions_wire_receipt_unknown")
        if age > 30 or sampled - age <= after:
            raise ValueError("orb_close_positions_wire_receipt_stale")
    matches = [p for p in positions if p.symbol.upper() == target.symbol]
    if len(matches) != 1 or matches[0].broker_account_name != target.account:
        raise ValueError("broker_position_not_positively_identified")
    quantity = matches[0].quantity
    if (not quantity.is_finite() or quantity <= 0 or not target.quantity.is_finite()
            or not 0 < target.quantity <= target.entry_quantity):
        raise ValueError("broker_position_not_within_orb_ownership")
    if service.settings.provider_for_account(target.account) == "webull":
        from project_mai_tai.oms.orb_country import _snapshot
        def owned(session):
            return _snapshot(service, session, target.entry_id)[3]
    else:
        def owned(session):
            return _schwab_close_quantity(session, target, quantity,
                allow_legacy=legacy_eod_receipt is not None)
    result = await service._run_db(owned, commit=False)
    if service.settings.provider_for_account(target.account) == "webull":
        own_quantity, unknown = result, None
    else:
        own_quantity, unknown = result
    if unknown is not None:
        await _incident(service, target, unknown, datetime.now(_ET))
    if not own_quantity.is_finite() or not 0 < own_quantity <= target.quantity:
        raise ValueError("orb_close_current_owned_balance_UNKNOWN")
    if quantity < own_quantity:
        raise ValueError("broker_position_below_orb_ownership")
    if own_quantity != own_quantity.to_integral_value():
        raise ValueError("orb_close_owned_quantity_not_whole_shares")
    if legacy_eod_receipt is not None:
        legacy_eod_receipt["broker_quantity"] = quantity
    return own_quantity


def _schwab_close_quantity(session, target, broker_quantity, *, allow_legacy):
    from project_mai_tai.oms.schwab_owned_positions import own_open_quantity

    _schwab_entry(session, target)  # Identity/real-lot conflicts never use a quantity fallback.
    try:
        owned = own_open_quantity(session, strategy_id=target.strategy_id,
            account_id=target.account_id, symbol=target.symbol)
        if owned > target.quantity:
            raise ValueError("orb_close_current_owned_balance_UNKNOWN")
        if allow_legacy and 0 < broker_quantity < owned:
            return broker_quantity, "orb_close_ownership_UNKNOWN_legacy_eod:broker_below_owned"
        return owned, None
    except (ValueError, TypeError, ArithmeticError) as exc:
        if not allow_legacy or not 0 < broker_quantity <= target.quantity:
            raise
        return min(broker_quantity, target.quantity), f"orb_close_ownership_UNKNOWN_legacy_eod:{exc}"


async def _post_release_quantity(service, target, released_at, clock):
    deadline = monotonic() + 30
    while True:
        try:
            return await _broker_quantity(service, target, after=released_at)
        except ValueError as exc:
            if str(exc) != "orb_close_positions_wire_receipt_stale":
                raise
            if monotonic() >= deadline or clock().astimezone(_ET).time() >= _DEADLINE:
                raise ValueError("orb_close_positions_wire_receipt_wait_elapsed") from exc
            await sleep(.25)


async def _attempt(
    service: OmsRiskService, target: _Target, event_id: UUID, clock: Callable[[], datetime],
    *, reason: str = "ORB_END_OF_DAY_CLOSE", start: time = _START,
    stop_context=None, attach_context=None,
) -> bool:
    """Normal and STOP reasons share one non-queued episode owner through commit."""
    controlled = bool(getattr(service.settings, "bot_control_enabled", False))
    from project_mai_tai.oms.webull_attach_riskoff import REASON as attach_reason
    riskoff = reason == attach_reason
    if riskoff and (attach_context is None or stop_context is not None
                    or attach_context.entry_id != target.entry_id):
        raise ValueError("orb_attach_exit_context_missing")
    if not riskoff and attach_context is not None:
        raise ValueError("orb_attach_exit_wrong_reason")
    if riskoff:
        start = time(9, 30)
    if reason == STOP_REASON and (not controlled or stop_context is None
            or not getattr(service.settings, "orb_country_routing_enabled", False)):
        raise ValueError("orb_stop_context_missing")
    key = (target.account, target.symbol, str(target.entry_id))
    lock = service.__dict__.setdefault("_orb_exit_claim_lock", Lock()) if controlled or riskoff else None
    if lock is not None:
        if not lock.acquire(blocking=False):
            return False
        try:
            claims = service.__dict__.setdefault("_orb_exit_claims", set())
            if key in claims:
                return False
            claims.add(key)
        finally:
            lock.release()
    try:
        if attach_context is not None and await attach_context.recover_existing_exit(target):
            return True  # Original identity read/accounting only, never another SELL.
        if reason == STOP_REASON and not await stop_context.claim(target, event_id):
            return False
        await _attempt_owned(service, target, event_id, clock, reason=reason, start=start,
                             stop_context=stop_context, attach_context=attach_context)
        return True
    finally:
        if lock is not None:
            with lock:
                service._orb_exit_claims.discard(key)


async def _attempt_owned(
    service: OmsRiskService, target: _Target, event_id: UUID, clock: Callable[[], datetime],
    *, reason: str, start: time, stop_context=None, attach_context=None,
) -> None:
    stopping = reason == STOP_REASON
    context = stop_context if stopping else attach_context
    protected = context is not None
    legacy_eod_receipt = {} if reason == "ORB_END_OF_DAY_CLOSE" and not protected else None
    now = clock()
    if protected:
        context.execution_metadata(target, now)  # Validate session/bid BEFORE releasing protection.
    def validate_quantity(session):
        from project_mai_tai.oms.orb_country import entry_quantity
        entry = session.get(BrokerOrder, target.entry_id)
        if (entry is None or entry_quantity(entry) != target.entry_quantity
                or not target.quantity.is_finite() or not 0 < target.quantity <= target.entry_quantity):
            raise ValueError("orb_close_entry_quantity_binding_invalid")
    await service._run_db(validate_quantity, commit=False)
    event = TradeIntentEvent(
        event_id=event_id,
        source_service="oms-risk",
        payload=TradeIntentPayload(
            strategy_code="orb_schwab", broker_account_name=target.account,
            symbol=target.symbol, side="sell", intent_type="close", quantity=target.quantity,
            reason=reason,
            metadata={
                "orb_schwab_eod": str(reason == "ORB_END_OF_DAY_CLOSE").lower(),
                "entry_order_id": str(target.entry_id),
                "entry_broker_order_id": target.broker_id,
                "order_type": "market", "time_in_force": "day",
            },
        ),
    )
    allowed, risk_reason = service._evaluate_risk(event)
    if not allowed:
        raise ValueError(f"risk_refused:{risk_reason}")
    def ownership_conflict(session):
        if attach_context is not None:
            return attach_context.ownership_conflict(session, target)
        if stopping:
            return stop_context.ownership_conflict(session, target)
        other_owner = session.scalar(select(VirtualPosition.id).where(
            VirtualPosition.broker_account_id == target.account_id,
            VirtualPosition.symbol == target.symbol,
            VirtualPosition.strategy_id != target.strategy_id,
            VirtualPosition.quantity != 0,
        ))
        other_order = session.scalar(select(BrokerOrder.id).where(
            BrokerOrder.broker_account_id == target.account_id,
            BrokerOrder.symbol == target.symbol,
            BrokerOrder.id != target.entry_id,
            BrokerOrder.status.in_(service.store.OPEN_ORDER_STATUSES),
        ))
        return other_owner is not None or other_order is not None

    if await service._run_db(ownership_conflict, commit=False):
        raise ValueError("other_owner_or_working_order")
    parent = await service.broker_adapter.fetch_order_update(OrderRequest(
        client_order_id=target.client_id, broker_account_name=target.account,
        strategy_code="orb_schwab", symbol=target.symbol, side="buy", intent_type="open",
        quantity=target.entry_quantity, reason="ORB_EOD_VERIFY_PARENT",
        metadata={"broker_order_id": target.broker_id, **({
            "orb_country_route": "true", "orb_country_broker": "webull",
        } if service.settings.provider_for_account(target.account) == "webull" else {})},
    ))
    parent_proven = bool(parent is not None and parent.broker_order_id == target.broker_id
                         and parent.event_type == "filled" and parent.filled_quantity == target.entry_quantity)
    if (target.routed or stopping) and parent is not None:
        def bought(session):
            rows = session.scalars(select(Fill).where(
                Fill.order_id == target.entry_id, Fill.side == "buy",
                Fill.strategy_id == target.strategy_id, Fill.broker_account_id == target.account_id,
                Fill.symbol == target.symbol, Fill.broker_fill_id.is_not(None),
            )).all()
            return sum((r.quantity for r in rows), Decimal(0))
        verified = await service._run_db(bought, commit=False)
        parent_proven = bool(parent.broker_order_id == target.broker_id
            and parent.client_order_id == target.client_id and parent.symbol == target.symbol
            and parent.side == "buy" and parent.origin == "broker"
            and parent.event_type in {"filled", "cancelled", "rejected"}
            and parent.reported_at.tzinfo is not None and parent.reported_at <= clock()
            and 0 < verified == parent.filled_quantity <= target.entry_quantity)
    if not parent_proven:
        raise ValueError("entry_fill_not_confirmed")
    async def child_closed() -> bool:
        from project_mai_tai.oms.service import oco_exit_client_order_id

        if protected and service.settings.provider_for_account(target.account) == "webull":
            await context.poll_children(target)
        else:
            await service._poll_orb_schwab_child_exits(entry_id=target.entry_id)
        def read(session):
            position = service.store.get_virtual_position(
                session, strategy_id=target.strategy_id,
                broker_account_id=target.account_id, symbol=target.symbol,
            )
            entry = session.get(BrokerOrder, target.entry_id)
            if position is None or position.quantity != 0 or entry is None:
                return False
            children = session.execute(select(Fill, BrokerOrder).join(
                BrokerOrder, BrokerOrder.id == Fill.order_id,
            ).where(or_(BrokerOrder.intent_id == entry.intent_id,
                        BrokerOrder.payload["entry_order_id"].as_string() == str(entry.id)), Fill.side == "sell",
                    Fill.broker_fill_id.is_not(None), Fill.broker_account_id == target.account_id,
                    Fill.strategy_id == target.strategy_id, Fill.symbol == target.symbol)).all()
            quantity = sum((fill.quantity for fill, child in children
                            if child.broker_order_id and (
                                child.client_order_id == oco_exit_client_order_id(target.client_id, child.broker_order_id)
                                or ((entry.payload or {}).get("orb_country_broker") == "webull"
                                    and (child.payload or {}).get("orbroute1_child") == "true"
                                    and (child.payload or {}).get("entry_order_id") == str(entry.id)))), Decimal("0"))
            return quantity >= target.quantity
        return await service._run_db(read, commit=False)

    # The native target/stop may already have sold inside the break bar while
    # the virtual position is still stale. Attribute its exact child fill first.
    if await child_closed() and not protected:
        await _finish(service, target, "resolved_by_child_fill", clock())
        return
    if not protected:
        await _broker_quantity(service, target, legacy_eod_receipt=legacy_eod_receipt)
    native_attempt = await stop_context.before_release(target) if stopping else None
    if service.settings.provider_for_account(target.account) == "webull":
        from project_mai_tai.oms.orb_country import release_protection
        if attach_context is not None:
            released = await release_protection(service, target.entry_id, attach_context=attach_context)
        else:
            released = await release_protection(service, target.entry_id, stop_context=stop_context) if stopping else (
                await release_protection(service, target.entry_id))
        release = "released" if released else "unanswerable"
    else:
        release = await service.broker_adapter.release_native_oco_for_close(target.account, target.broker_id)
    if stopping:
        await stop_context.after_release(native_attempt, release)
    if release == "resolved_by_fill":
        if not await child_closed():
            raise ValueError("child_fill_not_recorded")
        await _finish(service, target, "resolved_by_child_fill", clock())
        return
    if release != "released":
        raise ValueError("sell_pair_release_unconfirmed")
    released_at = monotonic()
    terminal_at = clock()
    if await child_closed():
        await _finish(service, target, "resolved_by_child_fill", clock())
        return
    # Reread after confirmed cancellation; an earlier position read cannot size this sell.
    if protected:
        quantity = await context.quantity_after_release(target, terminal_at)
    else:
        quantity = await _post_release_quantity(service, target, released_at, clock) if (
            service.settings.provider_for_account(target.account) == "webull"
        ) else await _broker_quantity(service, target, legacy_eod_receipt=legacy_eod_receipt)
    now = clock()
    if not stopping and not start <= now.astimezone(_ET).time() < _DEADLINE:
        raise ValueError("close_window_elapsed_after_release")
    event.payload.quantity = quantity
    if protected:
        event.payload.metadata.update(context.execution_metadata(target, now))
    if stopping:
        event.payload.metadata.update({"bot_control_command_id": str(stop_context.lease.command_id),
                                       "bot_control_generation": str(stop_context.lease.generation)})
    request = OrderRequest(
        client_order_id=service._build_client_order_id(event), broker_account_name=target.account,
        strategy_code="orb_schwab", symbol=target.symbol, side="sell", intent_type="close",
        quantity=quantity, reason=event.payload.reason, metadata=dict(event.payload.metadata),
        order_type=event.payload.metadata.get("order_type", "market"), time_in_force="day",
    )
    def prepare(session):
        entry = session.scalar(select(BrokerOrder).where(
            BrokerOrder.id == target.entry_id,
        ).with_for_update())
        strategy = session.get(Strategy, target.strategy_id)
        account = session.get(BrokerAccount, target.account_id)
        if entry is None or strategy is None or account is None:
            raise ValueError("entry_ownership_disappeared")
        if pending_generic_sell(session, entry):
            raise ValueError("orb_pending_generic_sell")
        if account.provider == "webull" and not protected:
            from project_mai_tai.oms.orb_country import _snapshot
            remaining = _snapshot(service, session, entry.id)[3]
            if remaining != quantity:
                raise ValueError("orb_owned_balance_changed_before_close")
        elif account.provider == "schwab" and not protected:
            remaining, unknown = _schwab_close_quantity(session, target,
                (legacy_eod_receipt or {}).get("broker_quantity", Decimal(0)),
                allow_legacy=legacy_eod_receipt is not None)
            if remaining != quantity:
                raise ValueError("orb_owned_balance_changed_before_close")
            if unknown is not None:
                _write_incident(session, target, unknown, now)
                service.logger.error("[OMS-ORB-SCHWAB-EOD] symbol=%s reason=%s", target.symbol, unknown)
        if protected:
            context.validate_prepared(session, target, quantity)
        intent = service.store.create_trade_intent(
            session, strategy=strategy, broker_account=account, event=event,
        )
        service.store.record_risk_check(
            session, intent=intent, strategy_id=target.strategy_id,
            broker_account_id=target.account_id, outcome="pass", reason="orb_eod_owned_released",
        )
        if protected and session.scalar(select(BrokerOrder.id).where(
            BrokerOrder.client_order_id == request.client_order_id,
        )) is not None:
            raise ValueError("orb_stop_existing_exit_not_unsent")
        prepared_order = service.store.get_or_create_order(
            session, intent=intent, strategy_id=target.strategy_id,
            broker_account_id=target.account_id, client_order_id=request.client_order_id,
            symbol=target.symbol, side="sell", quantity=quantity, metadata=request.metadata,
        )
        if protected:
            # New STOP row only: factory's historical timestamp default is not a wire.
            prepared_order.submitted_at = None
        entry.payload = {**(entry.payload or {}), _STATE: {
            **(dict((entry.payload or {}).get(_STATE) or {}) if protected else {}),
            "phase": "submitting", "event_id": str(event_id), "updated_at": now.isoformat(),
            **({"exit_order_id": str(prepared_order.id), "exit_coid": request.client_order_id,
                "exit_intent_id": str(intent.id)} if protected else {}),
        }}
        # Commit the attempt BEFORE POST. An uncertain response must never authorize a retry.
        return intent.id

    intent_id = await service._run_db(prepare)
    exit_attempt = await stop_context.before_submit(target, request) if stopping else None
    if protected:
        await context.validate_wire(target, request)
        # Recheck quote/session after journal awaits; no newly priced second order.
        if context.execution_metadata(target, clock()) != {
            key: request.metadata[key] for key in ("order_type", "session", "extended_hours", "limit_price")
            if key in request.metadata
        }:
            raise ValueError("orb_stop_quote_or_session_changed_before_wire")
    elif not start <= clock().astimezone(_ET).time() < _DEADLINE:
        raise ValueError("close_window_elapsed_before_submit")
    reports = await service.broker_adapter.submit_order(request)
    if protected and any(
        report.origin != "broker" or not report.filled_quantity.is_finite()
        or report.filled_quantity < 0 or report.quantity != quantity
        or report.reported_at.tzinfo is None or report.reported_at > clock()
        or (report.filled_quantity > 0 and (not report.broker_fill_id or report.fill_price is None
            or not report.fill_price.is_finite() or report.fill_price <= 0))
        for report in reports
    ):
        raise ValueError("orb_stop_exit_execution_unknown")
    if not reports or any(
        report.client_order_id != request.client_order_id
        or report.symbol != target.symbol or report.side != "sell"
        or report.intent_type != "close" or report.filled_quantity > quantity
        for report in reports
    ):
        raise ValueError("close_response_unknown")
    with service.session_factory() as session:
        intent = session.get(TradeIntent, intent_id)
        events = await service._record_order_reports(
            session=session, intent=intent, strategy_id=target.strategy_id,
            broker_account_id=target.account_id, intent_event=event, request=request, reports=reports,
        )
        session.commit()
    for order_event in events:
        await service._publish_order_event(order_event)
    if stopping:
        await stop_context.after_submit(exit_attempt, request, reports)
    if not any(r.event_type in {"accepted", "filled", "partially_filled"} for r in reports):
        if all(r.event_type == "rejected" and r.origin == "broker" for r in reports):
            raise ValueError("close_rejected_after_pair_release")
        raise ValueError("close_response_unknown_after_pair_release")
    if not reports[-1].broker_order_id or reports[-1].event_type in {"rejected", "cancelled"}:
        raise ValueError("close_not_confirmed_working_or_filled")
    await _finish(service, target, "submitted", clock())
    service.logger.info(
        "[OMS-ORB-SCHWAB-EOD] symbol=%s parent=%s status=SUBMITTED qty=%s broker_id=%s",
        target.symbol, target.broker_id, quantity, reports[-1].broker_order_id,
    )


async def close_orb_schwab_on_signal(
    service: OmsRiskService, event: TradeIntentEvent, *, clock: Callable[[], datetime]
) -> None:
    """Body/ATR exits and EOD share the same durable, single-sell claim."""
    if not service.settings.orb_live_schwab_orders_enabled:
        return
    now = clock()

    def validate_and_claim(session):
        entry = session.scalar(select(BrokerOrder).where(
            BrokerOrder.id == UUID(event.payload.metadata["entry_order_id"]),
        ).with_for_update())
        if entry is None or entry.side != "buy" or entry.symbol != event.payload.symbol:
            raise ValueError("exit_entry_identity_mismatch")
        strategy = session.get(Strategy, entry.strategy_id)
        account = session.get(BrokerAccount, entry.broker_account_id)
        if strategy.code != "orb_schwab" or account.name != event.payload.broker_account_name:
            raise ValueError("exit_account_or_strategy_mismatch")
        latest_id = session.scalar(select(BrokerOrder.id).where(
            BrokerOrder.strategy_id == entry.strategy_id,
            BrokerOrder.broker_account_id == entry.broker_account_id,
            BrokerOrder.symbol == entry.symbol, BrokerOrder.side == "buy",
        ).order_by(BrokerOrder.submitted_at.desc(), BrokerOrder.id.desc()).limit(1))
        if latest_id != entry.id:
            raise ValueError("exit_is_for_an_older_trip")
        fill = session.get(Fill, UUID(event.payload.metadata["entry_fill_id"]))
        if (fill is None or fill.order_id != entry.id or fill.side != "buy" or not fill.broker_fill_id
                or fill.quantity <= 0 or fill.symbol != entry.symbol
                or fill.strategy_id != entry.strategy_id or fill.broker_account_id != entry.broker_account_id):
            raise ValueError("exit_requires_exact_broker_buy_fill")
        context = dict((entry.payload or {}).get(CONTEXT_KEY) or {})
        fill_time = confirmed_entry_time(fill)
        if fill_time is None or context.get("fill_id") != str(fill.id) or context.get("fill_at") != fill_time.isoformat():
            raise ValueError("exit_context_fill_mismatch")
        reason, decision_at = exit_signal(context, now)
        if reason != event.payload.reason or decision_at is None:
            raise ValueError("exit_rule_not_satisfied")
        quote = service._latest_quotes_by_symbol.get(entry.symbol) or {}
        quote_at = quote.get("received_at")
        bid = Decimal(str(quote.get("bid", "0")))
        if not (
            isinstance(quote_at, datetime) and quote_at.tzinfo is not None
            and quote_at >= decision_at and 0 <= (now - quote_at).total_seconds() <= 5
            and bid.is_finite() and bid > 0
        ):
            # Do not consume the one-shot while waiting for an executable post-decision bid.
            return None, None
        position = service.store.get_virtual_position(
            session, strategy_id=entry.strategy_id, broker_account_id=entry.broker_account_id,
            symbol=entry.symbol,
        )
        from project_mai_tai.oms.orb_country import entry_quantity
        expected_quantity = entry_quantity(entry)
        if position is None or not 0 < position.quantity <= expected_quantity or (entry.payload or {}).get(_STATE):
            return None, None
        routed = (entry.payload or {}).get("orb_country_route") == "true"
        if entry.status != "filled" and not (routed and entry.status in {"cancelled", "rejected", "expired"}):
            # Never release a partially filled parent's protection while its buy
            # remainder can still execute. A later completed fill can retry.
            return None, None
        if pending_generic_sell(session, entry):
            service.logger.warning("[OMS-ORB-SCHWAB-EOD] entry=%s status=BLOCKED reason=orb_pending_generic_sell",
                                   entry.id)
            return None, None
        target = _Target(entry.id, entry.strategy_id, entry.broker_account_id, account.name,
                         entry.symbol, entry.broker_order_id or "", entry.client_order_id, position.quantity,
                         expected_quantity, routed)
        event_id = uuid4()
        entry.payload = {**(entry.payload or {}), _STATE: {
            "phase": "checking", "event_id": str(event_id), "reason": reason, "updated_at": now.isoformat(),
        }}
        return target, event_id

    target, event_id = await service._run_db(validate_and_claim)
    if target is None:
        return
    try:
        await _attempt(service, target, event_id, clock, reason=event.payload.reason, start=time(9, 30))
    except Exception as exc:
        await _finish(service, target, "needs_attention", clock())
        await _incident(service, target, str(exc), clock())


async def close_orb_schwab_before_close(
    service: OmsRiskService, *, clock: Callable[[], datetime]
) -> None:
    now = clock()
    local = now.astimezone(_ET)
    if not service.settings.orb_live_schwab_orders_enabled or local.weekday() >= 5:
        return
    if local.time() < _START:
        return
    def load_targets(session):
        positions = session.execute(
            select(VirtualPosition, BrokerAccount.name)
            .join(Strategy, Strategy.id == VirtualPosition.strategy_id)
            .join(BrokerAccount, BrokerAccount.id == VirtualPosition.broker_account_id)
            .where(
                Strategy.code == "orb_schwab", VirtualPosition.quantity > 0,
                BrokerAccount.name.in_({service.settings.strategy_schwab_1m_v2_account_name,
                                       service.settings.orb_webull_account_name}),
            )
        ).all()
        work = []
        for position, account in positions:
            entry = session.scalar(select(BrokerOrder).where(
                BrokerOrder.strategy_id == position.strategy_id,
                BrokerOrder.broker_account_id == position.broker_account_id,
                BrokerOrder.symbol == position.symbol, BrokerOrder.side == "buy",
                or_(service.settings.provider_for_account(account) != "schwab",
                    BrokerOrder.status.in_({"filled", "partially_filled"}),
                    select(Fill.id).where(Fill.order_id == BrokerOrder.id,
                        Fill.side == "buy", Fill.quantity > 0).exists()),
            ).order_by(BrokerOrder.submitted_at.desc()).limit(1))
            if entry is None:
                work.append(_Target(
                    position.id, position.strategy_id, position.broker_account_id,
                    account, position.symbol, "", "", position.quantity,
                ))
                continue
            work.append(_Target(
                entry.id, entry.strategy_id, entry.broker_account_id, account, entry.symbol,
                entry.broker_order_id or "", entry.client_order_id, position.quantity,
                entry.quantity,
                (entry.payload or {}).get("orb_country_route") == "true",
            ))
        return work

    for target in await service._run_db(load_targets, commit=False):
        if not target.broker_id:
            await _incident(service, target, "owned_entry_broker_id_missing", now)
            continue
        if local.time() >= _DEADLINE:
            await _incident(service, target, "position_unresolved_at_session_close", now)
            continue

        def claim(session, target=target):
            entry = session.scalar(select(BrokerOrder).where(
                BrokerOrder.id == target.entry_id
            ).with_for_update())
            if entry is not None and pending_generic_sell(session, entry):
                return "generic_sell_pending", None
            state = (entry.payload or {}).get(_STATE)
            if state:
                phase = state.get("phase", "unknown") if isinstance(state, dict) else "unknown"
                return phase, None
            event_id = uuid4()
            entry.payload = {**(entry.payload or {}), _STATE: {
                "phase": "checking", "event_id": str(event_id), "updated_at": now.isoformat(),
            }}
            return None, event_id

        existing_phase, event_id = await service._run_db(claim)
        if existing_phase is not None:
            if existing_phase != "submitted" or local.time() >= time(15, 59):
                await _incident(service, target, "prior_attempt_unresolved_no_resubmit", now)
            continue
        try:
            await _attempt(service, target, event_id, clock)
        except Exception as exc:
            await _finish(service, target, "needs_attention", clock())
            await _incident(service, target, str(exc), clock())
