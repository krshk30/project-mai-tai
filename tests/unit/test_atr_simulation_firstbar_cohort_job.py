"""New actual assembler/canonical-type job controls; all evidence is CONTROLLED.

No fake permission or execution callback. SQLite resource DI below is solely a
private storage control, not a PG/service/native or 173-seed acceptance claim.
"""

from copy import deepcopy
import hashlib
import json
from types import SimpleNamespace

import pytest
import sqlalchemy as sa

from project_mai_tai.atr_simulation.engine_binding import ENGINE_HEAD
from project_mai_tai.atr_simulation.cohort_assembler import timestamp_ns
from project_mai_tai.atr_simulation.firstbar_binding import FIRSTBAR_ENGINE_HEAD, FIRSTBAR_RESULT_VERSION
from project_mai_tai.atr_simulation.firstbar_cohort_job import (
    BAR_VERSION, CUTOFF_VERSION, JOB_VERSION, PACKET_VERSION, EvidenceFiles,
    load_firstbar_job, prepare_firstbar_job,
)
from project_mai_tai.atr_simulation.firstbar_replay import FirstBarReplayInput
from project_mai_tai.atr_simulation.models import inputs, metadata, rows as result_rows
from project_mai_tai.atr_simulation.repository import digest
from project_mai_tai.services import atr_simulation_firstbar_worker as worker


# Independently typed three-packet JSON fixture; not computed by production digest.
CONTROLLED_RAW_PACKETS_SHA256 = "03be313154bad525f11d7649c219801d7fa4e2a8258fa98feb67b483c54359d7"


def _put(root, name, value, asof):
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    (root / name).write_bytes(raw)
    return {"artifact": name, "sha256": hashlib.sha256(raw).hexdigest(),
            "pointer": "$", "kind": "CONTROLLED", "asof_ns": asof}


def _source_tree(value, source):
    if isinstance(value, dict):
        if {"artifact", "sha256", "pointer", "kind"} <= value.keys():
            return deepcopy(source)
        return {key: _source_tree(item, source) for key, item in value.items()}
    if isinstance(value, list):
        return [_source_tree(item, source) for item in value]
    return value


def _fixture():
    start = timestamp_ns("2026-10-22T11:02:00Z")
    account_id, strategy_id = "11111111-1111-4111-8111-111111111111", "22222222-2222-4222-8222-222222222222"
    account_key = "account_identity:" + account_id + ":" + strategy_id
    reference = {"artifact": "CONTROLLED", "sha256": "1" * 64, "pointer": "$",
                 "kind": "CONTROLLED", "asof_ns": start}
    coverage = {"start_ns": start, "end_ns": start + 180_000_000_000,
                "complete": None, "gaps": [], "sources": [reference]}
    identity = {"trading_day": "2026-10-22", "symbol": "LNKS", "broker": "schwab",
                "account_id": account_id, "strategy": "schwab_1m_v2", "flip_id": "buy", "segment_id": "segment"}
    spec = {"identity": identity, "strategy_id": strategy_id, "account_identity_packet_key": account_key,
        "account_name": "live:test", "buy_flip": {"available_ns": start + 30_000_000_000, "source": reference},
        "sell_flip": None, "session_end": {"available_ns": start + 120_000_000_000, "source": reference},
        "watched": True, "trigger": "1.53", "quantity": 10, "trigger_source": reference,
        "sizing_source": reference, "market_provider": "massive", "live_bought": None,
        "coverage": {name: deepcopy(coverage) for name in ("tape", "flip", "today", "live")}}
    trade = {"key": "trade:controlled", "kind": "trade", "row": {"id": "controlled", "symbol": "LNKS",
        "provider": "massive", "event_ts": "2026-10-22T11:02:01Z", "received_at": "2026-10-22T11:02:05Z",
        "price": "1.50", "size": "0"}}
    physical = {"key": account_key, "kind": "account_identity", "row": {
        "account_id": account_id, "strategy_id": strategy_id, "account_name": "live:test",
        "broker": "schwab", "strategy_code": "schwab_1m_v2"}}
    plan = {"version": "ATRSIM1_CAUSAL_COHORT_V2", "source_id": "CONTROLLED", "policy_version": "CONTROLLED",
        "accounts": [{"broker": "schwab", "account_id": account_id, "strategy": "schwab_1m_v2",
            "strategy_id": strategy_id, "account_name": "live:test", "account_identity_packet_key": account_key}],
        "episodes": [spec], "source_refs": [reference], "event_links": {trade["key"]: {
            "event_ns": start + 1_000_000_000, "sequence": 4, "raw_id": "controlled-provider-event", "source": reference}},
        "seal": {"all_overlapping_accounts_included": True, "sources": [reference],
                 "expected_episode_keys": [digest(identity)]}}
    return plan, [trade, physical]


def _job(root):
    plan, packets = _fixture()
    spec = plan["episodes"][0]
    start = spec["coverage"]["tape"]["start_ns"]
    observed = start + 240_000_000_000
    base_source = _put(root, "controlled-trace.json", {"CONTROLLED": "source operands"}, start)
    plan = _source_tree(plan, base_source)
    spec = plan["episodes"][0]
    spec["buy_flip"].update(available_ns=start + 60_000_000_000, bar_start_ns=start)
    plan.update(engine_dependency_head=ENGINE_HEAD, policy_version=FIRSTBAR_RESULT_VERSION,
                source_id="CONTROLLED-firstbar-job")
    packets[0]["row"].update(event_ts="2026-10-22T11:03:01Z", received_at="2026-10-22T11:03:05Z",
                              conditions=None)
    plan["event_links"][packets[0]["key"]].update(event_ns=start + 61_000_000_000)
    bar = {"key": "bar:controlled-bar", "kind": "bar", "row": {
        "id": "controlled-bar", "symbol": "LNKS", "strategy_code": "schwab_1m_v2", "interval_secs": 60,
        "bar_time": "2026-10-22T11:02:00Z", "created_at": "2026-10-22T11:03:00Z",
        "high_price": "2.00", "close_price": "1.99", "source": "CONTROLLED",
        "indicators": {"atr_trail": "1.98", "atr_state": "long"}}}
    packets.append(bar)
    publication_trace = _put(root, "controlled-publication-trace.json",
        {"CONTROLLED": "publication witness", "available_ns": start + 60_000_000_000}, start + 60_000_000_000)
    publication = {"version": BAR_VERSION, "basis": "RECORDED_PUBLICATION",
        "identity": spec["identity"], "packet_key": bar["key"], "packet_sha256": digest(bar),
        "available_ns": start + 60_000_000_000, "trace_sources": [publication_trace],
        "complete": True, "gaps": []}
    publication_ref = _put(root, "controlled-publication.json", publication, start + 60_000_000_000)
    cutoff = {"version": CUTOFF_VERSION, "basis": "DECLARED_SIM_RULE", "identity": spec["identity"],
              "available_ns": spec["session_end"]["available_ns"]}
    cutoff_ref = _put(root, "controlled-cutoff.json", cutoff, observed)
    packet_ref = _put(root, "packets.json", {"version": PACKET_VERSION, "packets": packets}, observed)
    body = {"version": JOB_VERSION, "engine_dependency_head": FIRSTBAR_ENGINE_HEAD,
        "population_label": "replay, in-sample", "evidence_cutoff_ns": observed, "plan": plan,
        "packet_files": [packet_ref], "proof_files": [], "flip_bar_bindings": [{
            "identity": spec["identity"], "bar_packet_key": bar["key"], "bar_packet_sha256": digest(bar),
            "publication_source": publication_ref, "entry_cutoff_source": cutoff_ref}],
        "expected_raw_packet_sha256": digest(packets)}
    return body, packets, observed


def _load(root, body):
    source = _put(root, "job.json", body, body["evidence_cutoff_ns"])
    return load_firstbar_job(root / "job.json", source["sha256"])


def _args(root, body, output, *, execute=False):
    job = _load(root, body)
    return SimpleNamespace(job_file=root / "job.json", job_sha256=job.sha256,
        evidence_root=root, output=output, execute=execute, sim_dsn_file=None, sim_role=None)


def test_actual_existing_assembler_constructs_canonical_inputs_preserving_zero_and_receive_clock(tmp_path):
    body, _packets, observed = _job(tmp_path)
    prepared = prepare_firstbar_job(_load(tmp_path, body), tmp_path, now_ns=observed)
    assert prepared.cohort is not None
    item = prepared.cohort.inputs[0]
    assert isinstance(item, FirstBarReplayInput)
    assert item.flip_bar.high == 2 and item.flip_bar.complete is True
    assert item.episode.watched is None  # A plan boolean is not watch-membership proof.
    assert item.episode.trades[0].size == 0
    assert item.episode.trades[0].key.received_ns - item.episode.trades[0].key.event_ns == 4_000_000_000
    assert prepared.diagnostics["raw_packet_sha256"] == CONTROLLED_RAW_PACKETS_SHA256
    assert prepared.diagnostics["engine_execution"] == "UNRUN"
    publication = prepared.diagnostics["publication"]
    assert publication["source_id"] == body["plan"]["source_id"]
    assert publication["trading_day_start"] == publication["trading_day_end"] == "2026-10-22"
    assert publication["publication_state"] == "UNPUBLISHED" and publication["reference_seed_binding"] is None


@pytest.mark.parametrize("defect", ["missing_binding", "missing_asof", "future_asof", "missing_file"])
def test_missing_or_future_publication_never_produces_partial_publishable_cohort(tmp_path, defect):
    body, _, observed = _job(tmp_path)
    binding = body["flip_bar_bindings"][0]
    if defect == "missing_binding":
        body["flip_bar_bindings"] = []
    elif defect == "missing_asof":
        binding["publication_source"]["asof_ns"] = None
    elif defect == "future_asof":
        binding["publication_source"]["asof_ns"] = observed + 1
    else:
        (tmp_path / binding["publication_source"]["artifact"]).unlink()
    prepared = prepare_firstbar_job(_load(tmp_path, body), tmp_path, now_ns=observed)
    assert prepared.cohort is None
    assert len(prepared.diagnostics["coverage"]["input_issues"]) == 1
    assert prepared.diagnostics["engine_execution"] == "UNRUN"
    assert prepared.diagnostics["publication"]["publication_state"] == "UNPUBLISHED"


@pytest.mark.parametrize("defect", ["late_created_at", "empty_indicators"])
def test_upsert_timestamp_or_empty_indicators_cannot_certify_completed_bar(tmp_path, defect):
    body, packets, observed = _job(tmp_path)
    bar = packets[-1]
    if defect == "late_created_at":
        bar["row"]["created_at"] = "2026-10-22T11:05:00Z"
    else:
        bar["row"]["indicators"] = {}
    binding = body["flip_bar_bindings"][0]
    binding["bar_packet_sha256"] = digest(bar)
    proof = json.loads((tmp_path / "controlled-publication.json").read_text())
    proof["packet_sha256"] = digest(bar)
    binding["publication_source"] = _put(tmp_path, "controlled-publication.json", proof, proof["available_ns"])
    body["packet_files"] = [_put(tmp_path, "packets.json", {"version": PACKET_VERSION, "packets": packets}, observed)]
    body["expected_raw_packet_sha256"] = digest(packets)
    prepared = prepare_firstbar_job(_load(tmp_path, body), tmp_path, now_ns=observed)
    assert prepared.cohort is not None
    assert prepared.cohort.inputs[0].flip_bar.complete is None
    assert prepared.diagnostics["coverage"]["original_bar_asof"] == "UNKNOWN"


def test_bar_proof_for_another_actual_packet_is_rejected_not_relabelled(tmp_path):
    body, _, observed = _job(tmp_path)
    body["flip_bar_bindings"][0]["bar_packet_sha256"] = "f" * 64
    with pytest.raises(ValueError, match="completed_packet_binding_mismatch"):
        prepare_firstbar_job(_load(tmp_path, body), tmp_path, now_ns=observed)


def test_extra_missing_account_in_population_seal_stays_unpublishable(tmp_path):
    body, _, observed = _job(tmp_path)
    body["plan"]["seal"]["expected_episode_keys"].append("f" * 64)
    prepared = prepare_firstbar_job(_load(tmp_path, body), tmp_path, now_ns=observed)
    assert prepared.cohort is None
    assert prepared.diagnostics["all_overlapping_accounts_included"] is False


def test_cutoff_rule_keeps_actual_later_asof_without_backdating_to_historical_session(tmp_path):
    body, _, observed = _job(tmp_path)
    prepared = prepare_firstbar_job(_load(tmp_path, body), tmp_path, now_ns=observed)
    boundary = prepared.cohort.inputs[0].entry_cutoff
    assert boundary.source.asof_ns == observed > boundary.available_ns
    assert prepared.diagnostics["coverage"]["native_asof"] == "UNKNOWN"


def test_caller_cannot_move_cutoff_earlier_to_skip_an_unwanted_trade(tmp_path):
    body, _, observed = _job(tmp_path)
    cutoff = json.loads((tmp_path / "controlled-cutoff.json").read_text())
    cutoff["available_ns"] -= 1
    body["flip_bar_bindings"][0]["entry_cutoff_source"] = _put(tmp_path, "controlled-cutoff.json", cutoff, observed)
    with pytest.raises(ValueError, match="cutoff_not_declared_1545_or_session_end"):
        prepare_firstbar_job(_load(tmp_path, body), tmp_path, now_ns=observed)


def test_evidence_outside_root_or_changed_bytes_cannot_be_accepted(tmp_path):
    root = tmp_path / "inside"
    root.mkdir()
    ref = _put(tmp_path, "outside.json", {"CONTROLLED": True}, 1)
    ref["artifact"] = "../outside.json"
    with pytest.raises(ValueError, match="path_outside_root"):
        EvidenceFiles(root).read(ref)
    inside = _put(root, "inside.json", {"CONTROLLED": True}, 1)
    (root / "inside.json").write_text("changed")
    with pytest.raises(ValueError, match="hash_mismatch"):
        EvidenceFiles(root).read(inside)


def test_duplicate_json_key_is_not_last_value_wins(tmp_path):
    raw = b'{"version":"first","version":"second"}'
    path = tmp_path / "job.json"
    path.write_bytes(raw)
    job = load_firstbar_job(path, hashlib.sha256(raw).hexdigest())
    with pytest.raises(ValueError, match="duplicate_firstbar_job_json_key"):
        prepare_firstbar_job(job, tmp_path)


def test_prepare_only_worker_uses_actual_codec_but_never_opens_a_database(tmp_path, monkeypatch):
    body, _, _ = _job(tmp_path)
    args = _args(tmp_path, body, tmp_path / "prepare-receipt.json")

    def forbidden(*_args, **_kwargs):
        raise AssertionError("prepare must not touch DB")

    monkeypatch.setattr(worker, "_sim_engine", forbidden)
    receipt = worker.run_job(args)
    assert receipt["engine_execution"] == "UNRUN"
    assert receipt["publication"]["publication_state"] == "UNPUBLISHED"
    assert receipt["store_engine_created"] is False
    assert (args.output.stat().st_mode & 0o777) == 0o600


def test_wrong_execute_packet_pin_refuses_before_any_db_or_model_run(tmp_path, monkeypatch):
    body, _, _ = _job(tmp_path)
    body["expected_raw_packet_sha256"] = "f" * 64
    args = _args(tmp_path, body, tmp_path / "error-receipt.json", execute=True)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("wrong input pin must not reach DB")

    monkeypatch.setattr(worker, "_sim_engine", forbidden)
    with pytest.raises(ValueError, match="exact_packet_population_pin"):
        worker.run_job(args)
    receipt = json.loads(args.output.read_text())
    assert receipt["engine_execution"] == "UNRUN" and receipt["automatic_retry"] is False
    assert receipt["publication"]["publication_state"] == "UNPUBLISHED"


def test_existing_raw_receipt_is_never_overwritten_even_before_invalid_job(tmp_path):
    output = tmp_path / "original.json"
    output.write_bytes(b"original raw failure")
    args = SimpleNamespace(output=output, job_file="missing", job_sha256="bad", execute=True)
    with pytest.raises(FileExistsError):
        worker.run_job(args)
    assert output.read_bytes() == b"original raw failure"


def test_canonical_worker_sqlite_publication_same_job_reentry_and_resource_cleanup(tmp_path, monkeypatch):
    body, _, _ = _job(tmp_path)
    database = tmp_path / "controlled-sim.sqlite"
    engine = sa.create_engine("sqlite:///" + str(database))
    metadata.create_all(engine)
    args = _args(tmp_path, body, tmp_path / "published-receipt.json", execute=True)
    args.sim_dsn_file, args.sim_role = "CONTROLLED-resource-DI", "CONTROLLED-role"
    monkeypatch.setattr(worker, "_sim_engine", lambda *_: engine)
    receipt = worker.run_job(args)
    assert receipt["status"] == "SIMULATION_PUBLISHED" and receipt["engine_execution"] == "simulate_many_once"
    assert receipt["publication"]["publication_state"] == "COMMITTED_OR_IDENTICAL_SOURCE"
    assert receipt["publication"]["reference_seed_population_count"] is None
    assert receipt["store_engine_disposed"] is True
    with engine.connect() as connection:
        stored = connection.execute(sa.select(result_rows.c.payload)).scalars().all()
        inbox = connection.execute(sa.select(inputs.c.stream_id, inputs.c.fields)).all()
    try:
        assert len(stored) == 2
        assert all(row["outcome_kind"] == "UNKNOWN" and not row["fills"] and row["gross_pnl"] is None for row in stored)
        assert all(len(item.stream_id) == 64 for item in inbox)
        assert any(len(json.loads(item.fields["data"])["key"]) > 64 for item in inbox)
        assert receipt["publication"]["known_page_outcomes"] == {
            "FIRSTBAR": {"TRADE": 0, "SKIP": 0, "UNKNOWN": 1},
            "FIRSTBAR_X3": {"TRADE": 0, "SKIP": 0, "UNKNOWN": 1}}
    finally:
        engine.dispose()
    args.output = tmp_path / "same-job-new-receipt.json"
    repeated = worker.run_job(args)
    assert repeated["publication"]["publication_state"] == "COMMITTED_OR_IDENTICAL_SOURCE"
    assert repeated["store_engine_disposed"] is True
    try:
        with engine.connect() as connection:
            assert connection.execute(sa.select(sa.func.count()).select_from(result_rows)).scalar_one() == 2
            assert connection.execute(sa.select(sa.func.count()).select_from(inputs)).scalar_one() == len(inbox)
    finally:
        engine.dispose()


@pytest.mark.parametrize("operand", [True, False, {}, {"root": True}, {"root": "x", "descriptor": True}])
def test_seed_permission_boolean_or_descriptor_cannot_replace_actual_files(tmp_path, operand):
    body, _, observed = _job(tmp_path)
    body["reference_seed"] = operand
    with pytest.raises(ValueError, match="reference_seed_root_required"):
        prepare_firstbar_job(_load(tmp_path, body), tmp_path, now_ns=observed)


def test_job_cannot_publish_an_http_supplied_seed_binding(tmp_path):
    body, _, observed = _job(tmp_path)
    body["reference_seed_binding"] = {"selected_signals": 173}
    with pytest.raises(ValueError, match="descriptor_must_be_derived"):
        prepare_firstbar_job(_load(tmp_path, body), tmp_path, now_ns=observed)


def test_seed_files_with_wrong_reviewed_bytes_cannot_authorize_month_reader(tmp_path):
    body, _, observed = _job(tmp_path)
    seed = tmp_path / "seed"
    (seed / "four").mkdir(parents=True)
    (seed / "four_columns.csv").write_bytes(b"CONTROLLED wrong reference bytes")
    body["reference_seed"] = {"root": "seed"}
    with pytest.raises(ValueError, match="evidence_hash_mismatch"):
        prepare_firstbar_job(_load(tmp_path, body), tmp_path, now_ns=observed)


def test_store_constructor_failure_is_redacted_and_leaves_unpublished_receipt(tmp_path, monkeypatch):
    body, _, _ = _job(tmp_path)
    args = _args(tmp_path, body, tmp_path / "constructor-failure.json", execute=True)
    args.sim_dsn_file, args.sim_role = "CONTROLLED-resource-DI", "CONTROLLED-role"
    def fail(*_args):
        raise RuntimeError("CONTROLLED_SECRET_DSN_MUST_NOT_APPEAR")
    monkeypatch.setattr(worker, "_sim_engine", fail)
    with pytest.raises(RuntimeError):
        worker.run_job(args)
    raw = args.output.read_text()
    assert "CONTROLLED_SECRET_DSN_MUST_NOT_APPEAR" not in raw
    receipt = json.loads(raw)
    assert receipt["phase"] == "OPEN_SIM_STORE"
    assert receipt["publication"]["publication_state"] == "UNPUBLISHED"
    assert receipt["engine_execution"] == "UNRUN"
    assert receipt["automatic_retry"] is False


def test_exact_seed_label_without_actual_seed_files_is_not_a_permission_flag(tmp_path):
    body, _, observed = _job(tmp_path)
    body["population_label"] = "replay, in-sample; underlying capture completeness UNKNOWN"
    with pytest.raises(ValueError, match="seed_label_requires_actual_seed_files"):
        prepare_firstbar_job(_load(tmp_path, body), tmp_path, now_ns=observed)


def test_seed_dependency_pins_bind_exact_reviewed_reference_and_native_json_reader():
    from project_mai_tai.atr_simulation import firstbar_cohort_job as job_module

    assert job_module.SEED_PINS == {
        "firstbar_reference.py": "b984c5268f5d2f4be0d00302573650251e3989edbe67935e7388d71bc91d3ae4",
        "firstbar_ui_reader.py": "927a7e77b8512c5bb2fc93cb8efd29ef900d05c6a58bf61f8b825a5d2e3d01b1",
    }


@pytest.mark.parametrize("name", ["firstbar_reference.py", "firstbar_ui_reader.py"])
def test_changed_seed_dependency_source_still_refuses_before_reference_files(
        tmp_path, monkeypatch, name):
    from pathlib import Path
    from project_mai_tai.atr_simulation import firstbar_cohort_job as job_module

    body, _, observed = _job(tmp_path)
    seed = tmp_path / "seed"
    seed.mkdir()
    body["reference_seed"] = {"root": "seed"}
    loaded = _load(tmp_path, body)
    expected = Path(job_module.__file__).resolve().parent / name
    original = Path.read_bytes

    def changed_source(path):
        if path.resolve() == expected:
            return b"CONTROLLED altered dependency source"
        return original(path)

    monkeypatch.setattr(Path, "read_bytes", changed_source)
    with pytest.raises(ValueError, match="firstbar_reference_source_pin_mismatch:" + name):
        prepare_firstbar_job(loaded, tmp_path, now_ns=observed)


def test_seed_dependency_foreign_import_root_still_refuses(tmp_path, monkeypatch):
    from project_mai_tai.atr_simulation import firstbar_ui_reader as seed_module

    body, _, observed = _job(tmp_path)
    (tmp_path / "seed").mkdir()
    body["reference_seed"] = {"root": "seed"}
    loaded = _load(tmp_path, body)
    monkeypatch.setattr(seed_module, "__file__", str(tmp_path / "firstbar_ui_reader.py"))
    with pytest.raises(ValueError,
            match="firstbar_reference_import_root_changed:firstbar_ui_reader"):
        prepare_firstbar_job(loaded, tmp_path, now_ns=observed)
