"""Sealed ORIGINAL-RR historical input reader; never a fresh CURRENT_RR read.

Stdlib only. No source imports, SQL, providers, clock patch, model call, current
publication or cache admission. Main alone executes the reviewed caller.
"""
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
import gzip
import hashlib
import json
import math
import os
from pathlib import Path
import stat
from copy import deepcopy
import re


SCOPE = 'ATRSIMLIVE1_SEALED_ORIGINAL_RR_HISTORICAL_REPLAY_V1'
MANIFEST = 'ATRSIMLIVE1_REVIEWED_HISTORICAL_INPUT_BINDING_V1'
SOURCE_HEAD = '37e6921293fc9571406a17e78e12a0ca384524bd'
SOURCE_MANIFEST = '3f78426fd5451c1c6082ce00c9b76450bb174c40273f418c873be5342e70b7f8'
POLICY_SHA = 'e42e5075bd296144b2a632438de01be1c7f1afe8ebaed34cd528d58f34e39cf3'
COMPRESSED_CAP = 16 * 1024 * 1024
EXPANDED_CAP = 192 * 1024 * 1024
LINE_CAP = 64 * 1024 * 1024
SMALL_CAP = 4 * 1024 * 1024


class CapturedRefusal(ValueError):
    pass


def _pairs(items):
    result = {}
    for name, value in items:
        if name in result:
            raise CapturedRefusal('duplicate_captured_key')
        result[name] = value
    return result


def _finite(raw):
    value = float(raw)
    if not math.isfinite(value):
        raise CapturedRefusal('nonfinite_captured_number')
    return value


def _bad(_):
    raise CapturedRefusal('nonfinite_captured_number')


def decode(raw):
    # Preserve JSONB's original numeric-vs-string distinction. Decimal-valued
    # SQL columns were already strings in the ORIGINAL evidence encoder.
    return json.loads(raw, object_pairs_hook=_pairs, parse_float=_finite, parse_constant=_bad)


def _identity(info):
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def _open(path, cap):
    path = Path(path)
    if (not path.is_absolute() or '..' in path.parts
            or any(part.is_symlink() for part in (path, *path.parents))):
        raise CapturedRefusal('literal_captured_file_path_required')
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid not in (0, os.geteuid())
                or info.st_mode & 0o022 or info.st_nlink != 1 or not 0 < info.st_size <= cap):
            raise CapturedRefusal('owned_bounded_single_link_capture_required')
    except BaseException:
        os.close(fd)
        raise
    return path, fd, info


def verified_bytes(spec, *, cap=SMALL_CAP):
    if not isinstance(spec, dict) or set(spec) != {'path', 'sha256'}:
        raise CapturedRefusal('exact_capture_file_binding_required')
    path, fd, before = _open(spec['path'], cap)
    with os.fdopen(fd, 'rb') as stream:
        raw = stream.read(cap + 1)
        after = os.fstat(stream.fileno())
    if (_identity(before) != _identity(after) or _identity(path.stat(follow_symlinks=False)) != _identity(after)
            or len(raw) != before.st_size or hashlib.sha256(raw).hexdigest() != spec['sha256']):
        raise CapturedRefusal('captured_metadata_changed_or_hash_mismatch')
    return raw


def _hash(stream):
    digest, count = hashlib.sha256(), 0
    stream.seek(0)
    while raw := stream.read(1024 * 1024):
        count += len(raw)
        if count > COMPRESSED_CAP:
            raise CapturedRefusal('captured_compressed_cap')
        digest.update(raw)
    return digest.hexdigest(), count


@dataclass(frozen=True)
class ClosedOriginalRR:
    """Historical evidence witness only; NOT a production-cache capability."""
    evidence_path: str
    evidence_sha256: str
    evidence_bytes: int
    expanded_sha256: str
    expanded_bytes: int
    file_identity: tuple
    snapshot_id: str
    source_asof: str
    symbols: tuple
    original_source_seal: str
    capture_binding_sha256: str
    scope: str = SCOPE
    fresh_SQL_performed: bool = False
    native_execution_qualified: bool = False


def verify_closed_original(manifest):
    """Bind existing small closure/audit receipts; no GZ metadata re-audit."""
    if (manifest.get('schema') != MANIFEST or manifest.get('scope') != SCOPE
            or manifest.get('trading_day') != '2026-10-28'):
        raise CapturedRefusal('explicit_historical_capture_scope_required')
    files = manifest['files']
    summary = decode(verified_bytes(files['summary']))
    outer = decode(verified_bytes(files['outer']))
    plan = decode(verified_bytes(files['original_plan']))
    audit = decode(verified_bytes(files['prior_full_audit']))
    driver = verified_bytes(files['original_driver_log'])
    phase = audit['phases']['warm']
    roster = summary['original_required_symbols']
    if (summary.get('schema') != 'ATRSIM_MAIN_CURRENT_RR_PHASE_CLOSED_V1'
            or summary.get('phase') != 'warm' or summary.get('source_head') != SOURCE_HEAD
            or summary.get('source_manifest_sha256') != SOURCE_MANIFEST
            or plan.get('source_head') != SOURCE_HEAD or plan.get('source_manifest_sha256') != SOURCE_MANIFEST
            or outer.get('source_head') != SOURCE_HEAD or outer.get('source_manifest_sha256') != SOURCE_MANIFEST
            or outer.get('physically_ended') is not True or outer.get('returncode') != 0
            or outer.get('remaining_source_actors') != [] or outer.get('source_pins_unchanged') is not True
            or len(roster) != 23 or len(set(roster)) != 23 or roster != phase['roster']
            or roster != plan['expected_symbols'] or summary['original_denominator'] != 23
            or phase['record_counts'] != {'input': 23, 'model': 23, 'run': 1}
            or summary['evidence']['records'] != 47 or summary['evidence']['sha256'] != phase['compressed_sha256']
            or summary['evidence']['uncompressed_sha256'] != phase['expanded_sha256']
            or plan['retained_policy_sha256'] != POLICY_SHA):
        raise CapturedRefusal('whole_original_roster_source_and_physical_closure_required')
    expected_stages = (b'ROLLBACK+', b'ROLLBACK-', b'DB_CLOSED-', b'MODEL:')
    positions = [driver.find(stage) for stage in expected_stages]
    if any(position < 0 for position in positions) or positions != sorted(positions):
        raise CapturedRefusal('actual_original_rollback_closed_before_models_required')
    snapshots = {phase['symbols'][name]['current_snapshot_id'] for name in roster}
    asofs = {phase['symbols'][name]['source_asof'] for name in roster}
    seals = {phase['symbols'][name]['source_seal'] for name in roster}
    if (len(snapshots) != 1 or len(asofs) != 1 or len(seals) != 1
            or next(iter(asofs)) != summary['source_asof']):
        raise CapturedRefusal('one_original_captured_RR_snapshot_and_asof_required')
    if hashlib.sha256(verified_bytes(files['policy'], cap=2 * 1024 * 1024)).hexdigest() != POLICY_SHA:
        raise CapturedRefusal('original_retained_policy_required')
    evidence = files['evidence']
    if evidence['sha256'] != phase['compressed_sha256'] or evidence['path'] != phase['local_path']:
        raise CapturedRefusal('exact_existing_local_evidence_binding_required')
    path, fd, before = _open(evidence['path'], COMPRESSED_CAP)
    with os.fdopen(fd, 'rb') as stream:
        actual = _hash(stream)
        after = os.fstat(stream.fileno())
    if (actual != (phase['compressed_sha256'], phase['compressed_bytes'])
            or _identity(before) != _identity(after) or tuple(phase['file_identity_before_after']) != _identity(after)
            or _identity(path.stat(follow_symlinks=False)) != _identity(after)):
        raise CapturedRefusal('original_evidence_generation_changed')
    key = hashlib.sha256(json.dumps(manifest, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()
    return ClosedOriginalRR(str(path), actual[0], actual[1], phase['expanded_sha256'], phase['expanded_bytes'],
        _identity(after), next(iter(snapshots)), next(iter(asofs)), tuple(roster), next(iter(seals)), key)


def original_pairs(witness):
    """Consume ONCE per reviewed replay phase, retaining one original pair.

    This is model INPUT reading, not a separate GZ audit. Caller must exhaust
    the generator and perform final source/output checks before qualification.
    Neither original snapshot IDs, input scope markers nor clocks are changed.
    """
    if type(witness) is not ClosedOriginalRR or witness.scope != SCOPE:
        raise CapturedRefusal('closed_original_capture_witness_required')
    path, fd, before = _open(witness.evidence_path, COMPRESSED_CAP)
    expanded, total, index, member, pending = hashlib.sha256(), 0, 0, 0, None
    with os.fdopen(fd, 'rb') as stream:
        if _identity(before) != witness.file_identity or _hash(stream) != (witness.evidence_sha256, witness.evidence_bytes):
            raise CapturedRefusal('captured_generation_changed_before_input_read')
        stream.seek(0)
        with gzip.GzipFile(fileobj=stream, mode='rb') as zipped:
            while True:
                line = zipped.readline(LINE_CAP + 1)
                if not line:
                    break
                total += len(line)
                if len(line) > LINE_CAP or not line.endswith(b'\n') or total > EXPANDED_CAP or index >= 47:
                    raise CapturedRefusal('complete_original_record_bounds_required')
                expanded.update(line)
                record = decode(line)
                if (set(record) != {'schema', 'index', 'kind', 'symbol', 'payload'}
                        or record['schema'] != 'ATRSIMLIVE1_EVIDENCE_JSONL_V1' or record['index'] != index):
                    raise CapturedRefusal('exact_original_record_grammar_required')
                if index == 46:
                    if record['kind'] != 'run' or record['symbol'] is not None or pending is not None:
                        raise CapturedRefusal('original_run_closure_record_required')
                    if (record['payload']['source']['head'] != SOURCE_HEAD
                            or record['payload']['source']['source_manifest_sha256'] != SOURCE_MANIFEST):
                        raise CapturedRefusal('original_run_source_changed')
                elif index % 2 == 0:
                    symbol = witness.symbols[member]
                    if record['kind'] != 'input' or record['symbol'] != symbol or record['payload']['symbols'] != [symbol]:
                        raise CapturedRefusal('complete_ordered_original_inputs_required')
                    pending = record['payload']
                    if pending['snapshot_id'] != witness.snapshot_id or pending['source_asof'] != witness.source_asof:
                        raise CapturedRefusal('original_snapshot_or_asof_changed')
                else:
                    symbol = witness.symbols[member]
                    original = record['payload']
                    if record['kind'] != 'model' or record['symbol'] != symbol or pending is None:
                        raise CapturedRefusal('one_original_model_per_input_required')
                    producer = original['producer']
                    if (producer['head'] != SOURCE_HEAD or producer['source_manifest_sha256'] != SOURCE_MANIFEST
                            or producer['snapshot_id'] != witness.snapshot_id or producer['observed_symbols'] != [symbol]
                            or producer['policy_sha256'] != POLICY_SHA
                            or producer['input_refs'] != pending['source_hashes']
                            or producer['source_counts'] != pending['source_counts']
                            or producer['query_sha256'] != pending['query_sha256']):
                        raise CapturedRefusal('original_model_source_input_policy_mismatch')
                    yield pending, original
                    pending = None
                    member += 1
                index += 1
        if (index != 47 or member != 23 or pending is not None or total != witness.expanded_bytes
                or expanded.hexdigest() != witness.expanded_sha256
                or _hash(stream) != (witness.evidence_sha256, witness.evidence_bytes)
                or _identity(os.fstat(stream.fileno())) != witness.file_identity
                or _identity(path.stat(follow_symlinks=False)) != witness.file_identity):
            raise CapturedRefusal('complete_original_stream_or_postidentity_mismatch')


def original_datetime(value):
    result = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if result.utcoffset() is None:
        raise CapturedRefusal('original_aware_clock_required')
    return result


QUOTE_COLUMNS = frozenset(('id', 'provider', 'symbol', 'event_ts', 'received_at',
                           'bid_price', 'ask_price', 'bid_size', 'ask_size'))
QUOTE_REHYDRATION = 'ORIGINAL_QUERY_QUOTES_NUMERIC_18_6_MODEL_COPY_V1'


def historical_model_input(original):
    """Restore ONLY original SQL Numeric quote columns in a MODEL COPY.

    Source proof, unchanged since37e: queries.CAPTURE_PAGES['quotes'] and
    incremental_reads.FIELDS['quotes']; db.models.MarketCaptureQuote declares
    bid_price/ask_price Numeric(18,6), nullable. collect.plain/evidence._default
    serialize SQL Decimal as text. The original model passes these two values
    directly into Quote, unlike its positive()/integral() conversion paths.
    No JSONB/condition/raw string inference, row deletion or default value.
    """
    data = dict(original)
    data['source_asof'] = original_datetime(original['source_asof'])
    if isinstance(original.get('read_proof'), dict) and 'source_asof' in original['read_proof']:
        data['read_proof'] = dict(original['read_proof'], source_asof=data['source_asof'])
    rows, quotes = original['rows'], []
    for row in rows['quotes']:
        if not isinstance(row, dict) or set(row) != QUOTE_COLUMNS:
            raise CapturedRefusal('original_quote_query_projection_required')
        quote = dict(row)
        for column in ('bid_price', 'ask_price'):
            value = row[column]
            if value is None:
                continue  # SQL NULL remains unknown, never zero or empty book.
            if type(value) is not str:
                raise CapturedRefusal('original_SQL_numeric_text_required')
            try:
                number = Decimal(value)
            except InvalidOperation:
                raise CapturedRefusal('unreadable_original_SQL_quote_numeric') from None
            if not number.is_finite() or str(number) != value:
                raise CapturedRefusal('exact_finite_SQL_quote_decimal_required')
            quote[column] = number
        quotes.append(quote)
    data['rows'] = dict(rows, quotes=quotes)
    return data


class SealedRoutes:
    """C1 12:51 sealed parser facts; raw text absent remains UNKNOWN.

    Construct only from the exact input/model pair yielded by original_pairs
    after verify_closed_original. No fabricated raw log line or route lookup.
    """
    def __init__(self, witness, data, original):
        if (type(witness) is not ClosedOriginalRR or data['symbols'] != original['producer']['observed_symbols']
                or data['snapshot_id'] != witness.snapshot_id
                or data['source_asof'] != witness.source_asof
                or original['producer']['head'] != SOURCE_HEAD):
            raise CapturedRefusal('sealed_original_route_pair_required')
        self.snapshot, self.asof = witness.snapshot_id, witness.source_asof
        self.symbols = tuple(data['symbols'])
        self.routes = {}
        for item in original['producer']['day_routes']:
            item = deepcopy(item)
            symbol = item.pop('symbol')
            if symbol not in self.symbols or symbol in self.routes:
                raise CapturedRefusal('exact_original_route_population_required')
            if item['status'] == 'RECORDED_DAY_ROUTE':
                if (item.get('broker') not in ('schwab', 'webull')
                        or item.get('notional') != ('600' if item['broker'] == 'schwab' else '300')
                        or not item.get('facts') or item.get('issues') != []):
                    raise CapturedRefusal('original_parsed_route_fields_required')
                for fact in item['facts']:
                    if (fact['symbol'] != symbol or fact['broker'] != item['broker']
                            or fact['notional'] != item['notional']
                            or not isinstance(fact['selected_text_sha256'], str)
                            or re.fullmatch('[0-9a-f]{64}', fact['selected_text_sha256']) is None):
                        raise CapturedRefusal('selected_original_route_hash_required')
            elif item['status'] != 'UNKNOWN' or item.get('broker') is not None or item.get('notional') is not None:
                raise CapturedRefusal('original_unknown_route_not_permission')
            self.routes[symbol] = item
        if set(self.routes) != set(self.symbols):
            raise CapturedRefusal('complete_original_route_population_required')

    def resolve(self, data, symbol):
        asof = data['source_asof']
        if isinstance(asof, datetime):
            asof = asof.isoformat()
        if (data['snapshot_id'] != self.snapshot or asof != self.asof
                or tuple(data['symbols']) != self.symbols or data['trading_day'] != '2026-10-28'):
            raise CapturedRefusal('original_route_snapshot_scope_changed')
        return deepcopy(self.routes[symbol])
