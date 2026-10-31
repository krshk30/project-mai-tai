"""WBEXIT3 close responsibility; this module never grants permission to sell.

The existing managed owner, terminal children, native ceiling, O14 preparation
and final wire proof still authorize each order. An obligation is retained even
when those proofs cannot yet be obtained. No account total becomes bot ownership.
"""

from __future__ import annotations

import asyncio
from contextvars import ContextVar
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from functools import wraps
from math import isfinite
from uuid import UUID, uuid4, uuid5
from zoneinfo import ZoneInfo

from sqlalchemy import select

from project_mai_tai.db.models import BrokerAccount, BrokerOrder, Fill, OmsManagedPosition, Strategy, SystemIncident, TradeIntent
from project_mai_tai.oms.bot_control_scope import controlled_live_scope

SOURCE = "wbexit3"
NAMESPACE = UUID("3ba0790a-e46c-4e43-8677-eb622ce74d6d")
ET = ZoneInfo("America/New_York")
LIMIT = 256
MAX_BACKOFF = 60
PAGE_AFTER = 3
PAGE_SECONDS = 30
_ATTEMPT = ContextVar("wbexit3_attempt", default=None)
FULL_CLOSE_REASONS = frozenset({
    "oms_v2_managed_exit:CW_FLIP", "oms_v2_managed_exit:CONFIRMATION_EXIT",
    "oms_v2_managed_exit:CW_TARGET", "oms_v2_managed_exit:CW_HARD_STOP",
    "oms_v2_managed_exit:CW_FLOOR", "oms_v2_managed_exit:HARD_STOP",
    "oms_v2_managed_exit:FLOOR_BREACH", "V2_OVERNIGHT_FLATTEN", "V2_EOD_CANCEL_REEXIT",
    "WEBULL_ATTACH_RISK_OFF",
})


def enabled(service):
    return getattr(service.settings, "oms_webull_exit3_enabled", False) is True


def _now():
    from project_mai_tai.oms import webull_attach_riskoff as wb
    return wb.datetime.now(UTC)


def _time(value):
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("wbexit3_aware_clock_required")
    return value


def session(now):
    local = _time(now).astimezone(ET)
    minute = local.hour * 60 + local.minute
    from project_mai_tai.strategy_core.time_utils import is_fillable_et_session
    if not is_fillable_et_session(now, 4, 20):
        return "CLOSED"
    return "PM" if minute < 570 else "RTH" if minute < 960 else "AH"


def v2_reason(reason):
    # This is responsibility selection, NEVER permission to convert a partial
    # scale into liquidation. Full-close native proof uses the finite set above.
    return bool(isinstance(reason, str) and len(reason) <= 160 and (
        reason.startswith("oms_v2_managed_exit:") and len(reason) > len("oms_v2_managed_exit:")
        or reason in {"V2_OVERNIGHT_FLATTEN", "V2_EOD_CANCEL_REEXIT", "WEBULL_ATTACH_RISK_OFF"}))


def selected(service, reason, account, *, now=None):
    if not enabled(service) or not v2_reason(reason):
        return False
    provider = getattr(service.settings, "provider_for_account", None)
    if (not callable(provider) or provider(account) != "webull"
            or not controlled_live_scope("schwab_1m_v2", "live", account)):
        return False
    # Existing selected RTH exits receive the approved fresh-read/retry hook.
    from project_mai_tai.oms.wbexit2 import REASONS
    period = session(now or _now())
    return reason in REASONS or period in {"PM", "AH"}


def wire_session(service, reason, account):
    return selected(service, reason, account) and session(_now()) != "CLOSED"


def full_close_selected(service, reason, account):
    return wire_session(service, reason, account) and reason in FULL_CLOSE_REASONS


def partial_request(service, reason, account, kind):
    from project_mai_tai.oms.wbexit3_partial import selected as partial_selected
    return partial_selected(service, reason, account, kind)


def account_session(service, account):
    provider = getattr(service.settings, "provider_for_account", None)
    return bool(enabled(service) and callable(provider) and provider(account) == "webull"
        and controlled_live_scope("schwab_1m_v2", "live", account) and session(_now()) != "CLOSED")


def managed_order_session(service, account, order, strategy):
    metadata = order.payload or {}
    return bool(account_session(service, account) and getattr(strategy, "code", None) == "schwab_1m_v2"
        and order.side == "sell" and metadata.get("oms_v2_managed_exit") == "true"
        and metadata.get("oms_managed_row_id") and metadata.get("entry_order_id"))


def _quantity(value):
    result = Decimal(str(value))
    if not result.is_finite() or result <= 0 or result != result.to_integral_value():
        raise ValueError("wbexit3_positive_whole_quantity_required")
    return result


def prior_software_quantity(db, entry):
    """Actual canonical exits of THIS entry before its full-close obligation.

    Needed after an exact partial job; native original-child fills are accounted
    separately by WB2. This never creates a fill or chooses size from a broker
    total. Missing attribution can only leave the unchanged owned cap refusing.
    """
    records = db.execute(select(Fill, BrokerOrder).join(BrokerOrder, BrokerOrder.id == Fill.order_id)
        .join(TradeIntent, TradeIntent.id == BrokerOrder.intent_id).where(
            TradeIntent.payload["metadata"]["entry_order_id"].as_string() == str(entry.id),
            TradeIntent.intent_type.in_(("scale", "close")), BrokerOrder.side == "sell", Fill.side == "sell",
            BrokerOrder.strategy_id == entry.strategy_id, BrokerOrder.broker_account_id == entry.broker_account_id,
            BrokerOrder.symbol == entry.symbol).limit(257)).all()
    if len(records) > 256:
        raise ValueError("wbexit3_complete_prior_software_execution_inventory_over_bound")
    quantity = Decimal(0)
    for fill, order in records:
        if order.order_type == "oco_exit" or (order.payload or {}).get("wbattach1_child") == "true":
            continue
        if (fill.strategy_id != entry.strategy_id or fill.broker_account_id != entry.broker_account_id
                or fill.symbol != entry.symbol or not fill.broker_fill_id or not order.broker_order_id
                or not fill.quantity.is_finite() or fill.quantity <= 0
                or not fill.price.is_finite() or fill.price <= 0):
            raise ValueError("wbexit3_prior_software_execution_UNKNOWN")
        quantity += fill.quantity
    return quantity


def _binding(service, unit, account, symbol, managed_id):
    row = unit.scalar(select(OmsManagedPosition).where(OmsManagedPosition.id == UUID(managed_id)).with_for_update())
    entry = unit.scalar(select(BrokerOrder).where(BrokerOrder.id == row.entry_order_id).with_for_update()) if row is not None and row.entry_order_id else None
    acct = unit.get(BrokerAccount, entry.broker_account_id) if entry is not None else None
    strategy = unit.get(Strategy, entry.strategy_id) if entry is not None else None
    parent = unit.get(TradeIntent, entry.intent_id) if entry is not None else None
    if (row is None or entry is None or acct is None or strategy is None
            or row.strategy_code != "schwab_1m_v2" or strategy.code != row.strategy_code
            or row.broker_account_name != account or acct.name != account or acct.provider != "webull"
            or not acct.is_active or not controlled_live_scope(strategy.code, strategy.execution_mode, acct.name)
            or row.symbol != symbol or entry.symbol != symbol or entry.side != "buy"
            or row.entry_client_order_id != entry.client_order_id or not entry.broker_order_id
            or parent is None or parent.strategy_id != entry.strategy_id or parent.broker_account_id != entry.broker_account_id
            or parent.symbol != symbol or parent.side != "buy" or parent.intent_type != "open"):
        raise ValueError("wbexit3_actual_owned_episode_UNKNOWN")
    from project_mai_tai.oms.route_owner import _utc_row_time
    anchor = _utc_row_time(entry.submitted_at) or _utc_row_time(parent.created_at)
    if anchor is None or anchor > _now():
        raise ValueError("wbexit3_actual_entry_anchor_UNKNOWN")
    return row, entry, {"managed_id": str(row.id), "entry_id": str(entry.id),
        "entry_coid": entry.client_order_id, "entry_native_id": entry.broker_order_id,
        "strategy_id": str(strategy.id), "account_id": str(acct.id), "account": account, "symbol": symbol,
        "entry_anchor_at": anchor.isoformat()}


def _address(binding, reason, kind, level):
    return uuid5(NAMESPACE, ":".join((binding["managed_id"], binding["entry_id"], reason, kind, level or "")))


def _validate(state, owner_id):
    if (not isinstance(state, dict) or state.get("source") != SOURCE or state.get("schema") != 1
            or state.get("owner_id") != str(owner_id) or state.get("phase") not in {"OPEN", "RETRY", "PAGED", "RESOLVED"}
            or type(state.get("attempts")) is not int or state["attempts"] < 0
            or type(state.get("revision")) is not int or state["revision"] < 1):
        raise ValueError("wbexit3_obligation_corrupt")
    _quantity(state["requested"])
    if _address(state["binding"], state["reason"], state["kind"], state["level"]) != owner_id:
        raise ValueError("wbexit3_durable_address_changed")
    for key in ("opened_at", "next_retry_at"):
        _time(datetime.fromisoformat(state[key]))
    return state


def _remember(service, state):
    cache = service.__dict__.setdefault("_wbexit3_pending", {})
    key = state["owner_id"]
    if state["phase"] == "RESOLVED":
        cache.pop(key, None)
    else:
        cache[key] = deepcopy(state)


async def _joined(work):
    """Join one admitted unit physically; no abandoned DB/SDK future or lease."""
    task = asyncio.create_task(work)
    cancelled = None
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError as exc:
            cancelled = cancelled or exc
        except Exception:
            if cancelled is None:
                raise
            break
    if cancelled is not None:
        if not task.cancelled():
            task.exception()
        raise cancelled
    return task.result()


def obligated_exit(method):
    """Run INSIDE existing #1014/control/ATR owner wrappers; no preclaim await."""
    @wraps(method)
    async def wrapped(service, *args, **kwargs):
        if not enabled(service):
            return await method(service, *args, **kwargs)
        previous = _ATTEMPT.get()
        if previous is not None and previous["service"] is service and previous["task"] is asyncio.current_task():
            return await method(service, *args, **kwargs)  # Shared path -> emitter is ONE invocation.
        account = args[0] if args else kwargs.get("acct")
        symbol = args[1] if len(args) > 1 else kwargs.get("symbol")
        for state in service.__dict__.get("_wbexit3_pending", {}).values():
            if (state["binding"]["account"] == account and state["binding"]["symbol"] == symbol
                    and state["reason"] == kwargs.get("reason") and state["kind"] == kwargs.get("kind")
                    and state["level"] == (kwargs.get("level") or "")):
                if state.get("active_token"):
                    return "wbexit3_prior_physical_invocation_UNKNOWN"
                if datetime.fromisoformat(state["next_retry_at"]) > _now():
                    return "wbexit3_paced"  # Before DB/native awaits; independent hard-stop decision still evaluates.
        context = {"service": service, "task": asyncio.current_task(), "token": str(uuid4()),
                   "state": None, "probes": set(), "guard": "existing_proof_or_order_wait"}
        token = _ATTEMPT.set(context)
        outcome = "UNKNOWN"
        primary = None
        try:
            outcome = await method(service, *args, **kwargs)
            return outcome
        except BaseException as exc:
            primary = exc
            raise
        finally:
            try:
                if context["state"] is not None:
                    try:
                        from project_mai_tai.oms import webull_attach_riskoff as wb
                        call = wb._ATR_CALL.get()
                        if call is not None and getattr(call, "wb3_partial", False):
                            from project_mai_tai.oms.wbexit3_partial import finish_created_unsent
                            await finish_created_unsent(service, call)
                        await finish_attempt(service, context, outcome)
                    except BaseException:
                        service.logger.critical("[WBEXIT3-RECORD-UNKNOWN] owner=%s responsibility=RETAINED", context["state"]["owner_id"])
                        if primary is None:
                            raise
            finally:
                _ATTEMPT.reset(token)
    return wrapped


async def begin_exit(service, row, *, reason, kind, reference_price, sell_qty=None, level=None, context=None,
                     account=None):
    """Called AFTER the actual managed claim, before original-history/ledger guards.

    It stores requested size and proof UNKNOWN, not an owned quantity override.
    Exact existing entry/managed bindings keep manual positions outside the set.
    """
    current = _ATTEMPT.get()
    account = account or row.broker_account_name
    if not selected(service, reason, account):
        return
    if service.__dict__.get("_wbexit3_boot_complete") is not True:
        raise ValueError("wbexit3_complete_boot_inventory_required")
    if (current is None or current["service"] is not service or current["task"] is not asyncio.current_task()):
        raise ValueError("wbexit3_actual_invocation_missing")
    if current["state"] is not None:
        if context is not None:
            context["wbexit3_owner_id"] = current["state"]["owner_id"]
        return
    requested = _quantity(sell_qty if kind == "SCALE" else row.current_quantity)
    managed_id = getattr(row, "managed_row_id", None) or str(row.id)
    metadata = deepcopy(context or {})
    metadata.pop("wbexit3_owner_id", None)
    def unit(db):
        managed, entry, binding = _binding(service, db, account, row.symbol, managed_id)
        riskoff_origin = None
        if reason == "WEBULL_ATTACH_RISK_OFF":
            from project_mai_tai.oms import wbexit3_riskoff
            if not all(metadata.get(wbexit3_riskoff.PREFIX + field) for field in wbexit3_riskoff.FIELDS):
                raise ValueError("wbexit3_riskoff_only_actual_positive_origin_recovery_route")
            _, riskoff_origin, _ = wbexit3_riskoff.read_origin(service, db, account, row.symbol, managed_id,
                supplied=metadata, responsibility_only=True)
        if managed.status != "open" or Decimal(managed.current_quantity) < requested:
            raise ValueError("wbexit3_current_managed_request_changed")
        address = _address(binding, reason, kind, level)
        incident = db.scalar(select(SystemIncident).where(SystemIncident.id == address).with_for_update())
        if incident is None:
            from project_mai_tai.oms import webull_attach_riskoff as wb
            try:
                wb.require_current_episode(entry, db.get(TradeIntent, entry.intent_id), _now())
            except ValueError:
                # A real earlier RTH O14 riskoff intent + positive abort is
                # already durable admission, never a task/nonce to adopt.
                initial_episode_current = riskoff_origin is not None
            else:
                initial_episode_current = True
            now = _now()
            state = {"source": SOURCE, "schema": 1, "owner_id": str(address), "binding": binding,
                "reason": reason, "kind": kind, "level": level or "", "requested": str(requested),
                "reference_price": str(reference_price), "context": metadata, "attempts": 0,
                "initial_episode_current": initial_episode_current,
                "revision": 1, "opened_at": now.isoformat(), "next_retry_at": now.isoformat(),
                "phase": "OPEN", "active_token": current["token"], "last_result": "UNKNOWN", "filled_highwater": "0",
                "native_protection": "UNKNOWN", "last_guard": "initial_proofs_pending"}
            if riskoff_origin is not None:
                state["riskoff_origin"] = riskoff_origin
                state["initial_episode_current_source"] = "actual_RTH_riskoff_O14_positive_unsent_origin"
            incident = SystemIncident(id=address, service_name="oms-risk", severity="info", status="open",
                title=f"Webull exit responsibility: {binding['symbol']}"[:255], opened_at=now, payload=state)
            db.add(incident)
        else:
            state = deepcopy(_validate(incident.payload, address))
            if (state["binding"] != binding
                    or state.get("riskoff_origin") != riskoff_origin
                    or (kind == "SCALE" and requested not in {Decimal(state["requested"]),
                        Decimal(state["requested"]) - Decimal(state.get("filled_highwater", "0"))})
                    or requested > Decimal(state["requested"])
                    or state["phase"] == "RESOLVED" or state.get("active_token") is not None):
                raise ValueError("wbexit3_prior_responsibility_or_physical_owner_requires_read")
            if datetime.fromisoformat(state["next_retry_at"]) > _now():
                raise ValueError("wbexit3_persisted_retry_not_due")
            state.update(active_token=current["token"], revision=state["revision"] + 1)
            incident.payload = state
        db.flush()
        return deepcopy(state)
    # On cancellation the transaction may have committed. Publish its actual
    # receipt before propagating cancellation so finally can release THIS token.
    work = asyncio.create_task(service._run_db(unit, commit=True))
    cancelled = None
    while not work.done():
        try:
            await asyncio.shield(work)
        except asyncio.CancelledError as exc:
            cancelled = cancelled or exc
    state = work.result()
    current["state"] = state
    _remember(service, state)
    if context is not None:
        context["wbexit3_owner_id"] = state["owner_id"]
    if reason == "WEBULL_ATTACH_RISK_OFF":
        from project_mai_tai.oms import webull_attach_riskoff as wb, wbexit3_riskoff
        wbexit3_riskoff.activate(service, wb._ATR_CALL.get(), state)
    if cancelled is not None:
        raise cancelled


async def probe_guard(service, category, *, receipt=None):
    """One fresh position read after each invocation's first specific refusal.

    The observed account quantity is NEVER returned as owned size or written to
    VP/fills. Original terminal/owned gates must run again; error/429 is UNKNOWN.
    Native children are independently reread by the existing WB2 handoff.
    """
    current = _ATTEMPT.get()
    if (current is None or current["service"] is not service or current["task"] is not asyncio.current_task()
            or current["state"] is None or "positions" in current["probes"]):
        return
    current["probes"].add("positions")  # Claim before await; no recursive/read storm.
    current["guard"] = category
    from time import monotonic
    requested = monotonic()
    try:
        result = receipt if receipt is not None else await _joined(service.broker_adapter.read_wbexit2_positions(
            current["state"]["binding"]["account"], after=requested))
        current["position_receipt"] = result
        current["position_requested"] = requested if receipt is None else None
        rows, _, native = result
        matches = [p for p in rows if p.symbol == current["state"]["binding"]["symbol"]]
        current["position_read"] = "HELD" if (len(matches) == 1 and matches[0].quantity > 0) else "UNKNOWN"
        if type(native) is not str or not native:
            current["position_read"] = "UNKNOWN"
    except asyncio.CancelledError:
        raise
    except Exception:
        current["position_read"] = "UNKNOWN"


async def refresh_originals(service):
    """Recover ONE exact locally addressed protection attempt from a fresh census.

    The positive native children plus their original durable protection intent
    can repair missing history / a submitting attach ACK. No absent child or
    guessed account inventory can do so. An old software exit nonce is retained.
    """
    current = _ATTEMPT.get()
    if (current is None or current["state"] is None or current["service"] is not service
            or current["task"] is not asyncio.current_task() or "originals" in current["probes"]):
        return False
    current["probes"].add("originals")
    state = current["state"]
    if state.get("initial_episode_current") is not True:
        return False  # A newly discovered old row cannot manufacture an audited recovery permission.
    binding = state["binding"]
    from project_mai_tai.oms import webull_attach_riskoff as wb, wbexit2
    def snapshot(db):
        row, entry, actual = _binding(service, db, binding["account"], binding["symbol"], binding["managed_id"])
        if actual != binding:
            raise ValueError("wbexit3_original_episode_changed")
        journal = deepcopy((entry.payload or {}).get(wb.KEY) or {})
        if journal.get("exit_phase"):
            return None  # Never steal another durable invocation/SELL reservation.
        wb2_before = None
        marker = journal.get("wbexit2_owner_id")
        if marker:
            prior = db.get(SystemIncident, UUID(marker))
            wb2_before = deepcopy(prior.payload) if prior is not None else None
            if (not isinstance(wb2_before, dict) or wb2_before.get("source") != "wbexit2"
                    or wb2_before.get("invocation") is not None or wb2_before.get("sell") is not None
                    or wb2_before.get("identity", {}).get("entry_id") != binding["entry_id"]
                    or wb2_before.get("identity", {}).get("managed_id") != binding["managed_id"]
                    or wb2_before.get("identity", {}).get("account_id") != binding["account_id"]
                    or wb2_before.get("identity", {}).get("strategy_id") != binding["strategy_id"]):
                return None
        if journal.get("history_proven") is True and not any(
                a.get("phase") == "submitting" for a in journal.get("attempts", [])):
            return None
        attempts = journal.get("attempts", [])
        base = (entry.payload or {}).get("webull_protect_base_client_order_id")
        if not isinstance(attempts, list) or len(attempts) > 1 or not isinstance(base, str) or not base:
            return None
        intents = db.scalars(select(TradeIntent).where(TradeIntent.strategy_id == entry.strategy_id,
            TradeIntent.broker_account_id == entry.broker_account_id, TradeIntent.symbol == entry.symbol,
            TradeIntent.side == "sell", TradeIntent.intent_type == "close", TradeIntent.reason == "WBATTACH1_PROTECTION",
            TradeIntent.payload["metadata"]["entry_order_id"].as_string() == str(entry.id),
            TradeIntent.payload["metadata"]["webull_protect_base_client_order_id"].as_string() == base).limit(2)).all()
        if len(intents) != 1:
            return None
        intent = intents[0]
        quantity = _quantity(intent.quantity)
        if attempts and (attempts[0].get("base") != base or attempts[0].get("intent_id") != str(intent.id)
                         or Decimal(attempts[0]["quantity"]) != quantity):
            raise ValueError("wbexit3_original_local_intent_conflict")
        if wb2_before is not None and {(p["base"], Decimal(p["quantity"])) for p in wb2_before["originals"]} != {(base, quantity)}:
            raise ValueError("wbexit3_retained_original_census_changed")
        attempt = {"base": base, "quantity": str(quantity), "intent_id": str(intent.id),
                   "phase": "unknown", "cancel_requested": bool(attempts and attempts[0].get("cancel_requested"))}
        return journal, attempt, wb2_before
    before = await _joined(service._run_db(snapshot, commit=False))
    if before is None:
        return False
    journal, attempt, wb2_before = before
    current["guard"] = "original_history_native_read"
    started = _now()
    result = await _joined(service.broker_adapter.confirm_exit_pair_from_today_list(
        broker_account_name=binding["account"], symbol=binding["symbol"], base_client_order_id=attempt["base"],
        quantity=Decimal(attempt["quantity"]), after=started, original_attempts=[attempt]))
    import json
    reports = tuple(wb.validated_child_reads(result.reports, attempt["base"], Decimal(attempt["quantity"]), binding["symbol"], _now()))
    if len(reports) != 2 or len({r.broker_order_id for r in reports}) != 2 or any(not r.broker_order_id for r in reports):
        return False
    physical = set()
    for report in reports:
        proof = json.loads(report.metadata.get("wbattach1_list_census", "null"))
        if (not isinstance(proof, dict) or proof.get("complete") is not True or proof.get("truncated") is not False
                or proof.get("broker_account_name") != binding["account"] or proof.get("symbol") != binding["symbol"]
                or proof.get("base") != attempt["base"] or proof.get("original_bases") != [attempt["base"]]
                or type(proof.get("pages")) is not int or not 1 <= proof["pages"] <= 20
                or type(proof.get("account_id")) is not str or not proof["account_id"]):
            return False
        acquired, read_start = (datetime.fromisoformat(proof[k]) for k in ("acquired_at", "started_at"))
        if (acquired.tzinfo is None or read_start.tzinfo is None or read_start < started
                or not 0 <= (_now() - acquired).total_seconds() <= 2
                or not 0 <= (acquired - read_start).total_seconds() <= 30):
            return False
        physical.add(proof["account_id"])
    if len(physical) != 1:
        return False
    if result.outcome == "released":
        wbexit2.positive_original_terminal(reports, attempt["base"], Decimal(attempt["quantity"]), binding["symbol"], _now())
        # Do not label released before attribution. WB2 consumes and records the
        # positive terminal children through its normal, unchanged fill writer.
    elif result.outcome == "reserved":
        wbexit2.original_geometry(reports, attempt["base"], Decimal(attempt["quantity"]), binding["symbol"])
    else:
        return False
    def record(db):
        row, entry, actual = _binding(service, db, binding["account"], binding["symbol"], binding["managed_id"])
        if actual != binding or deepcopy((entry.payload or {}).get(wb.KEY) or {}) != journal:
            raise ValueError("wbexit3_original_preimage_changed_after_read")
        if wb2_before is not None:
            prior = db.scalar(select(SystemIncident).where(SystemIncident.id == UUID(journal["wbexit2_owner_id"])).with_for_update())
            if prior is None or prior.payload != wb2_before:
                raise ValueError("wbexit3_prior_owner_changed_during_original_read")
        attempt["phase"] = "accepted" if result.outcome == "reserved" else "unknown"
        repaired = {**journal, "entry_id": str(entry.id), "managed_id": str(row.id),
                    "attempts": [attempt], "history_proven": True}
        repaired.pop("legacy_base", None)
        entry.payload = {**entry.payload, wb.KEY: repaired}
    await _joined(service._run_db(record, commit=True))
    return True


def existing_obligation_episode(service, entry, managed, account, strategy, intent, now):
    """Only this actual durable retry may preserve its ORIGINAL prior-day entry.

    This is never BUY/entry authority, native proof, orphan-token transfer or an
    account/symbol fallback. A new obligation cannot be retroactively created.
    """
    current = _ATTEMPT.get()
    if (not enabled(service) or current is None or current["service"] is not service or current["state"] is None):
        return False
    state = current["state"]
    if state.get("initial_episode_current") is not True:
        return False
    binding = state["binding"]
    from project_mai_tai.oms.route_owner import _utc_row_time
    anchor = _utc_row_time(entry.submitted_at) or _utc_row_time(intent.created_at if intent is not None else None)
    if (state.get("active_token") != current["token"] or state["phase"] == "RESOLVED"
            or binding["entry_id"] != str(entry.id) or binding["managed_id"] != str(managed.id)
            or binding["entry_coid"] != entry.client_order_id or binding["entry_native_id"] != entry.broker_order_id
            or binding["account_id"] != str(account.id) or binding["account"] != account.name
            or binding["strategy_id"] != str(strategy.id) or binding["symbol"] != entry.symbol
            or strategy.code != "schwab_1m_v2" or account.provider != "webull"
            or intent is None or intent.side != "buy" or intent.intent_type != "open"
            or intent.strategy_id != entry.strategy_id or intent.broker_account_id != entry.broker_account_id
            or intent.symbol != entry.symbol or anchor is None or anchor.isoformat() != binding["entry_anchor_at"]
            or not anchor <= datetime.fromisoformat(state["opened_at"]) <= now):
        raise ValueError("wbexit3_existing_obligation_episode_CHANGED")
    return True


def _execution_complete(service, db, state, row, entry):
    fills = db.scalars(select(Fill).join(BrokerOrder, BrokerOrder.id == Fill.order_id)
        .join(TradeIntent, TradeIntent.id == BrokerOrder.intent_id).where(
        TradeIntent.payload["metadata"]["wbexit3_owner_id"].as_string() == state["owner_id"],
        BrokerOrder.strategy_id == entry.strategy_id, BrokerOrder.broker_account_id == entry.broker_account_id,
        BrokerOrder.symbol == entry.symbol, BrokerOrder.side == "sell", Fill.side == "sell",
        Fill.strategy_id == entry.strategy_id, Fill.broker_account_id == entry.broker_account_id,
        Fill.symbol == entry.symbol, Fill.broker_fill_id.is_not(None)).limit(LIMIT + 1)).all()
    if len(fills) > LIMIT or any(not f.quantity.is_finite() or f.quantity <= 0
            or not f.price.is_finite() or f.price <= 0 for f in fills):
        raise ValueError("wbexit3_attributed_fill_invalid")
    sold = sum((f.quantity for f in fills), Decimal(0))
    if sold > Decimal(state["requested"]):
        raise ValueError("wbexit3_attributed_execution_exceeds_request")
    if sold < Decimal(state.get("filled_highwater", "0")):
        raise ValueError("wbexit3_attributed_execution_regressed")
    state["filled_highwater"] = str(sold)
    native_progress = Decimal(state.get("partial_native_progress", "0")) if state["kind"] == "SCALE" else Decimal(0)
    if native_progress:
        total = Decimal(0)
        for proof in state.get("partial_terminal_reports", []):
            expected = Decimal(proof["filled_quantity"])
            child = db.scalar(select(BrokerOrder).where(BrokerOrder.client_order_id == proof["coid"],
                BrokerOrder.broker_order_id == proof["broker_order_id"], BrokerOrder.strategy_id == entry.strategy_id,
                BrokerOrder.broker_account_id == entry.broker_account_id, BrokerOrder.symbol == entry.symbol,
                BrokerOrder.side == "sell", BrokerOrder.payload["entry_order_id"].as_string() == str(entry.id)))
            actual = list(db.scalars(select(Fill).where(Fill.order_id == child.id).limit(LIMIT + 1))) if child is not None else []
            if (len(actual) > LIMIT or any(f.strategy_id != entry.strategy_id or f.broker_account_id != entry.broker_account_id
                    or f.symbol != entry.symbol or f.side != "sell" or not f.broker_fill_id
                    or not f.quantity.is_finite() or f.quantity <= 0
                    or not f.price.is_finite() or f.price <= 0 for f in actual)
                    or sum((f.quantity for f in actual), Decimal(0)) != expected):
                raise ValueError("wbexit3_partial_native_progress_not_canonically_attributed")
            total += expected
        if total != native_progress:
            raise ValueError("wbexit3_partial_native_progress_census_changed")
    from project_mai_tai.oms.service import _BoundOcoHistoryUnknown
    try:
        native = service._stored_owned_oco_balance(db, row)
    except _BoundOcoHistoryUnknown:
        native = None
    native_complete = native is not None and native[1] > 0 and native[1] == native[2] + sold
    if row.status == "closed" and row.current_quantity == 0 and not native_complete:
        # WBATTACH originals have their OWN protection intent; they are not the
        # native-entry OCO children inspected by _stored_owned_oco_balance.
        # Recheck exact saved terminal IDs AND real canonical fills, never infer
        # completion from the closed row or a WB2 lifecycle status alone.
        from project_mai_tai.oms import webull_attach_riskoff as wb, wbexit2
        journal = (entry.payload or {}).get(wb.KEY) or {}
        attempts = journal.get("attempts", [])
        if (len(attempts) == 1 and journal.get("entry_id") == str(entry.id)
                and journal.get("managed_id") == str(row.id) and journal.get("history_proven") is True):
            attempt = attempts[0]
            proofs = [wb.saved_report(p) for p in attempt.get("terminal_proof", [])]
            if attempt.get("phase") == "released" and proofs:
                proofs = wbexit2.positive_original_terminal(proofs, attempt["base"], Decimal(attempt["quantity"]), entry.symbol, _now())
                exited = Decimal(0)
                for report in proofs:
                    child = db.scalar(select(BrokerOrder).where(BrokerOrder.client_order_id == report.client_order_id,
                        BrokerOrder.broker_order_id == report.broker_order_id,
                        BrokerOrder.intent_id == UUID(attempt["intent_id"]),
                        BrokerOrder.strategy_id == entry.strategy_id, BrokerOrder.broker_account_id == entry.broker_account_id,
                        BrokerOrder.symbol == entry.symbol, BrokerOrder.side == "sell",
                        BrokerOrder.payload["entry_order_id"].as_string() == str(entry.id)))
                    executions = list(db.scalars(select(Fill).where(Fill.order_id == child.id).limit(LIMIT + 1))) if child is not None else []
                    if (len(executions) > LIMIT or any(f.strategy_id != entry.strategy_id
                            or f.broker_account_id != entry.broker_account_id or f.symbol != entry.symbol
                            or f.side != "sell" or not f.broker_fill_id or not f.quantity.is_finite() or f.quantity <= 0
                            or not f.price.is_finite() or f.price <= 0 for f in executions)
                            or sum((f.quantity for f in executions), Decimal(0)) != report.filled_quantity):
                        raise ValueError("wbexit3_terminal_original_execution_not_canonical")
                    exited += report.filled_quantity
                buys = list(db.scalars(select(Fill).where(Fill.order_id == entry.id).limit(LIMIT + 1)))
                if len(buys) > LIMIT or not buys or any(f.side != "buy" or f.strategy_id != entry.strategy_id
                        or f.broker_account_id != entry.broker_account_id or f.symbol != entry.symbol or not f.broker_fill_id
                        or not f.quantity.is_finite() or f.quantity <= 0 or not f.price.is_finite() or f.price <= 0 for f in buys):
                    raise ValueError("wbexit3_original_entry_execution_UNKNOWN")
                vp = service.store.get_virtual_position(db, strategy_id=entry.strategy_id,
                    broker_account_id=entry.broker_account_id, symbol=entry.symbol)
                entered = sum((f.quantity for f in buys), Decimal(0))
                native_complete = (vp is not None and vp.quantity == 0 and exited > 0
                    and entered == exited + prior_software_quantity(db, entry))
    return sold + native_progress >= Decimal(state["requested"]) or native_complete and row.status == "closed"


def _retire_completed_partial_claim(db, state, entry):
    """No physical invocation may be adopted; only a finished sell is retired.

    Normal sync can finish the canonical fill after its creator returned. Its
    exact terminal order plus full attributed goal permits retiring THIS partial
    claim; it supplies no permission for a later independent hard-stop order.
    """
    if state["kind"] != "SCALE" or state.get("active_token") is not None:
        return
    from project_mai_tai.oms import webull_attach_riskoff as wb
    journal = deepcopy((entry.payload or {}).get(wb.KEY) or {})
    previous = state.get("partial_sell")
    if (not previous or journal.get("exit_phase") != "claimed"
            or journal.get("wbexit3_partial_owner_id") != state["owner_id"]
            or journal.get("wbexit3_partial_sell_coid") != previous["coid"]):
        return
    order = db.scalar(select(BrokerOrder).where(BrokerOrder.client_order_id == previous["coid"],
        BrokerOrder.strategy_id == entry.strategy_id, BrokerOrder.broker_account_id == entry.broker_account_id,
        BrokerOrder.symbol == entry.symbol, BrokerOrder.side == "sell"))
    intent = db.get(TradeIntent, order.intent_id) if order is not None else None
    if (order is None or not order.broker_order_id or order.status not in wb.TERMINAL
            or order.quantity != Decimal(previous["quantity"]) or intent is None or intent.intent_type != "scale"
            or (intent.payload or {}).get("metadata", {}).get("wbexit3_owner_id") != state["owner_id"]):
        return  # Pending/unknown ACK keeps the original partial claim intact.
    for key in ("exit_phase", "exit_call_id", "wbexit3_partial_owner_id", "wbexit3_partial_sell_coid"):
        journal.pop(key, None)
    entry.payload = {**entry.payload, wb.KEY: journal}


async def finish_attempt(service, current, outcome):
    original = current["state"]
    from project_mai_tai.oms import webull_attach_riskoff as wb
    partial_call = wb._ATR_CALL.get()
    partial_call_id = (partial_call.call_id if partial_call is not None and getattr(partial_call, "wb3_partial", False)
        and partial_call.service is service and partial_call.task is asyncio.current_task() else None)
    def unit(db):
        incident = db.scalar(select(SystemIncident).where(SystemIncident.id == UUID(original["owner_id"])).with_for_update())
        state = deepcopy(_validate(incident.payload, incident.id))
        if state.get("active_token") != current["token"]:
            raise ValueError("wbexit3_actual_attempt_token_changed")
        binding = state["binding"]
        row, entry, actual = _binding(service, db, binding["account"], binding["symbol"], binding["managed_id"])
        if actual != binding:
            raise ValueError("wbexit3_episode_changed_no_resolution")
        state.update(active_token=None, attempts=state["attempts"] + 1,
                     revision=state["revision"] + 1, last_result=str(outcome), last_guard=current["guard"])
        if _execution_complete(service, db, state, row, entry):
            state.update(phase="RESOLVED", resolved_at=_now().isoformat())
            state["residual_quantity"], state["managed_status"] = str(row.current_quantity), row.status
            incident.status, incident.closed_at = "closed", _now()
            journal = deepcopy((entry.payload or {}).get(wb.KEY) or {})
            if (state["kind"] == "SCALE" and partial_call_id is not None
                    and journal.get("exit_call_id") == partial_call_id
                    and journal.get("wbexit3_partial_owner_id") == state["owner_id"]):
                for key in ("exit_phase", "exit_call_id", "wbexit3_partial_owner_id", "wbexit3_partial_sell_coid"):
                    journal.pop(key, None)
                entry.payload = {**entry.payload, wb.KEY: journal}
            else:
                _retire_completed_partial_claim(db, state, entry)
        else:
            state["phase"] = "PAGED" if state["attempts"] >= PAGE_AFTER else "RETRY"
            delay = min(MAX_BACKOFF, 10 * 2 ** min(state["attempts"] - 1, 3))
            state["next_retry_at"] = (_now() + timedelta(seconds=delay)).isoformat()
            incident.severity = "critical" if state["phase"] == "PAGED" else "warning"
        incident.payload = state
        return state
    state = await _joined(service._run_db(unit, commit=True))
    _remember(service, state)
    if state["phase"] == "PAGED":
        service.logger.critical("[WBEXIT3-PAGED] owner=%s account=%s symbol=%s requested=%s guard=%s position_read=%s row=RETAINED native_protection=UNKNOWN retry_at=%s",
            state["owner_id"], state["binding"]["account"], state["binding"]["symbol"], state["requested"],
            state["last_guard"], current.get("position_read", "UNKNOWN"), state["next_retry_at"])
    elif state["phase"] == "RESOLVED" and state["kind"] == "SCALE":
        service.logger.info("[WBEXIT3-PARTIAL-RESOLVED] owner=%s requested=%s software_filled=%s native_progress=%s residual_quantity=%s managed_status=%s software_cover=existing_ladder_if_remaining native_protection=UNKNOWN_UNAVAILABLE_PM_AH",
            state["owner_id"], state["requested"], state.get("filled_highwater", "0"), state.get("partial_native_progress", "0"),
            state.get("residual_quantity", "UNKNOWN"), state.get("managed_status", "UNKNOWN"))


async def hydrate_at_boot(service):
    if not enabled(service):
        return
    service._wbexit3_boot_complete = False
    def unit(db):
        incidents = db.scalars(select(SystemIncident).where(SystemIncident.service_name == "oms-risk",
            SystemIncident.status == "open", SystemIncident.payload["source"].as_string() == SOURCE)
            .order_by(SystemIncident.id).limit(LIMIT + 1)).all()
        if len(incidents) > LIMIT:
            raise ValueError("wbexit3_complete_boot_inventory_exceeds_bound")
        result = {}
        for incident in incidents:
            state = deepcopy(_validate(incident.payload, incident.id))
            binding = state["binding"]
            _, _, actual = _binding(service, db, binding["account"], binding["symbol"], binding["managed_id"])
            if actual != binding:
                raise ValueError("wbexit3_boot_owner_binding_changed")
            result[state["owner_id"]] = state
        return result
    snapshot = await _joined(service._run_db(unit, commit=False))
    service._wbexit3_pending = snapshot
    service._wbexit3_boot_complete = True
    for state in snapshot.values():
        if state.get("active_token"):
            service.logger.critical("[WBEXIT3-BOOT-UNKNOWN] owner=%s reason=prior_physical_invocation_UNKNOWN wire=0 responsibility=RETAINED",
                                    state["owner_id"])
    report_pending(service)


def report_pending(service):
    """Existing sync scan, BEFORE fallible broker work; no quote is needed to PAGE."""
    if not enabled(service):
        return
    now = _now()
    pending = service.__dict__.get("_wbexit3_pending", {})
    reported = service.__dict__.setdefault("_wbexit3_reported", {})
    for owner_id in tuple(reported):
        if owner_id not in pending:
            reported.pop(owner_id, None)
    for owner_id, state in pending.items():
        age = (now - datetime.fromisoformat(state["opened_at"])).total_seconds()
        if age < PAGE_SECONDS or owner_id in reported and (now - reported[owner_id]).total_seconds() < 60:
            continue
        service.logger.critical("[WBEXIT3-PAGED] owner=%s account=%s symbol=%s age_seconds=%.3f reason=unresolved_close_responsibility native_protection=UNKNOWN retry_at=%s quote=NOT_REQUIRED_FOR_PAGE",
            owner_id, state["binding"]["account"], state["binding"]["symbol"], age, state["next_retry_at"])
        reported[owner_id] = now


async def refresh_pending(service):
    """After existing normal order sync, account ONE fairly; no provider read.

    Observing a row disappear/close does not resolve a responsibility. Only
    actual attributed executions do. This also finishes accepted orders after
    their quote owner has returned; no new quote is required for accounting.
    """
    if not enabled(service) or service.__dict__.get("_wbexit3_boot_complete") is not True:
        return
    cache = service.__dict__.get("_wbexit3_pending", {})
    keys = sorted(cache)
    if not keys:
        return
    previous = service.__dict__.get("_wbexit3_refresh_cursor", "")
    key = next((k for k in keys if k > previous), keys[0])
    service._wbexit3_refresh_cursor = key
    def unit(db):
        incident = db.scalar(select(SystemIncident).where(SystemIncident.id == UUID(key)).with_for_update())
        if incident is None:
            raise ValueError("wbexit3_missing_durable_owner_NOT_resolution")
        state = deepcopy(_validate(incident.payload, incident.id))
        if state["phase"] == "RESOLVED" or state.get("active_token"):
            return state  # No process-adoption of a physically unjoined invocation.
        binding = state["binding"]
        row, entry, actual = _binding(service, db, binding["account"], binding["symbol"], binding["managed_id"])
        if actual != binding:
            raise ValueError("wbexit3_reconcile_owner_changed")
        if _execution_complete(service, db, state, row, entry):
            state.update(phase="RESOLVED", resolved_at=_now().isoformat(), revision=state["revision"] + 1)
            state["residual_quantity"], state["managed_status"] = str(row.current_quantity), row.status
            _retire_completed_partial_claim(db, state, entry)
            incident.status, incident.closed_at, incident.payload = "closed", _now(), state
        elif state.get("filled_highwater") != incident.payload.get("filled_highwater"):
            state["revision"] += 1
            incident.payload = state
        if state["phase"] != "RESOLVED" and (_now() - datetime.fromisoformat(state["opened_at"])).total_seconds() >= PAGE_SECONDS:
            state["phase"], incident.severity = "PAGED", "critical"
            state["revision"] += 1
            incident.payload = state
        return state
    _remember(service, await _joined(service._run_db(unit, commit=True)))


async def resume_on_quote(service, account, symbol, quote):
    """Existing quote/cadence only; one due original owner, no new task or poll."""
    if not enabled(service) or service.__dict__.get("_wbexit3_boot_complete") is not True:
        return False
    now = _now()
    received = quote.get("received_at") if isinstance(quote, dict) else None
    if (session(now) == "CLOSED" or not isinstance(received, datetime) or received.tzinfo is None
            or not 0 <= (now - received).total_seconds() <= 2.5):
        return False
    try:
        bid = float(quote.get("bid") or 0)
    except (ValueError, TypeError, OverflowError):
        return False
    if not isfinite(bid) or bid <= 0:
        return False
    candidates = [s for s in service.__dict__.get("_wbexit3_pending", {}).values()
        if s["binding"]["account"] == account and s["binding"]["symbol"] == symbol
        and not s.get("active_token") and datetime.fromisoformat(s["next_retry_at"]) <= now]
    if not candidates:
        return False
    state = min(candidates, key=lambda s: (s["reason"] != "oms_v2_managed_exit:CW_HARD_STOP", s["opened_at"], s["owner_id"]))
    key = (account, symbol)
    busy = service.__dict__.setdefault("_wbexit3_quote_claims", set())
    if (key in busy or key in service.__dict__.get("_webull_cw_exit_inflight", set())
            or key in service.__dict__.get("_wbattach_exit_owners", {})
            or key in service.__dict__.get("_control_managed_exit_symbol_owners", {})
            or service._webull_recovery_in_progress(account, symbol)):
        return False
    busy.add(key)  # Existing single-loop claim, before any read/dispatch await.
    try:
        snapshot = await _joined(service._run_db(
            lambda db: service._read_v2_managed_snapshot(db, account, symbol, True), commit=False))
        if snapshot is None or snapshot.managed_row_id != state["binding"]["managed_id"]:
            return False  # Missing/closed/replaced row never resolves this obligation.
        context = deepcopy(state["context"])
        context.update(wbexit3_owner_id=state["owner_id"], cw_decided_at=now.isoformat())
        await service._emit_v2_exit_on_loop(account, symbol, service._hydrate_v2_position(snapshot), snapshot.entry_price,
            kind=state["kind"], reference_price=float(state["reference_price"]), reason=state["reason"], bid=bid,
            close_on_fill=True, sell_qty=int(Decimal(state["requested"]) - Decimal(state.get("filled_highwater", "0"))) if state["kind"] == "SCALE" else None,
            level=state["level"] or None, expected_managed_row_id=state["binding"]["managed_id"], confirmation_context=context)
        return False  # The software hard-stop ladder must still evaluate THIS quote.
    finally:
        busy.discard(key)
