from __future__ import annotations

import importlib.util
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from project_mai_tai.services.schwab_1m_v2_bot import (
    BOOT_RESTORE_WARMUP_TIMEOUT_SECONDS,
    REST_WARMUP_FRESH_THRESHOLD_SECS,
)


MODULE_PATH = Path(__file__).resolve().parents[2] / "ops" / "health" / "v2_restart_evidence.py"
SPEC = importlib.util.spec_from_file_location("v2_restart_evidence", MODULE_PATH)
vre = importlib.util.module_from_spec(SPEC)
sys.modules["v2_restart_evidence"] = vre
SPEC.loader.exec_module(vre)


def test_traceback_is_scoped_by_its_nearest_preceding_timestamp() -> None:
    restart = datetime(2026, 9, 11, 0, 25, tzinfo=UTC)
    evidence = vre.parse_log_files(
        [
            (
                "oms.log",
                [
                    "2026-09-11 00:24:59,999 ERROR old failure",
                    "Traceback (most recent call last):",
                    "  File old.py, line 1",
                    "2026-09-11 00:25:01,001 ERROR new failure",
                    "Traceback (most recent call last):",
                    "  File new.py, line 2",
                    "ValueError: boom",
                    "2026-09-11 00:25:02,002 INFO recovered",
                ],
            )
        ],
        since=restart,
    )

    assert evidence.timestamped_records == 2
    assert evidence.traceback_times_utc == (datetime(2026, 9, 11, 0, 25, 1, tzinfo=UTC),)


def test_traceback_without_a_preceding_timestamp_is_unmeasured() -> None:
    with pytest.raises(vre.EvidenceUnknown, match="before any timestamp"):
        vre.parse_log_files(
            [("strategy.log.1", ["Traceback (most recent call last):"])],
            since=datetime(2026, 9, 11, tzinfo=UTC),
        )


def test_log_timestamp_context_does_not_leak_between_rotations() -> None:
    with pytest.raises(vre.EvidenceUnknown, match="strategy.log contains a traceback"):
        vre.parse_log_files(
            [
                ("strategy.log.1", ["2026-09-11 00:25:01,000 INFO prior file"]),
                ("strategy.log", ["Traceback (most recent call last):"]),
            ],
            since=datetime(2026, 9, 11, tzinfo=UTC),
        )


def test_moments_are_reported_in_et_and_utc() -> None:
    rendered = vre.format_moment(datetime(2026, 9, 11, 0, 25, tzinfo=UTC))
    assert rendered == "2026-09-10 20:25:00 EDT (2026-09-11 00:25:00 UTC)"


def test_systemd_timestamp_parser_refuses_a_non_utc_box_clock() -> None:
    with pytest.raises(vre.EvidenceUnknown, match="unparseable systemd UTC timestamp"):
        vre._parse_systemd_time("Thu 2026-09-10 20:25:00 EDT")


def test_expected_process_flag_requires_service_key_and_value() -> None:
    assert vre._parse_expected_flag("schwab-1m-v2:FEATURE=true") == (
        "schwab-1m-v2",
        "FEATURE",
        "true",
    )
    with pytest.raises(vre.EvidenceUnknown, match="SERVICE:KEY=VALUE"):
        vre._parse_expected_flag("FEATURE=true")


def test_warmup_bound_is_the_deployed_five_minute_contract() -> None:
    assert vre.REST_WARMUP_FRESH_AGE_SECONDS == int(REST_WARMUP_FRESH_THRESHOLD_SECS) == 300
    assert vre.BOOT_WARMUP_FALLBACK_BOUND_SECONDS == int(BOOT_RESTORE_WARMUP_TIMEOUT_SECONDS) == 369


def _report_fixture(monkeypatch, tmp_path: Path):
    start = datetime(2026, 9, 11, 0, 25, tzinfo=UTC)
    old = {
        name: {
            "service": name,
            "pid": index + 100,
            "active_state": "active",
            "sub_state": "running",
            "n_restarts": 0,
            "started_at_utc": datetime(2026, 9, 1, tzinfo=UTC).isoformat(),
        }
        for index, name in enumerate(vre.DEFAULT_SERVICES)
    }
    snapshot = tmp_path / "before.json"
    snapshot.write_text(
        __import__("json").dumps(
            {
                "schema_version": 2,
                "captured_at_utc": datetime(2026, 9, 11, tzinfo=UTC).isoformat(),
                "alembic_version": "20260910_0020",
                "live_exposure": {
                    "accounts_found": 2,
                    "accounts_expected": 2,
                    "open_managed_rows": 0,
                    "nonzero_account_position_rows": 0,
                },
                "services": old,
            }
        ),
        encoding="utf-8",
    )
    current = {
        name: vre.ServiceState(
            service=name,
            pid=(900 if name == vre.V2_SERVICE else row["pid"]),
            active_state="active",
            sub_state="running",
            n_restarts=0,
            started_at_utc=(
                start if name == vre.V2_SERVICE else datetime(2026, 9, 1, tzinfo=UTC)
            ).isoformat(),
        )
        for name, row in old.items()
    }
    monkeypatch.setattr(vre, "service_state", lambda name, runner: current[name])
    monkeypatch.setattr(vre, "_flat_counts", lambda runner: (2, 0, 0))
    monkeypatch.setattr(
        vre,
        "_migration_evidence",
        lambda runner, columns, constraints: ("20260910_0020", 0, 0, []),
    )
    monkeypatch.setattr(
        vre,
        "_process_environment",
        lambda pid, runner: {"MAI_TAI_TEST_FLAG": "true"},
    )
    clean_log = [
        "2026-09-11 00:25:01,000 INFO [V2-BOOT-RESTORE] restoration_complete=1 "
        "evaluated=3 confirmed=3 rest_warmed=3 timeout_released=0 could_not_tell=0",
        "2026-09-11 00:25:02,000 INFO [V2-BOOT-HOLD] released - "
        "restoration_complete=1 reconstructed_uncapped=0",
        "2026-09-11 00:25:03,000 INFO healthy",
    ]
    logs = {vre.V2_SERVICE: [("schwab-1m-v2.log", clean_log)]}
    monkeypatch.setattr(vre, "_log_files", lambda service, runner: logs[service])
    monkeypatch.setattr(vre, "_bar_continuity", lambda runner, restart: (3, 99, 2, 3, 0))
    args = SimpleNamespace(
        snapshot=snapshot,
        restarted=[vre.V2_SERVICE],
        expect_flag=[f"{vre.V2_SERVICE}:MAI_TAI_TEST_FLAG=true"],
        expected_alembic_head="20260910_0020",
        schema_column=[],
        schema_constraint=[],
        no_schema_change=True,
        output=None,
    )
    return args, current, logs


def test_report_contains_every_required_denominator(monkeypatch, tmp_path: Path, capsys) -> None:
    args, _, _ = _report_fixture(monkeypatch, tmp_path)

    assert vre.report(args, runner=lambda command: "") == 0

    output = capsys.readouterr().out
    assert "new active/running PID=1/1" in output
    assert "unchanged PID=8/8" in output
    assert "before resolved=2/2 open managed rows=0 nonzero account-position rows=0" in output
    assert "after resolved=2/2 open managed rows=0 nonzero account-position rows=0" in output
    assert "alembic_version=20260910_0020->20260910_0020" in output
    assert "schema objects=0/0" in output
    assert "matched=1/1" in output and "pid=900" in output
    assert "rest_warmed=3/evaluated=3" in output
    assert "warmup_pending_symbols=-" in output
    assert "fresh_bar_age_bound=300s" in output
    assert "seeded fallback=not used" in output
    assert "release markers=1/3" in output and "restoration_complete=1" in output
    assert "gaps>90s=2/99" in output and "gaps spanning restart=0/2" in output
    assert "headers=0/3" in output and "nearest-preceding timestamp scope" in output
    assert "2026-09-10 20:25:00 EDT (2026-09-11 00:25:00 UTC)" in output


def test_report_fails_if_an_untouched_service_restarted(monkeypatch, tmp_path: Path) -> None:
    args, current, _ = _report_fixture(monkeypatch, tmp_path)
    current["oms"] = vre.ServiceState(
        service="oms",
        pid=901,
        active_state="active",
        sub_state="running",
        n_restarts=0,
        started_at_utc=datetime(2026, 9, 11, 0, 25, tzinfo=UTC).isoformat(),
    )

    assert vre.report(args, runner=lambda command: "") == 1


def test_report_fails_if_restarted_service_has_auto_restarts(monkeypatch, tmp_path: Path) -> None:
    args, current, _ = _report_fixture(monkeypatch, tmp_path)
    current[vre.V2_SERVICE] = vre.ServiceState(
        service=vre.V2_SERVICE,
        pid=900,
        active_state="active",
        sub_state="running",
        n_restarts=1,
        started_at_utc=datetime(2026, 9, 11, 0, 25, tzinfo=UTC).isoformat(),
    )

    assert vre.report(args, runner=lambda command: "") == 1


def test_report_fails_when_pre_restart_snapshot_was_not_flat(monkeypatch, tmp_path: Path) -> None:
    args, _, _ = _report_fixture(monkeypatch, tmp_path)
    payload = json.loads(args.snapshot.read_text(encoding="utf-8"))
    payload["live_exposure"]["open_managed_rows"] = 1
    args.snapshot.write_text(json.dumps(payload), encoding="utf-8")

    assert vre.report(args, runner=lambda command: "") == 1


def test_no_schema_change_fails_if_the_migration_head_moved(monkeypatch, tmp_path: Path) -> None:
    args, _, _ = _report_fixture(monkeypatch, tmp_path)
    monkeypatch.setattr(
        vre,
        "_migration_evidence",
        lambda runner, columns, constraints: ("20260911_0021", 0, 0, []),
    )
    args.expected_alembic_head = "20260911_0021"

    assert vre.report(args, runner=lambda command: "") == 1


def test_declared_migration_requires_head_movement_and_named_schema_evidence(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    args, _, _ = _report_fixture(monkeypatch, tmp_path)
    args.no_schema_change = False
    args.schema_column = ["oms_managed_positions.fanout_slot_id"]
    args.expected_alembic_head = "20260911_0021"
    monkeypatch.setattr(
        vre,
        "_migration_evidence",
        lambda runner, columns, constraints: ("20260911_0021", 1, 1, []),
    )

    assert vre.report(args, runner=lambda command: "") == 0
    output = capsys.readouterr().out
    assert "alembic_version=20260910_0020->20260911_0021" in output
    assert "schema objects found=1/1" in output


def test_snapshot_records_pre_restart_flatness_denominators(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        vre,
        "service_state",
        lambda name, runner: vre.ServiceState(
            service=name,
            pid=100,
            active_state="active",
            sub_state="running",
            n_restarts=0,
            started_at_utc=datetime(2026, 9, 11, tzinfo=UTC).isoformat(),
        ),
    )
    monkeypatch.setattr(vre, "_flat_counts", lambda runner: (2, 0, 0))
    monkeypatch.setattr(
        vre,
        "_migration_evidence",
        lambda runner, columns, constraints: ("20260910_0020", 0, 0, []),
    )
    target = tmp_path / "snapshot.json"

    assert vre.snapshot(target, runner=lambda command: "") is True

    payload = json.loads(target.read_text(encoding="utf-8"))
    assert payload["schema_version"] == 2
    assert payload["alembic_version"] == "20260910_0020"
    assert payload["live_exposure"] == {
        "accounts_found": 2,
        "accounts_expected": 2,
        "open_managed_rows": 0,
        "nonzero_account_position_rows": 0,
    }


def test_snapshot_fails_immediately_when_a_live_account_is_not_flat(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        vre,
        "service_state",
        lambda name, runner: vre.ServiceState(
            service=name,
            pid=100,
            active_state="active",
            sub_state="running",
            n_restarts=0,
            started_at_utc=datetime(2026, 9, 11, tzinfo=UTC).isoformat(),
        ),
    )
    monkeypatch.setattr(vre, "_flat_counts", lambda runner: (2, 1, 0))
    monkeypatch.setattr(
        vre,
        "_migration_evidence",
        lambda runner, columns, constraints: ("20260910_0020", 0, 0, []),
    )

    assert vre.snapshot(tmp_path / "snapshot.json", runner=lambda command: "") is False


def test_report_fails_if_a_traceback_is_after_the_restart(monkeypatch, tmp_path: Path) -> None:
    args, _, logs = _report_fixture(monkeypatch, tmp_path)
    logs[vre.V2_SERVICE][0][1].extend(
        [
            "2026-09-11 00:25:04,000 ERROR loop failed",
            "Traceback (most recent call last):",
        ]
    )

    assert vre.report(args, runner=lambda command: "") == 1


def test_report_refuses_an_inconsistent_warmup_completion(monkeypatch, tmp_path: Path) -> None:
    args, _, logs = _report_fixture(monkeypatch, tmp_path)
    logs[vre.V2_SERVICE][0][1][0] = (
        "2026-09-11 00:25:01,000 INFO [V2-BOOT-RESTORE] restoration_complete=1 "
        "evaluated=3 confirmed=3 rest_warmed=2 timeout_released=0 could_not_tell=0"
    )

    assert vre.report(args, runner=lambda command: "") == 1


def test_timeout_release_requires_matching_bound_and_symbols(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    args, _, logs = _report_fixture(monkeypatch, tmp_path)
    logs[vre.V2_SERVICE][0][1][:1] = [
        "2026-09-11 00:25:01,000 ERROR [V2-BOOT-REST-WARMUP-TIMEOUT] "
        "outcome=warmup_gate_released elapsed_seconds=369.0 bound_seconds=369 "
        "evaluated=3 confirmed=3 released=1 symbols=YYGH",
        "2026-09-11 00:25:01,500 INFO [V2-BOOT-RESTORE] restoration_complete=1 "
        "evaluated=3 confirmed=3 rest_warmed=2 timeout_released=1 could_not_tell=0",
    ]

    assert vre.report(args, runner=lambda command: "") == 0
    output = capsys.readouterr().out
    assert "seeded fallback released=1/3; symbols=YYGH; bound=369s" in output


def test_timeout_release_cannot_claim_more_symbols_than_it_names(
    monkeypatch, tmp_path: Path
) -> None:
    args, _, logs = _report_fixture(monkeypatch, tmp_path)
    logs[vre.V2_SERVICE][0][1][:1] = [
        "2026-09-11 00:25:01,000 ERROR [V2-BOOT-REST-WARMUP-TIMEOUT] "
        "outcome=warmup_gate_released elapsed_seconds=369.0 bound_seconds=369 "
        "evaluated=3 confirmed=3 released=2 symbols=YYGH",
        "2026-09-11 00:25:01,500 INFO [V2-BOOT-RESTORE] restoration_complete=1 "
        "evaluated=3 confirmed=3 rest_warmed=1 timeout_released=2 could_not_tell=0",
    ]

    assert vre.report(args, runner=lambda command: "") == 1
