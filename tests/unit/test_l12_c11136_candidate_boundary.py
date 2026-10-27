"""CONTROLLED source comparisons, never actual corrected-APP qualification."""

from copy import deepcopy

import pytest

from tests.unit.test_l12_final_f_explicit_carry import final_operands
from tools.l12_composed_execution_binding import bind_composed_execution
from tools.l12_private_restore_main import (
    L11_REVIEW_CANDIDATE, inspect_l11_review_candidate, require_current_f_app_inventory,
)


def candidate_operands():
    approval, pins = final_operands()
    pins[L11_REVIEW_CANDIDATE["path"]] = L11_REVIEW_CANDIDATE["after_sha256"]
    return approval, pins


def test_literal_candidate_preserves_whole_F_without_mutating_inventory():
    _, pins = candidate_operands()
    before = deepcopy(pins)
    result = inspect_l11_review_candidate(pins, component_head=L11_REVIEW_CANDIDATE["head"])
    assert result == dict(status="SOURCE_CANDIDATE_ONLY_CORRECTED_APP_QUALIFICATION_PENDING",
        component_head="04052bc73c0d61adc1329e0bb28eb9ee5dae50aa",
        changed_paths=["src/project_mai_tai/services/schwab_1m_v2_bot.py"],
        preserved_component_head="2782fe6b1f8135703203682f4be633aa0c751768",
        executable_carry=None, permission_granted=False)
    assert pins == before


@pytest.mark.parametrize("fault", ["service", "core", "settings", "L12", "extra", "missing"])
def test_candidate_does_not_bless_other_source_or_an_arbitrary_service_hash(fault):
    _, pins = candidate_operands()
    path = {
        "service": L11_REVIEW_CANDIDATE["path"],
        "core": "src/project_mai_tai/strategy_core/schwab_1m_v2.py",
        "settings": "src/project_mai_tai/settings.py",
        "L12": "src/project_mai_tai/brokerdecides1_entry.py",
        "extra": "src/project_mai_tai/UNREVIEWED.py",
        "missing": "src/project_mai_tai/settings.py",
    }[fault]
    if fault == "missing":
        del pins[path]
    else:
        pins[path] = "a" * 64
    with pytest.raises(ValueError):
        inspect_l11_review_candidate(pins, component_head=L11_REVIEW_CANDIDATE["head"])


@pytest.mark.parametrize("head", ["0" * 40, "a" * 40, "HEAD", None])
def test_candidate_requires_literal_component_head_not_new_descendant_or_checkout_alias(head):
    _, pins = candidate_operands()
    with pytest.raises(ValueError):
        inspect_l11_review_candidate(pins, component_head=head)


def test_candidate_report_cannot_be_used_as_composed_execution_approval():
    _, pins = candidate_operands()
    result = inspect_l11_review_candidate(pins, component_head=L11_REVIEW_CANDIDATE["head"])
    with pytest.raises(ValueError):
        bind_composed_execution(result, observed_root=L11_REVIEW_CANDIDATE["head"], observed_pins=pins)


def test_existing_F_executable_profile_still_refuses_candidate_APP():
    _, pins = candidate_operands()
    with pytest.raises(ValueError):
        require_current_f_app_inventory(pins)
