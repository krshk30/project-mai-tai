"""C6 ORB driver: STOP is a reason of the existing ORB exit owner, never ATR."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import dataclass, replace
from datetime import UTC, datetime, time
from decimal import ROUND_FLOOR, Decimal
from hashlib import sha256
import json
from threading import Lock
from uuid import UUID, uuid4, uuid5
from zoneinfo import ZoneInfo

from sqlalchemy import or_, select

from project_mai_tai.broker_adapters.protocols import OrderRequest
from project_mai_tai.db.models import AccountPosition, BrokerAccount, BrokerOrder, Fill, Strategy, SystemIncident, TradeIntent, VirtualPosition
from project_mai_tai.events import TradeIntentEvent, TradeIntentPayload
from project_mai_tai.momentum_live_sec import US_MARKET_HOLIDAYS
from project_mai_tai.oms import orb_schwab_eod as owner
from project_mai_tai.oms.bot_control_scope import controlled_live_scope

_ET = ZoneInfo("America/New_York")
_TERMINAL = {"filled", "cancelled", "canceled", "rejected", "expired", "aborted"}
_BOOT_ALERT = "STOP: held across restart — sell manually or wait for normal exit"


def _owned_orb_cleanup_scope(strategy, account):
    """Deactivation fences new exposure, not an exact existing BUY cancellation."""
    return (strategy is not None and strategy.code == "orb_schwab" and account is not None
        and controlled_live_scope(strategy.code, strategy.execution_mode, account.name)
        and account.provider in {"schwab", "webull"})


def _live_orb_scope(strategy, account):
    return _owned_orb_cleanup_scope(strategy, account) and account.is_active


def _entry_accounting_metadata(metadata):
    """Typed event scalars only; the durable owner journal stays on the order."""
    result = {}
    for key, value in metadata.items():
        if not isinstance(key, str):
            raise ValueError("orb_stop_entry_metadata_key_unknown")
        if key == owner._STATE and isinstance(value, dict):
            continue
        if isinstance(value, str):
            result[key] = value
        elif isinstance(value, bool):
            result[key] = "true" if value else "false"
        elif isinstance(value, (int, float, Decimal)) and Decimal(str(value)).is_finite():
            result[key] = str(value)
        else:
            raise ValueError("orb_stop_entry_structured_or_nonfinite_metadata_unknown")
    return result


def _boot_guard(service, target):
    runtime = getattr(service, "_bot_control_runtime", None)
    identities = getattr(runtime, "boot_orb_entry_ids", None)
    reason = ("orb_stop_boot_inventory_unknown" if identities is None else
              _BOOT_ALERT if str(target.entry_id) in identities else None)
    if reason is None:
        return
    reported = service.__dict__.setdefault("_orb_stop_boot_alerts", set())
    key = (str(target.account_id), str(target.entry_id), reason)
    if key not in reported:
        reported.add(key)
        service.logger.critical("[KILLSW1-ORB-BOOT-BLOCK] account=%s symbol=%s entry=%s %s",
            target.account, target.symbol, target.entry_id, reason)
    return reason


def _valid_quantity(value):
    return isinstance(value, Decimal) and value.is_finite() and value >= 0


def _incremental_report(session, order, report):
    previous = session.scalars(select(Fill).where(Fill.order_id == order.id)).all()
    quantity = sum((fill.quantity for fill in previous), Decimal(0))
    cash = sum((fill.quantity * fill.price for fill in previous), Decimal(0))
    if report.filled_quantity < quantity:
        raise ValueError("orb_stop_execution_regressed")
    if report.filled_quantity == 0:
        return report
    if (not report.broker_fill_id or report.fill_price is None
            or not report.fill_price.is_finite() or report.fill_price <= 0):
        raise ValueError("orb_stop_execution_price_unknown")
    duplicate = session.scalar(select(Fill.id).where(Fill.broker_fill_id == report.broker_fill_id,
        Fill.order_id != order.id))
    if duplicate is not None:
        raise ValueError("orb_stop_foreign_execution_id")
    delta = report.filled_quantity - quantity
    cash_delta = report.filled_quantity * report.fill_price - cash
    if (delta == 0 and abs(cash_delta) > quantity * Decimal("0.000000005")) or (
        delta > 0 and (not cash_delta.is_finite() or cash_delta <= 0)):
        raise ValueError("orb_stop_cash_inconsistent")
    if delta > 0 and report.event_type not in {"filled", "partially_filled"} and not (
        report.event_type in {"cancelled", "rejected"}
        and report.metadata.get("webull_terminal_with_partial_fill") == report.event_type
    ):
        raise ValueError("orb_stop_terminal_partial_requires_existing_fill_reconciliation")
    return replace(report, fill_price=cash_delta / delta if delta else report.fill_price,
        metadata={**report.metadata, "orb_stop_broker_cumulative_quantity": str(report.filled_quantity),
            "orb_stop_broker_cumulative_average": str(report.fill_price),
            "orb_stop_price_source": "derived_cumulative_cash_delta"})


def _remaining(session, target, *, current_episode=True):
    entry = session.get(BrokerOrder, target.entry_id)
    if (entry is None or entry.strategy_id != target.strategy_id
            or entry.broker_account_id != target.account_id or entry.symbol != target.symbol
            or entry.side != "buy" or entry.client_order_id != target.client_id
            or entry.broker_order_id != target.broker_id or entry.status not in _TERMINAL):
        raise ValueError("orb_stop_entry_terminal_identity_unknown")
    if current_episode:
        newer = session.scalar(select(BrokerOrder.id).where(
            BrokerOrder.strategy_id == target.strategy_id, BrokerOrder.broker_account_id == target.account_id,
            BrokerOrder.symbol == target.symbol, BrokerOrder.side == "buy",
        ).order_by(BrokerOrder.submitted_at.desc(), BrokerOrder.id.desc()).limit(1))
        if newer != entry.id:
            raise ValueError("orb_stop_old_episode")
    buys = session.scalars(select(Fill).where(Fill.order_id == entry.id)).all()
    sells = session.scalars(select(Fill).join(BrokerOrder, BrokerOrder.id == Fill.order_id).where(
        BrokerOrder.strategy_id == target.strategy_id, BrokerOrder.broker_account_id == target.account_id,
        BrokerOrder.symbol == target.symbol, BrokerOrder.side == "sell",
        or_(BrokerOrder.intent_id == entry.intent_id,
            BrokerOrder.payload["entry_order_id"].as_string() == str(entry.id)),
    )).all()
    for side, rows in (("buy", buys), ("sell", sells)):
        if any(not _valid_quantity(row.quantity) or row.quantity <= 0 or not row.price.is_finite()
               or row.price <= 0 or not row.broker_fill_id or row.side != side
               or row.strategy_id != target.strategy_id or row.broker_account_id != target.account_id
               or row.symbol != target.symbol for row in rows):
            raise ValueError("orb_stop_fill_provenance_unknown")
    bought = sum((row.quantity for row in buys), Decimal(0))
    sold = sum((row.quantity for row in sells), Decimal(0))
    if not 0 < bought <= target.entry_quantity or sold > bought:
        raise ValueError("orb_stop_owned_fill_balance_unknown")
    return bought - sold


def _episode_proof(session, target):
    """Seal canonical per-episode accounting, independently of a later position."""
    if _remaining(session, target, current_episode=False) != 0:
        raise ValueError("orb_stop_historical_episode_not_flat")
    entry = session.get(BrokerOrder, target.entry_id)
    orders = session.scalars(select(BrokerOrder).where(
        BrokerOrder.strategy_id == target.strategy_id, BrokerOrder.broker_account_id == target.account_id,
        BrokerOrder.symbol == target.symbol, or_(BrokerOrder.id == entry.id,
            (BrokerOrder.side == "sell") & or_(BrokerOrder.intent_id == entry.intent_id,
                BrokerOrder.payload["entry_order_id"].as_string() == str(entry.id))),
    )).all()
    proof = []
    for row in orders:
        if (row.status not in _TERMINAL or not row.broker_order_id or not row.client_order_id
                or not _valid_quantity(row.quantity) or row.quantity <= 0):
            raise ValueError("orb_stop_historical_order_not_terminal")
        fills = session.scalars(select(Fill).where(Fill.order_id == row.id)).all()
        quantity = sum((fill.quantity for fill in fills), Decimal(0))
        cash = sum((fill.quantity * fill.price for fill in fills), Decimal(0))
        if (quantity > row.quantity or row.status == "filled" and quantity != row.quantity
                or not cash.is_finite() or (quantity > 0 and cash <= 0)):
            raise ValueError("orb_stop_historical_cash_quantity_unknown")
        proof.append({"order_id": str(row.id), "intent_id": str(row.intent_id),
            "coid": row.client_order_id, "broker_id": row.broker_order_id, "status": row.status,
            "quantity": str(quantity.normalize()), "cash": str(cash.normalize()),
            "fills": sorted([str(fill.id), fill.broker_fill_id,
                str(fill.quantity.normalize()), str(fill.price.normalize())] for fill in fills)})
    return {"entry_id": str(entry.id), "strategy_id": str(target.strategy_id),
        "account_id": str(target.account_id), "symbol": target.symbol,
        "orders": sorted(proof, key=lambda row: row["order_id"])}


def _terminal_stop_proven(session, lease, entry, row, state):
    """Old SETTLED commands are immutable history, never current cleanup authority."""
    from project_mai_tai.db.models import BotControlAttempt, BotControlCommand
    try:
        command = session.get(BotControlCommand, UUID(str(state.get("command_id"))))
    except (TypeError, ValueError):
        return False
    account = session.get(BrokerAccount, entry.broker_account_id)
    intent = session.get(TradeIntent, row.intent_id)
    if (command is None or not _live_orb_scope(session.get(Strategy, entry.strategy_id), account)
            or intent is None or command.action != "STOP"
            or command.execution_mode != "live" or command.strategy_id != lease.strategy_id
            or command.generation != state.get("generation") or intent.reason != owner.STOP_REASON
            or intent.strategy_id != entry.strategy_id or intent.broker_account_id != entry.broker_account_id
            or intent.symbol != entry.symbol or intent.side != "sell" or intent.intent_type != "close"
            or row.status not in {"filled", "cancelled", "canceled", "rejected", "expired"}
            or row.strategy_id != entry.strategy_id or row.broker_account_id != entry.broker_account_id
            or row.symbol != entry.symbol or row.side != "sell"
            or (row.payload or {}).get("entry_order_id") != str(entry.id)
            or command.account_scope.get("complete") is not True
            or str(entry.broker_account_id) not in command.account_scope.get("account_ids", [])):
        return False
    if command.command_id != lease.command_id:
        census = (command.inventory_census or {}).get(str(entry.broker_account_id)) or {}
        if (command.phase != "SETTLED" or command.active is not False or command.completed_at is None
                or command.generation >= lease.generation or census.get("complete") is not True
                or census.get("owned_quantity") != 0 or census.get("working_orders") != 0
                or census.get("in_flight") != 0 or not census.get("evidence_hash")):
            return False
    attempts = session.scalars(select(BotControlAttempt).where(
        BotControlAttempt.command_id == command.command_id, BotControlAttempt.target_order_id == row.id,
        BotControlAttempt.operation == "SUBMIT_EXIT")).all()
    if len(attempts) != 1:
        return False
    native = session.scalars(select(BotControlAttempt).where(
        BotControlAttempt.command_id == command.command_id, BotControlAttempt.entry_order_id == entry.id,
        BotControlAttempt.operation == "CANCEL_PROTECTION",
        BotControlAttempt.binding_kind == "orb_native_tree")).all()
    native_hash = sha256(f"{entry.strategy_id}:{entry.id}:{entry.broker_order_id}:"
        f"{entry.client_order_id}:None:{entry.broker_order_id}".encode()).hexdigest()
    if len(native) != 1:
        return False
    root = native[0]
    terminal_root = root.terminal_evidence or {}
    if (root.status != "TERMINAL" or root.account_id != entry.broker_account_id
            or root.strategy_id != entry.strategy_id or root.symbol != entry.symbol
            or root.generation != command.generation or root.binding_key != entry.broker_order_id
            or root.parent_intent_id != entry.intent_id or root.managed_row_id is not None
            or root.owner_evidence_hash != native_hash
            or terminal_root.get("entry_order_id") != str(entry.id)
            or terminal_root.get("entry_broker_order_id") != entry.broker_order_id
            or terminal_root.get("binding") != entry.broker_order_id
            or terminal_root.get("source") != "existing_owned_parent_release_reader"
            or terminal_root.get("outcome") != "released"):
        return False
    attempt = attempts[0]
    digest = sha256(json.dumps({"entry": str(entry.id), "coid": row.client_order_id,
        "account": account.name, "symbol": entry.symbol}, sort_keys=True).encode()).hexdigest()
    evidence = attempt.terminal_evidence or {}
    if (attempt.status != "TERMINAL" or attempt.strategy_id != entry.strategy_id
            or attempt.execution_mode != "live" or attempt.account_id != entry.broker_account_id
            or attempt.symbol != entry.symbol or attempt.generation != command.generation
            or attempt.binding_kind != "broker_order" or attempt.binding_key != str(row.id)
            or attempt.client_order_id != row.client_order_id or attempt.requested_quantity != row.quantity
            or attempt.owner_evidence_hash != digest or attempt.owner_evidence_revision != command.generation
            or evidence.get("source") != "authoritative_order_read"
            or evidence.get("status") != row.status or evidence.get("broker_order_id") != row.broker_order_id
            or evidence.get("client_order_id") != row.client_order_id):
        return False
    filled = sum(session.scalars(select(Fill.quantity).where(Fill.order_id == row.id)).all(), Decimal(0))
    try:
        if Decimal(str(evidence.get("filled_quantity"))) != filled:
            return False
        target = owner._Target(entry.id, entry.strategy_id, entry.broker_account_id, account.name,
            entry.symbol, entry.broker_order_id or "", entry.client_order_id, Decimal(0), entry.quantity)
        return state.get("terminal_episode_proof") == _episode_proof(session, target)
    except (ValueError, ArithmeticError, TypeError):
        return False


@dataclass
class OrbStopContext:
    service: object
    lease: object
    journal: object
    clock: object
    prior_unresolved: bool = False
    prior_terminal: bool = False
    boot_blocked: bool = False
    claimed_target: object = None
    native_binding: str | None = None
    terminal_at: datetime | None = None

    def _lease(self, session):
        return self.journal.store._leased(session, self.lease, self.clock())

    @staticmethod
    def _native_binding(entry):
        state = (entry.payload or {}).get("orbroute1_protection") or {}
        return sha256(json.dumps({"entry": str(entry.id), "strategy": str(entry.strategy_id),
            "account": str(entry.broker_account_id), "symbol": entry.symbol,
            "coid": entry.client_order_id, "root": entry.broker_order_id,
            "base": state.get("base"), "intent": state.get("intent_id"),
            "quantity": state.get("quantity")}, sort_keys=True).encode()).hexdigest()

    def validate_native_owner(self, session, entry):
        command = self._lease(session)
        target = self.claimed_target
        if entry is None or not _live_orb_scope(session.get(Strategy, entry.strategy_id),
                                                session.get(BrokerAccount, entry.broker_account_id)):
            raise ValueError("orb_stop_native_scope_unknown")
        if (target is None or entry.id != target.entry_id or entry.strategy_id != self.lease.strategy_id
                or entry.broker_account_id != target.account_id
                or str(entry.broker_account_id) not in command.account_scope["account_ids"]
                or entry.symbol != target.symbol or entry.side != "buy"
                or entry.client_order_id != target.client_id or entry.broker_order_id != target.broker_id
                or not self.native_binding or self._native_binding(entry) != self.native_binding):
            raise ValueError("orb_stop_native_binding_changed")
        state = (entry.payload or {}).get(owner._STATE) or {}
        if state.get("command_id") != str(self.lease.command_id) or state.get("reason") != owner.STOP_REASON:
            raise ValueError("orb_stop_native_claim_not_owned")

    def execution_metadata(self, target, now):
        if now.tzinfo is None:
            raise ValueError("orb_stop_clock_unknown")
        local = now.astimezone(_ET)
        provider = self.service.settings.provider_for_account(target.account)
        if (provider not in {"schwab", "webull"} or local.weekday() >= 5
                or local.date() in US_MARKET_HOLIDAYS or not time(4) <= local.time() < time(20)
                or (provider == "schwab" and local.time() < time(7))):
            raise ValueError("orb_stop_session_unsupported_keep_protection")
        if time(9, 30) <= local.time() < time(16):
            return {"order_type": "market", "session": "NORMAL", "extended_hours": "false"}
        quote = self.service._latest_quotes_by_symbol.get(target.symbol) or {}
        stamp = quote.get("received_at")
        try:
            bid = Decimal(str(quote.get("bid", "NaN")))
        except Exception as exc:
            raise ValueError("orb_stop_bid_unknown_keep_protection") from exc
        if (not isinstance(stamp, datetime) or stamp.tzinfo is None
                or not 0 <= (now - stamp).total_seconds() <= 2 or not bid.is_finite() or bid <= 0):
            raise ValueError("orb_stop_bid_not_fresh_keep_protection")
        tick = Decimal("0.01") if bid >= 1 else Decimal("0.0001")
        limit = bid.quantize(tick, rounding=ROUND_FLOOR)
        if limit <= 0:
            raise ValueError("orb_stop_bid_below_tick_keep_protection")
        return {"order_type": "limit", "session": "AM" if local.time() < time(9, 30) else "PM",
                "extended_hours": "true", "limit_price": str(limit)}

    async def claim(self, target, event_id):
        boot_reason = _boot_guard(self.service, target)
        if boot_reason:
            self.boot_blocked = self.prior_unresolved = True
            await self.report_boot_block(target, boot_reason)
            return False
        def claim(session):
            command = self._lease(session)
            if target.strategy_id != self.lease.strategy_id or str(target.account_id) not in command.account_scope["account_ids"]:
                raise ValueError("orb_stop_scope_changed")
            entry = session.scalar(select(BrokerOrder).where(BrokerOrder.id == target.entry_id).with_for_update())
            strategy, account = session.get(Strategy, target.strategy_id), session.get(BrokerAccount, target.account_id)
            if (not _live_orb_scope(strategy, account) or account.name != target.account or entry is None):
                raise ValueError("orb_stop_owner_not_live")
            remaining = _remaining(session, target)
            state = (entry.payload or {}).get(owner._STATE)
            if state:
                self.prior_terminal = (isinstance(state, dict) and remaining == 0
                    and state.get("phase") in {"resolved_by_child_fill", "resolved_by_stop_fill", "resolved_by_prior_exit_fill"})
                self.prior_unresolved = not self.prior_terminal
                return False  # Prior normal/STOP/UNKNOWN attempt must be read, never replayed.
            entry.payload = {**entry.payload, owner._STATE: {"phase": "checking", "reason": owner.STOP_REASON,
                "event_id": str(event_id), "command_id": str(self.lease.command_id),
                "generation": self.lease.generation, "native_binding": self._native_binding(entry),
                "updated_at": self.clock().isoformat()}}
            self.claimed_target, self.native_binding = target, self._native_binding(entry)
            return True
        return await self.service._run_db(claim)

    async def report_boot_block(self, target, reason):
        """Use the existing ORB incident/pager source, never the exit-owner state."""
        identity = uuid5(self.lease.command_id, f"orb-boot-block:{target.account_id}:{target.entry_id}")
        def write(session):
            command = self._lease(session)
            entry = session.get(BrokerOrder, target.entry_id)
            if (entry is None or entry.strategy_id != self.lease.strategy_id
                    or entry.broker_account_id != target.account_id or entry.symbol != target.symbol
                    or str(entry.broker_account_id) not in command.account_scope["account_ids"]):
                raise ValueError("orb_stop_boot_alert_owner_unknown")
            if not _live_orb_scope(session.get(Strategy, entry.strategy_id),
                                   session.get(BrokerAccount, entry.broker_account_id)):
                raise ValueError("orb_stop_boot_alert_scope_unknown")
            existing = session.get(SystemIncident, identity)
            if existing is None:
                session.add(SystemIncident(id=identity, service_name="oms-risk", severity="critical", status="open",
                    title=_BOOT_ALERT if reason == _BOOT_ALERT else "ORB STOP blocked: boot inventory UNKNOWN",
                    opened_at=self.clock(), payload={"source": owner._STATE, "kind": "killsw1_boot_block",
                        "command_id": str(self.lease.command_id), "generation": self.lease.generation,
                        "strategy_id": str(self.lease.strategy_id), "broker_account_name": target.account,
                        "symbol": target.symbol, "entry_order_id": str(target.entry_id),
                        "entry_broker_order_id": target.broker_id, "reason": reason}))
            elif reason == _BOOT_ALERT and (existing.payload or {}).get("reason") != reason:
                existing.title = _BOOT_ALERT
                existing.payload = {**existing.payload, "reason": reason}
        await self.service._run_db(write)

    async def before_release(self, target):
        def binding(session):
            self._lease(session)
            entry = session.get(BrokerOrder, target.entry_id)
            _remaining(session, target)
            return entry.broker_order_id
        bound = await self.service._run_db(binding, commit=False)
        prepared = await asyncio.to_thread(self.journal.prepare_native, self.lease,
            entry_id=target.entry_id, managed_id=None, binding_key=bound, now=self.clock())
        if prepared.status != "PREPARED":
            raise ValueError("orb_stop_prior_native_attempt_read_first")
        await asyncio.to_thread(self.journal.dispatch, self.lease, prepared.attempt_id, now=self.clock())
        return prepared

    async def poll_children(self, target):
        from project_mai_tai.oms.orb_country import PROTECTION, _probe
        def read(session):
            entry = session.get(BrokerOrder, target.entry_id)
            self.validate_native_owner(session, entry)
            return dict((entry.payload or {}).get(PROTECTION) or {})
        state = await self.service._run_db(read, commit=False)
        if state.get("base"):
            await _probe(self.service, target.entry_id, target.account, target.symbol, state,
                         fresh=True, stop_context=self)

    async def after_release(self, attempt, outcome):
        # Existing Schwab reader's filled sibling alone is not full-tree terminal proof.
        recorded = "unanswerable" if outcome == "resolved_by_fill" else outcome
        await asyncio.to_thread(self.journal.record_native_outcome, self.lease,
            attempt.attempt_id, outcome=recorded, now=self.clock())
        if outcome == "resolved_by_fill":
            raise ValueError("orb_stop_filled_child_requires_remaining_tree_proof")

    def validate_prepared(self, session, target, quantity):
        self._lease(session)
        remaining = _remaining(session, target)
        self.validate_native_owner(session, session.get(BrokerOrder, target.entry_id))
        position = self.service.store.get_virtual_position(session, strategy_id=target.strategy_id,
            broker_account_id=target.account_id, symbol=target.symbol)
        if (position is None or not _valid_quantity(position.quantity) or not _valid_quantity(quantity)
                or not 0 < quantity <= min(position.quantity, remaining)):
            raise ValueError("orb_stop_owned_quantity_changed")

    def ownership_conflict(self, session, target):
        from project_mai_tai.broker_adapters.webull import WebullBrokerAdapter
        from project_mai_tai.oms.service import oco_exit_client_order_id
        entry = session.get(BrokerOrder, target.entry_id)
        state = (entry.payload or {}).get("orbroute1_protection") or {}
        base = state.get("base")
        expected = {WebullBrokerAdapter._combo_leg_coid(base, suffix) for suffix in ("T", "S")} if base else set()
        rows = session.scalars(select(BrokerOrder).where(BrokerOrder.broker_account_id == target.account_id,
            BrokerOrder.symbol == target.symbol, BrokerOrder.id != target.entry_id,
            BrokerOrder.strategy_id == target.strategy_id,
            or_(BrokerOrder.status.not_in(_TERMINAL), BrokerOrder.status.is_(None)))).all()
        for row in rows:
            metadata = row.payload or {}
            owned = row.strategy_id == target.strategy_id and row.side == "sell" and row.broker_order_id
            webull_child = (owned and row.client_order_id in expected
                and metadata.get("orbroute1_child") == "true" and metadata.get("entry_order_id") == str(entry.id))
            schwab_child = (owned and row.intent_id == entry.intent_id
                and row.client_order_id == oco_exit_client_order_id(target.client_id, row.broker_order_id))
            if not webull_child and not schwab_child:
                return True  # Only the exact native children may pass to their release reader.
        return False

    async def quantity_after_release(self, target, terminal_at):
        from project_mai_tai.oms.bot_control_evidence import positions_after_protection, owned_exit_ceiling
        # Reuse the OMS sync receipt; a cached Webull return retains its old read_at.
        self.terminal_at = terminal_at
        until = self.clock().timestamp() + 30
        while True:
            self.execution_metadata(target, self.clock())
            await self.service.sync_broker_positions(account_names=[target.account])
            def ceiling(session):
                self._lease(session)
                positions_after_protection(session, account_name=target.account,
                                           terminal_at=terminal_at, now=self.clock())
                rows = session.scalars(select(AccountPosition).where(
                    AccountPosition.broker_account_id == target.account_id, AccountPosition.symbol == target.symbol,
                ).limit(2)).all()
                if len(rows) != 1:
                    raise ValueError("orb_stop_broker_position_unknown")
                quantity = owned_exit_ceiling(session, strategy_id=target.strategy_id,
                    account_id=target.account_id, symbol=target.symbol,
                    remaining=_remaining(session, target), broker_total=rows[0].quantity)
                if not _valid_quantity(quantity) or quantity <= 0 or quantity != quantity.to_integral_value():
                    raise ValueError("orb_stop_no_positive_whole_owned_quantity")
                return quantity
            try:
                return await self.service._run_db(ceiling, commit=False)
            except ValueError as exc:
                if str(exc) not in {"positions_wire_receipt_predates_release", "positions_wire_receipt_not_fresh"}:
                    raise
                if self.clock().timestamp() >= until:
                    raise ValueError("orb_stop_fresh_position_wait_elapsed") from exc
                await asyncio.sleep(.25)

    async def validate_wire(self, target, request):
        """Final STOP-only proof after journal awaits; never refresh/reprice here."""
        from project_mai_tai.oms.bot_control_evidence import positions_after_protection, owned_exit_ceiling
        if self.terminal_at is None:
            raise ValueError("orb_stop_final_release_time_unknown")
        def validate(session):
            self.validate_prepared(session, target, request.quantity)
            read_at = positions_after_protection(session, account_name=target.account,
                                                terminal_at=self.terminal_at, now=self.clock())
            rows = session.scalars(select(AccountPosition).where(
                AccountPosition.broker_account_id == target.account_id,
                AccountPosition.symbol == target.symbol,
            ).limit(2)).all()
            if len(rows) != 1:
                raise ValueError("orb_stop_final_broker_position_unknown")
            cap = owned_exit_ceiling(session, strategy_id=target.strategy_id,
                account_id=target.account_id, symbol=target.symbol,
                remaining=_remaining(session, target), broker_total=rows[0].quantity)
            if cap != request.quantity:
                raise ValueError("orb_stop_final_quantity_changed_no_reprice")
            return read_at
        read_at = await self.service._run_db(validate, commit=False)
        # A delayed event-loop resume must not extend the immutable receipt's age.
        if not 0 <= (self.clock() - read_at).total_seconds() <= 30 or read_at <= self.terminal_at:
            raise ValueError("orb_stop_final_receipt_aged_before_wire")

    async def before_submit(self, target, request):
        def find(session):
            self.validate_prepared(session, target, request.quantity)
            row = session.scalar(select(BrokerOrder).where(BrokerOrder.client_order_id == request.client_order_id))
            if (row is None or row.strategy_id != target.strategy_id or row.broker_account_id != target.account_id
                    or row.symbol != target.symbol or row.side != "sell" or row.quantity != request.quantity
                    or (row.payload or {}).get("entry_order_id") != str(target.entry_id)):
                raise ValueError("orb_stop_exit_identity_unprepared")
            return row.id
        identity = await self.service._run_db(find, commit=False)
        digest = sha256(json.dumps({"entry": str(target.entry_id), "coid": request.client_order_id,
            "account": target.account, "symbol": target.symbol}, sort_keys=True).encode()).hexdigest()
        prepared = await asyncio.to_thread(self.journal.prepare, self.lease, order_id=identity,
            operation="SUBMIT_EXIT", sequence_no=0, owner_hash=digest,
            owner_revision=self.lease.generation, now=self.clock())
        if prepared.status != "PREPARED":
            raise ValueError("orb_stop_exit_uncertain_no_resubmit")
        await asyncio.to_thread(self.journal.dispatch, self.lease, prepared.attempt_id, now=self.clock())
        return prepared

    async def after_submit(self, attempt, request, reports):
        for report in reports:
            if report.origin != "broker" or not report.broker_order_id:
                raise ValueError("orb_stop_exit_response_not_authoritative")
            await asyncio.to_thread(self.journal.reconcile, self.lease, attempt.attempt_id,
                broker_order_id=report.broker_order_id, client_order_id=request.client_order_id,
                status=report.event_type, filled_quantity=report.filled_quantity,
                observed_at=report.reported_at, now=self.clock())
            break  # Never duplicate a terminal transition from one returned batch.


def trusted_census(session, lease, journal, now):
    """Only a complete owned-ledger/native-proof census can unlock ON."""
    from project_mai_tai.db.models import BotControlAttempt, TradeIntent
    from project_mai_tai.broker_adapters.webull import WebullBrokerAdapter
    command = journal.store._leased(session, lease, now)
    if (not lease.scope_complete or not lease.account_ids
            or set(command.account_scope["account_ids"]) != {str(v) for v in lease.account_ids}):
        raise ValueError("orb_stop_settlement_scope_unknown")
    outside = session.scalar(select(VirtualPosition.id).where(
        VirtualPosition.strategy_id == lease.strategy_id,
        VirtualPosition.broker_account_id.not_in(lease.account_ids),
        or_(VirtualPosition.quantity != 0, VirtualPosition.quantity.is_(None))))
    if outside is not None:
        raise ValueError("orb_stop_settlement_unscoped_position")
    accounts = session.scalars(select(BrokerAccount).where(BrokerAccount.id.in_(lease.account_ids))).all()
    strategy = session.get(Strategy, lease.strategy_id)
    if len(accounts) != len(lease.account_ids) or any(
        not _live_orb_scope(strategy, account)
        for account in accounts
    ):
        raise ValueError("orb_stop_settlement_account_unknown")
    orders = session.scalars(select(BrokerOrder).where(BrokerOrder.strategy_id == lease.strategy_id,
        BrokerOrder.broker_account_id.in_(lease.account_ids))).all()
    by_id = {row.id: row for row in orders}
    if any(row.status not in _TERMINAL or not _valid_quantity(row.quantity) or row.quantity <= 0 for row in orders):
        raise ValueError("orb_stop_settlement_owned_order_unresolved")
    attempts = session.scalars(select(BotControlAttempt).where(
        BotControlAttempt.strategy_id == lease.strategy_id,
        BotControlAttempt.account_id.in_(lease.account_ids))).all()
    if any(row.status not in {"TERMINAL", "PROVEN_NOT_SENT"} for row in attempts):
        raise ValueError("orb_stop_settlement_inflight_unknown")
    fills = session.scalars(select(Fill).where(Fill.strategy_id == lease.strategy_id,
        Fill.broker_account_id.in_(lease.account_ids))).all()
    # Include fills reached by owned orders even if their denormalized owner was corrupted.
    linked = session.scalars(select(Fill).where(Fill.order_id.in_(by_id))).all() if by_id else []
    if {row.id for row in fills} != {row.id for row in linked}:
        raise ValueError("orb_stop_settlement_fill_owner_mismatch")
    totals, executed = {}, {}
    for fill in fills:
        order = by_id.get(fill.order_id)
        if (order is None or order.broker_account_id != fill.broker_account_id
                or order.symbol != fill.symbol or order.side != fill.side or not fill.broker_fill_id
                or not _valid_quantity(fill.quantity) or fill.quantity <= 0
                or not fill.price.is_finite() or fill.price <= 0 or fill.side not in {"buy", "sell"}):
            raise ValueError("orb_stop_settlement_fill_provenance_unknown")
        key = (fill.broker_account_id, fill.symbol)
        totals[key] = totals.get(key, Decimal(0)) + (fill.quantity if fill.side == "buy" else -fill.quantity)
        executed[order.id] = executed.get(order.id, Decimal(0)) + fill.quantity
    if any(value != 0 for value in totals.values()) or any(
        executed.get(row.id, Decimal(0)) > row.quantity or (
            row.status == "filled" and executed.get(row.id, Decimal(0)) != row.quantity) for row in orders
    ):
        raise ValueError("orb_stop_settlement_fill_conservation_unknown")
    positions = session.scalars(select(VirtualPosition).where(VirtualPosition.strategy_id == lease.strategy_id,
        VirtualPosition.broker_account_id.in_(lease.account_ids))).all()
    if any(not _valid_quantity(row.quantity) or row.quantity != 0 for row in positions):
        raise ValueError("orb_stop_settlement_vp_not_flat")
    protected_intents = set()
    for entry in orders:
        if entry.side != "buy" or executed.get(entry.id, Decimal(0)) == 0:
            continue
        native = [row for row in attempts if row.entry_order_id == entry.id
                  and row.binding_kind == "orb_native_tree" and row.status == "TERMINAL"
                  and (row.terminal_evidence or {}).get("entry_broker_order_id") == entry.broker_order_id
                  and (row.terminal_evidence or {}).get("outcome") == "released"]
        state = (entry.payload or {}).get("orbroute1_protection") or {}
        cached = state.get("terminal_children") or {}
        base = state.get("base")
        expected = {WebullBrokerAdapter._combo_leg_coid(base, suffix) for suffix in ("T", "S")} if base else set()
        webull_terminal = (state.get("phase") == "released" and bool(expected) and set(cached) == expected
            and all(value.get("broker_order_id") and value.get("event_type") in {"filled", "cancelled", "rejected"}
                    and (value.get("event_type") == "filled"
                         or (value.get("metadata") or {}).get("orbroute1_fresh_read") == "true")
                    for value in cached.values()))
        if not native and not webull_terminal:
            raise ValueError("orb_stop_settlement_native_tree_unknown")
        if state.get("intent_id"):
            protected_intents.add(state["intent_id"])
    intents = session.scalars(select(TradeIntent).where(TradeIntent.strategy_id == lease.strategy_id,
        TradeIntent.broker_account_id.in_(lease.account_ids))).all()
    if any(row.status not in _TERMINAL and not (
        str(row.id) in protected_intents and row.reason == "ORB_ATTACH_PROTECTION"
        and row.side == "sell" and row.intent_type == "close") for row in intents):
        raise ValueError("orb_stop_settlement_pending_intent_unknown")
    result = {}
    for account in accounts:
        proof = {"command_id": str(lease.command_id), "generation": lease.generation,
            "strategy_id": str(lease.strategy_id), "account_id": str(account.id),
            "orders": sorted(str(row.id) for row in orders if row.broker_account_id == account.id),
            "fills": sorted(str(row.id) for row in fills if row.broker_account_id == account.id),
            "attempts": sorted(str(row.attempt_id) for row in attempts if row.account_id == account.id),
            "observed_at": now.isoformat()}
        result[str(account.id)] = {"complete": True, "owned_quantity": 0, "working_orders": 0,
            "in_flight": 0, "observed_at": now.isoformat(),
            "evidence_hash": sha256(json.dumps(proof, sort_keys=True).encode()).hexdigest()}
    return result


async def _cancel_entry(service, lease, journal, entry_id, clock):
    """One exact owned cancel, then READ; cancel responses are never terminal proof."""
    def load(session):
        journal.store._leased(session, lease, clock())
        row = session.get(BrokerOrder, entry_id)
        if row is None:
            raise ValueError("orb_stop_entry_cancel_identity_unknown")
        strategy, account = session.get(Strategy, row.strategy_id), session.get(BrokerAccount, row.broker_account_id)
        intent = session.get(TradeIntent, row.intent_id) if row.intent_id is not None else None
        if (not _owned_orb_cleanup_scope(strategy, account) or row.strategy_id != lease.strategy_id or row.side != "buy"
                or account.id not in lease.account_ids
                or not row.client_order_id or not row.broker_order_id
                or not _valid_quantity(row.quantity) or row.quantity <= 0
                or intent is None or intent.strategy_id != row.strategy_id
                or intent.broker_account_id != account.id or intent.symbol != row.symbol
                or intent.side != "buy" or intent.intent_type not in {"open", "scale"}
                or row.status not in {"accepted", "working", "open", "partially_filled"}):
            raise ValueError("orb_stop_entry_cancel_identity_unknown")
        return row, strategy.code, account.name, account.provider
    entry, strategy_code, account_name, provider = await service._run_db(load, commit=False)
    if provider == "schwab":
        # Reuse C6's read-first partial-native protection policy, not parent DELETE.
        from project_mai_tai.oms.bot_control_legacy import cancel_owned_entry
        await cancel_owned_entry(service, lease, journal, entry.id, strategy_code)
        return
    original_metadata = deepcopy(entry.payload or {})
    request = OrderRequest(entry.client_order_id, account_name, strategy_code, entry.symbol, "buy", "open",
        entry.quantity, "KILLSW1_VERIFY_ORB_ENTRY", {**deepcopy(original_metadata), "broker_order_id": entry.broker_order_id})
    event_metadata = _entry_accounting_metadata(request.metadata)
    digest = sha256(f"{entry.id}:{entry.client_order_id}:{entry.broker_order_id}".encode()).hexdigest()
    prepared = await asyncio.to_thread(journal.prepare, lease, order_id=entry.id,
        operation="CANCEL_ENTRY", sequence_no=0, owner_hash=digest,
        owner_revision=lease.generation, now=clock())
    if prepared.status == "PREPARED":
        await asyncio.to_thread(journal.dispatch, lease, prepared.attempt_id, now=clock())
        await service.broker_adapter.submit_order(replace(request, intent_type="cancel", reason=owner.STOP_REASON))
    report = await service.broker_adapter.fetch_order_update(request)
    if (report is None or report.origin != "broker" or report.client_order_id != entry.client_order_id
            or report.broker_order_id != entry.broker_order_id or report.symbol != entry.symbol
            or report.side != "buy" or report.intent_type != "open" or not _valid_quantity(report.filled_quantity)
            or report.quantity != entry.quantity
            or (report.event_type == "filled" and report.filled_quantity != entry.quantity)
            or report.filled_quantity > entry.quantity or report.reported_at.tzinfo is None
            or not 0 <= (clock() - report.reported_at).total_seconds() <= 2):
        raise ValueError("orb_stop_entry_terminal_read_unknown")
    with service.session_factory() as session:
        # The generic writer replaces payload: retain current durable state, never the wire copy.
        accounting_request = replace(request, metadata=deepcopy(session.get(BrokerOrder, entry.id).payload or {}))
        intent = session.get(TradeIntent, entry.intent_id)
        previous = session.scalars(select(Fill).where(Fill.order_id == entry.id)).all()
        previous_quantity = sum((fill.quantity for fill in previous), Decimal(0))
        previous_cash = sum((fill.quantity * fill.price for fill in previous), Decimal(0))
        if report.filled_quantity < previous_quantity:
            raise ValueError("orb_stop_entry_execution_regressed")
        if report.filled_quantity > 0:
            if (not report.broker_fill_id or report.fill_price is None
                    or not report.fill_price.is_finite() or report.fill_price <= 0):
                raise ValueError("orb_stop_entry_execution_price_unknown")
            delta = report.filled_quantity - previous_quantity
            cash_delta = report.filled_quantity * report.fill_price - previous_cash
            if (delta == 0 and abs(cash_delta) > previous_quantity * Decimal("0.000000005")) or (
                delta > 0 and (not cash_delta.is_finite() or cash_delta <= 0)):
                raise ValueError("orb_stop_entry_cash_inconsistent")
            if delta > 0 and report.event_type not in {"filled", "partially_filled"} and not (
                report.event_type in {"cancelled", "rejected"}
                and report.metadata.get("webull_terminal_with_partial_fill") == report.event_type
            ):
                raise ValueError("orb_stop_terminal_partial_requires_existing_fill_reconciliation")
            writer_report = replace(report, fill_price=cash_delta / delta if delta else report.fill_price,
                metadata={**report.metadata, "orb_stop_broker_cumulative_quantity": str(report.filled_quantity),
                    "orb_stop_broker_cumulative_average": str(report.fill_price),
                    "orb_stop_price_source": "derived_cumulative_cash_delta"})
        else:
            writer_report = report
        event = TradeIntentEvent(source_service="oms-risk", payload=TradeIntentPayload(
            strategy_code=strategy_code, broker_account_name=account_name, symbol=entry.symbol,
            side="buy", intent_type="open", quantity=entry.quantity, reason=request.reason,
            metadata=event_metadata))
        events = await service._record_order_reports(session=session, intent=intent,
            strategy_id=entry.strategy_id, broker_account_id=entry.broker_account_id,
            intent_event=event, request=accounting_request, reports=[writer_report])
        session.commit()
    for event in events:
        await service._publish_order_event(event)
    await asyncio.to_thread(journal.reconcile, lease, prepared.attempt_id,
        broker_order_id=report.broker_order_id, client_order_id=report.client_order_id,
        status=report.event_type, filled_quantity=report.filled_quantity,
        observed_at=report.reported_at, now=clock())


async def recover_exit(service, lease, journal, order_id, attempt, clock):
    """Read/reconcile the persisted exit identity; this function cannot submit."""
    def load(session):
        journal.store._leased(session, lease, clock())
        row = session.get(BrokerOrder, order_id)
        if row is None:
            raise ValueError("orb_stop_prior_exit_missing")
        entry_id = UUID(str((row.payload or {})["entry_order_id"]))
        entry = session.get(BrokerOrder, entry_id)
        account, strategy = session.get(BrokerAccount, row.broker_account_id), session.get(Strategy, row.strategy_id)
        intent = session.get(TradeIntent, row.intent_id)
        if (entry is None or account is None or strategy is None or intent is None
                or row.strategy_id != lease.strategy_id or row.broker_account_id not in lease.account_ids
                or not _live_orb_scope(strategy, account)
                or entry.strategy_id != row.strategy_id or entry.broker_account_id != row.broker_account_id
                or entry.symbol != row.symbol or row.side != "sell" or not row.client_order_id
                or not row.broker_order_id or intent.side != "sell" or intent.intent_type != "close"
                or (row.payload or {}).get("orbroute1_child") == "true"
                or (intent.reason == owner.STOP_REASON and attempt is None)):
            raise ValueError("orb_stop_prior_exit_identity_unknown")
        metadata = {**(row.payload or {}), "broker_order_id": row.broker_order_id}
        if account.provider == "webull":
            # Existing strict ORB original-coid read, not a native-child identity.
            metadata.update({"orb_country_route": "true", "orb_country_broker": "webull",
                             "orbroute1_fresh_read": "true"})
        request = OrderRequest(row.client_order_id, account.name, strategy.code, row.symbol,
            "sell", "close", row.quantity, intent.reason, metadata,
            row.order_type or "market", row.time_in_force or "day")
        target = owner._Target(entry.id, entry.strategy_id, entry.broker_account_id, account.name,
            entry.symbol, entry.broker_order_id or "", entry.client_order_id, row.quantity,
            entry.quantity, (entry.payload or {}).get("orb_country_route") == "true")
        return request, target, intent.id, row.broker_order_id
    request, target, intent_id, broker_id = await service._run_db(load, commit=False)
    report = await service.broker_adapter.fetch_order_update(request)
    observed_at = clock()  # Read observation receipt, NEVER substituted for execution time.
    if (report is None or report.origin != "broker" or report.client_order_id != request.client_order_id
            or report.broker_order_id != broker_id or report.symbol != request.symbol or report.side != "sell"
            or report.intent_type != "close" or report.quantity != request.quantity
            or report.event_type not in {"accepted", "partially_filled", "filled", "cancelled", "rejected"}
            or not _valid_quantity(report.filled_quantity) or report.filled_quantity > request.quantity
            or (report.event_type == "filled" and report.filled_quantity != request.quantity)
            or report.reported_at.tzinfo is None or report.reported_at > observed_at):
        raise ValueError("orb_stop_prior_exit_read_unknown")
    with service.session_factory() as session:
        journal.store._leased(session, lease, clock())
        row = session.get(BrokerOrder, order_id)
        if (row.client_order_id != request.client_order_id or row.broker_order_id != broker_id
                or row.intent_id != intent_id or row.strategy_id != lease.strategy_id
                or row.broker_account_id != target.account_id):
            raise ValueError("orb_stop_prior_exit_changed_during_read")
        intent = session.get(TradeIntent, intent_id)
        writer_report = _incremental_report(session, row, report)
        event = TradeIntentEvent(event_id=UUID(str(intent.payload["event_id"])), source_service="oms-risk",
            payload=TradeIntentPayload(strategy_code="orb_schwab", broker_account_name=target.account,
                symbol=target.symbol, side="sell", intent_type="close", quantity=request.quantity,
                reason=intent.reason, metadata=dict(request.metadata)))
        events = await service._record_order_reports(session=session, intent=intent,
            strategy_id=target.strategy_id, broker_account_id=target.account_id,
            intent_event=event, request=request, reports=[writer_report])
        session.commit()
    for event in events:
        await service._publish_order_event(event)
    if attempt is not None and attempt.status != "TERMINAL":
        await asyncio.to_thread(journal.reconcile, lease, attempt.attempt_id,
            broker_order_id=report.broker_order_id, client_order_id=request.client_order_id,
            status=report.event_type, filled_quantity=report.filled_quantity,
            observed_at=observed_at, now=clock())
    def finish(session):
        journal.store._leased(session, lease, clock())
        entry = session.get(BrokerOrder, target.entry_id)
        remaining = _remaining(session, target)
        vp = service.store.get_virtual_position(session, strategy_id=target.strategy_id,
            broker_account_id=target.account_id, symbol=target.symbol)
        terminal = report.event_type in _TERMINAL
        resolved = terminal and remaining == 0 and vp is not None and vp.quantity == 0
        state = dict((entry.payload or {}).get(owner._STATE) or {})
        entry.payload = {**entry.payload, owner._STATE: {**state,
            "phase": ("resolved_by_stop_fill" if request.reason == owner.STOP_REASON else "resolved_by_prior_exit_fill")
                if resolved else "terminal_exit_remaining" if terminal else "exit_working",
            "exit_order_id": str(order_id), "exit_coid": request.client_order_id,
            **({"terminal_episode_proof": _episode_proof(session, target)} if resolved else {}),
            "last_read_at": observed_at.isoformat(), "updated_at": clock().isoformat()}}
        return "PENDING_CENSUS" if resolved else "BLOCKED_UNKNOWN" if terminal else "EXIT_WORKING"
    return await service._run_db(finish)


async def _recover_known_exits(service, lease, journal, clock):
    from project_mai_tai.db.models import BotControlAttempt
    def load(session):
        journal.store._leased(session, lease, clock())
        entries = session.scalars(select(BrokerOrder).where(BrokerOrder.strategy_id == lease.strategy_id,
            BrokerOrder.broker_account_id.in_(lease.account_ids), BrokerOrder.side == "buy")).all()
        work, blocked = [], False
        for entry in entries:
            state = (entry.payload or {}).get(owner._STATE) or {}
            if not state:
                continue
            coid = state.get("exit_coid")
            if not coid and state.get("event_id"):
                try:
                    event = TradeIntentEvent(event_id=UUID(str(state["event_id"])), source_service="oms-risk",
                        payload=TradeIntentPayload(strategy_code="orb_schwab", broker_account_name="bound-read-only",
                            symbol=entry.symbol, side="sell", intent_type="close", quantity=entry.quantity,
                            reason=str(state.get("reason") or "ORB_END_OF_DAY_CLOSE")))
                    coid = service._build_client_order_id(event)
                except (TypeError, ValueError):
                    blocked = True
                    continue
            row = session.scalar(select(BrokerOrder).where(BrokerOrder.strategy_id == lease.strategy_id,
                BrokerOrder.broker_account_id == entry.broker_account_id, BrokerOrder.symbol == entry.symbol,
                BrokerOrder.side == "sell", BrokerOrder.client_order_id == coid,
                BrokerOrder.payload["entry_order_id"].as_string() == str(entry.id))) if coid else None
            if row is None:
                if state.get("phase") not in {"resolved_by_child_fill", "resolved_by_stop_fill", "resolved_by_prior_exit_fill"}:
                    blocked = True  # Missing original prewire identity is the disclosed normal-exit limit.
                continue
            if state.get("reason") == owner.STOP_REASON and _terminal_stop_proven(session, lease, entry, row, state):
                continue
            if state.get("reason") == owner.STOP_REASON and state.get("command_id") != str(lease.command_id):
                blocked = True
                continue  # Never reconcile an old command using a new STOP lease.
            attempts = session.scalars(select(BotControlAttempt).where(
                BotControlAttempt.command_id == lease.command_id, BotControlAttempt.target_order_id == row.id,
                BotControlAttempt.operation.in_(("SUBMIT_EXIT", "ADOPT_EXISTING_EXIT")))).all()
            if len(attempts) > 1:
                raise ValueError("orb_stop_prior_exit_attempt_ambiguous")
            attempt = SimpleExitAttempt(attempts[0].attempt_id, attempts[0].status) if attempts else None
            work.append((row.id, attempt))
        return work, blocked
    work, blocked = await service._run_db(load, commit=False)
    phase = "BLOCKED_UNKNOWN" if blocked else None
    for identity, attempt in work:
        result = await recover_exit(service, lease, journal, identity, attempt, clock)
        if result == "BLOCKED_UNKNOWN" or phase != "BLOCKED_UNKNOWN" and result == "EXIT_WORKING":
            phase = result
    return phase


async def _recover_flat_native(service, lease, journal, clock):
    """Revisit an already-dispatched tree when no positive VP target remains."""
    from project_mai_tai.db.models import BotControlAttempt
    lock = service.__dict__.setdefault("_orb_exit_claim_lock", Lock())
    if not lock.acquire(blocking=False):
        return "RECONCILING_PROTECTION"
    lock.release()  # No SQL even for inventory when the admission mutex is already busy.
    def load(session):
        journal.store._leased(session, lease, clock())
        attempts = session.scalars(select(BotControlAttempt).where(
            BotControlAttempt.command_id == lease.command_id,
            BotControlAttempt.binding_kind == "orb_native_tree",
            BotControlAttempt.operation == "CANCEL_PROTECTION",
            BotControlAttempt.status.in_(("MAY_HAVE_SENT", "WORKING", "UNKNOWN", "TERMINAL")),
        )).all()
        result = []
        for attempt in attempts:
            entry = session.get(BrokerOrder, attempt.entry_order_id)
            if entry is None:
                raise ValueError("orb_stop_native_recovery_parent_missing")
            vp = service.store.get_virtual_position(session, strategy_id=lease.strategy_id,
                broker_account_id=entry.broker_account_id, symbol=entry.symbol)
            if vp is not None and _valid_quantity(vp.quantity) and vp.quantity == 0:
                account = session.get(BrokerAccount, entry.broker_account_id)
                if account is None:
                    raise ValueError("orb_stop_native_recovery_account_missing")
                result.append((entry.id, attempt.attempt_id, account.name, entry.symbol))
        return result
    candidates = await service._run_db(load, commit=False)
    blocked, contended = False, False
    for entry_id, attempt_id, account_name, symbol in candidates:
        context = OrbStopContext(service, lease, journal, clock)
        def bind(session):
            command = context._lease(session)
            entry = session.get(BrokerOrder, entry_id)
            attempt = session.get(BotControlAttempt, attempt_id)
            account = session.get(BrokerAccount, entry.broker_account_id)
            state = (entry.payload or {}).get(owner._STATE) or {}
            digest = sha256(f"{lease.strategy_id}:{entry.id}:{entry.broker_order_id}:"
                f"{entry.client_order_id}:None:{entry.broker_order_id}".encode()).hexdigest()
            if (attempt.command_id != lease.command_id or attempt.strategy_id != lease.strategy_id
                    or entry.strategy_id != lease.strategy_id or attempt.generation != lease.generation
                    or attempt.execution_mode != "live" or attempt.account_id != entry.broker_account_id
                    or str(entry.broker_account_id) not in command.account_scope["account_ids"]
                    or attempt.symbol != entry.symbol or attempt.parent_intent_id != entry.intent_id
                    or attempt.binding_key != entry.broker_order_id or attempt.managed_row_id is not None
                    or attempt.target_order_id is not None or attempt.dispatch_intent_at is None
                    or attempt.owner_evidence_hash != digest or attempt.owner_evidence_revision != lease.generation
                    or not _live_orb_scope(session.get(Strategy, entry.strategy_id), account)
                    or state.get("generation") != lease.generation
                    or not state.get("native_binding") or state["native_binding"] != context._native_binding(entry)):
                raise ValueError("orb_stop_native_recovery_binding_unknown")
            target = owner._Target(entry.id, entry.strategy_id, entry.broker_account_id, account.name,
                entry.symbol, entry.broker_order_id or "", entry.client_order_id, Decimal(0), entry.quantity)
            context.claimed_target, context.native_binding = target, state["native_binding"]
            context.validate_native_owner(session, entry)
            if _remaining(session, target) != 0:
                raise ValueError("orb_stop_native_recovery_not_flat")
            evidence = attempt.terminal_evidence or {}
            proven = (attempt.status == "TERMINAL"
                and evidence.get("source") == "existing_owned_parent_release_reader"
                and evidence.get("entry_order_id") == str(entry.id)
                and evidence.get("entry_broker_order_id") == entry.broker_order_id
                and evidence.get("binding") == entry.broker_order_id and evidence.get("outcome") == "released")
            return target, account.provider, proven
        try:
            key = (account_name, symbol, str(entry_id))
            if not lock.acquire(blocking=False):
                contended = True
                continue
            try:
                claims = service.__dict__.setdefault("_orb_exit_claims", set())
                if key in claims:
                    contended = True
                    continue
                claims.add(key)
            finally:
                lock.release()
            try:
                target, provider, proven = await service._run_db(bind, commit=False)
                if (target.account, target.symbol, str(target.entry_id)) != key:
                    raise ValueError("orb_stop_native_recovery_scope_changed")
                if not proven:
                    if provider == "webull":
                        from project_mai_tai.oms.orb_country import read_terminal_protection_for_stop
                        proven = await read_terminal_protection_for_stop(service, entry_id, stop_context=context)
                    elif provider == "schwab":
                        # Same original parent reader as normal ORB. No cancellation method here.
                        await service._poll_orb_schwab_child_exits(entry_id=entry_id)
                        proof = await service.broker_adapter.fetch_exit_legs_for_entry(target.account, target.broker_id)
                        proven = (isinstance(proof, dict) and proof.get("unsafe") is False
                            and isinstance(proof.get("working"), list) and not proof["working"]
                            and proof.get("filled") is True)
                    if not proven:
                        blocked = True
                        continue
                    await asyncio.to_thread(journal.record_native_outcome, lease, attempt_id,
                        outcome="released", now=clock())
                def finish(session):
                    entry = session.get(BrokerOrder, entry_id)
                    context.validate_native_owner(session, entry)
                    vp = service.store.get_virtual_position(session, strategy_id=lease.strategy_id,
                        broker_account_id=target.account_id, symbol=target.symbol)
                    if _remaining(session, target) != 0 or vp is None or vp.quantity != 0:
                        raise ValueError("orb_stop_native_recovery_balance_changed")
                    state = (entry.payload or {}).get(owner._STATE) or {}
                    # Do not replace an existing software-exit state with a native-only phase.
                    if not state.get("exit_order_id") and not state.get("exit_coid"):
                        entry.payload = {**entry.payload, owner._STATE: {**state,
                            "phase": "resolved_by_child_fill", "updated_at": clock().isoformat()}}
                await service._run_db(finish)
            finally:
                with lock:
                    service._orb_exit_claims.discard(key)
        except (ValueError, RuntimeError):
            blocked = True
    return "BLOCKED_UNKNOWN" if blocked else "RECONCILING_PROTECTION" if contended else None


@dataclass(frozen=True)
class SimpleExitAttempt:
    attempt_id: object
    status: str


async def drive_orb_stop(service, lease, journal, *, clock=None):
    """Register in main's executor; no account-flat inference or automatic resend."""
    from project_mai_tai.oms.bot_control_executor import StopProgress
    if not getattr(service.settings, "orb_country_routing_enabled", False):
        return StopProgress("BLOCKED_UNKNOWN")
    clock = clock or (lambda: datetime.now(UTC))
    context = OrbStopContext(service, lease, journal, clock)
    def load(session, *, entries_only=False):
        command = journal.store._leased(session, lease, clock())
        strategy = session.get(Strategy, lease.strategy_id)
        if (strategy is None or strategy.code != "orb_schwab" or strategy.execution_mode != "live"
                or not lease.scope_complete or set(command.account_scope["account_ids"]) != {str(v) for v in lease.account_ids}):
            raise ValueError("orb_stop_scope_unknown")
        orders = session.scalars(select(BrokerOrder).where(BrokerOrder.strategy_id == strategy.id,
            BrokerOrder.broker_account_id.in_(lease.account_ids), BrokerOrder.side == "buy",
            or_(BrokerOrder.status.not_in(_TERMINAL), BrokerOrder.status.is_(None)))).all()
        if entries_only:
            return tuple(row.id for row in orders)
        positions = session.scalars(select(VirtualPosition).where(VirtualPosition.strategy_id == strategy.id,
            VirtualPosition.broker_account_id.in_(lease.account_ids), VirtualPosition.quantity != 0)).all()
        targets, held_blocks = [], []
        for position in positions:
            if not _valid_quantity(position.quantity):
                held_blocks.append((str(position.broker_account_id), position.symbol, "UNKNOWN", "orb_stop_vp_unknown"))
                continue
            entry = session.scalar(select(BrokerOrder).where(BrokerOrder.strategy_id == strategy.id,
                BrokerOrder.broker_account_id == position.broker_account_id,
                BrokerOrder.symbol == position.symbol, BrokerOrder.side == "buy",
            ).order_by(BrokerOrder.submitted_at.desc(), BrokerOrder.id.desc()).limit(1))
            account = session.get(BrokerAccount, position.broker_account_id)
            if entry is None or not _live_orb_scope(strategy, account):
                held_blocks.append((str(position.broker_account_id), position.symbol,
                    str(position.quantity), "orb_stop_parent_unknown"))
                continue
            from project_mai_tai.oms.orb_country import entry_quantity
            targets.append(owner._Target(entry.id, strategy.id, account.id, account.name, entry.symbol,
                entry.broker_order_id or "", entry.client_order_id, position.quantity, entry_quantity(entry),
                (entry.payload or {}).get("orb_country_route") == "true"))
        return targets, held_blocks
    # Entry cleanup must not depend on a different held target's SELL eligibility.
    entries = await service._run_db(lambda session: load(session, entries_only=True), commit=False)
    if entries:
        blocked = False
        for identity in entries:
            try:
                await _cancel_entry(service, lease, journal, identity, clock)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                blocked = True
                from project_mai_tai.oms.bot_control_legacy import LegacyStopDriver
                def alert_identity(session):
                    row = session.get(BrokerOrder, identity)
                    if row is None or row.strategy_id != lease.strategy_id or row.broker_account_id not in lease.account_ids:
                        raise ValueError("orb_stop_entry_alert_identity_unknown")
                    return str(row.broker_account_id), row.symbol
                account, symbol = await service._run_db(alert_identity, commit=False)
                await LegacyStopDriver(service, "orb_schwab")._alert(
                    lease, account, symbol, "UNKNOWN", str(exc))
        return StopProgress("BLOCKED_UNKNOWN" if blocked else "CANCELLING_ENTRIES")
    native_phase = await _recover_flat_native(service, lease, journal, clock)
    recovery_phase = await _recover_known_exits(service, lease, journal, clock)
    if native_phase == "BLOCKED_UNKNOWN":
        recovery_phase = native_phase
    elif native_phase == "RECONCILING_PROTECTION" and recovery_phase is None:
        recovery_phase = native_phase
    targets, held_blocks = await service._run_db(load, commit=False)
    if held_blocks:
        from project_mai_tai.oms.bot_control_legacy import LegacyStopDriver
        for account, symbol, quantity, reason in held_blocks:
            await LegacyStopDriver(service, "orb_schwab")._alert(lease, account, symbol, quantity, reason)
        recovery_phase = "BLOCKED_UNKNOWN"
    for target in targets:
        boot_reason = _boot_guard(service, target)
        if boot_reason:
            await context.report_boot_block(target, boot_reason)
            recovery_phase = "BLOCKED_UNKNOWN"
            continue  # Leave the normal exit/protection owner and its durable state untouched.
        try:
            acquired = await owner._attempt(service, target, uuid4(), clock,
                reason=owner.STOP_REASON, stop_context=context)
            if not acquired:
                if context.prior_unresolved and recovery_phase is None:
                    recovery_phase = "BLOCKED_UNKNOWN"
                continue
        except Exception as exc:
            await owner._finish(service, target, "needs_attention", clock())
            await owner._incident(service, target, str(exc), clock())
            return StopProgress("BLOCKED_UNKNOWN")
    if targets:
        return StopProgress(recovery_phase or "EXIT_WORKING")
    if recovery_phase in {"BLOCKED_UNKNOWN", "EXIT_WORKING", "RECONCILING_PROTECTION"}:
        return StopProgress(recovery_phase)
    try:
        census = await service._run_db(lambda session: trusted_census(session, lease, journal, clock()), commit=False)
    except ValueError:
        return StopProgress("BLOCKED_UNKNOWN")
    return StopProgress("SETTLED", census)


class OrbStopDriver:
    """Executor seam installed only with the ORB routing layer enabled."""

    def __init__(self, service):
        self.service = service

    async def __call__(self, lease, journal):
        return await drive_orb_stop(self.service, lease, journal)
