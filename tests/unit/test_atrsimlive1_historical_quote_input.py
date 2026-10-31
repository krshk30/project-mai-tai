"""Private controlled SQL-shape rehydration; no retained GZ/model replay."""
from copy import deepcopy
from decimal import Decimal

import pytest

from atrsimlive1_captured_rr import historical_model_input, CapturedRefusal
from atrsimlive1_collect import digest


def packet(bid='2.010000', ask='2.030000'):
    return dict(source_asof='2026-10-29T03:36:15.030688+00:00',
        read_proof={'source_asof': '2026-10-29T03:36:15.030688+00:00'},
        source_hashes={'quotes': 'CONTROLLED-not-a-SQL-claim'},
        rows=dict(quotes=[dict(id=1, provider='massive', symbol='X',
            event_ts='2026-10-28T13:30:00+00:00', received_at='2026-10-28T13:30:00.001+00:00',
            bid_price=bid, ask_price=ask, bid_size=100, ask_size=200)],
            bars=[dict(indicators={'number': 1.5, 'text': '1.5', 'null': None})],
            trades=[dict(conditions='14', price='2.030000')]))


@pytest.mark.parametrize('bid,ask,branch', [
    ('2.010000', '2.030000', 'MARKETABLE_ASK'),
    (None, '2.030000', 'UNKNOWN_INVALID_BOOK'),
    ('2.010000', None, 'UNKNOWN_INVALID_BOOK'),
])
def test_quote_SQL_decimal_copy_reaches_real_placement_reader_without_raw_mutation(bid, ask, branch):
    from project_mai_tai.atr_simulation.entry import placement_book
    from project_mai_tai.atr_simulation.types import Quote, EventKey, SourceRef, EvidenceKind

    original = packet(bid, ask)
    before = deepcopy(original)
    actual = historical_model_input(original)
    quote = actual['rows']['quotes'][0]
    source = SourceRef('CONTROLLED:private-quote-input', 'a' * 64, '$', EvidenceKind.CONTROLLED)
    model = Quote(EventKey(1, 1, 'CONTROLLED', source), 'X', quote['bid_price'], quote['ask_price'],
                  quote['bid_size'], quote['ask_size'])
    assert placement_book(Decimal('2.030000'), model)[0] == branch
    assert original == before
    assert actual['rows']['quotes'] is not original['rows']['quotes']
    assert actual['rows']['quotes'][0] is not original['rows']['quotes'][0]
    assert actual['rows']['bars'] is original['rows']['bars']
    assert actual['rows']['trades'] is original['rows']['trades']
    assert type(actual['rows']['bars'][0]['indicators']['number']) is float
    assert type(actual['rows']['bars'][0]['indicators']['text']) is str
    assert actual['rows']['trades'][0]['conditions'] == '14'
    assert digest(actual) == digest(original)  # Original SQL/evidence serialized content unchanged.
    assert actual['source_hashes'] is original['source_hashes']
    if bid is None:
        assert quote['bid_price'] is None
    else:
        assert type(quote['bid_price']) is Decimal and str(quote['bid_price']) == bid


@pytest.mark.parametrize('bad', ['bad', 'NaN', True, 2.01])
def test_non_SQL_or_nonfinite_quote_carrier_refuses_instead_of_inventing_price(bad):
    with pytest.raises(CapturedRefusal):
        historical_model_input(packet(bid=bad))
