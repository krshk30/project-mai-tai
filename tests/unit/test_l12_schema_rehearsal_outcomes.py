"""State-machine controls: real PinnedSession/PrivateJournal, NO PG/child.

Native identity, source inventory, lease census and SQL results are explicit
controlled seams. These tests do NOT qualify any live/fresh target or actual DDL.
"""

from hashlib import sha256
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

from tests.unit.test_l12_main_nonce_target import capability, owned_files
from tests.unit.test_l12_schema_rehearsal_inputs import SQL, review
from tools import l12_production_schema_rehearsal as parent
from tools.l12_real_alembic_child import SourceReview
from tools.l12_schema_rehearsal_inputs import ReviewedSchema, canonical, END
from tools.l12_private_full23_lifecycle import CheckedFactory, PinnedSession
from tools.l12_nonce_copy_lease import NonceCopyLease
from tools.l12_private_evidence_journal import PrivateJournal


def state(monkeypatch, capability, root, fault):
    events, made = [], []
    schema_raw = canonical(review())
    schema = ReviewedSchema(SQL, schema_raw, sha256(schema_raw).hexdigest())
    source_root = root / "CONTROLLED_SOURCE"
    source_root.mkdir(mode=0o700)
    source_raw = canonical(dict(purpose="MAIN_REVIEWED_REAL_ENV_WHOLE_PRODUCTION_SCHEMA_REHEARSAL",
        source_root=str(source_root), source_head=capability.values["source_head"],
        source_pins={f"CONTROLLED_{index}": "c" * 64 for index in range(10)}, authority_sha256="b" * 64,
        review_id="CONTROLLED_NOT_SOURCE_APPROVAL"))
    source = SourceReview(source_raw, sha256(source_raw).hexdigest())
    monkeypatch.setattr(SourceReview, "verify", lambda self: True)
    monkeypatch.setattr(SourceReview, "verify_loaded_app", lambda self: True)
    journal_dir = root / "schema-rehearsal-journal"
    journal_dir.mkdir(mode=0o700)
    journal = PrivateJournal(journal_dir)
    lease = object.__new__(NonceCopyLease)
    lease.target = capability
    lease.receipt_sha256 = "d" * 64
    lease.context = dict(operation="construct", schema_review_sha256=schema.review_sha256,
        manifest_sha256=schema.review["dump_frames_sha256"], metadata_sha256=sha256(SQL).hexdigest())
    monkeypatch.setattr(NonceCopyLease, "_held_file", lambda self: True)
    monkeypatch.setattr(NonceCopyLease, "verify_session", lambda self, session: events.append("CONTROLLED_NATIVE_CENSUS_NOT_RUN"))
    monkeypatch.setattr(NonceCopyLease, "begin_ddl", lambda self: events.append("CONSUMED_ONE_ATTEMPT"))
    primary = OSError("CONTROLLED_PRIMARY_FAILURE")

    class Rows:
        def __init__(self, rows):
            self.rows = rows

        def mappings(self):
            return self

        def __iter__(self):
            return iter(self.rows)

    class Connection:
        closed = False

        def __init__(self):
            self.inventories = 0
            self.calls = []

        def exec_driver_sql(self, sql, **kwargs):
            self.calls.append((sql, kwargs))
            if sql == parent.INVENTORY_SQL:
                self.inventories += 1
                return Rows([] if self.inventories == 1 else schema.review["relations"])
            if "version_num" in sql:
                return Rows([])
            if "CREATE TABLE" in sql and fault == "DDL":
                raise primary
            return Rows([])

        def close(self):
            self.closed = True
            events.append("CONNECTION_CLOSED")

    class Session:
        closed = False

        def __init__(self, ordinal):
            self.ordinal, self.actual_connection = ordinal, Connection()

        def connection(self):
            return self.actual_connection

        def begin(self):
            return self

        def commit(self):
            events.append("COMMIT_ATTEMPT")
            if fault == "commit":
                raise primary

        def rollback(self):
            events.append("ROLLBACK")

        def close(self):
            self.closed = True
            if (fault == "restore_close" and self.ordinal == 0) or (fault == "post_close" and self.ordinal == 1):
                raise primary

        def execute(self, sql):
            if "version_num" in str(sql):
                return Rows([(END,)])
            if str(sql) == parent.INVENTORY_SQL:
                return Rows(schema.review["relations"] + [dict(schema="public", name=name, kind="r") for name in parent.NEW_TABLES])
            return Rows([])

    factory = object.__new__(CheckedFactory)
    factory.nonce_lease, factory.target_capability = lease, capability
    factory.authority = SimpleNamespace(execution_head=capability.values["source_head"],
        lease_receipt_sha256=lease.receipt_sha256,
        schema_construction_receipt_sha256=capability.values["creation_journal_tail_sha256"])

    def checked(self):
        session = Session(len(made))
        made.append(session)
        return PinnedSession(session, session.actual_connection)

    monkeypatch.setattr(CheckedFactory, "__call__", checked)

    def child(argv, environment, work, out):
        assert made[0].closed and made[0].actual_connection.closed
        events.append("CHILD_STARTED_AFTER_PARENT_CONNECTION_CLOSED")
        if fault == "child":
            raise primary
        return dict(exit_code=0), dict(target_capability_sha256=capability.reviewed_sha256,
            source_review_sha256=source.sha256)

    monkeypatch.setattr(parent, "run_child", child)
    cap_path = parent.write_new(root / "controlled-capability.json", capability.raw)
    source_path = parent.write_new(root / "controlled-source-review.json", source.raw)
    interpreter = dict(path=sys.executable, sha256=sha256(Path(sys.executable).read_bytes()).hexdigest())
    kwargs = dict(target=capability, lease=lease, journal=journal, schema=schema, source=source,
        child_inputs=dict(capability=cap_path, source_review=source_path,
            creation_review_sha256=capability.expected_creation_review_sha256),
        interpreter=interpreter, source_guard=lambda target: True)
    return factory, kwargs, events, made, primary, journal


def test_controlled_state_flow_uses_actual_PinnedSession_connection_API_and_private_journal(monkeypatch, capability, owned_files):
    factory, kwargs, events, made, primary, journal = state(monkeypatch, capability, owned_files, None)
    try:
        result = parent.rehearse(factory, **kwargs)
        assert result["status"] == "REAL_PRODUCTION_SCHEMA_ALEMBIC_REHEARSAL_COMMITTED_CATALOG_COMPARE_PENDING"
        assert result["permission_granted"] is False and result["canonical_approval"] is False
        assert events.count("CHILD_STARTED_AFTER_PARENT_CONNECTION_CLOSED") == 1
        assert all(session.closed and session.actual_connection.closed for session in made)
        calls = made[0].actual_connection.calls
        for index, (sql, options) in enumerate(calls):
            if options.get("no_parameters"):
                assert calls[index - 3:index] == [
                    ("SET LOCAL statement_timeout='750ms'", {}),
                    ("SET LOCAL lock_timeout='250ms'", {}),
                    ("SET LOCAL idle_in_transaction_session_timeout='15s'", {})]
        assert journal.last_receipt["exact_readback"] is True
    finally:
        journal.close()


@pytest.mark.parametrize("fault", ["DDL", "commit", "restore_close", "child", "post_close"])
def test_actual_wrapper_and_journal_never_record_false_qualified_outcome(monkeypatch, capability, owned_files, fault):
    factory, kwargs, events, made, primary, journal = state(monkeypatch, capability, owned_files, fault)
    try:
        with pytest.raises(OSError) as caught:
            parent.rehearse(factory, **kwargs)
        assert caught.value is primary
        records = [json.loads(path.read_bytes()) for path in sorted(journal.root.glob("*.json"))]
        assert records and not any(record["kind"] == "REAL_REHEARSAL_RETURNED_NOT_CANONICAL_APPROVAL_OR_COPY" for record in records)
        if fault in {"DDL", "commit", "restore_close"}:
            assert "CHILD_STARTED_AFTER_PARENT_CONNECTION_CLOSED" not in events
        if fault == "commit":
            assert records[-1]["report"]["status"] == "PRODUCTION_SCHEMA_RESTORE_COMMIT_UNKNOWN_STOP"
            assert events.count("COMMIT_ATTEMPT") == 1
        if fault == "restore_close":
            assert records[-1]["report"]["status"] == "PRODUCTION_SCHEMA_RESTORE_COMMITTED_CLEANUP_UNKNOWN_STOP"
            assert records[-1]["report"]["cleanup_failure_type"] == "OSError"
            assert made[0].actual_connection.closed
        assert all(record["report"]["permission_granted"] is False for record in records)
    finally:
        journal.close()
