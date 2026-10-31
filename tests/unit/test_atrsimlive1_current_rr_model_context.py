"""CONTROLLED production-context boundary; no engine, query or model replay.

Use the real same-SIP EpisodeInput and IncrementalRun, not a FILE _proof
replacement or an episode stand-in with fields the actual type does not have.
These controls establish input-proof plumbing, never native/economic truth.
"""
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import importlib
from pathlib import Path

import pytest


LEFT = datetime(2026, 10, 28, 8, tzinfo=timezone.utc)
ORIGIN = 'RECOMPUTED_BUY:CONTROLLED_BAR'


def context(monkeypatch, tmp_path, family, watched):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2] / 'tools'))
    cache = importlib.import_module('atrsimlive1_incremental_cache')
    reads = importlib.import_module('atrsimlive1_incremental_reads')
    coordinator = importlib.import_module('atrsimlive1_incremental_collect')
    controller = importlib.import_module('atrsimlive1_incremental_models')
    windows = importlib.import_module('atrsimlive1_working_windows')
    from project_mai_tai.atr_simulation import same_sip_types as t
    from project_mai_tai.atr_simulation.same_sip import SAME_SIP_CONSERVATIVE_V1
    from project_mai_tai.atr_simulation.same_sip_firstbar import CompletedFlipBar
    from project_mai_tai.atr_simulation.same_sip_firstbar_replay import FirstBarReplayInput

    source = t.SourceRef('CONTROLLED:current-RR-context', 'c' * 64, '$', t.EvidenceKind.CONTROLLED)
    pins = {'CONTROLLED:source': 'd' * 64}
    policies = dict(execution_rules_sha256='b' * 64, null_policy=None,
                    same_sip_policy=SAME_SIP_CONSERVATIVE_V1)
    binding = dict(version=cache.INCREMENTAL_V1, trading_day='2026-10-28',
                   source_seal=cache.fingerprint(pins), policies=policies, input_scope='CURRENT_RR')
    root = (tmp_path / 'new-private-cache').resolve()
    root.mkdir(mode=0o700)
    run = coordinator.IncrementalRun(cache.CacheStore(root, binding))
    asof, end = LEFT + timedelta(minutes=10), LEFT + timedelta(minutes=4)
    run.bind_snapshot(dict(read_only='on', isolation='repeatable read', snapshot_id='CONTROLLED_RR'),
        dict(day=date(2026, 10, 28), start=LEFT, end=asof, asof=asof), ('CONTROLLED',))
    certificates = {}
    for kind in ('trades', 'quotes'):
        certificates[kind] = reads.certify(
            dict(row_count=1, content_sha256='e' * 64, source_timezone='UTC'),
            kind=kind, symbol='CONTROLLED', start=LEFT, end=end, day_start=LEFT,
            asof=asof, snapshot_id='CONTROLLED_RR', query_sha256=reads.query_pair(kind)['query_sha256'],
            read_only='on', isolation='repeatable read', complete=True)
    start_ns, end_ns = windows.nanos(LEFT), windows.nanos(end)
    run.next['windows']['CONTROLLED'] = {ORIGIN: dict(end_key=(end_ns - 1000, 2))}
    run.inputs['CONTROLLED'] = [dict(origin=ORIGIN, certificates=certificates)]
    coverage = t.Coverage(start_ns, end_ns, True, (), (source,))
    unknown_live = t.LiveFacts(None, (), (), replace(coverage, complete=None, gaps=('CONTROLLED_UNKNOWN_LIVE',)))
    episode = t.EpisodeInput(
        identity=t.FlipIdentity('2026-10-28', 'CONTROLLED', 'webull', 'SIM_ROUTE:webull',
                               'schwab_1m_v2', 'CONTROLLED_BUY', 'CONTROLLED_SEGMENT'),
        buy_flip=t.Boundary(start_ns + 60_000_000_000, source, start_ns),
        watched=watched, watched_source=source if watched is True else None,
        trigger=Decimal('2.01'), quantity=100, sizing_source=source,
        sell_flip=t.Boundary(end_ns - 1000, source), session_end=t.Boundary(end_ns, source),
        trades=(t.TradePrint(t.EventKey(end_ns - 1000, 2, 'CONTROLLED:terminal', source),
                             'CONTROLLED', Decimal('2.00'), 100),),
        quotes=(), tape_coverage=coverage, flip_coverage=coverage,
        today_entries=(), today_entry_coverage=replace(coverage, complete=None),
        live=unknown_live, execution_policy=None, same_sip_policy=SAME_SIP_CONSERVATIVE_V1)
    item = (FirstBarReplayInput(episode, CompletedFlipBar('CONTROLLED', start_ns,
            start_ns + 60_000_000_000, Decimal('2.00'), Decimal('1.99'), Decimal('1.98'),
            'long', True, source), t.Boundary(end_ns, source)) if family == 'FIRSTBAR' else episode)
    data = dict(symbols=['CONTROLLED'], trading_day='2026-10-28', source_asof=asof,
        snapshot_id='CONTROLLED_RR', start=LEFT, end=asof,
        rows=dict(bars=[dict(id='CONTROLLED_BAR', bar_time=LEFT)], capture_bars=[], scanner=[], fills=[]))
    model = controller.IncrementalModels(run)
    model.require(dict(source_pins=pins), null_policy=None, same_sip_policy=SAME_SIP_CONSERVATIVE_V1,
                  execution_rules_sha256='b' * 64, policy_source=source)
    lines = [dict(derived_available_ns=start_ns + 60_000_000_000, bar_id='CONTROLLED_BAR')]
    return cache, run, model, episode, item, data, lines


@pytest.mark.parametrize('family', ['FIRSTBAR', 'CORE'])
@pytest.mark.parametrize('watched', [True, False, None], ids=['watched', 'not-watched', 'unknown-watch'])
def test_cold_current_RR_real_episode_uses_actual_watch_field(monkeypatch, tmp_path, family, watched):
    _, run, model, episode, item, data, lines = context(monkeypatch, tmp_path, family, watched)
    from project_mai_tai.atr_simulation.same_sip_types import EpisodeInput
    assert type(episode) is EpisodeInput
    assert episode.watched is watched
    assert not hasattr(episode, 'watched_at_flip')
    assert run.previous == {}  # Cold path still executes production _proof.
    result = model.arguments(family, data, (item,), (ORIGIN,), None, lines)
    assert result == {}  # No fabricated cache hit, engine execution or outcome.
    proof = model.contexts['CONTROLLED', family]
    assert proof['closed'] is True
    assert proof['guards_known'] is (watched is True)
    if watched is True:
        assert len(run.audit) == (2 if family == 'FIRSTBAR' else 3)
        assert all(row['reason'] == 'DEPENDENCY_CHANGED_OR_MISSING' for row in run.audit)
    else:
        assert run.audit == [dict(symbol='CONTROLLED', family=family,
                                  action='FRESH_WORLD', reason='OPEN_OR_UNKNOWN_INPUT')]
    assert run.next['worlds'] == {}


@pytest.mark.parametrize('family', ['FIRSTBAR', 'CORE'])
@pytest.mark.parametrize('fault', ['origin', 'absent-certificate', 'stale-RR'])
def test_current_RR_context_keeps_exact_dependency_refusals(monkeypatch, tmp_path, family, fault):
    cache, run, model, _, item, data, lines = context(monkeypatch, tmp_path, family, True)
    origins = (ORIGIN,)
    if fault == 'origin':
        origins = ('RECOMPUTED_BUY:FOREIGN_BAR',)
        expected = 'one_actual_model_window_origin_required'
    elif fault == 'absent-certificate':
        del run.inputs['CONTROLLED'][0]['certificates']['quotes']
        expected = 'complete_actual_window_dependencies_required'
    else:
        run.inputs['CONTROLLED'][0]['certificates']['quotes']['snapshot_id'] = 'CONTROLLED_OLD_RR'
        expected = 'cohort_current_RR_certificate_required'
    with pytest.raises(cache.CacheRefusal, match=expected):
        model.arguments(family, data, (item,), origins, None, lines)
    assert ('CONTROLLED', family) not in model.contexts
    assert run.next['worlds'] == {}


@pytest.mark.parametrize('family', ['FIRSTBAR', 'CORE'])
def test_current_RR_open_input_stays_fresh_after_watch_schema_fix(monkeypatch, tmp_path, family):
    _, run, model, _, item, data, lines = context(monkeypatch, tmp_path, family, True)
    run.next['windows']['CONTROLLED'][ORIGIN]['end_key'] = None
    assert model.arguments(family, data, (item,), (ORIGIN,), None, lines) == {}
    proof = model.contexts['CONTROLLED', family]
    assert proof['guards_known'] is True
    assert proof['closed'] is False
    assert run.audit == [dict(symbol='CONTROLLED', family=family,
                              action='FRESH_WORLD', reason='OPEN_OR_UNKNOWN_INPUT')]
