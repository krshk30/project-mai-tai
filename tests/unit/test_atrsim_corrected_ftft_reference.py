"""C1 corrected human input; no oracle or model change."""
from datetime import datetime, timedelta
from pathlib import Path
import sys
from zoneinfo import ZoneInfo
import pytest

# Tool modules retain sibling imports; make this file independently collectible.
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))

from atrsimlive1_line_route import parity, parity_summary, REFERENCES, SUPERSEDED_REFERENCES
from atrsimlive1_models import nanos

ET = ZoneInfo('America/New_York')
DAY = '2026-10-28'


def clock(value):
    return datetime.fromisoformat(DAY+'T'+value).replace(tzinfo=ET)


def buy(value):
    stamp = clock(value)
    return dict(original={'id': 'CONTROLLED:'+value}, oracle={'flip': 'BUY'},
        start_ns=nanos(stamp), available_ns=nanos(stamp+timedelta(minutes=1)))


@pytest.mark.parametrize('positive', ('15:38', '15:39', '15:40'))
def test_corrected_buy_window_and_absent_falseflip_pass(positive):
    result = parity(DAY, 'FTFT', [buy(positive)], asof_ns=nanos(clock('16:00')), nanos=nanos)
    fact, = result['comparisons']
    assert fact['status'] == 'MATCH' and fact['superseded_reference'] == '15:32'
    assert fact['forbidden_matches'] == [] and len(fact['matches']) == 1


@pytest.mark.parametrize('forbidden', ('14:37', '14:38', '14:39'))
def test_completed_falseflip_buy_cannot_be_waived_by_later_positive(forbidden):
    result = parity(DAY, 'FTFT', [buy(forbidden), buy('15:39')], asof_ns=nanos(clock('16:00')), nanos=nanos)
    fact, = result['comparisons']
    assert fact['status'] == 'MISMATCH' and len(fact['forbidden_matches']) == 1


def test_missing_positive_stays_mismatch_and_original_replacement_is_not_new_flip():
    result = parity(DAY, 'FTFT', [buy('15:32')], asof_ns=nanos(clock('16:00')), nanos=nanos)
    assert result['comparisons'][0]['status'] == 'MISMATCH'
    assert result['comparisons'][0]['matches'] == []


def test_future_reference_window_remains_unknown_until_its_full_completion():
    result = parity(DAY, 'FTFT', [buy('15:38')], asof_ns=nanos(clock('15:40')), nanos=nanos)
    assert result['comparisons'][0]['status'] == 'UNKNOWN_PENDING_REFERENCE'


def test_all_six_required_and_missing_corrected_reference_blocks():
    facts = [dict(symbol=symbol, reference=reference, status='MATCH') for symbol, reference in REFERENCES[DAY]]
    result = parity_summary(DAY, [dict(comparisons=facts)])
    assert result['expected'] == 6 and not result['blocks_qualification']
    absent = parity_summary(DAY, [dict(comparisons=facts[:-1])])
    assert absent['expected'] == 6 and absent['unresolved'] == 1 and absent['blocks_qualification']
    assert SUPERSEDED_REFERENCES[DAY] == (('FTFT', '15:32'),)
