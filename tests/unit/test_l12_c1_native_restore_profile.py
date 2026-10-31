"""New controlled profile seams only; no PG, engine, cluster or native calls."""

from copy import deepcopy
from contextlib import nullcontext
from hashlib import sha256
import json
import os
from types import SimpleNamespace

import pytest
from sqlalchemy import Column, Integer, MetaData, Table, UniqueConstraint

from tools import l12_private_schema_evidence as schema
from tools import l12_private_restore_main as caller
from tools import l12_main_nonce_target as target_module
from tools.l12_scoped_chunks_v2 import canonical
from tests.unit.test_l12_nonce_restore_caller import nonce as nonce  # Explicit own-nonce fixture, no transitive fixture reliance.
from tools.l12_nonce_copy_lease import NonceCopyLease


# Literal C1 dump objects, independent of the implementation's mapping tables.
UNIQUES = (
    ("broker_accounts", "uq_broker_accounts_name", "name"),
    ("strategies", "uq_strategies_code", "code"),
    ("bot_control_attempts", "uq_bot_control_attempts_client_order_id", "client_order_id"),
    ("bot_control_commands", "uq_bot_control_commands_request_id", "request_id"),
    ("broker_accounts", "uq_broker_accounts_external_account_id", "external_account_id"),
    ("broker_orders", "uq_broker_orders_broker_order_id", "broker_order_id"),
    ("broker_orders", "uq_broker_orders_client_order_id", "client_order_id"),
    ("fills", "uq_fills_broker_fill_id", "broker_fill_id"),
    ("v2_confirmation_exit_evaluations", "uq_v2_confirmation_exit_evaluations_fanout_slot_id", "fanout_slot_id"),
    ("v2_confirmation_exit_evaluations", "uq_v2_confirmation_exit_evaluations_source_fill_id", "source_fill_id"),
)


def catalog():
    result, native = {}, {}
    for table, name, column in UNIQUES:
        reflected = result.setdefault(table, dict(indexes=[], uniques=[], checks=[], foreign_keys=[]))
        actual = native.setdefault(table, dict(table=table, schema="public", indexes=[], constraints=[], triggers=[]))
        reflected["uniques"].append(dict(name=name, column_names=[column]))
        actual["constraints"].append(dict(name=name, kind="u", validated=True,
            deferrable=False, initially_deferred=False))
        actual["indexes"].append(dict(name=name, unique_index=True, valid=True, ready=True,
            live=True, immediate=True))
    for table, name, column in (("broker_accounts", "ix_broker_accounts_name", "name"),
                                ("strategies", "ix_strategies_code", "code")):
        result[table]["indexes"].append(dict(name=name, unique=False, column_names=[column]))
        native[table]["indexes"].append(dict(name=name, unique_index=False, valid=True, ready=True,
            live=True, immediate=True))
    result["__native_catalog__"] = list(native.values())
    return result


def native_table(value, name):
    return next(row for row in value["__native_catalog__"] if row["table"] == name)


def review(purpose="BASELINE_SOURCE21"):
    evidence = {key: dict(path="/private/tmp/CONTROLLED-" + key, sha256=digest)
        for key, digest in schema.ACCEPTED_NATIVE_EVIDENCE.items()}
    evidence.update(real_receipt=dict(path="/private/tmp/CONTROLLED-real", sha256="1" * 64),
        closure_receipt=dict(path="/private/tmp/CONTROLLED-closure", sha256="2" * 64))
    return dict(version="L12_C1_NATIVE_RESTORE_SCHEMA_V1",
        purpose="C1_NATIVE_BASELINE_PLUS_ACCEPTED_THREE_REVISION_LEDGER", review_id="CONTROLLED_NOT_APPROVAL",
        dialect="postgresql", target_purpose=purpose, canonical_catalog_sha256="3" * 64,
        unique_equivalences={
            "broker_accounts": dict(index="ix_broker_accounts_name", constraint="uq_broker_accounts_name", columns=["name"]),
            "strategies": dict(index="ix_strategies_code", constraint="uq_strategies_code", columns=["code"])},
        named_native_uniques={
            "bot_control_attempts": {"uq_bot_control_attempts_client_order_id": ["client_order_id"]},
            "bot_control_commands": {"uq_bot_control_commands_request_id": ["request_id"]},
            "broker_accounts": {"uq_broker_accounts_external_account_id": ["external_account_id"]},
            "broker_orders": {"uq_broker_orders_broker_order_id": ["broker_order_id"],
                "uq_broker_orders_client_order_id": ["client_order_id"]},
            "fills": {"uq_fills_broker_fill_id": ["broker_fill_id"]},
            "v2_confirmation_exit_evaluations": {
                "uq_v2_confirmation_exit_evaluations_fanout_slot_id": ["fanout_slot_id"],
                "uq_v2_confirmation_exit_evaluations_source_fill_id": ["source_fill_id"]}},
        evidence=evidence, execution_binding={},
        canonical_catalog_fingerprint_reviewed_against_accepted_native_graph=True,
        construction_review=dict(target_capability_sha256="4" * 64, target_purpose=purpose,
            creation_source_head="a" * 40, real_receipt_sha256="1" * 64, closure_receipt_sha256="2" * 64,
            restore_commit_verified=True, real_upgrade_verified=purpose == "UPGRADED_SOURCE21_PLUS2",
            cleanup_and_zero_clients_verified=True, acceptance_review_sha256="5" * 64))


def test_all_ten_literal_unique_objects_and_plain_indexes_enforce():
    value = catalog()
    schema.require_pg_enforcement(value["__native_catalog__"])
    assert schema.require_exact_c1_uniqueness(value) is None


@pytest.mark.parametrize("table,name,column", UNIQUES)
def test_every_unique_is_required_even_with_reviewed_catalog_hash(table, name, column):
    value = catalog()
    value[table]["uniques"] = [row for row in value[table]["uniques"] if row["name"] != name]
    with pytest.raises(ValueError):
        schema.require_exact_c1_uniqueness(value)


@pytest.mark.parametrize("table,name,column", UNIQUES[2:])
def test_eight_unique_columns_cannot_be_substituted(table, name, column):
    value = catalog()
    next(row for row in value[table]["uniques"] if row["name"] == name)["column_names"] = ["foreign"]
    with pytest.raises(ValueError, match="exact_eight_native"):
        schema.require_exact_c1_uniqueness(value)


@pytest.mark.parametrize("field", ["unique_index", "valid", "ready", "live", "immediate"])
def test_backing_unique_index_cannot_be_disabled(field):
    value = catalog()
    row = next(row for row in native_table(value, "broker_orders")["indexes"]
        if row["name"] == "uq_broker_orders_client_order_id")
    row[field] = False
    with pytest.raises(ValueError):
        schema.require_exact_c1_uniqueness(value)


@pytest.mark.parametrize("field,bad", [("kind", "c"), ("validated", False),
    ("deferrable", True), ("initially_deferred", True), ("name", "other")])
def test_named_unique_constraint_state_not_waivable(field, bad):
    value = catalog()
    next(row for row in native_table(value, "fills")["constraints"]
        if row["name"] == "uq_fills_broker_fill_id")[field] = bad
    with pytest.raises(ValueError):
        schema.require_exact_c1_uniqueness(value)


@pytest.mark.parametrize("table,name", [("broker_accounts", "ix_broker_accounts_name"),
    ("strategies", "ix_strategies_code")])
def test_two_plain_indexes_are_not_silently_normalized_to_unique(table, name):
    value = catalog()
    next(row for row in native_table(value, table)["indexes"] if row["name"] == name)["unique_index"] = True
    with pytest.raises(ValueError):
        schema.require_exact_c1_uniqueness(value)


@pytest.mark.parametrize("purpose", ["BASELINE_SOURCE21", "UPGRADED_SOURCE21_PLUS2"])
def test_exact_explicit_profile_only(purpose):
    assert schema.validate_c1_review(review(purpose)) == purpose


@pytest.mark.parametrize("fault", ["extra", "version", "missing_eight", "missing_two", "fingerprint",
    "unreviewed", "dump", "ledger", "cleanup", "commit", "upgrade", "closure"])
def test_profile_refuses_incomplete_or_unaccepted_proof(fault):
    value = review()
    if fault == "extra":
        value["extra"] = True
    elif fault == "version":
        value["version"] = "old"
    elif fault == "missing_eight":
        value["named_native_uniques"] = {}
    elif fault == "missing_two":
        value["unique_equivalences"] = {}
    elif fault == "fingerprint":
        value["canonical_catalog_sha256"] = "0" * 64
    elif fault == "unreviewed":
        value["canonical_catalog_fingerprint_reviewed_against_accepted_native_graph"] = False
    elif fault in {"dump", "ledger"}:
        value["evidence"]["c1_dump" if fault == "dump" else "ledger"]["sha256"] = "f" * 64
    else:
        key = {"cleanup": "cleanup_and_zero_clients_verified", "commit": "restore_commit_verified",
            "upgrade": "real_upgrade_verified", "closure": "closure_receipt_sha256"}[fault]
        value["construction_review"][key] = "9" * 64 if fault == "closure" else fault == "upgrade"
    with pytest.raises(ValueError):
        schema.validate_c1_review(value)


def test_legacy_model_unique_predicate_remains_strict():
    metadata = MetaData()
    Table("controlled", metadata, Column("id", Integer), UniqueConstraint("id", name="uq_controlled"))
    value = {"controlled": dict(uniques=[], checks=[]),
        "__native_catalog__": [dict(table="controlled", indexes=[])]}
    with pytest.raises(ValueError, match="canonical_model_constraint_or_index_missing"):
        schema.require_model_membership(value, metadata)


def test_existing_legacy_canonical_review_still_accepts_unchanged_model(monkeypatch):
    from sqlalchemy.dialects import postgresql
    metadata = MetaData()
    Table("controlled", metadata, Column("id", Integer), UniqueConstraint("id", name="uq_controlled"))
    observed = {"controlled": dict(uniques=[dict(name="uq_controlled", column_names=["id"])], checks=[]),
        "__native_catalog__": [dict(table="controlled", schema="public", indexes=[], triggers=[],
            constraints=[dict(name="uq_controlled", kind="u", validated=True, deferrable=False, initially_deferred=False)])]}
    connection = SimpleNamespace(dialect=postgresql.dialect(),
        execute=lambda statement: SimpleNamespace(scalar_one=lambda: "origin"))
    value = dict(purpose="INDEPENDENT_CANONICAL_PRIVATE_SCHEMA_REVIEW", review_id="CONTROLLED_LEGACY",
        dialect="postgresql", canonical_model_ddl_sha256=schema.model_ddl_sha256(metadata, connection.dialect),
        canonical_catalog_sha256=sha256(canonical(observed)).hexdigest())
    monkeypatch.setattr(schema, "reflected_catalog", lambda *a: observed)
    assert schema.verify_canonical_catalog(connection, metadata, value) == value["canonical_catalog_sha256"]


class Connection:
    dialect = SimpleNamespace(name="postgresql")

    def execute(self, statement):
        assert str(statement) == "SHOW session_replication_role"
        return SimpleNamespace(scalar_one=lambda: "origin")


def test_native_hash_still_refuses_extra_or_changed_catalog(monkeypatch):
    expected = catalog()
    value = review()
    value["canonical_catalog_sha256"] = sha256(canonical(expected)).hexdigest()
    observed = deepcopy(expected)
    observed["unexpected_object"] = {"not_approved": True}
    monkeypatch.setattr(schema, "c1_faithful_catalog", lambda *a: observed)
    with pytest.raises(ValueError, match="C1_native_schema_changed"):
        schema.verify_c1_native_catalog(Connection(), value)


def test_native_mode_does_not_weaken_or_rewrite_legacy_model_check(monkeypatch):
    value = review()
    observed = catalog()
    value["canonical_catalog_sha256"] = sha256(canonical(observed)).hexdigest()
    monkeypatch.setattr(schema, "c1_faithful_catalog", lambda *a: observed)
    monkeypatch.setattr(schema, "require_model_membership", lambda *a: pytest.fail("legacy mode not this profile"))
    assert schema.verify_canonical_catalog(Connection(), MetaData(), value) == value["canonical_catalog_sha256"]


@pytest.mark.parametrize("purpose,expected_count", [("BASELINE_SOURCE21", 21), ("UPGRADED_SOURCE21_PLUS2", 23)])
def test_metadata_scope_separates_baseline_and_upgraded_without_ddl(purpose, expected_count):
    from tests.unit.test_l12_scoped_chunks_v2 import operands
    from tools.l12_scoped_restore_v2 import private_schema
    _, _, metadata_raw, inputs = operands()
    metadata = private_schema(metadata_raw, reviewed_metadata_sha256=inputs["reviewed_metadata_sha256"],
        schema_review=review(purpose))
    assert len(metadata.tables) == expected_count
    assert ("brokerdecides_dispatch_retirements" in metadata.tables) is (expected_count == 23)
    assert ("brokerdecides_dispatch_evidence" in metadata.tables) is (expected_count == 23)
    assert metadata.tables["oms_managed_positions"].c.scales_done.nullable is True


def capability():
    value = dict(version="L12_MAIN_NONCE_PG16_TARGET_V1", nonce="1" * 32, uid=os.getuid(),
        database_user="controlled", system_identifier="12345", postmaster_pid=987654321,
        postmaster_started="CONTROLLED", server_version_num="160015", source_head="a" * 40,
        creation_review_sha256="b" * 64, creation_journal_tail_sha256="c" * 64,
        at_creation_schema_state="CATALOG_VERIFIED_ZERO_USER_RELATIONS_NO_COPY_OR_BOOT")
    value.update(target_module.layout(value["nonce"]))
    raw = json.dumps(value).encode()
    return target_module.MainNonceTarget(raw, sha256(raw).hexdigest(), "b" * 64)


def consumer_binding(target, root):
    return dict(version="L12_C1_1409_TOOLS_ONLY_COPY_CONSUMER_V1",
        target_capability_sha256=target.reviewed_sha256, creation_source_head="a" * 40,
        consumer_source_head="d" * 40, consumer_root=str(root.resolve()),
        consumer_inventory_sha256=sha256(b'{"controlled":"pin"}').hexdigest(), review_sha256="e" * 64)


def test_consumer_binding_preserves_original_capability(tmp_path):
    target = capability()
    original = target.raw
    assert target_module.validate_execution_binding(consumer_binding(target, tmp_path), target,
        inventory={"controlled": "pin"}) == "d" * 40
    assert target.raw == original
    assert target.values["source_head"] == "a" * 40


@pytest.mark.parametrize("field", ["version", "target_capability_sha256", "creation_source_head",
    "consumer_source_head", "consumer_root", "consumer_inventory_sha256", "review_sha256"])
def test_consumer_binding_refuses_identity_or_unreviewed_inventory_drift(tmp_path, field):
    target = capability()
    value = consumer_binding(target, tmp_path)
    value[field] = "foreign"
    with pytest.raises(ValueError):
        target_module.validate_execution_binding(value, target, inventory={"controlled": "pin"})


def test_unknown_profile_cannot_switch_legacy_caller_or_start_engine(monkeypatch):
    monkeypatch.setattr(caller, "create_engine", lambda *a, **k: pytest.fail("no engine"))
    value = dict(version="L12_MAIN_C1_NATIVE_ROW_RESTORE_V1", operation="construct", capability={},
        creation_review_sha256="a" * 64, source={}, manifest={}, metadata={}, bridge_receipt={}, parts_root="/none",
        chunk_review={}, schema_review={}, writer_exclusion={}, execution_review_sha256="b" * 64,
        construction_receipt=None, configured_accounts=[], expanded_accounts=[])
    with pytest.raises(ValueError, match="existing_target_row_restore_only"):
        caller.checked_packet(json.dumps(value).encode())


def test_real_file_lease_keeps_creation_identity_but_binds_new_consumer(nonce, tmp_path):
    value = consumer_binding(nonce, tmp_path)
    value["creation_source_head"] = nonce.values["source_head"]
    lease = NonceCopyLease(nonce, manifest_sha256="6" * 64, metadata_sha256="7" * 64,
        schema_review_sha256=sha256(canonical({})).hexdigest(), writer_exclusion_receipt_sha256="8" * 64,
        source_guard=lambda subject: subject is nonce, execution_binding=value)
    try:
        authority = SimpleNamespace(execution_head="a" * 40, lease_receipt_sha256=lease.receipt_sha256)
        plan = dict(manifest_sha256="6" * 64, metadata_sha256="7" * 64)
        with pytest.raises(ValueError, match="input_binding_changed"):
            lease.bind_inputs(plan=plan, schema_review={}, authority=authority, binding="9" * 64)
        authority.execution_head = "d" * 40
        lease.bind_inputs(plan=plan, schema_review={}, authority=authority, binding="9" * 64)
        assert lease.context["source_head"] == "a" * 40
        assert lease.context["execution_head"] == "d" * 40
        assert lease("9" * 64, authority) is True
    finally:
        lease.close()


def test_consumer_binding_cannot_authorize_new_ddl(nonce, tmp_path):
    value = consumer_binding(nonce, tmp_path)
    with pytest.raises(ValueError, match="copy_only_no_creation_or_DDL"):
        NonceCopyLease(nonce, manifest_sha256="6" * 64, metadata_sha256="7" * 64,
            schema_review_sha256="8" * 64, writer_exclusion_receipt_sha256="9" * 64,
            source_guard=lambda subject: subject is nonce, operation="construct", execution_binding=value)


@pytest.mark.parametrize("changed", [False, True])
def test_new_root_ini_is_verified_separately_not_dropped_from_inventory(tmp_path, monkeypatch, changed):
    root = tmp_path.resolve()
    path = root / "alembic.ini"
    path.write_bytes(b"[alembic]\n")
    inventory = {"alembic.ini": sha256(b"[alembic]\n").hexdigest(), "tools/controlled.py": "1" * 64}
    seen = []
    monkeypatch.setattr(caller.v10, "_verify_tree", lambda actual, pins: seen.append((actual, pins)))
    monkeypatch.setattr(caller, "sys", SimpleNamespace(modules={}))
    monkeypatch.setattr(caller.time, "time_ns", lambda: 1_000_000)
    guard = caller.CopySourceGuard(root, "d" * 40, tuple(inventory.items()), 100, "a" * 40)
    if changed:
        path.write_bytes(b"[changed]\n")
    assert guard(SimpleNamespace(values={"source_head": "a" * 40})) is (not changed)
    assert seen == ([] if changed else [(root, {"tools/controlled.py": "1" * 64})])


def test_creation_head_is_not_accepted_as_new_consumer_execution_head(monkeypatch, tmp_path):
    monkeypatch.setattr(caller.time, "time_ns", lambda: 1_000_000)
    monkeypatch.setattr(caller.v10, "_verify_tree", lambda *a: pytest.fail("must refuse before inventory"))
    guard = caller.CopySourceGuard(tmp_path.resolve(), "d" * 40, (), 100, "a" * 40)
    assert guard(SimpleNamespace(execution_head="a" * 40)) is False


class DuplicateSession:
    def __init__(self, outcome):
        self.outcome, self.reads, self.closed, self.rollback_calls = outcome, 0, False, 0
        self.row = dict(id="11111111-1111-4111-8111-111111111111", name="CONTROLLED", external_account_id="CONTROLLED")

    def begin(self):
        return SimpleNamespace(rollback=self.rollback)

    def begin_nested(self):
        return nullcontext()

    def get_bind(self):
        return SimpleNamespace(dialect=SimpleNamespace(name="postgresql"))

    def execute(self, statement, params=None):
        from sqlalchemy.exc import IntegrityError
        if getattr(statement, "is_insert", False):
            assert params["id"] != self.row["id"]
            assert params["name"] == "CONTROLLED"
            assert params["external_account_id"] is None
            if self.outcome == "success":
                return None
            orig = SimpleNamespace(sqlstate="23503" if self.outcome == "other_code" else "23505",
                diag=SimpleNamespace(constraint_name="foreign" if self.outcome in {"other_constraint", "cleanup"}
                    else "uq_broker_accounts_name"))
            raise IntegrityError("CONTROLLED", {}, orig)
        if getattr(statement, "is_select", False):
            self.reads += 1
            row = dict(self.row)
            if self.outcome == "changed_readback" and self.reads == 2:
                row["name"] = "CHANGED"
            return SimpleNamespace(mappings=lambda: [row])
        return None

    def rollback(self):
        self.rollback_calls += 1
        if self.outcome == "cleanup":
            raise RuntimeError("CONTROLLED_CLEANUP")

    def close(self):
        self.closed = True


def duplicate_control(monkeypatch, outcome):
    from tools import l12_scoped_restore_v2 as restore
    session = DuplicateSession(outcome)
    monkeypatch.setattr(restore, "require_private_factory", lambda *a, **k: None)
    monkeypatch.setattr(restore, "verify_private_session", lambda *a, **k: None)
    calls = []

    def held():
        calls.append(True)
        return not (outcome == "lost_lease" and len(calls) > 1)

    return session, lambda: restore.prove_duplicate_account_name_refused(lambda: session,
        schema_review=review(), require_held_proof=held, target_capability=object())


def test_connected_duplicate_control_requires_exact_violation_and_rollback(monkeypatch):
    session, invoke = duplicate_control(monkeypatch, "refused")
    result = invoke()
    assert result["status"] == "PRIVATE_RESTORED_DUPLICATE_ACCOUNT_REFUSED_AND_ROLLED_BACK"
    assert result["constraint"] == "uq_broker_accounts_name"
    assert result["sqlstate"] == "23505"
    assert result["account_rows"] == 1
    assert result["permission_granted"] is False
    assert result["before_sha256"] == result["after_sha256"]
    assert session.closed is True
    assert session.rollback_calls >= 2


@pytest.mark.parametrize("outcome", ["success", "other_constraint", "other_code", "changed_readback", "lost_lease"])
def test_duplicate_control_never_converts_uncertainty_to_success(monkeypatch, outcome):
    session, invoke = duplicate_control(monkeypatch, outcome)
    with pytest.raises(ValueError):
        invoke()
    assert session.closed is True


def test_duplicate_cleanup_never_masks_primary_and_still_closes(monkeypatch):
    session, invoke = duplicate_control(monkeypatch, "cleanup")
    with pytest.raises(ValueError, match="wrong_constraint") as error:
        invoke()
    assert session.closed is True
    assert "private_cleanup_failure_type=RuntimeError" in error.value.__notes__
