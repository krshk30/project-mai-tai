"""Closed-episode accounting only; never terminal proof or SELL authority."""
from copy import deepcopy
from datetime import datetime
from decimal import Decimal, InvalidOperation
import json
from uuid import UUID

from sqlalchemy import select

from project_mai_tai.db.models import (
    AccountPosition, BrokerAccount, BrokerOrder, BrokerOrderEvent, Fill,
    OmsManagedPosition, Strategy, TradeIntent, VirtualPosition,
)
from project_mai_tai.oms.route_owner import _utc_row_time


class _Unknown(ValueError):
    pass


def _require(value):
    if not value:
        raise _Unknown('durable_closed_episode_not_fully_attributed')


def _quantity(value):
    if type(value) is int:  # Managed-position quantities are mapped Integer.
        value = Decimal(value)
    _require(isinstance(value, Decimal) and value.is_finite()
             and value >= 0 and value == value.to_integral_value())
    return value


def _object(value):
    def unique(pairs):
        result = {}
        for key, item in pairs:
            _require(key not in result)
            result[key] = item
        return result
    if isinstance(value, str):
        value = json.loads(value, object_pairs_hook=unique,
            parse_constant=lambda _: (_ for _ in ()).throw(_Unknown('nonfinite_proof')))
    _require(isinstance(value, dict))
    return value


def _fills(db, order, side, anchor, now, *, terminal_zero=False):
    from project_mai_tai.oms.wbexit3 import LIMIT
    rows = db.scalars(select(Fill).where(Fill.order_id == order.id).limit(LIMIT + 1)
        .with_for_update().execution_options(populate_existing=True)).all()
    _require(len(rows) <= LIMIT and (bool(rows) or terminal_zero))
    seen = set()
    for fill in rows:
        at = _utc_row_time(fill.filled_at)
        _require(fill.strategy_id == order.strategy_id and fill.broker_account_id == order.broker_account_id
                 and fill.symbol == order.symbol and fill.side == side and bool(fill.broker_fill_id)
                 and fill.broker_fill_id not in seen and _quantity(fill.quantity) > 0
                 and isinstance(fill.price, Decimal) and fill.price.is_finite() and fill.price > 0 and at is not None
                 and anchor <= at <= now)
        seen.add(fill.broker_fill_id)
    return rows


def _reconcile(db, state, now):
    from project_mai_tai.oms import wbexit2, wbexit3
    from project_mai_tai.oms.service import oco_exit_client_order_id
    wbexit2.timestamp(now)
    # No durable task or SELL claim is adopted, even when the row is flat.
    _require(state['phase'] != 'RESOLVED' and state.get('invocation') is None
             and state.get('originals_proven') is True and bool(state.get('native_account_id')))
    current = wbexit2.read(db, state['identity'], lock=True)
    _require(current == state)
    binding = state['identity']
    entry = db.get(BrokerOrder, UUID(binding['entry_id']), with_for_update=True, populate_existing=True)
    managed = db.get(OmsManagedPosition, UUID(binding['managed_id']), with_for_update=True, populate_existing=True)
    _require(entry is not None and managed is not None)
    _require(wbexit2.identity(entry, managed, db.get(BrokerAccount, entry.broker_account_id),
        db.get(Strategy, entry.strategy_id)) == binding)
    parent = db.get(TradeIntent, entry.intent_id, with_for_update=True, populate_existing=True)
    _require(parent is not None and parent.intent_type == 'open' and parent.side == 'buy'
             and parent.strategy_id == entry.strategy_id and parent.broker_account_id == entry.broker_account_id
             and parent.symbol == entry.symbol and parent.status == entry.status == 'filled')
    _require(managed.status == 'closed' and _quantity(managed.current_quantity) == 0)
    original = wbexit2.positive_quantity(state['original_quantity'])
    _require(_quantity(entry.quantity) == original == _quantity(managed.original_quantity))
    anchor = datetime.fromisoformat(state['entry_anchor_at'])
    _require(anchor.tzinfo is not None and _utc_row_time(entry.submitted_at) == anchor and anchor <= now)
    buys = _fills(db, entry, 'buy', anchor, now)
    _require(sum((f.quantity for f in buys), Decimal(0)) == original)
    journal = (entry.payload or {}).get('wbattach1', {})
    _require(isinstance(journal, dict) and journal.get('entry_id') == str(entry.id)
             and journal.get('managed_id') == str(managed.id)
             and journal.get('wbexit2_owner_id') == state['owner_id']
             and journal.get('history_proven') is True and not journal.get('exit_phase'))
    attempts = journal.get('attempts')
    _require(isinstance(attempts, list) and attempts and len(attempts) <= wbexit3.LIMIT
             and len(attempts) == len(state['originals']))
    by_base = {a['base']: a for a in attempts}
    _require(len(by_base) == len(attempts) and set(by_base) == {p['base'] for p in state['originals']})
    orders = db.scalars(select(BrokerOrder).where(BrokerOrder.intent_id == entry.intent_id,
        BrokerOrder.side == 'sell').limit(wbexit3.LIMIT + 1)
        .with_for_update().execution_options(populate_existing=True)).all()
    _require(len(orders) <= wbexit3.LIMIT)
    highwater, used_orders, used_native, execution_ids = {}, set(), set(), []
    buy_boundary = max(_utc_row_time(f.filled_at) for f in buys)
    last_fill = buy_boundary
    for pair in state['originals']:
        attempt = by_base[pair['base']]
        protection = db.get(TradeIntent, UUID(attempt['intent_id']), with_for_update=True, populate_existing=True)
        _require(protection is not None and protection.strategy_id == entry.strategy_id
                 and protection.broker_account_id == entry.broker_account_id and protection.symbol == entry.symbol
                 and protection.side == 'sell' and protection.intent_type == 'close'
                 and protection.reason == 'WBATTACH1_PROTECTION'
                 and attempt.get('phase') in {'accepted', 'released'}
                 and Decimal(attempt['quantity']) == wbexit2.positive_quantity(pair['quantity']))
        metadata = (protection.payload or {}).get('metadata', {})
        _require(metadata.get('entry_order_id') == str(entry.id)
                 and metadata.get('managed_row_id') == str(managed.id)
                 and metadata.get('webull_protect_base_client_order_id') == pair['base']
                 and _quantity(protection.quantity) == wbexit2.positive_quantity(pair['quantity']))
        bookkeeping = _object(metadata.get('wbattach1_terminal_bookkeeping'))
        proof = _object(bookkeeping.get('proof'))
        _require(proof.get('account') == binding['account'] and proof.get('symbol') == entry.symbol
                 and proof.get('account_id') == state['native_account_id'] and proof.get('base') == pair['base']
                 and bookkeeping.get('combo_id') == metadata.get('webull_protect_combo_id')
                 == attempt.get('combo_id') and bool(attempt.get('combo_id')))
        saved = proof.get('children')
        _require(isinstance(saved, list) and len(saved) == 2
                 and {c['coid'] for c in saved} == {c['coid'] for c in pair['children']})
        _require(len(pair['children']) == 2)
        for child in pair['children']:
            coid, native = child['coid'], child['native_id']
            _require(coid in {pair['base'] + 'T', pair['base'] + 'S'} and coid not in highwater
                     and isinstance(native, str) and bool(native) and native not in used_native
                     and metadata.get('webull_protect_target_client_order_id' if coid.endswith('T')
                                      else 'webull_protect_stop_client_order_id') == coid)
            used_native.add(native)
            _require(next(c for c in saved if c['coid'] == coid).get('broker_order_id') == native)
            matches = [o for o in orders if o.broker_order_id == native]
            _require(len(matches) <= 1)
            quantity = Decimal(0)
            if matches:
                order = matches[0]
                _require(order.id not in used_orders and order.strategy_id == entry.strategy_id
                         and order.broker_account_id == entry.broker_account_id and order.symbol == entry.symbol
                         and order.client_order_id == oco_exit_client_order_id(entry.client_order_id, native)
                         and order.order_type == 'oco_exit' and order.status == 'filled'
                         and (order.payload or {}) == {'source': 'native_oco_child_leg', 'broker_order_id': native})
                fills = _fills(db, order, 'sell', buy_boundary, now)
                _require(all(f.payload == {'source': 'native_oco_child_leg', 'broker_order_id': native}
                             and f.broker_fill_id.startswith(native + ':') for f in fills))
                quantity = sum((f.quantity for f in fills), Decimal(0))
                _require(quantity == _quantity(order.quantity) and quantity <= original)
                execution_ids.extend(str(f.id) for f in fills)
                last_fill = max(last_fill, *(_utc_row_time(f.filled_at) for f in fills))
                used_orders.add(order.id)
            _require(quantity >= wbexit2.positive_quantity(state['child_fill_highwater'][coid], zero=True))
            highwater[coid] = str(quantity.to_integral_value())
    # No unrelated canonical child or ambiguous attribution can be omitted.
    _require(used_orders == {o.id for o in orders} and set(highwater) == set(state['child_fill_highwater']))
    prior = wbexit2.positive_quantity(state.get('prior_software_filled', '0'), zero=True)
    our = Decimal(0)
    sell = state.get('sell')
    if sell is not None:
        owned = db.scalars(select(BrokerOrder).where(BrokerOrder.client_order_id == sell['coid']).limit(2)
            .with_for_update().execution_options(populate_existing=True)).all()
        _require(len(owned) == 1)
        order = owned[0]
        intent = db.get(TradeIntent, order.intent_id, with_for_update=True, populate_existing=True)
        _require(order.strategy_id == entry.strategy_id and order.broker_account_id == entry.broker_account_id
                 and order.symbol == entry.symbol and order.side == 'sell' and bool(order.broker_order_id)
                 and order.status in {'filled', 'cancelled', 'rejected', 'expired'}
                 and intent is not None and intent.reason == state['reason'] and intent.intent_type == 'close'
                 and intent.strategy_id == entry.strategy_id and intent.broker_account_id == entry.broker_account_id
                 and intent.symbol == entry.symbol and intent.side == 'sell'
                 and (order.payload or {}).get('entry_order_id') == str(entry.id)
                 and (order.payload or {}).get('oms_managed_row_id') == str(managed.id)
                 and _quantity(order.quantity) == wbexit2.positive_quantity(sell['quantity'])
                 and db.scalar(select(BrokerOrderEvent.id).where(BrokerOrderEvent.order_id == order.id,
                    BrokerOrderEvent.event_source == 'broker', BrokerOrderEvent.event_type == order.status).limit(1)) is not None)
        fills = _fills(db, order, 'sell', anchor, now,
            terminal_zero=order.status in {'cancelled', 'rejected', 'expired'}
                and wbexit2.positive_quantity(sell['filled_highwater'], zero=True) == 0)
        our = sum((f.quantity for f in fills), Decimal(0))
        _require(our == wbexit2.positive_quantity(sell['filled_highwater'], zero=True))
        if fills:
            last_fill = max(last_fill, *(_utc_row_time(f.filled_at) for f in fills))
    _require(wbexit3.prior_software_quantity(db, entry) == prior + our)
    _require(sum((Decimal(v) for v in highwater.values()), Decimal(0)) + prior + our == original)
    vp = db.scalars(select(VirtualPosition).where(VirtualPosition.strategy_id == entry.strategy_id,
        VirtualPosition.broker_account_id == entry.broker_account_id, VirtualPosition.symbol == entry.symbol).limit(2)
        .with_for_update().execution_options(populate_existing=True)).all()
    positions = db.scalars(select(AccountPosition).where(AccountPosition.broker_account_id == entry.broker_account_id,
        AccountPosition.symbol == entry.symbol).limit(2)
        .with_for_update().execution_options(populate_existing=True)).all()
    _require(len(vp) == len(positions) == 1 and _quantity(vp[0].quantity) == _quantity(positions[0].quantity) == 0)
    closed, virtual, observed, recorded = (_utc_row_time(v) for v in
        (managed.updated_at, vp[0].updated_at, positions[0].source_updated_at, positions[0].updated_at))
    _require(all(v is not None for v in (closed, virtual, observed, recorded))
             and last_fill <= closed <= observed <= recorded <= now and last_fill <= virtual <= now)
    new = deepcopy(state)
    new.update(child_fill_highwater=highwater, remaining='0', phase='RESOLVED',
               resolved_at=now.isoformat(), updated_at=now.isoformat(),
               durable_accounting_retirement={'source': 'canonical_original_child_fills_closed_flat',
                   'fill_ids': execution_ids, 'account_position_source_updated_at': observed.isoformat(),
                   'recorded_at': now.isoformat(), 'wire_authority': False, 'new_native_observation': False})
    # No TERMINAL transition, native receipt, cancel ACK, economic row or journal
    # mutation. The existing owner lock/generation CAS commits accounting only.
    return new


def reconcile_closed_originals(db, state, now):
    """Unknown/partial/held evidence retains the original responsibility."""
    try:
        new = _reconcile(db, state, now)
    except (KeyError, TypeError, ValueError, InvalidOperation):
        return state
    from project_mai_tai.oms import wbexit2
    return wbexit2._save(db, state, new)
