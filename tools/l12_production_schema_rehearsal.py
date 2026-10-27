"""Main-only production DDL restore followed by REAL env.py stamp + upgrade.

No PG creation, target adoption, schema correction, row copy or default endpoint.
All effects require the existing typed nonce/checked factory/held lease/journal.
The restore is one transaction; real Alembic follows as distinct journaled phases.
"""

from hashlib import sha256
import json
import os
from pathlib import Path
import re
import selectors
import subprocess
import sys
import time

from tools.l12_schema_rehearsal_inputs import ReviewedSchema, VERSION, START, END, canonical, unique
from tools.l12_real_alembic_child import SourceReview, private_environment

RESTORE_SECONDS, CHILD_SECONDS = 180, 180
CHILD_MAX = 1024 * 1024
NEW_TABLES = {"atr_simulation_inputs", "atr_simulation_consumers", "atr_simulation_runs", "atr_simulation_rows",
    "brokerdecides_dispatch_retirements", "brokerdecides_dispatch_evidence"}
INVENTORY_SQL = """SELECT n.nspname AS schema,c.relname AS name,c.relkind::text AS kind
FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
WHERE n.nspname NOT IN ('pg_catalog','information_schema') AND n.nspname NOT LIKE 'pg_toast%'
AND c.relkind IN ('r','p','v','m','S','f') ORDER BY n.nspname,c.relname LIMIT 4097"""


def real_env_logs(raw):
    """Keep raw logs; only exact normal actual-INI migration INFO is expected.

    No broad stderr suppression. Every named transition must occur once; warnings,
    exceptions, unknown logger/messages or missing logs stop even on process0.
    """
    messages = []
    for line in raw.decode("utf-8", errors="strict").splitlines():
        match = re.fullmatch(r"INFO\s+\[alembic\.runtime\.migration\] (.+)", line)
        if match is None:
            raise ValueError("unexpected_real_env_stderr_STOP_preserve_raw")
        text = match[1]
        if text in {"Context impl PostgresqlImpl.", "Will assume transactional DDL."}:
            messages.append(text)
        elif re.fullmatch(r"Running stamp_revision\s+->\s+20261015_bot_control", text):
            messages.append("STAMP")
        else:
            upgrade = re.fullmatch(r"Running upgrade (20261015_bot_control|20261023_atr_simulation|20261023_atr_aggregate)"
                r" -> (20261023_atr_simulation|20261023_atr_aggregate|20261022_l12_retirement), [^\r\n]+", text)
            if upgrade is None:
                raise ValueError("unexpected_real_env_log_message_STOP")
            messages.append((upgrade[1], upgrade[2]))
    expected = ["Context impl PostgresqlImpl.", "Will assume transactional DDL.", "STAMP",
        "Context impl PostgresqlImpl.", "Will assume transactional DDL.",
        ("20261015_bot_control", "20261023_atr_simulation"),
        ("20261023_atr_simulation", "20261023_atr_aggregate"),
        ("20261023_atr_aggregate", "20261022_l12_retirement")]
    if messages != expected:
        raise ValueError("real_env_exact_stamp_three_upgrade_log_order_required")
    return dict(policy="EXACT_ACTUAL_INI_NORMAL_INFO_ONLY_RAW_RETAINED", lines=len(messages))


def write_new(path, raw):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        view = memoryview(raw)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise OSError("rehearsal_artifact_short_write")
            view = view[written:]
        os.fsync(fd)
    finally:
        os.close(fd)
    directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)
    if path.read_bytes() != raw:
        raise ValueError("rehearsal_artifact_readback_UNKNOWN")
    return dict(path=str(path), sha256=sha256(raw).hexdigest())


def run_child(argv, environment, cwd, out):
    """One bounded local child. No shell, signal, retry or auto target reuse."""
    started, process, selector, failure = time.monotonic(), None, None, None
    collected, closes = {"stdout": bytearray(), "stderr": bytearray()}, []
    report = dict(status="REAL_ENV_CHILD_NOT_STARTED", backend_closure="UNVERIFIED")

    def preserve(error, label):
        nonlocal failure
        note = "real_env_" + label + "_type=" + type(error).__name__
        report.setdefault("cleanup_failure_types", []).append(note)
        if failure is None:
            failure = error
        else:
            failure.add_note(note)

    try:
        process = subprocess.Popen(argv, cwd=cwd, env=environment, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        report["child_pid"] = process.pid
        selector = selectors.DefaultSelector()
        selector.register(process.stdout, selectors.EVENT_READ, "stdout")
        selector.register(process.stderr, selectors.EVENT_READ, "stderr")
        while selector.get_map():
            if time.monotonic() - started > CHILD_SECONDS:
                raise TimeoutError("real_env_child180s_UNKNOWN_no_signal_no_retry")
            for key, _ in selector.select(timeout=0.2):
                raw = os.read(key.fd, 65536)
                if not raw:
                    selector.unregister(key.fileobj)
                elif sum(map(len, collected.values())) + len(raw) > CHILD_MAX:
                    raise ValueError("real_env_child1MiB_bound_STOP")
                else:
                    collected[key.data].extend(raw)
    except BaseException as error:
        failure = error
    finally:
        if selector is not None:
            closes.append(("selector_close", selector))
        if process is not None:
            closes.extend((("stdout_close", process.stdout), ("stderr_close", process.stderr)))
        for label, resource in closes:
            try:
                resource.close()
            except BaseException as error:
                preserve(error, label)
        if process is not None:
            try:
                report["exit_code"] = process.wait(timeout=10)
            except BaseException as error:
                preserve(error, "terminal_wait")
        for name, raw in collected.items():
            try:
                report[name] = write_new(out / name, bytes(raw))
            except BaseException as error:
                preserve(error, name + "_persist")
        report["status"] = "REAL_ENV_CHILD_FAILED_OR_UNKNOWN_NO_RETRY" if failure else "REAL_ENV_CHILD_TERMINATED_NOT_APPROVAL"
        if failure is not None:
            report["error_type"] = type(failure).__name__
        try:
            write_new(out / "receipt.json", canonical(report))
        except BaseException as error:
            preserve(error, "receipt_persist")
    if failure is not None:
        raise failure
    if report.get("exit_code") != 0:
        raise ValueError("real_env_child_nonzero_STOP_preserve_outputs")
    report["stderr_log_classification"] = real_env_logs(bytes(collected["stderr"]))
    lines = [json.loads(line, object_pairs_hook=unique) for line in collected["stdout"].splitlines()]
    if ([row.get("kind") for row in lines] != ["REAL_ENV_STAMP_RETURNED_NOT_FINAL_OUTCOME", "REAL_ENV_UPGRADE_RETURNED"]
            or lines[0].get("revision") != START or lines[1].get("revision") != END
            or lines[1].get("connections") != 2 or lines[1].get("permission_granted") is not False):
        raise ValueError("exact_real_env_child_terminal_witnesses_required")
    report["validated_output"] = write_new(out / "validated-output.json", canonical(dict(
        process_receipt=report, terminal=lines[-1], permission_granted=False, canonical_approval=False)))
    return report, lines[-1]


def rehearse(factory, *, target, lease, journal, schema, source, child_inputs, interpreter, source_guard):
    """One exact Main-owned action, no default construction/runtime authority.

    child_inputs contains actual reviewed artifact descriptors already saved by
    Main. Parent independently verifies them, writes its exact child plan, then
    the child reads/verifies them again. Copy/boot are never invoked here.
    """
    from sqlalchemy import text
    from tools.l12_main_nonce_target import MainNonceTarget
    from tools.l12_nonce_copy_lease import NonceCopyLease
    from tools.l12_private_full23_lifecycle import CheckedFactory
    from tools.l12_private_evidence_journal import PrivateJournal
    from tools.l12_real_alembic_child import read_artifact
    if (type(target) is not MainNonceTarget or type(lease) is not NonceCopyLease or type(factory) is not CheckedFactory
            or type(journal) is not PrivateJournal or type(schema) is not ReviewedSchema or type(source) is not SourceReview
            or factory.nonce_lease is not lease or factory.target_capability.reviewed_sha256 != target.reviewed_sha256
            or factory.authority.execution_head != target.values["source_head"]
            or factory.authority.lease_receipt_sha256 != lease.receipt_sha256
            or factory.authority.schema_construction_receipt_sha256 != target.values["creation_journal_tail_sha256"]
            or lease.target.reviewed_sha256 != target.reviewed_sha256 or lease.context["operation"] != "construct"
            or journal.root != Path(target.values["root"]) / "schema-rehearsal-journal"
            or not callable(source_guard) or source_guard(target) is not True
            or target.values["source_head"] != source.values["source_head"]
            or schema.review["authority_sha256"] != source.values["authority_sha256"]
            or lease.context["schema_review_sha256"] != schema.review_sha256
            or lease.context["manifest_sha256"] != schema.manifest_binding_sha256
            or lease.context["metadata_sha256"] != sha256(schema.raw).hexdigest()):
        raise ValueError("exact_actual_nonce_lease_reviewed_whole_schema_source_and_journal_required")
    source.verify()
    source.verify_loaded_app()
    if (not isinstance(child_inputs, dict) or set(child_inputs) != {"capability", "creation_review_sha256", "source_review"}
            or read_artifact(child_inputs["source_review"]) != source.raw
            or read_artifact(child_inputs["capability"], limit=16384) != target.raw
            or child_inputs["creation_review_sha256"] != target.expected_creation_review_sha256):
        raise ValueError("child_artifact_descriptors_must_match_actual_parent_objects")
    if (not isinstance(interpreter, dict) or set(interpreter) != {"path", "sha256"}
            or not Path(interpreter["path"]).is_absolute()
            or sha256(Path(interpreter["path"]).read_bytes()).hexdigest() != interpreter["sha256"]):
        raise ValueError("actual_Main_reviewed_private_Python_binary_required")
    root = Path(target.values["root"])
    work = root / "rehearsal-work"
    # Fresh directory is another refusal marker, never an adoption/retry path.
    work.mkdir(mode=0o700)
    report = dict(version=VERSION, status="PRODUCTION_SCHEMA_REHEARSAL_NOT_STARTED", permission_granted=False,
        target_capability_sha256=target.reviewed_sha256, source_review_sha256=source.sha256,
        empty_creation_journal_tail_sha256=target.values["creation_journal_tail_sha256"],
        schema_sha256=sha256(schema.raw).hexdigest(), schema_review_sha256=schema.review_sha256,
        schema_provenance=schema.provenance,
        phase="RESTORE", actual_runtime="MAIN_ONLY", copy="NOT_RUN", boot="NOT_RUN", canonical_approval=False)
    session, transaction, failure, commit_started, committed = None, None, None, False, False

    def record(kind):
        raw = canonical(dict(kind=kind, report=report))
        if journal(raw) != raw:
            raise ValueError("real_schema_rehearsal_journal_readback_UNKNOWN")

    try:
        lease.begin_ddl()
        record("BEGIN_ONE_PRODUCTION_SCHEMA_REHEARSAL")
        session = factory()
        transaction = session.begin()
        lease.verify_session(session)
        connection = session.connection()  # Actual corrected PinnedSession API.
        before = [dict(row) for row in connection.exec_driver_sql(
            INVENTORY_SQL, execution_options={"no_parameters": True}).mappings()]
        if before:
            raise ValueError("fresh_nonce_zero_user_relations_required_no_adoption")
        started, executed, preserved = time.monotonic(), 0, 0
        for ordinal, piece in enumerate(schema.pieces):
            if time.monotonic() - started > RESTORE_SECONDS:
                raise TimeoutError("private_schema_restore180s_STOP_no_retry")
            lease._held_file()
            if piece.kind in {"TRIVIA", "PSQL_RESTRICT", "PSQL_UNRESTRICT"}:
                preserved += 1
                continue
            # Execute original SQL bytes, including pg_dump SET controls. Reset
            # strict local budgets before EVERY next statement, not just startup.
            for setting in ("statement_timeout='750ms'", "lock_timeout='250ms'", "idle_in_transaction_session_timeout='15s'"):
                connection.exec_driver_sql("SET LOCAL " + setting)
            connection.exec_driver_sql(piece.raw.decode("utf-8"), execution_options={"no_parameters": True})
            executed += 1
            if executed % 32 == 0:
                report.update(last_piece=ordinal, executed_statements=executed)
                record("RESTORE_PROGRESS_UNCOMMITTED")
        for setting in ("statement_timeout='750ms'", "lock_timeout='250ms'", "idle_in_transaction_session_timeout='15s'"):
            connection.exec_driver_sql("SET LOCAL " + setting)
        actual = [dict(row) for row in connection.exec_driver_sql(
            INVENTORY_SQL, execution_options={"no_parameters": True}).mappings()]
        expected = schema.review["relations"]
        if len(actual) > 4096 or sorted(actual, key=canonical) != sorted(expected, key=canonical):
            raise ValueError("whole_production_relation_membership_changed_STOP")
        if any(row["schema"] == "public" and row["name"] in NEW_TABLES for row in actual):
            raise ValueError("new_migrations_already_present_in_production_baseline_STOP")
        if list(connection.exec_driver_sql("SELECT version_num FROM public.alembic_version LIMIT 2")) != []:
            raise ValueError("schema_only_restore_must_have_empty_Alembic_version_table")
        lease.verify_session(session)
        if source.verify() is not True or source_guard(target) is not True:
            raise ValueError("reviewed_source_changed_before_restore_commit")
        if time.monotonic() - started > RESTORE_SECONDS:
            raise TimeoutError("private_schema_restore180s_before_commit_STOP")
        report.update(executed_statements=executed, preserved_client_or_trivia_pieces=preserved,
            restored_relations=actual, status="PRODUCTION_SCHEMA_RESTORE_COMMIT_PENDING")
        commit_started = True
        transaction.commit()
        committed = True
        report["status"] = "PRODUCTION_SCHEMA_RESTORE_COMMITTED_UPGRADE_NOT_RUN"
    except BaseException as error:
        failure = error
        report.update(status="PRODUCTION_SCHEMA_RESTORE_COMMIT_UNKNOWN_STOP" if commit_started else
            "PRODUCTION_SCHEMA_RESTORE_FAILED_ROLLBACK_PENDING", failure_type=type(error).__name__)
        if transaction is not None and not commit_started:
            try:
                transaction.rollback()
                report["status"] = "PRODUCTION_SCHEMA_RESTORE_FAILED_ROLLED_BACK_STOP"
            except BaseException as cleanup:
                report["status"] = "PRODUCTION_SCHEMA_RESTORE_ROLLBACK_UNKNOWN_STOP"
                error.add_note("production_schema_rollback_type=" + type(cleanup).__name__)
    finally:
        if session is not None:
            try:
                session.close()
            except BaseException as cleanup:
                report["cleanup_failure_type"] = type(cleanup).__name__
                if failure is None:
                    failure = cleanup
                else:
                    failure.add_note("production_schema_session_cleanup_type=" + type(cleanup).__name__)
                if committed:
                    report["status"] = "PRODUCTION_SCHEMA_RESTORE_COMMITTED_CLEANUP_UNKNOWN_STOP"
        try:
            record("RESTORE_OUTCOME_STOP" if failure else "RESTORE_COMMITTED_NOT_CANONICAL_APPROVAL")
        except BaseException as cleanup:
            if failure is None:
                failure = cleanup
            else:
                failure.add_note("production_schema_journal_type=" + type(cleanup).__name__)
    if failure is not None:
        raise failure

    # Parent has closed its physical connection before REAL env opens its own.
    try:
        lease._held_file()
        source.verify()
        plan = write_new(work / "real-env-plan.json", canonical(child_inputs))
        output = work / "real-env-output"
        output.mkdir(mode=0o700)
        report.update(phase="REAL_ENV_STAMP_UPGRADE", status="REAL_ENV_CHILD_ATTEMPT_PENDING")
        record("BEGIN_REAL_ENV_STAMP_AND_UPGRADE")
        argv = [interpreter["path"], "-I", "-B", str(Path(source.values["source_root"]) / "tools/l12_real_alembic_child.py"),
            "--plan", plan["path"], "--plan-sha256", plan["sha256"]]
        receipt, terminal = run_child(argv, private_environment(target), work, output)
        if (terminal["target_capability_sha256"] != target.reviewed_sha256
                or terminal["source_review_sha256"] != source.sha256):
            raise ValueError("child_target_or_source_terminal_binding_mismatch")
        # New checked connection proves child backends are gone and revision
        # actually committed; a returned Python command alone is insufficient.
        with factory() as check:
            check.execute(text("SET TRANSACTION READ ONLY"))
            lease.verify_session(check)
            revisions = list(check.execute(text("SELECT version_num FROM public.alembic_version LIMIT 2")))
            if revisions != [(END,)]:
                raise ValueError("actual_real_upgrade_final_revision_mismatch")
            after = [dict(row) for row in check.execute(text(INVENTORY_SQL)).mappings()]
            required = schema.review["relations"] + [dict(schema="public", name=name, kind="r") for name in NEW_TABLES]
            if len(after) > 4096 or sorted(after, key=canonical) != sorted(required, key=canonical):
                raise ValueError("real_upgrade_unlisted_relation_delta_STOP")
            report["post_upgrade_relations"] = after
            check.rollback()
        source.verify()
        lease._held_file()
        report.update(status="REAL_PRODUCTION_SCHEMA_ALEMBIC_REHEARSAL_COMMITTED_CATALOG_COMPARE_PENDING",
            actual_final_revision=END, child_receipt=receipt, child_terminal=terminal)
        record("REAL_REHEARSAL_RETURNED_NOT_CANONICAL_APPROVAL_OR_COPY")
        return report
    except BaseException as error:
        report.update(status="REAL_ENV_PHASE_FAILED_OR_COMMIT_CLEANUP_UNKNOWN_STOP", failure_type=type(error).__name__)
        try:
            record("STOP_PRESERVE_PARTIAL_FRESH_TARGET_NO_RETRY")
        except BaseException as cleanup:
            error.add_note("real_env_STOP_journal_type=" + type(cleanup).__name__)
        raise
