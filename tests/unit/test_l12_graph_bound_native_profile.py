"""Controlled Graph/connection/journal seams, never actual schema approval.

The expected fixture is declared BEFORE the separately mutated observation.
Only tests replace the literal accepted-artifact SHA with that controlled
reference. Real mode always requires the immutable C61/FE1 artifact hashes.
"""

import base64
from copy import deepcopy
from dataclasses import replace
from hashlib import sha256
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from tools import l12_private_schema_evidence as schema
from tools import l12_native_schema_graph as native
from tools import l12_native_graph_adapter as adapter
from tools import l12_whole_schema_semantics as semantic
from tools import l12_private_full23_lifecycle as lifecycle
from tools import l12_private_restore_main as caller
from tools.l12_private_evidence_journal import PrivateJournal
from tests.unit.test_l12_c1_native_restore_profile import review as old_review, catalog, Connection
from tests.unit.test_l12_native_graph_adapter import packet, with_table, EPOCH


def controlled(monkeypatch, purpose="BASELINE_SOURCE21"):
    expected_packet = with_table(packet())
    expected_packet["source_sha256"] = "5c618ca470335b839797011251bfbab44f9e294c5f742c4e3fa8a3d8617100ec"
    expected_packet["role"] = "PRIVATE_BASELINE" if purpose == "BASELINE_SOURCE21" else "PRIVATE_UPGRADED"
    expected_packet["sections"]["pg_class"][0]["raw"]["reloptions"] = ["one", "two"]
    expected = adapter.adapt(semantic.canonical(expected_packet), semantic.digest(expected_packet),
        source_pins=schema.GRAPH_SOURCE_PINS, profile_review_sha256="d" * 64,
        closure_audit_sha256="e" * 64, epoch=EPOCH)
    expected_raw = semantic.canonical(expected)
    key = "baseline_graph" if purpose == "BASELINE_SOURCE21" else "upgraded_graph"
    monkeypatch.setitem(schema.ACCEPTED_NATIVE_EVIDENCE, key, sha256(expected_raw).hexdigest())
    value = old_review(purpose)
    value.update(version="L12_C1_NATIVE_RESTORE_SCHEMA_V2_GRAPH_BOUND", canonical_catalog_sha256=None,
        canonical_catalog_fingerprint_reviewed_against_accepted_native_graph=False,
        graph_projection=dict(version="WHOLE_ACCEPTED_GRAPH_THEN_SAME_RR_REFLECTION_DRIFT_FENCE_V1",
            source_pins=dict(schema.GRAPH_SOURCE_PINS), owner_mapping={}, extra_exclusions=[],
            app_component=deepcopy(schema.COMBINED_APP_COMPONENT),
            accepted_artifact_sha256=sha256(expected_raw).hexdigest()))
    reference = schema.accepted_native_reference(value, expected_raw)
    return value, reference, expected_packet, catalog()


@pytest.mark.parametrize("purpose", ["BASELINE_SOURCE21", "UPGRADED_SOURCE21_PLUS2"])
def test_graph_bound_projection_uses_separately_declared_reference(monkeypatch, purpose):
    value, reference, observed, reflected = controlled(monkeypatch, purpose)
    proof = schema._bind_native_projection(value, reference, semantic.canonical(observed), reflected, "f" * 64)
    assert proof.summary()["version"] == "WHOLE_ACCEPTED_GRAPH_THEN_SAME_RR_REFLECTION_DRIFT_FENCE_V1"
    assert proof.summary()["reflection_is_independent_approval"] is False
    assert proof.summary()["permission_granted"] is False
    assert proof.summary()["snapshot"] == "1:1:"
    assert proof.accepted_artifact_sha256 == sha256(reference.raw).hexdigest()


@pytest.mark.parametrize("fault", ["observed_digest", "observed_approval", "owner_map", "extra_exclusion",
    "projection_version", "source_pin", "accepted_hash"])
def test_V2_review_cannot_bless_an_observed_reflection_or_favorable_mapping(monkeypatch, fault):
    value, _, _, _ = controlled(monkeypatch)
    if fault == "observed_digest":
        value["canonical_catalog_sha256"] = "f" * 64
    elif fault == "observed_approval":
        value["canonical_catalog_fingerprint_reviewed_against_accepted_native_graph"] = True
    elif fault == "owner_map":
        value["graph_projection"]["owner_mapping"] = {"one": "another"}
    elif fault == "extra_exclusion":
        value["graph_projection"]["extra_exclusions"] = ["unexpected"]
    elif fault == "projection_version":
        value["graph_projection"]["version"] = "other"
    elif fault == "source_pin":
        value["graph_projection"]["source_pins"]["tools/l12_native_schema_graph.py"] = "f" * 64
    else:
        value["graph_projection"]["accepted_artifact_sha256"] = "f" * 64
    with pytest.raises(ValueError):
        schema.validate_c1_review(value)


@pytest.mark.parametrize("fault", ["owner", "acl", "options_order", "persistence", "rls", "trigger_flag",
    "extra_dependency", "permission", "missing_layout", "missing_section", "contradiction"])
def test_full_graph_drift_or_incompleteness_cannot_mint_projection(monkeypatch, fault):
    value, reference, observed, reflected = controlled(monkeypatch)
    if fault == "owner":
        observed["native_identities"][1]["object_names"] = ["another_owner"]
    elif fault == "acl":
        observed["sections"]["pg_namespace"][0]["raw"]["nspacl"] = []
    elif fault == "options_order":
        observed["sections"]["pg_class"][0]["raw"]["reloptions"].reverse()
    elif fault == "persistence":
        observed["sections"]["pg_class"][0]["raw"]["relpersistence"] = "u"
    elif fault == "rls":
        observed["sections"]["pg_class"][0]["raw"]["relrowsecurity"] = True
    elif fault == "trigger_flag":
        observed["sections"]["pg_class"][0]["raw"]["relhastriggers"] = True
    elif fault == "extra_dependency":
        observed["dependencies"].append(dict(catalog="pg_shdepend", dependent_class="pg_catalog.pg_namespace",
            dependent_class_oid=2615, objid=10, objsubid=0, referenced_class="pg_catalog.pg_authid",
            refobjid=20, refobjsubid=0, deptype="o"))
    elif fault == "permission":
        observed["permission_granted"] = True
    elif fault == "missing_layout":
        observed["catalog_layout"] = [row for row in observed["catalog_layout"]
            if (row["catalog"], row["field"]) != ("pg_namespace", "nspacl")]
        del observed["sections"]["pg_namespace"][0]["raw"]["nspacl"]
    elif fault == "missing_section":
        del observed["sections"]["pg_index"]
    else:
        observed["sections"]["pg_namespace"][0]["typed_fields"]["nspowner"] = 999
    with pytest.raises(ValueError):
        schema._bind_native_projection(value, reference, semantic.canonical(observed), reflected, "f" * 64)


def test_assessed_target_cannot_be_substituted_for_sealed_expected_reference(monkeypatch):
    value, _, observed, _ = controlled(monkeypatch)
    observed["sections"]["pg_namespace"][0]["raw"]["nspacl"] = []
    changed = adapter.adapt(semantic.canonical(observed), semantic.digest(observed),
        source_pins=schema.GRAPH_SOURCE_PINS, profile_review_sha256="d" * 64,
        closure_audit_sha256="e" * 64, epoch=EPOCH)
    with pytest.raises(ValueError, match="only_C1_accepted_graph_bytes"):
        schema.accepted_native_reference(value, semantic.canonical(changed))


@pytest.mark.parametrize("fault", ["missing", "different_review", "changed_seal", "changed_reflection"])
def test_write_phase_requires_live_proof_and_same_reflected_fields(monkeypatch, fault):
    value, reference, observed, reflected = controlled(monkeypatch)
    proof = schema._bind_native_projection(value, reference, semantic.canonical(observed), reflected, "f" * 64)
    if fault == "missing":
        proof = None
    elif fault == "different_review":
        value["review_id"] = "ANOTHER"
    elif fault == "changed_seal":
        proof = replace(proof, reflected_sha256="1" * 64)
    else:
        reflected = deepcopy(reflected)
        reflected["__columns_and_defaults__"] = {"changed_default": "not_allowed"}
    monkeypatch.setattr(schema, "c1_faithful_catalog", lambda *a: reflected)
    with pytest.raises(ValueError, match="full_graph_verified_projection_missing_or_schema_changed"):
        schema.verify_c1_native_catalog(Connection(), value, native_projection=proof)


def test_write_phase_uses_verified_projection_not_user_expected_digest(monkeypatch):
    value, reference, observed, reflected = controlled(monkeypatch)
    proof = schema._bind_native_projection(value, reference, semantic.canonical(observed), reflected, "f" * 64)
    monkeypatch.setattr(schema, "c1_faithful_catalog", lambda *a: reflected)
    result = schema.verify_c1_native_catalog(Connection(), value, native_projection=proof)
    assert value["canonical_catalog_sha256"] is None
    assert result == sha256(semantic.canonical(reflected)).hexdigest()


def test_old_native_review_cannot_reach_main_artifact_or_engine(monkeypatch):
    monkeypatch.setattr(caller, "artifact", lambda *a, **k: pytest.fail("old mode must refuse first"))
    with pytest.raises(ValueError, match="graph_bound_V2"):
        caller.bind_native_restore_evidence(old_review(), object())


def test_old_native_review_cannot_reach_stream_or_factory(monkeypatch):
    from tools import l12_scoped_restore_v2 as restore
    monkeypatch.setattr(restore, "validate_v2", lambda *a, **k: pytest.fail("old mode must refuse first"))
    with pytest.raises(ValueError, match="graph_bound_V2"):
        restore.restore_v2(b"", b"", None, None, review={}, require_held_proof=None,
            journal_committed=None, schema_review=old_review())


class Session:
    def __init__(self, events, failure=None):
        self.events, self.failure, self.transaction = events, failure, False

    def execute(self, statement):
        sql = str(statement)
        if sql.startswith("SET TRANSACTION"):
            assert sql == "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"
            self.events.append("RR_RO")
            self.transaction = True
        else:
            assert sql == "SHOW session_replication_role"
            return SimpleNamespace(scalar_one=lambda: "replica" if self.failure == "replica" else "origin")

    def connection(self):
        return self

    def in_transaction(self):
        return self.transaction

    def rollback(self):
        self.events.append("rollback")
        if self.failure in {"rollback", "multiple"}:
            raise RuntimeError("CONTROLLED_ROLLBACK")
        self.transaction = False

    def close(self):
        self.events.append("close")
        if self.failure in {"close", "multiple"}:
            raise OSError("CONTROLLED_CLOSE")


def preflight(monkeypatch, failure=None):
    value, reference, observed, reflected = controlled(monkeypatch)
    events = []
    factory = object.__new__(lifecycle.CheckedFactory)
    factory.target_capability = SimpleNamespace(reviewed_sha256="9" * 64, values=dict(
        database=EPOCH["database"], database_user=EPOCH["role"], system_identifier=EPOCH["system_identifier"],
        postmaster_started=EPOCH["postmaster_started"], server_version_num="160015"))
    session = Session(events, failure)
    monkeypatch.setattr(lifecycle.CheckedFactory, "__call__", lambda self: session)

    def collect(connection, role, source_sha):
        assert connection is session and session.transaction is True
        assert role == "PRIVATE_BASELINE"
        assert source_sha == "5c618ca470335b839797011251bfbab44f9e294c5f742c4e3fa8a3d8617100ec"
        events.append("collect")
        if failure in {"collect", "multiple"}:
            raise ValueError("CONTROLLED_COLLECT")
        return observed

    def reflect(connection, purpose):
        assert connection is session and session.transaction is True
        assert purpose == "BASELINE_SOURCE21"
        events.append("reflect_same_RR")
        return reflected

    def held():
        events.append("held")
        return not (failure == "held_after_close" and "close" in events)

    def append(closure, raw, reflection):
        assert session.transaction is False
        assert events[-2:] == ["close", "held"]
        assert closure["backend_absence_proven"] is False
        assert closure["rollback_succeeded"] is True
        assert closure["session_close_succeeded"] is True
        assert sha256(raw).hexdigest() == closure["raw_graph_sha256"]
        assert semantic.decode(reflection) == reflected
        events.append("fsync_receipt")
        if failure == "journal":
            raise OSError("CONTROLLED_JOURNAL")
        return "f" * 64

    monkeypatch.setattr(native, "collect", collect)
    monkeypatch.setattr(schema, "c1_faithful_catalog", reflect)
    return events, lambda: schema.prepare_native_projection(factory, value, reference,
        held=held, append_closure=append)


def test_actual_preflight_order_is_same_RR_then_ROLLBACK_close_journal_and_bind(monkeypatch):
    events, invoke = preflight(monkeypatch)
    proof = invoke()
    assert events == ["held", "RR_RO", "collect", "reflect_same_RR", "rollback", "close", "held", "fsync_receipt"]
    assert proof.summary()["permission_granted"] is False


@pytest.mark.parametrize("failure", ["collect", "replica", "rollback", "close", "held_after_close", "journal", "multiple"])
def test_preflight_uncertainty_never_mints_proof_or_masks_primary(monkeypatch, failure):
    events, invoke = preflight(monkeypatch, failure)
    with pytest.raises((ValueError, RuntimeError, OSError)) as error:
        invoke()
    assert "close" in events
    if failure != "journal":
        assert "fsync_receipt" not in events
    if failure == "multiple":
        assert isinstance(error.value, ValueError)
        assert str(error.value) == "CONTROLLED_COLLECT"
        assert "private_cleanup_failure_type=RuntimeError" in error.value.__notes__
        assert "private_cleanup_failure_type=OSError" in error.value.__notes__


def test_existing_real_journal_preserves_full_graph_and_reflection_in_bounded_parts(tmp_path):
    directory = tmp_path / "journal"
    directory.mkdir(mode=0o700)
    store = PrivateJournal(directory)
    journal = lifecycle.Journal("a" * 64, store)
    raw = b"CONTROLLED_RAW_GRAPH" * 60000
    reflected = b'{"CONTROLLED_REFLECTION":true}'
    try:
        tail = lifecycle.journal_native_closure(journal, {"backend_absence_proven": False}, raw, reflected)
    finally:
        store.close()
    packets = [json.loads(path.read_bytes()) for path in sorted(directory.iterdir())]
    assert all(path.stat().st_size <= 1024 * 1024 for path in directory.iterdir())
    assert packets[-1]["kind"] == "NATIVE_RO_TRANSACTION_AND_SESSION_CLOSED"
    assert packets[-1]["value"]["backend_absence_proven"] is False
    assert tail == sha256(sorted(directory.iterdir())[-1].read_bytes()).hexdigest()
    for stream, expected in (("raw_graph", raw), ("reflected_catalog", reflected)):
        pieces = [row["value"] for row in packets if row["kind"] == "NATIVE_RO_EVIDENCE_PART"
            and row["value"]["stream"] == stream]
        assert [row["offset"] for row in pieces] == list(range(0, len(expected), 512 * 1024))
        assert b"".join(base64.b64decode(row["data_base64"], validate=True) for row in pieces) == expected
        assert all(row["whole_sha256"] == sha256(expected).hexdigest() for row in pieces)


@pytest.mark.parametrize("raw", [b"", b"x" * (32 * 1024**2 + 1)])
def test_evidence_size_caps_not_raised_or_silently_truncated(raw):
    journal = SimpleNamespace(record=lambda *a: pytest.fail("must refuse before writing"))
    with pytest.raises(ValueError, match="bounded_native_projection_evidence_required"):
        lifecycle.journal_native_closure(journal, {}, raw, b"x")


def test_reused_native_source_files_are_exact_reviewed_bytes():
    root = Path(__file__).parents[2]
    assert sha256((root / "tools/l12_native_schema_graph.py").read_bytes()).hexdigest() == "5c618ca470335b839797011251bfbab44f9e294c5f742c4e3fa8a3d8617100ec"
    assert sha256((root / "tools/l12_native_graph_adapter.py").read_bytes()).hexdigest() == "f09a1e4495e2fad3c86b3357344b6e1c4736eaf05967d74365c0ac6fcfca7f23"
    assert sha256((root / "tools/l12_whole_schema_semantics.py").read_bytes()).hexdigest() == "c6600d8a2ec4b3dccfda398d484de5d8e5263d5959ef5ee3ef8e47598bcde3ef"


def combined_inventory():
    path = Path(__file__).parents[2] / "tools/L12_06936_APP_COMPONENT.sha256"
    return {line.split("  ", 1)[1]: line.split("  ", 1)[0]
        for line in path.read_text(encoding="ascii").splitlines()}


def test_exact_06936_full_component_not_a_moving_head_or_old_settings():
    inventory = combined_inventory()
    assert len(inventory) == 333
    assert sha256(semantic.canonical(inventory)).hexdigest() == "eec18a105278def661e58879ed53e9559ac59443580864c3d4ef12da8fc6a5c9"
    assert schema.COMBINED_APP_COMPONENT["head"] == "06936d2ecc9469664bddbcf605896272afd00b25"
    assert len(schema.COMBINED_APP_PINS) == 13
    assert inventory["src/project_mai_tai/settings.py"] == "bf9a77551eb7805886e1c73ed3e3a74b474841e3a9f1c4e8de5cbaf835a8559a"
    caller.require_combined_app_inventory(inventory)


@pytest.mark.parametrize("fault", ["missing", "extra", "cb5_settings", "unchanged_byte",
    "missing_L11", "new_helper", "missing_display"])
def test_component_refuses_any_nonliteral_union_or_omitted_dependency(fault):
    inventory = combined_inventory()
    if fault == "missing":
        inventory.pop("src/project_mai_tai/__init__.py")
    elif fault == "extra":
        inventory["src/project_mai_tai/unreviewed.py"] = "a" * 64
    elif fault == "cb5_settings":
        inventory["src/project_mai_tai/settings.py"] = "99b3640610cc81b7be3d6ea3a0fadc9813f7064a052ebe00ac04aae38eb5b0d6"
    elif fault == "unchanged_byte":
        inventory["src/project_mai_tai/brokerdecides1.py"] = "a" * 64
    elif fault == "missing_L11":
        inventory.pop("src/project_mai_tai/services/keepwatch_bar_owner.py")
    elif fault == "new_helper":
        inventory["src/project_mai_tai/services/keepwatch_startup_veto.py"] = "a" * 64
    else:
        inventory.pop("src/project_mai_tai/atr_simulation/realized_display.py")
    with pytest.raises(ValueError, match="exact_06936_full_APP"):
        caller.require_combined_app_inventory(inventory)


def test_schema_review_must_also_name_the_exact_consumer_component(monkeypatch):
    value, _, _, _ = controlled(monkeypatch)
    value["graph_projection"]["app_component"]["head"] = "cb5de6e7659246118647de89d32fd39069028dce"
    with pytest.raises(ValueError):
        schema.validate_c1_review(value)


def test_historical_V10_FORBIDDEN_is_not_rewritten_by_combined_profile():
    assert "src/project_mai_tai/services/keepwatch_bar_owner.py" in caller.v10.FORBIDDEN
    assert "src/project_mai_tai/services/keepwatch_startup_veto.py" in caller.v10.FORBIDDEN
    assert "src/project_mai_tai/oms/broker_sync_generation.py" in caller.v10.FORBIDDEN
    assert "src/project_mai_tai/services/keepwatch_startup_veto.py" not in schema.COMBINED_APP_PINS
