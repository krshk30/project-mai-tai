"""C6-alone legacy owners: cancel own entries, never sell held exposure."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from hashlib import sha256
import json

from sqlalchemy import or_, select

from project_mai_tai.broker_adapters.protocols import OrderRequest
from project_mai_tai.db.models import (
    BotControlAttempt, BrokerAccount, BrokerOrder, Fill, OmsManagedPosition,
    Strategy, SystemIncident, TradeIntent, VirtualPosition,
)
from project_mai_tai.oms.bot_control import ControlConflict
from project_mai_tai.oms.bot_control_executor import StopProgress
from project_mai_tai.oms.bot_control_scope import controlled_live_scope

TERMINAL = frozenset({"filled", "cancelled", "canceled", "rejected", "expired"})
READABLE = TERMINAL | {"accepted", "working", "partially_filled"}
BOUND = 5000
HELD_MESSAGE = "STOP: legacy held position - sell manually or wait for normal exit"


def _number(value):
    if not isinstance(value, Decimal) or not value.is_finite() or value < 0:
        raise ControlConflict("legacy_owned_quantity_UNKNOWN")
    return value


def _hash(value):
    return sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def _identity(order):
    return (order.id, order.strategy_id, order.broker_account_id, order.intent_id,
            order.client_order_id, order.broker_order_id, order.symbol,
            order.side, order.quantity, order.order_type, order.time_in_force,
            (order.payload or {}).get("bracket"), (order.payload or {}).get("native_oco_bracket"))


async def cancel_owned_entry(service, lease, journal, order_id, code):
    def read(unit):
        order = unit.get(BrokerOrder, order_id)
        account = unit.get(BrokerAccount, order.broker_account_id) if order else None
        intent = unit.get(TradeIntent, order.intent_id) if order and order.intent_id else None
        strategy = unit.get(Strategy, lease.strategy_id)
        if (order is None or account is None or strategy is None or strategy.code != code
                or not controlled_live_scope(strategy.code, strategy.execution_mode, account.name)
                or account.provider not in {"schwab", "webull"}
                or order.strategy_id != lease.strategy_id or account.id not in lease.account_ids
                or order.side != "buy" or not order.client_order_id or not order.broker_order_id
                or intent is None or intent.strategy_id != lease.strategy_id
                or intent.broker_account_id != account.id or intent.symbol != order.symbol
                or intent.side != "buy" or intent.intent_type not in {"open", "scale"}):
            raise ControlConflict("legacy_cancel_entry_identity_UNKNOWN")
        if _number(order.quantity) <= 0:
            raise ControlConflict("legacy_cancel_entry_quantity_UNKNOWN")
        return order, account.name, account.provider
    order, account, provider = await service._run_db(read, commit=False)
    binding = _identity(order)
    attempt = await asyncio.to_thread(journal.prepare, lease, order_id=order.id,
        operation="CANCEL_ENTRY", sequence_no=0,
        owner_hash=_hash([str(value) for value in binding]),
        owner_revision=lease.generation, now=datetime.now(UTC))
    if attempt.status == "TERMINAL":
        return
    request = OrderRequest(order.client_order_id, account, code, order.symbol, "buy", "open",
        order.quantity, "KILLSW1_READ_ENTRY",
        {**(order.payload or {}), "broker_order_id": order.broker_order_id},
        order.order_type, order.time_in_force)

    async def observe(*, before_dispatch=False):
        report = await service.broker_adapter.fetch_order_update(request)
        if (report is None or report.origin != "broker"
                or report.client_order_id != order.client_order_id
                or report.broker_order_id != order.broker_order_id or report.symbol != order.symbol
                or report.side != "buy" or report.quantity != order.quantity
                or report.event_type not in READABLE or report.reported_at.tzinfo is None
                or report.reported_at > datetime.now(UTC)
                or not isinstance(report.filled_quantity, Decimal)
                or not report.filled_quantity.is_finite()
                or not 0 <= report.filled_quantity <= order.quantity
                or (report.event_type == "filled" and report.filled_quantity != order.quantity)
                or (report.event_type == "partially_filled" and not 0 < report.filled_quantity < order.quantity)):
            raise ControlConflict("legacy_cancel_entry_read_UNKNOWN")
        if provider == "schwab" and report.event_type not in TERMINAL and report.filled_quantity > 0:
            flags = [(order.payload or {}).get(key) for key in ("bracket", "native_oco_bracket")]
            known_bare = all(str(value).lower() in {"0", "false", "no"} for value in flags)
            if not known_bare:
                # Parent DELETE has no proven child-preservation contract.
                # Missing shape provenance is UNKNOWN, not proof of a bare BUY.
                raise ControlConflict("legacy_partial_native_parent_protection_UNPROVEN")
        def record(unit):
            current = unit.get(BrokerOrder, order.id)
            if current is None or _identity(current) != binding:
                raise ControlConflict("legacy_cancel_entry_owner_changed")
            fills = unit.scalars(select(Fill).where(Fill.order_id == order.id)).all()
            if any(fill.strategy_id != lease.strategy_id
                   or fill.broker_account_id != order.broker_account_id
                   or fill.symbol != order.symbol or fill.side != "buy"
                   or not fill.broker_fill_id for fill in fills):
                raise ControlConflict("legacy_cancel_entry_fill_binding_UNKNOWN")
            quantity = sum((_number(fill.quantity) for fill in fills), Decimal(0))
            if quantity > report.filled_quantity:
                raise ControlConflict("legacy_cancel_entry_fill_regressed")
            matched = quantity == report.filled_quantity
            if matched:
                current.status = report.event_type
            return matched
        matched = await service._run_db(record)
        if not matched:
            # Cancel a proven working remainder, but leave fill attribution to
            # the normal writer. Never infer held-flat from the cancel ACK.
            if before_dispatch and report.event_type not in TERMINAL:
                return False
            raise ControlConflict("legacy_cancel_entry_fill_writer_pending")
        if not before_dispatch or report.event_type in TERMINAL:
            await asyncio.to_thread(journal.reconcile, lease, attempt.attempt_id,
                broker_order_id=order.broker_order_id, client_order_id=order.client_order_id,
                status=report.event_type, filled_quantity=report.filled_quantity,
                observed_at=datetime.now(UTC), now=datetime.now(UTC))
        return report.event_type in TERMINAL

    if await observe(before_dispatch=True):
        return
    if attempt.status != "PREPARED":
        raise ControlConflict("legacy_cancel_entry_uncertain_no_resend")
    await asyncio.to_thread(journal.dispatch, lease, attempt.attempt_id, now=datetime.now(UTC))
    cancel = replace(request, intent_type="cancel", reason="KILLSW1_CANCEL_ENTRY",
                     metadata={**request.metadata, "resting_entry_cancel": "true"})
    await service.broker_adapter.submit_order(cancel)
    if not await observe():
        raise ControlConflict("legacy_cancel_entry_not_terminal")


class LegacyStopDriver:
    def __init__(self, service, code):
        if code not in {"schwab_1m_v2", "orb_schwab"}:
            raise ValueError("legacy_stop_scope_unsupported")
        self.service, self.code = service, code

    def _inventory(self, unit, lease):
        strategy = unit.get(Strategy, lease.strategy_id)
        accounts = unit.scalars(select(BrokerAccount).where(BrokerAccount.id.in_(lease.account_ids))).all()
        if (not lease.scope_complete or not lease.account_ids or strategy is None
                or strategy.code != self.code or strategy.execution_mode != "live"
                or len(accounts) != len(lease.account_ids)
                or any(not controlled_live_scope(strategy.code, strategy.execution_mode, account.name)
                       or account.provider not in {"schwab", "webull"}
                       for account in accounts)):
            raise ControlConflict("legacy_stop_scope_UNKNOWN")
        orders = unit.scalars(select(BrokerOrder).where(
            BrokerOrder.strategy_id == lease.strategy_id,
            BrokerOrder.broker_account_id.in_(lease.account_ids),
            or_(BrokerOrder.status.is_(None), BrokerOrder.status.not_in(TERMINAL)),
        ).limit(BOUND + 1)).all()
        if len(orders) > BOUND:
            raise ControlConflict("legacy_stop_working_inventory_bound_UNKNOWN")
        held = {}
        for row in unit.scalars(select(VirtualPosition).where(
                VirtualPosition.strategy_id == lease.strategy_id,
                VirtualPosition.broker_account_id.in_(lease.account_ids),
                or_(VirtualPosition.quantity.is_(None), VirtualPosition.quantity != 0))):
            held[(str(row.broker_account_id), row.symbol)] = str(row.quantity)
        names = {account.name: str(account.id) for account in accounts}
        if self.code == "schwab_1m_v2":
            for row in unit.scalars(select(OmsManagedPosition).where(
                    OmsManagedPosition.strategy_code == self.code,
                    OmsManagedPosition.broker_account_name.in_(names),
                    OmsManagedPosition.status == "open",
                    or_(OmsManagedPosition.current_quantity.is_(None), OmsManagedPosition.current_quantity != 0))):
                held[(names[row.broker_account_name], row.symbol)] = str(row.current_quantity)
        return tuple((order.id, str(order.broker_account_id), order.symbol)
                     for order in orders if order.side == "buy"), held

    async def _alert(self, lease, account, symbol, quantity, reason):
        def write(unit):
            old = unit.scalar(select(SystemIncident.id).where(
                SystemIncident.payload["source"].as_string() == "killsw1_legacy_blocked",
                SystemIncident.payload["command_id"].as_string() == str(lease.command_id),
                SystemIncident.payload["account_id"].as_string() == account,
                SystemIncident.payload["symbol"].as_string() == symbol,
                SystemIncident.status == "open"))
            if old is not None:
                incident = unit.get(SystemIncident, old)
                if reason == HELD_MESSAGE and (incident.payload or {}).get("reason") != HELD_MESSAGE:
                    incident.title = HELD_MESSAGE
                    incident.payload = {**incident.payload, "reason": reason, "owned_quantity": quantity}
                    return True
                return False
            unit.add(SystemIncident(service_name="oms-risk", severity="critical", status="open",
                title=HELD_MESSAGE if reason == HELD_MESSAGE else "STOP: own entry requires reconciliation",
                payload={"source": "killsw1_legacy_blocked",
                    "command_id": str(lease.command_id), "strategy_code": self.code,
                    "account_id": account, "symbol": symbol,
                    "owned_quantity": quantity, "reason": reason}))
            return True
        if await self.service._run_db(write):
            self.service.logger.critical("[KILLSW1-LEGACY-BLOCKED] bot=%s account=%s symbol=%s reason=%s no_sell=1",
                                         self.code, account, symbol, reason)

    def _flat(self, unit, lease):
        working, held = self._inventory(unit, lease)
        if working or held:
            raise ControlConflict("legacy_stop_not_owned_flat")
        unbound = unit.scalar(select(TradeIntent.id).where(
            TradeIntent.strategy_id == lease.strategy_id,
            TradeIntent.broker_account_id.in_(lease.account_ids), TradeIntent.side == "buy",
            TradeIntent.status.not_in(TERMINAL | {"refused", "completed", "superseded"}),
            ~select(BrokerOrder.id).where(BrokerOrder.intent_id == TradeIntent.id).exists(),
        ).limit(1))
        if unbound is not None:
            raise ControlConflict("legacy_stop_unbound_entry_UNKNOWN")
        orders = unit.scalars(select(BrokerOrder).where(BrokerOrder.strategy_id == lease.strategy_id,
            BrokerOrder.broker_account_id.in_(lease.account_ids)).limit(BOUND + 1)).all()
        if len(orders) > BOUND or any(order.status not in TERMINAL for order in orders):
            raise ControlConflict("legacy_stop_protection_or_order_not_terminal")
        unresolved = unit.scalar(select(BotControlAttempt.attempt_id).where(
            BotControlAttempt.command_id == lease.command_id,
            BotControlAttempt.status.not_in(("TERMINAL", "PROVEN_NOT_SENT"))).limit(1))
        if unresolved is not None:
            raise ControlConflict("legacy_stop_dispatch_unresolved")
        totals = {}
        for order in orders:
            if order.side not in {"buy", "sell"}:
                raise ControlConflict("legacy_stop_order_side_UNKNOWN")
            executed = Decimal(0)
            for fill in unit.scalars(select(Fill).where(Fill.order_id == order.id)):
                if (fill.strategy_id != lease.strategy_id or fill.broker_account_id != order.broker_account_id
                        or fill.symbol != order.symbol or fill.side != order.side or not fill.broker_fill_id
                        or not isinstance(fill.price, Decimal) or not fill.price.is_finite() or fill.price <= 0):
                    raise ControlConflict("legacy_stop_fill_binding_UNKNOWN")
                executed += _number(fill.quantity)
            if executed > _number(order.quantity) or (order.status == "filled" and executed != order.quantity):
                raise ControlConflict("legacy_stop_fill_conservation_UNKNOWN")
            key = (str(order.broker_account_id), order.symbol)
            totals[key] = totals.get(key, Decimal(0)) + executed * (1 if order.side == "buy" else -1)
        if any(value != 0 for value in totals.values()):
            raise ControlConflict("legacy_stop_owned_fills_not_flat")
        observed = datetime.now(UTC).isoformat()
        return {str(account): {"complete": True, "owned_quantity": 0, "working_orders": 0,
            "in_flight": 0, "observed_at": observed, "evidence_hash": _hash({
                "command": str(lease.command_id), "generation": lease.generation,
                "account": str(account), "orders": sorted(str(order.id) for order in orders),
                "source": "legacy_owned_fill_vp_order_conservation"})} for account in lease.account_ids}

    async def __call__(self, lease, journal):
        try:
            entries, _ = await self.service._run_db(lambda unit: self._inventory(unit, lease), commit=False)
            blocked = False
            for order_id, account, symbol in entries:
                try:
                    await cancel_owned_entry(self.service, lease, journal, order_id, self.code)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    blocked = True
                    self.service.logger.error("[KILLSW1-LEGACY-ENTRY] bot=%s order=%s reason=%s",
                                              self.code, order_id, exc)
                    await self._alert(lease, account, symbol, "UNKNOWN", str(exc))
            _, held = await self.service._run_db(lambda unit: self._inventory(unit, lease), commit=False)
            for (account, symbol), quantity in held.items():
                await self._alert(lease, account, symbol, quantity, HELD_MESSAGE)
            if blocked or held:
                return StopProgress("BLOCKED_UNKNOWN")
            if entries:
                return StopProgress("CANCELLING_ENTRIES")
            census = await self.service._run_db(lambda unit: self._flat(unit, lease), commit=False)
            return StopProgress("SETTLED", census)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.service.logger.error("[KILLSW1-LEGACY-BLOCKED] bot=%s command=%s reason=%s",
                                      self.code, lease.command_id, exc)
            return StopProgress("BLOCKED_UNKNOWN")
