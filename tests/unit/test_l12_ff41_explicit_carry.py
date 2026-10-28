"""CONTROLLED corrected-component carry; no actual review/boot/PG authority."""

from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.unit.test_l12_final_f_explicit_carry import final_operands, reseal
from tools.l12_composed_execution_binding import (
    CORRECTED_L11_BOOT_VERSION, CURRENT_F_BOOT_VERSION, bind_composed_execution,
)
from tools.l12_private_restore_main import (
    CopySourceGuard, require_corrected_l11_app_inventory, require_current_f_app_inventory,
)
from tools.l12_private_schema_evidence import CORRECTED_L11_APP_COMPONENT, CURRENT_F_APP_COMPONENT


def operands():
    approval, pins = final_operands()
    component = CORRECTED_L11_APP_COMPONENT
    pins[component["path"]] = component["after_sha256"]
    approval.update(binding_version=CORRECTED_L11_BOOT_VERSION, app_component=deepcopy(component),
        carry_review=dict(version="L12_FF41_COMPONENT_SOURCE_CARRY_V1", review_sha256="9" * 64,
            before_component=deepcopy(CURRENT_F_APP_COMPONENT), after_component=deepcopy(component),
            scope="SOURCE_ONLY_NOT_BOOT_NATIVE_122_OR_PERMISSION"))
    reseal(approval, pins)
    return approval, pins


def test_corrected_shared_completion_carry_binds334_APP_without_retagging_original_copy():
    approval, pins = operands()
    before = deepcopy((approval, pins))
    result = bind_composed_execution(approval, observed_root="f" * 40, observed_pins=pins)
    assert result["app_component_head"] == "ff41a0ffbeb8d34a6413913808d5cfa79dfe700f"
    assert result["creation_source_head"] == "e" * 40
    assert result["execution_root"] == "f" * 40
    assert result["permission_granted"] is False
    assert sum(name.startswith("src/project_mai_tai/") for name in pins) == 334
    assert (approval, pins) == before


@pytest.mark.parametrize("fault", ["F_service", "04052_service", "arbitrary_service", "core",
    "settings", "L12", "models", "extra", "missing"])
def test_literal_carry_cannot_accept_old_ordering_or_unrelated_APP_changes(fault):
    approval, pins = operands()
    component = CORRECTED_L11_APP_COMPONENT
    if fault == "F_service":
        pins[component["path"]] = component["before_sha256"]
    elif fault == "04052_service":
        pins[component["path"]] = "ddc930f1bcf1f6253e54394b4fe5902e8b1fa21515a2641e6c3fa9e9e60b2a0c"
    elif fault == "missing":
        del pins["src/project_mai_tai/settings.py"]
    else:
        path = {"arbitrary_service": component["path"],
            "core": "src/project_mai_tai/strategy_core/schwab_1m_v2.py",
            "settings": "src/project_mai_tai/settings.py", "L12": "src/project_mai_tai/brokerdecides1_entry.py",
            "models": "src/project_mai_tai/db/models.py", "extra": "src/project_mai_tai/EXTRA.py"}[fault]
        pins[path] = "a" * 64
    reseal(approval, pins)
    with pytest.raises(ValueError):
        bind_composed_execution(approval, observed_root="f" * 40, observed_pins=pins)


@pytest.mark.parametrize("fault", ["before", "after", "version", "review", "scope", "extra"])
def test_each_new_carry_review_operand_is_required_independently(fault):
    approval, pins = operands()
    carry = approval["carry_review"]
    if fault == "before":
        carry["before_component"] = {}
    elif fault == "after":
        carry["after_component"]["head"] = "a" * 40
    elif fault == "version":
        carry["version"] = "L12_F278_COMPONENT_SOURCE_CARRY_V1"
    elif fault == "review":
        carry["review_sha256"] = "0" * 64
    elif fault == "scope":
        carry["scope"] = "GO"
    else:
        carry["approval_inferred"] = True
    with pytest.raises(ValueError):
        bind_composed_execution(approval, observed_root="f" * 40, observed_pins=pins)


def test_old_F_profile_does_not_silently_accept_corrected_APP():
    approval, pins = operands()
    with pytest.raises(ValueError):
        require_current_f_app_inventory(pins)
    approval["binding_version"] = CURRENT_F_BOOT_VERSION
    with pytest.raises(ValueError):
        bind_composed_execution(approval, observed_root="f" * 40, observed_pins=pins)


@pytest.mark.parametrize("mode", ["old_F", "old359", "no_combined", "no_creation", "nonbool"])
def test_corrected_filesystem_mode_cannot_mix_legacy_profiles_or_creation_identity(mode):
    kwargs = dict(creation_head="e" * 40, combined_app=True, corrected_l11_app=True)
    if mode == "old_F":
        kwargs["current_f_app"] = True
    elif mode == "old359":
        kwargs["current_359_app"] = True
    elif mode == "no_combined":
        kwargs["combined_app"] = False
    elif mode == "no_creation":
        kwargs["creation_head"] = None
    else:
        kwargs["corrected_l11_app"] = 1
    guard = CopySourceGuard(Path("/CONTROLLED_NOT_READ"), "f" * 40, (), 10 ** 20, **kwargs)
    assert guard(SimpleNamespace(execution_head="f" * 40)) is False


def test_corrected_inventory_check_does_not_change_inputs():
    _, pins = operands()
    before = deepcopy(pins)
    require_corrected_l11_app_inventory(pins)
    assert pins == before
