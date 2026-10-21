"""Positive historical scope for waiving a missing clearwait cancel account.

Never use current country routing, a cancel set, or absent broker rows as scope.
The existing full-two-account barrier needs no missing-leg waiver.
"""

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import or_, select

from project_mai_tai.db.models import BrokerOrder, DashboardSnapshot, TradeIntent
from project_mai_tai.owned_inventory import owned_position_present
from project_mai_tai.positions_read_receipt import load_receipts
from project_mai_tai.v2_removed_wait import DISPATCH_SNAPSHOT_TYPE, ROW_LIMIT


@dataclass(frozen=True)
class ObligationScope:
    """Read cutpoint, not a synthesized cancellation request or receipt."""

    symbol: str
    opportunity_id: int
    requested_at_ms: int
    account_names: tuple[str, ...]


@dataclass(frozen=True)
class FlatObligations:
    scope: ObligationScope
    observed_at_ms: int
    required_accounts: frozenset[str]


def _metadata(row):
    payload = getattr(row, "payload", None)
    md = payload.get("metadata", payload) if isinstance(payload, dict) else None
    if not isinstance(md, dict):
        raise ValueError("cancel_obligations_metadata_UNKNOWN")
    return md


def _at(value):
    if not isinstance(value, datetime):
        raise ValueError("cancel_obligations_clock_UNKNOWN")
    return int((value.replace(tzinfo=UTC) if value.tzinfo is None else value).timestamp() * 1000)


def proven_accounts(request, accounts, strategy_id, journal, owners, intents, orders):
    """Closed dispatch history for THIS opportunity, before THIS clear request."""
    universe = set(request.account_names)
    if (request.opportunity_id <= 0 or len(request.account_names) != 2 or len(accounts) != 2
            or len(universe) != 2 or set(accounts.values()) != universe
            or not journal or not owners):
        raise ValueError("cancel_obligations_history_UNKNOWN")
    begins, attempts = [], {}
    for row in journal:
        p = row.payload
        if (not isinstance(p, dict) or p.get("schema_version") != 1
                or p.get("strategy_code") != "schwab_1m_v2" or p.get("symbol") != request.symbol
                or str(p.get("opportunity_id")) != str(request.opportunity_id)
                or not isinstance(p.get("account_names"), list) or len(p["account_names"]) != 2
                or set(p["account_names"]) != universe
                or not request.opportunity_id <= _at(row.created_at) <= request.requested_at_ms):
            raise ValueError("cancel_obligations_journal_UNKNOWN")
        if p.get("kind") == "begin" and p.get("attempt_token") == "" and p.get("account_name") == "":
            begins.append(row)
        elif (p.get("kind") == "attempt" and isinstance(p.get("attempt_token"), str)
              and p["attempt_token"] and p.get("account_name") in universe
              and p["attempt_token"] not in attempts):
            attempts[p["attempt_token"]] = row
        else:
            raise ValueError("cancel_obligations_journal_UNKNOWN")
    if len(begins) != 1 or not attempts:
        raise ValueError("cancel_obligations_history_UNKNOWN")
    if any(_at(row.created_at) < _at(begins[0].created_at) for row in attempts.values()):
        raise ValueError("cancel_obligations_journal_order_UNKNOWN")
    required = {row.payload["account_name"] for row in attempts.values()}
    # A later journal cannot erase a previously filled sibling in this owner.
    for row in owners:
        p = row.payload
        if (not isinstance(p, dict) or p.get("schema_version") != 1
                or p.get("strategy_code") != "schwab_1m_v2" or p.get("symbol") != request.symbol
                or str(p.get("opportunity_id")) != str(request.opportunity_id)
                or not isinstance(p.get("fill_accounts"), list)
                or not set(p["fill_accounts"]) <= required
                or not isinstance(p.get("position_ids"), dict)
                or not set(p["position_ids"]) <= required):
            raise ValueError("cancel_obligations_owner_history_UNKNOWN")
    matched, by_id = set(), {}
    for row in intents:
        md = _metadata(row)
        token = md.get("clearwait_dispatch_token")
        attempt = attempts.get(token)
        if (row.strategy_id != strategy_id or row.symbol != request.symbol or row.side != "buy"
                or row.intent_type != "open" or str(md.get("fanout_segment_id")) != str(request.opportunity_id)
                or attempt is None or token in matched
                or accounts.get(row.broker_account_id) != attempt.payload["account_name"]
                or not _at(attempt.created_at) <= _at(row.created_at) <= request.requested_at_ms):
            raise ValueError("cancel_obligations_buy_history_UNKNOWN")
        matched.add(token)
        by_id[row.id] = row
    if matched != set(attempts):
        raise ValueError("cancel_obligations_dispatch_unmatched")
    for row in orders:
        md = _metadata(row)
        parent = by_id.get(row.intent_id)
        if (parent is None or row.strategy_id != strategy_id or row.symbol != request.symbol
                or row.side != "buy" or row.broker_account_id != parent.broker_account_id
                or md.get("clearwait_dispatch_token") != _metadata(parent).get("clearwait_dispatch_token")
                or str(md.get("fanout_segment_id")) != str(request.opportunity_id)):
            raise ValueError("cancel_obligations_order_history_UNKNOWN")
    return frozenset(required)


def load_proven_accounts(session, request, accounts, strategy_id):
    def bounded(query):
        rows = list(session.scalars(query.limit(ROW_LIMIT + 1)))
        if len(rows) > ROW_LIMIT:
            raise ValueError("cancel_obligations_history_bound")
        return rows

    anchor = datetime.fromtimestamp(request.opportunity_id / 1000, UTC)
    journal = bounded(select(DashboardSnapshot).where(
        DashboardSnapshot.snapshot_type == DISPATCH_SNAPSHOT_TYPE,
        DashboardSnapshot.payload["symbol"].as_string() == request.symbol,
        DashboardSnapshot.payload["opportunity_id"].as_string() == str(request.opportunity_id)))
    owners = bounded(select(DashboardSnapshot).where(
        DashboardSnapshot.snapshot_type == "v2_flip_entry_ownership",
        DashboardSnapshot.payload["symbol"].as_string() == request.symbol,
        DashboardSnapshot.payload["opportunity_id"].as_string() == str(request.opportunity_id)))
    intents = bounded(select(TradeIntent).where(TradeIntent.strategy_id == strategy_id,
        TradeIntent.symbol == request.symbol, TradeIntent.side == "buy", TradeIntent.intent_type == "open",
        or_(TradeIntent.created_at >= anchor,
            TradeIntent.payload["metadata"]["fanout_segment_id"].as_string() == str(request.opportunity_id))))
    orders = bounded(select(BrokerOrder).where(BrokerOrder.strategy_id == strategy_id,
        BrokerOrder.symbol == request.symbol, BrokerOrder.side == "buy",
        or_(BrokerOrder.intent_id.in_([row.id for row in intents]), BrokerOrder.submitted_at >= anchor,
            BrokerOrder.payload["fanout_segment_id"].as_string() == str(request.opportunity_id))))
    return proven_accounts(request, accounts, strategy_id, journal, owners, intents, orders)


def optional_proven_accounts(session, request, accounts, strategy_id):
    """None means UNKNOWN; callers may not waive any missing receipt with it."""
    try:
        return load_proven_accounts(session, request, accounts, strategy_id)
    except (ValueError, TypeError, ArithmeticError):
        return None


def receipts_cover(request, receipts, required):
    universe = set(request.account_names)
    if receipts == universe:
        return True  # Existing full-coverage path excludes no possible account.
    return bool(required and set(required) <= universe and set(required) <= receipts)


def cancel_outcome_known(row):
    try:
        md = _metadata(row)
    except ValueError:
        return False
    payload = row.payload
    known = {None, "confirmed", "already_absent", "confirmed_after_accepted_request", "resolved_by_fill"}
    outcomes = (md.get("cancel_outcome"), payload.get("cancel_outcome"))
    return (all(value is None or isinstance(value, str) and value in known for value in outcomes)
            and payload.get("refusal_origin") != "could_not_tell")


def buy_only_cancel(row):
    """Legacy receipt projections use the explicit BUY-only capability stamp.

    Missing data is never defaulted to BUY. Any supplied side must corroborate
    the stamp; absent/null capability or contradictory/null side holds.
    """
    try:
        md = _metadata(row)
    except ValueError:
        return False
    if getattr(row, "intent_type", None) != "cancel" or md.get("clearwait_buy_only") != "true":
        return False
    if hasattr(row, "side"):
        return row.side == "buy"
    return True  # Positive capability above, not a default for a missing stamp.


def working_buy_present(session, symbol):
    terminal = {"cancelled", "canceled", "filled", "rejected", "expired"}
    return bool(session.scalar(select(BrokerOrder.id).where(
        BrokerOrder.symbol == symbol, BrokerOrder.side == "buy",
        or_(BrokerOrder.status.is_(None), BrokerOrder.status.notin_(terminal))).limit(1))
        or session.scalar(select(TradeIntent.id).where(
            TradeIntent.symbol == symbol, TradeIntent.side == "buy", TradeIntent.intent_type == "open",
            or_(TradeIntent.status.is_(None), TradeIntent.status.notin_(terminal))).limit(1)))


def load_flat_obligations(session, scope, accounts, strategy_id, now, max_age_ms):
    """Missing-row/leftover-request waiver only; never a native finality receipt."""
    observed = _at(now)
    receipts = load_receipts(session)
    for name in scope.account_names:
        receipt = receipts.get(name, {})
        if (receipt.get("writer") != "oms.sync_broker_positions"
                or not 0 <= observed - _at(receipt.get("read_at")) <= max_age_ms):
            raise ValueError("cancel_obligations_positions_UNKNOWN")
    if (owned_position_present(session, symbol=scope.symbol, accounts=accounts,
            strategy_id=strategy_id, now=now, max_age_ms=max_age_ms)
            or working_buy_present(session, scope.symbol)):
        raise ValueError("cancel_obligations_position_or_buy_held")
    required = load_proven_accounts(session, scope, accounts, strategy_id)
    return FlatObligations(scope, observed, required)
