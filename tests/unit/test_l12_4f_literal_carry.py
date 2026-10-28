"""CONTROLLED source binding only; no actual review, boot or target authority."""

from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.unit.test_l12_final_f_explicit_carry import final_operands, reseal
from tools.l12_composed_execution_binding import (
    BOUNDED_L11_BOOT_VERSION, CORRECTED_L11_BOOT_VERSION, CURRENT_F_BOOT_VERSION,
    bind_composed_execution,
)
from tools.l12_private_restore_main import (
    CopySourceGuard, require_bounded_l11_app_inventory,
    require_corrected_l11_app_inventory, require_current_f_app_inventory,
)
from tools.l12_private_schema_evidence import (
    BOUNDED_L11_APP_COMPONENT, CORRECTED_L11_APP_COMPONENT, CURRENT_F_APP_COMPONENT,
)


def operands():
    approval, pins = final_operands()
    component = BOUNDED_L11_APP_COMPONENT
    pins[component["path"]] = component["after_sha256"]
    approval.update(binding_version=BOUNDED_L11_BOOT_VERSION,
        app_component=deepcopy(component), carry_review=dict(
            version="L12_4F0150_COMPONENT_SOURCE_CARRY_V1", review_sha256="9" * 64,
            before_component=deepcopy(CURRENT_F_APP_COMPONENT),
            after_component=deepcopy(component),
            scope="SOURCE_ONLY_NOT_BOOT_NATIVE_122_OR_PERMISSION"))
    reseal(approval, pins)
    return approval, pins


def test_literal_4f_component_and_consumer_are_not_original_copy_identity():
    approval, pins = operands()
    before = deepcopy((approval, pins))
    result = bind_composed_execution(approval, observed_root="f" * 40, observed_pins=pins)
    assert result["app_component_head"] == "4f0150c188e92063ebfb160b6437a4ab5d432498"
    assert result["creation_source_head"] == "e" * 40
    assert result["execution_root"] == "f" * 40
    assert result["permission_granted"] is False
    assert sum(name.startswith("src/project_mai_tai/") for name in pins) == 334
    assert (approval, pins) == before


@pytest.mark.parametrize("digest", [CURRENT_F_APP_COMPONENT["before_sha256"],
    BOUNDED_L11_APP_COMPONENT["before_sha256"], CORRECTED_L11_APP_COMPONENT["after_sha256"],
    "ddc930f1bcf1f6253e54394b4fe5902e8b1fa21515a2641e6c3fa9e9e60b2a0c", "a" * 64])
def test_previous_or_unreviewed_service_cannot_be_resealed_as_4f(digest):
    approval, pins = operands()
    pins[BOUNDED_L11_APP_COMPONENT["path"]] = digest
    reseal(approval, pins)
    with pytest.raises(ValueError):
        bind_composed_execution(approval, observed_root="f" * 40, observed_pins=pins)


@pytest.mark.parametrize("path", ["src/project_mai_tai/settings.py",
    "src/project_mai_tai/strategy_core/schwab_1m_v2.py",
    "src/project_mai_tai/brokerdecides1_entry.py", "src/project_mai_tai/db/models.py",
    "src/project_mai_tai/UNREVIEWED.py"])
def test_no_other_APP_delta_allowed_even_with_resealed_inventory(path):
    approval, pins = operands()
    pins[path] = "a" * 64
    reseal(approval, pins)
    with pytest.raises(ValueError):
        bind_composed_execution(approval, observed_root="f" * 40, observed_pins=pins)


def test_missing_APP_is_not_completeness():
    approval, pins = operands()
    del pins["src/project_mai_tai/settings.py"]
    reseal(approval, pins)
    with pytest.raises(ValueError):
        bind_composed_execution(approval, observed_root="f" * 40, observed_pins=pins)


@pytest.mark.parametrize("fault", ["before", "after", "version", "review", "scope", "extra"])
def test_new_independent_carry_review_is_required(fault):
    approval, pins = operands()
    carry = approval["carry_review"]
    if fault == "before":
        carry["before_component"] = deepcopy(CORRECTED_L11_APP_COMPONENT)
    elif fault == "after":
        carry["after_component"]["head"] = "a" * 40
    elif fault == "version":
        carry["version"] = "L12_FF41_COMPONENT_SOURCE_CARRY_V1"
    elif fault == "review":
        carry["review_sha256"] = "0" * 64
    elif fault == "scope":
        carry["scope"] = "GO"
    else:
        carry["inferred_approval"] = True
    with pytest.raises(ValueError):
        bind_composed_execution(approval, observed_root="f" * 40, observed_pins=pins)


@pytest.mark.parametrize("version", [CURRENT_F_BOOT_VERSION, CORRECTED_L11_BOOT_VERSION])
def test_old_binding_versions_cannot_accept_4f(version):
    approval, pins = operands()
    approval["binding_version"] = version
    with pytest.raises(ValueError):
        bind_composed_execution(approval, observed_root="f" * 40, observed_pins=pins)


@pytest.mark.parametrize("check", [require_current_f_app_inventory, require_corrected_l11_app_inventory])
def test_old_inventory_profiles_still_refuse_4f(check):
    _, pins = operands()
    with pytest.raises(ValueError):
        check(pins)


@pytest.mark.parametrize("fault", ["old359", "oldF", "oldff41", "no_combined", "no_creation", "nonbool"])
def test_new_filesystem_mode_cannot_mix_profiles_or_omit_creation_identity(fault):
    kwargs = dict(creation_head="e" * 40, combined_app=True, bounded_l11_app=True)
    if fault in {"old359", "oldF", "oldff41"}:
        kwargs[{"old359": "current_359_app", "oldF": "current_f_app",
            "oldff41": "corrected_l11_app"}[fault]] = True
    elif fault == "no_combined":
        kwargs["combined_app"] = False
    elif fault == "no_creation":
        kwargs["creation_head"] = None
    else:
        kwargs["bounded_l11_app"] = 1
    guard = CopySourceGuard(Path("/CONTROLLED_NOT_READ"), "f" * 40, (), 10 ** 20, **kwargs)
    assert guard(SimpleNamespace(execution_head="f" * 40)) is False


def test_inventory_verification_never_mutates_observed_pins():
    _, pins = operands()
    before = deepcopy(pins)
    require_bounded_l11_app_inventory(pins)
    assert pins == before


@pytest.mark.parametrize("fault", ["consumer_head", "consumer_root", "inventory", "schema", "missing_tool"])
def test_consumer_schema_and_complete_tools_are_still_required(fault):
    approval, pins = operands()
    if fault == "consumer_head":
        approval["consumer_binding"]["consumer_source_head"] = "a" * 40
    elif fault == "consumer_root":
        approval["consumer_binding"]["consumer_root"] = "relative"
    elif fault == "inventory":
        approval["consumer_binding"]["consumer_inventory_sha256"] = "a" * 64
    else:
        if fault == "schema":
            pins["sql/migrations/versions/20261023_atr_aggregate_entry.py"] = "a" * 64
        else:
            del pins["tools/l12_boot_owner_clock.py"]
        reseal(approval, pins)
    with pytest.raises(ValueError):
        bind_composed_execution(approval, observed_root="f" * 40, observed_pins=pins)
