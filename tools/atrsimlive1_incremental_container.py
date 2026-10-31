"""V2 byte-verified cache container. No model admission or executable codecs.

The index and each deferred JSON section are integrity-checked by their exact
serialized byte digest BEFORE parsing. An unvisited section is not claimed
verified. All content stays in the single bounded, atomically replaced file.
"""
import hashlib
import json
import math
import os
import struct
import atrsimlive1_incremental_cache as guards

from atrsimlive1_incremental_cache import (
    CacheRefusal, _identity, _regular, _sha,
    _pairs, _constant, _validate_values, chunks,
)


MAGIC = b'ATRSIMCACHEV2\x00\x00\x00'
HEADER = struct.Struct('!16sQQ32s')
SCHEMA = 'ATRSIMLIVE1_PRIVATE_INCREMENTAL_BYTE_CONTAINER_V2'
MAX_INDEX_BYTES = 8 * 1024 * 1024
BLOCK = 1024 * 1024


def _coalesced(pieces):
    """Same bytes/order, BLOCK-sized batches except the final tail.

    Encoder fragments can be single punctuation tokens. Batch those without
    reparsing/re-encoding; an already-large block passes through. Pending
    bytes never exceed BLOCK (one extra BLOCK during immutable conversion).
    """
    pending = bytearray()
    for piece in pieces:
        if not pending and len(piece) >= BLOCK:
            yield piece
        elif len(pending) + len(piece) < BLOCK:
            pending.extend(piece)
        else:
            needed = BLOCK - len(pending)
            view = memoryview(piece)
            pending.extend(view[:needed])
            block = bytes(pending)
            pending.clear()
            yield block
            del block
            remaining = view[needed:]
            if len(remaining) >= BLOCK:
                yield remaining
            else:
                pending.extend(remaining)
            del remaining, view
    if pending:
        yield bytes(pending)


def _decode(raw):
    try:
        value = json.loads(raw, object_pairs_hook=_pairs, parse_constant=_constant)
    except (RecursionError, UnicodeError, json.JSONDecodeError) as error:
        raise CacheRefusal('cache_strict_json_refused') from error
    _validate_decoded(value)
    return value


def _validate_decoded(value, depth=0):
    # Native tags have already become safe datetime/Decimal/UUID scalars.
    if depth > 96:
        raise CacheRefusal('cache_nesting_cap')
    if isinstance(value, float) and not math.isfinite(value):
        raise CacheRefusal('nonfinite_cache_number')
    if isinstance(value, dict):
        for child in value.values():
            _validate_decoded(child, depth + 1)
    elif isinstance(value, list):
        for child in value:
            _validate_decoded(child, depth + 1)


class Section:
    def __init__(self, store, identity, descriptor):
        self.store, self.identity, self.descriptor = store, identity, descriptor

    def blocks(self):
        with self.store.directory() as directory:
            fd = os.open('cache.json', os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory)
            with os.fdopen(fd, 'rb') as stream:
                before = os.fstat(stream.fileno())
                _regular(before)
                if _identity(before) != self.identity:
                    raise CacheRefusal('cache_section_generation_changed')
                stream.seek(self.descriptor['offset'])
                remaining, digest = self.descriptor['length'], hashlib.sha256()
                while remaining:
                    block = stream.read(min(BLOCK, remaining))
                    if not block:
                        raise CacheRefusal('cache_section_truncated')
                    remaining -= len(block)
                    digest.update(block)
                    yield block
                after = os.fstat(stream.fileno())
                current = os.stat('cache.json', dir_fd=directory, follow_symlinks=False)
                if (_identity(after) != self.identity or _identity(current) != self.identity
                        or digest.hexdigest() != self.descriptor['sha256']):
                    raise CacheRefusal('cache_section_bytes_changed')

    def decode(self):
        # Exhaust verification before handing a byte to the JSON decoder.
        raw = b''.join(self.blocks())
        value = _decode(raw)
        if not isinstance(value, dict):
            raise CacheRefusal('cache_section_mapping_required')
        return value


class SectionMapping(dict):
    """Small metadata stays eager; requested payload is verified and decoded.

    Once materialized it is ordinary mutable caller data, so save reserializes
    it. Only never-materialized sections may be copied by verified raw bytes.
    """
    def __init__(self, metadata, section):
        super().__init__(metadata)
        self.section = section
        self.loaded = False

    def materialize(self):
        if not self.loaded:
            value = self.section.decode()
            if set(value).intersection(dict.keys(self)):
                raise CacheRefusal('cache_section_metadata_collision')
            dict.update(self, value)
            self.loaded = True
        return self

    def __getitem__(self, key):
        if not dict.__contains__(self, key):
            self.materialize()
        return dict.__getitem__(self, key)

    def get(self, key, default=None):
        if not dict.__contains__(self, key):
            self.materialize()
        return dict.get(self, key, default)

    def __setitem__(self, key, value):
        self.materialize()
        return dict.__setitem__(self, key, value)

    def __delitem__(self, key):
        self.materialize()
        return dict.__delitem__(self, key)

    def update(self, *args, **kwargs):
        self.materialize()
        return dict.update(self, *args, **kwargs)

    def setdefault(self, *args):
        self.materialize()
        return dict.setdefault(self, *args)

    def pop(self, *args):
        self.materialize()
        return dict.pop(self, *args)

    def popitem(self):
        self.materialize()
        return dict.popitem(self)

    def clear(self):
        self.materialize()
        return dict.clear(self)

    def __ior__(self, other):
        self.update(other)
        return self

    def __iter__(self):
        self.materialize()
        return dict.__iter__(self)

    def __len__(self):
        self.materialize()
        return dict.__len__(self)

    def __contains__(self, key):
        self.materialize()
        return dict.__contains__(self, key)

    def keys(self):
        self.materialize()
        return dict.keys(self)

    def items(self):
        self.materialize()
        return dict.items(self)

    def values(self):
        self.materialize()
        return dict.values(self)

    def copy(self):
        self.materialize()
        return dict.copy(self)

    def __eq__(self, other):
        self.materialize()
        if isinstance(other, SectionMapping):
            other.materialize()
        return dict.__eq__(self, other)

    def __ne__(self, other):
        result = self.__eq__(other)
        return NotImplemented if result is NotImplemented else not result


def copied_world(value, **metadata):
    # Avoid dict(subclass) silently copying only an unloaded index skeleton.
    if isinstance(value, SectionMapping):
        value.materialize()
    return dict(value, **metadata)


def load(store):
    with store.directory() as directory:
        try:
            fd = os.open('cache.json', os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory)
        except FileNotFoundError:
            return None
        with os.fdopen(fd, 'rb') as stream:
            before = os.fstat(stream.fileno())
            _regular(before)
            if not HEADER.size < before.st_size <= guards.MAX_CACHE_BYTES:
                raise CacheRefusal('cache_file_byte_cap')
            header = stream.read(HEADER.size)
            if len(header) != HEADER.size:
                raise CacheRefusal('cache_container_header_required')
            magic, offset, length, expected = HEADER.unpack(header)
            if (magic != MAGIC or offset < HEADER.size or not 0 < length <= MAX_INDEX_BYTES
                    or offset + length != before.st_size):
                raise CacheRefusal('cache_V2_container_new_namespace_required')
            stream.seek(offset)
            raw = stream.read(length)
            after = os.fstat(stream.fileno())
        current = os.stat('cache.json', dir_fd=directory, follow_symlinks=False)
        if (_identity(before) != _identity(after) or _identity(current) != _identity(after)
                or len(raw) != length or hashlib.sha256(raw).digest() != expected):
            raise CacheRefusal('cache_index_bytes_changed')
    document = _decode(raw)
    if (not isinstance(document, dict) or set(document) != {'schema', 'binding', 'body', 'sections'}
            or document['schema'] != SCHEMA or document['binding'] != store.binding
            or not isinstance(document['body'], dict) or not isinstance(document['sections'], list)):
        raise CacheRefusal('cache_source_policy_day_mismatch')
    body, end, paths = document['body'], HEADER.size, set()
    for item in document['sections']:
        if (not isinstance(item, dict) or set(item) != {'path', 'offset', 'length', 'sha256'}
                or not isinstance(item['path'], list) or not 2 <= len(item['path']) <= 4
                or any(not isinstance(part, str) or not part for part in item['path'])
                or type(item['offset']) is not int or type(item['length']) is not int
                or item['offset'] != end or item['length'] <= 0
                or item['offset'] + item['length'] > offset):
            raise CacheRefusal('cache_section_geometry_refused')
        _sha(item['sha256'])
        path = tuple(item['path'])
        if path in paths or not _allowed(path, raw_pool=guards.features(store.binding)['raw_pool']):
            raise CacheRefusal('cache_section_path_refused')
        paths.add(path)
        parent = body
        for part in path[:-1]:
            if not isinstance(parent, dict) or part not in parent:
                raise CacheRefusal('cache_section_parent_missing')
            parent = parent[part]
        if not isinstance(parent, dict) or not isinstance(parent.get(path[-1]), dict):
            raise CacheRefusal('cache_section_metadata_required')
        parent[path[-1]] = SectionMapping(parent[path[-1]], Section(store, _identity(after), item))
        end += item['length']
    if end != offset:
        raise CacheRefusal('cache_unindexed_bytes_refused')
    return body


def _allowed(path, *, raw_pool=False):
    return ((len(path) == 2 and path[0] == 'worlds')
            or (raw_pool and len(path) == 2 and path[0] == 'raw_pools')
            or (len(path) == 4 and path[0] == 'windows' and path[3] in ('trades', 'quotes')))


def _parts(body):
    # Only established cache population paths are split. No arbitrary code tags.
    if not isinstance(body, dict):
        raise CacheRefusal('cache_body_mapping_required')
    index, sections = {}, []
    for key, value in body.items():
        if key == 'worlds':
            index[key] = {}
            for slot, world in value.items():
                metadata_keys = ('member_keys', 'bindings', 'last_verification')
                metadata = {name: dict.__getitem__(world, name) for name in metadata_keys if dict.__contains__(world, name)}
                index[key][slot] = metadata
                sections.append((['worlds', slot], world, set(metadata)))
        elif key == 'windows':
            index[key] = {}
            for symbol, windows in value.items():
                target = index[key][symbol] = {}
                for origin, state in windows.items():
                    target[origin] = {name: item for name, item in state.items() if name not in ('trades', 'quotes')}
                    for kind in ('trades', 'quotes'):
                        payload = state[kind]
                        target[origin][kind] = {'certificate': payload['certificate']}
                        sections.append((['windows', symbol, origin, kind], payload, {'certificate'}))
        elif key == 'raw_pools':
            index[key] = {}
            for name, pool in value.items():
                index[key][name] = {}
                sections.append((['raw_pools', name], pool, set()))
        else:
            index[key] = value
    return index, sections


def save(store, body):
    # Validate any existing namespace before creating a staging file. V1 or
    # foreign bindings must remain untouched, even after a caller fresh fallback.
    load(store)
    skeleton, sections = _parts(body)
    with store.directory() as directory:
        fd = os.open('.next.json', os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
        created = os.fstat(fd)
        try:
            with os.fdopen(fd, 'w+b') as stream:
                _regular(created)
                stream.write(bytes(HEADER.size))
                descriptors = []
                position = HEADER.size
                def write(block):
                    nonlocal position
                    if position + len(block) > guards.MAX_CACHE_BYTES:
                        raise CacheRefusal('cache_file_byte_cap')
                    stream.write(block)
                    position += len(block)
                for path, value, metadata_keys in sections:
                    start, digest = position, hashlib.sha256()
                    if isinstance(value, SectionMapping) and not value.loaded:
                        pieces = value.section.blocks()
                    else:
                        payload = {key: item for key, item in value.items() if key not in metadata_keys}
                        _validate_values(payload)
                        pieces = (chunk.encode('utf-8') for chunk in chunks(payload))
                    for block in _coalesced(pieces):
                        write(block)
                        digest.update(block)
                    descriptors.append(dict(path=path, offset=start, length=position - start,
                                            sha256=digest.hexdigest()))
                index_start, index_digest = position, hashlib.sha256()
                document = dict(schema=SCHEMA, binding=store.binding, body=skeleton, sections=descriptors)
                _validate_values(document)
                for block in _coalesced(chunk.encode('utf-8') for chunk in chunks(document)):
                    if position - index_start + len(block) > MAX_INDEX_BYTES:
                        raise CacheRefusal('cache_index_byte_cap')
                    write(block)
                    index_digest.update(block)
                total, index_length = position, position - index_start
                stream.seek(0)
                header = HEADER.pack(MAGIC, index_start, index_length, index_digest.digest())
                stream.write(header)
                stream.flush()
                os.fsync(stream.fileno())
                stream.seek(0)
                if stream.read(HEADER.size) != header:
                    raise CacheRefusal('cache_write_header_readback_mismatch')
                # Raw-byte readback of every written section and index. No JSON
                # decoding/re-encoding to manufacture an integrity checksum.
                for item in [*descriptors, dict(offset=index_start, length=index_length,
                                               sha256=index_digest.hexdigest())]:
                    stream.seek(item['offset'])
                    remaining, actual = item['length'], hashlib.sha256()
                    while remaining:
                        block = stream.read(min(BLOCK, remaining))
                        if not block:
                            raise CacheRefusal('cache_write_truncated')
                        actual.update(block)
                        remaining -= len(block)
                    if actual.hexdigest() != item['sha256']:
                        raise CacheRefusal('cache_write_readback_hash_mismatch')
                stream.seek(0)
                checksum = hashlib.sha256()
                while block := stream.read(BLOCK):
                    checksum.update(block)
                actual_identity = _identity(os.fstat(stream.fileno()))
            current = os.stat('.next.json', dir_fd=directory, follow_symlinks=False)
            if _identity(current) != actual_identity:
                raise CacheRefusal('cache_stage_changed')
            os.replace('.next.json', 'cache.json', src_dir_fd=directory, dst_dir_fd=directory)
            os.fsync(directory)
            return dict(bytes=total, sha256=checksum.hexdigest(), readback_sha256=checksum.hexdigest(),
                        container_schema=SCHEMA)
        finally:
            try:
                current = os.stat('.next.json', dir_fd=directory, follow_symlinks=False)
                if (current.st_dev, current.st_ino) == (created.st_dev, created.st_ino):
                    os.unlink('.next.json', dir_fd=directory)
            except FileNotFoundError:
                pass
