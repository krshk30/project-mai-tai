"""Exact closed-child episode recovery; never flat-from-missing-row evidence."""

from dataclasses import dataclass
from datetime import UTC
from decimal import Decimal

from project_mai_tai.brokerdecides1_sync import enabled
from project_mai_tai.fanout_identity import fanout_slot_id


@dataclass(frozen=True)
class ClosedOwnerEpisode:
    account: str
    row_id: str
    opportunity: int
    entry_ms: int
    closed_ms: int


def held_entry_ids(accounts, opportunities):
    """Only exact already-held UNKNOWN identities may extend the session read."""
    from sqlalchemy import and_, false, or_, select
    from project_mai_tai.db.models import BrokerAccount, BrokerOrder, Strategy

    if len(opportunities or {}) > 512:
        raise ValueError("closed_owner_held_scope_overflow")
    terms = []
    for symbol, opportunity in (opportunities or {}).items():
        if (not isinstance(symbol, str) or not symbol or symbol != symbol.upper()
                or type(opportunity) is not int or opportunity <= 0):
            raise ValueError("closed_owner_held_identity_UNKNOWN")
        terms.append(and_(BrokerOrder.symbol == symbol,
            BrokerOrder.payload["fanout_segment_id"].as_string() == str(opportunity)))
    return (select(BrokerOrder.id)
        .join(BrokerAccount, BrokerAccount.id == BrokerOrder.broker_account_id)
        .join(Strategy, Strategy.id == BrokerOrder.strategy_id)
        .where(BrokerAccount.name.in_(accounts), Strategy.code == "schwab_1m_v2",
            BrokerOrder.side == "buy", or_(*terms) if terms else false()).correlate(None))


def _ms(value):
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return int(value.timestamp() * 1000)


def closed_child_episodes(rows, entries, exits):
    """Consume linked durable BUY/SELL fills from the existing background poll.

    entries/exits are (Fill, BrokerOrder, BrokerAccount) tuples. An OMS closed
    row alone, or a same-symbol SELL alone, cannot manufacture this proof.
    """
    buys, sells = {}, {}
    for destination, records, side in ((buys, entries, "buy"), (sells, exits, "sell")):
        seen = set()
        for fill, order, account in records:
            if fill.id in seen:
                raise ValueError("closed_owner_duplicate_fill")
            seen.add(fill.id)
            if (fill.order_id != order.id or fill.broker_account_id != account.id
                    or order.broker_account_id != account.id
                    or fill.strategy_id != order.strategy_id or fill.side != side
                    or order.side != side or fill.symbol != order.symbol
                    or Decimal(fill.quantity) <= 0):
                raise ValueError("closed_owner_fill_identity_UNKNOWN")
            destination.setdefault(order.id, []).append((fill, order, account))
    all_orders = {}
    for order_id, records in buys.items():
        order = records[0][1]
        try:
            opportunity = int((order.payload or {}).get("fanout_segment_id", 0))
        except (ValueError, TypeError):
            continue
        if opportunity > 0:
            all_orders.setdefault((order.symbol, opportunity), set()).add(order_id)
    result, proven_orders = {}, {}
    for row in rows:
        if (row.strategy_code != "schwab_1m_v2" or row.status != "closed"
                or row.current_quantity != 0 or row.original_quantity <= 0):
            continue
        records = buys.get(row.entry_order_id, ())
        if not records:
            continue
        _, entry, account = records[0]
        md = entry.payload or {}
        try:
            opportunity = int(md.get("fanout_segment_id", 0))
        except (ValueError, TypeError):
            continue
        if (opportunity <= 0 or not entry.intent_id or row.broker_account_name != account.name
                or row.symbol != entry.symbol or row.entry_client_order_id != entry.client_order_id
                or md.get("source") != "schwab_1m_v2" or md.get("resting_entry") != "true"
                or md.get("fanout_slot") != "resting" or md.get("cw_entry_slot") != "first"
                or md.get("fanout_slot_id") != fanout_slot_id(strategy_code="schwab_1m_v2",
                    symbol=row.symbol, segment_id=opportunity, slot="resting")
                or sum(Decimal(item[0].quantity) for item in records) != row.original_quantity
                or Decimal(entry.quantity) != row.original_quantity
                or entry.status != "filled"
                or any(_ms(fill.filled_at) < opportunity or _ms(fill.filled_at) > _ms(row.updated_at)
                    for fill, _, _ in records)):
            continue
        children = []
        for records in sells.values():
            _, child, child_account = records[0]
            if (child.intent_id != entry.intent_id or child.strategy_id != entry.strategy_id
                    or child_account.id != account.id or child.symbol != row.symbol
                    or child.order_type != "oco_exit" or child.status != "filled"
                    or not child.broker_order_id
                    or child.client_order_id != f"{entry.client_order_id}-ocoexit-{child.broker_order_id[-8:]}"
                    or Decimal(child.quantity) != row.original_quantity
                    or sum(Decimal(item[0].quantity) for item in records) != row.original_quantity
                    or any(_ms(fill.filled_at) < _ms(row.entry_time) or _ms(fill.filled_at) > _ms(row.updated_at)
                        for fill, _, _ in records)):
                continue
            children.append(max(_ms(fill.filled_at) for fill, _, _ in records))
        # More than one full child would contradict the one-episode quantity.
        if len(children) != 1:
            continue
        result.setdefault(row.symbol, []).append(ClosedOwnerEpisode(account.name,
            str(row.id), opportunity, _ms(row.entry_time), children[0]))
        proven_orders.setdefault((row.symbol, opportunity), set()).add(entry.id)
    return {symbol: tuple(proof for proof in proofs
        if proven_orders.get((symbol, proof.opportunity)) == all_orders.get((symbol, proof.opportunity)))
        for symbol, proofs in result.items()
        if any(proven_orders.get((symbol, proof.opportunity)) == all_orders.get((symbol, proof.opportunity))
            for proof in proofs)}


def recover_closed_owner(strategy, state, proofs):
    """Validate exact missed-child evidence without changing durable ownership.

    Recovery is evidence, not a consumed transition. Only the later-SHORT
    retirement below may write, after all current exception/card fences pass.
    """
    if (not enabled(getattr(strategy, "settings", None))
            or not strategy._flip_owner_restore_readable
            or state.flip_owner_phase != "unknown" or not state.flip_owner_first_rest_placed
            or not state.flip_owner_fill_accounts or state.flip_owner_open_positions
            or not strategy._flip_owner_evidence_fresh(state)):
        return False
    opportunity = state.flip_owner_opportunity_id
    if opportunity <= 0 or state.fanout_segment_id != opportunity:
        return False
    candidates = [proof for proof in proofs if proof.opportunity == opportunity]
    accounts = [proof.account for proof in candidates]
    now = strategy._now_ms()
    if (set(accounts) != state.flip_owner_fill_accounts or len(accounts) != len(set(accounts))
            or not set(accounts) <= strategy._flip_owner_accounts
            or any(not proof.row_id or not opportunity <= proof.entry_ms <= proof.closed_ms <= now
                for proof in candidates)
            or any(state.flip_owner_position_ids.get(proof.account, proof.row_id) != proof.row_id
                or state.flip_owner_position_entry_ms.get(proof.account, proof.entry_ms) != proof.entry_ms
                for proof in candidates)
            or set(state.flip_owner_position_ids) - set(accounts)
            or set(state.flip_owner_position_entry_ms) - set(accounts)):
        return False
    return True


def retire_closed_owner_on_new_short(strategy, state, proofs):
    """A missed SELL may retire a proven closed episode, never its old segment."""
    from project_mai_tai.brokerdecides1_entry import card_fill_segment_definite, exception_scope, line_segment

    if (not enabled(getattr(strategy, "settings", None))
            or state.flip_owner_phase != "unknown" or state.atr_state != "short"
            or not exception_scope(strategy, state)
            or not strategy._flip_owner_restore_readable or strategy._entries_held
            or not strategy._flip_owner_evidence_fresh(state)
            or state.symbol in getattr(strategy, "_removed_wait_requests", {})
            or not strategy.line_buy_ready(state.symbol) or strategy.gap_hold_active(state.symbol)
            or not strategy._fanout_identity_bar_is_live(state)
            or state.restart_rest_evidence_pending or state.restart_rest_cancel_fields
            or state.restart_rest_recover_accounts or state.flip_owner_open_positions
            or strategy._falseflip_entry_blocked(state)):
        return False
    candidates = [proof for proof in proofs if proof.opportunity == state.flip_owner_opportunity_id]
    accounts = [proof.account for proof in candidates]
    if (not accounts or len(accounts) != len(set(accounts))
            or set(accounts) != state.flip_owner_fill_accounts
            or state.fanout_segment_id != state.flip_owner_opportunity_id
            or not recover_closed_owner(strategy, state, candidates)
            or line_segment(state) <= max(state.flip_owner_retry_segment_id,
                state.flip_owner_flip_bar_ts, *(proof.entry_ms for proof in candidates))
            or any(proof.closed_ms > strategy._now_ms() for proof in candidates)
            or not card_fill_segment_definite(strategy, state)):
        return False
    return strategy._retire_flip_owner_opportunity(state, reason="brokerdecides_exact_child_closed_new_short")
