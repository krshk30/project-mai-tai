"""C1 tie-only controls; source/default compatibility is a frozen old-byte hash."""
from dataclasses import replace, fields
from decimal import Decimal
import hashlib
import json
from pathlib import Path

import pytest

from project_mai_tai.atr_simulation.same_sip import (
    TieTape, TieState, SAME_SIP_CONSERVATIVE_V1 as POLICY, LABEL, enabled,
)
from project_mai_tai.atr_simulation.same_sip_fill_models import PrintLiquidity
from project_mai_tai.atr_simulation.same_sip_exits import Position
from project_mai_tai.atr_simulation.same_sip_firstbar import FirstBarMode, plan_firstbar_entry
from project_mai_tai.atr_simulation.same_sip_firstbar_exits import FirstBarPosition
from project_mai_tai.atr_simulation.same_sip_firstbar_replay import FirstBarReplayEngine, FirstBarFillPolicy
from project_mai_tai.atr_simulation.same_sip_firstbar_live_baseline import simulate_aggregate_entry, BaselineDay
from project_mai_tai.atr_simulation.same_sip_types import EpisodeInput
from project_mai_tai.atr_simulation.types import EntryFact, FillModel, SimExitReason, Boundary, Quote, EventKey
from tests.support.atr_same_sip_cases import (
    SOURCE, NS, COVERAGE, trade, replay, order, aggregate, encoded, compatibility_cases,
)


def flag(value):
    return replace(value, episode=EpisodeInput(**{f.name:getattr(value.episode,f.name)
        for f in fields(value.episode)}, same_sip_policy=POLICY))


@pytest.mark.parametrize('name', [f'{engine}:{case}' for engine in ('firstbar', 'core')
    for case in ('target', 'stop', 'sell', 'unknown', 'excluded')]
    + ['aggregate:target', 'aggregate:stop', 'aggregate:confirmation'])
def test_default_case_exact_frozen_old_output(name):
    expected = json.loads((Path(__file__).parents[1] / 'fixtures/atr_same_sip_old7f95_hashes.json').read_text())
    actual = encoded(compatibility_cases(family=True)[name]).encode()
    assert hashlib.sha256(actual).hexdigest() == expected['cases'][name]


@pytest.mark.parametrize('model', tuple(FillModel))
def test_limit_mixed_group_waits_then_fills_at_limit_shared_volume(model):
    rows = (trade(61, '1.95', seq=1), trade(61, '2.05', seq=2), trade(62, '1.90', size=150))
    states = [TieState(TieTape(rows)), TieState(TieTape(rows))]
    ledger = PrintLiquidity(model)
    orders = [order(account=account, model=model) for account in ('one', 'two')]
    for row in rows[:2]:
        assert all(ledger.allocate(o, row, same_sip_state=s) is None for o, s in zip(orders, states))
    fills = [ledger.allocate(o, rows[2], same_sip_state=s) for o, s in zip(orders, states)]
    assert [x.quantity for x in fills] == [100, 50]
    assert all(x.price == Decimal('1.90') and LABEL not in x.assumption for x in fills)
    assert all(s.report()['entry_groups_blocked'] == 1 for s in states)
    assert sum(x.quantity for x in fills) == rows[2].size


def test_legacy_limit_still_uses_original_improved_print_price():
    result = PrintLiquidity(FillModel.OPTIMISTIC).allocate(order(), trade(61, '1.90'))
    assert result.price == Decimal('1.90') and LABEL not in result.assumption


@pytest.mark.parametrize('mode', tuple(FirstBarMode))
def test_firstbar_breakout_retreat_waits_later_eligible_breakout_at_print_price(mode):
    rows = (trade(61, '2.03', seq=1), trade(61, '1.99', seq=2),
            trade(62, '2.07'), trade(63, '2.25'))
    value = flag(replay(rows))
    engine = FirstBarReplayEngine()
    result = next(x for x in engine.simulate(value,
        fill_policy=FirstBarFillPolicy.OBSERVED_PRINT_VOLUME) if x.mode is mode)
    assert result.fills[0].at == rows[2].key and result.fills[0].price == Decimal('2.07')
    assert result.exits[0].reason is SimExitReason.TARGET_5
    report = next(v for k, v in engine.same_sip_reports.items() if k[1] == mode.value)
    assert report['entry_groups_blocked'] == 1 and report['label'] == LABEL


@pytest.mark.parametrize('firstbar', (False, True))
@pytest.mark.parametrize('reverse', (False, True))
def test_target_stop_same_timestamp_stop_wins_with_existing_stop_model(firstbar, reverse):
    values = ('1.70', '2.20') if reverse else ('2.20', '1.70')
    rows = tuple(trade(102, price, seq=i + 1) for i, price in enumerate(values))
    state = TieState(TieTape(rows))
    kwargs = dict(sell_flip=None, session_end=Boundary(300 * NS, SOURCE),
                  coverage=COVERAGE, same_sip_state=state)
    position = FirstBarPosition(mode=FirstBarMode.FIRSTBAR, **kwargs) if firstbar else Position(**kwargs)
    position.add_fill(EntryFact('CONTROLLED:entry', trade(100, '2').key, Decimal('2'), 100, SOURCE))
    for row in rows:
        position.on_trade(row)
    result = position.finish()[0]
    assert result.reason is SimExitReason.STOP_8
    assert result.price == (Decimal('1.70') if firstbar else Decimal('1.84'))
    assert result.at == next(row.key for row in rows if row.price == Decimal('1.70'))
    assert state.report()['exit_stop_wins'] == 1


def test_real_unknown_in_group_not_certified_by_conservative_rule():
    rows = (trade(61, '2.03', seq=1), trade(61, '1.99', seq=2, unknown=True), trade(62, '2.07'))
    value = flag(replay(rows))
    result = plan_firstbar_entry(value.episode, value.flip_bar, mode=FirstBarMode.FIRSTBAR,
                                 entry_cutoff=value.entry_cutoff)
    assert result.decision == 'UNKNOWN'
    assert 'UNKNOWN_EXECUTION_FIRSTBAR_ENTRY_ELIGIBILITY' in result.issues


def test_firstbar_two_accounts_keep_one_physical_pool_after_blocked_group():
    rows = (trade(61, '2.03', seq=1), trade(61, '1.99', seq=2),
            trade(62, '2.07', size=150), trade(63, '2.25'))
    engine = FirstBarReplayEngine()
    results = engine.simulate_many(tuple(flag(replay(rows, account=account)) for account in ('one', 'two')),
        fill_policy=FirstBarFillPolicy.OBSERVED_PRINT_VOLUME)
    for mode in FirstBarMode:
        selected = [x for x in results if x.mode is mode]
        assert sum(fill.quantity for x in selected for fill in x.fills if fill.at == rows[2].key) == 150
        assert all(fill.at.event_ns != 61 * NS for x in selected for fill in x.fills)


def test_excluded_and_oddlot_retreat_do_not_invent_ambiguity():
    rows = (trade(61, '2.03', seq=1), trade(61, '1.99', seq=2, size=99),
            trade(61, '1.99', seq=3, conditions='37'))
    state = TieState(TieTape(rows))
    assert state.entry_allowed(rows[0], Decimal('2.01'), kind='stop', placed_ns=60 * NS)
    assert state.report()['entry_groups_blocked'] == 0


def test_sell_boundary_keeps_priority_over_protective_timestamp():
    rows = (trade(240, '2.20', seq=1), trade(240, '1.70', seq=2))
    state = TieState(TieTape(rows))
    position = FirstBarPosition(mode=FirstBarMode.FIRSTBAR, sell_flip=Boundary(240 * NS, SOURCE),
        session_end=Boundary(300 * NS, SOURCE), coverage=COVERAGE, same_sip_state=state)
    position.add_fill(EntryFact('entry', trade(100, '2').key, Decimal('2'), 100, SOURCE))
    position.on_trade(rows[0])
    assert position.finish()[0].reason is SimExitReason.SELL_FLIP
    assert state.report()['exit_stop_wins'] == 0


def test_aggregate_recorded_entry_and_confirmation_boundary_remain_external_facts():
    rows = (trade(100, '2.20', seq=1), trade(100, '1.70', seq=2),
            trade(120, '2.20', seq=1), trade(120, '1.70', seq=2))
    value = aggregate(rows, confirmation=True)
    reports = {}
    result = simulate_aggregate_entry(replace(value, day=BaselineDay(**{
        f.name:getattr(value.day,f.name) for f in fields(value.day)},
        same_sip_policy=POLICY)), same_sip_reports=reports)
    assert len(result.same_sip_print_sources) == 2
    assert result.aggregate_entry.price == Decimal('2.00')
    assert result.exits[0].reason.value == 'CONFIRMATION_EXIT'
    assert result.exits[0].price == Decimal('2.20')
    assert next(iter(reports.values()))['exit_stop_wins'] == 0


def test_reports_bound_witnesses_but_retain_complete_counts():
    rows = tuple(row for second in range(1, 41) for row in
        (trade(second, '1.90', seq=1), trade(second, '2.10', seq=2)))
    state = TieState(TieTape(rows))
    for row in rows:
        state.entry_allowed(row, Decimal('2'))
    report = state.report()
    assert report['entry_groups_blocked'] == 40 and len(report['events']) == 32
    assert report['events_truncated'] is True


def test_unrecognized_policy_refuses_instead_of_defaulting():
    with pytest.raises(ValueError, match='explicit_supported_same_sip_policy_required'):
        enabled('operator-approved')


def test_original_sealed_loaders_still_accept_original_source_bytes():
    from project_mai_tai.atr_simulation.engine_binding import verify_engine_files
    from project_mai_tai.atr_simulation.firstbar_binding import verify_firstbar_files
    assert verify_engine_files() == '5e3aba0dfea1bce93b50c598e522b8b67902e19e'
    assert verify_firstbar_files() == 'e712a99800b9cfd716b406e027845f89567647fe'


def test_current_successor_requires_its_own_exact_source_manifest():
    from project_mai_tai.atr_simulation.same_sip_binding import verify_same_sip_files
    assert verify_same_sip_files() == POLICY


@pytest.mark.parametrize('name', ('same_sip_engine.py', 'types.py'))
def test_successor_does_not_accept_changed_new_or_legacy_dependency(tmp_path, name):
    from project_mai_tai.atr_simulation import same_sip_binding as binding
    from project_mai_tai.atr_simulation.engine_binding import ENGINE_PINS
    from project_mai_tai.atr_simulation.firstbar_binding import FIRSTBAR_PINS
    root = Path(binding.__file__).parent
    target = tmp_path / 'atr_simulation'
    target.mkdir()
    names = {*ENGINE_PINS, *FIRSTBAR_PINS, *binding.SOURCE_PINS, '../broker_adapters/target_price.py'}
    for member in names:
        path = target / member
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes((root / member).read_bytes())
    assert binding.verify_same_sip_files(target) == POLICY
    (target / name).write_bytes((target / name).read_bytes() + b'\n# changed\n')
    with pytest.raises(ValueError, match='source_pin_mismatch'):
        binding.verify_same_sip_files(target)


def core_inputs(rows, *, ask='2.02'):
    quote = Quote(EventKey(59 * NS, 0, 'quote:59', SOURCE), 'X',
                  Decimal('2.01') if ask == '2.02' else Decimal('1.99'), Decimal(ask), 0, 100)
    return tuple(replace(flag(replay(rows, account=account)).episode,
                         trigger=Decimal('2.00'), quotes=(quote,)) for account in ('one', 'two'))


def test_active_core_worlds_keep_shared_volume_and_conservative_exit():
    from project_mai_tai.atr_simulation.same_sip_engine import SimulationEngine
    rows = (trade(60, '2.00'), trade(61, '1.99', seq=1), trade(61, '2.04', seq=2),
            trade(62, '2.00', size=150), trade(63, '2.20', seq=1), trade(63, '1.70', seq=2))
    engine = SimulationEngine()
    results = engine.simulate_many(core_inputs(rows))
    for attr in ('volbody_queue_aware', 'volbody_optimistic'):
        selected = [getattr(result, attr) for result in results]
        assert sum(fill.quantity for value in selected for fill in value.fills) == 150
        assert sorted(value.position.filled_qty for value in selected) == [50, 100]
        assert all(fill.price == Decimal('2.00') and fill.print_key == rows[3].key
                   for value in selected for fill in value.fills)
        assert all(value.exits[0].reason is SimExitReason.STOP_8 for value in selected)
        assert all(value.exits[0].price == Decimal('1.8400') for value in selected)
    reports = [value for key, value in engine.same_sip_reports.items() if key[1].startswith('VOLBODY:')]
    assert len(reports) == 4 and all(value['entry_groups_blocked'] == 1 for value in reports)


def test_active_core_real_unknown_group_preserves_both_account_uncertainty():
    from project_mai_tai.atr_simulation.same_sip_engine import SimulationEngine
    rows = (trade(60, '2.00'), trade(61, '1.99', seq=1),
            trade(61, '2.04', seq=2, unknown=True), trade(62, '2.00'))
    results = SimulationEngine().simulate_many(core_inputs(rows))
    for result in results:
        for attr in ('volbody_queue_aware', 'volbody_optimistic'):
            value = getattr(result, attr)
            assert value.gross_pnl is None and value.execution_unknowns


def test_active_core_marketable_placement_tied_timestamp_is_not_filled():
    from project_mai_tai.atr_simulation.same_sip_engine import SimulationEngine
    rows = (trade(60, '2.00', seq=1), trade(60, '2.04', seq=2), trade(62, '1.99', size=150))
    results = SimulationEngine().simulate_many(core_inputs(rows, ask='2.00'))
    for result in results:
        for attr in ('volbody_queue_aware', 'volbody_optimistic'):
            value = getattr(result, attr)
            assert all(fill.print_key.event_ns == 62 * NS and fill.price == Decimal('1.99')
                       for fill in value.fills)


def test_policy_on_without_any_tie_has_byte_identical_outputs():
    from project_mai_tai.atr_simulation.same_sip_engine import SimulationEngine
    assert encoded(compatibility_cases(family=True, policy=POLICY)) == encoded(compatibility_cases(family=True))
    rows = (trade(60, '2.00'), trade(61, '1.90'), trade(63, '2.20'))
    for ask in ('2.00', '2.02'):
        active = core_inputs(rows, ask=ask)
        legacy = tuple(replace(item, same_sip_policy=None) for item in active)
        engine = SimulationEngine()
        assert encoded(engine.simulate_many(active)) == encoded(SimulationEngine().simulate_many(legacy))
        assert all(not value['entry_groups_blocked'] and not value['exit_stop_wins']
                   and not value['limit_pricing_fills'] for value in engine.same_sip_reports.values())


@pytest.mark.parametrize('model', tuple(FillModel))
def test_actual_tied_limit_fill_uses_limit_price_and_counts_only_ties(model):
    rows = (trade(61, '1.90', seq=1), trade(61, '1.95', seq=2))
    state = TieState(TieTape(rows))
    fill = PrintLiquidity(model).allocate(order(model=model), rows[0], same_sip_state=state)
    assert fill.price == Decimal('2') and LABEL in fill.assumption
    assert state.report()['limit_pricing_fills'] == 1
