"""Opt-in partial execution plumbing on the original collector/child lifecycle.

No own process, SQL or provider. Individual completed symbols are sealed with
the ORIGINAL EvidenceWriter; parent assembly waits for actual child closure.
The receipt is a NEW private schema, never current.json or a complete PASS.
"""
from datetime import datetime, timezone
import hashlib
import math
import os
from pathlib import Path
import stat
import time

from atrsimlive1_incremental_cache import CacheRefusal, fingerprint, _identity, _sha
from atrsimlive1_output import (EvidenceWriter, project_component, _chunks, _digest,
    _symbol, MAX_EVIDENCE_BYTES)
from atrsimlive1_partial import PartialRun, verify_component, _ClosedChild
from atrsimlive1_runtime import decode, MAX_BYTES, MAX_ROWS


REGISTER = 'deadline-roster.json'
RESULT = 'deadline-partial.json'
RUN_SCHEMA = 'ATRSIMLIVE1_CLOSED_DB_PARTIAL_ROSTER_V1'
CHILD_SCHEMA = 'ATRSIMLIVE1_DEADLINE_PARTIAL_CHILD_V1'


def _root(path):
    path = Path(path)
    if (not path.is_absolute() or '..' in path.parts
            or any(part.is_symlink() for part in (path, *path.parents))):
        raise CacheRefusal('literal_partial_run_root_required')
    info = path.stat(follow_symlinks=False)
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise CacheRefusal('owned_0700_partial_root_required')
    return path, (info.st_dev, info.st_ino)


def run_key(root, source_seal):
    root, identity = _root(root)
    return fingerprint(dict(root=str(root), root_identity=identity, source_seal=_sha(source_seal)))


def _write_new(root, name, value):
    # Original encoders, fixed source-owned filenames, exclusive/no replacement.
    root, identity = _root(root)
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        opened = os.fstat(fd)
        if (opened.st_dev, opened.st_ino) != identity:
            raise CacheRefusal('partial_root_changed')
        target = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=fd)
        total = 0
        try:
            with os.fdopen(target, 'wb') as stream:
                for raw in _chunks(value):
                    total += len(raw)
                    if total > MAX_BYTES:
                        raise CacheRefusal('partial_metadata_byte_cap')
                    stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            os.fsync(fd)
        except BaseException:
            # Keep failed evidence for Main; never unlink or adopt it.
            raise
    finally:
        os.close(fd)
    return total


def _read(root, name):
    root, identity = _root(root)
    directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        opened = os.fstat(directory)
        if (opened.st_dev, opened.st_ino) != identity:
            raise CacheRefusal('partial_root_changed')
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory)
        with os.fdopen(fd, 'rb') as stream:
            before = os.fstat(stream.fileno())
            if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.geteuid()
                    or stat.S_IMODE(before.st_mode) != 0o600 or before.st_nlink != 1
                    or not 0 < before.st_size <= MAX_BYTES):
                raise CacheRefusal('private_bounded_partial_metadata_required')
            raw = stream.read(MAX_BYTES + 1)
            after = os.fstat(stream.fileno())
        current = os.stat(name, dir_fd=directory, follow_symlinks=False)
        if _identity(before) != _identity(after) or _identity(current) != _identity(after) or len(raw) != before.st_size:
            raise CacheRefusal('partial_metadata_changed')
        return decode(raw), len(raw), hashlib.sha256(raw).hexdigest()
    finally:
        os.close(directory)


class PartialWriter:
    def __init__(self, root, *, source_seal, budget_seconds):
        if (type(budget_seconds) not in (int, float) or not math.isfinite(budget_seconds)
                or not 0 < budget_seconds < 240):
            raise CacheRefusal('partial_soft_budget_below_original_hard_deadline_required')
        self.root, self.root_identity = _root(root)
        self.source_seal = _sha(source_seal)
        self.run_id = run_key(root, source_seal)
        self.began, self.budget = time.monotonic(), budget_seconds
        self.metadata = None
        self.completed = []
        self.raw_bytes = self.gzip_bytes = self.projected_bytes = self.rows = 0

    def bind_roster(self, *, day, source_asof, snapshot_id, roster):
        if (self.metadata is not None or not isinstance(roster, (list, tuple)) or not 1 <= len(roster) <= 256
                or len(set(roster)) != len(roster) or source_asof.utcoffset() is None
                or not isinstance(snapshot_id, str) or not snapshot_id):
            raise CacheRefusal('actual_closed_DB_snapshot_and_full_roster_required')
        names = [_symbol(symbol) for symbol in roster]
        self.metadata = dict(schema=RUN_SCHEMA, run_id=self.run_id, source_seal=self.source_seal,
            child_pid=os.getpid(), trading_day=day, source_asof=source_asof.isoformat(),
            snapshot_hash=_digest(snapshot_id), original_symbols=names,
            registration_basis='ORIGINAL_RR_ROLLBACK_AND_CONNECTION_CLOSED_BEFORE_MODEL_LOOP',
            native_execution_qualified=False, soft_budget_seconds=self.budget)
        _write_new(self.root, REGISTER, self.metadata)

    def stop_before_next_symbol(self):
        if self.metadata is None:
            raise CacheRefusal('full_roster_must_precede_partial_deadline_check')
        return time.monotonic() - self.began >= self.budget

    def process(self, data, model):
        if self.metadata is None or len(data['symbols']) != 1:
            raise CacheRefusal('original_one_symbol_partial_packet_required')
        number = len(self.completed)
        if data['symbols'] != [self.metadata['original_symbols'][number]]:
            raise CacheRefusal('original_ordered_partial_population_required')
        if (data['trading_day'] != self.metadata['trading_day']
                or data['source_asof'].isoformat() != self.metadata['source_asof']
                or _digest(data['snapshot_id']) != self.metadata['snapshot_hash']):
            raise CacheRefusal('same_actual_partial_snapshot_required')
        directory = self.root / ('symbol-' + str(number).zfill(3))
        os.mkdir(directory, 0o700)  # No reuse, no retry, no user-derived path.
        with EvidenceWriter(directory, remaining_bytes=MAX_EVIDENCE_BYTES - self.raw_bytes) as writer:
            writer.append('input', data, symbol=data['symbols'][0])
            original = model(data)
            writer.append('model', original, symbol=data['symbols'][0])
            component = project_component(original)
            evidence = writer.seal()
        raw_total = self.raw_bytes + evidence['uncompressed_bytes']
        gzip_total = self.gzip_bytes + evidence['bytes']
        if max(raw_total, gzip_total) > MAX_EVIDENCE_BYTES:
            raise CacheRefusal('whole_run_evidence_cap_not_per_symbol_cap')
        count = sum(len(table['rows']) for table in component['tables'].values())
        projected = sum(len(raw) for raw in _chunks(component))
        if self.rows + count > MAX_ROWS or self.projected_bytes + projected > MAX_BYTES:
            raise CacheRefusal('whole_partial_row_or_projection_cap')
        value = dict(schema='ATRSIMLIVE1_SEALED_SYMBOL_MANIFEST_V1', run_id=self.run_id,
            source_seal=self.source_seal, symbol=data['symbols'][0], ordinal=number,
            directory=directory.name, component=component, evidence=dict(evidence))
        # Seal/readback precede exposing the completed manifest. If killed
        # before this exclusive write finishes, parent refuses that manifest.
        verify_component(component, run_directory=directory, evidence=evidence)
        _write_new(self.root, 'completed-' + str(number).zfill(3) + '.json', value)
        self.completed.append(data['symbols'][0])
        self.raw_bytes, self.gzip_bytes = raw_total, gzip_total
        self.rows += count
        self.projected_bytes += projected
        return component

    def finish(self):
        if self.metadata is None:
            raise CacheRefusal('partial_no_closed_DB_roster_no_receipt')
        return dict(schema=CHILD_SCHEMA, run_id=self.run_id, source_seal=self.source_seal,
            completed_symbols=list(self.completed), pending_symbols=self.metadata['original_symbols'][len(self.completed):],
            elapsed_seconds=time.monotonic() - self.began,
            deadline_kind='COOPERATIVE_SYMBOL_BOUNDARY_NOT_INFLIGHT_PREEMPTION',
            native_execution_qualified=False)


def finalize(root, *, source_seal, closure, stop_reason, child_document=None):
    """Parent-only FILE assembly AFTER the existing owned child actually ends."""
    if stop_reason not in ('COOPERATIVE_CHILD_EXIT', 'ACTUAL_CHILD_WALL_TIMEOUT'):
        raise CacheRefusal('literal_partial_stop_reason_required')
    metadata, _, metadata_sha = _read(root, REGISTER)
    expected_id = run_key(root, source_seal)
    if (metadata.get('schema') != RUN_SCHEMA or metadata.get('run_id') != expected_id
            or metadata.get('source_seal') != source_seal or metadata.get('child_pid') != closure['pid']
            or closure.get('run_id') != expected_id or closure.get('source_seal') != source_seal
            or metadata.get('native_execution_qualified') is not False
            or metadata.get('registration_basis') != 'ORIGINAL_RR_ROLLBACK_AND_CONNECTION_CLOSED_BEFORE_MODEL_LOOP'):
        raise CacheRefusal('actual_same_run_source_child_and_closed_DB_registration_required')
    if stop_reason == 'COOPERATIVE_CHILD_EXIT':
        if (closure['returncode'] != 0 or not isinstance(child_document, dict)
                or child_document.get('schema') != CHILD_SCHEMA or child_document.get('run_id') != expected_id
                or child_document.get('source_seal') != source_seal):
            raise CacheRefusal('original_successful_partial_child_receipt_required')
    collector = PartialRun(metadata['original_symbols'], trading_day=metadata['trading_day'],
        source_asof=metadata['source_asof'], snapshot_hash=metadata['snapshot_hash'], source_seal=source_seal,
        child_pid=metadata['child_pid'], run_id=expected_id, enabled=True)
    artifacts, total_metadata_bytes, rows = [], 0, 0
    for ordinal, symbol in enumerate(metadata['original_symbols']):
        name = 'completed-' + str(ordinal).zfill(3) + '.json'
        try:
            value, size, checksum = _read(root, name)
        except FileNotFoundError:
            break  # Later names remain pending, never empty/never-traded.
        total_metadata_bytes += size
        if (total_metadata_bytes > MAX_BYTES or value.get('schema') != 'ATRSIMLIVE1_SEALED_SYMBOL_MANIFEST_V1'
                or value.get('ordinal') != ordinal or value.get('symbol') != symbol
                or value.get('run_id') != expected_id or value.get('source_seal') != source_seal
                or value.get('directory') != 'symbol-' + str(ordinal).zfill(3)):
            raise CacheRefusal('exact_ordered_sealed_symbol_manifest_required')
        verified = verify_component(value['component'], run_directory=Path(root) / value['directory'], evidence=value['evidence'])
        rows += sum(len(table['rows']) for table in value['component']['tables'].values())
        if rows > MAX_ROWS:
            raise CacheRefusal('whole_partial_row_or_projection_cap')
        collector.add(verified)
        artifacts.append(dict(name=name, sha256=checksum))
    result = collector.assemble(closure, allow_all_pending=True)
    if child_document is not None and (child_document.get('completed_symbols') != result['completed_symbols']
            or child_document.get('pending_symbols') != result['pending_symbols']):
        raise CacheRefusal('child_parent_completed_pending_population_mismatch')
    result.update(stop_reason=stop_reason, qualification='INCOMPLETE_NOT_A_FULL_RUN_PASS',
        roster_manifest_sha256=metadata_sha, completed_manifest_hashes=artifacts,
        assembled_at=datetime.now(timezone.utc).isoformat())
    _write_new(root, RESULT, result)
    return result


def record_timeout(root, *, closure, source_seal, exception_class='TimeoutError'):
    """Retain actual failure, not raw exception/SQL/provider stringification."""
    if (type(closure) is not _ClosedChild or closure.get('run_id') != run_key(root, source_seal)
            or exception_class not in ('TimeoutError', 'TimeoutExpired')):
        raise CacheRefusal('actual_owned_child_timeout_and_literal_class_required')
    _write_new(root, 'deadline-timeout.json', dict(schema='ATRSIMLIVE1_ACTUAL_CHILD_TIMEOUT_V1',
        exception_class=exception_class,
        literal_code='today_wall_deadline' if exception_class == 'TimeoutError' else 'owned_child_wait_deadline',
        source_seal=_sha(source_seal), actual_child_closure=dict(closure),
        full_run_qualified=False, retry_authorized=False))
