"""Default-OFF partial-run assembly. Never promotes an interrupted cache.

Parent integration must use the EXISTING owned child after its actual wait and
pipe closure, and individually sealed/read-back symbol evidence. This module
does not launch children, invoke models, publish files, or change deadlines.
"""
from copy import deepcopy
from datetime import date, datetime
import hashlib
import os
from pathlib import Path
import stat
import subprocess
import time

from atrsimlive1_incremental_cache import CacheRefusal, _identity, _sha
from atrsimlive1_output import KEYS, SCHEMA, NAME, EVIDENCE_SCHEMA, MAX_EVIDENCE_BYTES


PARTIAL_V1 = 'ATRSIMLIVE1_EXPLICIT_COMPLETED_PENDING_V1'


class _ClosedChild(dict):
    """Only observe_closed_child creates the in-process observation marker."""


class _VerifiedComponent(dict):
    """A local sealed-file byte readback, not a bare JSON success flag."""


def observe_closed_child(process, *, run_id, source_seal, allow_failed=False):
    # No fake process/returncode/closed=True port accepted. The caller owns the
    # actual Popen in existing bounded_child; ownership is a source obligation.
    if type(process) is not subprocess.Popen or type(process.pid) is not int or process.pid <= 0:
        raise CacheRefusal('actual_owned_Popen_required')
    _sha(run_id)
    _sha(source_seal)
    if type(allow_failed) is not bool:
        raise CacheRefusal('explicit_failed_child_observation_policy_required')
    observed = process.poll()
    if (type(observed) is not int or process.returncode != observed or not allow_failed and observed != 0
            or process.stdout is None or process.stderr is None
            or not process.stdout.closed or not process.stderr.closed):
        raise CacheRefusal('child_not_successfully_joined_and_pipes_closed')
    return _ClosedChild(pid=process.pid, returncode=observed,
                        observed_at_ns=time.time_ns(), pipes_closed=True,
                        run_id=run_id, source_seal=source_seal, successful_exit=observed == 0)


def verify_component(component, *, run_directory, evidence):
    """Verify a closed writer artifact without expanding/replaying its payload.

    The projected component still comes from the reviewed projection validator;
    Main's parent hook must bind its producer/source manifest independently.
    """
    if (not isinstance(component, dict) or component.get('schema') != SCHEMA
            or component.get('native_execution_qualified') is not False
            or set(component.get('tables', {})) != set(KEYS)):
        raise CacheRefusal('reviewed_projected_component_required')
    scope = component.get('component', {})
    symbols = scope.get('symbols')
    if (not isinstance(symbols, list) or len(symbols) != 1 or type(symbols[0]) is not str
            or not symbols[0] or not isinstance(scope.get('input_hashes'), dict)
            or not scope['input_hashes']):
        raise CacheRefusal('one_original_symbol_and_input_hashes_required')
    _sha(scope.get('snapshot_hash'))
    for checksum in scope['input_hashes'].values():
        _sha(checksum)
    symbol = symbols[0]
    if (not isinstance(evidence, dict) or evidence.get('schema') != EVIDENCE_SCHEMA
            or evidence.get('file') != NAME or evidence.get('records') != 2
            or evidence.get('records_by_kind') != {'input': 1, 'model': 1}
            or evidence.get('records_by_symbol') != {symbol: {'input': 1, 'model': 1}}
            or type(evidence.get('bytes')) is not int or not 0 < evidence['bytes'] <= MAX_EVIDENCE_BYTES
            or type(evidence.get('uncompressed_bytes')) is not int
            or not 0 < evidence['uncompressed_bytes'] <= MAX_EVIDENCE_BYTES):
        raise CacheRefusal('complete_sealed_single_symbol_evidence_required')
    _sha(evidence.get('sha256'))
    _sha(evidence.get('uncompressed_sha256'))
    root = Path(run_directory)
    if (not root.is_absolute() or '..' in root.parts
            or any(path.is_symlink() for path in (root, *root.parents))):
        raise CacheRefusal('literal_private_component_directory_required')
    rootfd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        info = os.fstat(rootfd)
        if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise CacheRefusal('owned_0700_component_directory_required')
        fd = os.open(NAME, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=rootfd)
        digest, count = hashlib.sha256(), 0
        with os.fdopen(fd, 'rb') as stream:
            before = os.fstat(stream.fileno())
            if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.geteuid()
                    or stat.S_IMODE(before.st_mode) != 0o600 or before.st_nlink != 1
                    or before.st_size != evidence['bytes']):
                raise CacheRefusal('private_sealed_component_file_required')
            while block := stream.read(1024 * 1024):
                count += len(block)
                if count > evidence['bytes']:
                    raise CacheRefusal('sealed_component_byte_cap')
                digest.update(block)
            after = os.fstat(stream.fileno())
        current = os.stat(NAME, dir_fd=rootfd, follow_symlinks=False)
        if (_identity(before) != _identity(after) or _identity(after) != _identity(current)
                or count != evidence['bytes'] or digest.hexdigest() != evidence['sha256']):
            raise CacheRefusal('sealed_component_changed_or_hash_mismatch')
    finally:
        os.close(rootfd)
    return _VerifiedComponent(symbol=symbol, component=deepcopy(component), evidence=deepcopy(evidence),
                              artifact=str(root / NAME), directory_identity=(info.st_dev, info.st_ino),
                              file_identity=_identity(after))


class PartialRun:
    def __init__(self, roster, *, trading_day, source_asof, snapshot_hash, source_seal,
                 child_pid=None, run_id=None, enabled=False):
        if type(enabled) is not bool:
            raise CacheRefusal('explicit_partial_boolean_required')
        if (not isinstance(roster, (list, tuple)) or not 1 <= len(roster) <= 256
                or any(type(symbol) is not str or not symbol for symbol in roster)
                or len(set(roster)) != len(roster)):
            raise CacheRefusal('full_original_partial_roster_required')
        self.enabled, self.roster = enabled, tuple(roster)
        if (type(trading_day) is not str or date.fromisoformat(trading_day).isoformat() != trading_day
                or type(source_asof) is not str):
            raise CacheRefusal('canonical_partial_day_and_aware_asof_required')
        asof = datetime.fromisoformat(source_asof.replace('Z', '+00:00'))
        if asof.utcoffset() is None:
            raise CacheRefusal('canonical_partial_day_and_aware_asof_required')
        self.day, self.asof = trading_day, source_asof
        self.snapshot_hash, self.source_seal = _sha(snapshot_hash), _sha(source_seal)
        self.child_pid, self.run_id = child_pid, run_id
        if enabled:
            if type(child_pid) is not int or child_pid <= 0:
                raise CacheRefusal('partial_run_actual_owned_child_pid_required')
            _sha(run_id)
        self.completed = {}
        self.total_raw_bytes = self.total_gzip_bytes = 0

    def add(self, value):
        if not self.enabled:
            raise CacheRefusal('partial_output_disabled')
        if type(value) is not _VerifiedComponent:
            raise CacheRefusal('actual_sealed_component_readback_required')
        symbol, component = value['symbol'], value['component']
        if (symbol not in self.roster or symbol in self.completed
                or component['trading_day'] != self.day or component['source_asof'] != self.asof
                or component['component']['snapshot_hash'] != self.snapshot_hash):
            raise CacheRefusal('same_snapshot_disjoint_original_components_required')
        raw = self.total_raw_bytes + value['evidence']['uncompressed_bytes']
        zipped = self.total_gzip_bytes + value['evidence']['bytes']
        if raw > MAX_EVIDENCE_BYTES or zipped > MAX_EVIDENCE_BYTES:
            raise CacheRefusal('whole_run_evidence_cap_not_per_symbol_cap')
        self.completed[symbol] = deepcopy(value)
        self.total_raw_bytes, self.total_gzip_bytes = raw, zipped

    def assemble(self, closure, *, allow_all_pending=False):
        if not self.enabled:
            raise CacheRefusal('partial_output_disabled')
        if type(closure) is not _ClosedChild:
            raise CacheRefusal('actual_child_closure_required_before_partial_assembly')
        if (closure['pid'] != self.child_pid or closure['run_id'] != self.run_id
                or closure['source_seal'] != self.source_seal):
            raise CacheRefusal('same_child_run_and_source_closure_required')
        if closure['observed_at_ns'] < int(datetime.fromisoformat(self.asof.replace('Z', '+00:00')).timestamp() * 10**9):
            raise CacheRefusal('partial_snapshot_later_than_actual_child_end')
        if type(allow_all_pending) is not bool:
            raise CacheRefusal('explicit_all_pending_policy_required')
        if not self.completed and not allow_all_pending:
            raise CacheRefusal('no_completed_symbol_no_partial_promotion')
        for value in self.completed.values():
            artifact = Path(value['artifact'])
            fd = os.open(artifact.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                directory = os.fstat(fd)
                current = os.stat(NAME, dir_fd=fd, follow_symlinks=False)
                if ((directory.st_dev, directory.st_ino) != value['directory_identity']
                        or _identity(current) != value['file_identity']):
                    raise CacheRefusal('completed_evidence_changed_before_assembly')
            finally:
                os.close(fd)
        pending = [symbol for symbol in self.roster if symbol not in self.completed]
        # A new explicit envelope, NEVER relabelled as legacy complete V2.
        # Main's reader/publisher must review this schema before adoption.
        return dict(schema=PARTIAL_V1, trading_day=self.day, source_asof=self.asof,
            source_seal=self.source_seal, snapshot_hash=self.snapshot_hash,
            native_execution_qualified=False, status='PENDING' if not self.completed else 'PARTIAL' if pending else 'FULL_COMPONENT_SET',
            label=f'{len(pending)} symbols pending', original_symbols=list(self.roster),
            completed_symbols=[symbol for symbol in self.roster if symbol in self.completed],
            pending_symbols=pending, whole_population_pnl=None,
            completed=[deepcopy(self.completed[symbol]) for symbol in self.roster if symbol in self.completed],
            pending=[dict(symbol=symbol, status='PENDING', pnl=None, pnl_label_known=False,
                          source_status='NOT_COMPLETED_NO_ZERO_OR_NO_TRADE_CLAIM') for symbol in pending],
            actual_child_closure=dict(closure), last_good_complete_must_remain_separate=True)
