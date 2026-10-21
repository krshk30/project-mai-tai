"""Positive historical scope for waiving a missing clearwait cancel account.

Never use current country routing, a cancel set, or absent broker rows as scope.
The existing full-two-account barrier needs no missing-leg waiver.
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import or_, select

from project_mai_tai.db.models import (
    BrokerAccount, BrokerOrder, BrokerOrderEvent, DashboardSnapshot, Fill,
    OmsManagedPosition, Strategy, TradeIntent,
)
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
    # A read cutpoint may be days after this opportunity. Exclude another episode
    # only after independently proving its complete identity AND durable closure.
    groups = {}
    for row in intents:
        segment = _metadata(row).get("fanout_segment_id")
        if str(segment) != str(request.opportunity_id):
            try:
                foreign = int(segment)
            except (ValueError, TypeError) as exc:
                raise ValueError("cancel_obligations_foreign_identity_UNKNOWN") from exc
            if foreign <= request.opportunity_id or foreign > request.requested_at_ms:
                raise ValueError("cancel_obligations_foreign_identity_UNKNOWN")
            groups.setdefault(foreign, []).append(row)
    excluded_intents, excluded_orders = set(), set()
    if any(not isinstance(row.payload, dict) for row in journal):
        raise ValueError("cancel_obligations_journal_UNKNOWN")
    own_tokens = {row.payload.get("attempt_token") for row in journal}
    for foreign, parents in groups.items():
        parent_ids = {row.id for row in parents}
        children = [row for row in orders if row.intent_id in parent_ids
                    or str(_metadata(row).get("fanout_segment_id")) == str(foreign)]
        scope = ObligationScope(request.symbol, foreign, request.requested_at_ms, request.account_names)
        foreign_journal = bounded(select(DashboardSnapshot).where(
            DashboardSnapshot.snapshot_type == DISPATCH_SNAPSHOT_TYPE,
            DashboardSnapshot.payload["symbol"].as_string() == request.symbol,
            DashboardSnapshot.payload["opportunity_id"].as_string() == str(foreign)))
        foreign_owners = bounded(select(DashboardSnapshot).where(
            DashboardSnapshot.snapshot_type == "v2_flip_entry_ownership",
            DashboardSnapshot.payload["symbol"].as_string() == request.symbol,
            DashboardSnapshot.payload["opportunity_id"].as_string() == str(foreign)))
        proven_accounts(scope, accounts, strategy_id, foreign_journal, foreign_owners, parents, children)
        latest = max(foreign_owners, key=lambda row: (_at(row.created_at), str(row.id)))
        known_owner = ((latest.payload.get("phase") == "idle" and latest.payload.get("active") is False)
            or (latest.payload.get("phase") in {"bound", "consumed", "awaiting_close"}
                and latest.payload.get("active") is True))
        if (any(_metadata(row).get("clearwait_dispatch_token") in own_tokens for row in parents)
                or not known_owner
                or any(_at(row.created_at) == _at(latest.created_at) and row.payload != latest.payload
                       for row in foreign_owners)
                or any(_at(row.created_at) > request.requested_at_ms for row in foreign_owners)
                or any(not any(child.intent_id == row.id for child in children) for row in parents)
                or any(not _closed_order(session, row, request.requested_at_ms) for row in children)):
            raise ValueError("cancel_obligations_foreign_closed_UNKNOWN")
        excluded_intents.update(parent_ids)
        excluded_orders.update(row.id for row in children)
    intents = [row for row in intents if row.id not in excluded_intents]
    orders = [row for row in orders if row.id not in excluded_orders]
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


def _bounded(session, query):
    rows = list(session.scalars(query.limit(ROW_LIMIT + 1)))
    if len(rows) > ROW_LIMIT:
        raise ValueError("cancel_obligations_history_bound")
    return rows


def _closed_order(session, order, observed, *, replacement_linked=False):
    """Positive durable closure, not a general terminal-status expansion."""
    try:
        if not 0 <= _at(order.updated_at) <= observed - 15_000:
            return False
        parent = session.get(TradeIntent, order.intent_id) if order.intent_id else None
        if (parent is None or parent.strategy_id != order.strategy_id
                or parent.broker_account_id != order.broker_account_id or parent.symbol != order.symbol
                or parent.side != "buy" or parent.intent_type != "open"
                or not 0 <= _at(parent.updated_at) <= observed - 15_000):
            return False
        events = _bounded(session, select(BrokerOrderEvent).where(BrokerOrderEvent.order_id == order.id))
        fills = _bounded(session, select(Fill).where(Fill.order_id == order.id))
        md, payload = _metadata(order), parent.payload or {}
        if any(key in md for key in ("orb_replace_hold", "momcancel1_ack_identity")):
            return False
        if order.status == "aborted":
            if (order.broker_order_id or fills or "bot_control_permit" in md
                    or "momlive_terminal_broker_event" in md):
                return False
            if md.get("rpg_local_abort_no_wire") == "true":
                from project_mai_tai.oms.atr_reprice_handoff import local_rpg_abort_proof
                proof = local_rpg_abort_proof(session, order)
                return bool(proof and proof[1] * 1000 <= observed - 15_000
                    and events and all(e.event_source == "client" and e.event_type == "aborted"
                        and _at(e.event_at) <= observed - 15_000 for e in events))
            episode = payload.get("momlive_episode", {})
            strategy = session.get(Strategy, order.strategy_id)
            return bool(not events and order.submitted_at is None
                and md.get("momlive_entry_never_dispatched") is True
                and strategy is not None and strategy.code == "momentum_warn_live"
                and payload.get("momlive_parent") is True and parent.status == "cancelled"
                and md.get("episode_id") == str(parent.id) == episode.get("episode_id")
                and episode.get("state") == "terminal_no_fill"
                and str(episode.get("filled")) == "0" and str(episode.get("exited")) == "0"
                and (payload.get("momlive_c6_unsent_proof") is True
                     or payload.get("momstuck2_refusal") == md.get("momstuck2_refusal")
                     and isinstance(payload.get("momstuck2_refusal"), dict)
                     and payload["momstuck2_refusal"].get("reason") == "refused_before_wire"))
        if not order.broker_order_id or not events:
            return False
        latest = max(events, key=lambda row: (_at(row.event_at), str(row.id)))
        def native_terminal(event):
            return (event.event_source == "broker" and cancel_outcome_known(event)
                and all(value in {None, "confirmed"} for value in
                    (event.payload.get("cancel_outcome"), _metadata(event).get("cancel_outcome"))))

        if (not native_terminal(latest)
                or not _at(order.submitted_at) <= _at(latest.event_at) <= observed - 15_000
                or any(_at(e.event_at) == _at(latest.event_at)
                    and (e.event_type != latest.event_type or e.event_source != "broker"
                         or not native_terminal(e)) for e in events)):
            return False
        status = "cancelled" if order.status == "canceled" else order.status
        event_status = "cancelled" if latest.event_type == "canceled" else latest.event_type
        if status == "filled":
            account = session.get(BrokerAccount, order.broker_account_id)
            strategy = session.get(Strategy, order.strategy_id)
            managed = _bounded(session, select(OmsManagedPosition).where(
                OmsManagedPosition.entry_order_id == order.id))
            return bool(event_status == "filled" and fills and account and strategy and managed
                and all(f.broker_fill_id and f.symbol == order.symbol and f.side == "buy"
                    and f.strategy_id == order.strategy_id and f.broker_account_id == order.broker_account_id
                    and f.quantity.is_finite() and f.quantity > 0
                    and _at(f.filled_at) <= observed - 15_000 for f in fills)
                and sum(f.quantity for f in fills) == order.quantity
                and all(m.status == "closed" and m.current_quantity == 0
                    and m.symbol == order.symbol and m.strategy_code == strategy.code
                    and m.broker_account_name == account.name
                    and m.entry_client_order_id == order.client_order_id for m in managed))
        return bool(not fills and status in {"cancelled", "rejected", "expired", "replaced"}
            and event_status in {"cancelled", "rejected", "expired"}
            and (status == event_status or status == "replaced" and replacement_linked))
    except (ValueError, TypeError, ArithmeticError, AttributeError):
        return False


def _replacement_closed(session, job, symbol, observed):
    """A journal phase alone cannot discharge an old order or its successor."""
    try:
        if job.get("phase") not in {"expired", "refused", "filled"}:
            return False
        old, replacement = job["old"], job.get("replacement")
        if replacement and (replacement.get("client_order_id") == old.get("client_order_id")
                or any(replacement.get(key) != old.get(key) for key in
                    ("broker_account_name", "strategy_code", "symbol", "side"))):
            return False
        for request in (old, replacement):
            if request is None:
                continue
            if (request.get("symbol") != symbol or request.get("side") != "buy"
                    or not request.get("client_order_id")):
                return False
            order = session.scalar(select(BrokerOrder).where(
                BrokerOrder.client_order_id == request["client_order_id"]))
            if order is None or not _closed_order(session, order, observed, replacement_linked=True):
                return False
            account = session.get(BrokerAccount, order.broker_account_id)
            strategy = session.get(Strategy, order.strategy_id)
            if (not account or not strategy or account.name != request.get("broker_account_name")
                    or strategy.code != request.get("strategy_code") or order.symbol != symbol
                    or order.side != "buy" or Decimal(str(request.get("quantity"))) != order.quantity):
                return False
        return bool(old)
    except (ValueError, TypeError, KeyError, AttributeError, ArithmeticError):
        return False


def working_buy_present(session, symbol, *, now=None):
    """All strategies/accounts; only exact persisted no-wire/closed proof exempts history."""
    observed = _at(now or datetime.now(UTC))
    terminal = {"cancelled", "canceled", "filled", "rejected", "expired"}
    jobs = _bounded(session, select(DashboardSnapshot).where(
        DashboardSnapshot.snapshot_type == "atr_reprice_handoff",
        DashboardSnapshot.payload["old"]["symbol"].as_string() == symbol))
    if any(not _replacement_closed(session, row.payload, symbol, observed) for row in jobs):
        return True

    def closed(order):
        linked = any(row.payload.get("replacement")
            and any(request.get("client_order_id") == order.client_order_id
                for request in (row.payload["old"], row.payload["replacement"])) for row in jobs)
        return _closed_order(session, order, observed, replacement_linked=linked)

    orders = _bounded(session, select(BrokerOrder).where(
        BrokerOrder.symbol == symbol, BrokerOrder.side == "buy",
        or_(BrokerOrder.status.is_(None), BrokerOrder.status.notin_(terminal))))
    if any(not closed(row) for row in orders):
        return True
    intents = _bounded(session, select(TradeIntent).where(
            TradeIntent.symbol == symbol, TradeIntent.side == "buy", TradeIntent.intent_type == "open",
            or_(TradeIntent.status.is_(None), TradeIntent.status.notin_(terminal))))
    for parent in intents:
        if parent.status not in {"aborted", "completed", "replaced"}:
            return True
        siblings = _bounded(session, select(BrokerOrder).where(BrokerOrder.intent_id == parent.id))
        if not siblings or any(not closed(row) for row in siblings):
            return True
    return False


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
            or working_buy_present(session, scope.symbol, now=now)):
        raise ValueError("cancel_obligations_position_or_buy_held")
    required = load_proven_accounts(session, scope, accounts, strategy_id)
    return FlatObligations(scope, observed, required)
