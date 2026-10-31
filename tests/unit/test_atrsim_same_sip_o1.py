"""Changed-source equality and bounded group work; no performance rate claim."""
from dataclasses import replace
from decimal import Decimal
import hashlib
import json
from pathlib import Path

import pytest

from project_mai_tai.atr_simulation.same_sip import TieTape, TieState, SAME_SIP_CONSERVATIVE_V1
from project_mai_tai.atr_simulation.execution_eligibility import execution_status
from project_mai_tai.atr_simulation.same_sip_fill_models import PrintLiquidity
from project_mai_tai.atr_simulation.types import FillModel
from tests.support.atr_same_sip_cases import trade, order, encoded, compatibility_cases, NS


class Counted(tuple):
    def __iter__(self):
        for row in super().__iter__():
            self.visits += 1
            yield row


@pytest.mark.parametrize('round_lot', (False, True))
def test_repeated_stop_queries_scan_group_once_keep_live_thresholds(monkeypatch, round_lot):
    rows = tuple(trade(61, '1.5' if i % 2 else '2.5', seq=i) for i in range(512))
    tape = TieTape(rows)
    counted = Counted(rows)
    counted.visits = 0
    monkeypatch.setattr(tape, 'eligible', lambda row: counted)
    tape._unresolved[('X', 61 * NS)] = False
    state = TieState(tape)
    for row in rows:
        assert state.stop_choice(row, stop=Decimal('1.6'), target=Decimal('2.4'),
                                 round_lot=round_lot) == rows[1]
    # Thresholds are intentionally NOT cached: changing them must change the result.
    assert state.stop_choice(rows[0], stop=Decimal('1.4'), target=Decimal('2.4'),
                             round_lot=round_lot) is None
    assert counted.visits <= 2 * len(rows)
    assert state.report()['exit_stop_wins'] == 1


@pytest.mark.parametrize('stop,target', (('1','2'), ('1.7','2.2'), ('1.8','2.2'),
    ('1.8','2.3'), ('2.3','2.2'), ('1.7','1.8'), ('3','4')))
def test_cached_exit_choice_equals_original_scan_including_exact_witness(stop, target):
    rows = (trade(61, '2.2', seq=5), trade(61, '1.7', seq=3),
            trade(61, '1.7', seq=1), trade(61, '1.1', size=99, seq=7),
            trade(61, '1.0', conditions='37', seq=8))
    eligible = tuple(row for row in rows if execution_status(row)[0] is True)
    stop, target = Decimal(stop), Decimal(target)
    stops = tuple(row for row in eligible if row.price <= stop)
    expected = (min(stops, key=lambda row: (row.price, row.key.sequence, row.key.raw_id))
        if stops and any(row.price >= target for row in eligible) else None)
    state = TieState(TieTape(rows))
    assert [state.stop_choice(row, stop=stop, target=target) for row in rows] == [expected] * len(rows)
    assert state.report()['exit_stop_wins'] == int(expected is not None)


@pytest.mark.parametrize('low,high,allowed', (('4','6',True), ('1','1',False), ('0','11',True)))
def test_band_lookup_keeps_price_gap_and_mixed_threshold_semantics(low, high, allowed):
    rows = (trade(61, '1', seq=1), trade(61, '10', seq=2))
    state = TieState(TieTape(rows))
    assert all(state.entry_in_band(row, Decimal(low), Decimal(high)) is allowed for row in rows)


def test_fixed_episode_band_does_not_rescan_group_for_every_print(monkeypatch):
    rows = tuple(trade(61, '2', seq=i) for i in range(512))
    tape = TieTape(rows)
    counted = Counted(rows)
    counted.visits = 0
    monkeypatch.setattr(tape, 'eligible', lambda row: counted)
    state = TieState(tape)
    assert all(state.entry_in_band(row, Decimal('1.9'), Decimal('2.1')) for row in rows)
    assert counted.visits <= 2 * len(rows)


def test_unknown_witness_preserves_first_evidence_minimum_sequence_and_expiry():
    rows = (trade(61, '2', seq=9, unknown=True), trade(61, '2', seq=2, unknown=True),
            trade(61, '2', seq=4))
    tape = TieTape(rows)
    assert tape.unknown_witness(rows[2]) == (2, rows[0])
    ledger = PrintLiquidity(FillModel.OPTIMISTIC)
    assert ledger.allocate(order(), rows[2], same_sip_state=TieState(tape)) is None
    evidence, = ledger.unknown_evidence('X', 61 * NS, 2)
    assert evidence.raw_id == rows[0].key.raw_id
    assert ledger.unknown_evidence('X', 61 * NS, 1) == ()
    assert ledger.unknown_evidence('X', 240 * NS, 2) == ()


def test_singletons_keep_fast_empty_lookup_and_do_not_accumulate_false_cache_entries():
    rows = tuple(trade(i, '2') for i in range(61, 561))
    tape = TieTape(rows)
    assert all(not tape.unresolved(row) and tape.eligible(row) == () for row in rows)
    assert tape._unresolved == {} and tape._eligible == {}


def test_same_clock_other_symbol_never_shares_exit_or_band_cache():
    rows = (trade(61, '1', seq=1), trade(61, '10', seq=2))
    other = tuple(replace(row, symbol='Y', price=row.price + 20, execution=None) for row in rows)
    tape = TieTape(rows + other)
    assert tape.band_mixed(rows[0], Decimal('1'), Decimal('2')) is True
    assert tape.band_mixed(other[0], Decimal('1'), Decimal('2')) is False
    assert tape.unresolved(other[0]) is True


@pytest.mark.parametrize('policy', (None, SAME_SIP_CONSERVATIVE_V1))
@pytest.mark.parametrize('name', [f'{engine}:{case}' for engine in ('firstbar', 'core')
    for case in ('target', 'stop', 'sell', 'unknown', 'excluded')]
    + ['aggregate:target', 'aggregate:stop', 'aggregate:confirmation'])
def test_changed_cache_source_retains_complete_frozen_non_tie_outputs(policy, name):
    expected = json.loads((Path(__file__).parents[1]/'fixtures/atr_same_sip_old7f95_hashes.json').read_text())
    document = compatibility_cases(family=True, policy=policy)[name]
    assert hashlib.sha256(encoded(document).encode()).hexdigest() == expected['cases'][name]
