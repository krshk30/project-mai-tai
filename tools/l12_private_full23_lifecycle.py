"""Main-owned FULL23 copy -> canonical projection -> indexed coverage runner.

No CLI, engine construction, DDL, Alembic env, provider or service start. The
caller must separately construct and review the private schema, source guard,
stopped-writer lease and durable journal. A successful result is NOT boot/native
permission. Historical V9/native role checks are not bypassed by this runner.
"""

from dataclasses import asdict, dataclass
from hashlib import sha256
from math import ceil
from pathlib import Path
import base64

from sqlalchemy import select, text

from tools.l12_close_asof_contract import is_head, is_sha

COPY_OK = "V2_PRIVATE_STREAM_COPY_READBACK_COMMITTED_NOT_BOOT_OR_NATIVE_PROOF"
IDENTITY_SQL = text("SELECT (pg_control_system()).system_identifier::text, "
    "pg_postmaster_start_time()::text, current_user, current_setting('server_version_num')")


def require_private_factory(factory, *, target_capability=None):
    if target_capability is not None:
        from tools.l12_main_nonce_target import MainNonceTarget
        if type(target_capability) is not MainNonceTarget:
            raise ValueError("reviewed_Main_nonce_target_capability_required")
        return target_capability.require_factory(factory)
    from tools.l12_service_boot_replay import require_private_factory as legacy
    return legacy(factory)


def verify_private_session(session, *, target_capability=None):
    if target_capability is not None:
        from tools.l12_main_nonce_target import MainNonceTarget
        if type(target_capability) is not MainNonceTarget:
            raise ValueError("reviewed_Main_nonce_target_capability_required")
        return target_capability.verify_session(session)
    from tools.l12_service_boot_replay import verify_private_session as legacy
    return legacy(session)


@dataclass(frozen=True)
class PrivateAuthority:
    execution_head: str
    execution_review_sha256: str
    source_guard_receipt_sha256: str
    schema_construction_receipt_sha256: str
    lease_receipt_sha256: str
    system_identifier: str
    postmaster_started: str
    database_user: str
    server_version_num: str
    configured_accounts: tuple[str, str]
    expanded_accounts: tuple[str, ...]

    def __post_init__(self):
        if (not is_head(self.execution_head)
                or any(not is_sha(value) or value == "0" * 64 for value in (
                    self.execution_review_sha256, self.source_guard_receipt_sha256,
                    self.schema_construction_receipt_sha256, self.lease_receipt_sha256))
                or not self.system_identifier.isdecimal()
                or not self.postmaster_started or not self.database_user
                or not self.server_version_num.isdecimal()
                or not 160000 <= int(self.server_version_num) < 170000
                or type(self.configured_accounts) is not tuple
                or len(self.configured_accounts) != 2 or len(set(self.configured_accounts)) != 2
                or type(self.expanded_accounts) is not tuple
                or not 2 <= len(self.expanded_accounts) <= 64
                or tuple(sorted(set(self.expanded_accounts))) != self.expanded_accounts
                or not set(self.configured_accounts).issubset(self.expanded_accounts)
                or any(not isinstance(name, str) or not name.startswith("live:")
                       for name in self.expanded_accounts)):
            raise ValueError("private_FULL23_explicit_authority_required")


class CheckedFactory:
    """Every session verifies actual private endpoint AND pinned server epoch.

    Identity probes are READ ONLY and explicitly rolled back before returning
    the fresh session required by the canonical retirement transaction boundary.
    """

    def __init__(self, factory, authority, held, *, target_capability=None, nonce_lease=None):
        if target_capability is None:
            require_private_factory(factory)
        else:
            require_private_factory(factory, target_capability=target_capability)
            from tools.l12_nonce_copy_lease import NonceCopyLease
            if (type(nonce_lease) is not NonceCopyLease
                    or nonce_lease.target.reviewed_sha256 != target_capability.reviewed_sha256):
                raise ValueError("nonce_checked_factory_requires_actual_exact_lease")
        if factory.kw["bind"].dialect.name != "postgresql" or factory.kw["bind"].echo:
            raise ValueError("private_PG16_no_payload_logging_required")
        self.factory, self.authority, self.held = factory, authority, held
        self.target_capability = target_capability
        self.nonce_lease = nonce_lease
        self.kw = factory.kw

    def __call__(self):
        self.held()
        connection = self.kw["bind"].connect()
        session = None
        try:
            session = self.factory(bind=connection)
            if session.in_transaction():
                raise ValueError("private_fresh_session_required")
            session.execute(text("SET TRANSACTION READ ONLY"))
            session.execute(text("SET LOCAL statement_timeout = '750ms'"))
            session.execute(text("SET LOCAL lock_timeout = '250ms'"))
            if self.target_capability is None:
                verify_private_session(session)
            else:
                verify_private_session(session, target_capability=self.target_capability)
                self.nonce_lease.verify_session(session)
            actual = tuple(session.execute(IDENTITY_SQL).one())
            expected = (self.authority.system_identifier, self.authority.postmaster_started,
                        self.authority.database_user, self.authority.server_version_num)
            if actual != expected:
                raise ValueError("private_cluster_epoch_or_user_changed")
            session.rollback()
            self.held()
            return PinnedSession(session, connection)
        except BaseException:
            # Preserve the original target/proof failure, including cancellation.
            try:
                if session is not None:
                    session.close()
            except BaseException:
                pass
            try:
                connection.close()
            except BaseException:
                pass
            raise


class PinnedSession:
    """Keep the checked Connection across rollback and subsequent transactions.

    Session.rollback() must not return this connection to the pool between
    identity verification and the canonical writer's fresh transaction.
    """

    def __init__(self, session, connection):
        # Do not shadow Session.connection(...), used by DDL, reflection and
        # retirement's execution_options. Keep ownership under a private name.
        self.session, self._pinned_connection = session, connection

    def __getattr__(self, name):
        return getattr(self.session, name)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        try:
            self.close()
        except BaseException as cleanup:
            if exc is None:
                raise
            exc.add_note("private_cleanup_failure_type=" + type(cleanup).__name__)

    def close(self):
        failure = None
        try:
            self.session.close()
        except BaseException as exc:
            failure = exc
        try:
            self._pinned_connection.close()
        except BaseException:
            if failure is None:
                raise
        if failure is not None:
            raise failure


class Journal:
    """External append+readback must return the exact canonical record bytes.

    Readback equivalence is not filesystem durability proof: that independent
    responsibility remains part of the reviewed journal implementation/lease.
    No success/cursor is returned after ambiguous append or different readback.
    """

    def __init__(self, binding, append_readback):
        self.binding, self.append_readback = binding, append_readback
        self.previous, self.ordinal = None, 0

    def record(self, kind, value):
        from tools.l12_scoped_chunks_v2 import canonical
        raw = canonical(dict(binding=self.binding, ordinal=self.ordinal,
                             previous=self.previous, kind=kind, value=value))
        observed = self.append_readback(raw)
        if type(observed) is not bytes or observed != raw:
            raise ValueError("private_journal_write_readback_UNKNOWN_no_boot")
        self.previous, self.ordinal = sha256(raw).hexdigest(), self.ordinal + 1


def journal_native_closure(journal, closure, raw_graph, reflected):
    """Keep full evidence in existing bounded, hash-chained fsync records."""
    streams = []
    for stream, raw in (("raw_graph", raw_graph), ("reflected_catalog", reflected)):
        if type(raw) is not bytes or not 0 < len(raw) <= 32 * 1024**2:
            raise ValueError("bounded_native_projection_evidence_required")
        digest = sha256(raw).hexdigest()
        parts = 0
        for offset in range(0, len(raw), 512 * 1024):
            journal.record("NATIVE_RO_EVIDENCE_PART", dict(stream=stream,
                offset=offset, bytes=len(raw), whole_sha256=digest,
                data_base64=base64.b64encode(raw[offset:offset + 512 * 1024]).decode("ascii")))
            parts += 1
        streams.append(dict(stream=stream, bytes=len(raw), whole_sha256=digest, parts=parts))
    journal.record("NATIVE_RO_TRANSACTION_AND_SESSION_CLOSED",
        dict(closure, streams=streams, evidence_tail_sha256=journal.previous))
    return journal.previous


def run_full23(factory, manifest_raw, metadata_raw, reader, *, review, schema_review,
               authority, source_guard, require_lease, append_readback, observe_ms,
               target_capability=None, execution_binding=None, native_reference=None):
    """One invocation; no automatic retry, resume, schema creation or boot call.

    Partial/unknown commit or journal failures preserve target/journal for Main.
    Re-entry requires a separate explicit recovery review, not this fresh-copy
    path. Source rows and immutable receipts are never deleted or reparented.
    """
    from project_mai_tai.brokerdecides1_accounts import local_accounts
    from project_mai_tai.brokerdecides1_install_chunks import finalize_install_before_start, resume_install
    from project_mai_tai.brokerdecides1_retirement import BATCH, scope
    from project_mai_tai.db.models import DashboardSnapshot
    from tools.l12_scoped_chunks_v2 import canonical, validate_v2
    from tools.l12_scoped_restore_v2 import restore_v2, prove_duplicate_account_name_refused
    from tools.l12_private_schema_evidence import (
        C1_BASELINE, C1_GRAPH_VERSION, is_c1_review, validate_c1_review, prepare_native_projection,
    )
    if type(authority) is not PrivateAuthority or not all(callable(callback) for callback in
            (source_guard, require_lease, append_readback, observe_ms)):
        raise ValueError("private_explicit_callbacks_required")
    # The bridge must already have passed. Its failure/STOP is not an input.
    plan = validate_v2(manifest_raw, metadata_raw, reader, **review)
    binding_values = dict(authority=asdict(authority),
        manifest=plan["manifest_sha256"], metadata=plan["metadata_sha256"],
        schema=plan["schema_sha256"], schema_review=schema_review,
        review=dict(review, limits=vars(review["limits"])))
    native_purpose = validate_c1_review(schema_review) if is_c1_review(schema_review) else None
    if native_purpose is not None and schema_review["version"] != C1_GRAPH_VERSION:
        raise ValueError("C1_row_DML_requires_graph_bound_V2_not_old_reflected_approval")
    if native_purpose is not None and (target_capability is None or execution_binding is None):
        raise ValueError("C1_native_copy_requires_nonce_and_explicit_consumer_binding")
    if execution_binding is not None and native_purpose is None:
        raise ValueError("consumer_carry_only_available_in_explicit_C1_native_profile")
    if target_capability is not None:
        from tools.l12_main_nonce_target import MainNonceTarget
        if type(target_capability) is not MainNonceTarget:
            raise ValueError("reviewed_Main_nonce_target_capability_required")
        value = target_capability.values
        execution_head = value["source_head"]
        if execution_binding is not None:
            from tools.l12_main_nonce_target import validate_execution_binding
            execution_head = validate_execution_binding(execution_binding, target_capability)
            binding_values["execution_binding"] = execution_binding
        if (authority.execution_head != execution_head
                or authority.system_identifier != value["system_identifier"]
                or authority.postmaster_started != value["postmaster_started"]
                or authority.database_user != value["database_user"]
                or authority.server_version_num != value["server_version_num"]):
            raise ValueError("lifecycle_authority_and_nonce_target_disagree")
        binding_values["target_capability_sha256"] = target_capability.reviewed_sha256
    binding = sha256(canonical(binding_values)).hexdigest()
    if target_capability is not None:
        from tools.l12_nonce_copy_lease import NonceCopyLease
        from tools.l12_private_evidence_journal import PrivateJournal
        if (type(require_lease) is not NonceCopyLease
                or require_lease.target.reviewed_sha256 != target_capability.reviewed_sha256):
            raise ValueError("nonce_lifecycle_requires_actual_exact_lease_not_callback_stub")
        if (type(append_readback) is not PrivateJournal
                or append_readback.root != Path(target_capability.values["root"]) / "copy-journal"):
            raise ValueError("nonce_copy_requires_actual_target_bound_fsync_journal")
        require_lease.bind_inputs(plan=plan, schema_review=schema_review,
            authority=authority, binding=binding)

    def held():
        if source_guard(authority) is not True or require_lease(binding, authority) is not True:
            raise ValueError("private_source_or_writer_epoch_UNKNOWN")
        return True

    def clock():
        value = observe_ms()
        if type(value) is not int or value <= 0:
            raise ValueError("private_observation_UNKNOWN")
        return value

    held()
    checked = (CheckedFactory(factory, authority, held) if target_capability is None else
        CheckedFactory(factory, authority, held, target_capability=target_capability,
            nonce_lease=require_lease))
    journal = Journal(binding, append_readback)
    journal.record("BEGIN", dict(authority=asdict(authority), permission_granted=False))
    report = dict(status="FULL23_COPY_NOT_STARTED", permission_granted=False,
        binding=binding, execution_head=authority.execution_head,
        manifest_sha256=plan["manifest_sha256"], metadata_sha256=plan["metadata_sha256"],
        service_boot="NOT_RUN", old_A2_three_service_60s="NOT_RUN",
        native_permission="UNKNOWN", source_tables=plan["tables"])
    stage = "COPY"
    try:
        native_projection = None
        if native_purpose is not None:
            stage = "NATIVE_GRAPH_RO_PREFLIGHT"

            native_projection = prepare_native_projection(checked, schema_review, native_reference,
                held=held, append_closure=lambda closure, raw, reflected:
                    journal_native_closure(journal, closure, raw, reflected))
            report["native_graph_projection"] = native_projection.summary()
            journal.record("ACCEPTED_WHOLE_NATIVE_GRAPH_MATCH", native_projection.summary())
            held()
        stage = "COPY"
        copy = restore_v2(manifest_raw, metadata_raw, reader, checked, review=review,
            schema_review=schema_review, require_held_proof=lambda _copy_binding: held(),
            journal_committed=lambda copy_binding, value: journal.record(
                "COPY_COMMITTED", dict(copy_binding=copy_binding, report=value)),
            target_capability=target_capability,
            **({"native_projection": native_projection} if native_projection is not None else {}))
        report["copy"] = copy
        if copy["status"] != COPY_OK or copy["committed"] is not True:
            report["status"] = "FULL23_COPY_UNQUALIFIED_NO_CONSTRUCTION_OR_BOOT"
            journal.record("STOP", report)
            return report
        if native_purpose is not None:
            stage = "DUPLICATE_UNIQUE_ROLLBACK_CONTROL"
            proof = prove_duplicate_account_name_refused(checked, schema_review=schema_review,
                require_held_proof=held, target_capability=target_capability)
            report["duplicate_account_name_control"] = proof
            journal.record("DUPLICATE_UNIQUE_REFUSED_AND_ROLLED_BACK", proof)
            if native_purpose == C1_BASELINE:
                held()
                report["status"] = "C1_BASELINE_SOURCE21_COPY_NOT_CONSTRUCTION_OR_BOOT"
                report["projection_construction"] = "NOT_APPLICABLE_BASELINE_WITHOUT_L12"
                journal.record("BASELINE_SOURCE21_COMPLETE_NOT_BOOT", report)
                report["journal_tail_sha256"] = journal.previous
                return report
        stage = "SCOPE"
        with checked() as session:
            session.execute(text("SET TRANSACTION READ ONLY"))
            session.execute(text("SET LOCAL statement_timeout = '750ms'"))
            session.execute(text("SET LOCAL lock_timeout = '250ms'"))
            names = tuple(sorted(account.name for account in local_accounts(
                session, authority.configured_accounts)))
            if names != authority.expanded_accounts:
                raise ValueError("private_expanded_alias_scope_changed")
            upper = session.scalar(select(DashboardSnapshot.id).where(scope(names))
                .order_by(DashboardSnapshot.id.desc()).limit(1))
            held()
            session.rollback()
        journal.record("SCOPE", dict(expanded_accounts=names, upper_id=upper))
        unknown = []
        stage = "CONSTRUCTION"
        if upper is None:
            # Actual complete private-scope query result, NOT local broker-flat proof.
            report["indexed_coverage"] = dict(scope_empty=True, retireable_remaining=0,
                                               unknown=[], verified_count=0)
        else:
            # Source full-membership row count is an upper bound, not a guessed
            # eligible count. Canonical <=32-row chunks and each 750/250 guard stay.
            source_rows = plan["tables"]["dashboard_snapshots"]["exact_export_rows"]
            budget = ceil(source_rows / BATCH) + 1
            chunks = ()
            while budget > 0 and (not chunks or not chunks[-1].exhausted):
                allocation = min(500, budget)
                previous = len(chunks)
                chunks = resume_install(checked, authority.configured_accounts, upper_id=upper,
                    committed_chunks=chunks, chunk_budget=allocation, observe_ms=clock,
                    require_stopped_writers=held,
                    journal_committed=lambda chunk: journal.record("CHUNK_COMMITTED", asdict(chunk)))
                used = len(chunks) - previous
                if not 0 < used <= allocation:
                    raise ValueError("private_chunk_progress_UNKNOWN")
                budget -= used
            if not chunks[-1].exhausted:
                raise ValueError("private_full_membership_budget_exhausted_no_boot")
            stage = "INDEXED_COVERAGE"
            coverage = finalize_install_before_start(checked, authority.configured_accounts,
                chunks, observed_ms=clock(), require_stopped_writers=held,
                report_unknown=lambda fence: unknown.append(asdict(fence)))
            report["indexed_coverage"] = asdict(coverage)
            if report["indexed_coverage"]["unknown"] != tuple(unknown):
                raise ValueError("private_UNKNOWN_accounting_changed")
        held()
        report["status"] = "FULL23_READBACK_AND_INDEXED_COVERAGE_NOT_SERVICE_BOOT_OR_NATIVE_PROOF"
        journal.record("COVERAGE_COMPLETE_NOT_BOOT", report)
        report["journal_tail_sha256"] = journal.previous
        return report
    except BaseException as exc:
        # Never convert an unknown write into automatic replay/target deletion.
        report.update(status="FULL23_STOP_PRESERVE_TARGET_NO_BOOT", stage=stage,
            failure_type=type(exc).__name__,
            sqlstate=getattr(getattr(exc, "orig", None), "sqlstate", None))
        try:
            journal.record("STOP_UNKNOWN", report)
        except BaseException as journal_exc:
            report["journal_failure_type"] = type(journal_exc).__name__
        raise
