from __future__ import annotations

from pathlib import Path
from typing import get_args

import pytest
from pydantic.fields import FieldInfo

from ops.health import expected_flags_check as flags
from project_mai_tai.settings import Settings


CATALOG = Path(__file__).resolve().parents[2] / "ops" / "health" / "expected_flags.json"


def _entry(name: str, expected: bool, service: str) -> dict[str, object]:
    return {
        "name": name,
        "expected": expected,
        "owning_service": service,
        "reason": "test",
        "ruling": "operator test",
    }


def test_catalog_covers_every_settings_bool_exactly_once() -> None:
    entries = flags.load_catalog(CATALOG)
    assert len(entries) == 120
    assert {entry["name"] for entry in entries} == {
        name
        for name, field in Settings.model_fields.items()
        if field.annotation is bool or bool in get_args(field.annotation)
    }
    by_name = {entry["name"]: entry for entry in entries}
    assert by_name["strategy_schwab_1m_v2_retry_one_enabled"]["expected"] is True
    assert by_name["strategy_schwab_1m_v2_gap_hold_enabled"]["expected"] is True
    assert by_name["orb_schwab_observe_enabled"]["expected"] is True
    assert by_name["orb_live_schwab_orders_enabled"]["expected"] is False
    assert by_name["oms_v2_cw_floor_exit_enabled"]["expected"] is False
    assert by_name["strategy_schwab_1m_v2_atr_massive_seed_enabled"]["expected"] is False
    assert by_name["strategy_schwab_1m_v2_dual_broker_fanout_enabled"][
        "also_check_services"
    ] == ["oms"]


def test_new_unlisted_settings_bool_blocks_catalog(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(
        Settings.model_fields,
        "new_fix_enabled",
        FieldInfo(annotation=bool, default=True),
    )
    with pytest.raises(flags.CatalogError, match="new_fix_enabled"):
        flags.load_catalog(CATALOG)


def test_new_optional_bool_also_blocks_catalog(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(
        Settings.model_fields,
        "new_optional_fix_enabled",
        FieldInfo(annotation=bool | None, default=None),
    )
    with pytest.raises(flags.CatalogError, match="new_optional_fix_enabled"):
        flags.load_catalog(CATALOG)


@pytest.mark.parametrize(
    ("name", "service", "env_key"),
    [
        (
            "strategy_schwab_1m_v2_retry_one_enabled",
            "schwab-1m-v2",
            "MAI_TAI_STRATEGY_SCHWAB_1M_V2_RETRY_ONE_ENABLED",
        ),
        (
            "orb_live_schwab_orders_enabled",
            "orb-schwab",
            "MAI_TAI_ORB_LIVE_SCHWAB_ORDERS_ENABLED",
        ),
        (
            "oms_v2_cw_floor_exit_enabled",
            "oms",
            "MAI_TAI_OMS_V2_CW_FLOOR_EXIT_ENABLED",
        ),
    ],
)
def test_running_fix_and_deliberate_off_drift_are_real_failures(
    name: str, service: str, env_key: str
) -> None:
    expected = name.endswith("retry_one_enabled")
    entry = _entry(name, expected, service)
    actual = "false" if expected else "true"
    rc, lines = flags.audit([entry], lambda _: (1234, {env_key: actual}))
    assert rc == 1
    assert f"REAL FAILURE flag={name} service={service}" in lines[0]
    assert f"running={actual} expected={str(expected).lower()}" in lines[0]
    assert lines[-1].startswith("Final call: REAL FAILURE;")


def test_unreadable_owner_environment_is_unknown_not_pass() -> None:
    def unreadable(_: str) -> tuple[int, dict[str, str]]:
        raise PermissionError("proc denied")

    rc, lines = flags.audit([_entry("oms_v2_cw_floor_exit_enabled", False, "oms")], unreadable)
    assert rc == 2
    assert "UNKNOWN flag=oms_v2_cw_floor_exit_enabled service=oms" in lines[0]
    assert lines[-1].startswith("Final call: UNKNOWN;")


def test_unset_flag_uses_settings_default() -> None:
    name = "strategy_schwab_1m_v2_eh_resting_stream_cross_enabled"
    rc, lines = flags.audit([_entry(name, True, "schwab-1m-v2")], lambda _: (1234, {}))
    assert rc == 0
    assert f"PASS flag={name}" in lines[0]
    assert "source=settings-default" in lines[0]


def test_shared_switch_checks_both_consumers() -> None:
    entry = _entry("strategy_schwab_1m_v2_dual_broker_fanout_enabled", True, "schwab-1m-v2")
    entry["also_check_services"] = ["oms"]

    def process_env(service: str) -> tuple[int, dict[str, str]]:
        return 1234, {
            "MAI_TAI_STRATEGY_SCHWAB_1M_V2_DUAL_BROKER_FANOUT_ENABLED": (
                "true" if service == "schwab-1m-v2" else "false"
            )
        }

    rc, lines = flags.audit([entry], process_env)
    assert rc == 1
    assert "PASS flag=" in lines[0]
    assert "REAL FAILURE flag=" in lines[1]
    assert "service=oms" in lines[1]
    assert "checked=2/2" in lines[-1]


def test_legacy_alias_uses_running_process_value() -> None:
    value, source = flags.flag_value(
        "strategy_polygon_30s_enabled", {"MAI_TAI_STRATEGY_WEBULL_30S_ENABLED": "true"}
    )
    assert value is True
    assert source == "env:MAI_TAI_STRATEGY_WEBULL_30S_ENABLED"


def test_malformed_running_bool_is_unknown() -> None:
    rc, lines = flags.audit(
        [_entry("oms_v2_cw_floor_exit_enabled", False, "oms")],
        lambda _: (1234, {"MAI_TAI_OMS_V2_CW_FLOOR_EXIT_ENABLED": "perhaps"}),
    )
    assert rc == 2
    assert "UNKNOWN flag=oms_v2_cw_floor_exit_enabled" in lines[0]


def test_main_pid_change_during_proc_read_is_unknown(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    pids = iter([1234, 5678])
    monkeypatch.setattr(flags, "_unit_pid", lambda _: next(pids))
    proc = tmp_path / "1234"
    proc.mkdir()
    (proc / "environ").write_bytes(b"MAI_TAI_OMS_V2_CW_FLOOR_EXIT_ENABLED=false\0")
    with pytest.raises(OSError, match="changed during the read"):
        flags.read_service_environment("oms", tmp_path)
