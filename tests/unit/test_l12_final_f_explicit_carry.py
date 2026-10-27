"""CONTROLLED explicit final-component binding, never actual Main acceptance."""

from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.unit.test_l12_current359_consumer_carry import operands
from tools.l12_composed_execution_binding import CURRENT_F_BOOT_VERSION, bind_composed_execution
from tools.l12_private_schema_evidence import CURRENT_F_APP_COMPONENT, CURRENT_359_APP_COMPONENT
from tools.l12_private_restore_main import (
    CopySourceGuard, require_current_f_app_inventory, require_current_359_app_inventory,
)


def final_operands():
    approval, pins = operands()
    component = CURRENT_F_APP_COMPONENT
    pins[component["path"]] = component["after_sha256"]
    approval.update(binding_version=CURRENT_F_BOOT_VERSION, app_component=deepcopy(component),
        carry_review=dict(version="L12_F278_COMPONENT_SOURCE_CARRY_V1", review_sha256="9" * 64,
            before_component=deepcopy(CURRENT_359_APP_COMPONENT), after_component=deepcopy(component),
            scope="SOURCE_ONLY_NOT_BOOT_NATIVE_122_OR_PERMISSION"))
    reseal(approval, pins)
    return approval, pins


def reseal(approval, pins):
    approval["reviewed_pins"] = deepcopy(pins)
    approval["consumer_binding"]["consumer_inventory_sha256"] = sha256(json.dumps(pins,
        sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def test_explicit_f_profile_requires_only_literal_display_delta_not_capability_retag():
    approval, pins = final_operands()
    original = deepcopy((approval, pins))
    result = bind_composed_execution(approval, observed_root="f" * 40, observed_pins=pins)
    assert result["app_component_head"] == "2782fe6b1f8135703203682f4be633aa0c751768"
    assert result["creation_source_head"] == "e" * 40
    assert result["permission_granted"] is False
    assert (approval, pins) == original


def test_historical359_profile_cannot_accept_final_f_as_its_own_source():
    _, pins = final_operands()
    with pytest.raises(ValueError):
        require_current_359_app_inventory(pins)


@pytest.mark.parametrize("fault", ["old_display", "other_L12_change", "extra_APP", "missing_APP"])
def test_f_profile_cannot_adopt_arbitrary_other_source_even_when_inventory_resealed(fault):
    approval, pins = final_operands()
    if fault == "old_display":
        pins[CURRENT_F_APP_COMPONENT["path"]] = CURRENT_F_APP_COMPONENT["before_sha256"]
    elif fault == "other_L12_change":
        pins["src/project_mai_tai/brokerdecides1_entry.py"] = "a" * 64
    elif fault == "extra_APP":
        pins["src/project_mai_tai/UNKNOWN.py"] = "a" * 64
    else:
        del pins["src/project_mai_tai/settings.py"]
    reseal(approval, pins)
    with pytest.raises(ValueError):
        bind_composed_execution(approval, observed_root="f" * 40, observed_pins=pins)


@pytest.mark.parametrize("fault", ["old_version", "old_before", "wrong_after", "zero_review", "scope"])
def test_separate_actual_f_carry_review_is_required_not_an_old_359_approval(fault):
    approval, pins = final_operands()
    carry = approval["carry_review"]
    if fault == "old_version":
        carry["version"] = "L12_359B_COMPONENT_SOURCE_CARRY_V1"
    elif fault == "old_before":
        carry["before_component"] = dict(head="NOT359")
    elif fault == "wrong_after":
        carry["after_component"]["head"] = "a" * 40
    elif fault == "zero_review":
        carry["review_sha256"] = "0" * 64
    else:
        carry["scope"] = "GO"
    with pytest.raises(ValueError):
        bind_composed_execution(approval, observed_root="f" * 40, observed_pins=pins)


@pytest.mark.parametrize("current359,combined,creation", [(True, True, "e" * 40),
    (False, False, "e" * 40), (False, True, None)])
def test_filesystem_f_mode_is_explicit_and_cannot_mix_original_or_359_modes(current359, combined, creation):
    guard = CopySourceGuard(Path("/CONTROLLED_NOT_READ"), "f" * 40, (), 10 ** 20,
        creation_head=creation, combined_app=combined, current_359_app=current359, current_f_app=True)
    assert guard(SimpleNamespace(execution_head="f" * 40)) is False
