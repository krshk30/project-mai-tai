"""Explicit standalone L12 source/schema review, not broker/boot permission."""

import ast
from hashlib import sha256

from tools.l12_close_asof_contract import is_head, is_sha

VERSION = "L12_ONLY_COMPONENTS_V8"
BASELINE = "2e79a1282eaed6ec8ae6b8ca4770ebdf08e0e8e9"
BASELINE_PINS = {
    "brokerdecides1.py": "ABSENT_AT_2E79",
    "brokerdecides1_entry.py": "ABSENT_AT_2E79",
    "brokerdecides1_native.py": "ABSENT_AT_2E79",
    "brokerdecides1_collect.py": "ABSENT_AT_2E79",
    "brokerdecides1_install_chunks.py": "ABSENT_AT_2E79",
    "brokerdecides1_retirement.py": "ABSENT_AT_2E79",
    "brokerdecides1_sync.py": "ABSENT_AT_2E79",
    "brokerdecides1_accounts.py": "ABSENT_AT_2E79",
    "cancelack1.py": "1e4887dbc9b6fbdf54bafc92f3ab48a8f01ecc01ba145f1c8042d5d17817c381",
    "db/models.py": "bed246599fc114e9c261bcbd54e44fbe4b229a80311baeb2244914e1860947e8",
    "brokerdecides1_set_proof.py": "ABSENT_AT_2E79",
    "oms/service.py": "c5f5ee806f19d28ce7ebfe64f0bc9a01d550e52a20444a0535f4cff122625bec",
    "broker_adapters/webull.py": "196eebe5171dd2fdf75c0c1f2b855e01c90ad4527e24ac6b1e6c284f54bbe878",
    "strategy_core/schwab_1m_v2.py": "08ed70d3e4c5cc8576caf1e25685f06d56766235f41af168a9d61a3e81fbd0f4",
    "services/schwab_1m_v2_bot.py": "bca4596f2bd63576571352af6a51bacedfb93c68fff86615d9ee6acb1d01f093",
    "settings.py": "3f383aef9b51522b984bf70312fe3bb2c935ad534000938f358871362a93f454",
    "v2_flip_entry_ownership.py": "1140f5fb8b5881987811bb5f9591567e082eeb048d51bcfa9e59052c434d6b1a",
    "brokerdecides1_closed_owner.py": "ABSENT_AT_2E79",
    "brokerdecides1_owner_watchdog.py": "ABSENT_AT_2E79",
    "brokerdecides1_owner_phone.py": "ABSENT_AT_2E79",
    "ops/health/armed_segments_check.py": "ee8c3116635429e7ad2ed5453bcd8cb13ebc311f528bc46da2c5fa20cedbe1e2",
    "brokerdecides1_prewire.py": "ABSENT_AT_2E79",
    "brokerdecides1_prewire_proof.py": "ABSENT_AT_2E79",
    "brokerdecides1_prewire_authority.py": "ABSENT_AT_2E79",
    "brokerdecides1_publication_callbacks.py": "ABSENT_AT_2E79",
    "brokerdecides1_alert_transport.py": "ABSENT_AT_2E79",
    "v2_removed_wait.py": "f40d95d165951b4cd1455e57efc76842497cf9f90baae61ed8fd8a8e4cd5fdb5",
    "sql/migrations/versions/20261022_l12_dispatch_retirement.py": "ABSENT_AT_2E79",
    "db/momlive_sec_country.py": "8a31f0199f2ea8271a2400eee6660933580ad09c94a7e5d2a77c55854a9008ec",
    "tools/l12_private_schema_evidence.py": "ABSENT_AT_2E79",
    "tools/l12_scoped_chunks_v2.py": "ABSENT_AT_2E79",
    "tools/l12_scoped_restore_v2.py": "ABSENT_AT_2E79",
    "tools/l12_required_copy_scope.py": "ABSENT_AT_2E79",
    "tools/l12_close_asof_contract.py": "ABSENT_AT_2E79",
    "tools/l12_close_export_restore.py": "ABSENT_AT_2E79",
    "tools/l12_service_boot_replay.py": "ABSENT_AT_2E79",
    "tools/l12_today_close_replay.py": "ABSENT_AT_2E79",
    "tools/l12_close_line_decode.py": "ABSENT_AT_2E79",
    "tools/l12_generation_class_binding.py": "ABSENT_AT_2E79",
    "tools/l12_composed_execution_binding.py": "ABSENT_AT_2E79",
    "tools/l12_aa87_original122_ledger.py": "ABSENT_AT_2E79",
    "services/strategy_engine_app.py": "3b94a64df918a764f2eb92a9f18a4f94308d4742a60b8bed723cb7c585a83a65",
    "broker_adapters/protocols.py": "4f82ade676bcce507be101498a1bde3102e28acfa4f7bfd8f8e1b72da1f43557",
    "broker_adapters/routing.py": "f10e8e2b6a35b27e9347eef94d893fde579e57411a6a6b48f71803468bbdd3ac",
    "broker_adapters/schwab.py": "1273f691aeadc419779ff6271dd068c5548194a94522843593277abca97d7774",
    "tools/l12_scoped_role_packet_v2.py": "ABSENT_AT_2E79",
    "tools/l12_close_acquisition_protocol.py": "ABSENT_AT_2E79",
    "strategy_core/session_line_restore.py": "da69d0dbd1f9a681caa5a623055904c4cbbeb1c5842bb9ac40b50e353605a460",
    "tools/l12_standalone_class_binding.py": "ABSENT_AT_2E79"
}
MIGRATION = "sql/migrations/versions/20261022_l12_dispatch_retirement.py"
SCHEMA_TABLES = {"brokerdecides_dispatch_retirements", "brokerdecides_dispatch_evidence"}
ABSENT_SOURCE = ["src/project_mai_tai/strategy_core/keepwatch.py",
    "src/project_mai_tai/services/keepwatch_bar_owner.py", "src/project_mai_tai/services/keepwatch_halt_proof.py",
    "src/project_mai_tai/services/keepwatch_startup_veto.py", "src/project_mai_tai/market_data/keepwatch_history.py",
    "src/project_mai_tai/oms/broker_sync_generation.py", "src/project_mai_tai/oms/broker_sync_collection.py",
    "src/project_mai_tai/oms/broker_sync_end_marker.py", "src/project_mai_tai/brokerdecides1_startup_alert.py",
    "sql/migrations/versions/20261023_broker_sync_generation.py",
    "src/project_mai_tai/atr_simulation/", "sql/migrations/versions/20261023_atr_simulation_store.py"]


def migration_observation(sources):
    if not isinstance(sources, dict) or set(sources) != {MIGRATION}:
        raise ValueError("standalone_exact_one_l12_migration_required")
    raw = sources[MIGRATION]
    if not isinstance(raw, bytes) or not 0 < len(raw) <= 256 * 1024:
        raise ValueError("standalone_actual_migration_bytes_required")
    values = {}
    for node in ast.parse(raw).body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id in {"revision", "down_revision"}:
                    if target.id in values or not isinstance(node.value, ast.Constant) or not isinstance(node.value.value, str):
                        raise ValueError("standalone_unique_literal_migration_identity_required")
                    values[target.id] = node.value.value
    if values != {"revision": "20261022_l12_retirement", "down_revision": "20261015_bot_control"}:
        raise ValueError("standalone_l12_migration_parent_required")
    return {MIGRATION: {**values, "source_sha256": sha256(raw).hexdigest()}}


def bind_standalone(approval, *, observed_root, observed_pins, sources, observed_schema, observed_absent):
    if (not isinstance(approval, dict) or approval.get("binding_version") != VERSION
            or not is_head(observed_root) or approval.get("execution_root") != observed_root
            or approval.get("baseline_head") != BASELINE or approval.get("baseline_pins") != BASELINE_PINS
            or not is_sha(approval.get("review_sha256"))
            or not isinstance(observed_pins, dict) or set(observed_pins) != set(BASELINE_PINS)
            or any(not is_sha(pin) for pin in observed_pins.values())
            or approval.get("reviewed_pins") != observed_pins):
        raise ValueError("standalone_fixed_source_inventory_and_independent_review_required")
    changes = {name for name, before in BASELINE_PINS.items() if observed_pins[name] != before}
    reviews = approval.get("differences")
    if not isinstance(reviews, dict) or set(reviews) != changes:
        raise ValueError("standalone_every_added_changed_source_review_required")
    for name in changes:
        item = reviews[name]
        if (not isinstance(item, dict) or item.get("before") != BASELINE_PINS[name]
                or item.get("after") != observed_pins[name] or not is_sha(item.get("review_sha256"))
                or not isinstance(item.get("reason"), str) or not item["reason"].strip()):
            raise ValueError("standalone_source_review_unbound")
    scope = approval.get("scope")
    if (not isinstance(scope, dict) or set(scope) != {"ruling", "execution_root", "items", "hunk_review_sha256"}
            or scope["ruling"] != "C1_20261023_1530_L12_INDEPENDENCE"
            or scope["execution_root"] != observed_root or scope["items"] not in
                (["MANSTOP1", "L12"], ["MANSTOP1", "L12", "WBEXIT1"])
            or not is_sha(scope["hunk_review_sha256"])
            or observed_absent != {path: [] for path in ABSENT_SOURCE}
            or approval.get("reviewed_absent") != observed_absent):
        raise ValueError("standalone_actual_scope_and_no_l11_sim_observations_required")
    migrations = migration_observation(sources)
    if (approval.get("migrations") != migrations
            or observed_pins[MIGRATION] != migrations[MIGRATION]["source_sha256"]):
        raise ValueError("standalone_actual_migration_review_unbound")
    if (not isinstance(observed_schema, dict) or set(observed_schema) != {
                "inventory_sha256", "construction_review_sha256", "canonical_schema_review_sha256",
                "schema_policy_sha256", "source_table_states", "generation_table"}
            or approval.get("schema") != observed_schema
            or any(not is_sha(observed_schema[k]) for k in
                ("inventory_sha256", "construction_review_sha256", "canonical_schema_review_sha256"))
            or observed_schema["schema_policy_sha256"] != observed_pins["tools/l12_private_schema_evidence.py"]
            or observed_schema["generation_table"] != "ABSENT_NOT_REQUIRED_NO_FRESHNESS_INFERENCE"
            or not isinstance(observed_schema["source_table_states"], dict)
            or set(observed_schema["source_table_states"]) != SCHEMA_TABLES
            or any(state not in {"ABSENT_REQUIRES_PRIVATE_CONSTRUCTION", "PRESENT_REQUIRES_FULL_MEMBERSHIP"}
                for state in observed_schema["source_table_states"].values())):
        raise ValueError("standalone_exact_two_projection_schema_provenance_required")
    return {"status": "STANDALONE_L12_SOURCE_SCHEMA_BOUND_NOT_BOOT_OR_PERMISSION",
        "execution_root": observed_root, "components": sorted(BASELINE_PINS),
        "migration_head": "20261022_l12_retirement", "l11_dependency": "ABSENT",
        "native_permission": "UNKNOWN", "actual_close_boot": "REQUIRES_AFTER16_ACTUAL_OPERANDS",
        "permission_granted": False}
