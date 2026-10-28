"""Private-only streaming V2 restoration; no DDL, provider, boot or retry.

Full byte/key validation precedes target access. Source parts are revalidated
when consumed. All rows are read back before commit; unknown commit/journal
outcomes preserve the target and never authorize service start.
"""

from copy import deepcopy
from collections.abc import Mapping
from hashlib import sha256
import sys

from sqlalchemy import JSON, MetaData, String, bindparam, cast, inspect, null, select, text, tuple_, update

from tools.l12_scoped_chunks_v2 import (
    TABLES, canonical, decoded_rows, inventory_schema, read_part, strict_json, type_name, validate_v2,
    encode_source_row, NULL_FLAG, SQL_NULLS, JSON_TEXTS, TEXT_FLAG,
)
from tools.l12_private_schema_evidence import (
    C1_BASELINE, C1_GRAPH_VERSION, EXACT_UNIQUENESS, is_c1_review, validate_c1_review, verify_canonical_catalog,
)
from tools.l12_required_copy_scope import PROJECTIONS
from tools.l12_service_boot_replay import require_private_factory, verify_private_session
from tools.l12_close_export_restore import normalized


def require_named_source_row(table, row):
    """All21 source insert/readback paths must expose column names, not offsets."""
    if not isinstance(row, Mapping) or not set(table.c.keys()).issubset(row.keys()):
        raise ValueError("all21_restore_requires_by_name_mapping_never_positional")
    return row


def private_schema(metadata_raw, *, reviewed_metadata_sha256, schema_review=None):
    """Returns schema objects ONLY. Caller needs separate isolated-DDL authority.

    The one source nullable-column adapter affects the private target only;
    source NULLs are never replaced by candidate defaults or permission facts.
    """
    source = inventory_schema(metadata_raw, reviewed_sha256=reviewed_metadata_sha256)
    metadata = MetaData()
    baseline = is_c1_review(schema_review) and validate_c1_review(schema_review) == C1_BASELINE
    for name, table in TABLES.items():
        if baseline and name in PROJECTIONS:
            continue
        table.to_metadata(metadata)
    for name in source["required_column_adapters"]:
        table, column = name.split(".")
        metadata.tables[table].c[column].nullable = True
    return metadata


def _keys(table, dialect):
    return tuple(col.collate("C" if dialect == "postgresql" else "BINARY")
                 if isinstance(col.type, String) else col for col in table.primary_key.columns)


def _target_schema(session, metadata, schema_review, *, native_projection=None):
    inspector = inspect(session.connection())
    for table in metadata.tables.values():
        actual = inspector.get_columns(table.name)
        expected = {col.name: col.nullable for col in table.columns}
        dialect = session.get_bind().dialect
        expected_types = {col.name: type_name(str(col.type.compile(dialect=dialect))) for col in table.columns}
        if ({col["name"]: col["nullable"] for col in actual} != expected
                or {col["name"]: type_name(str(col["type"].compile(dialect=dialect))) for col in actual} != expected_types
                or inspector.get_pk_constraint(table.name)["constrained_columns"] != [col.name for col in table.primary_key.columns]):
            raise ValueError("private_schema_column_or_pk_mismatch")
        required = {(tuple(fk.column_keys), fk.referred_table.name,
                     tuple(element.column.name for element in fk.elements)) for fk in table.foreign_key_constraints}
        present = {(tuple(fk["constrained_columns"]), fk["referred_table"],
                    tuple(fk["referred_columns"])) for fk in inspector.get_foreign_keys(table.name)}
        if required != present:
            raise ValueError("private_foreign_constraints_mismatch")
    if native_projection is None:
        return verify_canonical_catalog(session.connection(), metadata, schema_review)
    return verify_canonical_catalog(session.connection(), metadata, schema_review, native_projection=native_projection)


def restore_v2(manifest_raw, metadata_raw, reader, factory, *, review, require_held_proof, journal_committed,
               schema_review=None, target_capability=None, native_projection=None):
    if is_c1_review(schema_review) and schema_review["version"] != C1_GRAPH_VERSION:
        raise ValueError("C1_row_DML_requires_graph_bound_V2_not_old_reflected_approval")
    plan = validate_v2(manifest_raw, metadata_raw, reader, **review)
    report = dict(status="V2_PRIVATE_COPY_NOT_STARTED", manifest_sha256=plan["manifest_sha256"],
        metadata_sha256=plan["metadata_sha256"], permission_granted=False, source_tables=plan["tables"],
        committed=False, source_absent_projections=plan["projection_construction"],
        excluded_source_tables="NOT_EXPORTED_NOT_PROVEN_EMPTY_NO_SERVICE_READER_PERMISSION")
    binding_values = dict(manifest=plan["manifest_sha256"], metadata=plan["metadata_sha256"],
        schema=plan["schema_sha256"], schema_review=schema_review,
        review=dict(review, limits=vars(review["limits"])))
    if target_capability is not None:
        from tools.l12_main_nonce_target import MainNonceTarget
        if type(target_capability) is not MainNonceTarget:
            raise ValueError("reviewed_Main_nonce_target_capability_required")
        binding_values["target_capability_sha256"] = target_capability.reviewed_sha256
        report["target_capability_sha256"] = target_capability.reviewed_sha256
    binding = sha256(canonical(binding_values)).hexdigest()
    if not callable(require_held_proof) or not callable(journal_committed) or require_held_proof(binding) is not True:
        report["status"] = "V2_PRIVATE_WRITER_PROOF_UNKNOWN"
        return report
    if target_capability is None:
        require_private_factory(factory)
    else:
        require_private_factory(factory, target_capability=target_capability)
    engine = factory.kw["bind"]
    if engine.echo:
        raise ValueError("private_payload_logging_forbidden")
    metadata = private_schema(metadata_raw, reviewed_metadata_sha256=review["reviewed_metadata_sha256"],
        schema_review=schema_review)
    if is_c1_review(schema_review):
        report["target_purpose"] = validate_c1_review(schema_review)
    manifest = strict_json(manifest_raw)
    adapters = plan["required_column_adapters"]
    session, transaction, commit_started = None, None, False

    def held():
        if require_held_proof(binding) is not True:
            raise ValueError("private_writer_epoch_changed")

    def batches(name):
        previous = None
        for descriptor in manifest["tables"][name].get("chunks", []):
            held()
            part = read_part(reader, descriptor, limit=review["limits"].chunk_bytes)
            rows, previous = decoded_rows(name, part["rows"], asof_ms=plan["asof_ms"],
                                           previous=previous, adapters=adapters)
            for row in rows:
                require_named_source_row(metadata.tables[name], row)
            yield rows

    try:
        session = factory()
        transaction = session.begin()
        if engine.dialect.name == "postgresql":
            session.execute(text("SET LOCAL statement_timeout = '750ms'"))
            session.execute(text("SET LOCAL lock_timeout = '250ms'"))
            if session.scalar(text("SHOW session_replication_role")) != "origin":
                raise ValueError("private_constraints_not_origin")
        elif engine.dialect.name == "sqlite":
            if session.scalar(text("PRAGMA foreign_keys")) != 1:
                raise ValueError("private_sqlite_foreign_constraints_not_enabled")
        if target_capability is None:
            verify_private_session(session)
        else:
            verify_private_session(session, target_capability=target_capability)
        report["verified_private_schema_sha256"] = _target_schema(session, metadata, schema_review,
            native_projection=native_projection)
        for table in metadata.tables.values():
            if session.execute(select(*table.primary_key.columns).limit(1)).first() is not None:
                raise ValueError("fresh_empty_private_scope_required")
        for table in metadata.sorted_tables:
            for rows in batches(table.name):
                if table.name == "bot_control_states":
                    rows = [dict(row, active_command_id=None) for row in rows]
                groups = {}
                for row in rows:
                    sql_nulls = tuple(row[SQL_NULLS])
                    values = {key: value for key, value in row.items() if key not in {SQL_NULLS, JSON_TEXTS} and key not in sql_nulls}
                    for col in table.columns:
                        if isinstance(col.type, JSON) and col.name in values:
                            values[col.name] = row[JSON_TEXTS][col.name]
                    groups.setdefault(sql_nulls, []).append(values)
                for sql_nulls, group in groups.items():
                    expressions = {key: null() for key in sql_nulls}
                    for col in table.columns:
                        if isinstance(col.type, JSON) and col.name not in sql_nulls:
                            parameter = bindparam(col.name, type_=String())
                            expressions[col.name] = cast(parameter, col.type) if engine.dialect.name == "postgresql" else parameter
                    statement = table.insert().values(expressions)
                    for index in range(0, len(group), 32):
                        held()
                        session.execute(statement, group[index:index + 32])
        # Known FK cycle: stage nullable active_command_id privately inside this
        # uncommitted transaction, then restore EXACT source value after commands.
        states = metadata.tables["bot_control_states"]
        for rows in batches(states.name):
            for row in rows:
                result = session.execute(update(states).where(*(
                    col == row[col.name] for col in states.primary_key.columns))
                    .values(active_command_id=row["active_command_id"]))
                if result.rowcount != 1:
                    raise ValueError("private_cycle_identity_mismatch")
        verified = {}
        for table in metadata.sorted_tables:
            keys, previous, count = _keys(table, engine.dialect.name), None, 0
            for rows in batches(table.name):
                last = tuple(rows[-1][col.name] for col in table.primary_key.columns)
                flags = [col.is_(None).label(NULL_FLAG + col.name) for col in table.columns if isinstance(col.type, JSON)]
                texts = [cast(col, String).label(TEXT_FLAG + col.name) for col in table.columns if isinstance(col.type, JSON)]
                query = select(table, *flags, *texts).where(tuple_(*keys) <= tuple_(*last)).order_by(*keys).limit(len(rows) + 1)
                if previous is not None:
                    query = query.where(tuple_(*keys) > tuple_(*previous))
                actual = session.execute(query).mappings().all()
                for row in actual:
                    require_named_source_row(table, row)
                if ([normalized(encode_source_row(table.name, row), sqlite=engine.dialect.name == "sqlite") for row in actual]
                        != [normalized(row) for row in rows]):
                    raise ValueError("private_stream_readback_mismatch")
                previous, count = last, count + len(rows)
            tail = select(*table.primary_key.columns).order_by(*keys).limit(1)
            if previous is not None:
                tail = tail.where(tuple_(*keys) > tuple_(*previous))
            if session.execute(tail).first() is not None:
                raise ValueError("unexpected_private_membership_after_tail")
            verified[table.name] = count
        held()
        report["verified_private_rows"] = verified
        commit_started = True
        transaction.commit()
        report.update(status="V2_PRIVATE_COPY_COMMITTED_JOURNAL_PENDING", committed=True)
    except Exception as exc:
        report.update(status="V2_COMMIT_OUTCOME_UNKNOWN_NO_BOOT" if commit_started else "V2_COPY_FAILED_ROLLBACK_PENDING",
            failure_type=type(exc).__name__, sqlstate=getattr(getattr(exc, "orig", None), "sqlstate", None))
        if commit_started:
            report["committed"] = None  # Neither proven committed nor proven rolled back.
        if not commit_started and transaction is not None:
            try:
                transaction.rollback()
                report["status"] = "V2_COPY_FAILED_ROLLED_BACK_NO_BOOT"
            except Exception:
                report["status"] = "V2_ROLLBACK_OUTCOME_UNKNOWN_NO_BOOT"
        return report
    finally:
        if session is not None:
            try:
                session.close()
            except Exception as exc:
                report["cleanup_failure_type"] = type(exc).__name__
    try:
        journal_committed(binding, deepcopy(report))
    except Exception as exc:
        report.update(status="V2_COPY_COMMITTED_JOURNAL_UNKNOWN_NO_BOOT", journal_failure_type=type(exc).__name__)
        return report
    report["status"] = ("V2_COPY_COMMITTED_CLEANUP_UNKNOWN_JOURNALED_NO_BOOT" if "cleanup_failure_type" in report
                        else "V2_PRIVATE_STREAM_COPY_READBACK_COMMITTED_NOT_BOOT_OR_NATIVE_PROOF")
    return report


def prove_duplicate_account_name_refused(factory, *, schema_review, require_held_proof, target_capability):
    """One expected UNIQUE violation on the restored private copy, rolled back.

    Uses the existing checked factory/lease. No engine, target, role or schema is
    created. Invoke while the same restore lease is held, not on production.
    """
    from uuid import uuid4
    from sqlalchemy.exc import IntegrityError

    validate_c1_review(schema_review)
    if not callable(require_held_proof) or require_held_proof() is not True:
        raise ValueError("actual_private_restore_lease_required_for_duplicate_control")
    require_private_factory(factory, target_capability=target_capability)
    table = TABLES["broker_accounts"]
    session, primary = None, None
    try:
        session = factory()
        outer = session.begin()
        session.execute(text("SET LOCAL statement_timeout = '750ms'"))
        session.execute(text("SET LOCAL lock_timeout = '250ms'"))
        verify_private_session(session, target_capability=target_capability)
        if session.get_bind().dialect.name != "postgresql":
            raise ValueError("actual_PG_duplicate_enforcement_control_required")
        before = [dict(row) for row in session.execute(select(table).order_by(table.c.id).limit(129)).mappings()]
        if not 0 < len(before) <= 128:
            raise ValueError("complete_bounded_restored_account_membership_required")
        duplicate = dict(before[0], id=uuid4(), external_account_id=None)
        refused = False
        try:
            with session.begin_nested():
                session.execute(table.insert(), duplicate)
        except IntegrityError as error:
            code = getattr(error.orig, "sqlstate", None)
            constraint = getattr(getattr(error.orig, "diag", None), "constraint_name", None)
            if code != "23505" or constraint != EXACT_UNIQUENESS["broker_accounts"]["constraint"]:
                raise ValueError("wrong_constraint_or_unproven_duplicate_refusal") from error
            refused = True
        if not refused:
            raise ValueError("duplicate_account_name_was_not_refused")
        outer.rollback()
        if require_held_proof() is not True:
            raise ValueError("duplicate_control_writer_epoch_changed")
        session.execute(text("SET TRANSACTION READ ONLY"))
        session.execute(text("SET LOCAL statement_timeout = '750ms'"))
        session.execute(text("SET LOCAL lock_timeout = '250ms'"))
        after = [dict(row) for row in session.execute(select(table).order_by(table.c.id).limit(129)).mappings()]
        session.rollback()
        if before != after or require_held_proof() is not True:
            raise ValueError("duplicate_control_rollback_or_source_membership_changed")
        return dict(status="PRIVATE_RESTORED_DUPLICATE_ACCOUNT_REFUSED_AND_ROLLED_BACK",
            sqlstate="23505", constraint=EXACT_UNIQUENESS["broker_accounts"]["constraint"],
            account_rows=len(before), before_sha256=sha256(canonical(before)).hexdigest(),
            after_sha256=sha256(canonical(after)).hexdigest(), permission_granted=False)
    except BaseException as error:
        primary = error
        raise
    finally:
        if session is not None:
            for cleanup in (session.rollback, session.close):
                try:
                    cleanup()
                except BaseException as error:
                    if primary is None:
                        primary = error
                    else:
                        primary.add_note("private_cleanup_failure_type=" + type(error).__name__)
            if primary is not None and sys.exc_info()[0] is None:
                raise primary
