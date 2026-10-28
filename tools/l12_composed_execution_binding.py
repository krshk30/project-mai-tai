"""Explicit composed-root review separate from immutable historical AA87 pins.

No git, moving HEAD, digest refresh, filesystem writes or permission authority.
Main supplies independently observed bytes/root and separately reviewed approval.
"""

from tools.l12_close_asof_contract import is_head, is_sha

COMBINED_BOOT_VERSION = "L12_COMBINED_06936_GRAPHV2_BOOT_V1"
CURRENT_359_BOOT_VERSION = "L12_COMBINED_359B_GRAPHV2_BOOT_V2"
CURRENT_F_BOOT_VERSION = "L12_COMBINED_F278_GRAPHV2_BOOT_V3"
CORRECTED_L11_BOOT_VERSION = "L12_COMBINED_FF41_GRAPHV2_BOOT_V4"
BOUNDED_L11_BOOT_VERSION = "L12_COMBINED_4F0150_GRAPHV2_BOOT_V5"


def bind_combined_boot_execution(approval, *, observed_root, observed_pins):
    from hashlib import sha256
    from tools.l12_scoped_chunks_v2 import canonical
    from tools.l12_private_schema_evidence import (
        COMBINED_APP_COMPONENT, CURRENT_359_APP_COMPONENT, CURRENT_F_APP_COMPONENT,
        CORRECTED_L11_APP_COMPONENT, BOUNDED_L11_APP_COMPONENT, GRAPH_SOURCE_PINS)
    from tools.l12_private_restore_main import (
        require_combined_app_inventory, require_current_359_app_inventory, require_current_f_app_inventory,
        require_corrected_l11_app_inventory, require_bounded_l11_app_inventory,
        ONLINE_SCHEMA_PINS, OFFLINE_CHILD_SHA)

    bounded = isinstance(approval, dict) and approval.get("binding_version") == BOUNDED_L11_BOOT_VERSION
    corrected = bounded or isinstance(approval, dict) and approval.get("binding_version") == CORRECTED_L11_BOOT_VERSION
    final = corrected or isinstance(approval, dict) and approval.get("binding_version") == CURRENT_F_BOOT_VERSION
    current = final or isinstance(approval, dict) and approval.get("binding_version") == CURRENT_359_BOOT_VERSION
    component = (BOUNDED_L11_APP_COMPONENT if bounded else CORRECTED_L11_APP_COMPONENT if corrected else CURRENT_F_APP_COMPONENT if final
        else CURRENT_359_APP_COMPONENT if current else COMBINED_APP_COMPONENT)
    expected_version = (BOUNDED_L11_BOOT_VERSION if bounded else CORRECTED_L11_BOOT_VERSION if corrected else CURRENT_F_BOOT_VERSION if final
        else CURRENT_359_BOOT_VERSION if current else COMBINED_BOOT_VERSION)
    fields = {"binding_version", "execution_root", "reviewed_pins", "review_sha256",
        "app_component", "copy_receipt_sha256", "schema_review_sha256", "consumer_binding"}
    if current:
        fields.add("carry_review")
    if (not isinstance(approval, dict) or set(approval) != fields
            or approval["binding_version"] != expected_version
            or not is_head(observed_root) or approval["execution_root"] != observed_root
            or approval["app_component"] != component
            or not isinstance(observed_pins, dict) or approval["reviewed_pins"] != observed_pins
            or any(not is_sha(value) or value == "0" * 64 for value in observed_pins.values())
            or any(not is_sha(approval[key]) or approval[key] == "0" * 64
                for key in ("review_sha256", "copy_receipt_sha256", "schema_review_sha256"))):
        raise ValueError("exact_combined_APP_copy_schema_consumer_review_required")
    if current:
        carry = approval["carry_review"]
        if (type(carry) is not dict or set(carry) != {"version", "review_sha256", "before_component",
                "after_component", "scope"}
                or carry["version"] != ("L12_4F0150_COMPONENT_SOURCE_CARRY_V1" if bounded
                    else "L12_FF41_COMPONENT_SOURCE_CARRY_V1" if corrected
                    else "L12_F278_COMPONENT_SOURCE_CARRY_V1" if final else "L12_359B_COMPONENT_SOURCE_CARRY_V1")
                or carry["before_component"] != (CURRENT_F_APP_COMPONENT if corrected
                    else CURRENT_359_APP_COMPONENT if final else COMBINED_APP_COMPONENT)
                or carry["after_component"] != component
                or carry["scope"] != "SOURCE_ONLY_NOT_BOOT_NATIVE_122_OR_PERMISSION"
                or not is_sha(carry["review_sha256"]) or carry["review_sha256"] == "0" * 64):
            raise ValueError("independent_exact_359B_carry_review_not_copy_capability_retag_required")
        if bounded:
            require_bounded_l11_app_inventory(observed_pins)
        elif corrected:
            require_corrected_l11_app_inventory(observed_pins)
        elif final:
            require_current_f_app_inventory(observed_pins)
        else:
            require_current_359_app_inventory(observed_pins)
    else:
        require_combined_app_inventory(observed_pins)
    tools = {"tools/l12_service_boot_replay.py", "tools/l12_composed_execution_binding.py",
        "tools/l12_scoped_role_packet_v2.py", "tools/l12_v2_native_semantics.py",
        "tools/l12_close_acquisition_protocol.py", "tools/l12_close_line_decode.py",
        "tools/l12_private_restore_main.py", "tools/l12_private_full23_lifecycle.py",
        "tools/l12_main_nonce_target.py", "tools/l12_nonce_copy_lease.py"}
    if current:
        tools.update({"tools/l12_recorded_boot_driver.py", "tools/l12_recorded_boot_timeline.py",
            "tools/l12_boot_owner_clock.py"})
    if not tools <= set(observed_pins):
        raise ValueError("combined_boot_complete_actual_source_closure_required")
    schema = dict(ONLINE_SCHEMA_PINS)
    schema.update(GRAPH_SOURCE_PINS)
    child = "sql/migrations/versions/20261023_atr_aggregate_entry.py"
    if observed_pins.get(child) == OFFLINE_CHILD_SHA:
        schema[child] = OFFLINE_CHILD_SHA
    if any(observed_pins.get(name) != digest for name, digest in schema.items()):
        raise ValueError("combined_boot_actual_schema_and_ini_pins_required")
    binding = approval["consumer_binding"]
    expected = {"version", "target_capability_sha256", "creation_source_head", "consumer_source_head",
        "consumer_root", "consumer_inventory_sha256", "review_sha256"}
    if (not isinstance(binding, dict) or set(binding) != expected
            or binding["version"] != "L12_C1_1409_TOOLS_ONLY_COPY_CONSUMER_V1"
            or binding["consumer_source_head"] != observed_root
            or not is_head(binding["creation_source_head"])
            or not isinstance(binding["consumer_root"], str) or not binding["consumer_root"].startswith("/")
            or binding["consumer_inventory_sha256"] != sha256(canonical(observed_pins)).hexdigest()
            or any(not is_sha(binding[key]) or binding[key] == "0" * 64
                for key in ("target_capability_sha256", "review_sha256"))):
        raise ValueError("combined_boot_original_nonce_separate_consumer_binding_required")
    return dict(status="EXPLICIT_COMBINED_SOURCE_BOUND_NOT_BOOT_OR_PERMISSION",
        execution_root=observed_root, app_component_head=component["head"],
        creation_source_head=binding["creation_source_head"],
        target_capability_sha256=binding["target_capability_sha256"],
        copy_receipt_sha256=approval["copy_receipt_sha256"],
        schema_review_sha256=approval["schema_review_sha256"], permission_granted=False)

CLASS_ADDITIONS = {
    "v2_flip_entry_ownership.py": "1140f5fb8b5881987811bb5f9591567e082eeb048d51bcfa9e59052c434d6b1a",
    "brokerdecides1_closed_owner.py": "ABSENT_AT_AA87",
    "brokerdecides1_owner_watchdog.py": "ABSENT_AT_AA87",
}
PHONE_CLASS_ADDITIONS = {**CLASS_ADDITIONS,
    "brokerdecides1_owner_phone.py": "ABSENT_AT_AA87",
    "ops/health/armed_segments_check.py": "ee8c3116635429e7ad2ed5453bcd8cb13ebc311f528bc46da2c5fa20cedbe1e2",
}


def bind_composed_execution(approval, *, observed_root, observed_pins, migration_sources=None, observed_schema=None,
                            observed_exclusions=None):
    if isinstance(approval, dict) and approval.get("binding_version") in {COMBINED_BOOT_VERSION,
            CURRENT_359_BOOT_VERSION, CURRENT_F_BOOT_VERSION, CORRECTED_L11_BOOT_VERSION, BOUNDED_L11_BOOT_VERSION}:
        if any(value is not None for value in (migration_sources, observed_schema, observed_exclusions)):
            raise ValueError("combined_boot_uses_actual_copy_schema_not_legacy_schema_substitutes")
        return bind_combined_boot_execution(approval, observed_root=observed_root, observed_pins=observed_pins)
    if isinstance(approval, dict) and approval.get("binding_version") == "L12_WITH_L14_SCHEMA_V9":
        from tools.l12_l14_class_binding import bind_l12_l14_schema

        return bind_l12_l14_schema(approval, observed_root=observed_root, observed_pins=observed_pins,
            sources=migration_sources, observed_schema=observed_schema, observed_absent=observed_exclusions)
    if isinstance(approval, dict) and approval.get("binding_version") == "L12_ONLY_COMPONENTS_V8":
        from tools.l12_standalone_class_binding import bind_standalone

        return bind_standalone(approval, observed_root=observed_root, observed_pins=observed_pins,
            sources=migration_sources, observed_schema=observed_schema, observed_absent=observed_exclusions)
    if isinstance(approval, dict) and approval.get("binding_version") == "L12_CLASS_COMPONENTS_V7_TRIM":
        from tools.l12_generation_class_binding import bind_trim_current_class

        return bind_trim_current_class(approval, observed_root=observed_root, observed_pins=observed_pins,
                                      migration_sources=migration_sources, observed_schema=observed_schema,
                                      observed_exclusions=observed_exclusions)
    if isinstance(approval, dict) and approval.get("binding_version") == "L12_CLASS_COMPONENTS_V6_TRIM":
        from tools.l12_generation_class_binding import bind_trim_class

        return bind_trim_class(approval, observed_root=observed_root, observed_pins=observed_pins,
                              migration_sources=migration_sources, observed_schema=observed_schema,
                              observed_exclusions=observed_exclusions)
    if isinstance(approval, dict) and approval.get("binding_version") == "L12_CLASS_COMPONENTS_V5":
        from tools.l12_generation_class_binding import bind_producer_closure_class

        return bind_producer_closure_class(approval, observed_root=observed_root, observed_pins=observed_pins,
                                           migration_sources=migration_sources, observed_schema=observed_schema)
    if isinstance(approval, dict) and approval.get("binding_version") == "L12_CLASS_COMPONENTS_V4":
        from tools.l12_generation_class_binding import bind_end_marker_class

        return bind_end_marker_class(approval, observed_root=observed_root, observed_pins=observed_pins,
                                     migration_sources=migration_sources, observed_schema=observed_schema)
    if isinstance(approval, dict) and approval.get("binding_version") == "L12_CLASS_COMPONENTS_V3":
        from tools.l12_generation_class_binding import bind_generation_class

        return bind_generation_class(approval, observed_root=observed_root, observed_pins=observed_pins,
                                     migration_sources=migration_sources, observed_schema=observed_schema)
    if isinstance(approval, dict) and approval.get("binding_version") in {"L12_CLASS_COMPONENTS_V1", "L12_CLASS_COMPONENTS_V2"}:
        return bind_class_execution(approval, observed_root=observed_root, observed_pins=observed_pins)
    # A declared version must select its reviewed fixed membership. Unknown
    # versions cannot silently fall back to the historical unversioned shape.
    if isinstance(approval, dict) and "binding_version" in approval:
        raise ValueError("explicit_class_version_required")
    from tools.l12_aa87_original122_ledger import APP_HEAD, APP_PINS
    if (not isinstance(approval, dict) or not is_head(observed_root)
            or approval.get("execution_root") != observed_root
            or approval.get("historical_component_root") != APP_HEAD
            or approval.get("historical_component_pins") != APP_PINS
            or not is_sha(approval.get("review_record_sha256"))
            or not isinstance(observed_pins, dict) or set(observed_pins) != set(APP_PINS)
            or approval.get("reviewed_execution_pins") != observed_pins
            or any(not is_sha(value) for value in observed_pins.values())):
        raise ValueError("explicit_composed_root_and_component_review_required")
    changed = {name for name in APP_PINS if observed_pins[name] != APP_PINS[name]}
    reviews = approval.get("reviewed_differences")
    if not isinstance(reviews, dict) or set(reviews) != changed:
        raise ValueError("each_component_difference_requires_independent_review")
    for name in changed:
        record = reviews[name]
        if (not isinstance(record, dict) or record.get("before") != APP_PINS[name]
                or record.get("after") != observed_pins[name]
                or not is_sha(record.get("diff_review_sha256"))
                or not isinstance(record.get("reason"), str) or not record["reason"].strip()):
            raise ValueError("component_difference_unbound")
    return dict(status="EXPLICIT_COMPOSED_ROOT_BOUND_NOT_EXECUTION_OR_GO", execution_root=observed_root,
        historical_component_root=APP_HEAD, schema_component_root=APP_HEAD,
        reviewed_changed_components=sorted(changed), permission_granted=False)


def bind_class_execution(approval, *, observed_root, observed_pins):
    """Pure explicit source closure, NOT reader completeness, execution or GO.

    Preserve AA87 bytes as historical provenance. The new file membership is
    fixed here, not selected by supplied hashes or a moving checkout HEAD.
    Independent review must name every existing difference AND added helper.
    """
    from tools.l12_aa87_original122_ledger import APP_HEAD, APP_PINS
    version = approval.get("binding_version") if isinstance(approval, dict) else None
    additions = {"L12_CLASS_COMPONENTS_V1": CLASS_ADDITIONS,
        "L12_CLASS_COMPONENTS_V2": PHONE_CLASS_ADDITIONS}.get(version)
    if additions is None:
        raise ValueError("explicit_class_version_required")
    required = set(APP_PINS) | set(additions)
    if (not isinstance(approval, dict)
            or not is_head(observed_root) or approval.get("execution_root") != observed_root
            or approval.get("historical_component_root") != APP_HEAD
            or approval.get("historical_component_pins") != APP_PINS
            or approval.get("historical_class_additions") != additions
            or not is_sha(approval.get("review_record_sha256"))
            or not isinstance(observed_pins, dict) or set(observed_pins) != required
            or approval.get("reviewed_execution_pins") != observed_pins
            or any(not is_sha(value) for value in observed_pins.values())):
        raise ValueError("explicit_class_source_closure_review_required")
    changed = {name for name in APP_PINS if observed_pins[name] != APP_PINS[name]} | set(additions)
    reviews = approval.get("reviewed_differences")
    if not isinstance(reviews, dict) or set(reviews) != changed:
        raise ValueError("class_helper_and_difference_reviews_required")
    for name in changed:
        record = reviews[name]
        if (not isinstance(record, dict) or record.get("before") != {**APP_PINS, **additions}[name]
                or record.get("after") != observed_pins[name]
                or not is_sha(record.get("diff_review_sha256"))
                or not isinstance(record.get("reason"), str) or not record["reason"].strip()):
            raise ValueError("class_component_difference_unbound")
    return dict(status="EXPLICIT_CLASS_SOURCE_BOUND_NOT_EXECUTION_OR_GO", execution_root=observed_root,
        historical_component_root=APP_HEAD, schema_component_root=APP_HEAD,
        reviewed_changed_components=sorted(changed), reviewed_component_names=sorted(required),
        permission_granted=False)
