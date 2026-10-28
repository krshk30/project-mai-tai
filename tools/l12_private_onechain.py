"""Checked private baseline21 + three reviewed migration bodies, never env.py.

No CLI/engine/Settings/default target/stamp/provider. Main supplies actual nonce,
source guard and lease. Canonical private baseline reconstruction is explicitly
NOT execution of all historical A2 migrations or a full old-A2 service boot.
"""

from hashlib import sha256
from pathlib import Path
import sys
from types import ModuleType

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import text

from tools.l12_main_nonce_target import MainNonceTarget
from tools.l12_nonce_copy_lease import NonceCopyLease
from tools.l12_private_full23_lifecycle import CheckedFactory
from tools.l12_private_evidence_journal import PrivateJournal
from tools.l12_private_schema_evidence import (
    model_ddl_sha256, reflected_catalog, require_model_membership, require_pg_enforcement,
)
from tools.l12_scoped_chunks_v2 import canonical
from tools.l12_scoped_restore_v2 import private_schema

SCHEMA_PINS = {
    "sql/migrations/versions/20261023_atr_simulation_store.py": "185e4ee38f868b84fe031fc77232061e081b02dacf429bd4a0048d26aaaeea0b",
    "src/project_mai_tai/atr_simulation/store_schema_20261023.py": "c6e98ec1865b36f48d810d813dc36c6a335d579860d77549319e418d462bb2bc",
    "sql/migrations/versions/20261023_atr_aggregate_entry.py": "61beb90a5b16a4ffd6a25a66ba1b363a13648efa988d13281777f85dd02cca9a",
    "src/project_mai_tai/atr_simulation/store_schema_20261023_aggregate.py": "7a072363a7337a65c72f753ae9428907024f940c4b34c1bef3313a6f92aef828",
    "src/project_mai_tai/atr_simulation/models.py": "b0e7ce2b2f947ae343a78dfc992eea31658345047bb1561ee59fdfeda0c10c6a",
    "sql/migrations/versions/20261022_l12_dispatch_retirement.py": "f028854fcda1a520c0228683ef71e7fbab0b254fbc1a33b904dc34559c5b4d54",
}
CHAIN = (
    ("sql/migrations/versions/20261023_atr_simulation_store.py", "20261023_atr_simulation", "20261015_bot_control"),
    ("sql/migrations/versions/20261023_atr_aggregate_entry.py", "20261023_atr_aggregate", "20261023_atr_simulation"),
    ("sql/migrations/versions/20261022_l12_dispatch_retirement.py", "20261022_l12_retirement", "20261023_atr_aggregate"),
)
PROJECTIONS = frozenset({"brokerdecides_dispatch_evidence", "brokerdecides_dispatch_retirements"})
NEW_INDEXES = frozenset({"ix_l12_dispatch_scope", "ix_l12_order_dispatch_token", "ix_l12_control_uncertainty"})
_RUNNING = False


def source_bytes(root):
    root = Path(root)
    if not root.is_absolute() or root.is_symlink() or root.resolve() != root:
        raise ValueError("canonical_reviewed_onechain_source_root_required")
    result = {}
    for name, expected in SCHEMA_PINS.items():
        path = root
        for part in name.split("/"):
            path = path / part
            if path.is_symlink():
                raise ValueError("onechain_source_symlink_refused")
        raw = path.read_bytes()
        if sha256(raw).hexdigest() != expected:
            raise ValueError("exact_reviewed_onechain_source_bytes_required")
        result[name] = raw
    return result


def baseline21(metadata_raw, reviewed_metadata_sha256):
    """Clone ONLY21 full source tables, defer two projections and three indexes.

    Four SIM tables are created only by the reviewed first migration body;
    no source absence/empty source data is inferred from their private creation.
    """
    metadata = private_schema(metadata_raw, reviewed_metadata_sha256=reviewed_metadata_sha256)
    removed = set()
    for table in metadata.tables.values():
        for index in tuple(table.indexes):
            if index.name in NEW_INDEXES:
                table.indexes.remove(index)
                removed.add(index.name)
    if removed != NEW_INDEXES or not PROJECTIONS.issubset(metadata.tables):
        raise ValueError("onechain_projection_or_index_partition_changed")
    for name in ("brokerdecides_dispatch_evidence", "brokerdecides_dispatch_retirements"):
        metadata.remove(metadata.tables[name])
    if len(metadata.tables) != 21:
        raise ValueError("exact_21_source_baseline_tables_required")
    return metadata


def migration_modules(root):
    """Executes pinned reviewed module declarations only, NOT production env.py."""
    sources = source_bytes(root)
    result = []
    for relative, revision, parent in CHAIN:
        name = "_l12_private_reviewed_" + revision
        module = ModuleType(name)
        module.__file__ = str(Path(root) / relative)
        # Execute exactly the verified source bytes, never a second unchecked
        # read via import machinery or production Alembic env.py.
        exec(compile(sources[relative], module.__file__, "exec"), module.__dict__)
        if (module.revision != revision or module.down_revision != parent
                or module.depends_on is not None or not callable(module.upgrade)):
            raise ValueError("onechain_revision_parent_or_upgrade_changed")
        result.append(module)
    for name, relative in (
        ("project_mai_tai.atr_simulation.store_schema_20261023", "src/project_mai_tai/atr_simulation/store_schema_20261023.py"),
        ("project_mai_tai.atr_simulation.store_schema_20261023_aggregate", "src/project_mai_tai/atr_simulation/store_schema_20261023_aggregate.py"),
    ):
        loaded = sys.modules.get(name)
        if loaded is None or Path(loaded.__file__).resolve() != Path(root) / relative:
            raise ValueError("onechain_imported_schema_origin_changed")
    return tuple(result)


def construct_onechain(factory, *, root, metadata_raw, metadata_sha256, target,
                       lease, source_guard, append_readback):
    """One fresh private DDL transaction, then journal; never starts a copy/boot.

    Source guard must validate actual approved current root/head/closure. The
    same real lease must remain held through subsequent separately reviewed
    operations. Unknown commit/journal/cleanup never authorizes target reuse.
    """
    global _RUNNING
    if (type(target) is not MainNonceTarget or type(lease) is not NonceCopyLease
            or type(factory) is not CheckedFactory or factory.nonce_lease is not lease
            or factory.target_capability.reviewed_sha256 != target.reviewed_sha256
            or lease.target.reviewed_sha256 != target.reviewed_sha256
            or not callable(source_guard) or type(append_readback) is not PrivateJournal
            or append_readback.root != Path(target.values["root"]) / "ddl-journal"
            or source_guard(target) is not True):
        raise ValueError("actual_onechain_nonce_lease_factory_and_source_guard_required")
    source_bytes(root)
    base = baseline21(metadata_raw, metadata_sha256)
    full = private_schema(metadata_raw, reviewed_metadata_sha256=metadata_sha256)
    modules = migration_modules(root)
    aggregate = sys.modules["project_mai_tai.atr_simulation.store_schema_20261023_aggregate"]
    for table in aggregate.simulation_metadata().tables.values():
        table.to_metadata(full)
    if len(full.tables) != 27:
        raise ValueError("onechain_23_required_plus_four_schema_only_tables_required")
    if _RUNNING:
        raise ValueError("onechain_alembic_proxy_already_owned")
    _RUNNING = True
    report = dict(status="PRIVATE_ONECHAIN_NOT_STARTED", target_capability_sha256=target.reviewed_sha256,
        source_head=target.values["source_head"], source_pins=SCHEMA_PINS,
        metadata_sha256=metadata_sha256, permission_granted=False,
        baseline="CANONICAL_PRIVATE21_RECONSTRUCTION_NOT_HISTORICAL_A2_ALEMBIC_EXECUTION",
        applied_historical_baseline="NOT_CLAIMED", alembic_version_stamp="NOT_WRITTEN",
        source_absence_for_SIM="NOT_EXPORTED_NOT_INFERRED", copy="NOT_RUN", service_boot="NOT_RUN")
    session, transaction, commit_started, committed = None, None, False, False
    primary_error = None
    def record(kind):
        raw = canonical(dict(kind=kind, report=report))
        if append_readback(raw) != raw:
            raise ValueError("onechain_journal_readback_UNKNOWN")
    try:
        lease.begin_ddl()
        record("BEGIN_REVIEWED_PRIVATE_DDL")
        session = factory()
        transaction = session.begin()
        session.execute(text("SET LOCAL statement_timeout = '750ms'"))
        session.execute(text("SET LOCAL lock_timeout = '250ms'"))
        lease.verify_session(session)
        count = session.scalar(text("SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
            "WHERE n.nspname NOT IN ('pg_catalog','information_schema') AND n.nspname NOT LIKE 'pg_toast%' "
            "AND c.relkind IN ('r','p','v','m','S','f')"))
        if count != 0 or type(count) is not int:
            raise ValueError("onechain_requires_actual_zero_user_relations")
        connection = session.connection()
        base.create_all(connection, checkfirst=False)
        completed = []
        with Operations.context(MigrationContext.configure(connection)):
            for module in modules:
                if source_guard(target) is not True:
                    raise ValueError("onechain_source_changed_before_DDL")
                lease._held_file()
                module.upgrade()
                completed.append(dict(revision=module.revision, down_revision=module.down_revision))
        catalog = reflected_catalog(connection, full)
        if session.scalar(text("SHOW session_replication_role")) != "origin":
            raise ValueError("onechain_enforcement_not_origin")
        require_pg_enforcement(catalog["__native_catalog__"])
        require_model_membership(catalog, full)
        report.update(body_sequence=completed, model_ddl_sha256=model_ddl_sha256(full, connection.dialect),
            canonical_catalog_observation=catalog,
            canonical_catalog_observation_sha256=sha256(canonical(catalog)).hexdigest(),
            canonical_catalog_independent_review="PENDING_NOT_SELF_APPROVED")
        lease.verify_session(session)
        if source_guard(target) is not True:
            raise ValueError("onechain_source_changed_before_commit")
        commit_started = True
        transaction.commit()
        committed = True
        report["status"] = "PRIVATE_ONECHAIN_COMMITTED_CATALOG_REVIEW_PENDING_NOT_COPY_OR_BOOT"
    except BaseException as exc:
        primary_error = exc
        report.update(status="PRIVATE_ONECHAIN_COMMIT_UNKNOWN_NO_COPY" if commit_started else
            "PRIVATE_ONECHAIN_FAILED_ROLLBACK_PENDING_NO_COPY", failure_type=type(exc).__name__)
        if transaction is not None and not commit_started:
            try:
                transaction.rollback()
                report["status"] = "PRIVATE_ONECHAIN_FAILED_ROLLED_BACK_NO_COPY"
            except BaseException:
                report["status"] = "PRIVATE_ONECHAIN_ROLLBACK_UNKNOWN_NO_COPY"
        try:
            record("STOP_PRESERVE_PRIVATE_TARGET")
        except BaseException as journal_error:
            exc.add_note("onechain_journal_failure_type=" + type(journal_error).__name__)
        raise
    finally:
        _RUNNING = False
        if session is not None:
            try:
                session.close()
            except BaseException as cleanup:
                report["cleanup_failure_type"] = type(cleanup).__name__
                if primary_error is not None:
                    primary_error.add_note("onechain_cleanup_failure_type=" + type(cleanup).__name__)
    if committed:
        # Persist the final outcome, not an earlier COMMITTED status that could
        # authorize a subsequent copy despite unknown connection cleanup.
        if "cleanup_failure_type" in report:
            report["status"] = "PRIVATE_ONECHAIN_COMMITTED_CLEANUP_UNKNOWN_NO_COPY"
        try:
            record("STOP_COMMITTED_DDL_CLEANUP_UNKNOWN" if "cleanup_failure_type" in report else
                "COMMITTED_PRIVATE_DDL_NOT_INDEPENDENT_SCHEMA_REVIEW")
        except BaseException:
            report["status"] = "PRIVATE_ONECHAIN_COMMITTED_JOURNAL_UNKNOWN_NO_COPY"
            raise
    return report
