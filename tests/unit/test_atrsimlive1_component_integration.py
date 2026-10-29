"""Author-UNRUN connected cache/profile controls; Main alone executes."""
from dataclasses import replace
from datetime import timedelta
import importlib
from pathlib import Path

import pytest


def modules(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2] / 'tools'))
    return (importlib.import_module('atrsimlive1_incremental_cache'),
            importlib.import_module('atrsimlive1_namespace'))


def binding(cache, **flags):
    result = dict(version=cache.INCREMENTAL_V1, trading_day='2026-10-28', source_seal='a' * 64,
        input_scope='CURRENT_RR', policies=dict(execution_rules_sha256='b' * 64,
                                               null_policy=None, same_sip_policy=None))
    if flags:
        result['features'] = dict(version=cache.FEATURE_PROFILE,
            **{name: flags.get(name, False) for name in cache.FEATURE_NAMES})
    return result


def test_features_default_OFF_and_explicit_profile_get_new_namespace(monkeypatch):
    cache, namespace = modules(monkeypatch)
    original = binding(cache)
    assert cache.features(original) == {'closed_components': False, 'raw_pool': False, 'since_last_run': False}
    for flag in cache.FEATURE_NAMES:
        selected = binding(cache, **{flag: True})
        assert namespace.namespace_name(selected) != namespace.namespace_name(original)
        assert cache.features(selected)[flag] is True


@pytest.mark.parametrize('fault', ['null', 'string-flag', 'foreign-version', 'extra'])
def test_foreign_feature_profiles_fail_closed(monkeypatch, fault):
    cache, _ = modules(monkeypatch)
    value = binding(cache, closed_components=True)
    if fault == 'null': value['features'] = None
    elif fault == 'string-flag': value['features']['closed_components'] = 'true'
    elif fault == 'foreign-version': value['features']['version'] = 'foreign'
    else: value['features']['permission'] = True
    with pytest.raises(cache.CacheRefusal, match='exact_reviewed_cache_feature_profile_required'):
        cache.features(value)


def test_actual_cache_pool_roundtrip_reduces_file_without_new_namespace_files(monkeypatch, tmp_path):
    cache, _ = modules(monkeypatch)
    from tests.unit.test_atrsimlive1_cache_pool import body

    roots, receipts = [], []
    for name, flags in (('old', {}), ('pooled', {'raw_pool': True})):
        root = (tmp_path / name).resolve()
        root.mkdir(mode=0o700)
        store = cache.CacheStore(root, binding(cache, **flags))
        original = body()
        receipts.append(store.save(original))
        restored = store.load()
        assert cache.fingerprint(restored) == cache.fingerprint(original)
        assert {entry.name for entry in root.iterdir()} == {'cache.json'}
        assert store.load()['windows']['CONTROLLED']['a-origin']['trades']['rows'][0]['conditions'] is None
        roots.append(root)
    assert receipts[1]['bytes'] < receipts[0]['bytes']  # Controlled fixture, not actual warm savings.
    foreign = cache.CacheStore(roots[1], binding(cache))
    with pytest.raises(cache.CacheRefusal):
        foreign.load()


def test_pool_expansion_refuses_before_unbounded_reference_copy(monkeypatch):
    cache, _ = modules(monkeypatch)
    pool = importlib.import_module('atrsimlive1_cache_pool')
    from tests.unit.test_atrsimlive1_cache_pool import body

    packed = pool.pack_windows(body(), enabled=True)
    monkeypatch.setattr(pool, 'MAX_CACHE_BYTES', 100)
    with pytest.raises(cache.CacheRefusal, match='raw_pool_expanded_cache_byte_cap'):
        pool.unpack_windows(packed, enabled=True)


def test_only_current_collector_observation_provenance_is_separate_from_input_key(monkeypatch, tmp_path):
    cache, _ = modules(monkeypatch)
    module = importlib.import_module('atrsimlive1_reuse_components')
    collect = importlib.import_module('atrsimlive1_collect')
    from tests.unit.test_atrsimlive1_current_rr_model_context import context
    from project_mai_tai.atr_simulation.same_sip_types import SourceRef, EvidenceKind
    from atrsimlive1_working_windows import nanos

    _, _, _, episode, _, data, _ = context(monkeypatch, tmp_path, 'CORE', True)
    hashes = {'trades': 'e' * 64}
    old = SourceRef('postgres:readonly-snapshot:OLD', collect.digest(hashes), '$', EvidenceKind.RETAINED,
                    nanos(data['source_asof']))
    next_asof = data['source_asof'] + timedelta(minutes=1)
    new = replace(old, artifact='postgres:readonly-snapshot:NEW', asof_ns=nanos(next_asof))
    old_episode, new_episode = replace(episode, sizing_source=old), replace(episode, sizing_source=new)
    old_key = module.input_key(old_episode, snapshot_id='OLD', source_asof=data['source_asof'], source_hashes=hashes)
    new_key = module.input_key(new_episode, snapshot_id='NEW', source_asof=next_asof, source_hashes=hashes)
    assert old_key == new_key
    assert old_episode.sizing_source == old and new_episode.sizing_source == new  # No mutation or retag.
    for altered in (replace(new_episode, quantity=99), replace(new_episode, trigger=episode.trigger + 1),
                    replace(new_episode, sizing_source=replace(new, pointer='DIFFERENT')),
                    replace(new_episode, sizing_source=replace(new, artifact='actual-non-wrapper-source'))):
        assert module.input_key(altered, snapshot_id='NEW', source_asof=next_asof, source_hashes=hashes) != old_key


def test_growing_full_population_digest_cannot_replace_or_weaken_component_history(monkeypatch, tmp_path):
    cache, _ = modules(monkeypatch)
    module = importlib.import_module('atrsimlive1_reuse_components')
    collect = importlib.import_module('atrsimlive1_collect')
    from tests.unit.test_atrsimlive1_current_rr_model_context import context
    from project_mai_tai.atr_simulation.same_sip_types import SourceRef, EvidenceKind
    from atrsimlive1_working_windows import nanos

    _, _, _, episode, _, data, _ = context(monkeypatch, tmp_path, 'CORE', True)
    old_hashes, new_hashes = {'trades': 'e' * 64}, {'trades': 'f' * 64}
    old_asof, new_asof = data['source_asof'], data['source_asof'] + timedelta(minutes=1)
    old_source = SourceRef('postgres:readonly-snapshot:OLD', collect.digest(old_hashes), '$', EvidenceKind.RETAINED, nanos(old_asof))
    new_source = SourceRef('postgres:readonly-snapshot:NEW', collect.digest(new_hashes), '$', EvidenceKind.RETAINED, nanos(new_asof))
    old_item, new_item = replace(episode, sizing_source=old_source), replace(episode, sizing_source=new_source)
    strong_closed_history = 'a' * 64  # CONTROLLED complete prefix/cert/header key, not an absence watermark.
    left = module.input_key(old_item, snapshot_id='OLD', source_asof=old_asof, source_hashes=old_hashes,
        component_history_sha256=strong_closed_history)
    right = module.input_key(new_item, snapshot_id='NEW', source_asof=new_asof, source_hashes=new_hashes,
        component_history_sha256=strong_closed_history)
    assert left == right and old_item.sizing_source == old_source and new_item.sizing_source == new_source
    assert module.input_key(new_item, snapshot_id='NEW', source_asof=new_asof, source_hashes=new_hashes,
        component_history_sha256='b' * 64) != left
    # Without a supplied full scoped proof, changing source digests stays an
    # invalidation. No generic "ignore SourceRef.sha256" behavior.
    assert module.input_key(old_item, snapshot_id='OLD', source_asof=old_asof, source_hashes=old_hashes) != module.input_key(
        new_item, snapshot_id='NEW', source_asof=new_asof, source_hashes=new_hashes)


def test_retained_FILE_cannot_enter_current_RR_component_profile(monkeypatch, tmp_path):
    cache, _ = modules(monkeypatch)
    model = importlib.import_module('atrsimlive1_incremental_models')
    from types import SimpleNamespace

    value = binding(cache, closed_components=True)
    value['input_scope'] = 'RETAINED_FILE'
    with pytest.raises(cache.CacheRefusal, match='closed_components_require_current_RR_not_FILE_witness'):
        model.IncrementalModels(SimpleNamespace(binding=value))
