"""CONTROLLED cache/merge/admission cases; Main alone executes this source."""
from copy import deepcopy
from dataclasses import fields, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import importlib
import hashlib
import json
from pathlib import Path
from uuid import UUID

import pytest


LEFT = datetime(2026, 10, 28, 8, tzinfo=timezone.utc)


def modules(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2] / 'tools'))
    return tuple(importlib.import_module('atrsimlive1_incremental_' + name)
                 for name in ('cache', 'reads', 'codec', 'collect'))


def binding(cache):
    return dict(version=cache.INCREMENTAL_V1, trading_day='2026-10-28', source_seal='a' * 64, input_scope='CURRENT_RR',
        policies=dict(execution_rules_sha256='b' * 64, null_policy=None, same_sip_policy=None))


def root(tmp_path):
    value = (tmp_path / 'private-cache').resolve()
    value.mkdir(mode=0o700)
    return value


def test_private_cache_atomic_readback_preserves_native_scalars(monkeypatch, tmp_path):
    cache, *_ = modules(monkeypatch)
    value = dict(when=LEFT, price=Decimal('1.5300'), id=UUID('11111111-1111-4111-8111-111111111111'),
                 rows=[dict(conditions=None, size=100)], native_complete=None)
    directory = root(tmp_path)
    with cache.locked_cache(directory, binding(cache)) as store:
        assert store.load() is None
        receipt = store.save(value)
        assert receipt['sha256'] == receipt['readback_sha256']
        recovered = store.load()
        assert recovered == value and str(recovered['price']) == '1.5300'
        assert cache.fingerprint(recovered) == cache.fingerprint(value)
    assert (directory / 'cache.json').stat().st_mode & 0o777 == 0o600
    assert not (directory / '.next.json').exists()


def test_atomic_cap_failure_preserves_previous_cache_and_cleans_only_own_temp(monkeypatch, tmp_path):
    cache, *_ = modules(monkeypatch)
    directory = root(tmp_path)
    store = cache.CacheStore(directory, binding(cache))
    store.save({'first': 1})
    original = (directory / 'cache.json').read_bytes()
    monkeypatch.setattr(cache, 'MAX_CACHE_BYTES', 10)
    with pytest.raises(cache.CacheRefusal, match='cache_file_byte_cap'):
        store.save({'second': 'x' * 100})
    assert (directory / 'cache.json').read_bytes() == original
    assert not (directory / '.next.json').exists()


def test_stale_fixed_stage_refuses_without_deleting_or_overwriting_it(monkeypatch, tmp_path):
    cache, *_ = modules(monkeypatch)
    directory = root(tmp_path)
    stage = directory / '.next.json'
    stage.write_bytes(b'CONTROLLED killed writer')
    stage.chmod(0o600)
    with pytest.raises(FileExistsError):
        cache.CacheStore(directory, binding(cache)).save({'next': 1})
    assert stage.read_bytes() == b'CONTROLLED killed writer'


@pytest.mark.parametrize('mutation', ['source', 'day', 'policy', 'body', 'duplicate', 'mode', 'symlink'])
def test_cache_source_integrity_and_private_file_guards_refuse(monkeypatch, tmp_path, mutation):
    cache, *_ = modules(monkeypatch)
    directory = root(tmp_path)
    expected = binding(cache)
    store = cache.CacheStore(directory, expected)
    store.save({'number': 1})
    path = directory / 'cache.json'
    if mutation in ('source', 'day', 'policy'):
        changed = deepcopy(expected)
        if mutation == 'source':
            changed['source_seal'] = 'c' * 64
        elif mutation == 'day':
            changed['trading_day'] = '2026-10-29'
        else:
            changed['policies']['null_policy'] = 'DIFFERENT'
        store = cache.CacheStore(directory, changed)
    elif mutation == 'body':
        raw = path.read_bytes()
        assert b'"number":1' in raw
        path.write_bytes(raw.replace(b'"number":1', b'"number":2', 1))
    elif mutation == 'duplicate':
        path.write_bytes(b'{"schema":1,"schema":2}')
    elif mutation == 'mode':
        path.chmod(0o644)
    else:
        target = directory / 'foreign.json'
        path.rename(target)
        path.symlink_to(target)
    with pytest.raises((cache.CacheRefusal, OSError)):
        store.load()


def identity():
    return dict(trading_day='2026-10-28', symbol='CONTROLLED', broker='webull', account_id='SIM_ROUTE:webull',
        strategy='schwab_1m_v2', flip_id='BUY:one', segment_id='SEGMENT:one')


@pytest.mark.parametrize('change', ['account', 'segment', 'cohort', 'window', 'policy', 'source'])
def test_episode_key_invalidates_actual_identity_cohort_input_policy_and_source(monkeypatch, change):
    cache, *_ = modules(monkeypatch)
    args = dict(identity=identity(), world='VOLBODY:OPTIMISTIC', window_sha256='c' * 64,
        cohort_sha256='d' * 64, policies=binding(cache)['policies'], source_seal='a' * 64)
    old = cache.cache_key(**args)
    changed = deepcopy(args)
    if change == 'account': changed['identity']['account_id'] = 'OTHER'
    elif change == 'segment': changed['identity']['segment_id'] = 'OTHER'
    elif change == 'cohort': changed['cohort_sha256'] = 'e' * 64
    elif change == 'window': changed['window_sha256'] = 'e' * 64
    elif change == 'policy': changed['policies']['null_policy'] = 'OTHER'
    else: changed['source_seal'] = 'e' * 64
    assert cache.cache_key(**changed) != old


@pytest.mark.parametrize('case', ['issue', 'open', 'zero', 'quantity', 'unknown_usd'])
def test_unknown_open_or_mixed_outcome_never_becomes_reusable(monkeypatch, case):
    cache, *_ = modules(monkeypatch)
    receipt = dict(issues=[], fills=[dict(quantity=100)], exits=[dict(quantity=100, reason='TARGET_5')], gross_pnl='5')
    row = dict(pnl_label_known=True, pnl_pct_label_known=True)
    assert cache.finished_known(receipt, row)
    if case == 'issue': receipt['issues'] = ['UNKNOWN_CONDITIONS_COLUMN_MISSING']
    elif case == 'open': receipt['exits'][0]['reason'] = 'OPEN'
    elif case == 'zero': receipt['fills'] = []
    elif case == 'quantity': receipt['exits'][0]['quantity'] = 101
    else: row['pnl_label_known'] = False
    assert not cache.finished_known(receipt, row)


def cert(reads, *, end=None, asof=None, snapshot='CONTROLLED-RR', checksum='c' * 64, count=1):
    end = end or LEFT + timedelta(minutes=3)
    asof = asof or end
    return reads.certify(dict(row_count=count, content_sha256=checksum, source_timezone='UTC'),
        kind='trades', symbol='CONTROLLED', start=LEFT, end=end, day_start=LEFT,
        asof=asof, snapshot_id=snapshot, query_sha256=reads.query_pair('trades')['query_sha256'],
        read_only='on', isolation='repeatable read', complete=True)


def test_late_or_corrected_prefix_same_count_forces_fresh_not_time_only_hit(monkeypatch):
    _, reads, *_ = modules(monkeypatch)
    old = cert(reads)
    current = cert(reads, asof=LEFT + timedelta(minutes=5), snapshot='NEW-RR')
    exact = reads.read_plan(old, current, current_asof=current['asof'], current_snapshot='NEW-RR', open_window=True)
    assert exact['action'] == 'DELTA' and exact['start'] == old['end']
    corrected = dict(current, content_sha256='d' * 64)
    refused = reads.read_plan(old, corrected, current_asof=current['asof'], current_snapshot='NEW-RR', open_window=True)
    assert refused['action'] == 'FRESH_WINDOW'


@pytest.mark.parametrize('case', ['snapshot', 'asof', 'query', 'incomplete'])
def test_current_RR_certificate_cannot_be_replaced_by_stale_or_partial_metadata(monkeypatch, case):
    cache, reads, *_ = modules(monkeypatch)
    current = cert(reads)
    if case == 'snapshot': current['snapshot_id'] = 'FOREIGN'
    elif case == 'asof': current['asof'] += timedelta(seconds=1)
    elif case == 'query': current['schema'] = 'UNREVIEWED'
    else: current['complete'] = False
    with pytest.raises(cache.CacheRefusal):
        reads.read_plan(cert(reads), current, current_asof=LEFT + timedelta(minutes=3),
                        current_snapshot='CONTROLLED-RR', open_window=False)


def trade(at, ident, *, price='2', conditions=None):
    return dict(id=ident, provider='massive', symbol='CONTROLLED', event_ts=at,
        received_at=LEFT + timedelta(minutes=6), price=Decimal(price), size=100,
        exchange=1, conditions=conditions, cumulative_volume=None)


def test_delta_merge_keeps_entire_boundary_group_and_exact_raw_null(monkeypatch):
    cache, reads, *_ = modules(monkeypatch)
    prefix = [trade(LEFT + timedelta(minutes=1), 1)]
    delta = [trade(LEFT + timedelta(minutes=3), 2), trade(LEFT + timedelta(minutes=3), 3, price='1.9')]
    hashes = {(r['symbol'], r['id']): cache.fingerprint(r) for r in prefix + delta}
    checksum = reads.content_hash(prefix + delta, hashes)
    full = cert(reads, end=LEFT + timedelta(minutes=6), asof=LEFT + timedelta(minutes=6), checksum=checksum, count=3)
    merged, recovered = reads.merge_open(prefix_rows=prefix, prefix_hashes=hashes,
        delta_rows=delta, delta_hashes=hashes, cutoff=LEFT + timedelta(minutes=3),
        current_full_certificate=full, current_asof=full['asof'], current_snapshot=full['snapshot_id'])
    assert merged == prefix + delta and len({r['id'] for r in merged}) == 3
    assert all(r['conditions'] is None for r in merged) and recovered == hashes
    assert cache.fingerprint(merged) == cache.fingerprint(prefix + delta)


def test_conflicting_physical_row_never_double_counts_or_overwrites_prefix(monkeypatch):
    cache, reads, *_ = modules(monkeypatch)
    old = trade(LEFT + timedelta(minutes=1), 1)
    changed = trade(LEFT + timedelta(minutes=3), 1, price='99')
    full = cert(reads, end=LEFT + timedelta(minutes=6), asof=LEFT + timedelta(minutes=6), count=2)
    with pytest.raises(cache.CacheRefusal, match='conflicting_physical_capture_row'):
        reads.merge_open(prefix_rows=[old], prefix_hashes={('CONTROLLED', 1): cache.fingerprint(old)},
            delta_rows=[changed], delta_hashes={('CONTROLLED', 1): cache.fingerprint(changed)},
            cutoff=LEFT + timedelta(minutes=3), current_full_certificate=full,
            current_asof=full['asof'], current_snapshot=full['snapshot_id'])


def test_derived_SQL_row_hash_is_separate_from_unchanged_raw_payload(monkeypatch):
    cache, reads, *_ = modules(monkeypatch)
    original = trade(LEFT, 1)
    supplied = dict(original, incremental_row_sha256='a' * 64)
    rows, hashes = reads.rows_with_fingerprints([supplied], kind='trades')
    assert rows == [original] and rows[0]['conditions'] is None
    assert hashes == {('CONTROLLED', 1): 'a' * 64}
    assert cache.fingerprint(rows[0]) == cache.fingerprint(original)


def test_private_model_codec_rejects_unknown_class_and_missing_fields(monkeypatch):
    cache, _, codec, _ = modules(monkeypatch)
    with pytest.raises(cache.CacheRefusal):
        codec.decode_model({codec.TAG: 'dataclass', 'type': 'os:system', 'fields': {}})
    from project_mai_tai.atr_simulation.types import SourceRef, EvidenceKind
    packed = codec.encode_model(SourceRef('/CONTROLLED', 'a' * 64, '$', EvidenceKind.CONTROLLED, None))
    del packed['fields']['sha256']
    with pytest.raises(cache.CacheRefusal, match='exact_sealed_model_fields_required'):
        codec.decode_model(packed)


def test_typed_cache_preserves_enum_tuple_decimal_and_original_source_reference(monkeypatch):
    _, _, codec, _ = modules(monkeypatch)
    from project_mai_tai.atr_simulation.types import SourceRef, EvidenceKind, EventKey, EntryFact
    source = SourceRef('/CONTROLLED/old-input', 'a' * 64, '$', EvidenceKind.CONTROLLED, 123)
    value = (EntryFact('CONTROLLED', EventKey(123, 1, 'CONTROLLED:1', source, 124), Decimal('1.5300'), 100, source),)
    actual = codec.decode_model(codec.encode_model(value))
    assert type(actual) is tuple and type(actual[0]) is EntryFact and actual == value
    assert actual[0].source.kind is EvidenceKind.CONTROLLED and str(actual[0].price) == '1.5300'


def test_default_none_session_does_not_open_or_create_cache(monkeypatch, tmp_path):
    _, _, _, coordinator = modules(monkeypatch)
    absent = tmp_path / 'never-created'
    with coordinator.incremental_session(absent, None, version=None) as run:
        assert run is None
    assert not absent.exists()


def test_roster_partial_processing_cannot_commit_a_partial_cache(monkeypatch, tmp_path):
    cache, _, _, coordinator = modules(monkeypatch)
    store = cache.CacheStore(root(tmp_path), binding(cache))
    run = coordinator.IncrementalRun(store)
    run.bind_snapshot(dict(read_only='on', isolation='repeatable read', snapshot_id='CONTROLLED'),
        dict(day='2026-10-28', asof=LEFT), ['ONE', 'TWO'])
    run.next['windows']['ONE'] = {}
    with pytest.raises(cache.CacheRefusal, match='all_original_roster_components_required'):
        run.persist()


def flagged(value):
    from project_mai_tai.atr_simulation.same_sip_types import EpisodeInput
    from project_mai_tai.atr_simulation.same_sip import SAME_SIP_CONSERVATIVE_V1
    return EpisodeInput(**{field.name: getattr(value, field.name) for field in fields(value)},
                        same_sip_policy=SAME_SIP_CONSERVATIVE_V1)


def test_firstbar_cached_file_worlds_equal_fresh_identical_input_and_keep_shared_volume(monkeypatch, tmp_path):
    cache, _, codec, _ = modules(monkeypatch)
    from project_mai_tai.atr_simulation.same_sip_firstbar_replay import FirstBarReplayEngine, FirstBarFillPolicy
    from tests.support.atr_same_sip_cases import replay, trade, encoded
    # The default fixture SELL is240s: before() cancels the remaining50 before
    # this eligible exit print. A63s print is still entry liquidity and would
    # correctly fill it, defeating this isolated150-share conservation case.
    rows = (trade(61, '2.03', seq=1), trade(61, '1.99', seq=2),
            trade(62, '2.07', size=150), trade(240, '2.25'))
    inputs = tuple(replace(replay(rows, account=account), episode=flagged(replay(rows, account=account).episode))
                   for account in ('one', 'two'))
    engine = FirstBarReplayEngine()
    policy = FirstBarFillPolicy.OBSERVED_PRINT_VOLUME
    fresh = engine.simulate_many(inputs, fill_policy=policy)
    assert sum(fill.quantity for result in fresh if result.mode.value == 'FIRSTBAR' for fill in result.fills) == 150
    store = cache.CacheStore(root(tmp_path), binding(cache))
    store.save(dict(results=codec.encode_model(fresh), reports=codec.encode_reports(engine.same_sip_reports)))
    saved = store.load()
    decoded = codec.decode_model(saved['results'])
    reports = codec.decode_reports(saved['reports'])
    retained = {mode: tuple(row for row in decoded if row.mode.value == mode) for mode in ('FIRSTBAR', 'FIRSTBAR_X3')}
    proof = {mode: ('a' * 64, 'b' * 64) for mode in retained}  # Controlled SAME-input proof, no SQL claim.
    cached = FirstBarReplayEngine().simulate_many(inputs, fill_policy=policy, retained_modes=retained,
        retained_reports=reports, retained_bindings=proof, current_bindings=proof)
    assert encoded(cached) == encoded(fresh)
    assert decoded == fresh and reports == engine.same_sip_reports


def test_core_complete_typed_result_codec_preserves_original_scenarios_and_provenance(monkeypatch):
    _, _, codec, _ = modules(monkeypatch)
    from project_mai_tai.atr_simulation.same_sip_engine import SimulationEngine
    from tests.support.atr_same_sip_cases import episode, trade, encoded
    rows = (trade(61, '1.95', seq=1), trade(61, '2.05', seq=2), trade(62, '1.90', size=150), trade(63, '2.20'))
    inputs = tuple(flagged(episode(rows, account=account)) for account in ('one', 'two'))
    fresh = SimulationEngine().simulate_many(inputs)
    decoded = codec.decode_model(codec.encode_model(fresh))
    assert decoded == fresh and encoded(decoded) == encoded(fresh)
    assert tuple(row.identity.account_id for row in decoded) == ('one', 'two')
    # This tests typed codec only. UNKNOWN scenarios are NOT admitted by the
    # production cache merely because the API can represent them.


def proof_window(reads, who, *, snapshot='CONTROLLED-RR', asof=None):
    asof = asof or LEFT + timedelta(minutes=3)
    trades = cert(reads, asof=asof, snapshot=snapshot)
    quotes = reads.certify(dict(row_count=1, content_sha256='e' * 64, source_timezone='UTC'),
        kind='quotes', symbol='CONTROLLED', start=LEFT, end=LEFT + timedelta(minutes=3), day_start=LEFT,
        asof=asof, snapshot_id=snapshot, query_sha256=reads.query_pair('quotes')['query_sha256'],
        read_only='on', isolation='repeatable read', complete=True)
    return dict(identity=who, certificates=dict(trades=trades, quotes=quotes),
        header_hashes={name: 'd' * 64 for name in
            ('bars', 'capture_bars', 'scanner', 'fills', 'route', 'coverage', 'oracle', 'membership')})


def test_stable_content_bindings_require_new_RR_witness_without_retagging_old_asof(monkeypatch):
    cache, reads, *_ = modules(monkeypatch)
    who = identity()
    original = proof_window(reads, who)
    new_asof = LEFT + timedelta(minutes=5)
    current = proof_window(reads, who, snapshot='NEW-RR', asof=new_asof)
    kwargs = dict(cohort_dependencies={'ordered': [who]}, policies=binding(cache)['policies'], source_seal='a' * 64)
    before = reads.proof_bindings([who], windows=[original], current_asof=LEFT + timedelta(minutes=3),
                                 current_snapshot='CONTROLLED-RR', **kwargs)
    after = reads.proof_bindings([who], windows=[current], current_asof=new_asof, current_snapshot='NEW-RR', **kwargs)
    assert before['bindings'] == after['bindings']
    assert before['current_verification'] != after['current_verification']
    assert original['certificates']['trades']['asof'] == LEFT + timedelta(minutes=3)


@pytest.mark.parametrize('field', ['bars', 'scanner', 'fills', 'route', 'coverage', 'oracle', 'membership', 'quote_content'])
def test_any_changed_dependency_invalidates_whole_cohort_binding(monkeypatch, field):
    cache, reads, *_ = modules(monkeypatch)
    who = identity()
    window = proof_window(reads, who)
    kwargs = dict(cohort_dependencies={'ordered': [who]}, policies=binding(cache)['policies'], source_seal='a' * 64,
                  current_asof=LEFT + timedelta(minutes=3), current_snapshot='CONTROLLED-RR')
    before = reads.proof_bindings([who], windows=[window], **kwargs)
    changed = deepcopy(window)
    if field == 'quote_content': changed['certificates']['quotes']['content_sha256'] = 'f' * 64
    else: changed['header_hashes'][field] = 'f' * 64
    after = reads.proof_bindings([who], windows=[changed], **kwargs)
    assert before['bindings'] != after['bindings'] and before['cohort_sha256'] != after['cohort_sha256']


def test_quote_delta_keeps_true_carry_and_whole_timestamp_without_duplicate_volume(monkeypatch):
    cache, reads, *_ = modules(monkeypatch)
    def quote(ident, seconds, bid):
        return dict(id=ident, provider='massive', symbol='CONTROLLED', event_ts=LEFT + timedelta(seconds=seconds),
            received_at=LEFT + timedelta(minutes=6), bid_price=bid, ask_price=Decimal('2.1'), bid_size=100, ask_size=100)
    prior, within = quote(1, 10, Decimal('2')), quote(2, 90, Decimal('2'))
    later, invalid = quote(3, 180, Decimal('2')), quote(4, 180, None)
    rows = [prior, within, later, invalid]
    hashes = {(row['symbol'], row['id']): cache.fingerprint(row) for row in rows}
    full = reads.certify(dict(row_count=4, content_sha256=reads.content_hash(rows, hashes), source_timezone='UTC'),
        kind='quotes', symbol='CONTROLLED', start=LEFT + timedelta(minutes=1), end=LEFT + timedelta(minutes=6),
        asof=LEFT + timedelta(minutes=6), day_start=LEFT, snapshot_id='CONTROLLED-RR',
        query_sha256=reads.query_pair('quotes')['query_sha256'], read_only='on', isolation='repeatable read', complete=True)
    merged, _ = reads.merge_open(prefix_rows=[prior, within], prefix_hashes=hashes,
        delta_rows=[within, later, invalid], delta_hashes=hashes, cutoff=LEFT + timedelta(minutes=3),
        current_full_certificate=full, current_asof=full['asof'], current_snapshot=full['snapshot_id'])
    assert merged == rows and len(merged) == 4
    assert merged[-1]['bid_price'] is None and merged[0] == prior


def test_missing_quote_certificate_cannot_be_replaced_by_zero_or_time(monkeypatch):
    cache, reads, *_ = modules(monkeypatch)
    who = identity()
    window = proof_window(reads, who)
    del window['certificates']['quotes']
    with pytest.raises(cache.CacheRefusal, match='complete_actual_window_dependencies_required'):
        reads.proof_bindings([who], windows=[window], cohort_dependencies={}, policies=binding(cache)['policies'],
            source_seal='a' * 64, current_asof=LEFT + timedelta(minutes=3), current_snapshot='CONTROLLED-RR')


def test_saved_file_witness_is_explicitly_not_current_RR_and_requires_actual_bytes(monkeypatch, tmp_path):
    cache, *_ = modules(monkeypatch)
    files = importlib.import_module('atrsimlive1_incremental_file')
    path = (tmp_path / 'controlled-retained-input.json').resolve()
    raw = b'{"scope":"CONTROLLED retained FILE only"}'
    path.write_bytes(raw)
    path.chmod(0o600)
    witness = files.verify_saved_file(path, hashlib.sha256(raw).hexdigest())
    assert witness['scope'] == 'RETAINED_FILE_NOT_CURRENT_RR' and 'snapshot_id' not in witness
    with pytest.raises(cache.CacheRefusal, match='retained_input_changed_or_hash_mismatch'):
        files.verify_saved_file(path, 'f' * 64)


def test_file_cache_cannot_be_used_as_production_RR_cache_scope(monkeypatch, tmp_path):
    cache, _, _, coordinator = modules(monkeypatch)
    expected = dict(binding(cache), input_scope='RETAINED_FILE')
    store = cache.CacheStore(root(tmp_path), expected)
    with pytest.raises(cache.CacheRefusal, match='production_requires_current_RR_cache_scope'):
        coordinator.IncrementalRun(store)
