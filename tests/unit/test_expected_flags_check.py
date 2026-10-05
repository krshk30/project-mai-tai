from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
from typing import get_args

import pytest
from pydantic.fields import FieldInfo

from ops.health import expected_flags_check as flags
from project_mai_tai.settings import Settings


CATALOG = Path(__file__).resolve().parents[2] / "ops" / "health" / "expected_flags.json"
NUMERIC_CATALOG = CATALOG.with_name("expected_numeric.json")


def _entry(name: str, expected: bool, service: str) -> dict[str, object]:
    return {
        "name": name,
        "expected": expected,
        "owning_service": service,
        "reason": "test",
        "ruling": "operator test",
    }


def _reading(pid: int, environ: dict[str, str]) -> flags.ServiceEnvironment:
    return flags.ServiceEnvironment(pid, environ, frozenset(), None)


def _proc_reader(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    environ: bytes,
    dotenv: str | None,
):
    proc = tmp_path / "1234"
    proc.mkdir()
    (proc / "environ").write_bytes(environ)
    cwd = proc / "cwd"
    cwd.mkdir()
    if dotenv is not None:
        (cwd / ".env").write_text(dotenv)
    monkeypatch.setattr(flags, "_unit_pid", lambda _: 1234)
    return lambda service: flags.read_service_environment(service, tmp_path)


def test_catalog_covers_every_settings_bool_exactly_once() -> None:
    entries = flags.load_catalog(CATALOG)
    assert len(entries) == 127
    assert {entry["name"] for entry in entries} == {
        name
        for name, field in Settings.model_fields.items()
        if field.annotation is bool or bool in get_args(field.annotation)
    }
    by_name = {entry["name"]: entry for entry in entries}
    assert by_name["strategy_schwab_1m_v2_pm_rest_reprice_enabled"]["expected"] is False
    assert by_name["strategy_schwab_1m_v2_pm_rest_reprice_enabled"]["owning_service"] == "schwab-1m-v2"
    assert by_name["strategy_schwab_1m_v2_atr_reprice_handoff_enabled"]["expected"] is False
    assert by_name["strategy_schwab_1m_v2_atr_reprice_handoff_enabled"]["owning_service"] == "schwab-1m-v2"
    assert by_name["strategy_schwab_1m_v2_retry_one_enabled"]["expected"] is True
    assert by_name["strategy_schwab_1m_v2_gap_hold_enabled"]["expected"] is True
    assert by_name["orb_schwab_observe_enabled"]["expected"] is False
    assert by_name["orb_live_schwab_orders_enabled"]["expected"] is True
    assert by_name["oms_v2_cw_floor_exit_enabled"]["expected"] is False
    assert by_name["oms_v2_cw_target_stay_enabled"]["expected"] is True
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


def test_fixed_dollar_numeric_catalog_covers_both_running_consumers() -> None:
    entries = flags.load_numeric_catalog(NUMERIC_CATALOG)
    assert {entry["name"] for entry in entries} == {
        "strategy_schwab_1m_v2_entry_notional_usd",
        "strategy_schwab_1m_v2_webull_entry_notional_usd",
        "strategy_schwab_1m_v2_entry_max_shares",
        "oms_v2_webull_mirror_quote_max_age_ms",
        "redis_snapshot_batch_stream_maxlen",
    }

    def process_env(service: str) -> flags.ServiceEnvironment:
        assert service in {"schwab-1m-v2", "oms", "market-data"}
        return _reading(1234, {
            "MAI_TAI_STRATEGY_SCHWAB_1M_V2_ENTRY_NOTIONAL_USD": "600",
            "MAI_TAI_STRATEGY_SCHWAB_1M_V2_WEBULL_ENTRY_NOTIONAL_USD": "300",
            "MAI_TAI_REDIS_SNAPSHOT_BATCH_STREAM_MAXLEN": "120",
        })

    rc, lines = flags.audit(entries, process_env)
    assert rc == 0
    assert "checked=8/8" in lines[-1]


def test_missing_or_wrong_live_notional_fails_numeric_gate() -> None:
    entry = next(e for e in flags.load_numeric_catalog(NUMERIC_CATALOG)
                 if e["name"] == "strategy_schwab_1m_v2_entry_notional_usd")
    rc, lines = flags.audit([entry], lambda _: _reading(1234, {}))
    assert rc == 1
    assert "source=settings-default" in lines[0]
    rc, lines = flags.audit([entry], lambda _: _reading(1234, {
        "MAI_TAI_STRATEGY_SCHWAB_1M_V2_ENTRY_NOTIONAL_USD": "601",
    }))
    assert rc == 1
    assert "running=601 expected=600" in lines[0]


def test_new_optional_bool_also_blocks_catalog(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(
        Settings.model_fields,
        "new_optional_fix_enabled",
        FieldInfo(annotation=bool | None, default=None),
    )
    with pytest.raises(flags.CatalogError, match="new_optional_fix_enabled"):
        flags.load_catalog(CATALOG)


def test_removed_settings_bool_blocks_extra_catalog_entry(tmp_path: Path) -> None:
    document = json.loads(CATALOG.read_text())
    document["flags"].append(_entry("removed_fix_enabled", True, "oms"))
    catalog = tmp_path / "expected_flags.json"
    catalog.write_text(json.dumps(document))
    with pytest.raises(flags.CatalogError, match="extra=\\['removed_fix_enabled'\\]"):
        flags.load_catalog(catalog)


def test_duplicate_catalog_name_is_rejected(tmp_path: Path) -> None:
    document = json.loads(CATALOG.read_text())
    document["flags"].append(document["flags"][0].copy())
    catalog = tmp_path / "expected_flags.json"
    catalog.write_text(json.dumps(document))
    with pytest.raises(flags.CatalogError, match="duplicate names"):
        flags.load_catalog(catalog)


def test_non_boolean_expected_is_rejected(tmp_path: Path) -> None:
    document = json.loads(CATALOG.read_text())
    document["flags"][0]["expected"] = "true"
    catalog = tmp_path / "expected_flags.json"
    catalog.write_text(json.dumps(document))
    with pytest.raises(flags.CatalogError, match="expected must be a boolean"):
        flags.load_catalog(catalog)


def test_invalid_catalog_main_is_unknown_not_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    catalog = tmp_path / "bad.json"
    catalog.write_text("not json")
    monkeypatch.setattr(sys, "argv", ["expected_flags_check.py", "--catalog", str(catalog)])
    assert flags.main() == 2
    assert "Final call: UNKNOWN;" in capsys.readouterr().out


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
            "orb_schwab_observe_enabled",
            "orb-schwab",
            "MAI_TAI_ORB_SCHWAB_OBSERVE_ENABLED",
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
    expected = name in {
        "strategy_schwab_1m_v2_retry_one_enabled",
        "orb_live_schwab_orders_enabled",
    }
    entry = _entry(name, expected, service)
    actual = "false" if expected else "true"
    rc, lines = flags.audit([entry], lambda _: _reading(1234, {env_key: actual}))
    assert rc == 1
    assert f"REAL FAILURE flag={name} service={service}" in lines[0]
    assert f"running={actual} expected={str(expected).lower()}" in lines[0]
    assert lines[-1].startswith("Final call: REAL FAILURE;")


def test_unreadable_owner_environment_is_unknown_not_pass() -> None:
    def unreadable(_: str) -> flags.ServiceEnvironment:
        raise PermissionError("proc denied")

    rc, lines = flags.audit([_entry("oms_v2_cw_floor_exit_enabled", False, "oms")], unreadable)
    assert rc == 2
    assert "UNKNOWN flag=oms_v2_cw_floor_exit_enabled service=oms" in lines[0]
    assert lines[-1].startswith("Final call: UNKNOWN;")


def test_unset_flag_uses_settings_default() -> None:
    name = "strategy_schwab_1m_v2_eh_resting_stream_cross_enabled"
    rc, lines = flags.audit([_entry(name, True, "schwab-1m-v2")], lambda _: _reading(1234, {}))
    assert rc == 0
    assert f"PASS flag={name}" in lines[0]
    assert "source=settings-default" in lines[0]


def test_shared_switch_checks_both_consumers() -> None:
    entry = _entry("strategy_schwab_1m_v2_dual_broker_fanout_enabled", True, "schwab-1m-v2")
    entry["also_check_services"] = ["oms"]

    def process_env(service: str) -> flags.ServiceEnvironment:
        return _reading(1234, {
            "MAI_TAI_STRATEGY_SCHWAB_1M_V2_DUAL_BROKER_FANOUT_ENABLED": (
                "true" if service == "schwab-1m-v2" else "false"
            )
        })

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
        lambda _: _reading(1234, {"MAI_TAI_OMS_V2_CW_FLOOR_EXIT_ENABLED": "perhaps"}),
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
    (proc / "cwd").mkdir()
    with pytest.raises(OSError, match="changed during the read"):
        flags.read_service_environment("oms", tmp_path)


def test_process_environment_wins_over_cwd_dotenv(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    reader = _proc_reader(
        monkeypatch, tmp_path,
        b"MAI_TAI_OMS_V2_CW_FLOOR_EXIT_ENABLED=false\0",
        "MAI_TAI_OMS_V2_CW_FLOOR_EXIT_ENABLED=true\n",
    )
    rc, lines = flags.audit([_entry("oms_v2_cw_floor_exit_enabled", False, "oms")], reader)
    assert rc == 0
    assert "source=env:MAI_TAI_OMS_V2_CW_FLOOR_EXIT_ENABLED" in lines[0]


@pytest.mark.parametrize(
    ("name", "dotenv_key"),
    [
        ("oms_v2_cw_floor_exit_enabled", "MAI_TAI_OMS_V2_CW_FLOOR_EXIT_ENABLED"),
        ("strategy_polygon_30s_enabled", "MAI_TAI_STRATEGY_WEBULL_30S_ENABLED"),
    ],
)
def test_dotenv_only_catalogued_flag_is_unknown_even_when_default_matches(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, name: str, dotenv_key: str
) -> None:
    reader = _proc_reader(monkeypatch, tmp_path, b"OTHER=value\0", f"{dotenv_key}=true\n")
    rc, lines = flags.audit([_entry(name, False, "oms")], reader)
    assert rc == 2
    assert f"UNKNOWN flag={name} service=oms" in lines[0]
    assert "cwd/.env" in lines[0]
    assert "absent from process environment" in lines[0]


def test_unreadable_existing_dotenv_is_unknown(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    reader = _proc_reader(monkeypatch, tmp_path, b"OTHER=value\0", "KEY=value\n")
    (tmp_path / "1234" / "cwd" / ".env").unlink()
    (tmp_path / "1234" / "cwd" / ".env").mkdir()
    rc, lines = flags.audit([_entry("oms_v2_cw_floor_exit_enabled", False, "oms")], reader)
    assert rc == 2
    assert "UNKNOWN flag=oms_v2_cw_floor_exit_enabled" in lines[0]


def test_malformed_dotenv_line_is_unknown(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    reader = _proc_reader(
        monkeypatch, tmp_path, b"OTHER=value\0",
        "MAI_TAI_OMS_V2_CW_FLOOR_EXIT_ENABLED='unterminated\n",
    )
    rc, lines = flags.audit([_entry("oms_v2_cw_floor_exit_enabled", False, "oms")], reader)
    assert rc == 2
    assert "malformed" in lines[0]
    assert lines[-1].startswith("Final call: UNKNOWN;")


def test_lowercase_dotenv_flag_is_not_mistaken_for_absent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    reader = _proc_reader(
        monkeypatch, tmp_path, b"OTHER=value\0",
        "mai_tai_oms_v2_cw_floor_exit_enabled=true\n",
    )
    rc, lines = flags.audit([_entry("oms_v2_cw_floor_exit_enabled", False, "oms")], reader)
    assert rc == 2
    assert "absent from process environment" in lines[0]
    assert lines[-1].startswith("Final call: UNKNOWN;")


@pytest.mark.parametrize("service", ["orb", "orb-schwab"])
def test_orb_services_explicitly_ignore_checkout_dotenv(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, service: str
) -> None:
    reader = _proc_reader(
        monkeypatch, tmp_path, b"OTHER=value\0", "MAI_TAI_ORB_LIVE_SCHWAB_ORDERS_ENABLED=true\n"
    )
    rc, lines = flags.audit([_entry("orb_live_schwab_orders_enabled", False, service)], reader)
    assert rc == 0
    assert "source=settings-default" in lines[0]


def test_missing_checkout_dotenv_keeps_settings_default(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    reader = _proc_reader(monkeypatch, tmp_path, b"OTHER=value\0", None)
    rc, lines = flags.audit([_entry("oms_v2_cw_floor_exit_enabled", False, "oms")], reader)
    assert rc == 0
    assert "source=settings-default" in lines[0]


def test_real_mismatch_takes_precedence_over_unreadable_owner() -> None:
    entries = [
        _entry("oms_v2_cw_floor_exit_enabled", False, "oms"),
        _entry("strategy_schwab_1m_v2_retry_one_enabled", True, "schwab-1m-v2"),
    ]

    def reader(service: str) -> flags.ServiceEnvironment:
        if service == "schwab-1m-v2":
            raise PermissionError("proc denied")
        return _reading(1234, {"MAI_TAI_OMS_V2_CW_FLOOR_EXIT_ENABLED": "true"})

    rc, lines = flags.audit(entries, reader)
    assert rc == 1
    assert any("REAL FAILURE flag=oms_v2_cw_floor_exit_enabled" in line for line in lines)
    assert any("UNKNOWN flag=strategy_schwab_1m_v2_retry_one_enabled" in line for line in lines)
    assert lines[-1] == "Final call: REAL FAILURE; checked=1/2 mismatches=1 unknown=1"


def test_inactive_unit_cannot_supply_running_flags(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        flags.subprocess, "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            ["systemctl"], 0, "MainPID=1234\nActiveState=inactive\n", ""
        ),
    )
    with pytest.raises(OSError, match="not active"):
        flags._unit_pid("oms")


def test_duplicate_process_environment_key_is_unknown(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    reader = _proc_reader(
        monkeypatch, tmp_path,
        b"MAI_TAI_OMS_V2_CW_FLOOR_EXIT_ENABLED=false\0"
        b"MAI_TAI_OMS_V2_CW_FLOOR_EXIT_ENABLED=true\0",
        None,
    )
    rc, lines = flags.audit([_entry("oms_v2_cw_floor_exit_enabled", False, "oms")], reader)
    assert rc == 2
    assert "duplicate environment key" in lines[0]
