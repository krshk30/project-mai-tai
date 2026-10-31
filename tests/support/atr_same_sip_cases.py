"""Explicit CONTROLLED cases shared with the frozen 7f95 output producer."""
from dataclasses import asdict, replace, fields
from decimal import Decimal
import json

from project_mai_tai.atr_simulation.types import (
    Boundary, Coverage, EpisodeInput, EventKey, EvidenceKind,
    FillModel, FlipIdentity, LiveFacts, SimOrder, SourceRef, TradePrint,
)
from project_mai_tai.atr_simulation.execution_eligibility import classify_execution
from project_mai_tai.atr_simulation.firstbar import CompletedFlipBar
from project_mai_tai.atr_simulation.firstbar_replay import FirstBarReplayInput
from project_mai_tai.atr_simulation.firstbar_live_baseline import (
    AggregateBaselineInput, BaselineBarClose, BaselineDay, AGGREGATE_ASSUMPTION,
)
from project_mai_tai.atr_simulation.firstbar_study import StudyBuyLeg
from tests.support.atr_simulation_eligibility import POLICY as ORIGINAL_POLICY

NS = 1_000_000_000
SOURCE = SourceRef('CONTROLLED:same-sip-cases', 'a' * 64, '$', EvidenceKind.CONTROLLED)
POLICY = replace(ORIGINAL_POLICY, source=SOURCE)
COVERAGE = Coverage(0, 300 * NS, True, (), (SOURCE,))
IDENTITY = FlipIdentity('2026-10-23', 'X', 'webull', 'one', 'ATR', 'flip', 'segment')


def trade(second, price='2.01', size=100, seq=0, *, unknown=False, conditions='14'):
    key = EventKey(second * NS, seq, f'CONTROLLED:{second}:{seq}', SOURCE)
    raw = TradePrint(key, 'X', Decimal(price), size)
    return replace(raw, execution=None if unknown else classify_execution(raw, conditions, POLICY))


def episode(rows, *, account='one', quantity=100):
    return EpisodeInput(replace(IDENTITY, account_id=account),
        Boundary(60 * NS, SOURCE, bar_start_ns=0), True, SOURCE,
        Decimal('1.98'), quantity, SOURCE, Boundary(240 * NS, SOURCE),
        Boundary(300 * NS, SOURCE), tuple(rows), (), COVERAGE, COVERAGE, (), COVERAGE,
        LiveFacts(None, (), (), replace(COVERAGE, complete=None)), POLICY)


def replay(rows, *, account='one', quantity=100):
    return FirstBarReplayInput(episode(rows, account=account, quantity=quantity),
        CompletedFlipBar('X', 0, 60 * NS, Decimal('2'), Decimal('1.99'),
                        Decimal('1.98'), 'long', True, SOURCE), Boundary(180 * NS, SOURCE))


def aggregate(rows, *, confirmation=False):
    from project_mai_tai.atr_simulation.firstbar_study import _boundary
    base = _boundary('2026-10-23', 4)
    rows = tuple(replace(row, key=replace(row.key, event_ns=base + row.key.event_ns)) for row in rows)
    leg = StudyBuyLeg('one', 'order', 'STOP_LIMIT', 'X', base + 90 * NS,
                     base + 100 * NS, Decimal('2.00'), 100, '2.00', '2.00', SOURCE)
    day = BaselineDay('X', '2026-10-23', tuple(rows),
        (Boundary(base + 50 * NS, SOURCE), Boundary(base + 240 * NS, SOURCE)),
        Boundary(base + 300 * NS, SOURCE), replace(COVERAGE, start_ns=base, end_ns=base + 300 * NS), (), SOURCE,
        (('SELL', Boundary(base + 50 * NS, SOURCE)), ('BUY', Boundary(base + 75 * NS, SOURCE)),
         ('SELL', Boundary(base + 240 * NS, SOURCE))),
        (BaselineBarClose(base + 60 * NS, base + 120 * NS, Decimal('1.80') if confirmation else Decimal('2.00'),
                          Decimal('1.90'), 'short' if confirmation else 'long', SOURCE),))
    return AggregateBaselineInput(('X', '2026-10-23', '04:01:40'), IDENTITY, leg, day,
                                  AGGREGATE_ASSUMPTION)


def order(*, account='one', model=FillModel.OPTIMISTIC, quantity=100):
    return SimOrder('order:' + account, replace(IDENTITY, account_id=account), model,
                    EventKey(60 * NS, 0, 'PLACEMENT', SOURCE), Decimal('2'), quantity,
                    0, None, valid_until_ns=240 * NS)


def encoded(value):
    return json.dumps(value, default=lambda x: str(x) if not hasattr(x, '__dataclass_fields__')
                      else asdict(x), sort_keys=True, separators=(',', ':'))


def compatibility_cases(*, family=False, policy=None):
    """No tied timestamps. Economic outcomes and raw source refs are fixed."""
    from project_mai_tai.atr_simulation.firstbar_replay import FirstBarReplayEngine, FirstBarFillPolicy
    from project_mai_tai.atr_simulation.firstbar_live_baseline import simulate_aggregate_entry
    from project_mai_tai.atr_simulation.engine import SimulationEngine
    if family:
        from project_mai_tai.atr_simulation.same_sip_firstbar_replay import FirstBarReplayEngine, FirstBarFillPolicy
        from project_mai_tai.atr_simulation.same_sip_firstbar_live_baseline import simulate_aggregate_entry, BaselineDay
        from project_mai_tai.atr_simulation.same_sip_engine import SimulationEngine
        from project_mai_tai.atr_simulation.same_sip_types import EpisodeInput as CurrentEpisode
    rows = (trade(61, size=100), trade(62, '2.20'))
    output = {}
    for name, tape in (
        ('target', rows), ('stop', (trade(61), trade(62, '1.80'))),
        ('sell', (trade(61), trade(240, '2.00'))),
        ('unknown', (trade(61), trade(62, '2.20', unknown=True))),
        ('excluded', (trade(61, conditions='37'), trade(62), trade(63, '2.20'))),
    ):
        value = replay(tape)
        item = episode(tape)
        if family:
            value = replace(value, episode=CurrentEpisode(**{f.name:getattr(value.episode, f.name)
                                                           for f in fields(value.episode)}, same_sip_policy=policy))
            item = CurrentEpisode(**{f.name:getattr(item, f.name) for f in fields(item)}, same_sip_policy=policy)
        output['firstbar:' + name] = FirstBarReplayEngine().simulate(value,
            fill_policy=FirstBarFillPolicy.OBSERVED_PRINT_VOLUME)
        output['core:' + name] = SimulationEngine().simulate(item)
    for name, tape, confirmation in (
        ('target', (trade(101, '2.20'),), False),
        ('stop', (trade(101, '1.80'),), False),
        ('confirmation', (trade(120, '1.80'),), True),
    ):
        value = aggregate(tape, confirmation=confirmation)
        if family:
            value = replace(value, day=BaselineDay(**{f.name:getattr(value.day,f.name) for f in fields(value.day)},
                                                  same_sip_policy=policy))
        output['aggregate:' + name] = simulate_aggregate_entry(value)
    return output
