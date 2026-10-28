"""CONTROLLED binding seams; real chunk validation is NOT exercised by these mocks."""

from copy import deepcopy
from hashlib import sha256

import pytest

from tests.unit.test_l12_current359_consumer_carry import operands
from tools import l12_scoped_chunks_v2 as chunks
from tools.l12_scoped_chunks_v2 import canonical
from tools.l12_service_boot_replay import (
    C11929_CONTROLLED_VERSION, _bind_v2_boot_inputs, prepare_c11929_controlled_native,
)


def inputs(monkeypatch):
    approval, pins = operands()
    accounts = {"schwab": "live:schwab_1m_v2", "webull": "live:orb"}
    boot = 1793001600000
    model = prepare_c11929_controlled_native(accounts=accounts, modeled_boot_ms=boot, variant="EMPTY_FLAT")
    plan = dict(manifest_sha256="b" * 64, schema_sha256="d" * 64,
        snapshot_id="CONTROLLED_NOT_AN_OBSERVED_SNAPSHOT", asof_ms=1792880000000,
        tables={name: dict(source_state="ABSENT_CANDIDATE_PROJECTION") for name in
            ("brokerdecides_dispatch_retirements", "brokerdecides_dispatch_evidence")})
    monkeypatch.setattr(chunks, "validate_v2", lambda *a, **k: deepcopy(plan))
    packet = dict(version=2, purpose=C11929_CONTROLLED_VERSION, capture_head="c" * 40,
        snapshot_id=plan["snapshot_id"], asof_ms=plan["asof_ms"], source_schema_sha256=plan["schema_sha256"],
        execution_root="f" * 40, accounts=accounts, modeled_boot_ms=boot, variant="EMPTY_FLAT",
        working_provider=None, working_symbol=None, publisher_epoch=model["publisher_epoch"],
        artifacts=model["artifacts"])
    review = dict(role_review=dict(manifest_raw=b"CONTROLLED_NOT_ACTUAL_MANIFEST",
        metadata_raw=b"CONTROLLED_NOT_ACTUAL_METADATA", chunk_review=dict(expected_capture_head="c" * 40),
        execution_approval=approval, observed_execution_root="f" * 40,
        observed_component_pins=pins, reviewed_boot_ms=boot),
        controlled_native_review=dict(version=C11929_CONTROLLED_VERSION, c1_ruling_sha256="1" * 64,
            review_sha256="2" * 64, manifest_sha256=plan["manifest_sha256"],
            controlled_artifacts_sha256=sha256(canonical(model["artifacts"])).hexdigest(),
            case_factory_sha256="3" * 64,
            local_line_mode="REAL_RESTORED_STORE_LOADERS_AND_RECORDED_CALLBACKS_NOT_EXPORTED_PREFIX"))
    return packet, model["blobs"], review


def test_explicit_controlled_mode_binds_all_roles_but_does_not_mint_native_or_line_proof(monkeypatch):
    packet, blobs, review = inputs(monkeypatch)
    stage, callback = _bind_v2_boot_inputs(packet, blobs, review)
    assert stage["status"] == "V2_PRIVATE_BOOT_INPUTS_BOUND_NOT_STARTED"
    assert stage["native_evidence_mode"] == C11929_CONTROLLED_VERSION
    assert stage["permission_granted"] is False
    assert len(stage["roles"]) == 15
    assert stage["line_raw_sha256"] is None
    assert stage["line_coverage_supplied"] is False
    assert stage["future_boot_freshness"] == "UNKNOWN_NO_NATIVE_CLOCK_CLAIM"
    assert callable(callback)


@pytest.mark.parametrize("fault", ["real_complete", "http200", "wire_clock", "origin", "role_omitted"])
def test_controlled_input_cannot_be_relabeled_real_complete_or_wire_fresh(monkeypatch, fault):
    import json
    packet, blobs, review = inputs(monkeypatch)
    role = "schwab_working_orders"
    item = packet["artifacts"][role]
    if fault == "origin":
        item["origin"] = "CAPTURED_BYTES"
    elif fault == "role_omitted":
        del packet["artifacts"][role]
    else:
        body = json.loads(blobs[item["reference"]])
        if fault == "real_complete":
            body["complete"] = True
        elif fault == "http200":
            body["http_status"] = 200
        else:
            body["request_started_ms"] = packet["modeled_boot_ms"]
        raw = canonical(body)
        blobs[item["reference"]] = raw
        item["sha256"] = sha256(raw).hexdigest()
    review["controlled_native_review"]["controlled_artifacts_sha256"] = sha256(canonical(packet["artifacts"])).hexdigest()
    with pytest.raises(ValueError, match="controlled_fixture_bytes_or_explicit_origin"):
        _bind_v2_boot_inputs(packet, blobs, review)


@pytest.mark.parametrize("fault", ["zero_ruling", "zero_review", "extra_acceptance", "changed_snapshot", "changed_model_clock"])
def test_no_unreviewed_claim_or_copy_clock_substitution(monkeypatch, fault):
    packet, blobs, review = inputs(monkeypatch)
    if fault == "zero_ruling":
        review["controlled_native_review"]["c1_ruling_sha256"] = "0" * 64
    elif fault == "zero_review":
        review["controlled_native_review"]["review_sha256"] = "0" * 64
    elif fault == "extra_acceptance":
        review["controlled_native_review"]["GO"] = True
    elif fault == "changed_snapshot":
        packet["snapshot_id"] = "OTHER"
    else:
        packet["modeled_boot_ms"] += 1
    with pytest.raises(ValueError):
        _bind_v2_boot_inputs(packet, blobs, review)
