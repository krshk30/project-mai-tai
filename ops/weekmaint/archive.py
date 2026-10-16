"""Transactional archive primitives; caller must hold reviewed quiescent maintenance gates."""
from __future__ import annotations

import csv
import hashlib
import io
import json
import os
from pathlib import Path
from uuid import UUID

from sqlalchemy import text

from ops.weekmaint.policy import REASON, RETIRE_TABLES, Refused, encoded

DDL = (
    """CREATE TABLE IF NOT EXISTS weekmaint_runs (
        operation_id uuid PRIMARY KEY, csv_name text NOT NULL, csv_sha256 text NOT NULL,
        row_count integer NOT NULL, committed_at timestamptz NOT NULL DEFAULT now())""",
    """CREATE TABLE IF NOT EXISTS weekmaint_archive (
        operation_id uuid NOT NULL REFERENCES weekmaint_runs(operation_id),
        table_name text NOT NULL, row_id uuid NOT NULL, source_sha256 text NOT NULL,
        account_name text NOT NULL, symbol text NOT NULL, reason text NOT NULL,
        source_json_text text NOT NULL, original_row jsonb NOT NULL, changes jsonb NOT NULL,
        PRIMARY KEY(table_name,row_id,source_sha256))""",
)
ALLOWED = {
    "broker_orders": {"status", "payload", "updated_at"},
    "trade_intents": {"status", "payload", "updated_at"},
    "oms_managed_positions": {"status", "updated_at"},
    "virtual_positions": {"quantity", "updated_at"},
}
HEADER = ("operation_id", "table", "row_id", "account", "symbol", "source_sha256",
          "reason", "source_json_text", "changes_json")


def csv_bytes(operation, records):
    stream = io.StringIO(newline="")
    writer = csv.writer(stream, lineterminator="\n")
    writer.writerow(HEADER)
    for row in sorted(records, key=lambda value: (value["table"], value["id"], value["source_sha256"])):
        writer.writerow((str(operation), row["table"], row["id"], row["account"], row["symbol"],
                         row["source_sha256"], REASON, row["source_json_text"], row["changes_json"]))
    return stream.getvalue().encode()


def private_directory(directory):
    directory = Path(directory)
    if directory.is_symlink() or directory.parent.is_symlink():
        raise Refused("audit_directory_symlink")
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    if directory.stat().st_uid != os.getuid() or directory.stat().st_mode & 0o077:
        raise Refused("audit_directory_not_private")
    return directory


def stage_file(path, raw):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    fsync_directory(path.parent)


def fsync_directory(directory):
    descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def publish(staged, final, expected):
    if final.exists() or final.is_symlink():
        if final.is_symlink() or hashlib.sha256(final.read_bytes()).hexdigest() != expected:
            raise Refused("audit_CSV_existing_conflict")
    else:
        os.link(staged, final)  # Atomic no-replace, not an overwrite of an audit.
    staged.unlink()
    fsync_directory(final.parent)


def archive_transaction(engine, operation, retirements, directory):
    """CSV staged/fsynced before DB commit; published CSV recovered from archive on crash."""
    operation = UUID(str(operation))
    retirements = tuple(retirements)
    directory = private_directory(directory)
    records = []
    seen = set()
    for item in retirements:
        key = (item.table, item.row_id)
        if (item.table not in RETIRE_TABLES or key in seen or item.before_json is None
                or set(item.changes) - ALLOWED[item.table]):
            raise Refused("archive_plan_identity_columns_or_original_text_UNKNOWN")
        seen.add(key)
        UUID(item.row_id)
        records.append({"table": item.table, "id": item.row_id, "account": item.account,
            "symbol": item.symbol, "source_sha256": item.source_hash,
            "source_json_text": item.before_json, "changes_json": encoded(item.changes).decode()})
    raw = csv_bytes(operation, records)
    digest = hashlib.sha256(raw).hexdigest()
    final = directory / f"{operation}.csv"
    staged = directory / f"{operation}.csv.staged"
    stage_file(staged, raw)
    # If commit fails, staged evidence remains explicitly uncommitted. Never
    # treat CSV existence as proof that rows changed or retry blindly.
    with engine.begin() as connection:
        if connection.dialect.name != "postgresql":
            raise Refused("archive_requires_actual_PostgreSQL_transaction")
        connection.execute(text("SET LOCAL statement_timeout='5s'"))
        connection.execute(text("SET LOCAL lock_timeout='2s'"))
        connection.execute(text("SET LOCAL timezone='UTC'"))
        connection.execute(text("SELECT pg_advisory_xact_lock(hashtext('project-mai-tai-weekmaint'))"))
        for statement in DDL:
            connection.execute(text(statement))
        connection.execute(text("INSERT INTO weekmaint_runs(operation_id,csv_name,csv_sha256,row_count) "
                                "VALUES(:operation,:name,:digest,:count)"),
                           {"operation": operation, "name": final.name, "digest": digest, "count": len(records)})
        for item, record in zip(retirements, records, strict=True):
            current = connection.execute(text(
                f"SELECT to_jsonb(t)::text FROM public.{item.table} t WHERE id=:id FOR UPDATE"),
                {"id": UUID(item.row_id)}).scalar_one()
            if hashlib.sha256(current.encode()).hexdigest() != item.source_hash:
                raise Refused("retirement_row_changed_after_quiescent_plan")
            # Separate binds avoid conflicting psycopg text/jsonb inference;
            # both receive the identical original text, without re-encoding.
            connection.execute(text("""INSERT INTO weekmaint_archive
                (operation_id,table_name,row_id,source_sha256,account_name,symbol,reason,
                 source_json_text,original_row,changes)
                VALUES(:operation,:table,:id,:source_sha256,:account,:symbol,:reason,
                       CAST(:source_json_text AS text),CAST(:original_row_json AS jsonb),CAST(:changes_json AS jsonb))"""),
                {**record, "original_row_json": record["source_json_text"],
                 "id": UUID(item.row_id), "operation": operation, "reason": REASON})
            expressions, values = [], {"id": UUID(item.row_id)}
            for column, value in sorted(item.changes.items()):
                if column == "payload":
                    expressions.append("payload=(COALESCE(payload::jsonb,'{}'::jsonb) || CAST(:payload AS jsonb))::json")
                    values[column] = encoded(value).decode()
                elif column == "updated_at":
                    expressions.append("updated_at=CAST(:updated_at AS timestamptz)")
                    values[column] = value
                elif column == "quantity":
                    expressions.append("quantity=CAST(:quantity AS numeric)")
                    values[column] = value
                else:
                    expressions.append(f"{column}=:{column}")
                    values[column] = value
            result = connection.execute(text(
                f"UPDATE public.{item.table} SET {','.join(expressions)} WHERE id=:id"), values)
            if result.rowcount != 1:
                raise Refused("retirement_exact_row_update_failed")
    publish(staged, final, digest)
    return {"operation": str(operation), "rows": len(records), "csv_sha256": digest, "csv": str(final)}


def recover_committed_csv(engine, operation, directory):
    operation = UUID(str(operation))
    with engine.connect() as connection:
        run = connection.execute(text("SELECT csv_name,csv_sha256,row_count FROM weekmaint_runs "
                                      "WHERE operation_id=:operation"), {"operation": operation}).mappings().one()
        rows = connection.execute(text('SELECT table_name AS "table",row_id AS id,account_name AS account,symbol,'
            "source_sha256,source_json_text,changes::text AS changes_json FROM weekmaint_archive "
            "WHERE operation_id=:operation"), {"operation": operation}).mappings().all()
    # Normalize JSON serialization identically, preserving original row JSON verbatim.
    rows = [{**row, "id": str(row["id"]), "changes_json": encoded(json.loads(row["changes_json"])).decode()} for row in rows]
    raw = csv_bytes(operation, rows)
    if len(rows) != run["row_count"] or hashlib.sha256(raw).hexdigest() != run["csv_sha256"]:
        raise Refused("committed_archive_CSV_digest_inconsistent")
    directory = private_directory(directory)
    if run["csv_name"] != f"{operation}.csv":
        raise Refused("archive_CSV_identity_UNKNOWN")
    final, staged = directory / run["csv_name"], directory / f"{operation}.csv.recovery"
    stage_file(staged, raw)
    publish(staged, final, run["csv_sha256"])
    return final
