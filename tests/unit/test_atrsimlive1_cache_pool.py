"""CONTROLLED lossless-pool source cases; Main alone executes."""
from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal
import importlib
from pathlib import Path

import pytest


def setup(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2] / 'tools'))
    return importlib.import_module('atrsimlive1_cache_pool'), importlib.import_module('atrsimlive1_incremental_cache')


def body():
    at = datetime(2026, 10, 28, 12, tzinfo=timezone.utc)
    trade = dict(id=7, provider='massive', symbol='CONTROLLED', event_ts=at, received_at=at,
                 price=Decimal('1.5300'), size=100, exchange=1, conditions=None, cumulative_volume=100)
    quote = dict(id=8, provider='massive', symbol='CONTROLLED', event_ts=at, received_at=at,
                 bid_price=Decimal('1.52'), ask_price=Decimal('1.53'), bid_size=100, ask_size=None)
    raw = dict(trades=dict(rows=[trade], hashes={'7': 'a' * 64}, certificate={'kind': 'trades'}),
               quotes=dict(rows=[quote], hashes={'8': 'b' * 64}, certificate={'kind': 'quotes'}))
    # Intentionally nonlexical insertion order, matching planner rather than
    # a convenient pre-sorted fixture. JSON cache decoding sorts object keys.
    return dict(windows={'CONTROLLED': {'z-origin': deepcopy(raw), 'a-origin': deepcopy(raw),
                                       'm-origin': deepcopy(raw), 'b-origin': deepcopy(raw)}},
                worlds={}, roster=['CONTROLLED'], source_asof=at)


def test_pool_OFF_is_same_object_and_same_bytes(monkeypatch):
    pool, cache = setup(monkeypatch)
    original = body()
    assert pool.pack_windows(original) is original
    assert pool.unpack_windows(original) is original
    assert cache.fingerprint(pool.pack_windows(original)) == cache.fingerprint(original)


def test_pool_ON_roundtrip_preserves_all_raw_native_bytes_and_reduces_repetition(monkeypatch):
    pool, cache = setup(monkeypatch)
    original = body()
    packed = pool.pack_windows(original, enabled=True)
    assert len(packed['pools']['trades|CONTROLLED']['rows']) == 1
    assert len(packed['pools']['quotes|CONTROLLED']['rows']) == 1
    restored = pool.unpack_windows(packed, enabled=True)
    assert restored == original
    assert cache.fingerprint(restored) == cache.fingerprint(original)
    assert str(restored['windows']['CONTROLLED']['a-origin']['trades']['rows'][0]['price']) == '1.5300'
    assert restored['windows']['CONTROLLED']['a-origin']['trades']['rows'][0]['conditions'] is None
    old_bytes = b''.join(chunk.encode() for chunk in cache.chunks(original))
    new_bytes = b''.join(chunk.encode() for chunk in cache.chunks(packed))
    assert len(new_bytes) < len(old_bytes)  # Fixture size only, not a runtime claim.


def test_pool_json_key_reordering_does_not_change_reference_meaning(monkeypatch):
    pool, cache = setup(monkeypatch)
    original = body()
    packed = pool.pack_windows(original, enabled=True)
    # Reorder both namespace and window maps as the canonical JSON loader does.
    packed['body']['windows']['CONTROLLED'] = dict(sorted(packed['body']['windows']['CONTROLLED'].items()))
    packed['pools'] = dict(sorted(packed['pools'].items()))
    assert cache.fingerprint(pool.unpack_windows(packed, enabled=True)) == cache.fingerprint(original)


@pytest.mark.parametrize('field,value', [('conditions', []), ('price', Decimal('1.53')),
                                       ('size', True), ('symbol', 'FOREIGN')])
def test_pool_conflicting_same_physical_identity_is_not_dedup_repair(monkeypatch, field, value):
    pool, cache = setup(monkeypatch)
    original = body()
    original['windows']['CONTROLLED']['z-origin']['trades']['rows'][0][field] = value
    with pytest.raises(cache.CacheRefusal):
        pool.pack_windows(original, enabled=True)


def test_duplicate_inside_one_window_is_still_refused(monkeypatch):
    pool, cache = setup(monkeypatch)
    original = body()
    window = original['windows']['CONTROLLED']['a-origin']['trades']
    window['rows'].append(deepcopy(window['rows'][0]))
    with pytest.raises(cache.CacheRefusal, match='raw_pool_exact_physical_identity_required'):
        pool.pack_windows(original, enabled=True)


@pytest.mark.parametrize('fault', ['negative', 'boolean', 'duplicate', 'foreign-pool', 'unreferenced'])
def test_bad_pool_reference_cannot_change_selected_payload(monkeypatch, fault):
    pool, cache = setup(monkeypatch)
    packed = pool.pack_windows(body(), enabled=True)
    window = packed['body']['windows']['CONTROLLED']['a-origin']['trades']
    if fault == 'negative':
        window['row_refs'] = [-1]
    elif fault == 'boolean':
        window['row_refs'] = [False]
    elif fault == 'duplicate':
        window['row_refs'] = [0, 0]
    elif fault == 'foreign-pool':
        window['pool'] = 'quotes|CONTROLLED'
    else:
        extra = deepcopy(packed['pools']['trades|CONTROLLED']['rows'][0])
        extra['id'] = 99
        packed['pools']['trades|CONTROLLED']['rows'].append(extra)
        packed['pools']['trades|CONTROLLED']['hashes']['99'] = 'c' * 64
    with pytest.raises(cache.CacheRefusal):
        pool.unpack_windows(packed, enabled=True)


def test_pooled_format_never_silently_enabled(monkeypatch):
    pool, cache = setup(monkeypatch)
    packed = pool.pack_windows(body(), enabled=True)
    with pytest.raises(cache.CacheRefusal, match='pooled_cache_requires_explicit_profile'):
        pool.unpack_windows(packed)


def test_decoded_windows_do_not_share_mutable_row_aliases(monkeypatch):
    pool, _ = setup(monkeypatch)
    packed = pool.pack_windows(body(), enabled=True)
    restored = pool.unpack_windows(packed, enabled=True)
    restored['windows']['CONTROLLED']['a-origin']['trades']['rows'][0]['conditions'] = [37]
    assert restored['windows']['CONTROLLED']['z-origin']['trades']['rows'][0]['conditions'] is None
    assert packed['pools']['trades|CONTROLLED']['rows'][0]['conditions'] is None


def test_unpack_verifies_canonical_relations_without_second_pool_copy(monkeypatch):
    pool, cache = setup(monkeypatch)
    original = body()
    packed = pool.pack_windows(original, enabled=True)
    monkeypatch.setattr(pool, 'pack_windows', lambda *a, **k: pytest.fail('unpack rebuilt a duplicate pool'))
    assert cache.fingerprint(pool.unpack_windows(packed, enabled=True)) == cache.fingerprint(original)


@pytest.mark.parametrize('fault', ['duplicate-physical', 'unused-empty-partition', 'extra-hash'])
def test_unpack_refuses_noncanonical_identity_and_hash_populations(monkeypatch, fault):
    pool, cache = setup(monkeypatch)
    packed = pool.pack_windows(body(), enabled=True)
    if fault == 'duplicate-physical':
        packed['pools']['trades|CONTROLLED']['rows'].append(deepcopy(packed['pools']['trades|CONTROLLED']['rows'][0]))
        packed['body']['windows']['CONTROLLED']['z-origin']['trades']['row_refs'] = [1]
    elif fault == 'unused-empty-partition':
        packed['pools']['trades|FOREIGN'] = dict(rows=[], hashes={})
    else:
        packed['pools']['trades|CONTROLLED']['hashes']['999'] = 'c' * 64
    with pytest.raises(cache.CacheRefusal):
        pool.unpack_windows(packed, enabled=True)
