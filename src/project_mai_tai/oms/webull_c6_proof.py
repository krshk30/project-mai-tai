"""Exact WBATTACH originals under the existing C6 lease and managed sell claim."""
import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
import json
from time import monotonic
from uuid import UUID

from sqlalchemy import select

from project_mai_tai.db.models import BrokerAccount, BrokerOrder, Fill, OmsManagedPosition, TradeIntent
from project_mai_tai.oms import webull_attach_riskoff as wb


def inventory(session, row, entry, account):
    """No symbol/age discovery: every address and intent comes from this journal."""
    if account.provider != "webull" or wb.KEY not in (entry.payload or {}):
        return ()
    state = entry.payload[wb.KEY]
    if (entry.broker_account_id != account.id or entry.symbol != row.symbol
            or row.entry_order_id != entry.id or row.entry_client_order_id != entry.client_order_id
            or not isinstance(state, dict) or state.get("entry_id") != str(entry.id)
            or state.get("managed_id") != str(row.id) or state.get("history_proven") is not True
            or state.get("legacy_base") or not isinstance(state.get("attempts"), list)
            or not 0 < len(state["attempts"]) <= 512):
        raise ValueError("c6_original_journal_inventory_UNKNOWN")
    originals, seen = [], set()
    for attempt in state["attempts"]:
        base = attempt.get("base")
        quantity = Decimal(attempt["quantity"])
        intent = session.get(TradeIntent, UUID(attempt["intent_id"]))
        if (not isinstance(base, str) or not base or base != base.strip() or base in seen
                or attempt.get("phase") not in {"unknown", "accepted", "released", "rejected_no_wire"}
                or not quantity.is_finite() or not 0 < quantity <= entry.quantity
                or intent is None or intent.id == entry.intent_id or intent.reason != "WBATTACH1_PROTECTION"
                or intent.strategy_id != entry.strategy_id or intent.broker_account_id != account.id
                or intent.symbol != row.symbol or intent.side != "sell" or intent.intent_type != "close"
                or intent.quantity != quantity
                or (intent.payload or {}).get("metadata", {}).get("entry_order_id") != str(entry.id)):
            raise ValueError("c6_original_protection_binding_UNKNOWN")
        seen.add(base)
        originals.append(dict(attempt))
    return tuple(originals)


def original_child(order, originals, entry, account):
    for attempt in originals:
        expected = {wb.WebullBrokerAdapter._combo_leg_coid(attempt["base"], s) for s in ("T", "S")}
        if order.client_order_id not in expected:
            continue
        if (order.intent_id != UUID(attempt["intent_id"]) or order.strategy_id != entry.strategy_id
                or order.broker_account_id != account.id or order.symbol != entry.symbol
                or order.side != "sell" or order.quantity != Decimal(attempt["quantity"])):
            raise ValueError("c6_original_child_binding_UNKNOWN")
        return True
    return False


def _identity(originals):
    return tuple((a["base"], a["intent_id"], a["quantity"]) for a in originals)


def _owner(service, context, account, symbol):
    from project_mai_tai.oms.bot_control_atr import current_stop
    from project_mai_tai.oms.service import _CONTROL_MANAGED_EMIT
    owner = _CONTROL_MANAGED_EMIT.get()
    key = (account, symbol.upper(), context.managed_id)
    if (current_stop(service, context.managed_id) is not context
            or context.owner_task is not asyncio.current_task() or owner is None
            or owner.get("claim") is None or owner["claim"][1] != key
            or service.__dict__.get("_control_managed_exit_claims", {}).get(key) is not owner
            or service.__dict__.get("_control_managed_exit_symbol_owners", {}).get(key[:2]) is not owner):
        raise ValueError("c6_original_managed_owner_UNKNOWN")
    # The established C6 journal validates the generation/lease at every prepare,
    # dispatch and outcome write. The proof never creates a second seller.
    return owner


@dataclass
class Proof:
    account: str
    symbol: str
    coid: str
    originals: tuple
    list_at: datetime
    terminal_at: float
    attempt_id: object


async def prepare(service, context, row, entry, account, originals):
    from project_mai_tai.oms.bot_control_atr import _hash
    _owner(service, context, account.name, row.symbol)
    owners = service.__dict__.setdefault("_wbattach_exit_owners", {})
    key = (account.name, row.symbol)
    if key in owners and owners[key] is not context:
        raise ValueError("c6_original_protection_owner_busy")
    owners[key] = context
    context.original_owner_key = key
    binding = _hash({"managed": str(row.id), "entry": str(entry.id), "originals": _identity(originals)})
    audit = await asyncio.to_thread(context.journal.prepare_native, context.lease,
        entry_id=entry.id, managed_id=row.id, binding_key=binding, now=datetime.now(UTC))
    if audit.status == "PREPARED":
        await asyncio.to_thread(context.journal.dispatch, context.lease, audit.attempt_id, now=datetime.now(UTC))

    def mark(unit, parent, managed, acct, strategy, owned, journal):
        context.journal.store._leased(unit, context.lease, datetime.now(UTC))
        current = inventory(unit, managed, parent, acct)
        if (_identity(current) != _identity(originals)
                or journal.get("exit_phase") not in {None, "resolved_by_child_fill"}):
            raise ValueError("c6_original_episode_or_exit_changed")
        journal["exit_reason"] = "KILLSW1_STOP"
    await wb._atr_state(service, account.name, row.symbol, entry.client_order_id, mark, allow_closed=True)
    # Cancel once per durable original attempt, regardless of a legacy release latch.
    for attempt in originals:
        if attempt["phase"] == "released" or attempt.get("cancel_requested"):
            continue
        base = attempt["base"]
        def requested(unit, parent, managed, acct, strategy, owned, journal):
            context.journal.store._leased(unit, context.lease, datetime.now(UTC))
            if _identity(inventory(unit, managed, parent, acct)) != _identity(originals):
                raise ValueError("c6_original_inventory_changed_before_cancel")
            journal["attempts"] = [{**a, "cancel_requested": True} if a["base"] == base else a
                                   for a in journal["attempts"]]
        await wb._atr_state(service, account.name, row.symbol, entry.client_order_id, requested, allow_closed=True)
        _owner(service, context, account.name, row.symbol)
        service.logger.info("[WBATTACH1-CANCEL] actor=C6_OWNER account=%s symbol=%s entry=%s base=%s action=request terminal=UNKNOWN",
                            account.name, row.symbol, entry.id, base)
        try:
            await service.broker_adapter.cancel_exit_pair(broker_account_name=account.name,
                symbol=row.symbol, base_client_order_id=base)
        except asyncio.CancelledError:
            raise
        except Exception:
            service.logger.exception("[WBATTACH1-CANCEL] actor=C6_OWNER base=%s action=call_unknown terminal=UNKNOWN", base)
        completed = datetime.now(UTC).isoformat()
        def cancelled(unit, parent, managed, acct, strategy, owned, journal):
            journal["c6_list_after"] = completed
        await wb._atr_state(service, account.name, row.symbol, entry.client_order_id, cancelled, allow_closed=True)
    def fence(unit, parent, managed, acct, strategy, owned, journal):
        journal.setdefault("c6_list_after", datetime.now(UTC).isoformat())
    state, _, _, _, _ = await wb._atr_state(service, account.name, row.symbol, entry.client_order_id,
                                            fence, allow_closed=True)
    after = datetime.fromisoformat(state["c6_list_after"])
    if after.tzinfo is None:
        raise ValueError("c6_original_cancel_fence_UNKNOWN")
    result = await service.broker_adapter.confirm_exit_pair_from_today_list(
        broker_account_name=account.name, symbol=row.symbol, base_client_order_id=originals[-1]["base"],
        quantity=Decimal(originals[-1]["quantity"]), after=after, original_attempts=state["attempts"])
    list_at, proofs = None, {}
    for attempt in state["attempts"]:
        expected = {wb.WebullBrokerAdapter._combo_leg_coid(attempt["base"], s) for s in ("T", "S")}
        reports = tuple(r for r in result.reports if r is not None and r.client_order_id in expected)
        known = wb.validated_child_reads(reports, attempt["base"], Decimal(attempt["quantity"]), row.symbol, datetime.now(UTC))
        # Positive known executions must reach the ledger even when another child
        # is missing, working, or unreadable. Cumulative accounting is idempotent.
        if known:
            await wb._record_atr_children(service, account.name, row.symbol, entry.client_order_id, attempt, known)
        proofs[attempt["base"]] = reports
    if result.outcome != "released":
        raise ValueError("exit waiting for broker proof: " + row.symbol)
    for attempt in state["attempts"]:
        reports = proofs[attempt["base"]]
        if not wb.terminal_pair(reports, attempt["base"], Decimal(attempt["quantity"]), datetime.now(UTC), symbol=row.symbol):
            raise ValueError("exit waiting for broker proof: " + row.symbol)
        for report in reports:
            census = json.loads(report.metadata.get("wbattach1_list_census", "null"))
            if (not isinstance(census, dict) or census.get("complete") is not True
                    or census.get("truncated") is not False or census.get("broker_account_name") != account.name
                    or census.get("symbol") != row.symbol or census.get("base") != attempt["base"]
                    or census.get("original_bases") != sorted(a["base"] for a in originals)
                    or type(census.get("pages")) is not int or not 1 <= census["pages"] <= 20):
                raise ValueError("exit waiting for broker proof: " + row.symbol)
            started, acquired = (datetime.fromisoformat(census[k]) for k in ("started_at", "acquired_at"))
            if (started.tzinfo is None or acquired.tzinfo is None or started <= after
                    or not 0 <= (acquired - started).total_seconds() <= 30
                    or not 0 <= (datetime.now(UTC) - acquired).total_seconds() <= 2):
                raise ValueError("exit waiting for broker proof: " + row.symbol)
            list_at = min(list_at or acquired, acquired)

    def settled(unit, parent, managed, acct, strategy, owned, journal):
        if _identity(inventory(unit, managed, parent, acct)) != _identity(originals):
            raise ValueError("c6_original_inventory_changed")
        settled_attempts = []
        for attempt in journal["attempts"]:
            reports = proofs[attempt["base"]]
            for report in reports:
                child = unit.scalar(select(BrokerOrder).where(BrokerOrder.client_order_id == report.client_order_id))
                if child is not None:
                    if (not original_child(child, originals, parent, acct)
                            or child.broker_order_id not in {None, report.broker_order_id}):
                        raise ValueError("c6_original_native_identity_changed")
                    fills = unit.scalars(select(Fill).where(Fill.order_id == child.id)).all()
                    if sum((f.quantity for f in fills), Decimal(0)) != report.filled_quantity:
                        raise ValueError("c6_original_execution_conservation_UNKNOWN")
                    child.status, child.broker_order_id = report.event_type, report.broker_order_id
            # The journal is a JSON column, not a recursively tracked container.
            # Never mutate a nested value shared with the old payload in place.
            settled_attempts.append({**attempt, "phase": "released",
                                     "terminal_proof": [wb.report_payload(r) for r in reports]})
            unit.get(TradeIntent, UUID(attempt["intent_id"])).status = (
                "filled" if sum((r.filled_quantity for r in reports), Decimal(0)) == Decimal(attempt["quantity"])
                else "cancelled")
        journal["attempts"] = settled_attempts
        if managed.status == "closed" and managed.current_quantity == 0 and owned == 0:
            journal["exit_phase"] = "resolved_by_child_fill"
    await wb._atr_state(service, account.name, row.symbol, entry.client_order_id, settled, allow_closed=True)
    audit = await asyncio.to_thread(context.journal.record_native_outcome, context.lease,
        audit.attempt_id, outcome="released", now=datetime.now(UTC))
    proof = Proof(account.name, row.symbol, entry.client_order_id, originals, list_at, monotonic(), audit.attempt_id)
    context.original_proof = proof
    return proof


async def ceiling(service, context, row_id, *, unsent_order=None):
    from project_mai_tai.oms.bot_control_atr import _managed
    proof = context.original_proof
    _owner(service, context, proof.account, proof.symbol)
    if not 0 <= (datetime.now(UTC) - proof.list_at).total_seconds() <= 2:
        raise ValueError("exit waiting for broker proof: " + proof.symbol)
    def read(unit):
        context.journal.store._leased(unit, context.lease, datetime.now(UTC))
        current, parent, account, own = _managed(service, unit, context.lease, row_id,
            unsent_order=unsent_order, protection_terminal=True)
        _terminal_inventory(unit, current, parent, account, proof)
        return current, parent, account, own
    current, parent, account, own = await service._run_db(read, commit=False)
    amount = await wb.broker_ceiling(service, proof.account, proof.symbol, own,
                                    after=proof.terminal_at, allow_flat=True)
    latest = await service._run_db(read, commit=False)  # Re-fence lease/ownership after the provider await.
    if latest[3] != own:
        raise ValueError("c6_original_owned_quantity_changed")
    _owner(service, context, proof.account, proof.symbol)
    if not 0 <= (datetime.now(UTC) - proof.list_at).total_seconds() <= 2:
        raise ValueError("exit waiting for broker proof: " + proof.symbol)
    return current, parent, account, amount, datetime.now(UTC)


async def flat(service, context, row_id):
    proof = context.original_proof
    _owner(service, context, proof.account, proof.symbol)
    def close(unit):
        context.journal.store._leased(unit, context.lease, datetime.now(UTC))
        row = unit.get(OmsManagedPosition, row_id)
        entry = unit.get(BrokerOrder, row.entry_order_id)
        account = unit.get(BrokerAccount, entry.broker_account_id)
        if row.entry_client_order_id != proof.coid or row.broker_account_name != proof.account or row.symbol != proof.symbol:
            raise ValueError("c6_original_flat_episode_changed")
        _terminal_inventory(unit, row, entry, account, proof)
        service.store.close_managed_position(unit, row)
        state = dict(entry.payload[wb.KEY])
        state["exit_phase"] = "resolved_by_broker_flat"
        entry.payload = {**entry.payload, wb.KEY: state}
    await service._run_db(close, commit=True)
    context.phase = "RECONCILING_NATIVE"
    await wb.critical(service, proof.account, proof.symbol,
        "broker flat before software exit; execution ledger reconciliation required", entry_id=proof.coid)


def _terminal_inventory(unit, row, entry, account, proof):
    originals = inventory(unit, row, entry, account)
    if (_identity(originals) != _identity(proof.originals)
            or not 0 <= (datetime.now(UTC) - proof.list_at).total_seconds() <= 2):
        raise ValueError("c6_original_terminal_inventory_changed")
    for attempt in originals:
        reports = [wb.saved_report(p) for p in attempt.get("terminal_proof", [])]
        if attempt["phase"] != "released" or not wb.terminal_pair(reports, attempt["base"],
                Decimal(attempt["quantity"]), datetime.now(UTC), symbol=row.symbol):
            raise ValueError("c6_original_terminal_inventory_UNKNOWN")


def release_owner(service, context):
    key = getattr(context, "original_owner_key", None)
    owners = service.__dict__.get("_wbattach_exit_owners", {})
    if key is not None and owners.get(key) is context:
        owners.pop(key)


async def report_wait(service, context):
    """Resolve only this managed UUID; never invent an episode from symbol/age."""
    def binding(unit):
        row = unit.get(OmsManagedPosition, UUID(context.managed_id))
        if row is None or row.strategy_code != "schwab_1m_v2" or row.entry_order_id is None:
            return None
        entry = unit.get(BrokerOrder, row.entry_order_id)
        account = unit.scalar(select(BrokerAccount).where(BrokerAccount.name == row.broker_account_name))
        if (entry is None or account is None or account.provider != "webull"
                or entry.broker_account_id != account.id or entry.strategy_id != context.lease.strategy_id
                or account.id not in context.lease.account_ids or entry.symbol != row.symbol
                or row.entry_client_order_id != entry.client_order_id or wb.KEY not in (entry.payload or {})):
            return None
        return account.name, row.symbol, entry.id
    bound = await service._run_db(binding, commit=False)
    if bound is not None:
        await wb.critical(service, bound[0], bound[1], "exit waiting for broker proof: " + bound[1], entry_id=bound[2])
