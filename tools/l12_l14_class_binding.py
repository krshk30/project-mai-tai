"""V9 L12 execution + L14 schema binding, NOT whole SIM/runtime qualification.

No source discovery, imports of SIM, Git, DB, DDL or permission authority. Main
supplies independent actual-root/source/schema observations and review records.
Historical V8 keeps its no-SIM scope and original migration-parent contract.
"""

from hashlib import sha256

from tools.l12_close_asof_contract import is_head, is_sha
from tools.l12_migration_transition import (
    L12_PATH, L14_PATH, SCHEMA_PATH, OLD_PARENT, NEW_PARENT, review_transition,
)
from tools.l12_standalone_class_binding import (
    ABSENT_SOURCE as V8_ABSENT, BASELINE, BASELINE_PINS as V8_BASELINE_PINS,
)


VERSION = "L12_WITH_L14_SCHEMA_V9"
L14_COMPONENT_HEAD = "c0f4354244787a8a1f7a35a40cdfe9b8b00b1454"
ADDITIONS = {
    "tools/l12_close_consumer_probe.py": "ABSENT_AT_2E79",
    "tools/l12_v2_native_semantics.py": "ABSENT_AT_2E79",
    "tools/l12_migration_transition.py": "ABSENT_AT_2E79",
    "tools/l12_l14_class_binding.py": "ABSENT_AT_2E79",
    L14_PATH: "ABSENT_AT_2E79",
    "atr_simulation/store_schema_20261023.py": "ABSENT_AT_2E79",
    "atr_simulation/models.py": "ABSENT_AT_2E79",
}
BASELINE_PINS = {**V8_BASELINE_PINS, **ADDITIONS}
SOURCE_PINS = {
    L12_PATH: "32eab2188f1bbb42418d5554a9101ac918db50e0f630b1bd966261afa8eeb320",
    L14_PATH: "185e4ee38f868b84fe031fc77232061e081b02dacf429bd4a0048d26aaaeea0b",
    "atr_simulation/store_schema_20261023.py": "c6e98ec1865b36f48d810d813dc36c6a335d579860d77549319e418d462bb2bc",
    "atr_simulation/models.py": "385ae2ce6bdd1f1a20c05671c0ae5f5b3816de5df8e864b379a97e4432fceb3a",
}
SCHEMA_TABLES = frozenset({
    "brokerdecides_dispatch_retirements", "brokerdecides_dispatch_evidence",
    "atr_simulation_inputs", "atr_simulation_consumers", "atr_simulation_runs", "atr_simulation_rows",
})
ABSENT_SOURCE = [path for path in V8_ABSENT if path not in {
    "src/project_mai_tai/atr_simulation/", L14_PATH}]


def bind_l12_l14_schema(approval, *, observed_root, observed_pins, sources,
                        observed_schema, observed_absent):
    """Exactly 56 reviewed dependencies; no acceptance of extra/missing names.

    Only the L14 schema dependency is covered here. Its engine, population,
    worker, UI, results, native inputs and separate install remain unqualified.
    """
    if (not isinstance(approval, dict) or approval.get("binding_version") != VERSION
            or not is_head(observed_root) or approval.get("execution_root") != observed_root
            or approval.get("baseline_head") != BASELINE
            or approval.get("baseline_pins") != BASELINE_PINS
            or not is_sha(approval.get("review_sha256"))
            or not isinstance(observed_pins, dict) or set(observed_pins) != set(BASELINE_PINS)
            or any(not is_sha(pin) for pin in observed_pins.values())
            or approval.get("reviewed_pins") != observed_pins):
        raise ValueError("l12_l14_fixed_source_membership_and_independent_review_required")
    changed = {path for path, before in BASELINE_PINS.items() if observed_pins[path] != before}
    reviews = approval.get("differences")
    if not isinstance(reviews, dict) or set(reviews) != changed:
        raise ValueError("l12_l14_every_source_difference_review_required")
    for path in changed:
        item = reviews[path]
        if (not isinstance(item, dict) or item.get("before") != BASELINE_PINS[path]
                or item.get("after") != observed_pins[path] or not is_sha(item.get("review_sha256"))
                or not isinstance(item.get("reason"), str) or not item["reason"].strip()):
            raise ValueError("l12_l14_source_difference_review_unbound")
    scope = approval.get("scope")
    if (not isinstance(scope, dict) or set(scope) != {"rulings", "execution_root", "items", "hunk_review_sha256"}
            or scope["rulings"] != ["C1_20261023_1530_L12_INDEPENDENCE", "C1_20261023_1939_L14_BEFORE_L12"]
            or scope["execution_root"] != observed_root
            or scope["items"] != ["MANSTOP1", "L14_SIM", "L12"]
            or not is_sha(scope["hunk_review_sha256"])
            or observed_absent != {path: [] for path in ABSENT_SOURCE}
            or approval.get("reviewed_absent") != observed_absent):
        raise ValueError("l12_l14_exact_hunk_scope_and_no_l11_observations_required")
    if (not isinstance(sources, dict) or set(sources) != set(SOURCE_PINS)
            or any(not isinstance(raw, bytes) or sha256(raw).hexdigest() != SOURCE_PINS[path]
                   or observed_pins[path] != SOURCE_PINS[path] for path, raw in sources.items())):
        raise ValueError("l12_l14_exact_actual_migration_and_schema_source_bytes_required")
    ack = approval.get("parent_commit_ack")
    if not isinstance(ack, dict) or ack.get("head") != L14_COMPONENT_HEAD:
        raise ValueError("l12_l14_committed_parent_source_ack_required")
    chain = review_transition(
        original_l12=sources[L12_PATH].replace(NEW_PARENT, OLD_PARENT),
        proposed_l12=sources[L12_PATH],
        l14_sources={L14_PATH: sources[L14_PATH], SCHEMA_PATH: sources["atr_simulation/store_schema_20261023.py"]},
        commit_ack=ack, review_record_sha256=approval["review_sha256"])
    fields = {"inventory_sha256", "construction_review_sha256", "l12_canonical_schema_review_sha256",
              "l14_canonical_schema_review_sha256", "schema_policy_sha256", "source_table_states", "generation_table"}
    if (not isinstance(observed_schema, dict) or set(observed_schema) != fields
            or approval.get("schema") != observed_schema
            or any(not is_sha(observed_schema[key]) for key in (
                "inventory_sha256", "construction_review_sha256", "l12_canonical_schema_review_sha256",
                "l14_canonical_schema_review_sha256"))
            or observed_schema["schema_policy_sha256"] != observed_pins["tools/l12_private_schema_evidence.py"]
            or observed_schema["generation_table"] != "ABSENT_NOT_REQUIRED_NO_FRESHNESS_INFERENCE"
            or not isinstance(observed_schema["source_table_states"], dict)
            or set(observed_schema["source_table_states"]) != SCHEMA_TABLES
            or any(state not in {"ABSENT_REQUIRES_PRIVATE_CONSTRUCTION", "PRESENT_REQUIRES_FULL_MEMBERSHIP"}
                   for state in observed_schema["source_table_states"].values())):
        raise ValueError("l12_l14_all_six_schema_states_and_separate_canonical_reviews_required")
    return {"status": "L12_L14_SCHEMA_SOURCE_BOUND_NOT_BOOT_SIM_RUNTIME_OR_PERMISSION",
        "execution_root": observed_root, "components": sorted(BASELINE_PINS),
        "chain": chain["chain"], "historical_v8": "UNCHANGED",
        "l11_dependency": "ABSENT", "l14_runtime_qualification": "UNPROVEN",
        "native_permission": "UNKNOWN", "actual_close_boot": "REQUIRES_AFTER16_ACTUAL_OPERANDS",
        "permission_granted": False}
