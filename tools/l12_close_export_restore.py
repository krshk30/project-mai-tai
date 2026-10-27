"""Strict private close-copy decoder. No collection, DDL, provider or permission.

Missing tables/columns never mean empty/default. Every row retains its source
ordinal in the decoded plan. A complete declaration is not native scope proof.
"""

from datetime import UTC, date, datetime
from decimal import Decimal
from hashlib import sha256
import json
import math
from pathlib import Path
from uuid import UUID

from sqlalchemy import Boolean, Date, DateTime, Float, Integer, JSON, Numeric, String, Uuid, func, select, text

from project_mai_tai.db import models, momlive_sec_country
from tools.l12_close_asof_contract import CANDIDATE_HEAD, is_sha
from tools.l12_service_boot_replay import require_private_factory, verify_private_session
from tools.l12_today_close_replay import CLOSE_MS

SOURCE_PINS = {
    "models": "eb0593e46163bdeeedcd72bcf469cdbacdb50499b90e8e39db3393dfddfb9cd4",
    "country": "8a31f0199f2ea8271a2400eee6660933580ad09c94a7e5d2a77c55854a9008ec",
}
TABLES = {table.name: table for table in models.Base.metadata.sorted_tables}
MAX_BYTES = 16 * 1024 * 1024
MAX_ROWS = 100_000


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_json_key")
        result[key] = value
    return result


def strict_json(raw):
    if not isinstance(raw, bytes) or not raw or len(raw) > MAX_BYTES:
        raise ValueError("export_bytes_missing_or_over_bound")

    def bad_constant(value):
        raise ValueError("nonfinite_json")

    return json.loads(raw, object_pairs_hook=unique_object, parse_constant=bad_constant)


def typed_value(column, value):
    if value is None:
        if not column.nullable:
            raise ValueError("nonnull_column_is_null")
        return None
    kind = column.type
    if isinstance(kind, Uuid):
        if not isinstance(value, str) or str(UUID(value)) != value:
            raise ValueError("canonical_uuid_required")
        return UUID(value)
    if isinstance(kind, DateTime):
        if not isinstance(value, str):
            raise ValueError("explicit_timestamp_required")
        result = datetime.fromisoformat(value)
        if result.utcoffset() is None:
            raise ValueError("timezone_required")
        return result.astimezone(UTC)
    if isinstance(kind, Date):
        if not isinstance(value, str):
            raise ValueError("date_string_required")
        return date.fromisoformat(value)
    if isinstance(kind, Boolean):
        if type(value) is not bool:
            raise ValueError("boolean_required")
    elif isinstance(kind, Integer):
        if type(value) is not int:
            raise ValueError("integer_required_no_rounding")
    elif isinstance(kind, Numeric):
        if not isinstance(value, str) or not Decimal(value).is_finite():
            raise ValueError("exact_finite_decimal_string_required")
        return Decimal(value)
    elif isinstance(kind, Float):
        if type(value) not in (int, float) or not math.isfinite(value):
            raise ValueError("finite_float_required")
        return float(value)
    elif isinstance(kind, String):
        if not isinstance(value, str) or (kind.length and len(value) > kind.length):
            raise ValueError("bounded_string_required")
    elif not isinstance(kind, JSON):
        raise ValueError("unreviewed_column_type")
    return value


def normalized(value, *, sqlite=False):
    if isinstance(value, datetime):
        if value.utcoffset() is None:
            if not sqlite:
                raise ValueError("readback_timezone_missing")
            value = value.replace(tzinfo=UTC)  # SQLite fixture loses UTC offset on storage.
        return value.astimezone(UTC).isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return format(value.normalize(), "f")
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, dict):
        return {key: normalized(item, sqlite=sqlite) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [normalized(item, sqlite=sqlite) for item in value]
    return value


def rows_digest(rows, *, sqlite=False):
    encoded = [json.dumps(normalized(dict(row), sqlite=sqlite), sort_keys=True,
                          separators=(",", ":"), allow_nan=False) for row in rows]
    return sha256("\n".join(sorted(encoded)).encode()).hexdigest()


def decode_close_copy(raw, *, reviewed_sha256, reviewed_scope_sha256,
                      expected_snapshot_id, expected_asof_ms):
    if not isinstance(raw, bytes) or not is_sha(reviewed_sha256) or sha256(raw).hexdigest() != reviewed_sha256:
        raise ValueError("unreviewed_export_bytes")
    packet = strict_json(raw)
    actual_pins = {"models": sha256(Path(models.__file__).read_bytes()).hexdigest(),
                   "country": sha256(Path(momlive_sec_country.__file__).read_bytes()).hexdigest()}
    if (not isinstance(packet, dict) or packet.get("version") != 1
            or type(packet.get("version")) is not int
            or packet.get("purpose") != "L12_PRIVATE_CLOSE_COPY"
            or packet.get("candidate_head") != CANDIDATE_HEAD
            or packet.get("source_pins") != SOURCE_PINS or actual_pins != SOURCE_PINS
            or not is_sha(reviewed_scope_sha256)
            or packet.get("scope_sha256") != reviewed_scope_sha256
            or not isinstance(expected_snapshot_id, str) or not expected_snapshot_id
            or packet.get("snapshot_id") != expected_snapshot_id
            or type(expected_asof_ms) is not int or expected_asof_ms < CLOSE_MS
            or type(packet.get("asof_ms")) is not int or packet["asof_ms"] != expected_asof_ms):
        raise ValueError("close_copy_source_scope_or_asof_unknown")
    tables = packet.get("tables")
    if not isinstance(tables, dict) or set(tables) != set(TABLES):
        raise ValueError("complete_code_owned_table_inventory_required")
    decoded, total = {}, 0
    for name, table in TABLES.items():
        source = tables[name]
        rows = source.get("rows") if isinstance(source, dict) else None
        if (not isinstance(rows, list) or source.get("complete") is not True
                or type(source.get("row_count")) is not int or source["row_count"] != len(rows)):
            raise ValueError("table_count_or_completeness_unknown")
        total += len(rows)
        if total > MAX_ROWS:
            raise ValueError("export_row_bound_exceeded_no_truncation")
        decoded[name], identities = [], set()
        for ordinal, row in enumerate(rows, 1):
            if not isinstance(row, dict) or set(row) != set(table.columns.keys()):
                raise ValueError("complete_source_column_inventory_required")
            values = {column.name: typed_value(column, row[column.name]) for column in table.columns}
            for clock in ("created_at", "updated_at", "submitted_at", "event_at", "filled_at",
                          "event_ts", "bar_time"):
                stamp = values.get(clock)
                if isinstance(stamp, datetime) and stamp.timestamp() * 1000 > expected_asof_ms:
                    raise ValueError("row_clock_after_declared_snapshot")
            key = tuple(values[column.name] for column in table.primary_key.columns)
            if key in identities:
                raise ValueError("duplicate_source_primary_key")
            identities.add(key)
            decoded[name].append((ordinal, values))
    for name, table in TABLES.items():
        for column in table.columns:
            for foreign in column.foreign_keys:
                parent = foreign.column
                known = {values[parent.name] for _, values in decoded[parent.table.name]}
                if any(values[column.name] is not None and values[column.name] not in known
                       for _, values in decoded[name]):
                    raise ValueError("missing_source_foreign_identity")
    return {"raw_sha256": reviewed_sha256, "snapshot_id": expected_snapshot_id,
            "asof_ms": expected_asof_ms, "tables": decoded, "source_rows": total,
            "scope_sha256": reviewed_scope_sha256}


def restore_close_copy(raw, factory, **review):
    plan = decode_close_copy(raw, **review)
    require_private_factory(factory)
    engine = factory.kw["bind"]
    if engine.echo:
        raise ValueError("private_payload_logging_forbidden")
    counts = {name: len(rows) for name, rows in plan["tables"].items()}
    try:
        with factory.begin() as session:
            verify_private_session(session)
            if engine.dialect.name == "postgresql":
                session.execute(text("SET LOCAL statement_timeout = '750ms'"))
                session.execute(text("SET LOCAL lock_timeout = '250ms'"))
            for table in models.Base.metadata.sorted_tables:
                if session.scalar(select(func.count()).select_from(table)) != 0:
                    raise ValueError("fresh_empty_private_target_required")
            for table in models.Base.metadata.sorted_tables:
                rows = [values for _, values in plan["tables"][table.name]]
                for start in range(0, len(rows), 32):
                    session.execute(table.insert(), rows[start:start + 32])
                actual = session.execute(select(table)).mappings().all()
                if rows_digest(actual, sqlite=engine.dialect.name == "sqlite") != rows_digest(rows):
                    raise ValueError("private_copy_readback_mismatch")
    except Exception as exc:
        return {"status": "PRIVATE_COPY_FAILED_ROLLED_BACK", "error_type": type(exc).__name__,
                "sqlstate": getattr(getattr(exc, "orig", None), "sqlstate", None),
                "raw_sha256": plan["raw_sha256"], "permission_granted": False}
    return {"status": "PRIVATE_COPY_COMMITTED_AND_READBACK_NOT_NATIVE", "counts": counts,
            "source_rows": plan["source_rows"], "raw_sha256": plan["raw_sha256"],
            "snapshot_id": plan["snapshot_id"], "permission_granted": False}
