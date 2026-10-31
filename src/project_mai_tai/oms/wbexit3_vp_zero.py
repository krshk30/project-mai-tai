"""WB3 same-day VP-zero EXIT cap; no inventory, Fill, or durable quantity writer.

Native history is the existing complete account-wide today census. Prior-day
BUYs and any missing/ambiguous native operand remain UNKNOWN.
"""
from dataclasses import dataclass
import json
from datetime import datetime
from decimal import Decimal
from math import isfinite
from time import monotonic
from zoneinfo import ZoneInfo

from sqlalchemy import or_, select

from project_mai_tai.db.models import BrokerOrder, Fill, TradeIntent, VirtualPosition

ET = ZoneInfo("America/New_York")
MAX_AGE_SECONDS = 2
KNOWN_NATIVE_STATUS = frozenset({"FILLED", "PARTIAL_FILLED", "PARTIAL FILLED", "PARTIALLY_FILLED",
    "CANCELLED", "CANCELED", "FAILED", "REJECTED", "SUBMITTED", "PENDING", "WORKING", "ACCEPTED", "QUEUED"})


@dataclass(frozen=True)
class NativeDayOrder:
    coid: str
    native_id: str
    symbol: str
    side: str
    status: str
    quantity: Decimal
    filled: Decimal
    filled_at: datetime | None
    limit_price: Decimal | None = None
    stop_price: Decimal | None = None


@dataclass(frozen=True)
class NativeDayHistory:
    account: str
    native_account: str
    started_at: datetime
    acquired_at: datetime
    orders: tuple[NativeDayOrder, ...]


@dataclass(frozen=True)
class ExitProof:
    token: str
    binding: tuple
    buys: tuple
    quantity: Decimal
    buy_at: datetime
    native_account: str
    positions_started: float
    positions_requested: float
    history: NativeDayHistory
    history_requested: datetime
    originals: tuple


def _aware(value):
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("wbexit3_vp_zero_time_UNKNOWN")
    return value


def _native_fill_time(value):
    if type(value) is not str or not value or value != value.strip():
        raise ValueError("wbexit3_vp_zero_native_fill_time_UNKNOWN")
    # Require the broker's actual explicit offset, never the host's local zone.
    return _aware(datetime.fromisoformat(value.replace("Z", "+00:00")))


def normalize_history(adapter, account_name, native_account, census, *, after):
    """Normalize EVERY raw row before symbol selection; never selected-child absence."""
    if (not isinstance(census, dict) or census.get("account_id") != native_account
            or census.get("complete") is not True or census.get("truncated") is not False
            or type(census.get("pages")) is not int or not 1 <= census["pages"] <= 20
            or not isinstance(census.get("orders"), list) or len(census["orders"]) > 2000):
        raise ValueError("wbexit3_vp_zero_history_incomplete")
    started = _aware(datetime.fromisoformat(census["started_at"]))
    acquired = _aware(datetime.fromisoformat(census["acquired_at"]))
    if (not _aware(after) < started <= acquired or (acquired - started).total_seconds() > 30
            or started.astimezone(ET).date() != acquired.astimezone(ET).date()):
        raise ValueError("wbexit3_vp_zero_history_window_UNKNOWN")
    account = adapter.accounts_by_name.get(account_name)
    if account is None or account.account_id != native_account:
        raise ValueError("wbexit3_vp_zero_history_account_UNKNOWN")
    rows, seen = [], set()
    for raw in census["orders"]:
        if not isinstance(raw, dict):
            raise ValueError("wbexit3_vp_zero_history_row_UNKNOWN")
        items = raw.get("items")
        if not isinstance(items, list) or len(items) != 1 or not isinstance(items[0], dict):
            raise ValueError("wbexit3_vp_zero_history_items_UNKNOWN")
        item = items[0]
        ids = [part[key] for part in (raw, item) for key in ("client_order_id", "clientOrderId") if key in part]
        symbols = [part["symbol"] for part in (raw, item) if "symbol" in part]
        sides = [part["side"] for part in (raw, item) if "side" in part]
        if (not ids or any(type(v) is not str or not v or v != v.strip() or v != ids[0] for v in ids)
                or ids[0] in seen or not symbols or not sides
                or any(type(v) is not str or not v or v != v.strip() for v in symbols + sides)
                or len({v.upper() for v in symbols}) != 1 or len({v.lower() for v in sides}) != 1
                or sides[0].lower() not in {"buy", "sell"}
                or item.get("symbol", "").upper() != symbols[0].upper()
                or item.get("side", "").lower() != sides[0].lower()):
            raise ValueError("wbexit3_vp_zero_history_identity_UNKNOWN")
        symbol, side, coid = symbols[0].upper(), sides[0].lower(), ids[0]
        statuses = [part[key] for part in (raw, item) for key in ("status", "order_status", "orderStatus") if key in part]
        native_ids = [part[key] for part in (raw, item) for key in ("order_id", "orderId") if key in part]
        accounts = [part[key] for part in (raw, item) for key in ("account_id", "accountId") if key in part]
        if (not statuses or any(type(value) is not str or value.upper() not in KNOWN_NATIVE_STATUS for value in statuses)
                or len({adapter._map_status({"status": value}) for value in statuses}) != 1
                or not native_ids or any(type(value) is not str or not value or value != value.strip()
                    or value != native_ids[0] for value in native_ids)
                or any(type(value) is not str or value != native_account for value in accounts)):
            raise ValueError("wbexit3_vp_zero_history_status_or_native_alias_UNKNOWN")
        execution = adapter._validated_list_execution(raw, account, coid, symbol=symbol, side=side)
        if execution is None:
            raise ValueError("wbexit3_vp_zero_history_execution_UNKNOWN")
        status, native_id, amount, _, stamp = execution
        filled = Decimal(amount)
        requested = [adapter._decimal_or_none({key: part[key]}, key) for part in (raw, item)
                     for key in ("qty", "quantity", "order_qty", "orderQty") if key in part]
        if (not native_id or not requested or any(v is None or not v.is_finite() or v <= 0
                or v != requested[0] for v in requested) or not 0 <= filled <= requested[0]
                or status == "filled" and filled != requested[0]
                or any(adapter._map_status({"status": raw[key]}) != status for key in
                       ("status", "order_status", "orderStatus") if key in raw)
                or native_id != native_ids[0]
                or any(adapter._decimal_or_none({key: part[key]}, key) != filled for part in (raw, item)
                       for key in ("filled_qty", "filledQty", "filled_quantity", "filledQuantity") if key in part)
                or filled > 0 and any(adapter._parse_broker_time(part[key]) != adapter._parse_broker_time(stamp)
                       for part in (raw, item) for key in ("last_filled_time", "lastFilledTime") if key in part)
                or filled > 0 and any(adapter._decimal_or_none({key: part[key]}, key) != Decimal(execution[3])
                       for part in (raw, item) for key in ("filled_price", "filledPrice", "avg_fill_price", "avgFillPrice", "avg_price") if key in part)):
            raise ValueError("wbexit3_vp_zero_history_quantity_or_alias_UNKNOWN")
        filled_at = _aware(datetime.fromisoformat(stamp)) if stamp is not None else None
        native_stamps = [part[key] for part in (raw, item)
                         for key in ("last_filled_time", "lastFilledTime") if key in part]
        if filled > 0 and (filled_at is None or not native_stamps
                or any(_native_fill_time(value) != filled_at for value in native_stamps)):
            raise ValueError("wbexit3_vp_zero_history_fill_time_UNKNOWN")
        geometry = []
        for aliases in (("limit_price", "limitPrice"), ("stop_price", "stopPrice")):
            values = [adapter._decimal_or_none({key: part[key]}, key) for part in (raw, item)
                      for key in aliases if key in part]
            if values and any(v is None or not v.is_finite() or v <= 0 or v != values[0] for v in values):
                raise ValueError("wbexit3_vp_zero_history_geometry_alias_UNKNOWN")
            geometry.append(values[0] if values else None)
        seen.add(coid)
        rows.append(NativeDayOrder(coid, native_id, symbol, side, status, requested[0], filled, filled_at, *geometry))
    return NativeDayHistory(account_name, native_account, started, acquired, tuple(rows))


def _current(service):
    from project_mai_tai.oms import wbexit3, webull_attach_riskoff as wb
    current, call = wbexit3._ATTEMPT.get(), wb._ATR_CALL.get()
    if (not wbexit3.enabled(service) or current is None or call is None
            or current["service"] is not service or call.service is not service
            or current["task"] is not call.task or not call.wb2 or call.wb3_partial
            or current["state"] is None or current["state"].get("active_token") != current["token"]
            or service.__dict__.get("_wbattach_exit_owners", {}).get((call.account, call.symbol)) is not call):
        return None
    return current, call


def _durable(service, db, row, entry, account, strategy):
    from project_mai_tai.oms import wbexit3
    current_call = _current(service)
    if current_call is None:
        return None
    current, call = current_call
    actual, actual_entry, binding = wbexit3._binding(service, db, account.name, entry.symbol, str(row.id))
    if (actual.id != row.id or actual_entry.id != entry.id or binding != current["state"]["binding"]
            or str(row.id) != call.managed_id or entry.client_order_id != call.entry_coid
            or account.id != entry.broker_account_id or strategy.id != entry.strategy_id
            or row.status != "open" or strategy.code != "schwab_1m_v2"):
        raise ValueError("wbexit3_vp_zero_durable_identity_CHANGED")
    vp = db.scalar(select(VirtualPosition).where(VirtualPosition.strategy_id == strategy.id,
        VirtualPosition.broker_account_id == account.id, VirtualPosition.symbol == entry.symbol))
    if vp is None or vp.quantity != 0:
        return None
    fills = list(db.scalars(select(Fill).where(Fill.order_id == entry.id).limit(257)))
    if (not fills or len(fills) > 256 or any(f.strategy_id != strategy.id or f.broker_account_id != account.id
            or f.symbol != entry.symbol or f.side != "buy" or not f.broker_fill_id
            or not f.quantity.is_finite() or f.quantity <= 0 or not f.price.is_finite() or f.price <= 0
            for f in fills)):
        raise ValueError("wbexit3_vp_zero_BUY_UNKNOWN")
    quantity = sum((f.quantity for f in fills), Decimal(0))
    from project_mai_tai.oms.route_owner import _utc_row_time
    fill_times = [_utc_row_time(f.filled_at) for f in fills]
    if any(value is None for value in fill_times):
        raise ValueError("wbexit3_vp_zero_BUY_time_UNKNOWN")
    buy_at = min(fill_times)
    if (quantity != Decimal(row.current_quantity) or not 0 < quantity <= entry.quantity
            or quantity != quantity.to_integral_value()
            or any(stamp > wbexit3._now() or stamp.astimezone(ET).date() != wbexit3._now().astimezone(ET).date()
                   for stamp in fill_times)):
        raise ValueError("wbexit3_vp_zero_prior_day_or_quantity_UNKNOWN")
    # Any known SELL execution since this BUY, regardless of strategy/origin,
    # also refuses recovery; the native proof must independently cover manual orders.
    if db.scalar(select(Fill.id).where(Fill.broker_account_id == account.id, Fill.symbol == entry.symbol,
            Fill.side == "sell", Fill.filled_at >= buy_at).limit(1)) is not None:
        raise ValueError("wbexit3_vp_zero_exit_Fill_present")
    exits = db.scalars(select(Fill).join(BrokerOrder, BrokerOrder.id == Fill.order_id)
        .join(TradeIntent, TradeIntent.id == BrokerOrder.intent_id).where(Fill.side == "sell",
        BrokerOrder.broker_account_id == account.id, BrokerOrder.symbol == entry.symbol,
        or_(BrokerOrder.payload["oms_managed_row_id"].as_string() == str(row.id),
            BrokerOrder.payload["entry_order_id"].as_string() == str(entry.id),
            TradeIntent.payload["metadata"]["oms_managed_row_id"].as_string() == str(row.id),
            TradeIntent.payload["metadata"]["entry_order_id"].as_string() == str(entry.id))).limit(1)).first()
    if exits is not None:
        raise ValueError("wbexit3_vp_zero_attributed_exit_Fill_present")
    buys = tuple(sorted((str(f.id), f.broker_fill_id, str(f.quantity), str(f.price), _utc_row_time(f.filled_at).isoformat()) for f in fills))
    return tuple(sorted(binding.items())), buys, quantity, buy_at


def _originals(call):
    state = call.wb2_state
    if state is None or state.get("originals_proven") is not True or not state.get("native_account_id"):
        raise ValueError("wbexit3_vp_zero_native_originals_UNKNOWN")
    from project_mai_tai.oms import webull_attach_riskoff as wb
    originals = []
    for pair in state["originals"]:
        for child in pair["children"]:
            kinds = [suffix for suffix in ("T", "S")
                     if child["coid"] == wb.WebullBrokerAdapter._combo_leg_coid(pair["base"], suffix)]
            if len(kinds) != 1:
                raise ValueError("wbexit3_vp_zero_original_child_kind_UNKNOWN")
            originals.append((child["coid"], child["native_id"], pair["quantity"], child["price"], kinds[0]))
    return tuple(sorted(originals))


def _native(proof, call, *, now):
    history = proof.history
    if (not isinstance(history, NativeDayHistory) or history.account != call.account
            or history.native_account != proof.native_account or proof.native_account != call.wb2_state["native_account_id"]
            or not 0 <= (now - history.acquired_at).total_seconds() <= MAX_AGE_SECONDS
            or not proof.history_requested < history.started_at <= history.acquired_at
            or (history.acquired_at - history.started_at).total_seconds() > 30
            or not isinstance(history.orders, tuple) or len(history.orders) > 2000
            or not proof.positions_requested < proof.positions_started <= monotonic()
            or monotonic() - proof.positions_started > MAX_AGE_SECONDS
            or history.started_at.astimezone(ET).date() != proof.buy_at.astimezone(ET).date()
            or proof.buy_at.astimezone(ET).date() != now.astimezone(ET).date()
            or proof.originals != _originals(call)):
        raise ValueError("wbexit3_vp_zero_native_receipt_CHANGED_or_stale")
    expected = {coid: (native, Decimal(qty), Decimal(price) if price is not None else None, kind)
                for coid, native, qty, price, kind in proof.originals}
    seen_originals = set()
    for order in history.orders:
        if (not isinstance(order, NativeDayOrder) or not order.coid or not order.native_id
                or not order.symbol or order.symbol != order.symbol.upper() or order.side not in {"buy", "sell"}
                or order.status not in {"accepted", "filled", "partially_filled", "cancelled", "rejected"}
                or not order.quantity.is_finite() or order.quantity <= 0
                or not order.filled.is_finite() or not 0 <= order.filled <= order.quantity
                or order.filled > 0 and (order.filled_at is None or _aware(order.filled_at) > history.acquired_at)):
            raise ValueError("wbexit3_vp_zero_native_order_UNKNOWN")
        original = expected.get(order.coid)
        if original is not None:
            if (order.symbol != call.symbol or order.side != "sell"
                    or original[:2] != (order.native_id, order.quantity) or order.quantity > proof.quantity):
                raise ValueError("wbexit3_vp_zero_original_native_binding_CHANGED")
            seen_originals.add(order.coid)
            if order.filled > 0 or order.status in {"filled", "partially_filled"}:
                raise ValueError("wbexit3_vp_zero_original_exit_execution_requires_remainder_path")
        if order.symbol != call.symbol or order.side != "sell":
            continue
        if order.filled > 0 or order.status in {"filled", "partially_filled"}:
            if order.filled_at is None or order.filled_at >= proof.buy_at:
                raise ValueError("wbexit3_vp_zero_any_origin_SELL_execution")
        if order.status not in {"filled", "cancelled", "rejected"}:
            if original is None:
                raise ValueError("wbexit3_vp_zero_other_working_SELL_wait")
            price = order.limit_price if original[3] == "T" else order.stop_price
            if original[2] is None or price != original[2]:
                raise ValueError("wbexit3_vp_zero_working_original_geometry_CHANGED")
    if seen_originals != set(expected):
        raise ValueError("wbexit3_vp_zero_history_original_census_CHANGED")


def require_current_actor(service):
    """Coroutine entry only; DB-thread cap validation never calls current_task()."""
    current_call = _current(service)
    if current_call is None or not isinstance(current_call[0].get("vp_zero_exit_proof"), ExitProof):
        return
    from project_mai_tai.oms import webull_attach_riskoff as wb
    wb._current_atr_call(service, current_call[1].managed_id)


@dataclass(frozen=True)
class PreparePermit:
    service: object
    call: object
    owner_task: object
    work: object
    request: object
    request_binding: tuple
    episode_binding: tuple
    managed_id: str
    token: str
    atr_nonce: str
    nonce: object
    proof: ExitProof
    purpose: str = "WB3_PREPARE_ONLY"


_PREPARE_LEASE = "_wbexit3_exact_prepare_lease"


def _prepare_request_binding(request):
    # JSON text is immutable even when the request's metadata contains nested values.
    metadata = json.dumps(request.metadata, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return (request.client_order_id, request.broker_account_name, request.strategy_code,
        request.symbol, request.side, request.intent_type, request.quantity, request.reason,
        request.order_type, request.time_in_force, metadata, request.prewire_check)


def register_prepare_permit(service, call, request, work):
    """Only the authenticated owning coroutine can name this ONE preparation work."""
    import asyncio
    from project_mai_tai.oms import webull_attach_riskoff as wb
    current_call = _current(service)
    if current_call is None or not isinstance(current_call[0].get("vp_zero_exit_proof"), ExitProof):
        return None
    current, actual_call = current_call
    wb._current_atr_call(service, call.managed_id)
    proof = current["vp_zero_exit_proof"]
    from project_mai_tai.oms.managed_sell_prewire import prepare_managed_sell
    if (actual_call is not call or not asyncio.iscoroutine(work)
            or work.cr_code is not prepare_managed_sell.__code__ or call.wb2_pending is not None
            or call.__dict__.get(_PREPARE_LEASE) is not None
            or request.strategy_code != "schwab_1m_v2" or request.broker_account_name != call.account
            or request.symbol != call.symbol or request.side != "sell" or request.intent_type != "close"
            or request.reason != call.reason or request.quantity != call.quantity
            or request.quantity != proof.quantity or not request.client_order_id):
        raise ValueError("wbexit3_exact_prepare_admission_unknown")
    permit = PreparePermit(service, call, asyncio.current_task(), work, request,
        _prepare_request_binding(request), tuple(sorted(current["state"]["binding"].items())),
        call.managed_id, current["token"], call.call_id, object(), proof)
    call.__dict__[_PREPARE_LEASE] = {"permit": permit, "nonce": permit.nonce, "child": None, "used": False, "revoked": False}
    return permit


def require_preparation_actor(service, request, managed_id):
    """Admit only the actual joined task for the exact parent-named coroutine.

    A generic pending read/provider task, a copied request id, or a revoked role
    never obtains preparation authority from an inherited context.
    """
    import asyncio
    from project_mai_tai.oms import wbexit3, wbexit2
    raw = wbexit3._ATTEMPT.get()
    if (not wbexit3.enabled(service) or raw is None or raw["service"] is not service
            or not isinstance(raw.get("vp_zero_exit_proof"), ExitProof)):
        return  # Original preparation contract without this recovered proof.
    current_call = _current(service)
    if current_call is None:
        raise ValueError("wbattach1_live_owner_call_unknown")
    current, call = current_call
    lease = call.__dict__.get(_PREPARE_LEASE)
    permit = lease.get("permit") if isinstance(lease, dict) else None
    task = asyncio.current_task()
    if (not isinstance(permit, PreparePermit) or permit.purpose != "WB3_PREPARE_ONLY"
            or lease.get("nonce") is not permit.nonce or lease["revoked"] or lease["used"] or lease["child"] is not None
            or permit.service is not service or permit.call is not call or permit.owner_task is not call.task
            or current["task"] is not permit.owner_task or permit.proof is not current["vp_zero_exit_proof"]
            or permit.token != current["token"] or permit.atr_nonce != call.call_id
            or permit.episode_binding != tuple(sorted(current["state"]["binding"].items()))
            or str(managed_id) != permit.managed_id or permit.managed_id != call.managed_id
            or request is not permit.request or _prepare_request_binding(request) != permit.request_binding
            or task is None or task is not call.wb2_pending or task.get_coro() is not permit.work):
        raise ValueError("wbattach1_live_owner_call_unknown")
    # Recheck current STOP/session/deadline without pretending this child is the owner.
    wbexit2.check_call(service, call)
    lease["child"], lease["used"] = task, True


def revoke_prepare_permit(service, call, permit):
    """Cleanup only AFTER joined has finished/captured its actual commit receipt."""
    if permit is None:
        return
    import asyncio
    from project_mai_tai.oms import webull_attach_riskoff as wb
    lease = call.__dict__.get(_PREPARE_LEASE)
    child = lease.get("child") if isinstance(lease, dict) else None
    if (not isinstance(lease, dict) or lease.get("permit") is not permit or lease.get("nonce") is not permit.nonce or lease.get("revoked")
            or permit.service is not service or permit.call is not call
            or wb._ATR_CALL.get() is not call or asyncio.current_task() is not permit.owner_task
            or call.task is not permit.owner_task or call.wb2_pending is not None
            or child is not None and not child.done()):
        raise ValueError("wbexit3_prepare_revoke_requires_physical_join")
    lease["revoked"] = True
    del call.__dict__[_PREPARE_LEASE]


def exit_cap(service, db, row, entry, account, strategy):
    """Revalidate independently at EVERY cap reader, including prepared/final DB gates."""
    current_call = _current(service)
    if current_call is None:
        return None
    current, call = current_call
    proof = current.get("vp_zero_exit_proof")
    if not isinstance(proof, ExitProof):
        return None
    if proof.token != current["token"] or _durable(service, db, row, entry, account, strategy) != (
            proof.binding, proof.buys, proof.quantity, proof.buy_at):
        raise ValueError("wbexit3_vp_zero_exit_cap_changed")
    from project_mai_tai.oms import wbexit3
    _native(proof, call, now=wbexit3._now())
    return proof.quantity


async def admit(service, call):
    """No permission until fresh native operands AND current durable state agree."""
    from project_mai_tai.oms import wbexit3, wbexit2, webull_attach_riskoff as wb
    current_call = _current(service)
    if current_call is None or current_call[1] is not call:
        return
    wb._current_atr_call(service, call.managed_id)  # Actual coroutine/task before ANY new DB/native dispatch.
    current = current_call[0]
    if current.get("vp_zero_exit_proof") is not None:
        return
    def candidate(db):
        entry, row, account, strategy, owned = wb._atr(service, db, call.account, call.symbol, call.entry_coid)
        return _durable(service, db, row, entry, account, strategy) if owned == 0 else None
    durable = await wbexit2.joined(service, call, service._run_db(candidate, commit=False))
    wb._current_atr_call(service, call.managed_id)
    if durable is None:
        return
    originals = _originals(call)
    positions = current.get("position_receipt")
    requested = current.get("position_requested")
    if (not isinstance(positions, tuple) or len(positions) != 3 or type(requested) not in (int, float)
            or type(positions[1]) not in (int, float) or not isfinite(positions[1])
            or not requested < positions[1] <= monotonic()
            or monotonic() - positions[1] > MAX_AGE_SECONDS
            or positions[2] != call.wb2_state["native_account_id"]):
        raise ValueError("wbexit3_vp_zero_positions_UNKNOWN_or_stale")
    rows = [p for p in positions[0] if p.symbol == call.symbol]
    if (len(rows) != 1 or rows[0].broker_account_name != call.account or not rows[0].quantity.is_finite()
            or rows[0].quantity < durable[2]):
        raise ValueError("wbexit3_vp_zero_native_below_bot_quantity_UNKNOWN")
    reader = getattr(service.broker_adapter, "read_wbexit3_day_history", None)
    if not callable(reader):
        raise ValueError("wbexit3_vp_zero_day_history_UNAVAILABLE")
    history_requested = wbexit3._now()
    history = await wbexit2.joined(service, call, reader(call.account, after=history_requested))
    wb._current_atr_call(service, call.managed_id)
    proof = ExitProof(current["token"], *durable, positions[2], positions[1], requested, history, history_requested, originals)
    _native(proof, call, now=wbexit3._now())
    current["vp_zero_exit_proof"] = proof
    # Recheck real DB state AFTER native awaits. Failure never installs inventory.
    def recheck(db):
        entry, row, account, strategy, _ = wb._atr(service, db, call.account, call.symbol, call.entry_coid)
        if exit_cap(service, db, row, entry, account, strategy) != proof.quantity:
            raise ValueError("wbexit3_vp_zero_after_native_durable_CHANGED")
    await wbexit2.joined(service, call, service._run_db(recheck, commit=False))
    wb._current_atr_call(service, call.managed_id)
