"""Controlled raw-delivery provenance and real parent seams; no PG/child."""

import pytest

from tools import l12_schema_rehearsal_inputs as inputs
from tools import l12_real_alembic_child as child
from tests.unit.test_l12_schema_rehearsal_inputs import SQL, review as framed_review
from tests.unit.test_l12_schema_rehearsal_outcomes import state, parent
from tests.unit.test_l12_main_nonce_target import capability, owned_files

README = b"CONTROLLED_C1_DELIVERY_NOT_ACTUAL_AUTHORITY"
REVISION = b"20261015_bot_control\n"


def review(raw=SQL, readme=README, revision=REVISION):
    value = framed_review(raw)
    del value["exported_snapshot"]
    del value["dump_frames_sha256"]
    value.update(version="L12_C11030_C1_DELIVERED_PRODUCTION_SCHEMA_V1",
        purpose="MAIN_REVIEWED_C11030_C1_DELIVERED_PRODUCTION_DDL_ONLY",
        delivery_sha256={"c1_prod_schema_20261024.sql": inputs.sha(raw),
            "README.txt": inputs.sha(readme), "alembic_version.txt": inputs.sha(revision)})
    return value


def schema(value=None, *, raw=SQL, artifacts=(README, REVISION)):
    encoded = inputs.canonical(review() if value is None else value)
    return inputs.ReviewedSchema(raw, encoded, inputs.sha(encoded), delivery_artifacts=artifacts)


def test_C1_raw_artifacts_bound_without_invented_snapshot_or_frames():
    value = schema()
    assert value.raw == SQL and type(value) is inputs.ReviewedSchema
    assert "exported_snapshot" not in value.review and "dump_frames_sha256" not in value.review
    assert value.provenance["exported_snapshot_supplied"] is False
    assert value.provenance["dump_frames_supplied"] is False
    assert value.manifest_binding_sha256 == inputs.sha(inputs.canonical(value.provenance))
    assert b"".join(piece.raw for piece in value.pieces) == SQL


def test_original_framed_mode_remains_exact():
    encoded = inputs.canonical(framed_review())
    value = inputs.ReviewedSchema(SQL, encoded, inputs.sha(encoded))
    assert value.manifest_binding_sha256 == "a" * 64
    assert value.provenance == dict(version="L12_PRODUCTION_SCHEMA_REAL_ALEMBIC_REHEARSAL_V1",
        dump_frames_sha256="a" * 64, exported_snapshot="00000001-00000002-1")


@pytest.mark.parametrize("key", ["exported_snapshot", "dump_frames_sha256"])
def test_C1_mode_cannot_smuggle_framed_claim(key):
    value = review()
    value[key] = "INVENTED"
    with pytest.raises(ValueError, match="exact_C11030_raw_dump_README_revision_provenance_required"):
        schema(value)


@pytest.mark.parametrize("part", ["dump", "readme", "revision"])
def test_changed_actual_delivery_bytes_refuse(part):
    with pytest.raises(ValueError, match="actual_C1_delivery_artifact_bytes_or_revision_changed"):
        schema(raw=SQL + (b"-- changed\n" if part == "dump" else b""),
            artifacts=(README + (b"changed" if part == "readme" else b""),
                REVISION + (b"changed" if part == "revision" else b"")))


def test_refreshed_hash_cannot_accept_wrong_revision():
    wrong = b"20261022_l12_retirement\n"
    with pytest.raises(ValueError, match="actual_C1_delivery_artifact_bytes_or_revision_changed"):
        schema(review(revision=wrong), artifacts=(README, wrong))


@pytest.mark.parametrize("fault", ["missing", "extra"])
def test_exact_three_artifact_membership_required(fault):
    value = review()
    if fault == "missing":
        del value["delivery_sha256"]["README.txt"]
    else:
        value["delivery_sha256"]["FRAMED_RECEIPT"] = "a" * 64
    with pytest.raises(ValueError, match="actual_C1_delivery_artifact_bytes_or_revision_changed"):
        schema(value)


@pytest.mark.parametrize("artifacts", [(), (README,), [README, REVISION], (bytearray(README), REVISION)])
def test_explicit_immutable_README_revision_bytes_required(artifacts):
    with pytest.raises(ValueError, match="exact_C11030_raw_dump_README_revision_provenance_required"):
        schema(artifacts=artifacts)


@pytest.mark.parametrize("change", [dict(authority_sha256="0" * 64), dict(alembic_revision="head"),
    dict(execution_policy="UNBOUNDED"), dict(whole_database_schema=False)])
def test_existing_common_restore_guards_unchanged(change):
    value = review()
    value.update(change)
    with pytest.raises(ValueError):
        schema(value)


def test_every_statement_still_needs_exact_review():
    value = review()
    value["statements"] = []
    with pytest.raises(ValueError, match="every_raw_statement_and_client_control_requires_exact_review"):
        schema(value)


def test_provenance_has_no_mutable_review_alias():
    value = schema()
    changed = value.provenance
    changed["delivery_sha256"].clear()
    assert set(value.provenance["delivery_sha256"]) == {
        "c1_prod_schema_20261024.sql", "README.txt", "alembic_version.txt"}


def test_actual_parent_accepts_C1_binding_and_journals_provenance(monkeypatch, capability, owned_files):
    factory, kwargs, events, made, primary, journal = state(monkeypatch, capability, owned_files, None)
    value = schema()
    kwargs["schema"] = value
    kwargs["lease"].context.update(schema_review_sha256=value.review_sha256,
        manifest_sha256=value.manifest_binding_sha256)
    try:
        result = parent.rehearse(factory, **kwargs)
        assert result["status"] == "REAL_PRODUCTION_SCHEMA_ALEMBIC_REHEARSAL_COMMITTED_CATALOG_COMPARE_PENDING"
        assert result["schema_provenance"] == value.provenance
        assert events.count("CHILD_STARTED_AFTER_PARENT_CONNECTION_CLOSED") == 1
        assert all(session.closed and session.actual_connection.closed for session in made)
    finally:
        journal.close()


def test_actual_parent_refuses_old_frame_hash_for_C1_delivery(monkeypatch, capability, owned_files):
    factory, kwargs, events, made, primary, journal = state(monkeypatch, capability, owned_files, None)
    kwargs["schema"] = schema()
    kwargs["lease"].context["schema_review_sha256"] = kwargs["schema"].review_sha256
    try:
        with pytest.raises(ValueError, match="exact_actual_nonce_lease_reviewed_whole_schema_source_and_journal_required"):
            parent.rehearse(factory, **kwargs)
        assert not made and not events
    finally:
        journal.close()


def test_REAL_stage_minimum_includes_import_time_transitive_helpers():
    assert set(child.TOOL_FILES) == {
        "tools/l12_real_alembic_child.py", "tools/l12_schema_rehearsal_inputs.py",
        "tools/l12_production_schema_rehearsal.py", "tools/l12_main_nonce_target.py",
        "tools/l12_nonce_copy_lease.py", "tools/l12_private_evidence_journal.py",
        "tools/l12_private_full23_lifecycle.py", "tools/l12_create_private55521.py",
        "tools/l12_close_asof_contract.py", "tools/l12_scoped_chunks_v2.py",
        "tools/l12_scoped_restore_v2.py", "tools/l12_service_boot_replay.py",
        "tools/l12_close_export_restore.py", "tools/l12_required_copy_scope.py",
        "tools/l12_today_close_replay.py", "tools/l12_private_schema_evidence.py"}
