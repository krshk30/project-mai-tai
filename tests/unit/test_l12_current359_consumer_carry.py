"""CONTROLLED fixed-inventory/source carry, not actual Root/PG/boot permission."""

from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.unit.test_l12_combined_on_boot_binding import operands as old_operands
from tools.l12_composed_execution_binding import bind_composed_execution, CURRENT_359_BOOT_VERSION
from tools.l12_private_restore_main import (
    CopySourceGuard, require_combined_app_inventory, require_current_359_app_inventory)
from tools.l12_private_schema_evidence import (
    COMBINED_APP_COMPONENT, CURRENT_359_APP_COMPONENT, CURRENT_359_APP_DELTA)


def operands():
    approval, pins = old_operands()
    for name, (_, after) in CURRENT_359_APP_DELTA.items():
        pins[name] = after
    for name in ("l12_recorded_boot_driver.py", "l12_recorded_boot_timeline.py", "l12_boot_owner_clock.py"):
        pins["tools/" + name] = "7" * 64  # CONTROLLED metadata, never actual source observations.
    approval.update(binding_version=CURRENT_359_BOOT_VERSION, app_component=deepcopy(CURRENT_359_APP_COMPONENT),
        carry_review=dict(version="L12_359B_COMPONENT_SOURCE_CARRY_V1", review_sha256="8" * 64,
            before_component=deepcopy(COMBINED_APP_COMPONENT), after_component=deepcopy(CURRENT_359_APP_COMPONENT),
            scope="SOURCE_ONLY_NOT_BOOT_NATIVE_122_OR_PERMISSION"))
    reseal(approval, pins)
    return approval, pins


def reseal(approval, pins):
    approval["reviewed_pins"] = deepcopy(pins)
    approval["consumer_binding"]["consumer_inventory_sha256"] = sha256(json.dumps(pins,
        sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def test_fixed_359_inventory_has334_and_keeps_separate_creation_and_consumer():
    approval, pins = operands()
    result = bind_composed_execution(approval, observed_root="f" * 40, observed_pins=pins)
    assert sum(name.startswith("src/project_mai_tai/") for name in pins) == 334
    assert result["app_component_head"] == "359beec1b5051f84d5b0495cd8e61c91b8a6c3b6"
    assert result["creation_source_head"] == "e" * 40
    assert result["execution_root"] == "f" * 40
    assert result["permission_granted"] is False


def test_inverse_is_comparison_only_no_inventory_or_approval_rewrite():
    approval, pins = operands()
    original, original_approval = deepcopy(pins), deepcopy(approval)
    require_current_359_app_inventory(pins)
    bind_composed_execution(approval, observed_root="f" * 40, observed_pins=pins)
    assert pins == original
    assert approval == original_approval


def test_real_json_roundtrip_does_not_change_profile_component_representation():
    approval, pins = operands()
    decoded = json.loads(json.dumps(approval))
    result = bind_composed_execution(decoded, observed_root="f" * 40, observed_pins=pins)
    assert result["app_component_head"] == "359beec1b5051f84d5b0495cd8e61c91b8a6c3b6"
    assert result["permission_granted"] is False


@pytest.mark.parametrize("path", tuple(CURRENT_359_APP_DELTA))
def test_each_literal_delta_refuses_other_bytes_even_in_self_consistent_review(path):
    approval, pins = operands()
    pins[path] = "9" * 64
    reseal(approval, pins)
    with pytest.raises(ValueError):
        bind_composed_execution(approval, observed_root="f" * 40, observed_pins=pins)


@pytest.mark.parametrize("fault", ["missing_producer", "unknown_APP", "settings", "L12_entry", "models"])
def test_unchanged_membership_and_remaining_full_app_digest_are_not_widened(fault):
    approval, pins = operands()
    if fault == "missing_producer":
        del pins["src/project_mai_tai/market_data/keepwatch_producer.py"]
    elif fault == "unknown_APP":
        pins["src/project_mai_tai/UNKNOWN.py"] = "9" * 64
    else:
        path = {"settings": "settings.py", "L12_entry": "brokerdecides1_entry.py", "models": "db/models.py"}[fault]
        pins["src/project_mai_tai/" + path] = "9" * 64
    reseal(approval, pins)
    with pytest.raises(ValueError):
        bind_composed_execution(approval, observed_root="f" * 40, observed_pins=pins)


@pytest.mark.parametrize("fault", ["missing", "version", "zero_review", "scope", "before", "after", "extra"])
def test_new_source_carry_requires_exact_independent_review(fault):
    approval, pins = operands()
    carry = approval["carry_review"]
    if fault == "missing":
        del approval["carry_review"]
    elif fault == "version":
        carry["version"] = "UNREVIEWED"
    elif fault == "zero_review":
        carry["review_sha256"] = "0" * 64
    elif fault == "scope":
        carry["scope"] = "GO"
    elif fault in {"before", "after"}:
        carry[fault + "_component"]["head"] = "9" * 40
    else:
        carry["permit"] = True
    with pytest.raises(ValueError):
        bind_composed_execution(approval, observed_root="f" * 40, observed_pins=pins)


@pytest.mark.parametrize("name", ["l12_recorded_boot_driver.py", "l12_recorded_boot_timeline.py", "l12_boot_owner_clock.py"])
def test_current_profile_requires_revised_window_and_owner_closure(name):
    approval, pins = operands()
    del pins["tools/" + name]
    reseal(approval, pins)
    with pytest.raises(ValueError, match="complete_actual_source_closure"):
        bind_composed_execution(approval, observed_root="f" * 40, observed_pins=pins)


def test_old333_profile_still_rejects_new334_inventory():
    _, pins = operands()
    with pytest.raises(ValueError, match="exact_06936_full_APP_inventory"):
        require_combined_app_inventory(pins)


def test_new_profile_cannot_adopt_legacy_schema_operands():
    approval, pins = operands()
    with pytest.raises(ValueError, match="not_legacy_schema_substitutes"):
        bind_composed_execution(approval, observed_root="f" * 40, observed_pins=pins, observed_schema={})


@pytest.mark.parametrize("combined,creation,current", [(False, "e" * 40, True), (True, None, True), (True, "e" * 40, "yes")])
def test_current_filesystem_mode_requires_explicit_combined_and_original_creation_context(combined, creation, current):
    guard = CopySourceGuard(Path("/CONTROLLED_NOT_READ"), "f" * 40, (), 10 ** 20,
        creation_head=creation, combined_app=combined, current_359_app=current)
    assert guard(SimpleNamespace(execution_head="f" * 40)) is False
