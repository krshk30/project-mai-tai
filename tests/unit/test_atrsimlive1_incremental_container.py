"""SOURCE-only controlled byte-container cases; Main owns execution."""
from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import importlib
import json
from pathlib import Path
from uuid import UUID

import pytest


def setup(monkeypatch, tmp_path):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2] / 'tools'))
    cache = importlib.import_module('atrsimlive1_incremental_cache')
    container = importlib.import_module('atrsimlive1_incremental_container')
    root = (tmp_path / 'cache-v2').resolve()
    root.mkdir(mode=0o700)
    binding = dict(version=cache.INCREMENTAL_V1, trading_day='2026-10-28', source_seal='a' * 64,
        input_scope='CURRENT_RR', policies=dict(execution_rules_sha256='b' * 64,
                                               null_policy=None, same_sip_policy=None))
    return cache, container, cache.CacheStore(root, binding)


def population():
    raw = dict(rows=[dict(id=7, price=Decimal('1.5300'), conditions=None,
                         at=datetime(2026, 10, 28, 12, tzinfo=timezone.utc))], hashes={'7': 'c' * 64},
               certificate=dict(content_sha256='d' * 64, row_count=1))
    world = dict(member_keys=['e' * 64], bindings=['f' * 64],
                 entries=[dict(receipt={'issues': []}, row={'known': True})],
                 typed_results={'controlled': 'NOT_AN_EXECUTABLE_CODEC_TAG'}, typed_reports=[])
    return dict(windows={'TEST': {'one': dict(start_ns=1, end_key=[2, 7], trades=deepcopy(raw), quotes=deepcopy(raw))}},
                worlds={'TEST|FIRSTBAR': world}, source_asof=datetime(2026, 10, 28, 13, tzinfo=timezone.utc))


def index(store, container):
    raw = (store.root / 'cache.json').read_bytes()
    magic, start, size, checksum = container.HEADER.unpack(raw[:container.HEADER.size])
    return raw, json.loads(raw[start:start + size]), (magic, start, size, checksum)


def replace_index(store, container, document, old):
    magic, start, _, _ = old
    raw = (store.root / 'cache.json').read_bytes()
    value = json.dumps(document, separators=(',', ':')).encode()
    (store.root / 'cache.json').write_bytes(container.HEADER.pack(magic, start, len(value), hashlib.sha256(value).digest())
                                          + raw[container.HEADER.size:start] + value)


def test_index_admission_does_not_decode_raw_or_world_payload(monkeypatch, tmp_path):
    _, container, store = setup(monkeypatch, tmp_path)
    store.save(population())
    original, parsed = container._decode, []
    def observed(raw):
        parsed.append(raw)
        return original(raw)
    monkeypatch.setattr(container, '_decode', observed)
    body = store.load()
    assert len(parsed) == 1
    assert body['windows']['TEST']['one']['trades']['certificate']['row_count'] == 1
    assert body['worlds']['TEST|FIRSTBAR']['member_keys'] == ['e' * 64]
    assert len(parsed) == 1
    assert not body['windows']['TEST']['one']['trades'].loaded
    assert not body['worlds']['TEST|FIRSTBAR'].loaded


def test_needed_slice_only_decodes_after_byte_verification(monkeypatch, tmp_path):
    _, container, store = setup(monkeypatch, tmp_path)
    expected = population()
    store.save(expected)
    body = store.load()
    assert body['windows']['TEST']['one']['trades']['rows'] == expected['windows']['TEST']['one']['trades']['rows']
    assert not body['windows']['TEST']['one']['quotes'].loaded
    assert not body['worlds']['TEST|FIRSTBAR'].loaded


def test_cache_read_and_write_do_not_fingerprint_decoded_body(monkeypatch, tmp_path):
    cache, _, store = setup(monkeypatch, tmp_path)
    def forbidden(_):
        raise AssertionError('integrity-only re-encode')
    monkeypatch.setattr(cache, 'fingerprint', forbidden)
    receipt = store.save(population())
    body = store.load()
    assert body['worlds']['TEST|FIRSTBAR']['bindings'] == ['f' * 64]
    assert receipt['sha256'] == hashlib.sha256((store.root / 'cache.json').read_bytes()).hexdigest()


def test_each_section_serialized_once_and_not_as_full_body(monkeypatch, tmp_path):
    _, container, store = setup(monkeypatch, tmp_path)
    original, calls = container.chunks, []
    def observed(value):
        calls.append(value)
        return original(value)
    monkeypatch.setattr(container, 'chunks', observed)
    store.save(population())
    assert len(calls) == 4  # trades, quotes, whole world, then small index.
    assert sum('rows' in item for item in calls) == 2
    assert sum('typed_results' in item for item in calls) == 1
    assert sum(item.get('schema') == container.SCHEMA for item in calls) == 1
    assert all(not {'windows', 'worlds'} <= set(item) for item in calls)


def test_mutated_index_rejected_before_json_parser(monkeypatch, tmp_path):
    cache, container, store = setup(monkeypatch, tmp_path)
    store.save({'number': 1})
    path = store.root / 'cache.json'
    raw = path.read_bytes()
    assert b'"number":1' in raw
    path.write_bytes(raw.replace(b'"number":1', b'"number":2'))
    monkeypatch.setattr(container, '_decode', lambda _: pytest.fail('unverified JSON parsed'))
    with pytest.raises(cache.CacheRefusal, match='cache_index_bytes_changed'):
        store.load()


def test_mutated_raw_slice_refuses_before_payload_parser(monkeypatch, tmp_path):
    cache, container, store = setup(monkeypatch, tmp_path)
    store.save(population())
    raw, doc, _ = index(store, container)
    section = doc['sections'][0]
    changed = bytearray(raw)
    changed[section['offset']] ^= 1
    (store.root / 'cache.json').write_bytes(changed)
    body = store.load()  # Index valid; unread payload NOT claimed verified.
    monkeypatch.setattr(container, '_decode', lambda _: pytest.fail('unverified payload parsed'))
    with pytest.raises(cache.CacheRefusal, match='cache_section_bytes_changed'):
        body['windows']['TEST']['one']['trades']['rows']


def test_deferred_reference_refuses_generation_replacement(monkeypatch, tmp_path):
    cache, _, store = setup(monkeypatch, tmp_path)
    store.save(population())
    previous = store.load()
    store.save(population())
    with pytest.raises(cache.CacheRefusal, match='cache_section_generation_changed'):
        previous['worlds']['TEST|FIRSTBAR']['entries']


def test_unread_section_copy_validates_bytes_without_decode(monkeypatch, tmp_path):
    _, container, store = setup(monkeypatch, tmp_path)
    store.save(population())
    body = store.load()
    original = container.Section.decode
    monkeypatch.setattr(container.Section, 'decode', lambda _: pytest.fail('unneeded raw decode'))
    store.save(body)
    monkeypatch.setattr(container.Section, 'decode', original)
    assert store.load()['windows']['TEST']['one']['trades']['rows'][0]['price'] == Decimal('1.5300')


def test_mutated_materialized_rows_are_not_replaced_by_old_bytes(monkeypatch, tmp_path):
    _, _, store = setup(monkeypatch, tmp_path)
    store.save(population())
    body = store.load()
    body['windows']['TEST']['one']['trades']['rows'][0]['price'] = Decimal('9.0000')
    store.save(body)
    assert store.load()['windows']['TEST']['one']['trades']['rows'][0]['price'] == Decimal('9.0000')


def test_world_copy_preserves_complete_payload_and_original_provenance(monkeypatch, tmp_path):
    _, container, store = setup(monkeypatch, tmp_path)
    expected = population()
    store.save(expected)
    old = store.load()['worlds']['TEST|FIRSTBAR']
    new = container.copied_world(old, last_verification={'scope': 'CURRENT_RR'})
    assert new['entries'] == expected['worlds']['TEST|FIRSTBAR']['entries']
    assert new['typed_results'] == expected['worlds']['TEST|FIRSTBAR']['typed_results']
    assert new['last_verification'] == {'scope': 'CURRENT_RR'}


@pytest.mark.parametrize('kind', ['old-v1', 'foreign-binding'])
def test_save_preserves_incompatible_existing_namespace(monkeypatch, tmp_path, kind):
    cache, _, store = setup(monkeypatch, tmp_path)
    path = store.root / 'cache.json'
    if kind == 'old-v1':
        path.write_bytes(b'{"schema":"ATRSIMLIVE1_PRIVATE_INCREMENTAL_CACHE_V1","body":{}}')
        path.chmod(0o600)
    else:
        other = deepcopy(store.binding)
        other['source_seal'] = 'c' * 64
        cache.CacheStore(store.root, other).save({'old': True})
    old = path.read_bytes()
    with pytest.raises(cache.CacheRefusal):
        store.save({'new': True})
    assert path.read_bytes() == old
    assert not (store.root / '.next.json').exists()


@pytest.mark.parametrize('mutation', ['gap', 'overlap', 'foreign-path', 'duplicate-path', 'bad-sha'])
def test_exact_section_geometry_and_scope_refuse(monkeypatch, tmp_path, mutation):
    cache, container, store = setup(monkeypatch, tmp_path)
    store.save(population())
    _, doc, header = index(store, container)
    if mutation == 'gap':
        doc['sections'][0]['offset'] += 1
    elif mutation == 'overlap':
        doc['sections'][1]['offset'] -= 1
    elif mutation == 'foreign-path':
        doc['sections'][0]['path'][0] = 'EXECUTABLE'
    elif mutation == 'duplicate-path':
        doc['sections'][1]['path'] = doc['sections'][0]['path']
    else:
        doc['sections'][0]['sha256'] = 'not-a-sha'
    replace_index(store, container, doc, header)
    with pytest.raises(cache.CacheRefusal):
        store.load()


@pytest.mark.parametrize('raw', [b'{"duplicate":1,"duplicate":2}', b'{"number":NaN}', b'{"number":1e400}',
                                 b'{"__atrsim_incremental_native_v1__":"exec","value":"x"}'])
def test_verified_bytes_still_require_strict_json_codec(monkeypatch, tmp_path, raw):
    cache, container, _ = setup(monkeypatch, tmp_path)
    with pytest.raises(cache.CacheRefusal):
        container._decode(raw)


def test_native_scalar_precision_preserved(monkeypatch, tmp_path):
    _, _, store = setup(monkeypatch, tmp_path)
    value = {'decimal': Decimal('1.5300'), 'id': UUID('11111111-1111-4111-8111-111111111111'),
             'clock': datetime(2026, 10, 28, 12, tzinfo=timezone.utc)}
    store.save(value)
    actual = store.load()
    assert actual == value
    assert str(actual['decimal']) == '1.5300'


def test_fixed_stale_stage_and_private_mode_preserved(monkeypatch, tmp_path):
    _, _, store = setup(monkeypatch, tmp_path)
    store.save(population())
    path, stage = store.root / 'cache.json', store.root / '.next.json'
    before = path.read_bytes()
    stage.write_bytes(b'controlled crash residue')
    stage.chmod(0o600)
    with pytest.raises(FileExistsError):
        store.save(population())
    assert path.read_bytes() == before
    assert stage.read_bytes() == b'controlled crash residue'
    assert path.stat().st_mode & 0o777 == 0o600


def test_cap_failure_keeps_old_active_file(monkeypatch, tmp_path):
    cache, _, store = setup(monkeypatch, tmp_path)
    store.save({'old': True})
    before = (store.root / 'cache.json').read_bytes()
    monkeypatch.setattr(cache, 'MAX_CACHE_BYTES', len(before) + 10)
    with pytest.raises(cache.CacheRefusal, match='cache_file_byte_cap'):
        store.save({'payload': 'x' * (len(before) + 100)})
    assert (store.root / 'cache.json').read_bytes() == before
    assert not (store.root / '.next.json').exists()


@pytest.mark.parametrize('mutation', ['set', 'update', 'pop'])
def test_mutating_deferred_mapping_materializes_before_save(monkeypatch, tmp_path, mutation):
    _, _, store = setup(monkeypatch, tmp_path)
    store.save(population())
    body = store.load()
    value = body['windows']['TEST']['one']['trades']
    if mutation == 'set':
        value['rows'] = [{'id': 9}]
    elif mutation == 'update':
        value.update(rows=[{'id': 9}])
    else:
        assert value.pop('rows')[0]['id'] == 7
    assert value.loaded
    store.save(body)
    actual = store.load()['windows']['TEST']['one']['trades']
    if mutation == 'pop':
        assert actual.get('rows') is None
    else:
        assert actual['rows'] == [{'id': 9}]
