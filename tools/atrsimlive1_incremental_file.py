"""Honest retained-FILE reuse differential, NEVER a current-RR certificate.

Main's exact typed loader owns decode/schema/source proof. This verifies the
actual retained bytes and binds both file SHA and the supplied decoded input.
No database, provider, callback or OLD-model invocation occurs here.
"""
from contextlib import contextmanager
from dataclasses import asdict
import hashlib
import os
from pathlib import Path
import stat

from atrsimlive1_incremental_cache import CacheRefusal, fingerprint, locked_cache, MAX_CACHE_BYTES, _sha
from atrsimlive1_incremental_models import IncrementalModels


def verify_saved_file(path, expected_sha256):
    path = Path(path)
    _sha(expected_sha256)
    if not path.is_absolute() or '..' in path.parts or any(part.is_symlink() for part in (path, *path.parents)):
        raise CacheRefusal('literal_retained_input_file_required')
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    checksum, size = hashlib.sha256(), 0
    with os.fdopen(fd, 'rb') as stream:
        before = os.fstat(stream.fileno())
        if (not stat.S_ISREG(before.st_mode) or before.st_mode & 0o022
                or before.st_uid not in (0, os.geteuid()) or not 0 < before.st_size <= MAX_CACHE_BYTES):
            raise CacheRefusal('retained_file_type_mode_or_cap')
        while block := stream.read(1024 * 1024):
            size += len(block)
            if size > MAX_CACHE_BYTES:
                raise CacheRefusal('retained_input_byte_cap')
            checksum.update(block)
        after = os.fstat(stream.fileno())
    identity = lambda value: (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns)
    if (identity(before) != identity(after) or identity(path.stat(follow_symlinks=False)) != identity(after)
            or size != before.st_size or checksum.hexdigest() != expected_sha256):
        raise CacheRefusal('retained_input_changed_or_hash_mismatch')
    return dict(scope='RETAINED_FILE_NOT_CURRENT_RR', artifact=str(path), sha256=expected_sha256, bytes=size)


class FileModels(IncrementalModels):
    def _proof(self, family, data, episodes, origins, route_packet, line_inputs):
        if family not in ('CORE', 'FIRSTBAR'):
            raise CacheRefusal('reviewed_whole_world_family_required')
        symbol, = data['symbols']
        witness = self.run.file_witnesses[symbol]
        identities = [asdict(episode.identity) for episode in episodes]
        source = dict(scope='RETAINED_FILE_NOT_CURRENT_RR', file=witness, data_sha256=fingerprint(data),
            route_sha256=fingerprint(route_packet), policies=self.run.binding['policies'],
            source_seal=self.run.binding['source_seal'], family=family, identities=identities, origins=origins)
        cohort = fingerprint(source)
        hashes = tuple(fingerprint(dict(source=source, identity=who)) for who in identities)
        proof = dict(identities=identities, origins=origins, closed=True, guards_known=True,
            cohort_sha256=cohort, window_hashes=hashes,
            bindings=tuple(fingerprint(dict(cohort=cohort, identity=who)) for who in identities),
            current_verification=dict(scope='RETAINED_FILE_NOT_CURRENT_RR', file=witness,
                                      decoded_input_sha256=source['data_sha256']))
        # Here 'closed' means immutable FILE input, not a finished position.
        # Original finished_known still rejects every modeled OPEN/UNKNOWN.
        self.contexts[symbol, family] = proof
        self.run.inputs[symbol] = [proof['current_verification']]
        return proof


class SavedFileRun:
    def __init__(self, store, witnesses):
        if store.binding['input_scope'] != 'RETAINED_FILE':
            raise CacheRefusal('retained_file_cache_scope_required')
        self.store, self.binding, self.file_witnesses = store, store.binding, witnesses
        self.previous = store.load() or {}
        self.next, self.audit, self.inputs = dict(worlds={}), [], {}
        self.model_controller = FileModels(self)


@contextmanager
def saved_file_session(root, binding, *, files, retention_enabled=False):
    """files maps literal symbol -> {path,sha256}; caller supplies decoded input."""
    if not files or len(files) > 256:
        raise CacheRefusal('bounded_actual_file_population_required')
    witnesses = {symbol: verify_saved_file(value['path'], value['sha256']) for symbol, value in files.items()}
    with locked_cache(root, binding, retention_enabled=retention_enabled) as store:
        run = SavedFileRun(store, witnesses)
        yield run
        if set(run.inputs) != set(files):
            raise CacheRefusal('all_saved_file_components_required')
        run.cache_write_receipt = store.save(run.next)
