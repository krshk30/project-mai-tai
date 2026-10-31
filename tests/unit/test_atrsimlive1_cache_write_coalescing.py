"""CONTROLLED write-path byte identity/call counts, not a timing benchmark.

Parent32f2 wire recipe is reproduced independently of the new coalescer and
position bookkeeping. No model, DB, provider, process or retained payload.
"""
from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import importlib
from pathlib import Path
from uuid import UUID

import pytest


def context(monkeypatch, tmp_path):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2] / 'tools'))
    cache = importlib.import_module('atrsimlive1_incremental_cache')
    container = importlib.import_module('atrsimlive1_incremental_container')
    root = (tmp_path / 'private-v2-cache').resolve()
    root.mkdir(mode=0o700)
    binding = dict(version=cache.INCREMENTAL_V1, trading_day='2026-10-28', source_seal='a' * 64,
        input_scope='CURRENT_RR', policies=dict(execution_rules_sha256='b' * 64,
                                               null_policy=None, same_sip_policy=None))
    return cache, container, cache.CacheStore(root, binding)


def fixture_body():
    certificate = dict(row_count=1, content_sha256='c' * 64)
    rows = [dict(id=7, note='CONTROLLED:é/雪/\\\n', conditions=None, size=100,
                 price=Decimal('1.5300'), at=datetime(2026, 10, 28, 12, tzinfo=timezone.utc),
                 uid=UUID('11111111-1111-4111-8111-111111111111'))]
    raw = dict(certificate=certificate, rows=rows, hashes={'7': 'd' * 64})
    world = dict(member_keys=['e' * 64], bindings=['f' * 64], entries=[{'literal': 'CONTROLLED'}],
                 typed_results={'literal': ['CONTROLLED', None, True, 100]}, typed_reports=[])
    return dict(windows={'CONTROLLED': {'one': dict(start_ns=1, end_key=[2, 7],
                trades=deepcopy(raw), quotes=deepcopy(raw))}},
                worlds={'CONTROLLED|FIRSTBAR': world}, roster=['CONTROLLED'])


def parent_wire(cache, container, binding, body):
    """Literal parent32f2 layout/encoding, no call to new save/_parts/batching."""
    skeleton, sections = {}, []
    for key, value in body.items():
        if key == 'worlds':
            skeleton[key] = {}
            for slot, world in value.items():
                names = {'member_keys', 'bindings', 'last_verification'} & set(world)
                skeleton[key][slot] = {name: world[name] for name in names}
                sections.append((['worlds', slot], {name: part for name, part in world.items() if name not in names}))
        elif key == 'windows':
            skeleton[key] = {}
            for symbol, windows in value.items():
                target = skeleton[key][symbol] = {}
                for origin, state in windows.items():
                    target[origin] = {name: part for name, part in state.items() if name not in ('trades', 'quotes')}
                    for kind in ('trades', 'quotes'):
                        target[origin][kind] = {'certificate': state[kind]['certificate']}
                        sections.append((['windows', symbol, origin, kind],
                                         {name: part for name, part in state[kind].items() if name != 'certificate'}))
        else:
            skeleton[key] = value
    output, descriptors = bytearray(bytes(container.HEADER.size)), []
    for path, payload in sections:
        raw = b''.join(part.encode('utf-8') for part in cache.chunks(payload))
        descriptors.append(dict(path=path, offset=len(output), length=len(raw), sha256=hashlib.sha256(raw).hexdigest()))
        output.extend(raw)
    start = len(output)
    document = dict(schema=container.SCHEMA, binding=binding, body=skeleton, sections=descriptors)
    raw = b''.join(part.encode('utf-8') for part in cache.chunks(document))
    output.extend(raw)
    output[:container.HEADER.size] = container.HEADER.pack(container.MAGIC, start, len(raw), hashlib.sha256(raw).digest())
    return bytes(output)


@pytest.mark.parametrize('batch', [1, 7, 64, 1024 * 1024])
def test_coalesced_container_is_exact_parent32f2_wire(monkeypatch, tmp_path, batch):
    cache, container, store = context(monkeypatch, tmp_path)
    body = fixture_body()
    expected = parent_wire(cache, container, store.binding, body)
    monkeypatch.setattr(container, 'BLOCK', batch)
    receipt = store.save(body)
    actual = (store.root / 'cache.json').read_bytes()
    assert actual == expected
    assert receipt['bytes'] == len(expected)
    assert receipt['sha256'] == receipt['readback_sha256'] == hashlib.sha256(expected).hexdigest()
    recovered = store.load()
    assert recovered['windows']['CONTROLLED']['one']['trades']['rows'][0]['price'] == Decimal('1.5300')
    assert recovered['worlds']['CONTROLLED|FIRSTBAR']['typed_results'] == {'literal': ['CONTROLLED', None, True, 100]}


@pytest.mark.parametrize('pieces', [[], [b''], [b'a', b'bc', b'def', b'ghij'],
                                   [b'a', b'bcdefghijklmnopq', b'r'], [b'abcdefgh', b'ij']])
def test_batch_boundaries_keep_exact_bytes_without_small_nonfinal_blocks(monkeypatch, tmp_path, pieces):
    _, container, _ = context(monkeypatch, tmp_path)
    monkeypatch.setattr(container, 'BLOCK', 8)
    blocks = list(container._coalesced(iter(pieces)))
    assert b''.join(blocks) == b''.join(pieces)
    assert all(len(block) >= 8 for block in blocks[:-1])
    assert all(len(block) > 0 for block in blocks)


class TrackedStream:
    def __init__(self, actual, counts):
        self.actual, self.counts = actual, counts

    def __enter__(self):
        self.actual.__enter__()
        return self

    def __exit__(self, *args):
        return self.actual.__exit__(*args)

    def __getattr__(self, name):
        return getattr(self.actual, name)

    def write(self, block):
        self.counts['write'] += 1
        return self.actual.write(block)

    def tell(self):
        self.counts['tell'] += 1
        return self.actual.tell()


def test_tiny_tokens_do_not_call_file_write_tell_per_token(monkeypatch, tmp_path):
    cache, container, store = context(monkeypatch, tmp_path)
    monkeypatch.setattr(container, 'BLOCK', 64 * 1024)
    body = {'controlled': [dict(id=number, value='token') for number in range(5000)]}
    expected = parent_wire(cache, container, store.binding, body)
    counts, original = dict(write=0, tell=0), container.os.fdopen
    def observed(fd, mode, *args, **kwargs):
        actual = original(fd, mode, *args, **kwargs)
        return TrackedStream(actual, counts) if mode == 'w+b' else actual
    monkeypatch.setattr(container.os, 'fdopen', observed)
    store.save(body)
    assert (store.root / 'cache.json').read_bytes() == expected
    assert counts['tell'] == 0
    assert counts['write'] < 10  # bounded data blocks + placeholder/final header.


@pytest.mark.parametrize('cap', ['file', 'index'])
def test_coalesced_cap_refusal_precedes_oversize_write_and_keeps_active(monkeypatch, tmp_path, cap):
    cache, container, store = context(monkeypatch, tmp_path)
    store.save({'old': True})
    active = store.root / 'cache.json'
    before = active.read_bytes()
    monkeypatch.setattr(container, 'BLOCK', 32)
    if cap == 'file':
        monkeypatch.setattr(cache, 'MAX_CACHE_BYTES', len(before) + 10)
        expected = 'cache_file_byte_cap'
    else:
        _, _, old_index_length, _ = container.HEADER.unpack(before[:container.HEADER.size])
        monkeypatch.setattr(container, 'MAX_INDEX_BYTES', old_index_length + 10)
        expected = 'cache_index_byte_cap'
    with pytest.raises(cache.CacheRefusal, match=expected):
        store.save({'new': 'x' * (len(before) + 100)})
    assert active.read_bytes() == before
    assert not (store.root / '.next.json').exists()


def test_unread_copy_remains_lazy_and_byte_verified(monkeypatch, tmp_path):
    _, container, store = context(monkeypatch, tmp_path)
    store.save(fixture_body())
    loaded = store.load()
    monkeypatch.setattr(container.Section, 'decode', lambda _: pytest.fail('unrequested section decoded'))
    store.save(loaded)
    assert not loaded['worlds']['CONTROLLED|FIRSTBAR'].loaded
    assert (store.root / 'cache.json').stat().st_mode & 0o777 == 0o600


def test_late_copy_integrity_refusal_cannot_publish_buffered_partial(monkeypatch, tmp_path):
    cache, container, store = context(monkeypatch, tmp_path)
    store.save(fixture_body())
    before = (store.root / 'cache.json').read_bytes()
    loaded = store.load()
    original = container.Section.blocks
    def refused(section):
        yield from original(section)
        raise cache.CacheRefusal('CONTROLLED_source_copy_refusal')
    monkeypatch.setattr(container.Section, 'blocks', refused)
    with pytest.raises(cache.CacheRefusal, match='CONTROLLED_source_copy_refusal'):
        store.save(loaded)
    assert (store.root / 'cache.json').read_bytes() == before
    assert not (store.root / '.next.json').exists()
