"""Compact display projection + ONE lossless, streamed evidence file per run.

No engine/DB/provider imports. CLI integration and execution belong to Main.
Projection never makes unknown economics known or changes source clocks.
"""

from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal
import gzip
import hashlib
import io
import json
import math
import os
from pathlib import Path
import re
import stat
from uuid import UUID


SCHEMA = "ATRSIMLIVE1_TODAY_COMPACT_V2"
EVIDENCE_SCHEMA = "ATRSIMLIVE1_EVIDENCE_JSONL_V1"
RECEIPT_SCHEMA = "ATRSIMLIVE1_COMPACT_RUN_V1"
MAX_STDOUT = 1024 * 1024  # Target only; original 16MiB runtime cap unchanged.
MAX_EVIDENCE_BYTES = 2 * 1024 * 1024 * 1024
NAME = "atrsim-evidence.jsonl.gz"
KEYS = ("FIRSTBAR", "FIRSTBAR_X3", "AGG_ENTRY", "LIVE:ACTUAL", "TODAY:SUPPLIED",
        "VOLBODY:QUEUE_AWARE", "VOLBODY:OPTIMISTIC")
FIELDS = ("ticker", "path", "quantity", "entry_time", "entry_price", "exit_time",
          "exit_price", "pnl_label", "pnl_pct_label", "summary", "pnl", "pnl_label_known",
          "coverage_note", "pnl_pct", "pnl_pct_label_known", "sizing_status", "percent_basis")
SHA = re.compile(r"[0-9a-f]{64}\Z")


def _default(value):
    if isinstance(value, (Decimal, UUID)):
        return str(value)  # Same exact string representation as collector.plain.
    if isinstance(value, datetime) and value.utcoffset() is not None:
        return value.astimezone(timezone.utc).isoformat()
    raise TypeError("unsupported_evidence_value")


ENCODER = json.JSONEncoder(ensure_ascii=True, allow_nan=False, sort_keys=True,
                           separators=(",", ":"), default=_default)


def _chunks(value):
    for text in ENCODER.iterencode(value):
        yield text.encode("utf-8", errors="strict")


def _digest(value):
    digest = hashlib.sha256()
    for raw in _chunks(value):
        digest.update(raw)
    return digest.hexdigest()


def _public(value, limit=2048):
    if (not isinstance(value, str) or not 0 < len(value) <= limit
            or any(ord(c) < 32 for c in value)
            or re.search(r"/(?:Users|private|home|var|tmp|root)(?:/|\b)", value)
            or re.search(r"\b[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}\b", value)):
        raise ValueError("bounded_public_display_text_required")
    value.encode("utf-8", errors="strict")
    return value


def _hash(value):
    if not isinstance(value, str) or SHA.fullmatch(value) is None:
        raise ValueError("literal_sha256_required")
    return value


def _symbol(value):
    if not isinstance(value, str) or re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,31}", value) is None:
        raise ValueError("literal_symbol_required")
    return value


def _file_hash(fd):
    os.lseek(fd, 0, os.SEEK_SET)
    result = hashlib.sha256()
    size = 0
    while raw := os.read(fd, 1024 * 1024):
        result.update(raw)
        size += len(raw)
    return result.hexdigest(), size


class EvidenceWriter:
    """Owned 0700 run directory; exclusive fixed name, no overwrite/retry.

    append accepts the ORIGINAL input packet/document, without selecting fields.
    Partial files survive any failure. seal closes gzip, fsyncs, atomically links
    without replacement, fsyncs the directory and verifies final readback SHA.
    A second writer in this directory is refused, including after first failure.
    """

    def __init__(self, run_directory, *, remaining_bytes=None):
        if remaining_bytes is not None and (type(remaining_bytes) is not int or not 0 < remaining_bytes <= MAX_EVIDENCE_BYTES):
            raise ValueError('actual_remaining_evidence_byte_cap_required')
        self.byte_limit = MAX_EVIDENCE_BYTES if remaining_bytes is None else remaining_bytes
        directory = Path(run_directory)
        if not directory.is_absolute() or ".." in directory.parts:
            raise ValueError("literal_absolute_run_directory_required")
        if any(part.is_symlink() for part in (directory, *directory.parents)):
            raise ValueError("run_directory_symlink")
        self.dirfd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        self.raw = self.gz = None
        self.failed = self.closed = False
        self.counts = Counter()
        self.symbol_counts = {}
        self.total = 0
        self.raw_hash = hashlib.sha256()
        try:
            info = os.fstat(self.dirfd)
            if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
                raise ValueError("owned_0700_run_directory_required")
            try:
                os.stat(NAME, dir_fd=self.dirfd, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                raise ValueError("evidence_already_exists")
            fd = os.open("." + NAME + ".partial", os.O_CREAT | os.O_EXCL | os.O_RDWR | os.O_NOFOLLOW,
                         0o600, dir_fd=self.dirfd)
            try:
                self.raw = os.fdopen(fd, "w+b")
            except BaseException:
                os.close(fd)
                raise
            os.fchmod(self.raw.fileno(), 0o600)
            self.gz = io.BufferedWriter(gzip.GzipFile(filename="", mode="wb", fileobj=self.raw,
                                                     mtime=0, compresslevel=1), buffer_size=64*1024)
        except BaseException as error:
            try:
                self.close()
            except BaseException as cleanup:
                error.add_note("evidence cleanup failed: " + type(cleanup).__name__)
            raise

    def append(self, kind, payload, *, symbol=None):
        if self.failed or self.closed:
            raise ValueError("evidence_writer_not_open")
        try:
            if kind not in {"input", "model", "run", "witness"}:
                raise ValueError("explicit_evidence_kind_required")
            if symbol is not None:
                _symbol(symbol)
            if kind in {"input", "model"}:
                if symbol is None or self.symbol_counts.get(symbol, {}).get(kind, 0):
                    raise ValueError("one_original_input_and_model_per_symbol_required")
            record = dict(schema=EVIDENCE_SCHEMA, index=sum(self.counts.values()),
                          kind=kind, symbol=symbol, payload=payload)
            for raw in _chunks(record):
                self._write(raw)
            self._write(b"\n")
            self.counts[kind] += 1
            if kind in {"input", "model"}:
                self.symbol_counts.setdefault(symbol, {})[kind] = 1
        except BaseException:
            self.failed = True
            raise

    def _write(self, raw):
        if self.total + len(raw) > self.byte_limit:
            raise ValueError("evidence_byte_bound_no_truncation")
        self.gz.write(raw)
        self.raw_hash.update(raw)
        self.total += len(raw)

    def seal(self):
        if (self.failed or self.closed or not self.symbol_counts
                or any(counts != {"input": 1, "model": 1} for counts in self.symbol_counts.values())):
            self.failed = True
            raise ValueError("complete_nonempty_evidence_required")
        try:
            self.gz.close()
            self.gz = None
            self.raw.flush()
            os.fsync(self.raw.fileno())
            checksum, size = _file_hash(self.raw.fileno())
            self.raw.close()
            self.raw = None
            os.link("." + NAME + ".partial", NAME, src_dir_fd=self.dirfd,
                    dst_dir_fd=self.dirfd, follow_symlinks=False)
            os.unlink("." + NAME + ".partial", dir_fd=self.dirfd)
            os.fsync(self.dirfd)
            fd = os.open(NAME, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=self.dirfd)
            try:
                info = os.fstat(fd)
                if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                        or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1):
                    raise ValueError("evidence_readback_identity_mismatch")
                if _file_hash(fd) != (checksum, size):
                    raise ValueError("evidence_readback_mismatch")
                after = os.fstat(fd)
                if (info.st_size, info.st_mtime_ns, info.st_ctime_ns) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                    raise ValueError("evidence_changed_during_readback")
            finally:
                os.close(fd)
            descriptor = _SealedEvidence(schema=EVIDENCE_SCHEMA, file=NAME, sha256=checksum, bytes=size,
                              uncompressed_sha256=self.raw_hash.hexdigest(),
                              uncompressed_bytes=self.total, records=sum(self.counts.values()),
                              records_by_kind=dict(self.counts),
                              records_by_symbol={s: dict(c) for s, c in self.symbol_counts.items()})
            self.close()
            return descriptor
        except BaseException as error:
            self.failed = True
            try:
                self.close()
            except BaseException as cleanup:
                error.add_note("evidence cleanup failed: " + type(cleanup).__name__)
            raise

    def close(self):
        try:
            if self.gz is not None:
                self.gz.close()
                self.gz = None
        finally:
            try:
                if self.raw is not None:
                    self.raw.close()
                    self.raw = None
            finally:
                if not self.closed:
                    os.close(self.dirfd)
                    self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, kind, error, traceback):
        try:
            self.close()
        except BaseException as cleanup:
            if error is None:
                raise
            error.add_note("evidence cleanup failed: " + type(cleanup).__name__)


class _SealedEvidence(dict):
    """Only writer.seal returns this in-process readback-completed marker."""


def project_component(document):
    """Pure whitelist projection; full document MUST separately go to writer.

    Raw account/flip/segment/outcome IDs are evidence-only. display_key is a
    derived hash, explicitly not an account, segment or native identity.
    Missing signal time stays UNKNOWN; never derive a historical receipt clock.
    """
    from project_mai_tai.atr_simulation.today_file import validate_today_document, _instant

    if document.get("schema") != "ATRSIMLIVE1_TODAY_V1":
        raise ValueError("original_today_document_required")
    checked = validate_today_document(document, now=_instant(document["generated_at"]))
    if checked["file_status"] != "AVAILABLE":
        raise ValueError("invalid_source_display_envelope")
    output = {field: document[field] for field in ("trading_day", "timezone", "generated_at", "source_asof")}
    output.update(schema=SCHEMA, native_execution_qualified=False, tables={})
    producer = document["producer"]
    hashes = producer.get("input_refs")
    if not isinstance(hashes, dict) or not hashes:
        raise ValueError("actual_input_hashes_required")
    input_hashes = {_public(kind, 64): _hash(value) for kind, value in hashes.items()}
    if not isinstance(producer.get("observed_symbols"), list) or len(producer["observed_symbols"]) != 1:
        raise ValueError("original_one_symbol_component_required")
    snapshot = producer.get("snapshot_id")
    if not isinstance(snapshot, str) or not 0 < len(snapshot) <= 2048:
        raise ValueError("actual_snapshot_identity_required")
    snapshot.encode("utf-8", errors="strict")
    symbols = [_symbol(symbol) for symbol in producer["observed_symbols"]]
    output["component"] = dict(symbols=symbols, input_hashes=input_hashes,
                               snapshot_hash=_digest(snapshot),
                               unknown_counts_by_cause={}, comparisons=[])
    causes = Counter()
    for key in KEYS:
        original = document["tables"][key]
        if checked["tables"][key]["display_status"] in {"INVALID_TABLE", "TABLE_NOT_PUBLISHED"}:
            raise ValueError("invalid_source_table_no_row_drop")
        table = {field: original[field] for field in ("updated_at", "source_asof", "read_status",
                 "known_pnl_label", "skipped_count", "unknown_count", "truncated")}
        table.update(source_id="compact display projection; full source evidence retained separately",
                     issues=[_public(issue) for issue in original["issues"]], rows=[])
        causes.update(table["issues"])
        for item in original["rows"]:
            receipt = producer.get("engine_receipts", {}).get(item.get("outcome_id"), {})
            row = {name: item["row"][name] for name in FIELDS if name in item["row"]}
            for name, value in row.items():
                if isinstance(value, str):
                    _public(value)
            row.update(signal_time=_public(item["row"].get("signal_time", "UNKNOWN")),
                       status=_public(item["row"].get("status", receipt.get("decision", original["read_status"]))),
                       label=_public(item["row"].get("same_sip_policy_label", "REAL recorded" if key == "LIVE:ACTUAL" else "SIM")))
            identity = item["identity"]
            table["rows"].append(dict(identity=dict(trading_day=identity["trading_day"],
                symbol=identity["symbol"], display_key=_digest(dict(table=key, identity=identity,
                                                                outcome_id=item.get("outcome_id")))), row=row))
            causes.update(_public(issue) for issue in receipt.get("issues", []))
        output["tables"][key] = table
    for report in producer.get("c1_buy_parity", []):
        for fact in report.get("comparisons", []):
            output["component"]["comparisons"].append({name: _public(fact[name], 256)
                for name in ("symbol", "reference", "reference_direction", "status")})
    output["component"]["unknown_counts_by_cause"] = dict(causes)
    return output


def compact_document(components, *, evidence, timings):
    """Combine ALL projected symbols; never call model combine or trim rows."""
    if not components:
        raise ValueError("explicit_population_required")
    if (not isinstance(evidence, _SealedEvidence)
            or evidence.get("schema") != EVIDENCE_SCHEMA or evidence.get("file") != NAME
            or not _hash(evidence.get("sha256")) or type(evidence.get("records")) is not int
            or evidence["records"] <= 0):
        raise ValueError("sealed_evidence_required")
    for name, value in timings.items():
        _public(name, 64)
        if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
            raise ValueError("actual_finite_timing_required")
    first = components[0]
    result = {key: first[key] for key in ("schema", "trading_day", "timezone", "source_asof")}
    result.update(native_execution_qualified=False, generated_at=max((c["generated_at"] for c in components),
                  key=lambda value: datetime.fromisoformat(value.replace("Z", "+00:00"))),
                  producer=dict(schema="ATRSIMLIVE1_COMPACT_PRODUCER_V1"), tables={})
    population, causes, inputs, comparisons = set(), Counter(), [], []
    for component in components:
        if (any(component[key] != first[key] for key in ("schema", "trading_day", "timezone", "source_asof"))
                or component["component"]["snapshot_hash"] != first["component"]["snapshot_hash"]):
            raise ValueError("same_day_snapshot_required")
        scope = component["component"]
        for symbol in scope["symbols"]:
            if symbol in population:
                raise ValueError("duplicate_symbol_component")
            population.add(symbol)
        inputs.append(dict(symbols=scope["symbols"], hashes=scope["input_hashes"]))
        causes.update(scope["unknown_counts_by_cause"])
        comparisons.extend(scope["comparisons"])
    if set(evidence["records_by_symbol"]) != population:
        raise ValueError("evidence_population_mismatch_no_omission")
    for key in KEYS:
        tables = [component["tables"][key] for component in components]
        result["tables"][key] = dict(tables[0], rows=[row for table in tables for row in table["rows"]],
            issues=list(dict.fromkeys(issue for table in tables for issue in table["issues"])),
            updated_at=min((table["updated_at"] for table in tables if table["updated_at"] is not None), default=None,
                           key=lambda value: datetime.fromisoformat(value.replace("Z", "+00:00"))),
            read_status="AVAILABLE" if all(table["read_status"] == "AVAILABLE" for table in tables) else "UNKNOWN",
            skipped_count=sum(table["skipped_count"] for table in tables) if all(type(t["skipped_count"]) is int for t in tables) else None,
            unknown_count=sum(table["unknown_count"] for table in tables) if all(type(t["unknown_count"]) is int for t in tables) else None,
            known_pnl_label="UNKNOWN", truncated=any(table["truncated"] for table in tables))
    result["run_receipt"] = dict(schema=RECEIPT_SCHEMA, evidence=dict(evidence), input_hashes=inputs,
        input_snapshot_hash=first["component"]["snapshot_hash"],
        counts=dict(symbols=len(population), rows_by_table={key: len(result["tables"][key]["rows"]) for key in KEYS}),
        timings=dict(timings), unknown_counts_by_cause=dict(causes),
        cause_count_basis="table issue occurrences plus per-row receipt issue occurrences; not unique events",
        comparisons=comparisons, native_execution_qualified=False)
    return result


def encode_compact(document):
    """No stdout side effect: Main writes only returned bytes after validation."""
    if document.get("schema") != SCHEMA:
        raise ValueError("compact_schema_required")
    from project_mai_tai.atr_simulation.today_file import validate_today_document, _instant

    checked = validate_today_document(document, now=_instant(document["generated_at"]))
    if (checked["file_status"] != "AVAILABLE"
            or any(table["display_status"] == "INVALID_TABLE" for table in checked["tables"].values())):
        raise ValueError("compact_contract_refused_no_publication")
    raw = bytearray()
    for chunk in _chunks(document):
        if len(raw) + len(chunk) >= MAX_STDOUT:
            raise ValueError("compact_target_exceeded_no_population_truncation")
        raw.extend(chunk)
    return bytes(raw)
