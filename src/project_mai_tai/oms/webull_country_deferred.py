"""PA1's bounded retry policy for a country-selected Webull resting primary.

The market callback uses cached prices and queues an intent only. The serial
lane proves the existing account/slot ledger clear and runs the ordinary entry
pipeline. This is an OMS cache, not native inventory or a new dispatch permit.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
import time
from uuid import UUID

from sqlalchemy import select, text

from project_mai_tai.atr_country_route import selection_from_metadata
from project_mai_tai.db.models import BrokerAccount, BrokerOrder, BrokerOrderEvent, Strategy, TradeIntent
from project_mai_tai.events import TradeIntentEvent, stream_name
from project_mai_tai.strategy_core.v2_entry_sizing import resting_buy_limit, resting_buy_stop


@dataclass
class Deferred:
    event: TradeIntentEvent
    stop_price: Decimal
    attempts: int = 0
    queued_event: str = ""
    retry_not_before_monotonic: float = 0.0
    root_at: datetime | None = None
    root_id: str = ""
    queued_json: str = ""
    claimed: bool = False
    root_event: TradeIntentEvent | None = None
    root_local_no_wire: bool = False


def _clock():
    from project_mai_tai.oms.service import utcnow
    return utcnow()


def _rth():
    from project_mai_tai.oms.service import _is_regular_market_session
    return _is_regular_market_session()


def scope(service, event):
    p, md = event.payload, event.payload.metadata
    if (not service._webull_mirror_deferred_enabled()
            or service._resting_fanout_pair_key(event) is None
            or md.get("fanout_source") != "rth_resting_mirror"
            or md.get("fanout_leg") != "webull"
            or str(md.get("order_type", "")).upper() != "STOP_LIMIT"):
        return False
    try:
        selected = selection_from_metadata(md, service.settings)
        for name in ("fanout_slot_id", "rpg_resting_generation", "webull_mirror_generation_id"):
            UUID(str(md[name]))
        return (selected.broker == "webull" and selected.account == p.broker_account_name
                and selected.symbol == p.symbol.upper()
                and str(selected.segment_id) == str(md.get("fanout_segment_id"))
                and datetime.fromisoformat(selected.sealed_at) <= _clock()
                and service.settings.provider_for_account(selected.account) == "webull")
    except (KeyError, TypeError, ValueError):
        return False


def _key(event):
    p, md = event.payload, event.payload.metadata
    return (p.broker_account_name, p.strategy_code, p.symbol.upper(),
            str(md.get("fanout_segment_id", "")), str(md.get("fanout_slot_id", "")))


def _generation(event):
    md = event.payload.metadata
    return tuple(str(md.get(k, "")) for k in ("rpg_resting_generation", "webull_mirror_generation_id"))


def _states(service):
    return service.__dict__.setdefault("_webull_country_deferred_by_slot", {})


def _retry(event):
    return event.payload.metadata.get("webull_country_deferred_resubmit") == "true"


def _log(service, event, decision, reason, attempts=0):
    service.logger.info("[OMS-WEBULL-COUNTRY-DEFERRED] account=%s sym=%s segment=%s slot_id=%s "
        "decision=%s reason=%s attempts=%d/%d", _key(event)[0], _key(event)[2],
        _key(event)[3], _key(event)[4], decision, reason, attempts,
        service._WEBULL_MIRROR_RESUBMIT_MAX_ATTEMPTS)


def _forget(service, key, reason):
    state = _states(service).pop(key, None)
    if state is not None:
        service.__dict__.setdefault("_webull_country_deferred_retired", {})[key] = _generation(state.event)
        _log(service, state.event, "forgotten", reason, state.attempts)


def observe_intent(service, event):
    """A v2 replacement/cancel/SELL retires the old cache, including late reports."""
    p, md = event.payload, event.payload.metadata
    if not service._webull_mirror_deferred_enabled() or p.strategy_code != "schwab_1m_v2":
        return
    prefix = _key(event)[:3]
    relevant = (scope(service, event) and not _retry(event)) or p.intent_type == "cancel" or p.side == "sell"
    if not relevant:
        return
    seen = service.__dict__.setdefault("_webull_country_deferred_observed", {})
    prior = seen.get(prefix)
    if prior is not None and event.produced_at < prior[0]:
        return
    if prior is not None and prior[1] == str(event.event_id):
        return
    if p.intent_type == "cancel":
        current = [state.event for key, state in _states(service).items() if key[:3] == prefix]
        if current and not any(not any(md.get(k) and md[k] != old.payload.metadata.get(k)
                for k in ("fanout_segment_id", "fanout_slot_id", "rpg_resting_generation",
                          "webull_mirror_generation_id")) for old in current):
            return  # A foreign bound cancel cannot poison the current watermark.
    # Bound cancels cannot retire another generation. An unbound cancel still
    # retires this exact account/strategy/symbol, as the ordinary v2 path does.
    for key, state in list(_states(service).items()):
        if key[:3] != prefix or event.produced_at < state.event.produced_at:
            continue
        if p.intent_type == "cancel" and any(md.get(k) and md[k] != state.event.payload.metadata.get(k)
                for k in ("fanout_segment_id", "fanout_slot_id", "rpg_resting_generation",
                          "webull_mirror_generation_id")):
            continue
        if str(event.event_id) != str(state.event.event_id):
            _forget(service, key, "v2_cancel_or_sell" if p.intent_type == "cancel" or p.side == "sell"
                    else "new_v2_attempt")
    seen[prefix] = (event.produced_at, str(event.event_id), _key(event), _generation(event),
                    p.intent_type == "cancel" or p.side == "sell")
    if scope(service, event) and not _retry(event):
        retired = service.__dict__.setdefault("_webull_country_deferred_retired", {})
        for key in list(retired):
            if key[:3] == prefix:
                retired.pop(key)


def _remember(service, event, *, local=False):
    if not scope(service, event) or not _rth():
        return
    try:
        stop = Decimal(str(event.payload.metadata["stop_price"]))
        attempt = int(str(event.payload.metadata.get("webull_deferred_resubmit_attempt", "0")))
    except (KeyError, TypeError, ValueError, InvalidOperation):
        return
    if not stop.is_finite() or stop <= 0 or attempt < 0:
        return
    key = _key(event)
    if service.__dict__.get("_webull_country_deferred_retired", {}).get(key) == _generation(event):
        return
    seen = service.__dict__.get("_webull_country_deferred_observed", {}).get(key[:3])
    if seen and (seen[4] or seen[2] != key or seen[3] != _generation(event)
                 or (not _retry(event) and seen[1] != str(event.event_id))):
        return
    old = _states(service).get(key)
    attempt = max(attempt, old.attempts if old is not None else 0)
    if attempt >= service._WEBULL_MIRROR_RESUBMIT_MAX_ATTEMPTS:
        _forget(service, key, "attempt_cap_reached")
        return
    _states(service)[key] = Deferred(event.model_copy(deep=True), stop, attempt,
        retry_not_before_monotonic=(time.monotonic() + service._WEBULL_MIRROR_PRECHECK_REARM_SECONDS
                                    if local and _retry(event) else 0.0),
        root_at=old.root_at if old is not None else event.produced_at,
        root_id=old.root_id if old is not None else str(event.event_id),
        root_event=old.root_event if old is not None else event.model_copy(deep=True),
        root_local_no_wire=old.root_local_no_wire if old is not None else local)
    _log(service, event, "deferred", "local_distance" if local else "PRICE_AGGRESSIVE", attempt)


def observe_reports(service, event, reports, session):
    if not scope(service, event):
        return False
    key = _key(event)
    state = _states(service).get(key)
    if state is not None and _generation(state.event) != _generation(event):
        return True
    if any(r.event_type in {"accepted", "filled", "partially_filled", "cancelled", "canceled", "expired"}
           for r in reports):
        _forget(service, key, "terminal_or_working_observation")
    elif service._webull_price_aggressive_report(reports):
        _remember(service, event)
    else:
        _forget(service, key, "non_price_aggressive_result")
    return True


def precheck(service, event, session):
    """Use PA1's existing stamped quote and the same eight-percent bound."""
    if not scope(service, event) or not _rth():
        return False
    md = event.payload.metadata
    try:
        price, stop = Decimal(str(md["webull_shape_market_price"])), Decimal(str(md["stop_price"]))
        stamp = datetime.fromisoformat(md["webull_shape_market_at_utc"])
        if stamp.tzinfo is None:
            return False
        age = (_clock() - stamp).total_seconds() * 1000
        if (not price.is_finite() or not stop.is_finite() or price <= 0 or stop <= 0
                or age < 0 or age > max(0, service._mirror_max_age_ms())):
            return False
        if price >= stop * (1 - service._WEBULL_MIRROR_RESUBMIT_WITHIN_PCT / Decimal(100)):
            return False
    except (KeyError, TypeError, ValueError, InvalidOperation):
        return False
    _remember(service, event, local=True)
    return True


def no_working_reason(service, session, event, *, ignore_client=""):
    """Positive existing durable terminal audits, never a native-flat inference."""
    account = session.scalar(select(BrokerAccount).where(BrokerAccount.name == event.payload.broker_account_name))
    strategy = session.scalar(select(Strategy).where(Strategy.code == event.payload.strategy_code))
    if (account is None or account.provider != "webull" or account.is_active is not True
            or strategy is None or strategy.execution_mode != "live"):
        return "country_account_or_strategy_UNKNOWN"
    if session.get_bind().dialect.name == "postgresql":
        session.execute(text("SELECT pg_advisory_xact_lock(hashtext(:key))"),
                        {"key": f"orb-v2:{account.name}:{event.payload.symbol.upper()}"})
    # A later v2 cancel/reprice cannot revive its predecessor even after a
    # restart or an out-of-order broker report. The original generation must
    # still be the last durable v2 decision for this slot.
    state = _states(service).get(_key(event))
    if state is None or state.root_event is None:
        return "country_original_claim_UNKNOWN"
    root = state.root_event
    root_parent = session.scalar(select(TradeIntent).where(
        TradeIntent.payload["event_id"].as_string() == state.root_id))
    if (root_parent is None or root_parent.strategy_id != strategy.id
            or root_parent.broker_account_id != account.id or root_parent.symbol != root.payload.symbol
            or root_parent.side != "buy" or root_parent.intent_type != "open"
            or root_parent.quantity != root.payload.quantity):
        return "country_original_intent_UNKNOWN"
    original = session.scalar(select(BrokerOrder).where(
        BrokerOrder.client_order_id == service._build_client_order_id(root)))
    if original is None:
        if (not state.root_local_no_wire or root_parent.status != "rejected"
                or (root_parent.payload or {}).get("refusal_origin") != "skipped_before_submit"
                or (root_parent.payload or {}).get("refusal_code") != "webull_mirror_precheck_deferred"
                or session.scalar(select(BrokerOrder.id).where(BrokerOrder.intent_id == root_parent.id)) is not None):
            return "country_original_no_wire_UNKNOWN"
    elif (original.status != "rejected" or original.intent_id != root_parent.id or original.strategy_id != strategy.id
            or original.broker_account_id != account.id or original.symbol != root.payload.symbol
            or original.side != "buy" or original.quantity != root.payload.quantity
            or any((original.payload or {}).get(k) != root.payload.metadata.get(k) for k in (
                "fanout_slot_id", "fanout_segment_id", "rpg_resting_generation", "webull_mirror_generation_id",
                "stop_price", "limit_price"))
            or not service._mirrorhold_clear_order(session, original)):
        return "country_original_terminal_audit_UNKNOWN"
    elif not any(a.event_type == "rejected" and a.event_source == "broker"
            and (a.payload or {}).get("metadata", {}).get("webull_error_code")
                == service._WEBULL_MIRROR_PRICE_AGGRESSIVE_CODE
            for a in service._mirrorhold_audits(session, original)):
        return "country_original_PRICE_AGGRESSIVE_audit_UNKNOWN"
    for order in service._mirrorhold_session_orders(session, account, event):
        md = order.payload or {}
        if (str(md.get("fanout_segment_id")) == _key(event)[3]
                and md.get("fanout_slot_id") == _key(event)[4]
                and session.scalar(select(BrokerOrderEvent.id).where(
                    BrokerOrderEvent.order_id == order.id,
                    BrokerOrderEvent.event_type.in_({"filled", "partially_filled"})).limit(1)) is not None):
            return "country_slot_execution_audit"
    root_at = state.root_at if state is not None else event.produced_at
    root_id = state.root_id if state is not None else str(event.event_id)
    for intent in session.scalars(select(TradeIntent).where(
            TradeIntent.broker_account_id == account.id, TradeIntent.strategy_id == strategy.id,
            TradeIntent.symbol == event.payload.symbol)):
        stamp = intent.created_at
        if stamp is None:
            return "country_intent_clock_UNKNOWN"
        if stamp.tzinfo is None and session.get_bind().dialect.name == "sqlite":
            from datetime import UTC
            stamp = stamp.replace(tzinfo=UTC)
        if stamp.tzinfo is None:
            return "country_intent_clock_UNKNOWN"
        md = (intent.payload or {}).get("metadata", {})
        row_event = str((intent.payload or {}).get("event_id"))
        if stamp >= root_at and row_event not in {root_id, str(event.event_id)}:
            if intent.intent_type == "cancel" or intent.side == "sell":
                return "country_v2_cancel_or_sell"
            if (intent.intent_type == "open" and (md.get("webull_country_deferred_resubmit") != "true"
                    or str(md.get("fanout_segment_id", "")) != _key(event)[3]
                    or str(md.get("fanout_slot_id", "")) != _key(event)[4]
                    or tuple(str(md.get(k, "")) for k in ("rpg_resting_generation", "webull_mirror_generation_id"))
                    != _generation(event))):
                return "country_v2_attempt_superseded"
    if ignore_client:
        own = session.scalar(select(BrokerOrder).where(BrokerOrder.client_order_id == ignore_client))
        parent = session.get(TradeIntent, own.intent_id) if own is not None and own.intent_id else None
        if (own is None or own.broker_account_id != account.id or own.strategy_id != strategy.id
                or own.symbol != event.payload.symbol or own.side != "buy" or own.status != "pending"
                or own.broker_order_id is not None or own.quantity != event.payload.quantity
                or str((own.payload or {}).get("fanout_slot_id")) != _key(event)[4]
                or str((own.payload or {}).get("fanout_segment_id")) != _key(event)[3]
                or parent is None or str((parent.payload or {}).get("event_id")) != str(event.event_id)):
            return "country_owned_pending_UNKNOWN"
    return service._mirrorhold_gate(session, event, ignore_client=ignore_client, session_bound=True)


def claim(service, event):
    if not _retry(event):
        return None
    state = _states(service).get(_key(event))
    if (not scope(service, event) or state is None or state.claimed
            or state.queued_json != event.model_dump_json()):
        return False
    with service.session_factory() as session:
        reason = no_working_reason(service, session, event)
    if reason:
        _forget(service, _key(event), reason)
        return False
    state.claimed = True
    return True


def finish(service, event):
    if not _retry(event):
        return False
    state = _states(service).get(_key(event))
    if state is not None and state.queued_event == str(event.event_id):
        _forget(service, _key(event), "serial_finished_without_deferred_reject")
    return True


def dispatch(service, session, event, client):
    if not _retry(event):
        return None
    state = _states(service).get(_key(event))
    if (not scope(service, event) or state is None or not state.claimed
            or state.queued_event != str(event.event_id)):
        return "country_slot_claim_changed"
    return no_working_reason(service, session, event, ignore_client=client)


def schedule(service, symbol):
    if any(key[2] == symbol for key in _states(service)):
        service._schedule_symbol_tick_work(("country-pa1", symbol), lambda: evaluate(service, symbol))


async def evaluate(service, symbol):
    matching = [(key, state) for key, state in tuple(_states(service).items()) if key[2] == symbol.upper()]
    if not matching:
        return
    if not _rth():
        for key, _ in matching:
            _forget(service, key, "resting_window_ended")
        return
    try:
        market = service._fresh_webull_mirror_market(symbol.upper())
    except InvalidOperation:
        return  # A non-numeric cached observation is never eligibility.
    if market is None or not market[0].is_finite():
        return
    for key, state in matching:
        if state.queued_event or time.monotonic() < state.retry_not_before_monotonic:
            continue
        if state.attempts >= service._WEBULL_MIRROR_RESUBMIT_MAX_ATTEMPTS:
            _forget(service, key, "attempt_cap_reached")
            continue
        if market[0] < state.stop_price * (1 - service._WEBULL_MIRROR_RESUBMIT_WITHIN_PCT / Decimal(100)):
            continue
        payload = state.event.payload.model_copy(deep=True)
        if getattr(service.settings, "strategy_schwab_1m_v2_resting_buy_round_up_enabled", False):
            stop = resting_buy_stop(Decimal(payload.metadata["stop_price"]))
            limit = resting_buy_limit(stop, float(payload.metadata["resting_band_pct"]), leg="webull")
            payload.metadata.update(stop_price=f"{stop:.4f}", limit_price=str(limit),
                entry_price=f"{stop:.4f}", reference_price=f"{stop:.4f}", resting_buy_round_up="true",
                resting_wire_stop_price=f"{stop:.4f}", resting_wire_limit_price=str(limit))
        state.attempts += 1
        payload.metadata.update(webull_deferred_resubmit="true", webull_country_deferred_resubmit="true",
            webull_deferred_resubmit_attempt=str(state.attempts),
            fanout_predecessor_attempt_id=str(payload.metadata.get("fanout_attempt_id", "")))
        for token in ("nfq_retry_token", "nfq_hold_id", "mirrorhold_token", "bot_control_permit",
                      "clearwait_dispatch_token"):
            payload.metadata.pop(token, None)
        retry = TradeIntentEvent(source_service="oms", produced_at=_clock(), payload=payload)
        state.queued_event = str(retry.event_id)
        state.queued_json = retry.model_dump_json()
        _log(service, retry, "queued", "cached_market_in_band_serial_proof_pending", state.attempts)
        try:
            await service.redis.xadd(stream_name(service.settings.redis_stream_prefix, "strategy-intents"),
                {"data": retry.model_dump_json()}, maxlen=service.settings.redis_strategy_intent_stream_maxlen,
                approximate=True)
        except asyncio.CancelledError:
            _forget(service, key, "serial_enqueue_cancelled_or_unconfirmed")
            raise
        except Exception:
            _forget(service, key, "serial_enqueue_unconfirmed")
            raise
