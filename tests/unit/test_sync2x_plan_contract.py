"""Pure proof-runner controls, authored only; not actual PostgreSQL proof."""

import pytest

from ops.sync2x_plan import BOX_ESTIMATES, EVIDENCE_SHA, acceptable_plan, qualification_url, validate_manifest


@pytest.mark.parametrize("cost,milliseconds,functions,accepted", [
    (99999, 40, 0, True), (100000, 1, 0, False), (1, 40.001, 0, False), (1, 1, 1, False),
])
def test_no_cost_time_or_jit_waiver(cost, milliseconds, functions, accepted):
    assert acceptable_plan({"Plan": {"Total Cost": cost}, "Execution Time": milliseconds,
                            "JIT": {"Functions": functions}}) is accepted


@pytest.mark.parametrize("defect", ["absent_review", "small_dataset", "wrong_evidence", "mixed_empty"])
def test_manifest_requires_reviewed_box_scale_and_independent_expected_ids(defect):
    manifest = {"database": "qualification_sync2x_control", "source_evidence_sha256": EVIDENCE_SHA,
        "dataset_kind": "SYNTHETIC_NOT_PRODUCTION_COPY", "case": "idle", "account": "live:orb",
        "distribution_reviewed_by_main": True, "expected_ids": [], "expected_pg_major": 16,
        "row_counts": dict(BOX_ESTIMATES)}
    validate_manifest(manifest, manifest["database"])
    if defect == "absent_review":
        manifest["distribution_reviewed_by_main"] = False
    elif defect == "small_dataset":
        manifest["row_counts"]["broker_orders"] -= 1
    elif defect == "wrong_evidence":
        manifest["source_evidence_sha256"] = "not-the-box-artifact"
    else:
        manifest["case"] = "mixed"
    with pytest.raises(ValueError):
        validate_manifest(manifest, manifest["database"])


@pytest.mark.parametrize("raw,ack", [
    ("postgresql+psycopg://killsw1_replay@localhost:51767/project_mai_tai?host=/private/tmp/lab", "project_mai_tai"),
    ("postgresql+psycopg://killsw1_replay@remote:51767/qualification_sync2x_x?host=/private/tmp/lab", "qualification_sync2x_x"),
    ("postgresql+psycopg://killsw1_replay@localhost:5432/qualification_sync2x_x?host=/private/tmp/lab", "qualification_sync2x_x"),
    ("postgresql+psycopg://killsw1_replay@localhost:51767/qualification_sync2x_x?host=/var/run/postgresql", "qualification_sync2x_x"),
    ("postgresql+psycopg://killsw1_replay@localhost:51767/qualification_sync2x_x?host=/private/tmp/lab", "wrong-ack"),
])
def test_qualification_refuses_app_remote_default_port_or_unbound_lab_without_connecting(monkeypatch, raw, ack):
    monkeypatch.setattr("ops.sync2x_plan.os.geteuid", lambda: 1000)
    with pytest.raises(ValueError):
        qualification_url(raw, ack)
