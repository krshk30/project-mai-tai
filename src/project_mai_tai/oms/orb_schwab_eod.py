"""One durable, broker-confirmed close attempt for a residual live ORB position."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time
from decimal import Decimal
from typing import TYPE_CHECKING, Callable
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from sqlalchemy import select

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


async def _incident(service: OmsRiskService, target: _Target, reason: str, now: datetime) -> None:
    def write(session):
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

    if not await service._run_db(write):
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


async def _broker_quantity(service: OmsRiskService, target: _Target) -> Decimal:
    positions = await service.broker_adapter.list_account_positions(target.account)
    matches = [p for p in positions if p.symbol.upper() == target.symbol]
    if len(matches) != 1 or matches[0].broker_account_name != target.account:
        raise ValueError("broker_position_not_positively_identified")
    quantity = matches[0].quantity
    if not quantity.is_finite() or not 0 < quantity <= target.quantity <= 2:
        raise ValueError("broker_position_not_within_orb_ownership")
    if quantity != quantity.to_integral_value():
        raise ValueError("broker_position_not_whole_shares")
    return quantity


async def _attempt(
    service: OmsRiskService, target: _Target, event_id: UUID, clock: Callable[[], datetime],
    *, reason: str = "ORB_END_OF_DAY_CLOSE", start: time = _START,
) -> None:
    now = clock()
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
        quantity=Decimal("2"), reason="ORB_EOD_VERIFY_PARENT",
        metadata={"broker_order_id": target.broker_id},
    ))
    if (
        parent is None or parent.broker_order_id != target.broker_id
        or parent.event_type != "filled" or parent.filled_quantity != 2
    ):
        raise ValueError("entry_fill_not_confirmed")
    await _broker_quantity(service, target)
    release = await service.broker_adapter.release_native_oco_for_close(target.account, target.broker_id)
    if release == "resolved_by_fill":
        await service._poll_orb_schwab_child_exits()
        def child_closed(session):
            position = service.store.get_virtual_position(
                session, strategy_id=target.strategy_id,
                broker_account_id=target.account_id, symbol=target.symbol,
            )
            return position is not None and position.quantity == 0

        if not await service._run_db(child_closed, commit=False):
            raise ValueError("child_fill_not_recorded")
        await _finish(service, target, "resolved_by_child_fill", clock())
        return
    if release != "released":
        raise ValueError("sell_pair_release_unconfirmed")
    # Reread after confirmed cancellation; an earlier position read cannot size this sell.
    quantity = await _broker_quantity(service, target)
    now = clock()
    if not start <= now.astimezone(_ET).time() < _DEADLINE:
        raise ValueError("close_window_elapsed_after_release")
    event.payload.quantity = quantity
    request = OrderRequest(
        client_order_id=service._build_client_order_id(event), broker_account_name=target.account,
        strategy_code="orb_schwab", symbol=target.symbol, side="sell", intent_type="close",
        quantity=quantity, reason=event.payload.reason, metadata=dict(event.payload.metadata),
        order_type="market", time_in_force="day",
    )
    def prepare(session):
        entry = session.get(BrokerOrder, target.entry_id)
        strategy = session.get(Strategy, target.strategy_id)
        account = session.get(BrokerAccount, target.account_id)
        if entry is None or strategy is None or account is None:
            raise ValueError("entry_ownership_disappeared")
        intent = service.store.create_trade_intent(
            session, strategy=strategy, broker_account=account, event=event,
        )
        service.store.record_risk_check(
            session, intent=intent, strategy_id=target.strategy_id,
            broker_account_id=target.account_id, outcome="pass", reason="orb_eod_owned_released",
        )
        service.store.get_or_create_order(
            session, intent=intent, strategy_id=target.strategy_id,
            broker_account_id=target.account_id, client_order_id=request.client_order_id,
            symbol=target.symbol, side="sell", quantity=quantity, metadata=request.metadata,
        )
        entry.payload = {**(entry.payload or {}), _STATE: {
            "phase": "submitting", "event_id": str(event_id), "updated_at": now.isoformat(),
        }}
        # Commit the attempt BEFORE POST. An uncertain response must never authorize a retry.
        return intent.id

    intent_id = await service._run_db(prepare)
    if not start <= clock().astimezone(_ET).time() < _DEADLINE:
        raise ValueError("close_window_elapsed_before_submit")
    reports = await service.broker_adapter.submit_order(request)
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
        if position is None or not 0 < position.quantity <= 2 or (entry.payload or {}).get(_STATE):
            return None, None
        if entry.status != "filled":
            # Never release a partially filled parent's protection while its buy
            # remainder can still execute. A later completed fill can retry.
            return None, None
        target = _Target(entry.id, entry.strategy_id, entry.broker_account_id, account.name,
                         entry.symbol, entry.broker_order_id or "", entry.client_order_id, position.quantity)
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
                BrokerAccount.name == service.settings.strategy_schwab_1m_v2_account_name,
            )
        ).all()
        work = []
        for position, account in positions:
            entry = session.scalar(select(BrokerOrder).where(
                BrokerOrder.strategy_id == position.strategy_id,
                BrokerOrder.broker_account_id == position.broker_account_id,
                BrokerOrder.symbol == position.symbol, BrokerOrder.side == "buy",
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
