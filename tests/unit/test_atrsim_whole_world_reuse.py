"""Identical-input whole-cohort reuse; current SQL verification is external."""
from dataclasses import fields, replace
import hashlib
from itertools import combinations

import pytest

from project_mai_tai.atr_simulation.same_sip import SAME_SIP_CONSERVATIVE_V1, report_key
from project_mai_tai.atr_simulation.same_sip_types import EpisodeInput
from project_mai_tai.atr_simulation.same_sip_engine import SimulationEngine
from project_mai_tai.atr_simulation.same_sip_firstbar import FirstBarMode
from project_mai_tai.atr_simulation.same_sip_firstbar_replay import FirstBarReplayEngine, FirstBarFillPolicy
from project_mai_tai.atr_simulation.same_sip_reuse import CORE_WORLDS
from tests.support.atr_same_sip_cases import episode, replay, trade, encoded


def flagged(value):
    return EpisodeInput(**{field.name: getattr(value, field.name) for field in fields(value)},
                        same_sip_policy=SAME_SIP_CONSERVATIVE_V1)


def cohort():
    rows = (trade(61, '1.95', seq=1), trade(61, '2.05', seq=2),
            trade(62, '1.90', size=150), trade(63, '2.20'))
    return tuple(flagged(episode(rows, account=account)) for account in ('one', 'two'))


def bindings(items, worlds):
    seals = tuple(hashlib.sha256(encoded(item).encode()).hexdigest() for item in items)
    return {world: seals for world in worlds}


WORLD_SETS = [subset for count in range(1, 4) for subset in combinations(CORE_WORLDS, count)]


@pytest.mark.parametrize('worlds', WORLD_SETS)
def test_whole_world_assembly_is_exact_fresh_and_skips_selected_work(monkeypatch, worlds):
    import project_mai_tai.atr_simulation.same_sip_engine as module
    inputs = cohort()
    fresh_engine = SimulationEngine()
    fresh = fresh_engine.simulate_many(inputs)
    counts = {'today': 0, 'prepare': []}
    old_today, old_prepare = module._today_plans, module.prepare_entry
    def today(*args, **kwargs):
        counts['today'] += 1
        return old_today(*args, **kwargs)
    def prepare(item, model):
        counts['prepare'].append(model.value)
        return old_prepare(item, model)
    monkeypatch.setattr(module, '_today_plans', today)
    monkeypatch.setattr(module, 'prepare_entry', prepare)
    proof = bindings(inputs, worlds)
    cached_engine = SimulationEngine()
    actual = cached_engine.simulate_many(inputs, retained_worlds={world: fresh for world in worlds},
        retained_reports=fresh_engine.same_sip_reports, retained_bindings=proof, current_bindings=proof)
    assert encoded(actual) == encoded(fresh)
    assert cached_engine.same_sip_reports == fresh_engine.same_sip_reports
    assert counts['today'] == int('TODAY:SUPPLIED' not in worlds)
    for world in ('VOLBODY:QUEUE_AWARE', 'VOLBODY:OPTIMISTIC'):
        assert counts['prepare'].count(world.split(':')[1]) == (0 if world in worlds else len(inputs))


@pytest.mark.parametrize('failure', ('missing_member', 'reverse_order', 'input_changed',
    'missing_proof', 'bad_proof_shape', 'wrong_world_scenario', 'missing_report', 'partial_report'))
def test_partial_changed_or_unbound_cohort_refuses_before_any_skip(failure):
    inputs = cohort()
    engine = SimulationEngine()
    fresh = engine.simulate_many(inputs)
    world = 'TODAY:SUPPLIED'
    retained = {world: fresh}
    old = bindings(inputs, (world,))
    current = dict(old)
    reports = dict(engine.same_sip_reports)
    if failure == 'missing_member': retained[world] = fresh[:1]
    elif failure == 'reverse_order': retained[world] = tuple(reversed(fresh))
    elif failure == 'input_changed': current[world] = ('b'*64, old[world][1])
    elif failure == 'missing_proof': current = None
    elif failure == 'bad_proof_shape': current[world] = old[world] = ('true', 'true')
    elif failure == 'wrong_world_scenario':
        retained[world] = tuple(replace(item, today=item.volbody_optimistic) for item in fresh)
    elif failure == 'missing_report': reports.pop(report_key(inputs[0].identity, world))
    else: reports[report_key(inputs[0].identity, world)] = {'policy': SAME_SIP_CONSERVATIVE_V1}
    with pytest.raises(ValueError):
        SimulationEngine().simulate_many(inputs, retained_worlds=retained, retained_reports=reports,
            retained_bindings=old, current_bindings=current)


MODE_SETS = [(FirstBarMode.FIRSTBAR.value,), (FirstBarMode.FIRSTBAR_X3.value,),
             tuple(mode.value for mode in FirstBarMode)]


@pytest.mark.parametrize('modes', MODE_SETS)
def test_firstbar_mode_assembly_preserves_whole_shared_pool_and_reports(monkeypatch, modes):
    import project_mai_tai.atr_simulation.same_sip_firstbar_replay as module
    rows = (trade(61, '2.03', seq=1), trade(61, '1.99', seq=2),
            trade(62, '2.07', size=150), trade(63, '2.25'))
    items = tuple(replace(replay(rows, account=account), episode=flagged(replay(rows, account=account).episode))
                  for account in ('one', 'two'))
    engine = FirstBarReplayEngine()
    policy = FirstBarFillPolicy.OBSERVED_PRINT_VOLUME
    fresh = engine.simulate_many(items, fill_policy=policy)
    retained = {mode: tuple(row for row in fresh if row.mode.value == mode) for mode in modes}
    proof = bindings(tuple(item.episode for item in items), modes)
    calls = []
    original = module._position
    def position(item, mode, **kwargs):
        calls.append(mode.value)
        return original(item, mode, **kwargs)
    monkeypatch.setattr(module, '_position', position)
    cached = FirstBarReplayEngine()
    actual = cached.simulate_many(items, fill_policy=policy, retained_modes=retained,
        retained_reports=engine.same_sip_reports, retained_bindings=proof, current_bindings=proof)
    assert encoded(actual) == encoded(fresh) and cached.same_sip_reports == engine.same_sip_reports
    assert all(mode not in calls for mode in modes)
    assert len(calls) == (len(FirstBarMode) - len(modes)) * len(items)


def test_cache_arguments_cannot_silently_enable_without_retained_worlds():
    with pytest.raises(ValueError, match='retained_worlds_required'):
        SimulationEngine().simulate_many(cohort(), current_bindings={})
