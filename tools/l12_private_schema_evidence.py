"""Private catalogs bound to reviewed DDL or the exact accepted native graph.

No schema creation or observed-digest approval. Graph mode first proves the
whole accepted graph, then seals same-snapshot reflection only as a drift fence.
"""

from hashlib import sha256
from dataclasses import dataclass, field
import re

from sqlalchemy import CheckConstraint, MetaData, Table, UniqueConstraint, bindparam, inspect, text
from sqlalchemy.schema import CreateIndex, CreateTable

from tools.l12_close_asof_contract import is_sha
from tools.l12_scoped_chunks_v2 import canonical

C1_NATIVE_VERSION = "L12_C1_NATIVE_RESTORE_SCHEMA_V1"
C1_GRAPH_VERSION = "L12_C1_NATIVE_RESTORE_SCHEMA_V2_GRAPH_BOUND"
GRAPH_SOURCE_PINS = {
    "tools/l12_native_schema_graph.py": "5c618ca470335b839797011251bfbab44f9e294c5f742c4e3fa8a3d8617100ec",
    "tools/l12_native_graph_adapter.py": "f09a1e4495e2fad3c86b3357344b6e1c4736eaf05967d74365c0ac6fcfca7f23",
    "tools/l12_whole_schema_semantics.py": "c6600d8a2ec4b3dccfda398d484de5d8e5263d5959ef5ee3ef8e47598bcde3ef",
}
GRAPH_PROJECTION = "WHOLE_ACCEPTED_GRAPH_THEN_SAME_RR_REFLECTION_DRIFT_FENCE_V1"
COMBINED_APP_PINS = {
    "src/project_mai_tai/atr_simulation/accepted_aggregate_bootstrap.py": "9a4415d28ff2e3961b146b65428ce0f6fa49b192080160a59584df113a1ad85b",
    "src/project_mai_tai/atr_simulation/realized_display.py": "986888647065f3e85773f1ffe20bbb650b8701efdb13938ba99b77de2da9cb47",
    "src/project_mai_tai/market_data/keepwatch_history.py": "f3b06f4df429caca72faa9158fe4840df697eecd26c900f05d82a90807aa9c82",
    "src/project_mai_tai/market_data/schwab_v2_rest_client.py": "1724f768229cfe0c801654b495592affa988d25c367194863fbb4b47b92aca25",
    "src/project_mai_tai/services/keepwatch_bar_owner.py": "a5678ceb0f4a0e7ac7b93a728346317d50fa830779e774b166073d329edf793e",
    "src/project_mai_tai/services/atr_aggregate_routes.py": "6071f616abdb36b846bd3aec303d5442f5dedf411d46325767a43fa1c4832d18",
    "src/project_mai_tai/services/control_plane.py": "8ee169989749c719e7d6dbb8cad355cd3e4a17b147c70f1fee860c2789c2d5bf",
    "src/project_mai_tai/services/keepwatch_halt_proof.py": "f0fb0ddfbcc52135509b11216d81ef01bf96fff78c60dcae3a14548e948286c9",
    "src/project_mai_tai/services/schwab_1m_v2_bot.py": "d713c1332cc9b705a51916491de738f3f9c410316e04a5c7d77186462aea85a2",
    "src/project_mai_tai/settings.py": "bf9a77551eb7805886e1c73ed3e3a74b474841e3a9f1c4e8de5cbaf835a8559a",
    "src/project_mai_tai/strategy_core/keepwatch.py": "1444fa727fcf8496420f13c49018f9595d92c4570cd94bfe839c012dbe066ac1",
    "src/project_mai_tai/strategy_core/schwab_1m_v2.py": "c70af058b13a6928d48899ad9ad96d20e0ab66fdd68f3e14267913a4f93c494c",
    "src/project_mai_tai/strategy_core/session_line_restore.py": "5e90856bee5194ae3a8b33a7d09b69167ad4722e9010f3ebc30d842fb2e1d0cc",
}
COMBINED_APP_COMPONENT = dict(version="EXACT_06936_THIRTEEN_FILE_APP_COMPONENT_V1",
    head="06936d2ecc9469664bddbcf605896272afd00b25",
    lineage_head="cb5de6e7659246118647de89d32fd39069028dce",
    baseline_head="2aebc0579331e0e1f6de8c2b6e63bd6e243d41a5", files=333,
    app_inventory_sha256="eec18a105278def661e58879ed53e9559ac59443580864c3d4ef12da8fc6a5c9",
    changed_pins=COMBINED_APP_PINS)
CURRENT_359_APP_DELTA = {
    "src/project_mai_tai/atr_simulation/realized_display.py": [
        "986888647065f3e85773f1ffe20bbb650b8701efdb13938ba99b77de2da9cb47", "071ce216ec6ca493042ff82d1448662634226e0fb6fd4e937779684e752b3ebc"],
    "src/project_mai_tai/market_data/keepwatch_history.py": [
        "f3b06f4df429caca72faa9158fe4840df697eecd26c900f05d82a90807aa9c82", "ac409fcde484dcade865576e1c8ba431222f7dd30d402d346a1328aa095ad724"],
    "src/project_mai_tai/market_data/keepwatch_producer.py": [
        "ABSENT", "6fb5bda0f1e0258cc05b85a50b40a003c74a378afbd0fa292d7f74e592af06f5"],
    "src/project_mai_tai/market_data/massive_atr_seed.py": [
        "b813af1a254b95548891599c81e8f1439430f238181e93c036f8ff13df31e096", "3860a52c781656fa6f47b24b10e74c9c9cf7f31c41dcfae8d4ab7ac7bcb82466"],
    "src/project_mai_tai/market_data/schwab_v2_rest_client.py": [
        "1724f768229cfe0c801654b495592affa988d25c367194863fbb4b47b92aca25", "57c02d052e4a1afa121425b7c511afabfb0293d8ea572a22f6a1463fba89038c"],
    "src/project_mai_tai/market_data/schwab_v2_streamer.py": [
        "4ebda315861e7bd490dec323332ac41d5f7fa3a47cac841074b6b7c53c4e262c", "a158a86f99f89ddf3dd3dde4eba70ecae81b63393d9895d75cdd4b4639ebaa9d"],
    "src/project_mai_tai/services/keepwatch_bar_owner.py": [
        "a5678ceb0f4a0e7ac7b93a728346317d50fa830779e774b166073d329edf793e", "4385d9b8a353140af4b0a61619fb1db4cf6265ceee3171893820b698e1fd3fc5"],
    "src/project_mai_tai/services/keepwatch_halt_proof.py": [
        "f0fb0ddfbcc52135509b11216d81ef01bf96fff78c60dcae3a14548e948286c9", "50d1ed12d0218e6558ce873a8e91b69151a0f64ea9343b6c2e3552660c95f04e"],
    "src/project_mai_tai/services/schwab_1m_v2_bot.py": [
        "d713c1332cc9b705a51916491de738f3f9c410316e04a5c7d77186462aea85a2", "6e7cb1d5dfeba487974c7710fce3ff58bf9715d41ced92636571e73504a0d8b0"],
    "src/project_mai_tai/strategy_core/schwab_1m_v2.py": [
        "c70af058b13a6928d48899ad9ad96d20e0ab66fdd68f3e14267913a4f93c494c", "23922ccab034b3f4744a3dc97ccd67edccc8a91134c823bb74a26104f10ad8e3"],
    "src/project_mai_tai/strategy_core/session_line_restore.py": [
        "5e90856bee5194ae3a8b33a7d09b69167ad4722e9010f3ebc30d842fb2e1d0cc", "3b953c9248d9b2dd46800bcece1c0191ab0556367265bffefe0bf590ec1ed62e"],
}
CURRENT_359_APP_COMPONENT = dict(version="EXACT_359B_ELEVEN_DELTA_APP_COMPONENT_V2",
    head="359beec1b5051f84d5b0495cd8e61c91b8a6c3b6", files=334,
    baseline_component_head=COMBINED_APP_COMPONENT["head"],
    inventory_rule="INVERSE_LITERAL_ELEVEN_DELTAS_THEN_EXACT_06936_FULL_APP_DIGEST",
    deltas=CURRENT_359_APP_DELTA)
CURRENT_F_APP_COMPONENT = dict(version="EXACT_F278_ONE_DISPLAY_DELTA_APP_COMPONENT_V3",
    head="2782fe6b1f8135703203682f4be633aa0c751768", files=334,
    baseline_component_head=CURRENT_359_APP_COMPONENT["head"],
    path="src/project_mai_tai/atr_simulation/realized_display.py",
    before_sha256="071ce216ec6ca493042ff82d1448662634226e0fb6fd4e937779684e752b3ebc",
    after_sha256="111946903cd81c4058032fd4deeccbc5ae0dc05f6492c63db29c3e605a4d67c0",
    inventory_rule="INVERSE_LITERAL_DISPLAY_DELTA_THEN_EXACT_359B_WHOLE_APP")
CORRECTED_L11_APP_COMPONENT = dict(version="EXACT_FF41_SHARED_COMPLETION_APP_COMPONENT_V4",
    head="ff41a0ffbeb8d34a6413913808d5cfa79dfe700f", files=334,
    baseline_component_head=CURRENT_F_APP_COMPONENT["head"],
    path="src/project_mai_tai/services/schwab_1m_v2_bot.py",
    before_sha256="6e7cb1d5dfeba487974c7710fce3ff58bf9715d41ced92636571e73504a0d8b0",
    after_sha256="fe66b4a40d3932653ee8d2cfddb91be953a2d7aa9150be2c7f25f2ebf24747d8",
    inventory_rule="INVERSE_LITERAL_SHARED_COMPLETION_SERVICE_THEN_EXACT_F_FULL_APP")
BOUNDED_L11_APP_COMPONENT = dict(version="EXACT_4F0150_BOUNDED_CALLBACK_APP_COMPONENT_V5",
    head="4f0150c188e92063ebfb160b6437a4ab5d432498", files=334,
    baseline_component_head=CURRENT_F_APP_COMPONENT["head"],
    path="src/project_mai_tai/services/schwab_1m_v2_bot.py",
    before_sha256="6e7cb1d5dfeba487974c7710fce3ff58bf9715d41ced92636571e73504a0d8b0",
    after_sha256="2ff17a6c7bd739e49c024b9cda81784c3aba0b3e2f8871ed4abc0c5de88fb575",
    inventory_rule="INVERSE_LITERAL_4F0150_SERVICE_THEN_EXACT_F_FULL_APP")
_PROJECTION_SEAL = object()
C1_BASELINE = "BASELINE_SOURCE21"
C1_UPGRADED = "UPGRADED_SOURCE21_PLUS2"
C1_DUMP_SHA256 = "0c279d4771c00a46be5d29c3fee39411d720c3d5479a7a8f5af52c34bcb90cbd"
C1_TABLES = frozenset("""_stuck_cancel_intents_bak_20260730 account_positions ai_trade_reviews
alembic_version audit_repair_o1019_15_20261019 bot_control_attempts bot_control_commands
bot_control_states broker_accounts broker_order_events broker_orders dashboard_snapshots fills
market_capture_bars market_capture_quotes market_capture_trades market_data_subscriptions
market_quote_ticks market_trade_ticks momentum_live_sec_country momentum_live_sec_request_clock
momentum_paper_events oms_armed_stops oms_managed_positions orb_paper_events paper_exit_events
paper_exit_rule_configs reconciliation_findings reconciliation_runs rej_live risk_checks
scanner_blacklist_entries scanner_confirmed_events schwab_ineligible_today service_heartbeats
strategies strategy_bar_history strategy_runs system_incidents trade_intents
v2_confirmation_exit_evaluations virtual_positions webull_ineligible_today""".split())
C1_NEW_TABLES = frozenset("""atr_simulation_inputs atr_simulation_consumers atr_simulation_runs
atr_simulation_rows brokerdecides_dispatch_retirements brokerdecides_dispatch_evidence""".split())
EXACT_UNIQUENESS = {
    "broker_accounts": {"index": "ix_broker_accounts_name", "constraint": "uq_broker_accounts_name", "columns": ["name"]},
    "strategies": {"index": "ix_strategies_code", "constraint": "uq_strategies_code", "columns": ["code"]},
}
NAMED_NATIVE_UNIQUES = {
    "bot_control_attempts": {"uq_bot_control_attempts_client_order_id": ["client_order_id"]},
    "bot_control_commands": {"uq_bot_control_commands_request_id": ["request_id"]},
    "broker_accounts": {"uq_broker_accounts_external_account_id": ["external_account_id"]},
    "broker_orders": {
        "uq_broker_orders_broker_order_id": ["broker_order_id"],
        "uq_broker_orders_client_order_id": ["client_order_id"],
    },
    "fills": {"uq_fills_broker_fill_id": ["broker_fill_id"]},
    "v2_confirmation_exit_evaluations": {
        "uq_v2_confirmation_exit_evaluations_fanout_slot_id": ["fanout_slot_id"],
        "uq_v2_confirmation_exit_evaluations_source_fill_id": ["source_fill_id"],
    },
}
ACCEPTED_NATIVE_EVIDENCE = {
    "c1_ruling": "b9a1771ff22dcb6b059df20fe6a937bc56051aa2ea3b3663a5b0f92fef010c00",
    "c1_dump": C1_DUMP_SHA256,
    "baseline_graph": "b3c0084e09f98417301e30cfa6f8aa4ccb3c7ff1b93994ab5b8beadbecba22e0",
    "upgraded_graph": "8fe10dd8626c3fab94b6bedf4377e8915211dbf9f976ea443a36778c4f1b4cf4",
    "ledger": "3a6194eaa1078c7a768869e8f319371df8ff0d003cc9985be5879b784ce6f67e",
    "comparison": "e37500815c48085f85a435962b0cc5cd934a564fcf84c508fc5ec82b8496a070",
    "source_carry": "19ceb15e49d998568904c1a499a5b97196d7d8916fc3e0517124e9e933d0b31a",
}
NATIVE_EVIDENCE = frozenset({"c1_ruling", "c1_dump", "baseline_graph", "upgraded_graph", "ledger", "comparison",
                            "real_receipt", "closure_receipt", "source_carry"})


def is_c1_review(review):
    return isinstance(review, dict) and review.get("version") in {C1_NATIVE_VERSION, C1_GRAPH_VERSION}


def validate_c1_review(review):
    fields = {"version", "purpose", "review_id", "dialect", "target_purpose", "canonical_catalog_sha256",
        "unique_equivalences", "named_native_uniques", "evidence", "construction_review", "execution_binding",
        "canonical_catalog_fingerprint_reviewed_against_accepted_native_graph"}
    graph_mode = isinstance(review, dict) and review.get("version") == C1_GRAPH_VERSION
    if graph_mode:
        fields = fields | {"graph_projection"}
    if (not isinstance(review, dict) or set(review) != fields
            or review["version"] not in {C1_NATIVE_VERSION, C1_GRAPH_VERSION}
            or review["purpose"] != "C1_NATIVE_BASELINE_PLUS_ACCEPTED_THREE_REVISION_LEDGER"
            or review["dialect"] != "postgresql" or not isinstance(review["review_id"], str) or not review["review_id"]
            or review["target_purpose"] not in {C1_BASELINE, C1_UPGRADED}
            or (not graph_mode and (not is_sha(review["canonical_catalog_sha256"])
                or review["canonical_catalog_sha256"] == "0" * 64))
            or (graph_mode and review["canonical_catalog_sha256"] is not None)
            or review["unique_equivalences"] != EXACT_UNIQUENESS
            or review["named_native_uniques"] != NAMED_NATIVE_UNIQUES
            or review["canonical_catalog_fingerprint_reviewed_against_accepted_native_graph"] is not (not graph_mode)
            or not isinstance(review["evidence"], dict) or set(review["evidence"]) != NATIVE_EVIDENCE):
        raise ValueError("explicit_C1_native_schema_profile_and_exact_uniqueness_mapping_required")
    for descriptor in review["evidence"].values():
        if (not isinstance(descriptor, dict) or set(descriptor) != {"path", "sha256"}
                or not isinstance(descriptor["path"], str) or not descriptor["path"].startswith("/")
                or not is_sha(descriptor["sha256"]) or descriptor["sha256"] == "0" * 64):
            raise ValueError("immutable_independently_reviewed_native_schema_evidence_required")
    if any(review["evidence"][key]["sha256"] != digest for key, digest in ACCEPTED_NATIVE_EVIDENCE.items()):
        raise ValueError("exact_C1_accepted_native_baseline_ledger_and_carry_required")
    if graph_mode and review["graph_projection"] != dict(version=GRAPH_PROJECTION,
            source_pins=GRAPH_SOURCE_PINS, owner_mapping={}, extra_exclusions=[],
            app_component=COMBINED_APP_COMPONENT,
            accepted_artifact_sha256=ACCEPTED_NATIVE_EVIDENCE[
                "baseline_graph" if review["target_purpose"] == C1_BASELINE else "upgraded_graph"]):
        raise ValueError("exact_accepted_whole_graph_projection_no_observed_digest_approval_required")
    construction = review["construction_review"]
    required = {"target_capability_sha256", "target_purpose", "creation_source_head", "real_receipt_sha256",
        "closure_receipt_sha256", "restore_commit_verified", "real_upgrade_verified",
        "cleanup_and_zero_clients_verified", "acceptance_review_sha256"}
    if (not isinstance(construction, dict) or set(construction) != required
            or construction["target_purpose"] != review["target_purpose"]
            or not isinstance(construction["creation_source_head"], str)
            or re.fullmatch("[0-9a-f]{40}", construction["creation_source_head"]) is None
            or any(not is_sha(construction[key]) or construction[key] == "0" * 64 for key in
                ("target_capability_sha256", "real_receipt_sha256", "closure_receipt_sha256", "acceptance_review_sha256"))
            or construction["real_receipt_sha256"] != review["evidence"]["real_receipt"]["sha256"]
            or construction["closure_receipt_sha256"] != review["evidence"]["closure_receipt"]["sha256"]
            or construction["restore_commit_verified"] is not True
            or construction["cleanup_and_zero_clients_verified"] is not True
            or construction["real_upgrade_verified"] is not (review["target_purpose"] == C1_UPGRADED)):
        raise ValueError("actual_C1_restore_REAL_or_baseline_receipt_and_cleanup_review_required")
    return review["target_purpose"]


@dataclass(frozen=True)
class AcceptedNativeReference:
    raw: bytes
    review_sha256: str


@dataclass(frozen=True)
class VerifiedNativeProjection:
    review_sha256: str
    accepted_artifact_sha256: str
    semantic_sha256: str
    reflected_sha256: str
    closure_receipt_sha256: str
    snapshot: str
    _seal: object = field(repr=False)

    def summary(self):
        return dict(version=GRAPH_PROJECTION, accepted_artifact_sha256=self.accepted_artifact_sha256,
            semantic_sha256=self.semantic_sha256, reflected_sha256=self.reflected_sha256,
            closure_receipt_sha256=self.closure_receipt_sha256, snapshot=self.snapshot,
            reflection_is_independent_approval=False, permission_granted=False)

    def sealed_values(self):
        return (self.review_sha256, self.accepted_artifact_sha256, self.semantic_sha256,
            self.reflected_sha256, self.closure_receipt_sha256, self.snapshot)


def accepted_native_reference(review, raw):
    """Only the literal accepted C61/FE1 bytes can be an expected graph."""
    purpose = validate_c1_review(review)
    if review["version"] != C1_GRAPH_VERSION:
        raise ValueError("C1_row_DML_requires_graph_bound_V2_not_old_reflected_approval")
    from tools import l12_whole_schema_semantics as semantic
    from tools import l12_native_graph_adapter as adapter
    key = "baseline_graph" if purpose == C1_BASELINE else "upgraded_graph"
    if type(raw) is not bytes or sha256(raw).hexdigest() != ACCEPTED_NATIVE_EVIDENCE[key]:
        raise ValueError("only_C1_accepted_graph_bytes_not_assessed_target_may_be_expected")
    artifact = semantic.decode(raw)
    graph, profile = artifact["graph"], artifact["profile"]
    if (artifact["source_pins"] != GRAPH_SOURCE_PINS
            or profile != adapter.source_profile(GRAPH_SOURCE_PINS, profile["review_sha256"])):
        raise ValueError("accepted_native_graph_profile_or_source_changed")
    role = "PRIVATE_BASELINE" if purpose == C1_BASELINE else "PRIVATE_UPGRADED"
    checked = semantic.Graph(semantic.canonical(graph), semantic.digest(graph), role, profile, {})
    checked.normalized()
    return AcceptedNativeReference(raw, sha256(canonical(review)).hexdigest())


def _bind_native_projection(review, reference, raw, reflected, closure_sha256):
    """Existing normalizer compares FULL schema/dependency multiset first.

    The reflection hash is only a drift fence after that independent match,
    never a caller-supplied expected schema digest or favorable field subset.
    """
    from tools import l12_whole_schema_semantics as semantic
    from tools import l12_native_graph_adapter as adapter
    if (type(reference) is not AcceptedNativeReference
            or reference.review_sha256 != sha256(canonical(review)).hexdigest()
            or not is_sha(closure_sha256) or closure_sha256 == "0" * 64):
        raise ValueError("actual_accepted_reference_review_and_RO_closure_required")
    accepted_native_reference(review, reference.raw)
    expected = semantic.decode(reference.raw)
    graph, profile = expected["graph"], expected["profile"]
    epoch = {key: graph["identity"][key] for key in (
        "database", "role", "system_identifier", "postmaster_started", "server_version_num")}
    observed = adapter.adapt(raw, sha256(raw).hexdigest(), source_pins=GRAPH_SOURCE_PINS,
        profile_review_sha256=profile["review_sha256"], closure_audit_sha256=closure_sha256, epoch=epoch)
    before = semantic.Graph(semantic.canonical(graph), semantic.digest(graph), graph["role"], profile, {}).normalized()[0]
    current = observed["graph"]
    after = semantic.Graph(semantic.canonical(current), semantic.digest(current), graph["role"], profile, {}).normalized()[0]
    if before != after:
        raise ValueError("whole_native_schema_or_dependency_changed_from_C1_accepted_graph")
    require_pg_enforcement(reflected["__native_catalog__"])
    require_exact_c1_uniqueness(reflected)
    values = (reference.review_sha256, sha256(reference.raw).hexdigest(), semantic.digest(before),
        sha256(canonical(reflected)).hexdigest(), closure_sha256, current["identity"]["snapshot"])
    return VerifiedNativeProjection(*values, (_PROJECTION_SEAL, values))


def prepare_native_projection(factory, review, reference, *, held, append_closure):
    """Existing checked factory, one RR READ ONLY transaction, no DML or engine.

    Reuses the reviewed producer/adapter unchanged. Actual rollback AND session
    close precede the existing fsync journal receipt. No backend absence is
    inferred: the next CheckedFactory/lease census must still prove zero others.
    """
    from tools.l12_private_full23_lifecycle import CheckedFactory
    from tools import l12_native_schema_graph as native
    if (type(factory) is not CheckedFactory or factory.target_capability is None
            or type(reference) is not AcceptedNativeReference
            or not callable(held) or not callable(append_closure)):
        raise ValueError("actual_nonce_checked_factory_and_journal_required")
    if reference.review_sha256 != sha256(canonical(review)).hexdigest():
        raise ValueError("accepted_native_reference_review_changed")
    accepted_native_reference(review, reference.raw)
    from tools.l12_whole_schema_semantics import decode
    identity = decode(reference.raw)["graph"]["identity"]
    values = factory.target_capability.values
    if any(identity[key] != values[field] for key, field in (
            ("database", "database"), ("role", "database_user"), ("system_identifier", "system_identifier"),
            ("postmaster_started", "postmaster_started"), ("server_version_num", "server_version_num"))):
        raise ValueError("accepted_native_reference_not_actual_factory_nonce_epoch")
    if held() is not True:
        raise ValueError("native_preflight_writer_source_UNKNOWN")
    session, failure = None, None
    try:
        session = factory()
        session.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"))
        connection = session.connection()
        purpose = validate_c1_review(review)
        role = "PRIVATE_BASELINE" if purpose == C1_BASELINE else "PRIVATE_UPGRADED"
        observed = native.collect(connection, role, GRAPH_SOURCE_PINS["tools/l12_native_schema_graph.py"])
        reflected = c1_faithful_catalog(connection, purpose)
        if connection.execute(text("SHOW session_replication_role")).scalar_one() != "origin":
            raise ValueError("private_constraint_replication_role_not_origin")
        session.rollback()
        if session.in_transaction() or connection.in_transaction():
            raise ValueError("native_preflight_RO_transaction_closure_UNKNOWN")
        session.close()
        session = None
        if held() is not True:
            raise ValueError("native_preflight_writer_source_changed_after_RO_close")
        raw = native.canonical(observed)
        closure = dict(stage="NATIVE_RR_READ_ONLY_TRANSACTION_AND_SESSION_CLOSED",
            raw_graph_sha256=sha256(raw).hexdigest(), snapshot=observed["context"]["snapshot"],
            target_capability_sha256=factory.target_capability.reviewed_sha256,
            rollback_succeeded=True, session_close_succeeded=True,
            backend_absence_proven=False, writer_exclusion_still_held=True)
        receipt_sha = append_closure(closure, raw, canonical(reflected))
        return _bind_native_projection(review, reference, raw, reflected, receipt_sha)
    except BaseException as exc:
        failure = exc
        raise
    finally:
        cleanup_failure = None
        if session is not None:
            for cleanup in (session.rollback, session.close):
                try:
                    cleanup()
                except BaseException as exc:
                    if failure is not None:
                        failure.add_note("private_cleanup_failure_type=" + type(exc).__name__)
                    elif cleanup_failure is None:
                        cleanup_failure = exc
        if cleanup_failure is not None:
            raise cleanup_failure


def require_exact_c1_uniqueness(catalog):
    """Two equivalences plus eight literal native UNIQUE objects, not Base inference."""
    native = {row["table"]: row for row in catalog["__native_catalog__"]}
    for table, mapping in EXACT_UNIQUENESS.items():
        indexes = [row for row in native[table]["indexes"] if row["name"] == mapping["index"]]
        reflected = [row for row in catalog[table]["indexes"] if row["name"] == mapping["index"]]
        constraints = [row for row in catalog[table]["uniques"] if row["name"] == mapping["constraint"]]
        native_constraints = [row for row in native[table]["constraints"] if row["name"] == mapping["constraint"]]
        if (len(indexes) != 1 or indexes[0]["unique_index"] is not False
                or len(reflected) != 1 or reflected[0]["unique"] is not False
                or reflected[0]["column_names"] != mapping["columns"]
                or len(constraints) != 1 or constraints[0]["column_names"] != mapping["columns"]
                or len(native_constraints) != 1 or native_constraints[0]["kind"] != "u"
                or native_constraints[0]["validated"] is not True
                or native_constraints[0]["deferrable"] is not False
                or native_constraints[0]["initially_deferred"] is not False):
            raise ValueError("exact_named_plain_index_and_enforcing_UNIQUE_constraint_required")
    for table, objects in NAMED_NATIVE_UNIQUES.items():
        for name, columns in objects.items():
            constraints = [row for row in catalog[table]["uniques"] if row["name"] == name]
            enforced = [row for row in native[table]["constraints"] if row["name"] == name]
            indexes = [row for row in native[table]["indexes"] if row["name"] == name]
            if (len(constraints) != 1 or constraints[0]["column_names"] != columns
                    or len(enforced) != 1 or enforced[0]["kind"] != "u"
                    or enforced[0]["validated"] is not True
                    or enforced[0]["deferrable"] is not False
                    or enforced[0]["initially_deferred"] is not False
                    or len(indexes) != 1 or indexes[0]["unique_index"] is not True
                    or any(indexes[0][key] is not True for key in ("valid", "ready", "live", "immediate"))):
                raise ValueError("exact_eight_native_named_UNIQUE_constraints_and_backing_indexes_required")


def c1_faithful_catalog(connection, purpose):
    """Existing reflected/native catalog plus all actual columns and relations.

    Returns observations only; never manufactures a review/expected fingerprint.
    Whole public table scope keeps incoming FK enforcement membership complete.
    """
    if connection.dialect.name != "postgresql" or purpose not in {C1_BASELINE, C1_UPGRADED}:
        raise ValueError("exact_C1_native_catalog_PG_purpose_required")
    names = C1_TABLES | (C1_NEW_TABLES if purpose == C1_UPGRADED else frozenset())
    inspector = inspect(connection)
    if set(inspector.get_table_names(schema="public")) != names:
        raise ValueError("whole_C1_table_membership_changed")
    metadata = MetaData()
    for name in sorted(names):
        Table(name, metadata, schema="public")
    catalog = reflected_catalog(connection, metadata)
    columns = {}
    for name in sorted(names):
        columns[name] = [dict(row, type=str(row["type"].compile(dialect=connection.dialect)))
            for row in inspector.get_columns(name, schema="public")]
    catalog["__columns_and_defaults__"] = columns
    catalog["__primary_keys__"] = {name: inspector.get_pk_constraint(name, schema="public") for name in sorted(names)}
    catalog["__public_relations__"] = [dict(row) for row in connection.execute(text(
        "SELECT c.relname,c.relkind,c.relpersistence,c.relrowsecurity,c.relforcerowsecurity,"
        "c.relreplident,c.reloptions,c.relacl::text AS acl,pg_get_userbyid(c.relowner) AS owner "
        "FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace "
        "WHERE n.nspname='public' ORDER BY c.relname,c.relkind")).mappings()]
    catalog["__sequences__"] = [dict(row) for row in connection.execute(text(
        "SELECT c.relname,format_type(s.seqtypid,NULL) AS type,s.seqstart,s.seqincrement,"
        "s.seqmax,s.seqmin,s.seqcache,s.seqcycle "
        "FROM pg_catalog.pg_sequence s JOIN pg_catalog.pg_class c ON c.oid=s.seqrelid "
        "JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='public' ORDER BY c.relname")).mappings()]
    return catalog


def verify_c1_native_catalog(connection, review, *, native_projection=None):
    purpose = validate_c1_review(review)
    if connection.dialect.name != "postgresql":
        raise ValueError("C1_native_profile_requires_postgresql")
    catalog = c1_faithful_catalog(connection, purpose)
    if connection.execute(text("SHOW session_replication_role")).scalar_one() != "origin":
        raise ValueError("private_constraint_replication_role_not_origin")
    require_pg_enforcement(catalog["__native_catalog__"])
    require_exact_c1_uniqueness(catalog)
    actual = sha256(canonical(catalog)).hexdigest()
    if review["version"] == C1_GRAPH_VERSION:
        if (type(native_projection) is not VerifiedNativeProjection
                or native_projection._seal != (_PROJECTION_SEAL, native_projection.sealed_values())
                or native_projection.review_sha256 != sha256(canonical(review)).hexdigest()
                or actual != native_projection.reflected_sha256):
            raise ValueError("actual_full_graph_verified_projection_missing_or_schema_changed_before_DML")
        return actual
    if actual != review["canonical_catalog_sha256"]:
        raise ValueError("C1_native_schema_changed_from_independently_approved_catalog")
    return actual


def model_ddl_sha256(metadata, dialect):
    statements = []
    for table in sorted(metadata.tables.values(), key=lambda table: table.name):
        statements.append(str(CreateTable(table).compile(dialect=dialect)))
        statements.extend(str(CreateIndex(index).compile(dialect=dialect))
            for index in sorted(table.indexes, key=lambda index: index.name))
    return sha256(canonical(statements)).hexdigest()


def reflected_catalog(connection, metadata):
    inspector = inspect(connection)
    result = {}
    for table in sorted(metadata.tables.values(), key=lambda table: table.name):
        indexes = inspector.get_indexes(table.name, schema=table.schema)
        result[table.name] = dict(
            checks=inspector.get_check_constraints(table.name, schema=table.schema),
            uniques=inspector.get_unique_constraints(table.name, schema=table.schema),
            indexes=[dict(index, dialect_options={key: str(value) for key, value
                in index.get("dialect_options", {}).items()}) for index in indexes],
            foreign_keys=inspector.get_foreign_keys(table.name, schema=table.schema))
        for key, values in result[table.name].items():
            result[table.name][key] = sorted(values, key=lambda value: canonical(value))
    # Inspector may skip expression indexes (notably SQLite). Preserve native
    # catalog definitions too; a matching incomplete reflection is not evidence.
    names = sorted(table.name for table in metadata.tables.values())
    if connection.dialect.name == "sqlite":
        query = text("SELECT type, name, tbl_name, sql FROM sqlite_master "
            "WHERE type IN ('table', 'index', 'trigger') AND tbl_name IN :names "
            "ORDER BY type, name").bindparams(bindparam("names", expanding=True))
        result["__native_catalog__"] = [dict(row) for row in connection.execute(query, dict(names=names)).mappings()]
    elif connection.dialect.name == "postgresql":
        native = []
        for table in sorted(metadata.tables.values(), key=lambda table: table.name):
            schema = table.schema or inspector.default_schema_name
            operands = dict(schema=schema, table=table.name)
            indexes = connection.execute(text("SELECT ic.relname AS name, pg_get_indexdef(ix.indexrelid) AS definition, "
                "ix.indisvalid AS valid, ix.indisready AS ready, ix.indislive AS live, "
                "ix.indimmediate AS immediate, ix.indisunique AS unique_index "
                "FROM pg_catalog.pg_index ix JOIN pg_catalog.pg_class tc ON tc.oid=ix.indrelid "
                "JOIN pg_catalog.pg_namespace ns ON ns.oid=tc.relnamespace "
                "JOIN pg_catalog.pg_class ic ON ic.oid=ix.indexrelid "
                "WHERE ns.nspname=:schema AND tc.relname=:table ORDER BY ic.relname"), operands).mappings()
            constraints = connection.execute(text("SELECT con.conname AS name, con.contype AS kind, "
                "pg_get_constraintdef(con.oid, true) AS definition, con.convalidated AS validated, "
                "con.condeferrable AS deferrable, con.condeferred AS initially_deferred "
                "FROM pg_catalog.pg_constraint con JOIN pg_catalog.pg_class tc ON tc.oid=con.conrelid "
                "JOIN pg_catalog.pg_namespace ns ON ns.oid=tc.relnamespace "
                "WHERE ns.nspname=:schema AND tc.relname=:table ORDER BY con.conname"), operands).mappings()
            triggers = connection.execute(text("SELECT CASE WHEN tr.tgisinternal THEN NULL ELSE tr.tgname END AS name, "
                "CASE WHEN tr.tgisinternal THEN NULL ELSE pg_get_triggerdef(tr.oid, true) END AS definition, "
                "tr.tgenabled AS enabled, tr.tgisinternal AS internal, tr.tgtype AS event_type, "
                "tr.tgdeferrable AS deferrable, tr.tginitdeferred AS initially_deferred, "
                "proc.proname AS function_name, pns.nspname AS function_schema, "
                "con.conname AS constraint_name, con.contype AS constraint_kind, "
                "fkrel.relname AS constraint_table, fkns.nspname AS constraint_schema "
                "FROM pg_catalog.pg_trigger tr "
                "JOIN pg_catalog.pg_class tc ON tc.oid=tr.tgrelid "
                "JOIN pg_catalog.pg_namespace ns ON ns.oid=tc.relnamespace "
                "JOIN pg_catalog.pg_proc proc ON proc.oid=tr.tgfoid "
                "JOIN pg_catalog.pg_namespace pns ON pns.oid=proc.pronamespace "
                "LEFT JOIN pg_catalog.pg_constraint con ON con.oid=tr.tgconstraint "
                "LEFT JOIN pg_catalog.pg_class fkrel ON fkrel.oid=con.conrelid "
                "LEFT JOIN pg_catalog.pg_namespace fkns ON fkns.oid=fkrel.relnamespace "
                "WHERE ns.nspname=:schema AND tc.relname=:table ORDER BY tr.tgname"), operands).mappings()
            native.append(dict(table=table.name, schema=schema, indexes=[dict(row) for row in indexes],
                constraints=[dict(row) for row in constraints], triggers=sorted((dict(row) for row in triggers), key=canonical)))
        result["__native_catalog__"] = native
    else:
        raise ValueError("native_catalog_verification_not_implemented_for_dialect")
    return result


def require_pg_enforcement(native):
    """Enforcement is a hard predicate, not something a bad approval can waive.

    All four PostgreSQL RI triggers must be present and origin-enabled for each
    FK, including referenced-side internal triggers. OID-generated names are not
    semantic identities; constraint/function/event/relation are recorded instead.
    """
    if not isinstance(native, list) or not native:
        raise ValueError("postgres_enforcement_catalog_missing")
    foreign_keys, observed = set(), {}
    for table in native:
        for index in table["indexes"]:
            if (any(index.get(key) is not True for key in ("valid", "ready", "live"))
                    or type(index.get("unique_index")) is not bool
                    or type(index.get("immediate")) is not bool
                    or index["unique_index"] and index["immediate"] is not True):
                raise ValueError("private_index_not_enforcing")
        for constraint in table["constraints"]:
            if (constraint.get("validated") is not True or constraint.get("deferrable") is not False
                    or constraint.get("initially_deferred") is not False):
                raise ValueError("private_constraint_not_validated")
            if constraint["kind"] == "f":
                foreign_keys.add((table["schema"], table["table"], constraint["name"]))
        for trigger in table["triggers"]:
            if trigger.get("enabled") not in {"O", "A"} or type(trigger.get("internal")) is not bool:
                raise ValueError("private_trigger_not_origin_enabled")
            if not trigger["internal"]:
                continue
            if trigger.get("constraint_kind") != "f" or trigger.get("function_schema") != "pg_catalog":
                raise ValueError("unreviewed_internal_constraint_trigger")
            if trigger.get("deferrable") is not False or trigger.get("initially_deferred") is not False:
                raise ValueError("private_fk_trigger_not_immediate")
            key = (trigger.get("constraint_schema"), trigger.get("constraint_table"), trigger.get("constraint_name"))
            observed.setdefault(key, []).append((trigger.get("function_name"), trigger.get("event_type")))
    if set(observed) != foreign_keys:
        raise ValueError("private_fk_internal_trigger_membership_unknown")
    for triggers in observed.values():
        checks = [(name, kind) for name, kind in triggers if name in {"RI_FKey_check_ins", "RI_FKey_check_upd"}]
        actions = [(name, kind) for name, kind in triggers if name not in {"RI_FKey_check_ins", "RI_FKey_check_upd"}]
        if (len(triggers) != 4 or sorted(checks) != [("RI_FKey_check_ins", 5), ("RI_FKey_check_upd", 17)]
                or len(actions) != 2 or sorted(kind for name, kind in actions) != [9, 17]
                or any(not any(name == f"RI_FKey_{action}_{suffix}" and kind == event
                    for action in ("noaction", "restrict", "cascade", "setnull", "setdefault")
                    for suffix, event in (("del", 9), ("upd", 17))) for name, kind in actions)):
            raise ValueError("private_fk_check_or_action_trigger_incomplete")


def require_model_membership(catalog, metadata):
    """Independent reviewed hashes cannot approve missing model constraints."""
    native = {item["table"]: item for item in catalog["__native_catalog__"]}
    for table in metadata.tables.values():
        actual = catalog[table.name]
        unique = {tuple(item["column_names"]) for item in actual["uniques"]}
        required = {tuple(col.name for col in constraint.columns) for constraint in table.constraints
            if isinstance(constraint, UniqueConstraint)}
        checks = {item["name"] for item in actual["checks"]}
        required_checks = {constraint.name for constraint in table.constraints if isinstance(constraint, CheckConstraint)}
        indexes = {item["name"]: item for item in native[table.name]["indexes"]}
        if (not required <= unique or not required_checks <= checks
                or any(index.name not in indexes or indexes[index.name]["unique_index"] is not bool(index.unique)
                    for index in table.indexes)):
            raise ValueError("canonical_model_constraint_or_index_missing")


def verify_canonical_catalog(connection, metadata, review, *, native_projection=None):
    if is_c1_review(review):
        return verify_c1_native_catalog(connection, review, native_projection=native_projection)
    if (not isinstance(review, dict) or review.get("purpose") != "INDEPENDENT_CANONICAL_PRIVATE_SCHEMA_REVIEW"
            or not isinstance(review.get("review_id"), str) or not review["review_id"].strip()
            or review.get("dialect") != connection.dialect.name
            or review.get("canonical_model_ddl_sha256") != model_ddl_sha256(metadata, connection.dialect)
            or not is_sha(review.get("canonical_catalog_sha256"))):
        raise ValueError("independent_canonical_schema_evidence_required")
    catalog = reflected_catalog(connection, metadata)
    if connection.dialect.name == "postgresql":
        if connection.execute(text("SHOW session_replication_role")).scalar_one() != "origin":
            raise ValueError("private_constraint_replication_role_not_origin")
        require_pg_enforcement(catalog["__native_catalog__"])
        require_model_membership(catalog, metadata)
    actual = sha256(canonical(catalog)).hexdigest()
    if actual != review["canonical_catalog_sha256"]:
        raise ValueError("private_check_unique_partial_index_or_fk_schema_changed")
    return actual
