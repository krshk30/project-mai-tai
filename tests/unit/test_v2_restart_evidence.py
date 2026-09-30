from __future__ import annotations

import importlib.util
import json
import os
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from urllib.error import HTTPError

import pytest

from project_mai_tai.services.schwab_1m_v2_bot import (
    BOOT_RESTORE_WARMUP_TIMEOUT_SECONDS,
    REST_WARMUP_FRESH_THRESHOLD_SECS,
)


MODULE_PATH = Path(
    os.environ.get(
        "V2_RESTART_EVIDENCE_MODULE",
        str(Path(__file__).resolve().parents[2] / "ops" / "health" / "v2_restart_evidence.py"),
    )
)
SPEC = importlib.util.spec_from_file_location("v2_restart_evidence", MODULE_PATH)
vre = importlib.util.module_from_spec(SPEC)
sys.modules["v2_restart_evidence"] = vre
SPEC.loader.exec_module(vre)


_STOP = datetime(2026, 9, 11, 0, 25, tzinfo=UTC)


def _bars(
    symbols,
    pairs,
    gaps,
    brackets,
    spanning,
    excluded=0,
    *,
    pending=(),
    results=(),
) -> "vre.BarContinuity":
    return vre.BarContinuity(
        symbols=symbols,
        pairs=pairs,
        gaps=gaps,
        brackets=brackets,
        spanning=spanning,
        excluded=excluded,
        stopped_at_utc=_STOP,
        live_floor_utc=_STOP - timedelta(seconds=vre.LIVE_AT_STOP_BOUND_SECONDS),
        pending_symbols=tuple(pending),
        gap_results=tuple(results),
    )


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


def test_missing_service_logs_are_unmeasured() -> None:
    with pytest.raises(vre.EvidenceUnknown, match="no log files found for strategy"):
        vre._log_files("strategy", runner=lambda command: "")


def test_log_reader_skips_rotations_proven_older_than_the_restart() -> None:
    restart = datetime(2026, 9, 12, 21, 17, 54, tzinfo=UTC)
    commands: list[list[str]] = []

    def runner(command: list[str]) -> str:
        commands.append(command)
        if command[2] == "find":
            return (
                f"{restart.timestamp() - 1}|"
                "/var/log/project-mai-tai/reconciler.log-20260731.gz\n"
                f"{restart.timestamp()}|/var/log/project-mai-tai/reconciler.log\n"
            )
        assert command[-1] == "/var/log/project-mai-tai/reconciler.log"
        return "2026-09-12 21:17:54,000 INFO started"

    files = vre._log_files("reconciler", runner=runner, since=restart)

    assert files == [
        (
            "/var/log/project-mai-tai/reconciler.log",
            ["2026-09-12 21:17:54,000 INFO started"],
        )
    ]
    assert not any(command[-1].endswith("20260731.gz") for command in commands)


def test_log_reader_reports_zero_records_when_every_rotation_predates_restart() -> None:
    restart = datetime(2026, 9, 12, 21, 17, 54, tzinfo=UTC)

    def runner(command: list[str]) -> str:
        assert command[2] == "find"
        return f"{restart.timestamp() - 1}|/var/log/project-mai-tai/reconciler.log\n"

    files = vre._log_files("reconciler", runner=runner, since=restart)

    assert files == []
    evidence = vre.parse_log_files(files, since=restart)
    assert evidence.timestamped_records == 0
    assert evidence.traceback_times_utc == ()


def test_log_reader_keeps_an_ambiguous_file_at_the_restart_boundary() -> None:
    restart = datetime(2026, 9, 12, 21, 17, 54, tzinfo=UTC)

    def runner(command: list[str]) -> str:
        if command[2] == "find":
            return f"{restart.timestamp()}|/var/log/project-mai-tai/reconciler.log\n"
        return "Traceback (most recent call last):"

    files = vre._log_files("reconciler", runner=runner, since=restart)

    with pytest.raises(vre.EvidenceUnknown, match="before any timestamp"):
        vre.parse_log_files(files, since=restart)


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


def _systemd_runner(values: dict[str, str]):
    def runner(command) -> str:
        return values[command[-2]]

    return runner


def test_deliberately_inactive_service_needs_no_start_timestamp() -> None:
    state = vre.service_state(
        "tv-alerts",
        runner=_systemd_runner(
            {
                "MainPID": "0",
                "NRestarts": "0",
                "ActiveState": "inactive",
                "SubState": "dead",
                "ExecMainStartTimestamp": "",
            }
        ),
    )

    assert state.pid == 0
    assert state.active_state == "inactive"
    assert state.sub_state == "dead"
    assert state.started_at_utc == ""


def test_deliberately_inactive_service_with_a_stale_pid_is_unmeasured() -> None:
    with pytest.raises(vre.EvidenceUnknown, match="no ExecMainStartTimestamp for tv-alerts"):
        vre.service_state(
            "tv-alerts",
            runner=_systemd_runner(
                {
                    "MainPID": "123",
                    "NRestarts": "0",
                    "ActiveState": "inactive",
                    "SubState": "dead",
                    "ExecMainStartTimestamp": "",
                }
            ),
        )


def test_deliberately_inactive_service_in_an_unexpected_substate_is_unmeasured() -> None:
    with pytest.raises(vre.EvidenceUnknown, match="no ExecMainStartTimestamp for tv-alerts"):
        vre.service_state(
            "tv-alerts",
            runner=_systemd_runner(
                {
                    "MainPID": "0",
                    "NRestarts": "0",
                    "ActiveState": "inactive",
                    "SubState": "failed",
                    "ExecMainStartTimestamp": "",
                }
            ),
        )


def test_required_service_without_a_start_timestamp_is_unmeasured() -> None:
    with pytest.raises(vre.EvidenceUnknown, match="no ExecMainStartTimestamp for strategy"):
        vre.service_state(
            "strategy",
            runner=_systemd_runner(
                {
                    "MainPID": "0",
                    "NRestarts": "0",
                    "ActiveState": "inactive",
                    "SubState": "dead",
                    "ExecMainStartTimestamp": "",
                }
            ),
        )


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


@pytest.mark.parametrize(
    ("restart", "expected"),
    [
        (datetime(2026, 9, 12, 15, 11, tzinfo=UTC), False),  # Saturday 11:11 ET
        (datetime(2026, 9, 14, 15, 11, tzinfo=UTC), True),  # Monday 11:11 ET
        (datetime(2026, 11, 26, 15, 11, tzinfo=UTC), False),  # Thanksgiving
    ],
)
def test_restart_bar_session_uses_the_shared_market_calendar(
    restart: datetime, expected: bool
) -> None:
    assert vre._restart_inside_bar_session(restart) is expected


def _report_fixture(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(vre, "_installed_optional_services", lambda runner: ())
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

    def log_files(service, runner, *, since):
        expected = datetime.fromisoformat(current[service].started_at_utc).astimezone(UTC)
        assert since == expected
        return logs[service]

    monkeypatch.setattr(vre, "_log_files", log_files)
    monkeypatch.setattr(vre, "_bar_continuity", lambda runner, restart: _bars(3, 99, 2, 3, 0))
    monkeypatch.setattr(
        vre,
        "_rest_backfill_continuity",
        lambda runner, restart, stopped, lines, before: vre.BackfillContinuity(
            ("TEST",), (), (), "PASS"
        ),
    )
    args = SimpleNamespace(
        snapshot=snapshot,
        restarted=[vre.V2_SERVICE],
        expected_quiet_service=[],
        expect_flag=[f"{vre.V2_SERVICE}:MAI_TAI_TEST_FLAG=true"],
        expected_alembic_head="20260910_0020",
        schema_column=[],
        schema_constraint=[],
        no_schema_change=True,
        output=None,
    )
    return args, current, logs


def _move_v2_report_start(current, logs, start: datetime) -> None:
    current[vre.V2_SERVICE] = vre.ServiceState(
        service=vre.V2_SERVICE,
        pid=900,
        active_state="active",
        sub_state="running",
        n_restarts=0,
        started_at_utc=start.isoformat(),
    )
    stamp = start.strftime("%Y-%m-%d %H:%M:%S")
    logs[vre.V2_SERVICE] = [
        (
            "schwab-1m-v2.log",
            [
                f"{stamp},100 INFO [V2-BOOT-RESTORE] restoration_complete=1 "
                "evaluated=3 confirmed=3 rest_warmed=3 timeout_released=0 could_not_tell=0",
                f"{stamp},200 INFO [V2-BOOT-HOLD] released - "
                "restoration_complete=1 reconstructed_uncapped=0",
                f"{stamp},300 INFO healthy",
            ],
        )
    ]


def _write_install_record(args, tmp_path: Path, *, include_observer: bool = False) -> Path:
    before = json.loads(args.snapshot.read_text(encoding="utf-8"))
    actions = {name: "deliberately_untouched" for name in vre.DEFAULT_SERVICES}
    actions.update({name: "restarted" for name in args.restarted})
    if include_observer:
        actions["orb-schwab"] = "newly_installed"
    path = tmp_path / "install-record.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "snapshot_captured_at_utc": before["captured_at_utc"],
                "source_journal": "/home/trader/fleet_health/deployments-20260929.md",
                "service_actions": actions,
            }
        ),
        encoding="utf-8",
    )
    return path


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
    assert "0 failed checks / 0 unknown checks / 9 reported checks" in output
    assert "Final call: EXPECTED BY DESIGN; restart outside bar session" in output
    assert "2026-09-10 20:25:00 EDT (2026-09-11 00:25:00 UTC)" in output


def test_unreadable_report_has_unknown_final_call(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        vre,
        "build_parser",
        lambda: SimpleNamespace(parse_args=lambda argv: SimpleNamespace(command="report")),
    )

    def unreadable(_args):
        raise vre.EvidenceUnknown("bar history query unavailable")

    monkeypatch.setattr(vre, "report", unreadable)
    assert vre.main([]) == 2
    assert "Final call: UNKNOWN; evidence unreadable: bar history query unavailable" in capsys.readouterr().err


def test_report_allows_a_declared_quiet_service_without_log_records(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    args, current, logs = _report_fixture(monkeypatch, tmp_path)
    start = datetime(2026, 9, 11, 0, 25, tzinfo=UTC)
    current["reconciler"] = vre.ServiceState(
        service="reconciler",
        pid=901,
        active_state="active",
        sub_state="running",
        n_restarts=0,
        started_at_utc=start.isoformat(),
    )
    logs["reconciler"] = []
    args.restarted.append("reconciler")
    args.expected_quiet_service.append("reconciler")

    assert vre.report(args, runner=lambda command: "") == 0

    output = capsys.readouterr().out
    traceback_row = next(line for line in output.splitlines() if line.startswith("| Tracebacks |"))
    assert "reconciler=N/A_EXPECTED_QUIET(0/0)" in traceback_row
    assert "| PARTIAL_N/A |" in traceback_row
    assert "| PASS |" not in traceback_row


def test_report_fails_when_an_unexpected_service_has_no_log_records(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    args, current, logs = _report_fixture(monkeypatch, tmp_path)
    start = datetime(2026, 9, 11, 0, 25, tzinfo=UTC)
    current["oms"] = vre.ServiceState(
        service="oms",
        pid=901,
        active_state="active",
        sub_state="running",
        n_restarts=0,
        started_at_utc=start.isoformat(),
    )
    logs["oms"] = []
    args.restarted.append("oms")

    assert vre.report(args, runner=lambda command: "") == 1

    output = capsys.readouterr().out
    traceback_row = next(line for line in output.splitlines() if line.startswith("| Tracebacks |"))
    assert "oms=UNMEASURED(0/0)" in traceback_row
    assert "| FAIL |" in traceback_row
    assert "unexpected-silent service(s): oms" in output


def test_expected_quiet_declaration_cannot_name_an_untouched_service(
    monkeypatch, tmp_path: Path
) -> None:
    args, _, _ = _report_fixture(monkeypatch, tmp_path)
    args.expected_quiet_service.append("reconciler")

    with pytest.raises(vre.EvidenceUnknown, match="was not declared restarted: reconciler"):
        vre.report(args, runner=lambda command: "")


def test_expected_quiet_service_still_fails_on_a_real_traceback(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    args, current, logs = _report_fixture(monkeypatch, tmp_path)
    start = datetime(2026, 9, 11, 0, 25, tzinfo=UTC)
    current["reconciler"] = vre.ServiceState(
        service="reconciler",
        pid=901,
        active_state="active",
        sub_state="running",
        n_restarts=0,
        started_at_utc=start.isoformat(),
    )
    logs["reconciler"] = [
        (
            "reconciler.log",
            [
                "2026-09-11 00:25:01,000 ERROR failed",
                "Traceback (most recent call last):",
            ],
        )
    ]
    args.restarted.append("reconciler")
    args.expected_quiet_service.append("reconciler")

    assert vre.report(args, runner=lambda command: "") == 1

    output = capsys.readouterr().out
    traceback_row = next(line for line in output.splitlines() if line.startswith("| Tracebacks |"))
    assert "reconciler=1/1" in traceback_row
    assert "| FAIL |" in traceback_row


@pytest.mark.parametrize(
    "start",
    [
        datetime(2026, 9, 12, 15, 11, tzinfo=UTC),
        datetime(2026, 11, 26, 15, 11, tzinfo=UTC),
    ],
)
def test_report_does_not_require_bar_brackets_outside_a_market_session(
    monkeypatch, tmp_path: Path, capsys, start: datetime
) -> None:
    args, current, logs = _report_fixture(monkeypatch, tmp_path)
    _move_v2_report_start(current, logs, start)
    monkeypatch.setattr(vre, "_bar_continuity", lambda runner, restart: _bars(0, 0, 0, 0, 0))

    assert vre.report(args, runner=lambda command: "") == 0
    output = capsys.readouterr().out
    assert "| Bar continuity |" in output
    assert "| N/A_OFF_SESSION |" in output


def test_report_marks_no_watched_symbols_na_during_a_weekday_market_session(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    args, current, logs = _report_fixture(monkeypatch, tmp_path)
    _move_v2_report_start(current, logs, datetime(2026, 9, 14, 15, 11, tzinfo=UTC))
    monkeypatch.setattr(vre, "_bar_continuity", lambda runner, restart: _bars(0, 0, 0, 0, 0))
    monkeypatch.setattr(
        vre,
        "_rest_backfill_continuity",
        lambda runner, restart, stopped, lines, before: vre.BackfillContinuity(
            (), (), (), "N/A", "nothing watched at stop or added after restart"
        ),
    )

    assert vre.report(args, runner=lambda command: "") == 0
    output = capsys.readouterr().out
    assert "nothing watched at stop or added after restart" in output
    assert "| N/A |" in output
    assert "Final call: EXPECTED BY DESIGN; nothing watched at stop or added after restart" in output


def test_report_calls_an_undeclared_service_change_unknown(monkeypatch, tmp_path: Path, capsys) -> None:
    args, current, _ = _report_fixture(monkeypatch, tmp_path)
    current["oms"] = vre.ServiceState(
        service="oms",
        pid=901,
        active_state="active",
        sub_state="running",
        n_restarts=0,
        started_at_utc=datetime(2026, 9, 11, 0, 25, tzinfo=UTC).isoformat(),
    )

    assert vre.report(args, runner=lambda command: "") == 2
    assert (
        "Final call: UNKNOWN; service changed without a declared restart: oms"
        in capsys.readouterr().out
    )


def test_undeclared_automatic_restart_is_a_real_failure(monkeypatch, tmp_path: Path, capsys) -> None:
    args, current, _ = _report_fixture(monkeypatch, tmp_path)
    current["oms"] = vre.ServiceState(
        "oms", 901, "active", "running", 1, datetime(2026, 9, 11, 0, 25, tzinfo=UTC).isoformat()
    )

    assert vre.report(args, runner=lambda command: "") == 1
    output = capsys.readouterr().out
    assert "automatic service restart (NRestarts increased): oms" in output
    assert "Final call: REAL FAILURE" in output


def test_approved_orb_restart_and_new_observer_are_not_a_false_failure(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    args, current, logs = _report_fixture(monkeypatch, tmp_path)
    old = json.loads(args.snapshot.read_text(encoding="utf-8"))
    old["captured_at_utc"] = datetime(2026, 9, 27, tzinfo=UTC).isoformat()
    args.snapshot.write_text(json.dumps(old), encoding="utf-8")
    started = datetime(2026, 9, 30, 0, 11, tzinfo=UTC)
    for name in ("oms", "strategy", "orb"):
        current[name] = vre.ServiceState(
            name, 900 + len(name), "active", "running", 0, started.isoformat()
        )
        logs[name] = [(f"{name}.log", ["2026-09-30 00:11:01,000 INFO healthy"])]
    current["orb-schwab"] = vre.ServiceState(
        "orb-schwab", 1665845, "active", "running", 0, started.isoformat()
    )
    logs["orb-schwab"] = [("orb-schwab.log", ["2026-09-30 00:11:01,000 INFO OBSERVE_ONLY"])]
    monkeypatch.setattr(vre, "_installed_optional_services", lambda runner: ("orb-schwab",))
    args.restarted.extend(("oms", "strategy", "orb"))
    args.new_service = ["orb-schwab"]
    args.expect_flag.extend(
        (
            "orb-schwab:MAI_TAI_ORB_SCHWAB_OBSERVE_ENABLED=true",
            "orb-schwab:MAI_TAI_ORB_LIVE_SCHWAB_ORDERS_ENABLED=false",
        )
    )
    monkeypatch.setattr(
        vre,
        "_process_environment",
        lambda pid, runner: {
            "MAI_TAI_TEST_FLAG": "true",
            "MAI_TAI_ORB_SCHWAB_OBSERVE_ENABLED": "true",
            "MAI_TAI_ORB_LIVE_SCHWAB_ORDERS_ENABLED": "false",
        },
    )

    assert vre.report(args, runner=lambda command: "") == 0
    output = capsys.readouterr().out
    assert "new active/running PID=4/4" in output
    assert "active/running after snapshot=1/1; orb-schwab" in output
    assert "matched=3/3" in output
    assert "Final call: EXPECTED BY DESIGN" in output


def _new_observer_fixture(monkeypatch, tmp_path: Path):
    args, current, logs = _report_fixture(monkeypatch, tmp_path)
    args.new_service = ["orb-schwab"]
    current["orb-schwab"] = vre.ServiceState(
        "orb-schwab", 1665845, "active", "running", 0,
        datetime(2026, 9, 11, 0, 25, tzinfo=UTC).isoformat(),
    )
    logs["orb-schwab"] = [("orb-schwab.log", ["2026-09-11 00:25:01,000 INFO OBSERVE_ONLY"])]
    monkeypatch.setattr(vre, "_installed_optional_services", lambda runner: ("orb-schwab",))
    return args, current, logs


@pytest.mark.parametrize(
    ("active", "start", "n_restarts"),
    [
        ("inactive", datetime(2026, 9, 11, 0, 25, tzinfo=UTC), 0),
        ("active", datetime(2026, 8, 31, 0, 25, tzinfo=UTC), 0),
        ("active", datetime(2026, 9, 11, 0, 25, tzinfo=UTC), 1),
    ],
)
def test_new_observer_must_be_healthy_started_after_snapshot_without_auto_restart(
    monkeypatch, tmp_path: Path, capsys, active: str, start: datetime, n_restarts: int
) -> None:
    args, current, _ = _new_observer_fixture(monkeypatch, tmp_path)
    current["orb-schwab"] = vre.ServiceState(
        "orb-schwab", 1665845, active, "running" if active == "active" else "dead",
        n_restarts, start.isoformat(),
    )
    assert vre.report(args, runner=lambda command: "") == 1
    output = capsys.readouterr().out
    assert "new service orb-schwab did not start active/running after the snapshot" in output
    assert "| New services |" in output
    assert "Final call: REAL FAILURE" in output


def test_declared_new_observer_not_installed_is_unknown(monkeypatch, tmp_path: Path, capsys) -> None:
    args, _, _ = _report_fixture(monkeypatch, tmp_path)
    args.new_service = ["orb-schwab"]
    assert vre.report(args, runner=lambda command: "") == 2
    output = capsys.readouterr().out
    assert "declared new service not installed: orb-schwab" in output
    assert "| New services |" in output


def test_declared_restart_without_snapshot_pid_is_unknown(monkeypatch, tmp_path: Path) -> None:
    args, _, _ = _report_fixture(monkeypatch, tmp_path)
    args.restarted.append("orb-schwab")
    with pytest.raises(vre.EvidenceUnknown, match="no pre-restart PID in snapshot: orb-schwab"):
        vre.report(args, runner=lambda command: "")


def test_service_cannot_be_both_restarted_and_new(monkeypatch, tmp_path: Path) -> None:
    args, _, _ = _report_fixture(monkeypatch, tmp_path)
    args.restarted.append("orb-schwab")
    args.new_service = ["orb-schwab"]
    with pytest.raises(vre.EvidenceUnknown, match="both restarted and newly installed"):
        vre.report(args, runner=lambda command: "")


def test_last_nights_prose_install_record_is_unknown_until_every_unit_is_classified(
    monkeypatch, tmp_path: Path
) -> None:
    args, current, logs = _report_fixture(monkeypatch, tmp_path)
    started = datetime(2026, 9, 30, 0, 11, tzinfo=UTC)
    for name in ("oms", "strategy", "orb"):
        current[name] = vre.ServiceState(name, 900 + len(name), "active", "running", 0, started.isoformat())
        logs[name] = [(f"{name}.log", ["2026-09-30 00:11:01,000 INFO healthy"])]
    current["orb-schwab"] = vre.ServiceState(
        "orb-schwab", 1665845, "active", "running", 0, started.isoformat()
    )
    logs["orb-schwab"] = [("orb-schwab.log", ["2026-09-30 00:11:01,000 INFO OBSERVE_ONLY"])]
    monkeypatch.setattr(vre, "_installed_optional_services", lambda runner: ("orb-schwab",))
    args.restarted.extend(("oms", "strategy", "orb"))
    args.new_service = ["orb-schwab"]
    args.expect_flag.extend(
        (
            "orb-schwab:MAI_TAI_ORB_SCHWAB_OBSERVE_ENABLED=true",
            "orb-schwab:MAI_TAI_ORB_LIVE_SCHWAB_ORDERS_ENABLED=false",
        )
    )
    monkeypatch.setattr(
        vre,
        "_process_environment",
        lambda pid, runner: {
            "MAI_TAI_TEST_FLAG": "true",
            "MAI_TAI_ORB_SCHWAB_OBSERVE_ENABLED": "true",
            "MAI_TAI_ORB_LIVE_SCHWAB_ORDERS_ENABLED": "false",
        },
    )
    # The actual journal names these process changes but never classifies control,
    # market-capture, reconciler, or tv-alerts. Prose is not a complete install record.
    prose = tmp_path / "deployments-20260929.md"
    prose.write_text(
        "| Unit | PID | Start UTC | NRestarts | Action |\n"
        "| --- | ---: | --- | ---: | --- |\n"
        "| OMS | 1663656 | 00:05:36 | 0 | Restarted |\n"
        "| strategy | 1663666 | 00:05:36 | 0 | OMS companion stop/start |\n"
        "| schwab-1m-v2 | 1664453 | 00:08:16 | 0 | Restarted |\n"
        "| ORB paper | 1665228 | 00:09:18 | 0 | Restarted |\n"
        "| ORB-Schwab | 1665845 | 00:11:17 | 0 | Installed/start OBSERVE_ONLY |\n"
        "| market-data | 2202865 | 2026-08-30 19:54:39 | 0 | Unchanged |\n"
        "| Momentum-paper | 2704889 | 2026-09-18 11:27:53 | 0 | Unchanged |\n",
        encoding="utf-8",
    )
    args.install_record = prose
    with pytest.raises(vre.EvidenceUnknown, match="not structured JSON"):
        vre.report(args, runner=lambda command: "")

    args.install_record = _write_install_record(args, tmp_path, include_observer=True)
    record = json.loads(args.install_record.read_text(encoding="utf-8"))
    del record["service_actions"]["control"]
    args.install_record.write_text(json.dumps(record), encoding="utf-8")
    with pytest.raises(vre.EvidenceUnknown, match="missing=control"):
        vre.report(args, runner=lambda command: "")

    args.install_record = _write_install_record(args, tmp_path, include_observer=True)
    assert vre.report(args, runner=lambda command: "") == 0


def test_install_record_rejects_missing_and_conflicting_declarations(
    monkeypatch, tmp_path: Path
) -> None:
    args, _, _ = _report_fixture(monkeypatch, tmp_path)
    args.install_record = None
    with pytest.raises(vre.EvidenceUnknown, match="no complete install record"):
        vre.report(args, runner=lambda command: "")
    args.install_record = _write_install_record(args, tmp_path)
    record = json.loads(args.install_record.read_text(encoding="utf-8"))
    record["service_actions"]["oms"] = "restarted"
    args.install_record.write_text(json.dumps(record), encoding="utf-8")
    with pytest.raises(vre.EvidenceUnknown, match="disagrees"):
        vre.report(args, runner=lambda command: "")


@pytest.mark.parametrize(
    ("change", "expected"),
    [
        ("snapshot_mismatch", "does not match the pre-restart snapshot"),
        ("invalid_action", "invalid=oms"),
        ("extra_unit", "extra=not-a-fleet-unit"),
    ],
)
def test_install_record_rejects_stale_invalid_or_extra_classification(
    monkeypatch, tmp_path: Path, change: str, expected: str
) -> None:
    args, _, _ = _report_fixture(monkeypatch, tmp_path)
    args.install_record = _write_install_record(args, tmp_path)
    record = json.loads(args.install_record.read_text(encoding="utf-8"))
    if change == "snapshot_mismatch":
        record["snapshot_captured_at_utc"] = "2026-09-10T00:00:00+00:00"
    elif change == "invalid_action":
        record["service_actions"]["oms"] = "assumed_untouched"
    else:
        record["service_actions"]["not-a-fleet-unit"] = "deliberately_untouched"
    args.install_record.write_text(json.dumps(record), encoding="utf-8")
    with pytest.raises(vre.EvidenceUnknown, match=expected):
        vre.report(args, runner=lambda command: "")


def test_install_record_cli_has_unknown_route_without_file() -> None:
    args = vre.build_parser().parse_args(
        ["report", "--snapshot", "before.json", "--restarted", "schwab-1m-v2",
         "--expect-flag", "schwab-1m-v2:FLAG=true", "--expected-alembic-head", "head",
         "--no-schema-change"]
    )
    assert args.install_record is None


def test_incomplete_install_record_exits_unknown_not_failure(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    args, _, _ = _report_fixture(monkeypatch, tmp_path)
    args.install_record = _write_install_record(args, tmp_path)
    record = json.loads(args.install_record.read_text(encoding="utf-8"))
    del record["service_actions"]["market-capture"]
    args.install_record.write_text(json.dumps(record), encoding="utf-8")
    monkeypatch.setattr(
        vre,
        "build_parser",
        lambda: SimpleNamespace(parse_args=lambda argv: SimpleNamespace(command="report", **vars(args))),
    )
    assert vre.main([]) == 2
    assert "Final call: UNKNOWN" in capsys.readouterr().err


def test_undeclared_new_observer_is_unknown(monkeypatch, tmp_path: Path, capsys) -> None:
    args, current, _ = _report_fixture(monkeypatch, tmp_path)
    monkeypatch.setattr(vre, "_installed_optional_services", lambda runner: ("orb-schwab",))
    current["orb-schwab"] = vre.ServiceState(
        "orb-schwab", 1665845, "active", "running", 0, datetime(2026, 9, 30, tzinfo=UTC).isoformat()
    )

    assert vre.report(args, runner=lambda command: "") == 2
    assert (
        "installed service missing from snapshot and change declaration: orb-schwab"
        in capsys.readouterr().out
    )


def test_new_observer_already_in_snapshot_is_unknown(monkeypatch, tmp_path: Path) -> None:
    args, current, _ = _report_fixture(monkeypatch, tmp_path)
    payload = json.loads(args.snapshot.read_text(encoding="utf-8"))
    payload["services"]["orb-schwab"] = payload["services"]["orb"]
    args.snapshot.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setattr(vre, "_installed_optional_services", lambda runner: ("orb-schwab",))
    current["orb-schwab"] = vre.ServiceState(
        "orb-schwab", 1665845, "active", "running", 0, datetime(2026, 9, 30, tzinfo=UTC).isoformat()
    )
    args.new_service = ["orb-schwab"]

    assert vre.report(args, runner=lambda command: "") == 2


def test_report_accepts_a_deliberately_inactive_service_that_stays_unchanged(
    monkeypatch, tmp_path: Path
) -> None:
    args, current, _ = _report_fixture(monkeypatch, tmp_path)
    payload = json.loads(args.snapshot.read_text(encoding="utf-8"))
    inactive = {
        "service": "tv-alerts",
        "pid": 0,
        "active_state": "inactive",
        "sub_state": "dead",
        "n_restarts": 0,
        "started_at_utc": "",
    }
    payload["services"]["tv-alerts"] = inactive
    args.snapshot.write_text(json.dumps(payload), encoding="utf-8")
    current["tv-alerts"] = vre.ServiceState(**inactive)

    assert vre.report(args, runner=lambda command: "") == 0


def test_report_fails_if_a_deliberately_inactive_service_starts(
    monkeypatch, tmp_path: Path
) -> None:
    args, current, _ = _report_fixture(monkeypatch, tmp_path)
    payload = json.loads(args.snapshot.read_text(encoding="utf-8"))
    payload["services"]["tv-alerts"] = {
        "service": "tv-alerts",
        "pid": 0,
        "active_state": "inactive",
        "sub_state": "dead",
        "n_restarts": 0,
        "started_at_utc": "",
    }
    args.snapshot.write_text(json.dumps(payload), encoding="utf-8")
    current["tv-alerts"] = vre.ServiceState(
        service="tv-alerts",
        pid=901,
        active_state="active",
        sub_state="running",
        n_restarts=0,
        started_at_utc=datetime(2026, 9, 12, tzinfo=UTC).isoformat(),
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
    monkeypatch.setattr(vre, "_installed_optional_services", lambda runner: ())
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
    monkeypatch.setattr(vre, "_latest_v2_watchlist", lambda runner, now: (now, ("TEST",)))
    target = tmp_path / "snapshot.json"

    assert vre.snapshot(target, runner=lambda command: "") is True

    payload = json.loads(target.read_text(encoding="utf-8"))
    assert payload["schema_version"] == 3
    assert payload["v2_watchlist"]["symbols"] == ["TEST"]
    assert payload["alembic_version"] == "20260910_0020"
    assert payload["live_exposure"] == {
        "accounts_found": 2,
        "accounts_expected": 2,
        "open_managed_rows": 0,
        "nonzero_account_position_rows": 0,
    }


def test_snapshot_tracks_installed_orb_schwab_for_later_restarts(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(vre, "_installed_optional_services", lambda runner: ("orb-schwab",))
    monkeypatch.setattr(
        vre,
        "service_state",
        lambda name, runner: vre.ServiceState(
            name, 100, "active", "running", 0, datetime(2026, 9, 30, tzinfo=UTC).isoformat()
        ),
    )
    monkeypatch.setattr(vre, "_flat_counts", lambda runner: (2, 0, 0))
    monkeypatch.setattr(
        vre, "_migration_evidence", lambda runner, columns, constraints: ("20260910_0020", 0, 0, [])
    )
    monkeypatch.setattr(vre, "_latest_v2_watchlist", lambda runner, now: (now, ("TEST",)))
    target = tmp_path / "snapshot.json"

    assert vre.snapshot(target, runner=lambda command: "") is True
    payload = json.loads(target.read_text(encoding="utf-8"))
    assert "orb-schwab" in payload["services"]
    assert len(payload["services"]) == len(vre.DEFAULT_SERVICES) + 1


def test_optional_service_discovery_distinguishes_installed_from_unreadable() -> None:
    assert vre._installed_optional_services(lambda command: "loaded\n") == ("orb-schwab",)
    assert vre._installed_optional_services(lambda command: "not-found\n") == ()
    with pytest.raises(vre.EvidenceUnknown, match="unreadable unit load state"):
        vre._installed_optional_services(lambda command: "error\n")


def test_snapshot_watchlist_read_ignores_orb_and_requires_fresh_v2() -> None:
    now = datetime(2026, 9, 28, 22, 40, tzinfo=UTC)
    orb = json.dumps(
        {"source_service": "orb", "produced_at": now.isoformat(), "payload": {"watchlist": ["ORB"]}}
    )
    v2 = json.dumps(
        {
            "source_service": "schwab-1m-v2",
            "produced_at": (now - timedelta(seconds=5)).isoformat(),
            "payload": {"watchlist": ["TEST", "ONFO", "TEST"]},
        }
    )
    produced, symbols = vre._latest_v2_watchlist(lambda command: f"{orb}\n{v2}\n", now=now)
    assert produced == now - timedelta(seconds=5)
    assert symbols == ("ONFO", "TEST")
    with pytest.raises(vre.EvidenceUnknown, match="not fresh"):
        vre._latest_v2_watchlist(
            lambda command: v2.replace(
                (now - timedelta(seconds=5)).isoformat(),
                (now - timedelta(seconds=31)).isoformat(),
            ),
            now=now,
        )


def test_snapshot_fails_immediately_when_a_live_account_is_not_flat(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(vre, "_installed_optional_services", lambda runner: ())
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
    monkeypatch.setattr(vre, "_latest_v2_watchlist", lambda runner, now: (now, ()))

    assert vre.snapshot(tmp_path / "snapshot.json", runner=lambda command: "") is False


def test_non_numeric_flat_state_is_unmeasured() -> None:
    with pytest.raises(vre.EvidenceUnknown, match="non-numeric count"):
        vre._flat_counts(runner=lambda command: "2|unknown|0")


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


def _bar_continuity_runner(stopped: str, row: str, details: str = ""):
    """Answer systemd and both psql queries; keep every SQL text for assertions."""
    sql: list[str] = []

    def runner(command) -> str:
        if command[:2] == ["systemctl", "show"]:
            assert command[-3:] == ["-p", "InactiveEnterTimestamp", "--value"]
            assert command[2] == f"{vre.UNIT_PREFIX}{vre.V2_SERVICE}.service"
            return f"{stopped}\n"
        sql.append(command[-1])
        return f"{row if len(sql) == 1 else details}\n"

    return runner, sql


def test_bar_continuity_floors_the_bracketing_pairs_at_the_stop_not_the_start() -> None:
    # The 2026-07-30 shape: v2 stopped 10:12 ET, restarted 11:33 ET. The hole starts at the STOP,
    # so the live-at-stop floor must hang off the stop; a start-derived floor would hide it.
    stop = datetime(2026, 7, 30, 14, 12, 30, tzinfo=UTC)
    start = datetime(2026, 7, 30, 15, 33, 5, tzinfo=UTC)
    details = "\n".join(
        f"GAP|SYM{index}|2026-07-30T14:12:00+00:00|2026-07-30T15:34:00+00:00" for index in range(4)
    )
    runner, sql = _bar_continuity_runner("Thu 2026-07-30 14:12:30 UTC", "4|400|9|4|4|0|0", details)

    bars = vre._bar_continuity(runner, start, aggregate_fetcher=lambda *_: ())

    assert bars.symbols == 4
    assert (bars.pairs, bars.gaps, bars.brackets, bars.spanning, bars.excluded) == (400, 9, 4, 4, 0)
    assert bars.stopped_at_utc == stop
    assert bars.live_floor_utc == stop - timedelta(seconds=180)
    assert [result.verdict for result in bars.gap_results] == ["NO_TRADES_IN_GAP"] * 4
    assert len(sql) == 2
    floor = "prior >= TIMESTAMPTZ '2026-07-30 14:09:30+00'"
    restart = "prior < TIMESTAMPTZ '2026-07-30 15:33:05+00' AND bar_time > TIMESTAMPTZ '2026-07-30 15:33:05+00'"
    assert sql[0].count(f"FILTER (WHERE {restart} AND {floor})") == 1
    assert sql[0].count(f"FILTER (WHERE delta > 90 AND {restart} AND {floor})") == 1
    assert sql[0].count(f"FILTER (WHERE {restart} AND NOT ({floor}))") == 1
    assert "15:30:05+00" not in sql[0], "the floor must not be derived from the start"


def test_bar_continuity_reports_pairs_not_live_at_the_stop_as_excluded() -> None:
    # The 2026-09-15 shape: AIXC unsubscribed 16:44 ET, re-promoted 18:24 ET, v2 restarted 17:40 ET
    # by a plain `systemctl restart` (stop and start in the same second).
    start = datetime(2026, 9, 14, 21, 40, 48, tzinfo=UTC)
    runner, sql = _bar_continuity_runner("Mon 2026-09-14 21:40:48 UTC", "9|1793|43|3|0|1|0")

    bars = vre._bar_continuity(runner, start)

    assert (bars.brackets, bars.spanning, bars.excluded) == (3, 0, 1)
    assert bars.stopped_at_utc == start
    assert bars.live_floor_utc == datetime(2026, 9, 14, 21, 37, 48, tzinfo=UTC)
    assert "prior >= TIMESTAMPTZ '2026-09-14 21:37:48+00'" in sql[0]


def test_bar_continuity_refuses_a_stop_after_the_start() -> None:
    start = datetime(2026, 9, 14, 21, 40, 48, tzinfo=UTC)
    runner, _ = _bar_continuity_runner("Mon 2026-09-14 21:40:49 UTC", "0|0|0|0|0|0|0")
    with pytest.raises(vre.EvidenceUnknown, match="stopped at .* which is after its start"):
        vre._bar_continuity(runner, start)


def test_bar_continuity_without_a_stop_timestamp_is_unmeasured() -> None:
    start = datetime(2026, 9, 14, 21, 40, 48, tzinfo=UTC)
    runner, _ = _bar_continuity_runner("", "0|0|0|0|0|0|0")
    with pytest.raises(vre.EvidenceUnknown, match="no InactiveEnterTimestamp for schwab-1m-v2"):
        vre._bar_continuity(runner, start)


def test_bar_continuity_requires_all_seven_counts() -> None:
    start = datetime(2026, 9, 14, 21, 40, 48, tzinfo=UTC)
    runner, _ = _bar_continuity_runner("Mon 2026-09-14 21:40:48 UTC", "9|1793|43|3|0|1")
    with pytest.raises(vre.EvidenceUnknown, match="returned 6 fields, expected 7"):
        vre._bar_continuity(runner, start)


def test_report_passes_bar_continuity_when_only_excluded_pairs_bracket_the_restart(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    args, _, _ = _report_fixture(monkeypatch, tmp_path)
    monkeypatch.setattr(
        vre, "_bar_continuity", lambda runner, restart: _bars(9, 1793, 43, 3, 0, excluded=1)
    )

    assert vre.report(args, runner=lambda command: "") == 0
    output = capsys.readouterr().out
    assert "pairs bracketing restart=3/1793 (series live at stop)" in output
    assert "gaps spanning restart=0/43" in output
    assert "bracketing pairs NOT live at stop (subscription gaps, excluded)=1" in output
    assert "live-at-stop floor=180s" in output
    assert "v2 stopped at 2026-09-10 20:25:00 EDT (2026-09-11 00:25:00 UTC)" in output


def test_persisted_gap_is_info_when_restart_coverage_is_not_at_issue(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    args, _, _ = _report_fixture(monkeypatch, tmp_path)
    gap = vre.RestartGapResult(
        "TEST",
        datetime(2026, 9, 11, 0, 24, tzinfo=UTC),
        datetime(2026, 9, 11, 0, 27, tzinfo=UTC),
        "HOLE",
        ((datetime(2026, 9, 11, 0, 25, tzinfo=UTC), 2),),
    )
    monkeypatch.setattr(
        vre,
        "_bar_continuity",
        lambda runner, restart: _bars(3, 99, 2, 3, 1, excluded=1, results=(gap,)),
    )

    assert vre.report(args, runner=lambda command: "") == 0
    output = capsys.readouterr().out
    assert "persisted strategy_bar_history INFO" in output
    assert "PERSISTED_GAP_WITH_PRINTS symbol=TEST" in output
    assert "| Bar continuity |" in output and "| N/A_OFF_SESSION |" in output


def test_mnov_restart_gap_passes_when_massive_has_the_identical_no_trade_minutes() -> None:
    # Real 2026-09-17 fixtures. Both sources omit 16:15-16:18 ET.
    v2_minutes_et = (
        "16:06",
        "16:11",
        "16:13",
        "16:14",
        "16:19",
        "16:20",
        "16:22",
        "16:23",
        "16:24",
        "16:25",
        "16:26",
        "16:27",
        "16:28",
        "16:29",
    )
    massive_minutes_et = (
        "16:06",
        "16:11",
        "16:13",
        "16:14",
        "16:19",
        "16:20",
        "16:22",
        "16:23",
        "16:24",
        "16:25",
        "16:26",
        "16:27",
        "16:28",
        "16:29",
    )
    assert massive_minutes_et == v2_minutes_et

    start = datetime(2026, 9, 17, 20, 14, 30, tzinfo=UTC)
    runner, _ = _bar_continuity_runner(
        "Thu 2026-09-17 20:14:30 UTC",
        "21|3586|162|5|1|1|0",
        "GAP|MNOV|2026-09-17T20:14:00+00:00|2026-09-17T20:19:00+00:00",
    )
    calls: list[tuple[str, datetime, datetime]] = []

    def fetch(symbol: str, first: datetime, last: datetime):
        calls.append((symbol, first, last))
        return ()

    bars = vre._bar_continuity(runner, start, aggregate_fetcher=fetch)

    assert calls == [
        (
            "MNOV",
            datetime(2026, 9, 17, 20, 15, tzinfo=UTC),
            datetime(2026, 9, 17, 20, 18, tzinfo=UTC),
        )
    ]
    assert bars.pending_symbols == ()
    assert bars.gap_results[0].verdict == "NO_TRADES_IN_GAP"
    assert bars.gap_results[0].per_minute_counts == tuple(
        (datetime(2026, 9, 17, 20, minute, tzinfo=UTC), 0) for minute in range(15, 19)
    )


def test_report_passes_a_restart_gap_proven_to_have_no_eligible_trades(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    args, _, _ = _report_fixture(monkeypatch, tmp_path)
    result = vre.RestartGapResult(
        "MNOV",
        datetime(2026, 9, 11, 0, 24, tzinfo=UTC),
        datetime(2026, 9, 11, 0, 29, tzinfo=UTC),
        "NO_TRADES_IN_GAP",
        tuple((datetime(2026, 9, 11, 0, minute, tzinfo=UTC), 0) for minute in range(25, 29)),
    )
    monkeypatch.setattr(
        vre,
        "_bar_continuity",
        lambda runner, restart: _bars(1, 20, 1, 1, 1, results=(result,)),
    )

    assert vre.report(args, runner=lambda command: "") == 0
    output = capsys.readouterr().out
    assert "PERSISTED_GAP_NO_ELIGIBLE_PRINTS symbol=MNOV" in output
    assert "eligible_transactions=0" in output
    assert "condition codes unavailable" in output
    assert "later corrections may revise history" in output
    assert "| Bar continuity |" in output and "| PASS |" in output


def test_restart_gap_with_independent_eligible_prints_is_a_hole() -> None:
    start = datetime(2026, 9, 17, 20, 14, 30, tzinfo=UTC)
    runner, _ = _bar_continuity_runner(
        "Thu 2026-09-17 20:14:30 UTC",
        "1|20|1|1|1|0|0",
        "GAP|LIVE|2026-09-17T20:14:00+00:00|2026-09-17T20:19:00+00:00",
    )

    bars = vre._bar_continuity(
        runner,
        start,
        aggregate_fetcher=lambda *_: (
            vre.MinuteAggregate(datetime(2026, 9, 17, 20, 16, tzinfo=UTC), 3),
        ),
    )

    assert bars.gap_results[0].verdict == "HOLE"
    assert dict(bars.gap_results[0].per_minute_counts) == {
        datetime(2026, 9, 17, 20, 15, tzinfo=UTC): 0,
        datetime(2026, 9, 17, 20, 16, tzinfo=UTC): 3,
        datetime(2026, 9, 17, 20, 17, tzinfo=UTC): 0,
        datetime(2026, 9, 17, 20, 18, tzinfo=UTC): 0,
    }


def test_legacy_gap_source_429_is_info_off_session(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    result = vre.RestartGapResult(
        "RATE",
        datetime(2026, 9, 11, 0, 24, tzinfo=UTC),
        datetime(2026, 9, 11, 0, 27, tzinfo=UTC),
        "COULD_NOT_TELL",
        reason="Massive HTTP 429",
    )
    args, _, _ = _report_fixture(monkeypatch, tmp_path)
    monkeypatch.setattr(
        vre,
        "_bar_continuity",
        lambda runner, restart: _bars(1, 20, 1, 1, 1, results=(result,)),
    )

    assert vre.report(args, runner=lambda command: "") == 0
    output = capsys.readouterr().out
    assert "PERSISTED_GAP_UNGRADED symbol=RATE" in output
    assert "Massive HTTP 429" in output
    assert "| N/A_OFF_SESSION |" in output


def test_aggregate_source_failure_is_captured_for_the_exact_gap() -> None:
    start = datetime(2026, 9, 17, 20, 14, 30, tzinfo=UTC)
    runner, _ = _bar_continuity_runner(
        "Thu 2026-09-17 20:14:30 UTC",
        "1|20|1|1|1|0|0",
        "GAP|RATE|2026-09-17T20:14:00+00:00|2026-09-17T20:19:00+00:00",
    )

    def rate_limited(*_args):
        raise vre.AggregateSourceUnknown("Massive HTTP 429")

    bars = vre._bar_continuity(runner, start, aggregate_fetcher=rate_limited)

    assert bars.gap_results[0].verdict == "COULD_NOT_TELL"
    assert bars.gap_results[0].reason == "Massive HTTP 429"


def test_bar_continuity_is_pending_until_a_later_bar_exists(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    args, current, logs = _report_fixture(monkeypatch, tmp_path)
    _move_v2_report_start(current, logs, datetime(2026, 9, 14, 15, 11, tzinfo=UTC))
    monkeypatch.setattr(
        vre,
        "_bar_continuity",
        lambda runner, restart: _bars(1, 10, 0, 0, 0, pending=("WAIT",)),
    )
    monkeypatch.setattr(
        vre,
        "_rest_backfill_continuity",
        lambda runner, restart, stopped, lines, before: vre.BackfillContinuity(
            ("WAIT",), (), (), "COULD_NOT_TELL", "no post-restart current bar"
        ),
    )

    assert vre.report(args, runner=lambda command: "") == 2
    output = capsys.readouterr().out
    assert "REST backfill continuity UNKNOWN: no post-restart current bar" in output
    assert "pending next bar=1 symbols=WAIT" in output
    assert "| COULD_NOT_TELL |" in output
    assert "Final call: UNKNOWN; REST backfill continuity UNKNOWN: no post-restart current bar" in output
    assert "Overall: UNKNOWN (0 failed checks / 1 unknown checks / 9 reported checks)" in output
    assert "Failures:" not in output


def test_bar_continuity_query_marks_a_live_symbol_without_a_later_bar_pending() -> None:
    start = datetime(2026, 9, 14, 15, 11, tzinfo=UTC)
    runner, sql = _bar_continuity_runner(
        "Mon 2026-09-14 15:11:00 UTC",
        "1|12|0|0|0|0|1",
        "PENDING|WAIT|2026-09-14T15:10:00+00:00|",
    )

    bars = vre._bar_continuity(
        runner,
        start,
        aggregate_fetcher=lambda *_: pytest.fail("pending rows must not fetch Massive"),
    )

    assert bars.pending_symbols == ("WAIT",)
    assert bars.gap_results == ()
    assert len(sql) == 2


def test_monday_no_watched_symbols_and_no_late_additions_is_na(monkeypatch) -> None:
    restart = datetime(2026, 9, 28, 22, 42, 25, tzinfo=UTC)
    pre = "2026-09-28 22:40:00,000 INFO schwab_1m_v2 watchlist updated count=0 sample= warmed=0"
    monkeypatch.setattr(vre, "_log_files", lambda service, runner, since: [("v2.log", [pre])])

    result = vre._rest_backfill_continuity(
        lambda command: "", restart, restart, (), {"schema_version": 2}
    )

    assert result.verdict == "N/A"
    assert result.watched_at_stop == result.added_after_restart == ()


def _bar_row(minute: int, *, written_minute: int = 15):
    bar = datetime(2026, 9, 28, 14, minute, tzinfo=UTC)
    written = datetime(2026, 9, 28, 14, written_minute, 20, tzinfo=UTC)
    return bar, written, written


def test_mid_session_rest_fill_covers_each_minute_before_fresh_bar() -> None:
    restart = datetime(2026, 9, 28, 14, 12, 30, tzinfo=UTC)
    result = vre._grade_backfill_symbol(
        "TEST",
        watched_at_stop=True,
        start_utc=restart,
        restart_utc=restart,
        first_live_utc=datetime(2026, 9, 28, 14, 15, tzinfo=UTC),
        rows=(_bar_row(12, written_minute=12), _bar_row(13), _bar_row(14), _bar_row(15)),
        fetcher=lambda *_: pytest.fail("a complete fill must not need an independent tape read"),
    )

    assert result.verdict == "PASS"
    assert [minute.minute for minute in result.filled_minutes] == [13, 14]
    assert result.missing_print_minutes == ()


def test_rest_fill_missing_a_printed_minute_blocks_the_gate(monkeypatch, tmp_path: Path, capsys) -> None:
    restart = datetime(2026, 9, 28, 14, 12, 30, tzinfo=UTC)
    missing = datetime(2026, 9, 28, 14, 14, tzinfo=UTC)
    result = vre._grade_backfill_symbol(
        "TEST",
        watched_at_stop=True,
        start_utc=restart,
        restart_utc=restart,
        first_live_utc=datetime(2026, 9, 28, 14, 15, tzinfo=UTC),
        rows=(_bar_row(12, written_minute=12), _bar_row(13), _bar_row(15)),
        fetcher=lambda *_: (vre.MinuteAggregate(missing, 2),),
    )
    assert result.verdict == "MISSING_PRINTED_MINUTE"
    assert result.missing_print_minutes == (missing,)

    args, current, logs = _report_fixture(monkeypatch, tmp_path)
    _move_v2_report_start(current, logs, restart)
    monkeypatch.setattr(
        vre,
        "_rest_backfill_continuity",
        lambda runner, started, stopped, lines, before: vre.BackfillContinuity(
            ("TEST",), (), (result,), "MISSING_PRINTED_MINUTE"
        ),
    )
    assert vre.report(args, runner=lambda command: "") == 1
    output = capsys.readouterr().out
    assert "REST backfill coverage not proven for 1 independently printed minute(s)" in output
    assert "filled=1/2 missing_printed=1" in output
    assert "minutes=2026-09-28 10:14:00 EDT" in output
    assert "Final call: REAL FAILURE; REST backfill coverage not proven" in output
    assert "Overall: REAL FAILURE (1 failed checks / 0 unknown checks / 9 reported checks)" in output


def test_unreadable_tape_is_unknown_not_a_clean_minute() -> None:
    def unreadable(*_args):
        raise vre.AggregateSourceUnknown("Massive HTTP 429")

    result = vre._grade_backfill_symbol(
        "TEST",
        watched_at_stop=True,
        start_utc=datetime(2026, 9, 28, 14, 12, 30, tzinfo=UTC),
        restart_utc=datetime(2026, 9, 28, 14, 12, 30, tzinfo=UTC),
        first_live_utc=datetime(2026, 9, 28, 14, 15, tzinfo=UTC),
        rows=(_bar_row(12, written_minute=12), _bar_row(13), _bar_row(15)),
        fetcher=unreadable,
    )

    assert result.verdict == "COULD_NOT_TELL"
    assert "HTTP 429" in result.reason


def test_old_rest_replay_not_persisted_is_unknown_not_a_proven_hole(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    old_print = datetime(2026, 9, 28, 14, 13, tzinfo=UTC)
    result = vre._grade_backfill_symbol(
        "TEST",
        watched_at_stop=True,
        start_utc=datetime(2026, 9, 28, 14, 12, 30, tzinfo=UTC),
        restart_utc=datetime(2026, 9, 28, 14, 12, 30, tzinfo=UTC),
        first_live_utc=datetime(2026, 9, 28, 14, 50, tzinfo=UTC),
        first_live_seen_utc=datetime(2026, 9, 28, 14, 50, 20, tzinfo=UTC),
        rows=(_bar_row(12, written_minute=12), _bar_row(50, written_minute=50)),
        fetcher=lambda *_: (vre.MinuteAggregate(old_print, 3),),
    )
    assert result.verdict == "COULD_NOT_TELL"
    assert result.unverified_minutes == (old_print,)
    assert result.missing_print_minutes == ()

    args, current, logs = _report_fixture(monkeypatch, tmp_path)
    _move_v2_report_start(current, logs, datetime(2026, 9, 28, 14, 12, 30, tzinfo=UTC))
    monkeypatch.setattr(
        vre,
        "_rest_backfill_continuity",
        lambda runner, started, stopped, lines, before: vre.BackfillContinuity(
            ("TEST",), (), (result,), "COULD_NOT_TELL"
        ),
    )
    assert vre.report(args, runner=lambda command: "") == 2
    output = capsys.readouterr().out
    assert "Final call: UNKNOWN; REST backfill continuity UNKNOWN: TEST: " in output
    assert "older REST replay may exist only in strategy memory" in output
    assert "unverified_old=1" in output
    assert "Failures:" not in output


def test_historical_replay_row_cannot_end_the_coverage_window() -> None:
    result = vre._grade_backfill_symbol(
        "TEST",
        watched_at_stop=True,
        start_utc=datetime(2026, 9, 28, 14, 12, 30, tzinfo=UTC),
        restart_utc=datetime(2026, 9, 28, 14, 12, 30, tzinfo=UTC),
        first_live_utc=None,
        rows=(_bar_row(12, written_minute=12), _bar_row(13)),
        fetcher=lambda *_: (),
    )

    assert result.verdict == "COULD_NOT_TELL"
    assert "fresh post-restart warmup" in result.reason


def test_watched_at_stop_uses_fresh_rest_marker_not_historical_replay(monkeypatch) -> None:
    restart = datetime(2026, 9, 28, 14, 12, 30, tzinfo=UTC)
    marker_at = datetime(2026, 9, 28, 14, 15, 0, 500000, tzinfo=UTC)
    post_lines = (
        (
            datetime(2026, 9, 28, 14, 12, 31, tzinfo=UTC),
            "2026-09-28 14:12:31,000 INFO schwab_1m_v2 watchlist updated "
            "count=1 sample=TEST warmed=0",
        ),
        (
            marker_at,
            "2026-09-28 14:15:00,500 INFO [V2-REST-WARMED] schwab_v2 REST "
            "fresh-source warmup complete for TEST (bar_age_seconds=0.500 "
            "bound_seconds=300 warmed=1/1)",
        ),
    )
    monkeypatch.setattr(vre, "_log_files", lambda service, runner, since: [("v2.log", [])])
    monkeypatch.setattr(
        vre,
        "_symbol_bar_rows",
        lambda symbol, session, runner: (
            _bar_row(12, written_minute=12),
            _bar_row(13),
            _bar_row(14),
            _bar_row(15),
        ),
    )
    result = vre._rest_backfill_continuity(
        lambda command: "",
        restart,
        restart,
        post_lines,
        {
            "schema_version": 3,
            "v2_watchlist": {
                "produced_at_utc": datetime(2026, 9, 28, 14, 12, tzinfo=UTC).isoformat(),
                "symbols": ["TEST"],
            },
        },
        aggregate_fetcher=lambda *_: pytest.fail("all minutes are covered"),
    )
    assert result.verdict == "PASS"
    assert result.watched_at_stop == ("TEST",)
    assert result.symbols[0].first_post_utc == datetime(2026, 9, 28, 14, 15, tzinfo=UTC)
    assert [minute.minute for minute in result.symbols[0].filled_minutes] == [13, 14]


def test_late_addition_without_current_bar_is_unknown_not_na(monkeypatch) -> None:
    restart = datetime(2026, 9, 28, 22, 42, 25, tzinfo=UTC)
    pre = "2026-09-28 22:40:00,000 INFO schwab_1m_v2 watchlist updated count=0 sample= warmed=0"
    monkeypatch.setattr(vre, "_log_files", lambda service, runner, since: [("v2.log", [pre])])
    monkeypatch.setattr(vre, "_symbol_bar_rows", lambda symbol, session, runner: ())
    added = (
        datetime(2026, 9, 28, 22, 58, tzinfo=UTC),
        "2026-09-28 22:58:00,000 INFO schwab_1m_v2 watchlist updated "
        "count=1 sample=ONFO warmed=0",
    )
    result = vre._rest_backfill_continuity(
        lambda command: "", restart, restart, (added,), {"schema_version": 2},
        aggregate_fetcher=lambda *_: (),
    )
    assert result.added_after_restart == ("ONFO",)
    assert result.verdict == "COULD_NOT_TELL"
    assert "no fresh post-restart warmup bar" in result.symbols[0].reason


def test_late_addition_with_a_current_rest_bar_has_zero_gap_minutes(monkeypatch) -> None:
    restart = datetime(2026, 9, 28, 22, 42, 25, tzinfo=UTC)
    pre = "2026-09-28 22:40:00,000 INFO schwab_1m_v2 watchlist updated count=0 sample= warmed=0"
    monkeypatch.setattr(vre, "_log_files", lambda service, runner, since: [("v2.log", [pre])])
    monkeypatch.setattr(
        vre,
        "_symbol_bar_rows",
        lambda symbol, session, runner: (
            (
                datetime(2026, 9, 28, 22, 58, tzinfo=UTC),
                datetime(2026, 9, 28, 22, 58, 30, tzinfo=UTC),
                datetime(2026, 9, 28, 22, 58, 30, tzinfo=UTC),
            ),
        ),
    )
    post_lines = (
        (
            datetime(2026, 9, 28, 22, 58, tzinfo=UTC),
            "2026-09-28 22:58:00,000 INFO schwab_1m_v2 watchlist updated "
            "count=1 sample=ONFO warmed=0",
        ),
        (
            datetime(2026, 9, 28, 22, 58, 10, tzinfo=UTC),
            "2026-09-28 22:58:10,000 INFO [V2-REST-WARMED] schwab_v2 REST "
            "fresh-source warmup complete for ONFO (bar_age_seconds=10.000 "
            "bound_seconds=300 warmed=1/1)",
        ),
    )
    result = vre._rest_backfill_continuity(
        lambda command: "", restart, restart, post_lines, {"schema_version": 2},
        aggregate_fetcher=lambda *_: pytest.fail("zero gap minutes need no tape read"),
    )
    assert result.verdict == "PASS"
    assert result.added_after_restart == ("ONFO",)
    assert result.symbols[0].filled_minutes == ()


def test_massive_http_429_is_not_an_empty_clean_tape(monkeypatch) -> None:
    def fail(*_args, **_kwargs):
        raise HTTPError("https://api.massive.com", 429, "rate limited", None, None)

    monkeypatch.setattr(vre, "urlopen", fail)
    with pytest.raises(vre.AggregateSourceUnknown, match="HTTP 429"):
        vre._fetch_massive_minute_aggregates(
            "MNOV",
            datetime(2026, 9, 17, 20, 15, tzinfo=UTC),
            datetime(2026, 9, 17, 20, 18, tzinfo=UTC),
            api_key="secret",
        )


def test_massive_empty_response_is_not_an_empty_clean_tape(monkeypatch) -> None:
    class EmptyResponse:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return b""

    monkeypatch.setattr(vre, "urlopen", lambda *_args, **_kwargs: EmptyResponse())
    with pytest.raises(vre.AggregateSourceUnknown, match="empty response body"):
        vre._fetch_massive_minute_aggregates(
            "MNOV",
            datetime(2026, 9, 17, 20, 15, tzinfo=UTC),
            datetime(2026, 9, 17, 20, 18, tzinfo=UTC),
            api_key="secret",
        )


def test_massive_zero_transaction_row_is_not_clean_no_trades_evidence(monkeypatch) -> None:
    # #1016 pin. NO_TRADES_IN_GAP is proven by Massive returning NO row for a minute. A row that IS
    # present but says n=0 is a shape Massive does not produce for an empty minute, so it must read
    # as "source unknown" - never as a quiet minute that passes the restart gate.
    in_gap_ms = int(datetime(2026, 9, 17, 20, 16, tzinfo=UTC).timestamp() * 1000)

    class ZeroTransactionResponse:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return json.dumps(
                {"status": "OK", "resultsCount": 1, "results": [{"t": in_gap_ms, "n": 0, "v": 0}]}
            ).encode()

    monkeypatch.setattr(vre, "urlopen", lambda *_args, **_kwargs: ZeroTransactionResponse())
    with pytest.raises(vre.AggregateSourceUnknown, match="duplicate or empty aggregate minutes"):
        vre._fetch_massive_minute_aggregates(
            "MNOV",
            datetime(2026, 9, 17, 20, 15, tzinfo=UTC),
            datetime(2026, 9, 17, 20, 18, tzinfo=UTC),
            api_key="secret",
        )


def test_massive_gap_lookup_refuses_regular_session_before_reading_credentials() -> None:
    commands: list[list[str]] = []
    fetch = vre._massive_gap_fetcher(
        lambda command: commands.append(command) or "",
        clock=lambda: datetime(2026, 9, 21, 15, 0, tzinfo=UTC),  # 11:00 ET Monday
    )

    with pytest.raises(vre.AggregateSourceUnknown, match="refused during 09:30-16:00 ET"):
        fetch(
            "MNOV",
            datetime(2026, 9, 17, 20, 15, tzinfo=UTC),
            datetime(2026, 9, 17, 20, 18, tzinfo=UTC),
        )
    assert commands == []
