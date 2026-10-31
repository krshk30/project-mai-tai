"""WBPA3: positive never-accepted country Webull entry closure, DB-only.

No status relabel, mutation, provider call or native-flat/entry permission.
The generic native-ID/Schwab/replacement readers remain the caller's contract.
"""
from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from uuid import UUID

from sqlalchemy import or_, select

from project_mai_tai.atr_country_route import selection_from_metadata
from project_mai_tai.db.models import (BrokerAccount, BrokerOrder, BrokerOrderEvent,
                                     DashboardSnapshot, Fill, Strategy, TradeIntent)

SETTLE_MS = 15_000
BOUND = 2048
TERMINAL = frozenset({"cancelled", "canceled", "filled", "rejected", "expired", "aborted"})
COUNTRY_KEYS = ("atr_country_schema", "atr_country_symbol", "atr_country_segment_id",
    "atr_country_broker", "atr_country_account", "atr_country_notional_usd", "atr_country_reason",
    "atr_country_status", "atr_country_as_of", "atr_country_sealed_at")
IDENTITY_KEYS = ("fanout_segment_id", "fanout_slot_id", "rpg_resting_generation",
                 "webull_mirror_generation_id", "clearwait_dispatch_token", "stop_price", "limit_price")


def _at(value):
    if not isinstance(value, datetime):
        raise ValueError("rejected_entry_clock_UNKNOWN")
    # Existing DB readers treat SQLite's declared UTC-naive storage this way.
    return int((value.replace(tzinfo=UTC) if value.tzinfo is None else value).timestamp() * 1000)


def _md(row):
    payload = row.payload
    if not isinstance(payload, dict):
        raise ValueError("rejected_entry_payload_UNKNOWN")
    value = payload.get("metadata", payload)
    if not isinstance(value, dict):
        raise ValueError("rejected_entry_metadata_UNKNOWN")
    return value


def _rows(session, query):
    rows = list(session.scalars(query.limit(BOUND + 1)))
    if len(rows) > BOUND:
        raise ValueError("rejected_entry_history_overflow")
    return rows


def _journal(session, symbol, opportunity, observed, *, require_full_scope=False):
    anchor = datetime.fromtimestamp(opportunity / 1000, UTC)
    rows = _rows(session, select(DashboardSnapshot).where(
        DashboardSnapshot.snapshot_type == "v2_wait_dispatch",
        DashboardSnapshot.payload["symbol"].as_string() == symbol,
        or_(DashboardSnapshot.created_at >= anchor, DashboardSnapshot.created_at.is_(None),
            DashboardSnapshot.payload["opportunity_id"].as_string() == str(opportunity))))
    begins, attempts = [], {}
    expected = None
    all_tokens = set()
    for row in rows:
        p = row.payload
        if (not isinstance(p, dict) or type(p.get("schema_version")) is not int or p["schema_version"] != 1
                or p.get("strategy_code") != "schwab_1m_v2" or p.get("symbol") != symbol
                or type(p.get("opportunity_id")) not in {str, int}
                or not str(p["opportunity_id"]).isascii() or not str(p["opportunity_id"]).isdigit()
                or str(int(p["opportunity_id"])) != str(p["opportunity_id"])
                or int(p["opportunity_id"]) <= 0
                or not int(p["opportunity_id"]) <= _at(row.created_at) <= observed
                or not isinstance(p.get("account_names"), list) or len(p["account_names"]) != 2
                or any(type(name) is not str or not name for name in p["account_names"])
                or len(set(p["account_names"])) != 2):
            raise ValueError("rejected_entry_dispatch_history_UNKNOWN")
        names = frozenset(p["account_names"])
        is_begin = p.get("kind") == "begin" and p.get("attempt_token") == "" and p.get("account_name") == ""
        is_attempt = (p.get("kind") == "attempt" and type(p.get("attempt_token")) is str
            and 0 < len(p["attempt_token"]) <= 128 and p.get("account_name") in names)
        if not is_begin and not is_attempt:
            raise ValueError("rejected_entry_dispatch_token_UNKNOWN")
        if is_attempt:
            if p["attempt_token"] in all_tokens:
                raise ValueError("rejected_entry_dispatch_token_reused")
            all_tokens.add(p["attempt_token"])
        row_opportunity = int(p["opportunity_id"])
        if row_opportunity != opportunity:
            # A positively identified LATER opportunity has its own census.
            # Never resurrect an already-rejected old order because another
            # valid entry was attempted later. Unknown/older/malformed rows do
            # not acquire this exclusion; account-waiver reads stay complete.
            if require_full_scope or row_opportunity < opportunity:
                raise ValueError("rejected_entry_other_history_scope_UNKNOWN")
            later_accounts = _rows(session, select(BrokerAccount).where(BrokerAccount.name.in_(names)))
            if (len(later_accounts) != 2 or {a.name for a in later_accounts} != names
                    or {a.provider for a in later_accounts} != {"schwab", "webull"}):
                raise ValueError("rejected_entry_later_dispatch_accounts_UNKNOWN")
            continue
        if expected is not None and names != expected:
            raise ValueError("rejected_entry_dispatch_universe_changed")
        expected = names
        if not _at(row.created_at) <= observed - SETTLE_MS:
            raise ValueError("rejected_entry_dispatch_still_settling")
        if is_begin:
            begins.append(row)
        elif (p.get("kind") == "attempt" and type(p.get("attempt_token")) is str
                and 0 < len(p["attempt_token"]) <= 128 and p.get("account_name") in names
                and p["attempt_token"] not in attempts):
            attempts[p["attempt_token"]] = row
        else:
            raise ValueError("rejected_entry_dispatch_token_UNKNOWN")
    if len(begins) != 1 or not attempts or any(_at(row.created_at) < _at(begins[0].created_at)
                                               for row in attempts.values()):
        raise ValueError("rejected_entry_dispatch_begin_UNKNOWN")
    accounts = _rows(session, select(BrokerAccount).where(BrokerAccount.name.in_(expected)))
    if (len(accounts) != 2 or {a.name for a in accounts} != expected
            or {a.provider for a in accounts} != {"schwab", "webull"}):
        raise ValueError("rejected_entry_dispatch_accounts_UNKNOWN")
    return begins[0], attempts, {a.name: a for a in accounts}


def _selection(metadata, accounts, symbol, opportunity):
    primary = next(a.name for a in accounts.values() if a.provider == "schwab")
    webull = next(a.name for a in accounts.values() if a.provider == "webull")
    # Account names come from actual historical BEGIN and DB reference identities.
    # No current routing flag/router decision supplies historical account scope.
    selected = selection_from_metadata(metadata, SimpleNamespace(
        strategy_schwab_1m_v2_account_name=primary, strategy_schwab_1m_v2_webull_account_name=webull))
    if (selected.broker != "webull" or selected.account != webull or selected.symbol != symbol
            or selected.segment_id != opportunity):
        raise ValueError("rejected_entry_single_Webull_scope_UNKNOWN")
    return selected


def _row_proof(session, order, observed, *, journal=None):
    if type(observed) is not int or observed <= SETTLE_MS:
        return False
    if (order.status != "rejected" or order.broker_order_id is not None or order.side != "buy"
            or str(order.order_type).upper() != "STOP_LIMIT" or order.time_in_force != "day"
            or not isinstance(order.quantity, Decimal) or not order.quantity.is_finite() or order.quantity <= 0
            or order.quantity != order.quantity.to_integral_value()):
        return False
    parent = session.get(TradeIntent, order.intent_id) if order.intent_id else None
    account = session.get(BrokerAccount, order.broker_account_id)
    strategy = session.get(Strategy, order.strategy_id)
    if (parent is None or account is None or strategy is None or account.provider != "webull"
            or strategy.code != "schwab_1m_v2" or parent.status != "rejected"
            or parent.strategy_id != order.strategy_id or parent.broker_account_id != order.broker_account_id
            or parent.symbol != order.symbol or parent.side != "buy" or parent.intent_type != "open"
            or parent.quantity != order.quantity or not isinstance(parent.payload, dict)
            or parent.payload.get("refusal_origin") != "broker_reject"):
        return False
    md, pmd = _md(order), _md(parent)
    event_id = UUID(str(parent.payload["event_id"]))
    coid = f"schwab_1m_v2-{order.symbol}-open-{event_id.hex[:12]}"[:40]
    if order.client_order_id != coid or any(md.get(k) != pmd.get(k) for k in IDENTITY_KEYS + COUNTRY_KEYS):
        return False
    opportunity = int(str(pmd["fanout_segment_id"]))
    if opportunity <= 0 or str(opportunity) != str(pmd["atr_country_segment_id"]):
        return False
    begin, attempts, accounts = journal or _journal(session, order.symbol, opportunity, observed)
    selected = _selection(pmd, accounts, order.symbol, opportunity)
    if selected.account != account.name:
        return False
    token = pmd.get("clearwait_dispatch_token")
    attempt = attempts.get(token)
    if (attempt is None or attempt.payload["account_name"] != account.name
            or not opportunity <= _at(begin.created_at) <= _at(attempt.created_at) <= _at(parent.created_at)
            or not opportunity <= _at(datetime.fromisoformat(selected.sealed_at)) <= _at(parent.created_at)
            or not _at(parent.created_at) <= _at(parent.updated_at) <= observed - SETTLE_MS
            or not _at(parent.created_at) <= _at(order.submitted_at) <= _at(order.updated_at) <= observed - SETTLE_MS):
        return False
    for key in ("fanout_slot_id", "rpg_resting_generation", "webull_mirror_generation_id"):
        UUID(str(pmd[key]))
    for key in ("stop_price", "limit_price"):
        price = Decimal(str(pmd[key]))
        if not price.is_finite() or price <= 0:
            return False
    if (pmd.get("fanout_slot") != "resting" or pmd.get("resting_entry") != "true"
            or pmd.get("fanout_source") != "rth_resting_mirror" or pmd.get("fanout_leg") != "webull"
            or session.scalar(select(Fill.id).where(Fill.order_id == order.id).limit(1)) is not None):
        return False
    events = _rows(session, select(BrokerOrderEvent).where(BrokerOrderEvent.order_id == order.id))
    if not events:
        return False
    for event in events:
        payload, emd = event.payload, _md(event)
        if (event.event_type != "rejected" or event.event_source != "broker"
                or not isinstance(payload, dict) or payload.get("client_order_id") != coid
                or "broker_order_id" not in payload or payload["broker_order_id"] is not None
                or "broker_fill_id" not in payload or payload["broker_fill_id"] is not None
                or payload.get("refusal_origin") == "could_not_tell"
                or emd.get("webull_error_code") != "ORDER_RISK_RULE_PRICE_AGGRESSIVE"
                or str(emd.get("webull_http_status")) != "417"
                or any(emd.get(k) != md.get(k) for k in IDENTITY_KEYS + COUNTRY_KEYS)
                or not _at(parent.created_at) <= _at(event.event_at) <= _at(order.updated_at)
                or not _at(event.event_at) <= _at(parent.updated_at) <= observed - SETTLE_MS):
            return False
        # Native reject response is timestamped BEFORE canonical BO insertion.
        # Do not reuse generic submitted_at<=event_at or rewrite either clock.
        for value in (payload.get("filled_quantity"), emd.get("filled_quantity"),
                      emd.get("rpg_terminal_filled_quantity")):
            if value is not None and Decimal(str(value)) != 0:
                return False
    return True


def broker_rejected_never_accepted(session, order, observed_ms):
    """Only exact country Webull PRICE_AGGRESSIVE/no-ID/no-fill submit refusal."""
    try:
        return _row_proof(session, order, observed_ms)
    except (ValueError, TypeError, KeyError, AttributeError, ArithmeticError, StopIteration):
        return False


def historical_country_rejected_accounts(session, symbol, opportunity, universe, observed_ms):
    """None means no missing-account waiver; never native-flat/entry permission.

    Requires the complete current original dispatch/open/order/audit cohort to
    prove only one historical chosen venue. Another/unanswered attempt or row,
    changed country seal, fill or live native identity keeps legacy full scope.
    """
    try:
        begin, attempts, accounts = _journal(session, symbol, opportunity, observed_ms,
                                              require_full_scope=True)
        if frozenset(universe) != frozenset(accounts):
            return None
        strategy = session.scalar(select(Strategy).where(Strategy.code == "schwab_1m_v2"))
        if strategy is None:
            return None
        anchor = datetime.fromtimestamp(opportunity / 1000, UTC)
        account_ids = tuple(account.id for account in accounts.values())
        parents = _rows(session, select(TradeIntent).where(
            or_(TradeIntent.strategy_id == strategy.id, TradeIntent.broker_account_id.in_(account_ids)),
            TradeIntent.symbol == symbol, TradeIntent.side == "buy", TradeIntent.intent_type == "open",
            or_(TradeIntent.created_at >= anchor, TradeIntent.created_at.is_(None),
                TradeIntent.payload["metadata"]["fanout_segment_id"].as_string() == str(opportunity),
                TradeIntent.status.is_(None), TradeIntent.status.not_in(TERMINAL))))
        orders = _rows(session, select(BrokerOrder).where(
            or_(BrokerOrder.strategy_id == strategy.id, BrokerOrder.broker_account_id.in_(account_ids)),
            BrokerOrder.symbol == symbol, BrokerOrder.side == "buy",
            or_(BrokerOrder.intent_id.in_([p.id for p in parents]), BrokerOrder.submitted_at >= anchor,
                BrokerOrder.submitted_at.is_(None),
                BrokerOrder.payload["fanout_segment_id"].as_string() == str(opportunity),
                BrokerOrder.status.is_(None), BrokerOrder.status.not_in(TERMINAL))))
        if not parents or len(parents) != len(attempts) or len(orders) != len(parents):
            return None
        tokens, selected_names, seals = set(), set(), set()
        for parent in parents:
            md = _md(parent)
            token = md.get("clearwait_dispatch_token")
            if token in tokens or token not in attempts:
                return None
            tokens.add(token)
            selected = _selection(md, accounts, symbol, opportunity)
            selected_names.add(selected.account)
            seals.add(tuple(md.get(key) for key in COUNTRY_KEYS))
            own = [order for order in orders if order.intent_id == parent.id]
            if len(own) != 1 or not _row_proof(session, own[0], observed_ms,
                                               journal=(begin, attempts, accounts)):
                return None
        if (tokens != set(attempts) or len(selected_names) != 1 or len(seals) != 1
                or {row.payload["account_name"] for row in attempts.values()} != selected_names):
            return None
        return frozenset(selected_names)
    except (ValueError, TypeError, KeyError, AttributeError, ArithmeticError, StopIteration):
        return None
