"""CONTROLLED plans and real pure-kernel differentials; author UNRUN.

No actual market replay, provider, SQL or native/economic qualification.
"""
from copy import deepcopy
from dataclasses import asdict, replace
from datetime import timedelta
import importlib
from pathlib import Path

import pytest


def setup(monkeypatch, tmp_path):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2] / 'tools'))
    module = importlib.import_module('atrsimlive1_reuse_components')
    cache = importlib.import_module('atrsimlive1_incremental_cache')
    reads = importlib.import_module('atrsimlive1_incremental_reads')
    from tests.unit.test_atrsimlive1_current_rr_model_context import context
    from project_mai_tai.atr_simulation import same_sip_types as t
    _, run, _, first, _, data, _ = context(monkeypatch, tmp_path, 'CORE', True)
    base = first.tape_coverage.start_ns
    first = replace(first, tape_coverage=replace(first.tape_coverage, end_ns=base + 180_000_000_000),
                    flip_coverage=replace(first.flip_coverage, end_ns=base + 180_000_000_000))
    second = replace(first, identity=replace(first.identity, flip_id='CONTROLLED_OPEN', segment_id='CONTROLLED_OPEN'),
        buy_flip=t.Boundary(base + 660_000_000_000, first.buy_flip.source, base + 600_000_000_000),
        sell_flip=None, session_end=t.Boundary(base + 1200_000_000_000, first.buy_flip.source),
        trades=(), quotes=(), tape_coverage=replace(first.tape_coverage,
            start_ns=base + 600_000_000_000, end_ns=base + 1200_000_000_000),
        flip_coverage=replace(first.flip_coverage, start_ns=base + 600_000_000_000,
                             end_ns=base + 1200_000_000_000))
    episodes = (first, second)
    ids = tuple(module.identity_key(episode) for episode in episodes)
    policies = run.binding['policies']
    headers = {name: cache.fingerprint({'CONTROLLED_HEADER': name}) for name in
               ('bars', 'capture_bars', 'scanner', 'fills', 'route', 'coverage', 'oracle', 'membership')}
    frame = dict(schema=module.COMPONENT_V1, world='TODAY:SUPPLIED', source_seal=run.binding['source_seal'],
        policies=policies, whole_membership_sha256=cache.fingerprint(ids), component_members=(ids[0],),
        global_tie_context_sha256=module.tie_context(episodes), input_closed=True,
        certificates=deepcopy(run.inputs['CONTROLLED'][0]['certificates']), header_hashes=headers,
        dependencies={'CONTROLLED': 'complete original closed window, not cropped at the exit',
                      'component_tie_context_sha256': module.tie_context((first,))})
    def rebind(frame, asof=data['source_asof'], snapshot=data['snapshot_id']):
        bound = reads.proof_bindings([asdict(first.identity)], windows=[dict(identity=asdict(first.identity),
            certificates=frame['certificates'], header_hashes=frame['header_hashes'])],
            cohort_dependencies=frame['dependencies'], policies=frame['policies'], source_seal=frame['source_seal'],
            current_asof=asof, current_snapshot=snapshot)
        frame['history_binding'] = bound['bindings'][0]
        frame['current_verification'] = bound['current_verification']
    rebind(frame)
    # Explicit literal CONTROLLED facts, NOT claimed as an AXNX actual fill.
    receipt = dict(issues=[], fills=[dict(quantity=100, at={'event_ns': base + 61_000_000_000})],
                   exits=[dict(quantity=100, reason='TARGET_5', at={'event_ns': base + 120_000_000_000})],
                   gross_pnl='10.00')
    row = dict(pnl_label_known=True, pnl_pct_label_known=True)
    candidate = dict(identity=asdict(first.identity), receipt=receipt, row=row,
        receipt_sha256=cache.fingerprint(receipt), row_sha256=cache.fingerprint(row),
        original_provenance={'CONTROLLED_original_asof': 'not retagged'}, original_world_key='d' * 64,
        proof_key=module.candidate_proof_key(frame))
    options = dict(world='TODAY:SUPPLIED', source_seal=run.binding['source_seal'], policies=policies,
        global_tie_context_sha256=module.tie_context(episodes), current_asof=data['source_asof'],
        current_snapshot=data['snapshot_id'])
    return module, cache, episodes, {ids[0]: candidate}, {ids[0]: frame}, options, rebind


def test_component_OFF_keeps_original_no_admission_contract(monkeypatch, tmp_path):
    module, _, episodes, candidates, proofs, options, _ = setup(monkeypatch, tmp_path)
    monkeypatch.setattr(module, 'components', lambda _: pytest.fail('OFF new partition work'))
    assert module.plan(episodes, candidates, proofs, **options) == {'enabled': False, 'retained': (), 'fresh': (0, 1)}


def test_known_closed_component_can_be_planned_with_open_world_member(monkeypatch, tmp_path):
    module, _, episodes, candidates, proofs, options, _ = setup(monkeypatch, tmp_path)
    assert episodes[0].tape_coverage.end_ns < next(iter(proofs.values()))['certificates']['trades']['end'].timestamp() * 10**9
    plan = module.plan(episodes, candidates, proofs, **options, enabled=True)
    assert tuple(entry['index'] for entry in plan['retained']) == (0,)
    assert plan['fresh'] == (1,)
    assert plan['required_kernel_contract'] == module.KERNEL_CONTRACT
    assert plan['retained'][0]['candidate']['original_provenance'] == {'CONTROLLED_original_asof': 'not retagged'}


@pytest.mark.parametrize('fault', ['unknown', 'open-exit', 'uncovered', 'watch-UNKNOWN'])
def test_unknown_open_or_uncovered_component_is_never_planned_reused(monkeypatch, tmp_path, fault):
    module, cache, episodes, candidates, proofs, options, _ = setup(monkeypatch, tmp_path)
    entry = next(iter(candidates.values()))
    if fault == 'unknown':
        entry['receipt']['issues'] = ['CONTROLLED_UNKNOWN']
        entry['receipt_sha256'] = cache.fingerprint(entry['receipt'])
    elif fault == 'open-exit':
        entry['receipt']['exits'][0]['reason'] = 'OPEN'
        entry['receipt_sha256'] = cache.fingerprint(entry['receipt'])
    elif fault == 'uncovered':
        end = episodes[0].tape_coverage.start_ns + 90_000_000_000
        episodes = (replace(episodes[0], tape_coverage=replace(episodes[0].tape_coverage, end_ns=end)), episodes[1])
    else:
        episodes = (replace(episodes[0], watched=None), episodes[1])
    result = module.plan(episodes, candidates, proofs, **options, enabled=True)
    assert result['retained'] == ()
    assert set(result['fresh']) == {0, 1}


def test_overlapping_unknown_member_joins_dependency_component(monkeypatch, tmp_path):
    module, _, episodes, candidates, proofs, options, _ = setup(monkeypatch, tmp_path)
    second = replace(episodes[1], buy_flip=episodes[0].buy_flip)
    episodes = (episodes[0], second)
    assert module.components(episodes) == ((0, 1),)
    result = module.plan(episodes, candidates, proofs, **options, enabled=True)
    assert result['retained'] == ()
    assert result['fresh'] == (0, 1)


def test_post_SELL_supplied_tail_still_connects_another_order_on_the_global_tape(monkeypatch, tmp_path):
    module, _, episodes, _, _, _, _ = setup(monkeypatch, tmp_path)
    first, later = episodes
    tail = replace(first.trades[0], key=replace(first.trades[0].key,
        raw_id='CONTROLLED:post-sell-tail', event_ns=later.buy_flip.available_ns + 1))
    first = replace(first, trades=(*first.trades, tail))
    assert module.components((first, later)) == ((0, 1),)


def test_backfilled_full_history_invalidates_candidate_even_after_exit(monkeypatch, tmp_path):
    module, _, episodes, candidates, proofs, options, rebind = setup(monkeypatch, tmp_path)
    frame = next(iter(proofs.values()))
    frame['certificates']['trades']['content_sha256'] = 'f' * 64
    rebind(frame)
    result = module.plan(episodes, candidates, proofs, **options, enabled=True)
    assert result['retained'] == ()


def test_new_RR_witness_is_separate_from_unchanged_history_key(monkeypatch, tmp_path):
    module, _, episodes, candidates, proofs, options, rebind = setup(monkeypatch, tmp_path)
    frame = next(iter(proofs.values()))
    old_key = module.candidate_proof_key(frame)
    options['current_snapshot'] = 'CONTROLLED_NEXT_RR'
    options['current_asof'] += timedelta(minutes=1)
    for cert in frame['certificates'].values():
        cert['snapshot_id'] = options['current_snapshot']
        cert['asof'] = options['current_asof']
    rebind(frame, options['current_asof'], options['current_snapshot'])
    assert module.candidate_proof_key(frame) == old_key
    result = module.plan(episodes, candidates, proofs, **options, enabled=True)
    assert tuple(entry['index'] for entry in result['retained']) == (0,)
    assert result['retained'][0]['current_verification']['snapshot_id'] == 'CONTROLLED_NEXT_RR'


def test_stale_history_certificate_is_not_admitted(monkeypatch, tmp_path):
    module, cache, episodes, candidates, proofs, options, _ = setup(monkeypatch, tmp_path)
    next(iter(proofs.values()))['certificates']['quotes']['snapshot_id'] = 'FOREIGN'
    with pytest.raises(cache.CacheRefusal, match='cohort_current_RR_certificate_required'):
        module.plan(episodes, candidates, proofs, **options, enabled=True)


def test_executable_firstbar_seam_equals_full_engine_with_real_tie_context(monkeypatch, tmp_path):
    """Actual pure kernels on CONTROLLED data; never a provider/model replay."""
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2] / 'tools'))
    module = importlib.import_module('atrsimlive1_reuse_components')
    cache = importlib.import_module('atrsimlive1_incremental_cache')
    reads = importlib.import_module('atrsimlive1_incremental_reads')
    codec = importlib.import_module('atrsimlive1_incremental_codec')
    from datetime import datetime, timezone
    from dataclasses import fields
    from tests.support.atr_same_sip_cases import replay, trade
    from project_mai_tai.atr_simulation import same_sip_firstbar_replay as kernel
    from project_mai_tai.atr_simulation import same_sip_types as t
    from project_mai_tai.atr_simulation.same_sip import SAME_SIP_CONSERVATIVE_V1, report_key

    left = datetime(2026, 10, 28, 8, tzinfo=timezone.utc)
    base = int(left.timestamp()) * 10**9
    def shifted(value):
        e = value.episode
        def boundary(b):
            return None if b is None else replace(b, available_ns=base + b.available_ns,
                bar_start_ns=None if b.bar_start_ns is None else base + b.bar_start_ns)
        args = {f.name: getattr(e, f.name) for f in fields(e)}
        args.update(identity=replace(e.identity, trading_day='2026-10-28'),
            buy_flip=boundary(e.buy_flip), sell_flip=boundary(e.sell_flip), session_end=boundary(e.session_end),
            trades=tuple(replace(row, key=replace(row.key, event_ns=base + row.key.event_ns)) for row in e.trades),
            tape_coverage=replace(e.tape_coverage, start_ns=base, end_ns=base + e.tape_coverage.end_ns),
            flip_coverage=replace(e.flip_coverage, start_ns=base, end_ns=base + e.flip_coverage.end_ns))
        current = t.EpisodeInput(**args, same_sip_policy=SAME_SIP_CONSERVATIVE_V1)
        return kernel.FirstBarReplayInput(current, replace(value.flip_bar, start_ns=base + value.flip_bar.start_ns,
            available_ns=base + value.flip_bar.available_ns), boundary(value.entry_cutoff))
    first = shifted(replay((trade(61, '2.03'), trade(62, '2.25'), trade(240, '2.25')), account='one'))
    later = shifted(replay((trade(700, '3.00', seq=10), trade(700, '3.20', seq=11)), account='two'))
    later = replace(later, episode=replace(later.episode, watched=None, watched_source=None,
        identity=replace(later.episode.identity, flip_id='LATER_OPEN', segment_id='LATER_OPEN'),
        buy_flip=t.Boundary(base + 660 * 10**9, later.flip_bar.source, base + 600 * 10**9),
        sell_flip=None, session_end=t.Boundary(base + 900 * 10**9, later.flip_bar.source),
        tape_coverage=replace(later.episode.tape_coverage, start_ns=base + 600 * 10**9,
                              end_ns=base + 900 * 10**9, complete=None)),
        flip_bar=replace(later.flip_bar, start_ns=base + 600 * 10**9,
                         available_ns=base + 660 * 10**9, complete=None),
        entry_cutoff=t.Boundary(base + 840 * 10**9, later.flip_bar.source))
    items = (first, later)
    episodes = tuple(item.episode for item in items)
    original = kernel.FirstBarReplayEngine()
    baseline = original.simulate_many(items, fill_policy=kernel.FirstBarFillPolicy.OBSERVED_PRINT_VOLUME)
    known = next(result for result in baseline if result.identity == first.episode.identity and result.mode.value == 'FIRSTBAR')
    assert known.gross_pnl is not None and not known.issues
    assert sum(fill.quantity for fill in known.fills) == 100
    assert len(known.exits) == 1 and known.exits[0].quantity == 100
    assert any(result.issues for result in baseline if result.identity == later.episode.identity)
    source_seal = 'a' * 64
    policies = dict(execution_rules_sha256='b' * 64, null_policy=None, same_sip_policy=SAME_SIP_CONSERVATIVE_V1)
    asof = left + timedelta(minutes=20)
    certs = {kind: reads.certify(dict(row_count=3, content_sha256='c' * 64, source_timezone='UTC'),
        kind=kind, symbol='X', start=left, end=left + timedelta(minutes=5), day_start=left,
        asof=asof, snapshot_id='CONTROLLED_RR', query_sha256=reads.query_pair(kind)['query_sha256'],
        read_only='on', isolation='repeatable read', complete=True) for kind in ('trades', 'quotes')}
    headers = {name: 'd' * 64 for name in ('bars', 'capture_bars', 'scanner', 'fills', 'route', 'coverage', 'oracle', 'membership')}
    dependencies = {'CONTROLLED': 'complete closed original history',
                    'component_tie_context_sha256': module.tie_context((first.episode,))}
    bound = reads.proof_bindings([asdict(first.episode.identity)], windows=[dict(identity=asdict(first.episode.identity),
        certificates=certs, header_hashes=headers)], cohort_dependencies=dependencies, policies=policies,
        source_seal=source_seal, current_asof=asof, current_snapshot='CONTROLLED_RR')
    key = module.identity_key(first.episode)
    frame = dict(schema=module.COMPONENT_V1, world='FIRSTBAR', source_seal=source_seal, policies=policies,
        whole_membership_sha256=cache.fingerprint(tuple(module.identity_key(e) for e in episodes)),
        component_members=(key,), global_tie_context_sha256=module.tie_context(episodes),
        input_closed=True, certificates=certs, header_hashes=headers, dependencies=dependencies,
        history_binding=bound['bindings'][0], current_verification=bound['current_verification'])
    receipt = asdict(known)
    row = {'pnl_label_known': True, 'pnl_pct_label_known': True}
    report = {report_key(first.episode.identity, 'FIRSTBAR'): original.same_sip_reports[report_key(first.episode.identity, 'FIRSTBAR')]}
    candidate = dict(identity=asdict(first.episode.identity), receipt=receipt, row=row,
        receipt_sha256=cache.fingerprint(receipt), row_sha256=cache.fingerprint(row),
        original_provenance={'CONTROLLED': 'original full-cohort receipt'}, original_world_key='e' * 64,
        proof_key=module.candidate_proof_key(frame), typed_result=codec.encode_model(known), typed_reports=codec.encode_reports(report))
    calls, original_kernel_call = [], kernel.FirstBarReplayEngine.simulate_many
    def observed(self, values, **kwargs):
        calls.append((len(values), tuple(sorted(kwargs.get('retained_modes', {})))))
        return original_kernel_call(self, values, **kwargs)
    monkeypatch.setattr(kernel.FirstBarReplayEngine, 'simulate_many', observed)
    results, reports, audit = module.execute('FIRSTBAR', items,
        {'FIRSTBAR': {key: candidate}, 'FIRSTBAR_X3': {}}, {'FIRSTBAR': {key: frame}, 'FIRSTBAR_X3': {}},
        source_seal=source_seal, policies=policies, global_tie_context_sha256=module.tie_context(episodes),
        current_asof=asof, current_snapshot='CONTROLLED_RR',
        fill_policy=kernel.FirstBarFillPolicy.OBSERVED_PRINT_VOLUME, enabled=True)
    assert results == baseline
    assert reports == original.same_sip_reports
    assert audit[0]['reused_episode_indices'] == (0,)
    assert calls[-1] == (2, ('FIRSTBAR', 'FIRSTBAR_X3'))
    assert (1, ('FIRSTBAR',)) in calls
    assert all(count != 2 or modes for count, modes in calls)

    # Connected producer/controller -> cache writer -> new reader/controller,
    # not an injected candidate plan or mocked successful kernel result.
    controller = importlib.import_module('atrsimlive1_incremental_models')
    coordinator = importlib.import_module('atrsimlive1_incremental_collect')
    pins = {'CONTROLLED:producer': 'f' * 64}
    binding = dict(version=cache.INCREMENTAL_V1, trading_day='2026-10-28',
        source_seal=cache.fingerprint(pins), policies=policies, input_scope='CURRENT_RR',
        features=dict(version=cache.FEATURE_PROFILE, closed_components=True, raw_pool=False, since_last_run=False))
    root = (tmp_path / 'connected-component-cache').resolve()
    root.mkdir(mode=0o700)
    store = cache.CacheStore(root, binding)
    origins = ('CONTROLLED_FIRST', 'CONTROLLED_LATER_OPEN')
    data = dict(symbols=['X'], trading_day='2026-10-28', source_asof=asof,
        snapshot_id='CONTROLLED_RR', start=left, end=asof, source_hashes={'trades': 'f' * 64},
        rows={name: [] for name in ('bars', 'capture_bars', 'scanner', 'fills')})

    def current_run(snapshot):
        run = coordinator.IncrementalRun(store)
        run.bind_snapshot(dict(read_only='on', isolation='repeatable read', snapshot_id=snapshot),
            dict(day=left.date(), start=left, end=asof, asof=asof), ('X',))
        run.next['windows']['X'], run.inputs['X'] = {}, []
        for index, origin in enumerate(origins):
            current_certs = deepcopy(certs)
            for certificate in current_certs.values():
                certificate['snapshot_id'] = snapshot
                certificate['asof'] = asof
                if index:
                    certificate['start'] = left + timedelta(minutes=10)
                    certificate['end'] = asof
                    if snapshot == 'CONTROLLED_GROWN_RR':
                        certificate['row_count'] = 5
                        certificate['content_sha256'] = '7' * 64
            # CONTROLLED SQL-certificate port inputs; no actual DB claim.
            run.inputs['X'].append(dict(origin=origin, certificates=current_certs))
            run.next['windows']['X'][origin] = dict(end_key=(base + 240 * 10**9, 3) if not index else None,
                **{kind: dict(rows=[], hashes={}, certificate=current_certs[kind]) for kind in current_certs})
        actual = controller.IncrementalModels(run)
        actual.require(dict(source_pins=pins), null_policy=None, same_sip_policy=SAME_SIP_CONSERVATIVE_V1,
            execution_rules_sha256='b' * 64, policy_source=first.episode.execution_policy.source)
        return run, actual

    cold, actual = current_run('CONTROLLED_RR')
    cold_results, cold_reports = actual.execute('FIRSTBAR', data, items, origins, None, [],
        fill_policy=kernel.FirstBarFillPolicy.OBSERVED_PRINT_VOLUME)
    assert cold_results == baseline and cold_reports == original.same_sip_reports
    document = dict(tables={world: {'rows': []} for world in ('FIRSTBAR', 'FIRSTBAR_X3')},
                    producer={'engine_receipts': {}})
    for number, result in enumerate(cold_results):
        outcome_id = 'CONTROLLED:' + str(number)
        known_row = result.gross_pnl is not None and not result.issues
        document['tables'][result.mode.value]['rows'].append(dict(identity=asdict(result.identity),
            outcome_id=outcome_id, row={'pnl_label_known': known_row, 'pnl_pct_label_known': known_row}))
        document['producer']['engine_receipts'][outcome_id] = asdict(result)
    actual.record('FIRSTBAR', data, cold_results, cold_reports, document)
    assert key in cold.next['worlds']['X|FIRSTBAR']['component_candidates']
    assert module.identity_key(later.episode) not in cold.next['worlds']['X|FIRSTBAR']['component_candidates']
    original_provenance = deepcopy(cold.next['worlds']['X|FIRSTBAR']['component_candidates'][key]['original_provenance'])
    store.save(cold.next)
    warm, actual = current_run('CONTROLLED_NEXT_RR')
    warm_data = dict(data, snapshot_id='CONTROLLED_NEXT_RR')
    warm_results, warm_reports = actual.execute('FIRSTBAR', warm_data, items, origins, None, [],
        fill_policy=kernel.FirstBarFillPolicy.OBSERVED_PRINT_VOLUME)
    assert warm_results == baseline and warm_reports == original.same_sip_reports
    assert any(row.get('reused_episode_indices') == (0,) for row in warm.audit)
    actual.record('FIRSTBAR', warm_data, warm_results, warm_reports, document)
    retained = warm.next['worlds']['X|FIRSTBAR']['component_candidates'][key]
    assert retained['original_provenance'] == original_provenance
    assert retained['last_verification']['snapshot_id'] == 'CONTROLLED_NEXT_RR'
    store.save(warm.next)

    # Not merely a new snapshot: a LATER OPEN component gains bars, raw rows,
    # a second tie group and a larger original working certificate. The first
    # component's FULL original certificate remains unchanged, not exit-cut.
    asof += timedelta(minutes=1)
    extra = tuple(replace(trade(710, price, seq=sequence), key=replace(trade(710, price, seq=sequence).key,
        event_ns=base + 710 * 10**9)) for sequence, price in ((20, '3.30'), (21, '3.40')))
    grown_later = replace(later, episode=replace(later.episode, trades=(*later.episode.trades, *extra)))
    grown_items = (first, grown_later)
    grown_data = dict(data, snapshot_id='CONTROLLED_GROWN_RR', source_asof=asof, end=asof,
        source_hashes={'trades': '8' * 64}, rows=dict(data['rows'],
            bars=[dict(id='CONTROLLED_LATER_NEW_BAR', bar_time=left + timedelta(minutes=12))]))
    grown_lines = [dict(derived_available_ns=base + 13 * 60 * 10**9, bar_id='CONTROLLED_LATER_NEW_BAR')]
    current, actual = current_run('CONTROLLED_GROWN_RR')
    fresh_original = kernel.FirstBarReplayEngine()
    expected = fresh_original.simulate_many(grown_items, fill_policy=kernel.FirstBarFillPolicy.OBSERVED_PRINT_VOLUME)
    grown_results, grown_reports = actual.execute('FIRSTBAR', grown_data, grown_items, origins, None, grown_lines,
        fill_policy=kernel.FirstBarFillPolicy.OBSERVED_PRINT_VOLUME)
    assert grown_results == expected
    assert grown_reports == fresh_original.same_sip_reports
    assert grown_reports[report_key(first.episode.identity, 'FIRSTBAR')]['same_timestamp_groups'] == 2
    assert any(row.get('reused_episode_indices') == (0,) for row in current.audit)
    frame_now = actual.component_contexts['X', 'FIRSTBAR']['frames']['FIRSTBAR'][key]
    assert frame_now['certificates']['trades']['end'] == left + timedelta(minutes=5)
    assert frame_now['certificates']['trades']['content_sha256'] == 'c' * 64
    assert frame_now['global_tie_context_sha256'] != retained['original_provenance']['full_tie_context_sha256']


def test_executable_CORE_seam_preserves_known_TODAY_and_fresh_UNKNOWN_worlds(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2] / 'tools'))
    module = importlib.import_module('atrsimlive1_reuse_components')
    cache = importlib.import_module('atrsimlive1_incremental_cache')
    reads = importlib.import_module('atrsimlive1_incremental_reads')
    codec = importlib.import_module('atrsimlive1_incremental_codec')
    from datetime import datetime, timezone
    from decimal import Decimal
    from dataclasses import fields
    from tests.support.atr_same_sip_cases import episode, trade
    from project_mai_tai.atr_simulation import same_sip_engine as kernel
    from project_mai_tai.atr_simulation import same_sip_types as t
    from project_mai_tai.atr_simulation.same_sip import SAME_SIP_CONSERVATIVE_V1, report_key
    from project_mai_tai.atr_simulation.read_model import result_rows

    left = datetime(2026, 10, 28, 8, tzinfo=timezone.utc)
    base = int(left.timestamp()) * 10**9
    raw = episode((trade(61, '2.01'), trade(62, '2.20'), trade(240, '2.20')))
    args = {field.name: getattr(raw, field.name) for field in fields(raw)}
    args.update(identity=replace(raw.identity, trading_day='2026-10-28'),
        buy_flip=replace(raw.buy_flip, available_ns=base + 60 * 10**9, bar_start_ns=base),
        sell_flip=replace(raw.sell_flip, available_ns=base + 240 * 10**9),
        session_end=replace(raw.session_end, available_ns=base + 300 * 10**9),
        trades=tuple(replace(row, key=replace(row.key, event_ns=base + row.key.event_ns)) for row in raw.trades),
        tape_coverage=replace(raw.tape_coverage, start_ns=base, end_ns=base + 300 * 10**9),
        flip_coverage=replace(raw.flip_coverage, start_ns=base, end_ns=base + 300 * 10**9),
        today_entry_coverage=replace(raw.today_entry_coverage, start_ns=base, end_ns=base + 300 * 10**9))
    first = t.EpisodeInput(**args, same_sip_policy=SAME_SIP_CONSERVATIVE_V1)
    first = replace(first, today_entries=(t.EntryFact('CONTROLLED:supplied-entry', first.trades[0].key,
        Decimal('2.01'), 100, first.trades[0].key.source),))
    later = replace(first, identity=replace(first.identity, account_id='two', flip_id='LATER', segment_id='LATER'),
        watched=None, watched_source=None, today_entries=(),
        buy_flip=t.Boundary(base + 660 * 10**9, raw.buy_flip.source, base + 600 * 10**9), sell_flip=None,
        session_end=t.Boundary(base + 900 * 10**9, raw.buy_flip.source),
        trades=(replace(trade(700, '3.00'), key=replace(trade(700, '3.00').key, event_ns=base + 700 * 10**9)),),
        tape_coverage=replace(first.tape_coverage, start_ns=base + 600 * 10**9, end_ns=base + 900 * 10**9, complete=None))
    episodes = (first, later)
    original = kernel.SimulationEngine()
    baseline = original.simulate_many(episodes)
    assert baseline[0].today.gross_pnl == Decimal('10.0500')
    assert baseline[0].today.entry_facts[0].quantity == 100
    assert baseline[0].today.exits[0].at.event_ns == base + 62 * 10**9
    assert baseline[1].today.issues and baseline[1].today.gross_pnl is None
    asof = left + timedelta(minutes=20)
    policies = dict(execution_rules_sha256='b' * 64, null_policy=None, same_sip_policy=SAME_SIP_CONSERVATIVE_V1)
    certs = {kind: reads.certify(dict(row_count=3, content_sha256='c' * 64, source_timezone='UTC'),
        kind=kind, symbol='X', start=left, end=left + timedelta(minutes=5), day_start=left,
        asof=asof, snapshot_id='CONTROLLED_RR', query_sha256=reads.query_pair(kind)['query_sha256'],
        read_only='on', isolation='repeatable read', complete=True) for kind in ('trades', 'quotes')}
    headers = {name: 'd' * 64 for name in ('bars', 'capture_bars', 'scanner', 'fills', 'route', 'coverage', 'oracle', 'membership')}
    dependencies = {'CONTROLLED': 'full original history',
                    'component_tie_context_sha256': module.tie_context((first,))}
    bound = reads.proof_bindings([asdict(first.identity)], windows=[dict(identity=asdict(first.identity),
        certificates=certs, header_hashes=headers)], cohort_dependencies=dependencies, policies=policies,
        source_seal='a' * 64, current_asof=asof, current_snapshot='CONTROLLED_RR')
    key = module.identity_key(first)
    frame = dict(schema=module.COMPONENT_V1, world='TODAY:SUPPLIED', source_seal='a' * 64, policies=policies,
        whole_membership_sha256=cache.fingerprint(tuple(module.identity_key(e) for e in episodes)),
        component_members=(key,), global_tie_context_sha256=module.tie_context(episodes), input_closed=True,
        certificates=certs, header_hashes=headers, dependencies=dependencies,
        history_binding=bound['bindings'][0], current_verification=bound['current_verification'])
    receipt = next(row for row in result_rows(baseline[0]) if row['mode'] == 'TODAY')
    row = {'pnl_label_known': True, 'pnl_pct_label_known': True}
    rk = report_key(first.identity, 'TODAY:SUPPLIED')
    entry = dict(identity=asdict(first.identity), proof_key=module.candidate_proof_key(frame), receipt=receipt, row=row,
        receipt_sha256=cache.fingerprint(receipt), row_sha256=cache.fingerprint(row),
        original_provenance={'CONTROLLED': 'original source'}, original_world_key='e' * 64,
        typed_result=codec.encode_model(baseline[0]), typed_reports=codec.encode_reports({rk: original.same_sip_reports[rk]}))
    candidates = {world: {} for world in module.WORLDS[2:]}
    proofs = {world: {} for world in module.WORLDS[2:]}
    candidates['TODAY:SUPPLIED'][key], proofs['TODAY:SUPPLIED'][key] = entry, frame
    calls, actual = [], kernel.SimulationEngine.simulate_many
    def observed(self, values, **options):
        calls.append((len(values), tuple(sorted(options.get('retained_worlds', {})))))
        return actual(self, values, **options)
    monkeypatch.setattr(kernel.SimulationEngine, 'simulate_many', observed)
    results, reports, audit = module.execute('CORE', episodes, candidates, proofs, source_seal='a' * 64,
        policies=policies, global_tie_context_sha256=module.tie_context(episodes),
        current_asof=asof, current_snapshot='CONTROLLED_RR', enabled=True)
    assert results == baseline and reports == original.same_sip_reports
    assert audit[0]['reused_episode_indices'] == (0,) and audit[0]['fresh_episode_indices'] == (1,)
    assert calls[-1] == (2, tuple(sorted(module.WORLDS[2:])))
    assert (1, ('TODAY:SUPPLIED',)) in calls
