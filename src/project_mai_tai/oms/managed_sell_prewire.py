"""Durable normal ATR exit reservations; never submit or recover broker orders."""

from decimal import Decimal
from uuid import UUID

from sqlalchemy import or_, select

from project_mai_tai.db.models import (
    BrokerAccount, BrokerOrder, BrokerOrderEvent, OmsManagedPosition, Strategy, TradeIntent, VirtualPosition,
)


TERMINAL = ("filled", "cancelled", "canceled", "rejected", "expired", "aborted")


def read_managed_sell_quantity(service, session, *, managed_row_id, strategy_code,
                               broker_account_name, symbol, target_context=None,
                               allow_pending_fill_writer=False, locked_row=None):
    """Shared session-bound ownership read; absent VP retains managed-only policy.

    Preparation supplies its locked row. Other callers use their own fresh worker
    session, never a caller's cached positive ownership result.
    """
    from project_mai_tai.oms.service import _BoundOcoHistoryUnknown

    current = locked_row if locked_row is not None else service.store.get_open_managed_position(
        session, broker_account_name=broker_account_name, symbol=symbol,
    )
    if (strategy_code != "schwab_1m_v2" or current is None or current.status != "open"
            or str(current.id) != managed_row_id or current.strategy_code != strategy_code
            or current.broker_account_name != broker_account_name or current.symbol != symbol):
        return 0, None, "episode_or_owner_changed"
    if not service._cw_target_binding_matches(current, target_context):
        return 0, None, "target_binding_changed"
    pending = service.__dict__.get("_oco_exit_fill_pending", {}).get((broker_account_name, symbol))
    if pending is not None and pending.row_id == managed_row_id and not allow_pending_fill_writer:
        return 0, None, "same_episode_oco_exit_pending"
    strategy = session.scalar(select(Strategy).where(Strategy.code == strategy_code))
    account = session.scalar(select(BrokerAccount).where(BrokerAccount.name == broker_account_name))
    if strategy is None or account is None:
        return 0, None, "missing_strategy_or_account"
    owned = service.store.get_virtual_position(
        session, strategy_id=strategy.id, broker_account_id=account.id, symbol=symbol,
    )
    ledger_quantity = None
    if owned is not None:
        try:
            ledger_quantity = Decimal(owned.quantity)
        except (ValueError, TypeError, ArithmeticError):
            return 0, None, "invalid_own_quantity"
        if not ledger_quantity.is_finite() or ledger_quantity < 0:
            return 0, None, "invalid_own_quantity"
    managed_quantity = int(current.current_quantity)
    try:
        native = service._stored_owned_oco_balance(session, current)
    except _BoundOcoHistoryUnknown as exc:
        return 0, None, f"bound_oco_history_unknown:{exc}"
    if native is not None:
        _, entered, exited = native
        managed_quantity = min(managed_quantity, max(0, int(entered - exited)))
    return managed_quantity, ledger_quantity, ""


async def prepare_managed_sell(service, *, event, request, managed_row_id, now,
                               retry_intent_id=None, admitted_intent_id=None,
                               target_context=None, allow_pending_fill_writer=False):
    """Return a committed intent ID, or None; existing/unknown exits never redispatch.

    The existing caller retains its release, quantity and in-memory owner guards.
    This fresh worker transaction closes the durable reservation gap, including
    the gap between two callers' earlier ownership snapshots.
    """
    if (request.strategy_code != "schwab_1m_v2" or request.side != "sell"
            or request.intent_type not in {"close", "scale"}
            or not request.quantity.is_finite() or request.quantity <= 0
            or now.tzinfo is None):
        raise ValueError("managed_sell_prewire_invalid_request")
    if (event.payload.strategy_code != request.strategy_code
            or event.payload.broker_account_name != request.broker_account_name
            or event.payload.symbol != request.symbol or event.payload.side != request.side
            or event.payload.quantity != request.quantity
            or event.payload.intent_type != request.intent_type):
        raise ValueError("managed_sell_prewire_event_changed")
    if retry_intent_id is not None and admitted_intent_id is not None:
        raise ValueError("managed_sell_prewire_ambiguous_intent")

    def prepare(unit):
        row = unit.scalar(select(OmsManagedPosition).where(
            OmsManagedPosition.id == UUID(managed_row_id),
        ).with_for_update())
        strategy = unit.scalar(select(Strategy).where(Strategy.code == request.strategy_code))
        account = unit.scalar(select(BrokerAccount).where(BrokerAccount.name == request.broker_account_name))
        if (row is None or strategy is None or account is None or row.status != "open"
                or row.strategy_code != request.strategy_code
                or row.broker_account_name != request.broker_account_name
                or row.symbol != request.symbol
                or Decimal(row.current_quantity) < request.quantity):
            return None
        managed_quantity, ledger_quantity, refusal = read_managed_sell_quantity(
            service, unit, managed_row_id=managed_row_id, strategy_code=request.strategy_code,
            broker_account_name=request.broker_account_name, symbol=request.symbol,
            target_context=request.metadata if target_context is None else target_context,
            allow_pending_fill_writer=allow_pending_fill_writer,
            locked_row=row,
        )
        if (refusal or managed_quantity < request.quantity
                or ledger_quantity is not None and ledger_quantity < request.quantity):
            return None
        if row.entry_order_id is not None:
            entry_id = str(row.entry_order_id)
            if request.metadata.get("entry_order_id") not in (None, entry_id):
                return None
            request.metadata["entry_order_id"] = entry_id
            event.payload.metadata["entry_order_id"] = entry_id
        # A recovered or redelivered coid is reconciliation work, never a new POST.
        if unit.scalar(select(BrokerOrder.id).where(
            BrokerOrder.client_order_id == request.client_order_id,
        )) is not None:
            return None
        occupied = unit.scalars(select(BrokerOrder).where(
            BrokerOrder.strategy_id == strategy.id,
            BrokerOrder.broker_account_id == account.id,
            BrokerOrder.symbol == request.symbol, BrokerOrder.side == "sell",
            or_(BrokerOrder.status.is_(None), BrokerOrder.status.not_in(TERMINAL)),
        )).all()
        for order in occupied:
            payload = order.payload or {}
            if (payload.get("oms_v2_managed_exit") == "true"
                    or payload.get("managed_sell_prepared_at")):
                # No terminal proof: retain the existing seller across await/crash.
                # Previous-session archival/reconciliation is separate, not a POST.
                return None
        if admitted_intent_id is not None:
            intent = unit.get(TradeIntent, admitted_intent_id)
            if (intent is None or intent.strategy_id != strategy.id or intent.broker_account_id != account.id
                    or intent.symbol != request.symbol or intent.side != "sell"
                    or intent.intent_type != request.intent_type or intent.quantity < request.quantity
                    or intent.status in (*TERMINAL, "completed", "superseded")):
                return None
            intent.payload = {**(intent.payload or {}), "metadata": dict(request.metadata)}
        elif retry_intent_id is None:
            intent = service.store.create_trade_intent(
                unit, strategy=strategy, broker_account=account, event=event,
            )
            service._record_internal_risk_pass(
                unit, intent=intent, strategy=strategy, broker_account=account,
                metadata=dict(request.metadata), reason="oms_v2_managed_exit",
            )
        else:
            intent = unit.get(TradeIntent, retry_intent_id)
            refresh_of = request.metadata.get("managed_sell_refresh_of")
            corrected = request.metadata.get("confirmation_corrected_retry") == "true"
            if refresh_of:
                predecessor_coid = refresh_of
            elif corrected:
                predecessor_coid = request.metadata.get("confirmation_corrected_from_client_order_id")
            else:
                predecessor_coid = request.metadata.get("reserve1_retry_of")
            terminal = {"cancelled", "canceled"} if refresh_of else {"rejected"}
            predecessor = unit.scalar(select(BrokerOrder).where(
                BrokerOrder.client_order_id == predecessor_coid,
            ))
            if (intent is None or predecessor is None or predecessor.intent_id != intent.id
                    or predecessor.status not in terminal or predecessor.side != "sell"
                    or not (refresh_of or corrected or request.metadata.get("reserve1_retry") == "1")
                    or predecessor.strategy_id != strategy.id
                    or predecessor.broker_account_id != account.id
                    or predecessor.symbol != request.symbol or predecessor.quantity < request.quantity
                    or intent.strategy_id != strategy.id or intent.broker_account_id != account.id
                    or intent.symbol != request.symbol or intent.side != "sell"
                    or intent.quantity < request.quantity or intent.intent_type != request.intent_type
                    or (not refresh_of and (predecessor.quantity != request.quantity
                                           or intent.quantity != request.quantity))
                    or (predecessor.payload or {}).get("oms_managed_row_id") != managed_row_id):
                return None
        order = service.store.get_or_create_order(
            unit, intent=intent, strategy_id=strategy.id, broker_account_id=account.id,
            client_order_id=request.client_order_id, symbol=request.symbol, side="sell",
            quantity=request.quantity, metadata=dict(request.metadata), status="pending",
            order_type=request.order_type, time_in_force=request.time_in_force,
        )
        order.submitted_at = None  # Preparation is not evidence of a broker submission.
        return intent.id

    request.metadata.update(oms_managed_row_id=managed_row_id,
                            managed_sell_prepared_at=now.isoformat())
    event.payload.metadata.update(request.metadata)
    intent_id = await service._run_db(prepare)
    if intent_id is None:
        service.logger.critical(
            "[MANAGED-SELL-PREWIRE-REFUSED] account=%s symbol=%s row_id=%s coid=%s sell=0",
            request.broker_account_name, request.symbol, managed_row_id, request.client_order_id,
        )
    return intent_id


async def abandon_unposted_sell(service, *, request, intent_id, now):
    """Only the creating caller, BEFORE its first broker await, can use this path.

    Never call this after submit, timeout, cancellation of a broker await, or on
    restored pending rows. Those are unknown outcomes and require broker proof.
    """
    def abandon(unit):
        row = unit.scalar(select(BrokerOrder).where(
            BrokerOrder.client_order_id == request.client_order_id,
        ).with_for_update())
        if (row is None or row.intent_id != intent_id or row.side != "sell"
                or row.symbol != request.symbol or row.quantity != request.quantity
                or row.status != "pending" or row.submitted_at is not None or row.broker_order_id
                or not request.metadata.get("managed_sell_prepared_at")
                or (row.payload or {}).get("managed_sell_prepared_at")
                    != request.metadata["managed_sell_prepared_at"]):
            raise ValueError("managed_sell_not_proven_unposted")
        event = BrokerOrderEvent(order_id=row.id, event_type="aborted", event_source="client",
            event_at=now, payload={"reason": "session_changed_before_first_wire",
                "proven_not_sent": True, "client_order_id": request.client_order_id,
                "metadata": dict(row.payload or {})})
        unit.add(event)
        row.status = "aborted"
        intent = unit.get(TradeIntent, intent_id)
        if intent is not None and intent.status == "pending":
            intent.status = "aborted"
        unit.flush()
        return event.id
    return await service._run_db(abandon)


async def prepare_orb_protected_sell(service, *, event, request, admitted_intent_id, now):
    """Persist only the existing private RESERVE1 dispatch, never an external ORB close."""
    if (request.strategy_code != "orb_schwab" or request.side != "sell" or request.intent_type != "close"
            or not request.quantity.is_finite() or request.quantity <= 0 or now.tzinfo is None):
        raise ValueError("orb_protected_sell_prewire_invalid_request")
    episode_id = request.metadata.get("managed_sell_owner_episode_id", "")
    entry_id = request.metadata.get("managed_sell_entry_order_id", "")
    claim = (request.broker_account_name, request.symbol, episode_id)
    if claim not in service.__dict__.get("_reserve1_sell_inflight", set()):
        return None
    if (event.payload.strategy_code != request.strategy_code
            or event.payload.broker_account_name != request.broker_account_name
            or event.payload.symbol != request.symbol or event.payload.side != "sell"
            or event.payload.intent_type != "close" or event.payload.quantity != request.quantity):
        raise ValueError("orb_protected_sell_prewire_event_changed")

    def prepare(unit):
        from project_mai_tai.oms.orb_schwab_eod import _STATE
        entry = unit.scalar(select(BrokerOrder).where(BrokerOrder.id == UUID(entry_id)).with_for_update())
        position = unit.get(VirtualPosition, UUID(episode_id))
        intent = unit.get(TradeIntent, admitted_intent_id)
        if entry is None or position is None or intent is None:
            return None
        account = unit.get(BrokerAccount, entry.broker_account_id)
        strategy = unit.get(Strategy, entry.strategy_id)
        if (strategy is None or account is None or strategy.code != "orb_schwab"
                or account.name != request.broker_account_name or account.provider != "schwab"
                or entry.side != "buy" or entry.symbol != request.symbol or not entry.broker_order_id
                or entry.status != "filled" or (entry.payload or {}).get(_STATE)
                or position.strategy_id != entry.strategy_id
                or position.broker_account_id != entry.broker_account_id
                or position.symbol != request.symbol or position.quantity != request.quantity
                or intent.strategy_id != entry.strategy_id or intent.broker_account_id != entry.broker_account_id
                or intent.symbol != request.symbol or intent.side != "sell"
                or intent.intent_type != "close" or intent.quantity < request.quantity
                or intent.status in (*TERMINAL, "completed", "superseded")):
            return None
        from project_mai_tai.oms.orb_country import entry_quantity
        if request.quantity > entry_quantity(entry):
            return None
        if unit.scalar(select(BrokerOrder.id).where(
            BrokerOrder.client_order_id == request.client_order_id,
        )) is not None:
            return None
        occupied = unit.scalars(select(BrokerOrder).where(
            BrokerOrder.strategy_id == entry.strategy_id,
            BrokerOrder.broker_account_id == entry.broker_account_id,
            BrokerOrder.symbol == entry.symbol, BrokerOrder.side == "sell",
            or_(BrokerOrder.status.is_(None), BrokerOrder.status.not_in(TERMINAL)),
        )).all()
        if any((order.payload or {}).get("managed_sell_prepared_at")
               or (order.payload or {}).get("entry_order_id") == str(entry.id) for order in occupied):
            return None
        intent.payload = {**(intent.payload or {}), "metadata": dict(request.metadata)}
        order = service.store.get_or_create_order(
            unit, intent=intent, strategy_id=entry.strategy_id, broker_account_id=entry.broker_account_id,
            client_order_id=request.client_order_id, symbol=request.symbol, side="sell",
            quantity=request.quantity, metadata=dict(request.metadata), status="pending",
            order_type=request.order_type, time_in_force=request.time_in_force,
        )
        order.submitted_at = None
        return intent.id

    request.metadata["managed_sell_prepared_at"] = now.isoformat()
    intent_id = await service._run_db(prepare)
    if intent_id is None:
        service.logger.critical(
            "[MANAGED-SELL-PREWIRE-REFUSED] strategy=orb_schwab account=%s symbol=%s entry=%s sell=0",
            request.broker_account_name, request.symbol, entry_id,
        )
    return intent_id
