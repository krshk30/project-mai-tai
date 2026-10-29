"""Bounded read-only TODAY display transport; no engine, DB or trading calls.

Runner owns canonical engine projections and atomic publication. This reader
only validates their display fields/date/as-of status; never computes economics.
"""

from datetime import datetime, timezone
from decimal import Decimal
import json
import math
import os
from pathlib import Path
import re
import stat
from zoneinfo import ZoneInfo


SCHEMA = "ATRSIMLIVE1_TODAY_V1"
COMPACT_SCHEMA = "ATRSIMLIVE1_TODAY_COMPACT_V2"
MAX_COMPACT_BYTES = 1024 * 1024
DEFAULT_PATH = Path("/var/lib/project-mai-tai/atr-simulation/today/current.json")
ET = ZoneInfo("America/New_York")
MAX_BYTES = 16 * 1024 * 1024
MAX_ROWS = 10000
STALE_SECONDS = 600
TABLES = (
    ("FIRSTBAR", "FIRSTBAR", "FIRSTBAR"),
    ("FIRSTBAR_X3", "FIRSTBAR+2min", "FIRSTBAR_X3"),
    ("AGG_ENTRY", "Live replay", "AGGREGATE_ENTRY"),
    ("LIVE:ACTUAL", "Recorded LIVE comparison", "LIVE:ACTUAL"),
    ("TODAY:SUPPLIED", "SIM TODAY", "TODAY:SUPPLIED"),
    ("VOLBODY:QUEUE_AWARE", "SIM VOLBODY / Queue aware", "VOLBODY:QUEUE_AWARE"),
    ("VOLBODY:OPTIMISTIC", "SIM VOLBODY / Optimistic", "VOLBODY:OPTIMISTIC"),
)
ROW_FIELDS = ("ticker", "path", "quantity", "entry_time", "entry_price", "exit_time",
              "exit_price", "pnl_label", "pnl_pct_label", "summary")
IDENTITY_FIELDS = ("trading_day", "symbol", "broker", "account_id", "strategy", "flip_id", "segment_id")
COMPACT_ROW_FIELDS = {*ROW_FIELDS, "pnl", "pnl_label_known", "coverage_note", "pnl_pct",
                      "pnl_pct_label_known", "sizing_status", "percent_basis", "signal_time", "status", "label"}


def _sha(value):
    return isinstance(value, str) and re.fullmatch("[0-9a-f]{64}", value) is not None


def _count(value):
    return type(value) is int and value >= 0


def _compact_text(value, *, limit=2048):
    return (_plain(value, limit=limit)
            and re.search(r"/(?:Users|private|home|var|tmp|root)(?:/|\b)", value) is None
            and re.search(r"\b[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}\b", value) is None)


def _compact_receipt(document):
    """Validate only compact public metadata. Never open/decompress evidence."""
    if set(document) != {"schema", "trading_day", "timezone", "generated_at", "source_asof",
                         "producer", "native_execution_qualified", "tables", "run_receipt"}:
        raise ValueError("exact_compact_envelope_required")
    if document["producer"] != {"schema": "ATRSIMLIVE1_COMPACT_PRODUCER_V1"}:
        raise ValueError("no_bulk_producer_in_compact_display")
    receipt = document["run_receipt"]
    if (not isinstance(receipt, dict) or set(receipt) != {
            "schema", "evidence", "input_hashes", "input_snapshot_hash", "counts", "timings",
            "unknown_counts_by_cause", "cause_count_basis", "comparisons", "native_execution_qualified"}
            or receipt["schema"] != "ATRSIMLIVE1_COMPACT_RUN_V1"
            or receipt["native_execution_qualified"] is not False
            or not _sha(receipt["input_snapshot_hash"]) or not _compact_text(receipt["cause_count_basis"])):
        raise ValueError("exact_compact_receipt_required")
    evidence = receipt["evidence"]
    if (not isinstance(evidence, dict) or set(evidence) != {"schema", "file", "sha256", "bytes",
            "uncompressed_sha256", "uncompressed_bytes", "records", "records_by_kind", "records_by_symbol"}
            or evidence["schema"] != "ATRSIMLIVE1_EVIDENCE_JSONL_V1"
            or evidence["file"] != "atrsim-evidence.jsonl.gz"
            or not _sha(evidence["sha256"]) or not _sha(evidence["uncompressed_sha256"])
            or any(not _count(evidence[key]) or evidence[key] == 0
                   for key in ("bytes", "uncompressed_bytes", "records"))
            or not isinstance(evidence["records_by_kind"], dict)
            or not set(evidence["records_by_kind"]) <= {"input", "model", "run", "witness"}
            or any(not _count(v) for v in evidence["records_by_kind"].values())
            or sum(evidence["records_by_kind"].values()) != evidence["records"]):
        raise ValueError("compact_evidence_reference_required")
    if not isinstance(receipt["input_hashes"], list) or not receipt["input_hashes"]:
        raise ValueError("compact_input_hashes_required")
    population = set()
    for item in receipt["input_hashes"]:
        if (not isinstance(item, dict) or set(item) != {"symbols", "hashes"}
                or not isinstance(item["symbols"], list) or not item["symbols"]
                or not isinstance(item["hashes"], dict) or not item["hashes"]
                or any(not _compact_text(k, limit=64) or not _sha(v) for k, v in item["hashes"].items())):
            raise ValueError("compact_symbol_hash_contract")
        for symbol in item["symbols"]:
            if (not isinstance(symbol, str) or re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,31}", symbol) is None
                    or symbol in population):
                raise ValueError("compact_population_duplicate_or_invalid")
            population.add(symbol)
    counts = receipt["counts"]
    by_symbol = evidence["records_by_symbol"]
    if (not isinstance(by_symbol, dict) or set(by_symbol) != population
            or any(v != {"input": 1, "model": 1} or any(type(n) is not int for n in v.values())
                   for v in by_symbol.values())
            or evidence["records_by_kind"].get("input") != len(population)
            or evidence["records_by_kind"].get("model") != len(population)):
        raise ValueError("compact_evidence_population_incomplete")
    if (not isinstance(counts, dict) or set(counts) != {"symbols", "rows_by_table"}
            or type(counts["symbols"]) is not int or counts["symbols"] != len(population)
            or not isinstance(counts["rows_by_table"], dict)
            or set(counts["rows_by_table"]) != {key for key, _, _ in TABLES}
            or any(not _count(v) or v != len(document["tables"][key]["rows"])
                   for key, v in counts["rows_by_table"].items())):
        raise ValueError("compact_counts_do_not_match_all_rows")
    timings = receipt["timings"]
    if (not isinstance(timings, dict) or any(not _compact_text(k, limit=64)
            or type(v) not in (int, float, Decimal) or not math.isfinite(float(v)) or v < 0
            for k, v in timings.items())):
        raise ValueError("compact_finite_actual_timings_required")
    causes = receipt["unknown_counts_by_cause"]
    if not isinstance(causes, dict) or any(not _compact_text(k) or not _count(v) for k, v in causes.items()):
        raise ValueError("compact_unknown_cause_counts_required")
    if not isinstance(receipt["comparisons"], list):
        raise ValueError("compact_comparisons_required")
    for fact in receipt["comparisons"]:
        if (not isinstance(fact, dict) or set(fact) != {"symbol", "reference", "reference_direction", "status"}
                or any(not _compact_text(v, limit=256) for v in fact.values())):
            raise ValueError("compact_public_comparison_required")
    return receipt, population


def _text(value):
    if not isinstance(value, str) or not 0 < len(value) <= 2048:
        return False
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError:
        return False
    return True


def _plain(value, *, limit=2048):
    return _text(value) and len(value) <= limit and not any(ord(c) < 32 for c in value)


def _percent(row):
    """Validate supplied display evidence only; never derive a return or size."""
    if any(field in row for field in ("sizing_status", "percent_basis")):
        if any(not _plain(row.get(field)) for field in ("sizing_status", "percent_basis")):
            raise ValueError("bounded_percent_sizing_basis_required")
    present = "pnl_pct" in row or "pnl_pct_label_known" in row
    if not present:
        if row["pnl_pct_label"] != "UNKNOWN":
            raise ValueError("percent_label_requires_explicit_evidence")
        return
    if ("pnl_pct" not in row or type(row.get("pnl_pct_label_known")) is not bool):
        raise ValueError("explicit_percent_contract_required")
    if row["pnl_pct_label_known"]:
        amount = row["pnl_pct"]
        if (not isinstance(amount, str) or not _text(amount)
                or not _finite_decimal(amount).is_finite()
                or row["pnl_pct_label"] == "UNKNOWN"):
            raise ValueError("finite_known_percent_required")
    elif row["pnl_pct"] is not None or row["pnl_pct_label"] != "UNKNOWN":
        raise ValueError("unknown_percent_cannot_become_zero")
    for field in ("sizing_status", "percent_basis"):
        if not _plain(row.get(field)):
            raise ValueError("explicit_percent_sizing_basis_required")
    quantity = row["quantity"]
    if quantity == "UNKNOWN":
        if row["pnl_label_known"] or row.get("pnl") is not None or row["pnl_label"] != "UNKNOWN":
            raise ValueError("unknown_units_cannot_have_known_dollars")
    elif not quantity.isascii() or not quantity.isdigit():
        raise ValueError("integral_sized_units_required")


def _instant(value):
    if not isinstance(value, str):
        raise ValueError("actual_aware_update_clock_required")
    instant = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if instant.utcoffset() is None:
        raise ValueError("actual_aware_update_clock_required")
    return instant.astimezone(timezone.utc)


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_today_key")
        result[key] = value
    return result


def _finite_decimal(value):
    result = Decimal(value)
    if not result.is_finite() or not math.isfinite(float(result)):
        raise ValueError("today_numeric_overflow")
    return result  # Range validation only: retain exact Decimal, not float.


def _finite_integer(value):
    result = int(value)
    if not math.isfinite(float(result)):
        raise ValueError("today_numeric_overflow")
    return result


def _known_cell_day(value):
    if value in {"UNKNOWN", "OPEN", "-"}:
        return None
    if value.endswith(" ET"):
        for pattern in ("%Y-%m-%d %H:%M:%S ET", "%Y-%m-%d %I:%M:%S %p ET"):
            try:
                return datetime.strptime(value, pattern).date()
            except ValueError:
                continue
        raise ValueError("canonical_dated_time_cell_required")
    return _instant(value).astimezone(ET).date()


def _unavailable(day, reason):
    return dict(trading_day=day.isoformat(), file_status=reason,
                tables={key: dict(rows=[], updated_at=None, source_asof=None,
                                  issues=[reason], read_status="UNAVAILABLE",
                                  display_status=reason, known_pnl_label="UNKNOWN",
                                  source_id="UNKNOWN", unknown_count=None, skipped_count=None,
                                  truncated=False) for key, _, _ in TABLES})


def validate_today_document(value, *, now):
    """No date filter: wrong-day content is refused, never relabeled as today."""
    if not isinstance(now, datetime) or now.utcoffset() is None:
        raise ValueError("aware_request_clock_required")
    day = now.astimezone(ET).date()
    result = _unavailable(day, "INVALID_TODAY_FILE")
    compact = isinstance(value, dict) and value.get("schema") == COMPACT_SCHEMA
    if (not isinstance(value, dict) or value.get("schema") not in (SCHEMA, COMPACT_SCHEMA)
            or not {"schema", "trading_day", "timezone", "generated_at", "source_asof",
                    "producer", "native_execution_qualified", "tables"}.issubset(value)
            or value.get("trading_day") != day.isoformat()
            or value.get("timezone") != "America/New_York"
            or value.get("native_execution_qualified") is not False
            or not isinstance(value.get("producer"), dict) or not value["producer"]
            or "html" in value
            or not isinstance(value.get("tables"), dict)
            or set(value["tables"]) != {key for key, _, _ in TABLES}):
        return result
    try:
        receipt, population = _compact_receipt(value) if compact else (None, None)
        generated = _instant(value.get("generated_at"))
        if generated > now or generated.astimezone(ET).date() != day:
            return result
        source_asof = _instant(value["source_asof"]) if value.get("source_asof") is not None else None
        if source_asof is not None and source_asof > generated:
            return result
    except (ValueError, TypeError, KeyError, ArithmeticError, RecursionError):
        return result
    if compact:
        result["run_receipt"] = receipt
    result["file_status"] = "AVAILABLE"
    count = 0
    for key, _, _ in TABLES:
        table = value["tables"].get(key)
        if table is None:
            result["tables"][key]["display_status"] = "TABLE_NOT_PUBLISHED"
            result["tables"][key]["issues"] = ["TABLE_NOT_PUBLISHED"]
            continue
        try:
            if compact and (set(table) != {"updated_at", "source_asof", "read_status", "source_id", "issues",
                    "rows", "known_pnl_label", "skipped_count", "unknown_count", "truncated"}
                    or not _compact_text(table["source_id"])
                    or any(not _compact_text(issue) for issue in table["issues"])):
                raise ValueError("compact_table_public_projection_required")
            if (not isinstance(table, dict) or table.get("read_status") not in {
                    "AVAILABLE", "UNKNOWN", "UNAVAILABLE"}
                    or not {"updated_at", "source_asof", "read_status", "source_id", "issues",
                            "rows", "known_pnl_label", "skipped_count", "unknown_count",
                            "truncated"}.issubset(table)
                    or "html" in table
                    or not isinstance(table.get("issues"), list)
                    or any(not _text(issue) for issue in table["issues"])
                    or not _text(table.get("source_id"))
                    or not isinstance(table.get("rows"), list)
                    or type(table.get("truncated")) is not bool
                    or any(table.get(field) is not None and
                           (type(table[field]) is not int or table[field] < 0)
                           for field in ("skipped_count", "unknown_count"))
                    or not _text(table.get("known_pnl_label"))):
                raise ValueError("invalid_today_table")
            count += len(table["rows"])
            if count > MAX_ROWS:
                return _unavailable(day, "TODAY_ROW_LIMIT_EXCEEDED")
            updated = _instant(table.get("updated_at")) if table.get("updated_at") is not None else None
            input_asof = _instant(table.get("source_asof")) if table.get("source_asof") is not None else None
            if updated is not None and (updated > generated or updated.astimezone(ET).date() != day):
                raise ValueError("noncausal_or_wrong_day_table_clock")
            if input_asof is not None and (input_asof > generated
                    or updated is not None and input_asof > updated
                    or source_asof is not None and input_asof > source_asof):
                raise ValueError("noncausal_table_input_watermark")
            rows, seen = [], set()
            for item in table["rows"]:
                if (not isinstance(item, dict) or not isinstance(item.get("identity"), dict)
                        or item["identity"].get("trading_day") != day.isoformat()
                        or not isinstance(item.get("row"), dict)):
                    raise ValueError("today_identity_required")
                if compact:
                    if (set(item) != {"identity", "row"}
                            or set(item["identity"]) != {"trading_day", "symbol", "display_key"}
                            or item["identity"]["symbol"] not in population
                            or not _sha(item["identity"].get("display_key"))
                            or not set(item["row"]) <= COMPACT_ROW_FIELDS):
                        raise ValueError("compact_display_only_identity_required")
                    identity = tuple(item["identity"].get(field) for field in ("trading_day", "symbol", "display_key"))
                else:
                    identity = tuple(item["identity"].get(field) for field in IDENTITY_FIELDS)
                if (any(not _text(value) for value in identity)
                        or identity in seen):
                    raise ValueError("canonical_unique_today_identity_required")
                seen.add(identity)
                row = item["row"]
                if compact:
                    if any(not _compact_text(v) for v in row.values() if isinstance(v, str)):
                        raise ValueError("compact_public_text_required")
                    if any(not _plain(row.get(field)) for field in ("signal_time", "status", "label")):
                        raise ValueError("compact_display_labels_required")
                    signal_day = _known_cell_day(row["signal_time"])
                    if signal_day is not None and signal_day != day:
                        raise ValueError("compact_signal_clock_wrong_day")
                if (any(not _text(row.get(field)) for field in ROW_FIELDS)
                        or row["ticker"] != item["identity"].get("symbol")
                        or type(row.get("pnl_label_known")) is not bool):
                    raise ValueError("canonical_today_display_row_required")
                if not _plain(row.get("coverage_note"), limit=2048 if key == "LIVE:ACTUAL" else 512):
                    raise ValueError("bounded_plain_coverage_note_required")
                for field in ("entry_time", "exit_time"):
                    clock_day = _known_cell_day(row[field])
                    if clock_day is not None and clock_day != day:
                        raise ValueError("wrong_day_time_cell_not_today")
                if row["pnl_label_known"]:
                    amount = row.get("pnl")
                    if (isinstance(amount, bool) or not isinstance(amount, (str, int, Decimal))
                            or not _finite_decimal(str(amount)).is_finite()):
                        raise ValueError("canonical_finite_pnl_required")
                elif row.get("pnl") is not None or row["pnl_label"] != "UNKNOWN":
                    raise ValueError("unknown_pnl_cannot_become_zero")
                if key == "LIVE:ACTUAL":
                    # REAL retains its prior canonical USD/percent-label
                    # contract; v3 normalized SIM evidence is not a live fill.
                    if not row["pnl_label_known"] and row["pnl_pct_label"] != "UNKNOWN":
                        raise ValueError("unknown_real_pnl_cannot_become_percent")
                else:
                    _percent(row)
                rows.append(dict(row))
            if table["read_status"] == "UNAVAILABLE" and rows:
                raise ValueError("unavailable_table_cannot_present_rows")
            if table["read_status"] != "AVAILABLE" and not table["issues"]:
                raise ValueError("unknown_table_requires_actual_issues")
            if table["read_status"] != "AVAILABLE" and not rows and table["known_pnl_label"] != "UNKNOWN":
                raise ValueError("missing_inputs_must_not_become_success_zero")
            status = ("UNAVAILABLE" if table["read_status"] == "UNAVAILABLE" else
                      "UNKNOWN" if updated is None or input_asof is None else
                      "STALE" if max((now - updated).total_seconds(),
                                     (now - input_asof).total_seconds()) > STALE_SECONDS else
                      table["read_status"])
            result["tables"][key] = dict(table, rows=rows,
                updated_at=updated.isoformat() if updated else None,
                source_asof=input_asof.isoformat() if input_asof else None, display_status=status)
            if updated is None:
                result["tables"][key]["issues"] = [*table["issues"], "TABLE_UPDATE_TIME_UNKNOWN"]
            if input_asof is None:
                result["tables"][key]["issues"] = [*result["tables"][key]["issues"], "TABLE_INPUT_ASOF_UNKNOWN"]
        except (ValueError, TypeError, KeyError, ArithmeticError):
            result["tables"][key]["display_status"] = "INVALID_TABLE"
            result["tables"][key]["issues"] = ["INVALID_TABLE"]
    return result


def read_today_file(path=DEFAULT_PATH, *, now):
    """One regular immutable-inode snapshot; atomic rename never mixes runs."""
    day = now.astimezone(ET).date()
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode) or before.st_size > MAX_BYTES:
                raise ValueError("bounded_regular_today_file_required")
            raw = stream.read(MAX_BYTES + 1)
            after = os.fstat(stream.fileno())
            if (len(raw) > MAX_BYTES or (before.st_size, before.st_mtime_ns)
                    != (after.st_size, after.st_mtime_ns)):
                raise ValueError("today_file_changed_during_read")
        def no_constant(_):
            raise ValueError("nonfinite_today_json")
        document = json.loads(raw, parse_float=_finite_decimal, parse_int=_finite_integer,
                              parse_constant=no_constant,
                              object_pairs_hook=_unique)
        if isinstance(document, dict) and document.get("schema") == COMPACT_SCHEMA and len(raw) >= MAX_COMPACT_BYTES:
            raise ValueError("compact_file_target_exceeded")
        return validate_today_document(document, now=now)
    except (OSError, ValueError, TypeError, ArithmeticError, RecursionError):
        return _unavailable(day, "TODAY_FILE_UNAVAILABLE")
