"""Controlled source/refusal contracts, NOT a real restored-copy boot receipt."""

from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from tools.l12_composed_execution_binding import bind_composed_execution
from tools.l12_service_boot_replay import replay_service_boot


def operands():
    fixture = json.loads((Path(__file__).parents[1] / "fixtures/l12_combined_boot_component_controlled.json").read_bytes())
    pins = fixture["inventory"]
    digest = sha256(json.dumps(pins, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    approval = dict(binding_version="L12_COMBINED_06936_GRAPHV2_BOOT_V1",
        execution_root="f" * 40, reviewed_pins=deepcopy(pins), review_sha256="a" * 64,
        app_component=fixture["app_component"], copy_receipt_sha256="b" * 64,
        schema_review_sha256="c" * 64, consumer_binding=dict(
            version="L12_C1_1409_TOOLS_ONLY_COPY_CONSUMER_V1", target_capability_sha256="d" * 64,
            creation_source_head="e" * 40, consumer_source_head="f" * 40,
            consumer_root="/private/tmp/CONTROLLED_NOT_A_REAL_TARGET", consumer_inventory_sha256=digest,
            review_sha256="a" * 64))
    return approval, pins


def test_combined_source_binds_separate_creation_and_consumer_not_permission():
    approval, pins = operands()
    result = bind_composed_execution(approval, observed_root="f" * 40, observed_pins=pins)
    assert result["status"] == "EXPLICIT_COMBINED_SOURCE_BOUND_NOT_BOOT_OR_PERMISSION"
    assert result["execution_root"] == "f" * 40
    assert result["creation_source_head"] == "e" * 40
    assert result["app_component_head"] == "06936d2ecc9469664bddbcf605896272afd00b25"
    assert result["permission_granted"] is False


@pytest.mark.parametrize("fault", ["version", "head", "app", "settings", "missing_APP", "extra_APP",
    "omitted_helper", "missing_ini", "ini", "schema", "copy", "review", "schema_review",
    "nonce", "creation", "consumer", "inventory", "binding_review", "binding_extra", "approval_extra"])
def test_combined_source_refuses_every_unreviewed_or_missing_operand(fault):
    approval, pins = operands()
    binding = approval["consumer_binding"]
    if fault == "version":
        approval["binding_version"] = "UNREVIEWED"
    elif fault == "head":
        approval["execution_root"] = "e" * 40
    elif fault == "app":
        approval["app_component"]["head"] = "e" * 40
    elif fault == "settings":
        pins["src/project_mai_tai/settings.py"] = "e" * 64
    elif fault == "missing_APP":
        pins.pop("src/project_mai_tai/services/keepwatch_bar_owner.py")
    elif fault == "extra_APP":
        pins["src/project_mai_tai/UNREVIEWED.py"] = "e" * 64
    elif fault == "omitted_helper":
        pins.pop("tools/l12_close_line_decode.py")
    elif fault == "missing_ini":
        pins.pop("alembic.ini")
    elif fault == "ini":
        pins["alembic.ini"] = "e" * 64
    elif fault == "schema":
        pins["sql/migrations/env.py"] = "e" * 64
    elif fault in {"copy", "review", "schema_review"}:
        approval[{"copy": "copy_receipt_sha256", "review": "review_sha256", "schema_review": "schema_review_sha256"}[fault]] = "0" * 64
    elif fault == "nonce":
        binding["target_capability_sha256"] = "0" * 64
    elif fault == "creation":
        binding["creation_source_head"] = None
    elif fault == "consumer":
        binding["consumer_source_head"] = "e" * 40
    elif fault == "inventory":
        binding["consumer_inventory_sha256"] = "e" * 64
    elif fault == "binding_review":
        binding["review_sha256"] = "0" * 64
    elif fault == "binding_extra":
        binding["force"] = True
    else:
        approval["force"] = True
    if fault in {"settings", "missing_APP", "extra_APP", "omitted_helper", "missing_ini", "ini", "schema"}:
        # Even internally agreeing observed/reviewed maps cannot waive the
        # literal component, required closure or fixed schema pins.
        approval["reviewed_pins"] = deepcopy(pins)
        binding["consumer_inventory_sha256"] = sha256(json.dumps(pins, sort_keys=True,
            separators=(",", ":")).encode()).hexdigest()
    with pytest.raises(ValueError):
        bind_composed_execution(approval, observed_root="f" * 40, observed_pins=pins)


@pytest.mark.parametrize("legacy", ["migration_sources", "observed_schema", "observed_exclusions"])
def test_combined_mode_cannot_substitute_legacy_schema_operands(legacy):
    approval, pins = operands()
    with pytest.raises(ValueError, match="not_legacy_schema_substitutes"):
        bind_composed_execution(approval, observed_root="f" * 40, observed_pins=pins, **{legacy: {}})


@pytest.mark.asyncio
@pytest.mark.parametrize("profile,keepwatch,cap,message", [
    ("L12_ONLY", True, object(), "l12_only_boot_requires_actual_no_l11"),
    ("COMBINED_L11_L12", False, object(), "combined_v2_boot_requires_actual_keepwatch_ON"),
    ("COMBINED_L11_L12", True, None, "combined_v2_boot_requires_actual_keepwatch_ON")])
async def test_combined_lifecycle_refuses_missing_ON_profile_or_nonce_before_any_target(profile, keepwatch, cap,message):
    approval, _ = operands()
    case = SimpleNamespace(settings=SimpleNamespace(strategy_schwab_1m_v2_keepwatch1_enabled=keepwatch),
        bot=SimpleNamespace())
    with pytest.raises(ValueError, match=message):
        await replay_service_boot(case, None, packet={}, blobs={}, stage_operands=None,
            expected_channel="CONTROLLED", restore_lines=None, startup_profile=profile,
            v2_review={"role_review": {"execution_approval": approval}}, target_capability=cap)


@pytest.mark.asyncio
async def test_combined_nonce_cannot_use_unchecked_factory_or_foreign_capability():
    approval, _ = operands()
    case = SimpleNamespace(settings=SimpleNamespace(strategy_schwab_1m_v2_keepwatch1_enabled=True),
        bot=SimpleNamespace())
    with pytest.raises(ValueError, match="nonce_boot_requires_exact_per_session_checked_factory"):
        await replay_service_boot(case, None, packet={}, blobs={}, stage_operands=None,
            expected_channel="CONTROLLED", restore_lines=None, startup_profile="COMBINED_L11_L12",
            v2_review={"role_review": {"execution_approval": approval}}, target_capability=object())


@pytest.mark.asyncio
async def test_historical_V9_cannot_be_silently_relabelled_combined_ON():
    case = SimpleNamespace(settings=SimpleNamespace(), bot=SimpleNamespace())
    with pytest.raises(ValueError, match="v2_boot_requires_l12_only"):
        await replay_service_boot(case, None, packet={}, blobs={}, stage_operands=None,
            expected_channel="CONTROLLED", restore_lines=None, startup_profile="COMBINED_L11_L12",
            v2_review={"role_review": {"execution_approval": {"binding_version": "L12_WITH_L14_SCHEMA_V9"}}})
