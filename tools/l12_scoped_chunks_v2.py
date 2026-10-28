"""Strict scoped/chunked V2 input contract; no export, provider or DB execution.

Every present table is full-PK keyset membership, including explicit empty end
probe. Estimates never become counts. Missing candidate projections remain
source ABSENT and require separate private derivation review.
"""

from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
import json
from pathlib import Path

from sqlalchemy import JSON, MetaData, String, cast, select, tuple_
from sqlalchemy.dialects import postgresql

from tools.l12_close_asof_contract import CANDIDATE_HEAD, is_head, is_sha
from tools.l12_close_export_restore import (
    SOURCE_PINS, models, momlive_sec_country, strict_json, typed_value, normalized, unique_object,
)
from tools.l12_required_copy_scope import (
    REQUIRED, PROJECTIONS, scope_hash, validate_inventory, projection_construction_plan,
)
from tools.l12_today_close_replay import CLOSE_MS

INVENTORY_END = b"END_L12_REQUIRED_SCHEMA_INVENTORY_23_TABLES_ESTIMATES_ONLY"
ACTUAL_METADATA_SHA256 = "a4e1d50b88ddb463134136aae813fb25ebb06bf0f97ef4a04274a26b51106907"
TABLES = {name: models.Base.metadata.tables[name] for name in REQUIRED}
ZERO = "0" * 64
NULL_ADAPTER = {"oms_managed_positions.scales_done": "PRESERVE_SOURCE_NULL_NOT_EMPTY_SCALES"}
SQL_NULLS = "__l12_sql_null_columns"
NULL_FLAG = "__l12_source_is_sql_null__"
JSON_TEXTS = "__l12_json_text"
TEXT_FLAG = "__l12_source_json_text__"
MAX_PART_BYTES = 32 * 1024 * 1024
MAX_CAPTURE_FRAME_BYTES = 4 * ((MAX_PART_BYTES + 2) // 3) + 65536


def bounded_capture_json(raw, *, limit):
    """Explicit part/base64-frame decoder; historical 16-MiB decoder unchanged."""
    if (type(limit) is not int or not 0 < limit <= MAX_CAPTURE_FRAME_BYTES
            or not isinstance(raw, bytes) or not 0 < len(raw) <= limit):
        raise ValueError("bounded_capture_json_bytes_required")
    def invalid(value):
        raise ValueError("nonfinite_capture_json")
    return json.loads(raw, object_pairs_hook=unique_object, parse_constant=invalid)


def canonical(value):
    return json.dumps(normalized(value), sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def type_name(value):
    return "".join(value.lower().replace("character varying", "varchar").split())


def inventory_schema(raw, *, reviewed_sha256):
    if (not isinstance(raw, bytes) or len(raw) > 1024 * 1024 or not is_sha(reviewed_sha256)
            or sha256(raw).hexdigest() != reviewed_sha256):
        raise ValueError("reviewed_metadata_bytes_required")
    lines = raw.strip().splitlines()
    if len(lines) != len(REQUIRED) + 1 or lines[-1] != INVENTORY_END:
        raise ValueError("exact_metadata_records_and_end_required")
    rows = [strict_json(line) for line in lines[:-1]]
    summary = validate_inventory(rows)
    if summary["status"] == "UNKNOWN":
        raise ValueError("source_inventory_scope_unknown")
    shape, adapters = {}, {}
    for row in rows:
        name, table = row["table"], TABLES[row["table"]]
        if row["exists"] is False:
            shape[name] = {"source_state": "ABSENT_CANDIDATE_PROJECTION"}
            continue
        expected = [{"name": col.name, "type": type_name(str(col.type.compile(dialect=postgresql.dialect()))),
                     "not_null": not col.nullable} for col in table.columns]
        actual = [dict(col, type=type_name(col["type"])) for col in row["columns"]]
        expected_by_name = {col["name"]: col for col in expected}
        actual_by_name = {col["name"]: col for col in actual}
        for key, actual_col in actual_by_name.items():
            qualified = name + "." + key
            model_col = expected_by_name.get(key)
            if (qualified in NULL_ADAPTER and model_col is not None
                    and actual_col == dict(model_col, not_null=False) and model_col["not_null"] is True):
                adapters[qualified] = NULL_ADAPTER[qualified]
                expected_by_name[key] = dict(model_col, not_null=False)
        if (len(actual) != len(expected)
                or actual_by_name != expected_by_name
                or row["primary_key_columns"] != [col.name for col in table.primary_key.columns]):
            raise ValueError("actual_source_schema_requires_explicit_adapter")
        shape[name] = dict(source_state="PRESENT", columns=sorted(actual, key=lambda col: col["name"]),
            primary_key=row["primary_key_columns"],
            foreign_keys=sorted(row["foreign_key_targets"], key=lambda fk: (fk["schema"], fk["table"])))
    return dict(rows=rows, metadata_sha256=reviewed_sha256, schema=shape,
                schema_sha256=sha256(canonical(shape)).hexdigest(), inventory=summary,
                required_column_adapters=adapters)


@dataclass(frozen=True)
class Limits:
    rows_per_chunk: int
    chunk_bytes: int
    manifest_bytes: int
    chunks: int

    def __post_init__(self):
        if (any(type(value) is not int or value <= 0 for value in
                (self.rows_per_chunk, self.chunk_bytes, self.manifest_bytes, self.chunks))
                or self.rows_per_chunk > 128 or self.chunk_bytes > MAX_PART_BYTES
                or self.manifest_bytes > 16 * 1024 * 1024):
            raise ValueError("reviewed_bounded_chunk_policy_required")


def parse_key(name, raw):
    columns = tuple(TABLES[name].primary_key.columns)
    if not isinstance(raw, list) or len(raw) != len(columns):
        raise ValueError("full_composite_primary_key_required")
    values = tuple(typed_value(col, value) for col, value in zip(columns, raw))
    if any(value is None for value in values):
        raise ValueError("primary_key_null")
    return values


def keyset_query(name, *, after_key, page_rows):
    """Source-only query builder: full table, no OFFSET or other predicates.

    The eventual authorized collector must keep the SAME read-only snapshot
    for every chunk and end probe. This function never executes a statement.
    """
    if name not in REQUIRED or type(page_rows) is not int or not 1 <= page_rows <= 128:
        raise ValueError("reviewed_source_table_and_chunk_size_required")
    table = TABLES[name].to_metadata(MetaData(), schema="public")
    keys = tuple(table.primary_key.columns)
    ordered = tuple(col.collate("C") if isinstance(col.type, String) else col for col in keys)
    flags = [col.is_(None).label(NULL_FLAG + col.name) for col in table.columns if isinstance(col.type, JSON)]
    texts = [cast(col, String).label(TEXT_FLAG + col.name) for col in table.columns if isinstance(col.type, JSON)]
    query = select(table, *flags, *texts).order_by(*ordered).limit(page_rows)
    if after_key is not None:
        query = query.where(tuple_(*ordered) > tuple_(*parse_key(name, after_key)))
    compiled = query.compile(dialect=postgresql.dialect(), compile_kwargs={"render_postcompile": True})
    fingerprint = sha256(canonical(dict(sql=str(compiled), params=compiled.params))).hexdigest()
    return query, fingerprint


def read_part(reader, descriptor, *, limit):
    if (type(limit) is not int or not 0 < limit <= MAX_PART_BYTES
            or not isinstance(descriptor, dict) or not isinstance(descriptor.get("reference"), str)
            or not descriptor["reference"] or len(descriptor["reference"]) > 512
            or not is_sha(descriptor.get("sha256")) or type(descriptor.get("bytes")) is not int
            or not 0 < descriptor["bytes"] <= limit):
        raise ValueError("bounded_part_descriptor_required")
    raw = reader(descriptor["reference"])  # Contract: already-local bytes only.
    if (not isinstance(raw, bytes) or len(raw) != descriptor["bytes"]
            or sha256(raw).hexdigest() != descriptor["sha256"]):
        raise ValueError("part_bytes_changed_or_unreviewed")
    return bounded_capture_json(raw, limit=limit)


def scoped_value(name, col, value, adapters):
    if value is None and adapters.get(name + "." + col.name) == NULL_ADAPTER.get(name + "." + col.name) and name + "." + col.name in NULL_ADAPTER:
        return None  # Actual source NULL retained; not defaulted into a permission fact.
    return typed_value(col, value)


def encode_source_row(name, values):
    """Package actual query values plus actual IS NULL flags, never infer flags.

    Called by a future authorized collector or private readback, not a provider.
    No field is replaced by defaults. JSON null and SQL NULL remain distinct.
    """
    table, result, nulls, texts = TABLES[name], {}, [], {}
    expected = {col.name for col in table.columns}
    expected.update(NULL_FLAG + col.name for col in table.columns if isinstance(col.type, JSON))
    expected.update(TEXT_FLAG + col.name for col in table.columns if isinstance(col.type, JSON))
    if set(values) != expected:
        raise ValueError("source_values_and_json_is_null_flags_required")
    for col in table.columns:
        result[col.name] = values[col.name]
        if isinstance(col.type, JSON):
            flag = values[NULL_FLAG + col.name]
            if type(flag) is not bool or flag and values[col.name] is not None:
                raise ValueError("source_json_null_flag_inconsistent")
            if flag:
                nulls.append(col.name)
            texts[col.name] = values[TEXT_FLAG + col.name]
    result[SQL_NULLS] = sorted(nulls)
    result[JSON_TEXTS] = texts
    _json_text_identity(table, result)
    return result


def _json_text_identity(table, row):
    texts = row.get(JSON_TEXTS)
    names = {col.name for col in table.columns if isinstance(col.type, JSON)}
    if not isinstance(texts, dict) or set(texts) != names:
        raise ValueError("all_original_json_server_text_required")
    for name, data in texts.items():
        if name in row[SQL_NULLS]:
            if data is not None:
                raise ValueError("sql_null_has_no_json_server_text")
            continue
        if not isinstance(data, str):
            raise ValueError("json_server_text_missing")
        # PostgreSQL JSON may legally retain duplicate object keys. Preserve the
        # exact text; its decoded last-key semantics must match the driver value.
        def invalid(value):
            raise ValueError("nonfinite_json_server_text")
        parsed = json.loads(data, parse_constant=invalid)
        if canonical(parsed) != canonical(row[name]):
            raise ValueError("json_server_text_semantics_disagree")


def decoded_rows(name, rows, *, asof_ms, previous, adapters):
    table = TABLES[name]
    if not isinstance(rows, list):
        raise ValueError("rows_array_required")
    output = []
    for row in rows:
        if not isinstance(row, dict) or set(row) != {col.name for col in table.columns} | {SQL_NULLS, JSON_TEXTS}:
            raise ValueError("all_source_columns_required")
        nulls = row[SQL_NULLS]
        json_columns = {col.name for col in table.columns if isinstance(col.type, JSON)}
        if (not isinstance(nulls, list) or any(not isinstance(value, str) for value in nulls)
                or nulls != sorted(set(nulls)) or not set(nulls) <= json_columns
                or any(row[col] is not None for col in nulls)):
            raise ValueError("explicit_sql_json_null_identity_required")
        _json_text_identity(table, row)
        values = {}
        for col in table.columns:
            if isinstance(col.type, JSON) and row[col.name] is None and col.name not in nulls:
                values[col.name] = None  # Explicit JSON null, not a SQL NOT NULL violation.
            else:
                values[col.name] = scoped_value(name, col, row[col.name], adapters)
        values[SQL_NULLS] = list(nulls)
        values[JSON_TEXTS] = dict(row[JSON_TEXTS])
        for clock in ("created_at", "updated_at", "submitted_at", "event_at", "filled_at", "event_ts", "bar_time"):
            stamp = values.get(clock)
            if isinstance(stamp, datetime) and stamp.timestamp() * 1000 > asof_ms:
                raise ValueError("row_clock_after_snapshot")
        key = tuple(values[col.name] for col in table.primary_key.columns)
        if any(value is None for value in key) or previous is not None and not previous < key:
            raise ValueError("duplicate_or_nonmonotonic_full_primary_key")
        output.append(values)
        previous = key
    return output, previous


def validate_v2(manifest_raw, metadata_raw, reader, *, reviewed_manifest_sha256,
                reviewed_metadata_sha256, expected_capture_head, expected_snapshot_id,
                expected_asof_ms, limits, projection_approval, column_adapter_approval):
    if (not isinstance(limits, Limits) or not isinstance(manifest_raw, bytes)
            or len(manifest_raw) > limits.manifest_bytes or not is_sha(reviewed_manifest_sha256)
            or sha256(manifest_raw).hexdigest() != reviewed_manifest_sha256):
        raise ValueError("reviewed_v2_manifest_required")
    manifest = strict_json(manifest_raw)
    inventory = inventory_schema(metadata_raw, reviewed_sha256=reviewed_metadata_sha256)
    adapters = inventory["required_column_adapters"]
    if adapters and (not isinstance(column_adapter_approval, dict)
            or column_adapter_approval.get("source_metadata_sha256") != reviewed_metadata_sha256
            or column_adapter_approval.get("capture_head") != expected_capture_head
            or column_adapter_approval.get("adapters") != adapters
            or column_adapter_approval.get("private_schema_only_preserve_source_nulls") is not True
            or not isinstance(column_adapter_approval.get("review_id"), str)
            or not column_adapter_approval["review_id"]):
        raise ValueError("actual_nullable_source_column_requires_explicit_private_adapter_review")
    actual_pins = {"models": sha256(Path(models.__file__).read_bytes()).hexdigest(),
                   "country": sha256(Path(momlive_sec_country.__file__).read_bytes()).hexdigest()}
    if (type(manifest.get("version")) is not int or manifest["version"] != 2
            or manifest.get("purpose") != "L12_SCOPED_CHUNKED_CLOSE_COPY"
            or manifest.get("json_null_encoding") != "PER_ROW_SOURCE_IS_NULL_FLAGS_V1"
            or manifest.get("json_text_encoding") != "ORIGINAL_SERVER_TEXT_V1"
            or manifest.get("schema_component_head") != CANDIDATE_HEAD
            or manifest.get("source_pins") != SOURCE_PINS or actual_pins != SOURCE_PINS
            or manifest.get("scope_sha256") != scope_hash()
            or manifest.get("schema_sha256") != inventory["schema_sha256"]
            or manifest.get("column_adapters") != adapters
            or manifest.get("metadata_sha256") != reviewed_metadata_sha256
            or not is_head(expected_capture_head) or manifest.get("capture_head") != expected_capture_head
            or not isinstance(expected_snapshot_id, str) or not expected_snapshot_id
            or manifest.get("snapshot_id") != expected_snapshot_id
            or type(expected_asof_ms) is not int or expected_asof_ms < CLOSE_MS
            or type(manifest.get("asof_ms")) is not int or manifest["asof_ms"] != expected_asof_ms
            or manifest.get("limits") != vars(limits)
            or manifest.get("snapshot_policy") != "ONE_READONLY_REPEATABLE_READ_FULL_PK_NO_OTHER_PREDICATES"
            or not isinstance(manifest.get("tables"), dict) or set(manifest["tables"]) != set(REQUIRED)):
        raise ValueError("v2_source_scope_snapshot_binding_unknown")
    construction = projection_construction_plan(inventory["rows"], projection_approval,
        observed_inventory_sha256=reviewed_metadata_sha256, observed_capture_root=expected_capture_head)
    totals, refs, parts = {}, set(), 0
    for name in REQUIRED:
        entry = manifest["tables"][name]
        if not isinstance(entry, dict):
            raise ValueError("table_descriptor_required")
        if inventory["schema"][name]["source_state"] == "ABSENT_CANDIDATE_PROJECTION":
            if entry != {"source_state": "ABSENT_CANDIDATE_PROJECTION"}:
                raise ValueError("source_absence_must_not_be_encoded_empty")
            totals[name] = dict(source_state="ABSENT_CANDIDATE_PROJECTION")
            continue
        if (entry.get("source_state") != "PRESENT_FULL_EXHAUSTED"
                or not isinstance(entry.get("chunks"), list)
                or type(entry.get("exact_export_rows")) is not int or entry["exact_export_rows"] < 0
                or type(entry.get("exact_export_bytes")) is not int or entry["exact_export_bytes"] < 0):
            raise ValueError("explicit_full_table_export_required")
        previous, previous_raw, prior_sha = None, None, ZERO
        row_hash, pk_hash, count, size = sha256(), sha256(), 0, 0
        for sequence, descriptor in enumerate(entry["chunks"], 1):
            parts += 1
            if parts > limits.chunks:
                raise ValueError("reviewed_chunk_resource_budget_exceeded_no_truncation")
            part = read_part(reader, descriptor, limit=limits.chunk_bytes)
            if descriptor["reference"] in refs:
                raise ValueError("part_reference_reused")
            refs.add(descriptor["reference"])
            _, query_sha = keyset_query(name, after_key=previous_raw, page_rows=limits.rows_per_chunk)
            if (part.get("table") != name or part.get("snapshot_id") != expected_snapshot_id
                    or type(part.get("sequence")) is not int or part["sequence"] != sequence
                    or part.get("previous_sha256") != prior_sha or part.get("query_sha256") != query_sha
                    or part.get("asof_ms") != expected_asof_ms or type(part.get("asof_ms")) is not int):
                raise ValueError("chunk_query_chain_or_snapshot_mismatch")
            rows, last = decoded_rows(name, part.get("rows"), asof_ms=expected_asof_ms, previous=previous, adapters=adapters)
            if not 1 <= len(rows) <= limits.rows_per_chunk:
                raise ValueError("chunk_rows_over_budget_or_empty")
            first_key = [rows[0][col.name] for col in TABLES[name].primary_key.columns]
            if (parse_key(name, descriptor.get("first_key")) != tuple(first_key)
                    or parse_key(name, descriptor.get("last_key")) != last
                    or type(descriptor.get("rows")) is not int or descriptor["rows"] != len(rows)):
                raise ValueError("chunk_primary_key_bounds_or_count_mismatch")
            for row in rows:
                row_hash.update(canonical(row) + b"\n")
                pk_hash.update(canonical([row[col.name] for col in TABLES[name].primary_key.columns]) + b"\n")
            count += len(rows)
            size += descriptor["bytes"]
            previous, previous_raw, prior_sha = last, normalized(list(last)), descriptor["sha256"]
        end = read_part(reader, entry.get("end_probe"), limit=limits.chunk_bytes)
        if entry["end_probe"]["reference"] in refs:
            raise ValueError("end_probe_reference_reused")
        refs.add(entry["end_probe"]["reference"])
        _, end_query = keyset_query(name, after_key=previous_raw, page_rows=limits.rows_per_chunk)
        if (end != dict(table=name, snapshot_id=expected_snapshot_id, asof_ms=expected_asof_ms,
                        query_sha256=end_query, after_key=previous_raw, rows=[])):
            raise ValueError("same_snapshot_empty_keyset_exhaustion_required")
        if (entry["exact_export_rows"] != count or entry["exact_export_bytes"] != size
                or entry.get("rows_sha256") != row_hash.hexdigest() or entry.get("pk_sha256") != pk_hash.hexdigest()):
            raise ValueError("full_table_export_membership_digest_mismatch")
        totals[name] = dict(source_state="PRESENT_FULL_EXHAUSTED", exact_export_rows=count,
                            exact_export_bytes=size, pk_sha256=pk_hash.hexdigest(), rows_sha256=row_hash.hexdigest())
    return dict(status="V2_BYTE_PK_CHAIN_BOUND_NOT_RESTORED_OR_NATIVE_PROOF", tables=totals,
        chunks=parts, manifest_sha256=reviewed_manifest_sha256, metadata_sha256=reviewed_metadata_sha256,
        schema_sha256=inventory["schema_sha256"], projection_construction=construction,
        required_column_adapters=adapters,
        foreign_key_row_validation="UNRUN_REQUIRES_PRIVATE_RESTORER", permission_granted=False,
        snapshot_id=expected_snapshot_id, asof_ms=expected_asof_ms)
