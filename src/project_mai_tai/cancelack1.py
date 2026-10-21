"""Positive historical scope for waiving a missing clearwait cancel account.

Never use current country routing, a cancel set, or absent broker rows as scope.
The existing full-two-account barrier needs no missing-leg waiver.
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
import re
from uuid import UUID

from sqlalchemy import or_, select

from project_mai_tai.db.models import (
    BotControlAttempt, BrokerAccount, BrokerOrder, BrokerOrderEvent, DashboardSnapshot, Fill,
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
                or str(md.get("fanout_segment_id")) != str(request.opportunity_id)):
            raise ValueError("cancel_obligations_order_history_UNKNOWN")
        parent_token = _metadata(parent).get("clearwait_dispatch_token")
        order_token = md.get("clearwait_dispatch_token")
        # Reprice-cancelled historical rows retain the exact parent and segment
        # but not its dispatch token. This links identity, NOT native finality.
        if order_token is None and getattr(row, "status", None) in {
                "filled", "cancelled", "canceled", "rejected", "expired", "aborted", "replaced"}:
            order_token = parent_token
        if order_token != parent_token:
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
                or any(not _closed_with_replacements(session, row, request.requested_at_ms) for row in children)):
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


def _reviewed_momentum_abort(session, order, parent, observed):
    """Two reviewed repair writers only; a bare aborted label is not no-wire proof."""
    payload, md = parent.payload or {}, _metadata(order)
    episode = payload.get("momlive_episode", {})
    strategy = session.get(Strategy, order.strategy_id)
    account = session.get(BrokerAccount, order.broker_account_id)
    if (not strategy or strategy.code != "momentum_warn_live" or not account or not account.is_active
            or payload.get("momlive_parent") is not True or parent.status != "cancelled"
            or episode.get("episode_id") != str(parent.id) or md.get("episode_id") != str(parent.id)
            or episode.get("symbol") != order.symbol or episode.get("state") != "terminal_no_fill"
            or any(key not in episode or episode[key] is not None
                   for key in ("first_submit_ns", "first_fill_ns", "closed_ns"))
            or str(episode.get("filled")) != "0" or str(episode.get("exited")) != "0"
            or order.submitted_at is not None or order.quantity != parent.quantity or order.quantity <= 0
            or any("bot_control_permit" in value for value in (payload, _metadata(parent), order.payload or {}))):
        return False
    legacy = payload.get("repair_O_1019_26")
    settlement = payload.get("repair_MOMSTUCK2_20261021")
    if legacy is not None and settlement is not None:
        return False
    if isinstance(legacy, dict):
        if (str(parent.id) != "f723bbd2-5477-5a0b-b3ca-bc9ab023c5a7"
                or str(order.id) != "6c0c5a75-f01b-4e8f-98c9-8d5364edaf00"
                or order.client_order_id != "ml1-WORX-e-f723bbd254775a0bb3ca"
                or order.symbol != "WORX" or order.quantity != 32
                or account.name != "live:schwab_1m_v2" or account.provider != "schwab"
                or md.get("bot_control_generation") != "0"
                or legacy.get("repair_id") != "O-1019-26:momlive-f723-unsent-release"
                or legacy.get("actor") != "claude-1" or legacy.get("prior_state") != "claimed"
                or legacy.get("reason") != "proven-unsent claimed episode (order aborted, never wired, no permit/events/fills) blocked momentum-live boot"
                or re.fullmatch(r"[0-9a-f]{64}", str(legacy.get("backup_sha256", ""))) is None
                or not str(legacy.get("maintenance_receipt", "")).startswith("oms=inactive at ")):
            return False
        receipt = legacy
    elif isinstance(settlement, dict):
        if (str(parent.id) != "9ba4a2c0-aacc-5dba-a444-55a5e290ed2f"
                or str(order.id) != "75e3889e-fad2-44c5-8641-1b642cc68831"
                or order.client_order_id != "ml1-SPHL-e-9ba4a2c0aacc5dbaa444"
                or order.symbol != "SPHL" or account.name != "live:orb" or account.provider != "webull"
                or md.get("momlive_entry_never_dispatched") is not True
                or md.get("reject_reason") != "refused_before_wire"
                or md.get("repair_MOMSTUCK2_20261021") != settlement
                or settlement.get("repair_id") != "MOMSTUCK2:SPHL:20261021:refused-before-wire"
                or settlement.get("actor") != "claude-1" or settlement.get("scope") != "LIVE_STOPPED"
                or settlement.get("reason") != "refused_before_wire"
                or settlement.get("prior_episode_state") != "claimed"
                or settlement.get("prior_intent_status") != "pending"
                or settlement.get("prior_order_status") != "pending"
                or not settlement.get("snapshot_id") or not isinstance(settlement.get("evidence"), dict)):
            return False
        for name in ("backup", "export", "schema", "preimage_review", "journal",
                     "native_webull", "native_schwab", "maintenance", "installed_permit_proof", "refusal_log"):
            ref = settlement["evidence"].get(name, {})
            if (not isinstance(ref, dict) or not ref.get("ref") or ref.get("verified_by") != "claude-1"
                    or re.fullmatch(r"[0-9a-f]{64}", str(ref.get("sha256", ""))) is None):
                return False
        receipt = settlement
    else:
        return False
    stamp = datetime.fromisoformat(str(receipt.get("at", "")))
    if (stamp.tzinfo is None or not 0 <= _at(stamp) <= observed - 15_000
            or _at(parent.updated_at) < _at(stamp)):
        return False
    # Re-check the repair's single-entry/no-dispatch census. New contradictory
    # children or control attempts cannot be hidden behind a historical receipt.
    siblings = _bounded(session, select(BrokerOrder).where(or_(
        BrokerOrder.intent_id == parent.id,
        BrokerOrder.payload["episode_id"].as_string() == str(parent.id),
        BrokerOrder.client_order_id.like("%" + parent.id.hex[:20] + "%"))))
    attempts = _bounded(session, select(BotControlAttempt).where(or_(
        BotControlAttempt.parent_intent_id == parent.id, BotControlAttempt.existing_intent_id == parent.id,
        BotControlAttempt.entry_order_id == order.id,
        BotControlAttempt.existing_order_id == order.id, BotControlAttempt.target_order_id == order.id,
        BotControlAttempt.client_order_id == order.client_order_id)))
    return len(siblings) == 1 and siblings[0].id == order.id and not attempts


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
            if not events and _reviewed_momentum_abort(session, order, parent, observed):
                return True
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
        if any(latest.payload.get(key) is not None and latest.payload[key] != expected
               for key, expected in (("client_order_id", order.client_order_id),
                                     ("broker_order_id", order.broker_order_id))):
            return False
        status = "cancelled" if order.status == "canceled" else order.status
        event_status = "cancelled" if latest.event_type == "canceled" else latest.event_type
        if fills:
            fully_filled = status == event_status == "filled"
            account = session.get(BrokerAccount, order.broker_account_id)
            strategy = session.get(Strategy, order.strategy_id)
            managed = _bounded(session, select(OmsManagedPosition).where(
                OmsManagedPosition.entry_order_id == order.id))
            if not all(f.broker_fill_id and f.symbol == order.symbol and f.side == "buy"
                    and f.strategy_id == order.strategy_id and f.broker_account_id == order.broker_account_id
                    and f.quantity.is_finite() and f.quantity > 0
                    and _at(f.filled_at) <= observed - 15_000 for f in fills):
                return False
            executed = sum(f.quantity for f in fills)
            if (latest.payload.get("broker_fill_id") is not None
                    and latest.payload["broker_fill_id"] not in {f.broker_fill_id for f in fills}):
                return False
            if not fully_filled and any(not _at(order.submitted_at) <= _at(f.filled_at) <= _at(latest.event_at)
                                        for f in fills):
                return False
            for value in (latest.payload.get("filled_quantity"),
                          _metadata(latest).get("rpg_terminal_filled_quantity")):
                if value is not None and Decimal(str(value)) != executed:
                    return False
            remainder_closed = (status == event_status and status in {"cancelled", "rejected", "expired"}
                or status == "replaced" and replacement_linked
                    and event_status in {"cancelled", "rejected", "expired"})
            if status == "cancelled" and event_status == "partially_filled":
                remainder_closed = _rpg_partial_cancel(session, order, latest, executed, account, strategy)
            # A cancelled remainder is not a zero-execution order. The executed
            # quantity must have its own exact, fully closed managed attribution.
            return bool(account and strategy and managed and 0 < executed <= order.quantity
                and (remainder_closed or fully_filled and executed == order.quantity)
                and all(m.status == "closed" and m.current_quantity == 0
                    and m.symbol == order.symbol and m.strategy_code == strategy.code
                    and m.broker_account_name == account.name
                    and m.entry_client_order_id == order.client_order_id
                    and (fully_filled or m.original_quantity == executed) for m in managed))
        return bool(not fills and status in {"cancelled", "rejected", "expired", "replaced"}
            and event_status in {"cancelled", "rejected", "expired"}
            and (status == event_status or status == "replaced" and replacement_linked))
    except (ValueError, TypeError, ArithmeticError, AttributeError):
        return False


def _rpg_partial_cancel(session, order, event, executed, account, strategy):
    """The existing RPG writer's terminal readback, not a bare partial-fill label."""
    md = _metadata(event)
    if md.get("atr_reprice_terminal_cancel") != "true" or not account or not strategy:
        return False
    try:
        row = session.get(DashboardSnapshot, UUID(md["atr_reprice_readback"]))
        job = row.payload if row is not None and row.snapshot_type == "atr_reprice_handoff" else {}
        old = job.get("old", {})
        old_md = old.get("metadata", {})
        return bool(job.get("phase") == "filled" and job.get("no_rebuy") is True
            and not job.get("replacement") and job.get("broker_status") in {"CANCELED", "CANCELLED"}
            and Decimal(str(job.get("filled_quantity"))) == executed
            and Decimal(str(job.get("recorded_filled_quantity"))) == executed
            and old.get("client_order_id") == order.client_order_id
            and old.get("symbol") == order.symbol and old.get("side") == "buy"
            and old.get("broker_account_name") == account.name and old.get("strategy_code") == strategy.code
            and Decimal(str(old.get("quantity"))) == order.quantity
            and old_md.get("broker_order_id") == order.broker_order_id
            and (old_md.get("rpg_proof_order_id") is None
                 or old_md["rpg_proof_order_id"] == str(order.id)
                 and old_md.get("rpg_proof_broker_id") == order.broker_order_id))
    except (ValueError, TypeError, KeyError, ArithmeticError, AttributeError):
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


def _closed_with_replacements(session, order, observed, *, jobs=None):
    """Share linkage proof with foreign-history and global working-BUY readers."""
    if jobs is None:
        jobs = _bounded(session, select(DashboardSnapshot).where(
            DashboardSnapshot.snapshot_type == "atr_reprice_handoff",
            or_(DashboardSnapshot.payload["old"]["client_order_id"].as_string() == order.client_order_id,
                DashboardSnapshot.payload["replacement"]["client_order_id"].as_string() == order.client_order_id)))
    matched = [row for row in jobs if any(isinstance(request, dict)
        and request.get("client_order_id") == order.client_order_id
        for request in (row.payload.get("old"), row.payload.get("replacement")))]
    if any(not _replacement_closed(session, row.payload, order.symbol, observed) for row in matched):
        return False
    linked = any(row.payload.get("replacement") for row in matched)
    return _closed_order(session, order, observed, replacement_linked=bool(linked))


def working_buy_present(session, symbol, *, now=None):
    """All strategies/accounts; only exact persisted no-wire/closed proof exempts history."""
    observed = _at(now or datetime.now(UTC))
    terminal = {"cancelled", "canceled", "filled", "rejected", "expired"}
    jobs = _bounded(session, select(DashboardSnapshot).where(
        DashboardSnapshot.snapshot_type == "atr_reprice_handoff",
        DashboardSnapshot.payload["old"]["symbol"].as_string() == symbol))
    if any(not _replacement_closed(session, row.payload, symbol, observed) for row in jobs):
        return True

    orders = _bounded(session, select(BrokerOrder).where(
        BrokerOrder.symbol == symbol, BrokerOrder.side == "buy",
        or_(BrokerOrder.status.is_(None), BrokerOrder.status.notin_(terminal))))
    if any(not _closed_with_replacements(session, row, observed, jobs=jobs) for row in orders):
        return True
    intents = _bounded(session, select(TradeIntent).where(
            TradeIntent.symbol == symbol, TradeIntent.side == "buy", TradeIntent.intent_type == "open",
            or_(TradeIntent.status.is_(None), TradeIntent.status.notin_(terminal))))
    for parent in intents:
        if parent.status not in {"aborted", "completed", "replaced"}:
            return True
        siblings = _bounded(session, select(BrokerOrder).where(BrokerOrder.intent_id == parent.id))
        if not siblings or any(not _closed_with_replacements(session, row, observed, jobs=jobs) for row in siblings):
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
