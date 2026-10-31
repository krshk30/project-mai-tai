"""Author UNRUN: literal retained events + explicitly CONTROLLED DB bindings.

No database, adapter, SDK or provider. The partial original capture does not
contain typed order/account rows; their bindings below are CONTROLLED, not a
claim that any historical owner has been proven releasable on the live box.
"""

from copy import deepcopy
from datetime import UTC, datetime, timedelta
from decimal import Decimal
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace as Row
from uuid import UUID, uuid4

import pytest

from project_mai_tai.webull_same_day_terminal import zero_fill_terminal_event


FIXTURE = Path(__file__).parents[1] / 'fixtures/webull_same_day_terminal/pcla_original_events_20261029.jsonl'
FIXTURE_SHA = '585db761bc2947b9e0830d655c7c0be85e284e1d110aded50fa904d270301002'
PROVEN_EVENT = '67733a2f-4a9c-4e28-b753-8920fbb734f6'
UNPROVEN_ORDERS = (
    'df215d0b-c61a-45bb-ab35-9cc90a0eca88',
    '02dd3eab-2053-46e0-b6ef-faeebaf6942b',
    '60c8f24f-4344-4733-8abb-67c5e3d6d3a1',
    '29dfb281-6c0a-4099-941f-110a9b77c653',
)


def original_events():
    raw = FIXTURE.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == FIXTURE_SHA
    rows = [json.loads(line) for line in raw.splitlines()]
    assert len(rows) == 19 and all(row['symbol'] == 'PCLA' for row in rows)
    return rows


def controlled_binding():
    rows = original_events()
    positive = next(row for row in rows if row['id'] == PROVEN_EVENT)
    raw_proof = json.loads(positive['payload']['metadata']['_webull_terminal_proof'])
    events = [Row(id=UUID(row['id']), order_id=UUID(row['order_id']),
        event_type=row['event_type'], event_source=row['event_source'],
        event_at=datetime.fromisoformat(row['event_at']), payload=deepcopy(row['payload']))
        for row in rows if row['order_id'] == positive['order_id']]
    # CONTROLLED typed account UUID and typed quantity/native-account corroboration:
    # values from detail proof are inputs, not a second independent DB receipt.
    account = Row(id=UUID('00000000-0000-0000-0000-0000000000b1'),
        provider='webull', name='live:orb', external_account_id=raw_proof['account_id'])
    order = Row(id=UUID(positive['order_id']), broker_account_id=account.id,
        client_order_id=positive['client_order_id'], broker_order_id=positive['payload']['broker_order_id'],
        symbol='PCLA', side='buy', quantity=Decimal('35'), status='cancelled',
        submitted_at=datetime(2026, 10, 29, 13, 56, 3, tzinfo=UTC),
        payload=deepcopy(positive['payload']['metadata']))
    observed = int(datetime(2026, 10, 29, 18, tzinfo=UTC).timestamp() * 1000)
    return order, events, account, observed


def invoke(order, events, account, observed, *, enabled=True, strategy='schwab_1m_v2'):
    return zero_fill_terminal_event(order, events, account, strategy_code=strategy,
                                   observed_at_ms=observed, enabled=enabled)


def proof_event(events):
    return next(event for event in events if str(event.id) == PROVEN_EVENT)


def change_proof(events, change):
    event = proof_event(events)
    proof = json.loads(event.payload['metadata']['_webull_terminal_proof'])
    change(proof)
    event.payload['metadata']['_webull_terminal_proof'] = json.dumps(proof)


def test_literal_requested_event_needs_actual_zero_detail_and_does_not_mutate():
    order, events, account, observed = controlled_binding()
    before = deepcopy((vars(order), [vars(event) for event in events], vars(account)))
    assert proof_event(events).payload['metadata']['cancel_outcome'] == 'requested'
    assert invoke(order, events, account, observed) is proof_event(events)
    assert (vars(order), [vars(event) for event in events], vars(account)) == before


def test_default_off_never_inspects_operands():
    assert zero_fill_terminal_event(object(), object(), object(),
        strategy_code='schwab_1m_v2', observed_at_ms=0) is None


@pytest.mark.parametrize('outcome', ['requested', 'confirmed_after_accepted_request', 'confirmed', None])
def test_outcome_labels_never_substitute_for_positive_detail(outcome):
    order, events, account, observed = controlled_binding()
    event = proof_event(events)
    event.payload['metadata']['cancel_outcome'] = outcome
    assert invoke(order, events, account, observed) is event
    event.payload['metadata'].pop('_webull_terminal_proof')
    assert invoke(order, events, account, observed) is None


@pytest.mark.parametrize('order_id', UNPROVEN_ORDERS)
def test_four_literal_heuristic_cancellations_remain_unknown(order_id):
    order, _, account, observed = controlled_binding()
    rows = [row for row in original_events() if row['order_id'] == order_id]
    assert rows and all('_webull_terminal_proof' not in row['payload']['metadata'] for row in rows)
    assert all(row['payload']['broker_order_id'] is None for row in rows)
    events = [Row(id=UUID(row['id']), order_id=UUID(row['order_id']),
        event_type=row['event_type'], event_source=row['event_source'],
        event_at=datetime.fromisoformat(row['event_at']), payload=deepcopy(row['payload'])) for row in rows]
    order.id, order.client_order_id, order.broker_order_id = UUID(order_id), rows[0]['client_order_id'], None
    assert invoke(order, events, account, observed) is None


@pytest.mark.parametrize('fault', [
    'missing', 'wrong-source', 'wrong-account', 'wrong-client', 'wrong-native',
    'wrong-symbol', 'wrong-side', 'wrong-quantity', 'missing-fill', 'partial-fill',
    'nan-fill', 'bool-fill', 'contradictory-fill', 'nonterminal', 'version',
    'execution-positive', 'missing-native-body', 'multiple-items', 'future-proof',
    'naive-proof', 'duplicate-json', 'oversize-json', 'malformed-json', 'root-positive-fill',
    'root-working-status', 'outcome-conflict', 'refusal-origin',
])
def test_missing_ambiguous_or_conflicting_detail_stays_unknown(fault):
    order, events, account, observed = controlled_binding()
    event = proof_event(events)
    proof = json.loads(event.payload['metadata']['_webull_terminal_proof'])
    item = proof['body']['items'][0]
    if fault == 'missing':
        event.payload['metadata'].pop('_webull_terminal_proof')
    elif fault == 'wrong-source':
        proof['source'] = '/trade/orders/list'
    elif fault == 'wrong-account':
        proof['body']['account_id'] = 'CONTROLLED-foreign'
    elif fault == 'wrong-client':
        proof['body']['client_order_id'] = 'CONTROLLED-foreign'
    elif fault == 'wrong-native':
        proof['body']['order_id'] = 'CONTROLLED-foreign'
    elif fault == 'wrong-symbol':
        item['symbol'] = 'OTHER'
    elif fault == 'wrong-side':
        item['side'] = 'SELL'
    elif fault == 'wrong-quantity':
        item['qty'] = '36'
    elif fault == 'missing-fill':
        item.pop('filled_qty')
    elif fault == 'partial-fill':
        item['filled_qty'] = '1'
    elif fault == 'nan-fill':
        item['filled_qty'] = 'NaN'
    elif fault == 'bool-fill':
        item['filled_qty'] = False
    elif fault == 'contradictory-fill':
        item['filledQty'] = '1'
    elif fault == 'nonterminal':
        item['order_status'] = 'WORKING'
    elif fault == 'version':
        proof['version'] = '0' * 64
    elif fault == 'execution-positive':
        proof['execution'][2] = '1'
    elif fault == 'missing-native-body':
        proof['body'].pop('order_id')
    elif fault == 'multiple-items':
        proof['body']['items'].append(deepcopy(item))
    elif fault == 'future-proof':
        proof['acquired_at'] = '2026-10-30T00:00:00+00:00'
    elif fault == 'naive-proof':
        proof['acquired_at'] = '2026-10-29T13:57:04'
    elif fault == 'root-positive-fill':
        proof['body']['filled_quantity'] = '1'
    elif fault == 'root-working-status':
        proof['body']['status'] = 'WORKING'
    elif fault == 'outcome-conflict':
        event.payload['cancel_outcome'] = 'confirmed'
    elif fault == 'refusal-origin':
        event.payload['refusal_origin'] = 'could_not_tell'
    if fault == 'duplicate-json':
        event.payload['metadata']['_webull_terminal_proof'] = '{"source":"x","source":"y"}'
    elif fault == 'oversize-json':
        event.payload['metadata']['_webull_terminal_proof'] = ' ' * 65537
    elif fault == 'malformed-json':
        event.payload['metadata']['_webull_terminal_proof'] = '{'
    elif fault != 'missing':
        event.payload['metadata']['_webull_terminal_proof'] = json.dumps(proof)
    assert invoke(order, events, account, observed) is None


@pytest.mark.parametrize('fault', ['schwab', 'account-binding', 'account-native-missing', 'strategy',
    'segment', 'event-client', 'event-native', 'event-origin', 'event-order', 'duplicate-event',
    'fill-event', 'fill-id', 'partial-marker', 'fresh-event', 'working-order', 'empty-events'])
def test_scope_execution_and_maturity_negatives(fault):
    order, events, account, observed = controlled_binding()
    event = proof_event(events)
    strategy = 'schwab_1m_v2'
    if fault == 'schwab':
        account.provider = 'schwab'
    elif fault == 'account-binding':
        account.id = uuid4()
    elif fault == 'account-native-missing':
        account.external_account_id = None
    elif fault == 'strategy':
        strategy = 'momentum_warn_live'
    elif fault == 'segment':
        order.payload['fanout_segment_id'] = '1'
    elif fault == 'event-client':
        event.payload['client_order_id'] = 'CONTROLLED-foreign'
    elif fault == 'event-native':
        event.payload['broker_order_id'] = 'CONTROLLED-foreign'
    elif fault == 'event-origin':
        event.event_source = 'unknown'
    elif fault == 'event-order':
        event.order_id = uuid4()
    elif fault == 'duplicate-event':
        events.append(deepcopy(event))
    elif fault == 'fill-event':
        events[0].event_type = 'partially_filled'
    elif fault == 'fill-id':
        events[0].payload['broker_fill_id'] = 'CONTROLLED-fill'
    elif fault == 'partial-marker':
        event.payload['metadata']['webull_terminal_with_partial_fill'] = 'cancelled'
    elif fault == 'fresh-event':
        observed = int(event.event_at.timestamp() * 1000) + 14999
    elif fault == 'working-order':
        order.status = 'accepted'
    elif fault == 'empty-events':
        events.clear()
    assert invoke(order, events, account, observed, strategy=strategy) is None


def controlled_later_audit(events):
    """CONTROLLED additional audit; not present for the positive historical order."""
    audit = deepcopy(proof_event(events))
    audit.id = uuid4()
    audit.event_at += timedelta(seconds=1)
    audit.event_type = 'rejected'
    audit.payload['internal'] = 'CANCEL_VERIFY_RESUBMIT'
    audit.payload['broker_order_id'] = None
    md = audit.payload['metadata']
    md.pop('_webull_terminal_proof')
    md.update(webull_error_code='ORDER_CAN_NOT_BE_CANCEL', cancel_outcome='confirmed_after_accepted_request')
    return audit


def test_scoped_later_cancel_request_rejection_is_not_target_state_or_proof():
    order, events, account, observed = controlled_binding()
    audit = controlled_later_audit(events)
    events.append(audit)
    assert invoke(order, events, account, observed) is proof_event(events)
    proof_event(events).payload['metadata'].pop('_webull_terminal_proof')
    assert invoke(order, events, account, observed) is None


@pytest.mark.parametrize('fault', ['unknown-purpose', 'other-code', 'working', 'filled', 'foreign-native',
    'foreign-segment', 'unknown-origin', 'same-time', 'could-not-tell'])
def test_later_conflict_or_nonexact_audit_cannot_be_skipped(fault):
    order, events, account, observed = controlled_binding()
    audit = controlled_later_audit(events)
    if fault == 'unknown-purpose':
        audit.payload.pop('internal')
    elif fault == 'other-code':
        audit.payload['metadata']['webull_error_code'] = 'OTHER'
    elif fault == 'working':
        audit.event_type = 'accepted'
    elif fault == 'filled':
        audit.event_type = 'filled'
    elif fault == 'foreign-native':
        audit.payload['broker_order_id'] = 'CONTROLLED-other'
    elif fault == 'foreign-segment':
        audit.payload['metadata']['fanout_segment_id'] = '2'
    elif fault == 'unknown-origin':
        audit.event_source = 'unknown'
    elif fault == 'same-time':
        audit.event_at = proof_event(events).event_at
    elif fault == 'could-not-tell':
        audit.payload['metadata']['cancel_outcome'] = 'could_not_tell'
    events.append(audit)
    assert invoke(order, events, account, observed) is None


@pytest.mark.parametrize('hours', [-5, 0])
def test_same_pure_rule_in_controlled_premarket_and_regular_clocks(hours):
    order, events, account, observed = controlled_binding()
    shift = timedelta(hours=hours)
    order.submitted_at += shift
    for event in events:
        event.event_at += shift
    change_proof(events, lambda proof: proof.update(acquired_at=(
        datetime.fromisoformat(proof['acquired_at']) + shift).isoformat()))
    assert invoke(order, events, account, observed + hours * 3600000) is proof_event(events)
