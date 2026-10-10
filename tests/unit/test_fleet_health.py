"""F3 fleet function-health — unit-tests the pure verdict logic of the independent
check script (ops/health/fleet_health_check.py). Loaded by path (the script is stdlib-only
and imports NO app code, so it stays independent/unhangable); we test only its decision
functions, not the psql/redis I/O. The load-bearing property proven here is the
NO-FALSE-ALARM discipline: stale bars are RED only when the upstream feed is simultaneously
live (a frozen loop) — never on a quiet market / feed outage."""

from __future__ import annotations

import importlib.util
from datetime import UTC, date, datetime
from pathlib import Path
import subprocess

_MOD_PATH = Path(__file__).resolve().parents[2] / "ops" / "health" / "fleet_health_check.py"


def _load():
    spec = importlib.util.spec_from_file_location("fleet_health_check", _MOD_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


fhc = _load()


def _service_states(
    *,
    restart_overrides: dict[str, int] | None = None,
    state_overrides: dict[str, tuple[str, str]] | None = None,
) -> dict[str, fhc.ServiceRuntime]:
    restart_overrides = restart_overrides or {}
    state_overrides = state_overrides or {}
    return {
        service: fhc.ServiceRuntime(
            n_restarts=restart_overrides.get(service, 0),
            active_state=state_overrides.get(service, ("active", "running"))[0],
            sub_state=state_overrides.get(service, ("active", "running"))[1],
        )
        for service in fhc.RUNTIME_SERVICES
    }


def _systemctl_output(states: dict[str, fhc.ServiceRuntime]) -> str:
    return "\n\n".join(
        "\n".join(
            (
                f"NRestarts={state.n_restarts}",
                f"Id={service}",
                f"ActiveState={state.active_state}",
                f"SubState={state.sub_state}",
            )
        )
        for service, state in states.items()
    )


def test_retired_strategy_bar_freshness_check_is_absent() -> None:
    # HEALTHNOISE1: polygon_30s is off on the box (last bar 2026-09-02); its check is retired.
    assert not hasattr(fhc, "check_strategy_bar_freshness")
    assert not hasattr(fhc, "classify_bar_freshness")
    assert "check_strategy_bar_freshness" not in {spec.check.__name__ for spec in fhc.CHECKS}


# --- service runtime: every expected unit, around the clock ----------------------------- #


def test_runtime_inventory_includes_momentum_and_all_continuous_project_services() -> None:
    assert set(fhc.RUNTIME_SERVICES) == {
        "project-mai-tai-control.service",
        "project-mai-tai-market-capture.service",
        "project-mai-tai-market-data.service",
        "project-mai-tai-momentum-paper.service",
        "project-mai-tai-oms.service",
        "project-mai-tai-orb-schwab.service",
        "project-mai-tai-reconciler.service",
        "project-mai-tai-schwab-1m-v2.service",
        "project-mai-tai-strategy.service",
    }


def test_0355_to_0400_restart_storm_is_red() -> None:
    momentum = "project-mai-tai-momentum-paper.service"
    current = _service_states(restart_overrides={momentum: 5})
    previous = {service: 0 for service in fhc.RUNTIME_SERVICES}

    level, detail = fhc.classify_service_restarts(current, previous, elapsed_s=300)

    assert level == "RED"
    assert f"{momentum} +5" in detail
    assert "delta>3 within 300s" in detail


def test_healthy_services_and_three_or_fewer_restarts_do_not_page() -> None:
    momentum = "project-mai-tai-momentum-paper.service"
    current = _service_states(restart_overrides={momentum: 3})
    previous = {service: 0 for service in fhc.RUNTIME_SERVICES}

    level, detail = fhc.classify_service_restarts(current, previous, elapsed_s=300)

    assert level == "GREEN"
    assert "all 9 services active" in detail


def test_runtime_check_persists_baseline_then_detects_the_next_five_minute_delta(
    tmp_path: Path,
) -> None:
    momentum = "project-mai-tai-momentum-paper.service"
    states = _service_states()

    def runner(*_args, **_kwargs):
        return subprocess.CompletedProcess(
            args=["systemctl"],
            returncode=0,
            stdout=_systemctl_output(states),
            stderr="",
        )

    state_path = tmp_path / "restart-counts.json"
    first = fhc.check_service_restart_storms(
        now_epoch=1000,
        state_path=state_path,
        runner=runner,
    )
    states[momentum] = fhc.ServiceRuntime(5, "active", "running")
    second = fhc.check_service_restart_storms(
        now_epoch=1300,
        state_path=state_path,
        runner=runner,
    )

    assert not [row for row in first if row[0] == "RED"]
    momentum_storm = next(
        row for row in second if row[1] == "service-runtime:momentum-paper:restart-storm"
    )
    assert momentum_storm[0] == "RED"
    assert "delta=5" in momentum_storm[2]


def test_market_data_1008_is_red_from_socket_evidence_even_if_a_heartbeat_says_healthy(
    tmp_path: Path,
) -> None:
    market = tmp_path / "market-data.log"
    momentum = tmp_path / "momentum-paper.log"
    state = tmp_path / "socket-offsets.json"
    market.write_text(
        "heartbeat status=healthy\nMassive websocket error: received 1008 (policy violation)\n",
        encoding="utf-8",
    )
    momentum.write_text("heartbeat status=healthy\n", encoding="utf-8")

    rows = fhc.check_massive_socket_policy_violations(
        state_path=state,
        market_data_log=market,
        momentum_log=momentum,
    )

    market_row = next(row for row in rows if row[1].endswith("market-data:massive-1008"))
    assert market_row[0] == "RED"
    assert "new_matches=1" in market_row[2]


def test_market_data_policy_detector_ignores_info_counts_but_sees_vendor_close(
    tmp_path: Path,
) -> None:
    market = tmp_path / "market-data.log"
    momentum = tmp_path / "momentum-paper.log"
    state = tmp_path / "socket-offsets.json"
    market.write_text(
        "published snapshot batch with 11008 records\n"
        "subscription count=1008 -> 1008 symbols\n",
        encoding="utf-8",
    )
    momentum.write_text("", encoding="utf-8")

    ordinary = fhc.check_massive_socket_policy_violations(
        state_path=state, market_data_log=market, momentum_log=momentum,
    )
    ordinary_row = next(row for row in ordinary if row[1].endswith("market-data:massive-1008"))
    assert ordinary_row[0] == "GREEN"

    with market.open("a", encoding="utf-8") as stream:
        stream.write("ConnectionClosedError: sent 1008 (policy violation)\n")
    violation = fhc.check_massive_socket_policy_violations(
        state_path=state, market_data_log=market, momentum_log=momentum,
    )
    violation_row = next(row for row in violation if row[1].endswith("market-data:massive-1008"))
    assert violation_row[0] == "RED"
    assert "new_matches=1" in violation_row[2]


def test_socket_evidence_is_incremental_and_a_new_1008_rearms_after_a_clean_sample(
    tmp_path: Path,
) -> None:
    market = tmp_path / "market-data.log"
    momentum = tmp_path / "momentum-paper.log"
    state = tmp_path / "socket-offsets.json"
    market.write_text("received 1008 (policy violation)\n", encoding="utf-8")
    momentum.write_text("", encoding="utf-8")

    first = fhc.check_massive_socket_policy_violations(
        state_path=state,
        market_data_log=market,
        momentum_log=momentum,
    )
    second = fhc.check_massive_socket_policy_violations(
        state_path=state,
        market_data_log=market,
        momentum_log=momentum,
    )
    with market.open("a", encoding="utf-8") as stream:
        stream.write("received 1008 (policy violation)\n")
    third = fhc.check_massive_socket_policy_violations(
        state_path=state,
        market_data_log=market,
        momentum_log=momentum,
    )

    def market_level(rows):
        return next(row[0] for row in rows if row[1].endswith("market-data:massive-1008"))

    assert [market_level(rows) for rows in (first, second, third)] == [
        "RED",
        "GREEN",
        "RED",
    ]


def test_retired_paper_policy_markers_do_not_latch_or_clear_gateway_evidence(tmp_path: Path) -> None:
    market = tmp_path / "market-data.log"
    momentum = tmp_path / "momentum-paper.log"
    state = tmp_path / "socket-offsets.json"
    market.write_text("", encoding="utf-8")
    momentum.write_text(
        "\n".join(
            f"received 1008 (policy violation) consecutive={number}"
            for number in range(1, 5)
        )
        + "\n",
        encoding="utf-8",
    )

    first = fhc.check_massive_socket_policy_violations(
        state_path=state,
        market_data_log=market,
        momentum_log=momentum,
    )
    with momentum.open("a", encoding="utf-8") as stream:
        stream.write(
            "[MOMENTUM-PAPER-FEED-POLICY] decision=cooloff "
            "reason=feed_policy_violation consecutive_1008=5\n"
        )
    second = fhc.check_massive_socket_policy_violations(
        state_path=state,
        market_data_log=market,
        momentum_log=momentum,
    )
    third = fhc.check_massive_socket_policy_violations(
        state_path=state,
        market_data_log=market,
        momentum_log=momentum,
    )
    with momentum.open("a", encoding="utf-8") as stream:
        stream.write(
            "[MOMENTUM-PAPER-FEED-POLICY] decision=recovered "
            "reason=stable_connection prior_consecutive_1008=5\n"
        )
    recovered = fhc.check_massive_socket_policy_violations(
        state_path=state,
        market_data_log=market,
        momentum_log=momentum,
    )

    # Fifth-close cooloff/recovery no longer exists in the paper gateway feed.
    # Retain the old sequence as proof that neither marker can affect the gateway.
    for rows in (first, second, third, recovered):
        assert len(rows) == 1
        assert rows[0][0:2] == ("GREEN", "service-runtime:market-data:massive-1008")

    market.write_text("received 1008 (policy violation)\n", encoding="utf-8")
    with momentum.open("a", encoding="utf-8") as stream:
        stream.write("[MOMENTUM-PAPER-FEED-POLICY] decision=recovered\n")
    assert fhc.check_massive_socket_policy_violations(
        state_path=state, market_data_log=market, momentum_log=momentum,
    )[0][0] == "RED"


def test_corrupt_socket_evidence_cursor_fails_closed_without_replaying_old_logs(
    tmp_path: Path,
) -> None:
    market = tmp_path / "market-data.log"
    momentum = tmp_path / "momentum-paper.log"
    state = tmp_path / "socket-offsets.json"
    market.write_text("old 1008\n", encoding="utf-8")
    momentum.write_text("", encoding="utf-8")
    state.write_text("{broken", encoding="utf-8")

    rows = fhc.check_massive_socket_policy_violations(
        state_path=state,
        market_data_log=market,
        momentum_log=momentum,
    )

    assert rows[0][0:2] == (
        "RED",
        "service-runtime:socket-evidence:state-unreadable",
    )
    assert state.read_text(encoding="utf-8") == "{broken"


def test_inactive_expected_service_is_red_even_without_a_prior_sample() -> None:
    momentum = "project-mai-tai-momentum-paper.service"
    current = _service_states(state_overrides={momentum: ("inactive", "dead")})

    level, detail = fhc.classify_service_restarts(current, None, elapsed_s=None)

    assert level == "RED"
    assert f"{momentum}(inactive/dead)" in detail


# --- HEALTHNOISE1: momentum-paper runs 03:40-09:40 ET on session days by its own schedule ---- #

_MOMENTUM = "project-mai-tai-momentum-paper.service"


_CLEAN_STOP = fhc.ServiceRuntime(0, "inactive", "dead", "success", "1", "0")


def _momentum_inactive_rows(
    now: datetime, *, restarts: int = 0, runtime: fhc.ServiceRuntime | None = None
):
    current = _service_states(restart_overrides={_MOMENTUM: restarts})
    current[_MOMENTUM] = (runtime or _CLEAN_STOP)._replace(n_restarts=restarts)
    rows = fhc.classify_service_runtime_rows(
        current,
        {service: 0 for service in fhc.RUNTIME_SERVICES},
        elapsed_s=300,
        now=now,
        maintenance={},
    )
    return {name: (level, detail) for level, name, detail in rows}


def test_momentum_inactive_inside_its_window_is_red() -> None:
    # Thu 2026-10-08 08:00 EDT = 12:00 UTC; also both window edges.
    for now in (
        datetime(2026, 10, 8, 12, 0, tzinfo=UTC),
        datetime(2026, 10, 8, 7, 45, tzinfo=UTC),  # 03:45 ET, first run that must see it up
        datetime(2026, 10, 8, 13, 35, tzinfo=UTC),  # 09:35 ET, last run inside
    ):
        level, _detail = _momentum_inactive_rows(now)["service-runtime:momentum-paper:inactive"]
        assert level == "RED", now


def test_momentum_inactive_outside_its_window_is_expected_not_red() -> None:
    for now in (
        datetime(2026, 10, 9, 19, 20, tzinfo=UTC),  # Fri 15:20 ET: the live false RED
        datetime(2026, 10, 8, 13, 40, tzinfo=UTC),  # 09:40 ET stop instant
        datetime(2026, 10, 8, 7, 40, tzinfo=UTC),  # 03:40 ET start instant
        datetime(2026, 10, 8, 2, 0, tzinfo=UTC),  # 22:00 ET previous evening
        datetime(2026, 10, 10, 12, 0, tzinfo=UTC),  # Saturday 08:00 ET
        datetime(2026, 11, 26, 13, 0, tzinfo=UTC),  # Thanksgiving 08:00 EST
    ):
        rows = _momentum_inactive_rows(now)
        level, detail = rows["service-runtime:momentum-paper:inactive"]
        assert level == "EXPECTED", now
        assert "scheduled_stop=1 window_et=03:45-09:40" in detail
        assert all(level != "RED" for level, _ in rows.values()), now


_OFF_HOURS = datetime(2026, 10, 9, 19, 20, tzinfo=UTC)  # Fri 15:20 ET


def test_momentum_failed_outside_its_window_is_red() -> None:
    # Codex P1 on 8ef9fa99: failed/failed off-hours was EXPECTED and never paged.
    rows = _momentum_inactive_rows(
        _OFF_HOURS, runtime=fhc.ServiceRuntime(1, "failed", "failed", "exit-code", "1", "1")
    )
    level, detail = rows["service-runtime:momentum-paper:inactive"]
    assert level == "RED"
    assert "state=failed/failed result=exit-code" in detail


def test_momentum_abnormal_stops_outside_its_window_stay_red() -> None:
    abnormal = (
        fhc.ServiceRuntime(0, "inactive", "dead", "exit-code", "1", "1"),  # non-zero exit
        fhc.ServiceRuntime(0, "inactive", "dead", "signal", "2", "9"),  # SIGKILL
        fhc.ServiceRuntime(0, "inactive", "dead", "success", "2", "15"),  # killed by signal
        fhc.ServiceRuntime(0, "inactive", "dead", "core-dump", "3", "11"),
        fhc.ServiceRuntime(0, "failed", "failed", "start-limit-hit", "1", "1"),
        fhc.ServiceRuntime(0, "activating", "auto-restart", "exit-code", "1", "1"),
        fhc.ServiceRuntime(0, "deactivating", "stop-sigterm", "success", "1", "0"),
        fhc.ServiceRuntime(0, "inactive", "dead"),  # Result/ExecMain* not read
    )
    for runtime in abnormal:
        level, _detail = _momentum_inactive_rows(_OFF_HOURS, runtime=runtime)[
            "service-runtime:momentum-paper:inactive"
        ]
        assert level == "RED", runtime


def test_momentum_clean_stop_outside_window_is_expected_and_never_started_too() -> None:
    for runtime in (_CLEAN_STOP, fhc.ServiceRuntime(0, "inactive", "dead", "success", "0", "0")):
        level, _detail = _momentum_inactive_rows(_OFF_HOURS, runtime=runtime)[
            "service-runtime:momentum-paper:inactive"
        ]
        assert level == "EXPECTED", runtime


def test_momentum_clean_stop_inside_its_window_is_red() -> None:
    level, _detail = _momentum_inactive_rows(
        datetime(2026, 10, 8, 12, 0, tzinfo=UTC), runtime=_CLEAN_STOP
    )["service-runtime:momentum-paper:inactive"]
    assert level == "RED"


def test_systemctl_parser_reads_exit_properties() -> None:
    states = _service_states()
    states[_MOMENTUM] = _CLEAN_STOP
    text = "\n\n".join(
        "\n".join(
            (
                f"Result={state.result or 'success'}",
                f"NRestarts={state.n_restarts}",
                f"ExecMainCode={state.exec_main_code or '1'}",
                f"ExecMainStatus={state.exec_main_status or '0'}",
                f"Id={service}",
                f"ActiveState={state.active_state}",
                f"SubState={state.sub_state}",
            )
        )
        for service, state in states.items()
    )

    def runner(argv, **_kwargs):
        assert {"Result", "ExecMainCode", "ExecMainStatus"} <= set(argv)
        return subprocess.CompletedProcess(argv, 0, stdout=text, stderr="")

    parsed = fhc._read_service_runtimes(runner)
    assert parsed[_MOMENTUM] == _CLEAN_STOP
    assert fhc.is_clean_scheduled_stop(parsed[_MOMENTUM])


def test_scheduled_window_covers_only_momentum_paper() -> None:
    assert set(fhc.SCHEDULED_SERVICE_WINDOWS) == {_MOMENTUM}
    off_hours = datetime(2026, 10, 10, 12, 0, tzinfo=UTC)
    for service in fhc.RUNTIME_SERVICES:
        if service == _MOMENTUM:
            continue
        assert fhc.scheduled_service_expected_running(service, off_hours)
        current = _service_states(state_overrides={service: ("inactive", "dead")})
        rows = fhc.classify_service_runtime_rows(
            current, None, elapsed_s=None, now=off_hours, maintenance={}
        )
        slug = fhc._service_slug(service)
        assert (("RED", f"service-runtime:{slug}:inactive") in {(r[0], r[1]) for r in rows})


def test_momentum_restart_storm_stays_red_outside_its_window() -> None:
    rows = _momentum_inactive_rows(datetime(2026, 10, 9, 19, 20, tzinfo=UTC), restarts=5)
    assert rows["service-runtime:momentum-paper:restart-storm"][0] == "RED"


def test_expected_level_never_raises_the_summary_or_pages(monkeypatch, capsys) -> None:
    rows = (("EXPECTED", "service-runtime:momentum-paper:inactive", "scheduled_stop=1"),)
    monkeypatch.setattr(fhc, "RUNTIME_CHECKS", (fhc.CheckSpec(lambda: rows, fhc.FLEET_RUNTIME),))
    assert fhc.main(runtime_only=True) == 0
    out = capsys.readouterr().out
    assert "SUMMARY: GREEN" in out and "fleet_runtime_red=0" in out


def test_runtime_rows_are_independent_per_service_and_condition() -> None:
    momentum = "project-mai-tai-momentum-paper.service"
    oms = "project-mai-tai-oms.service"
    current = _service_states(
        restart_overrides={momentum: 5},
        state_overrides={oms: ("inactive", "dead")},
    )

    rows = fhc.classify_service_runtime_rows(
        current,
        {service: 0 for service in fhc.RUNTIME_SERVICES},
        elapsed_s=300,
        now=datetime(2026, 9, 17, 12, tzinfo=UTC),
        maintenance={},
    )

    red_names = {name for level, name, _detail in rows if level == "RED"}
    assert red_names == {
        "service-runtime:momentum-paper:restart-storm",
        "service-runtime:oms:inactive",
    }


def test_active_maintenance_is_expiring_explicit_and_never_green(tmp_path: Path) -> None:
    maintenance_path = tmp_path / "maintenance.txt"
    momentum = "project-mai-tai-momentum-paper.service"
    maintenance_path.write_text(
        f"{momentum} 2026-09-17T13:00:00Z planned deploy\n",
        encoding="utf-8",
    )
    windows, errors = fhc._read_maintenance_windows(maintenance_path)

    rows = fhc.classify_service_runtime_rows(
        _service_states(state_overrides={momentum: ("inactive", "dead")}),
        None,
        elapsed_s=None,
        now=datetime(2026, 9, 17, 12, tzinfo=UTC),
        maintenance=windows,
        maintenance_errors=errors,
    )

    momentum_rows = [row for row in rows if ":momentum-paper:" in row[1]]
    assert {row[0] for row in momentum_rows} == {"MAINTENANCE"}
    assert all("until=2026-09-17T13:00:00+00:00" in row[2] for row in momentum_rows)


def test_expired_maintenance_pages_and_no_expiry_is_rejected(tmp_path: Path) -> None:
    momentum = "project-mai-tai-momentum-paper.service"
    expired_path = tmp_path / "expired.txt"
    expired_path.write_text(
        f"{momentum} 2026-09-17T11:59:59Z planned deploy\n",
        encoding="utf-8",
    )
    windows, errors = fhc._read_maintenance_windows(expired_path)
    rows = fhc.classify_service_runtime_rows(
        _service_states(),
        None,
        elapsed_s=None,
        now=datetime(2026, 9, 17, 12, tzinfo=UTC),
        maintenance=windows,
        maintenance_errors=errors,
    )
    assert any(
        row[0:2] == ("RED", "service-runtime:momentum-paper:maintenance-expired") for row in rows
    )

    invalid_path = tmp_path / "invalid.txt"
    invalid_path.write_text(f"{momentum} planned-stop-without-expiry\n", encoding="utf-8")
    _windows, invalid_errors = fhc._read_maintenance_windows(invalid_path)
    assert invalid_errors == ("line 1: expected <unit> <until-ISO8601-UTC> <reason>",)


# --- check #2: oms-order-lifecycle (alive-but-not-executing) ------------------ #


def test_no_stuck_intents_is_green_quiet_or_executing():
    # THE no-false-alarm guard: no stuck intents -> GREEN, whether the market is quiet
    # (no intents) or the OMS is executing normally.
    assert fhc.classify_order_lifecycle(0, None)[0] == "GREEN"


def test_stuck_intents_is_red_alive_but_not_executing():
    level, detail = fhc.classify_order_lifecycle(3, 12)
    assert level == "RED"
    assert "not-executing" in detail or "not executing" in detail


def test_unreadable_intents_is_amber_not_red():
    assert fhc.classify_order_lifecycle(None, None)[0] == "AMBER"


# --- check #5: independently watch the D6 scheduler's durable success marker ------------------ #


def test_d6_current_success_is_green() -> None:
    level, detail = fhc.classify_d6_status(
        "[D6-OUTCOME-ACCEPTANCE-SUCCESS] session=2026-08-28 verdict=PASS\n",
        expected_session=date(2026, 8, 28),
    )
    assert level == "GREEN"
    assert "session=2026-08-28" in detail


def test_d6_yesterdays_success_is_red_when_a_new_session_is_due() -> None:
    level, detail = fhc.classify_d6_status(
        "[D6-OUTCOME-ACCEPTANCE-SUCCESS] session=2026-08-27 verdict=PASS\n",
        expected_session=date(2026, 8, 28),
    )
    assert level == "RED"
    assert "stale" in detail


def test_d6_future_success_is_red_instead_of_blessing_the_wrong_session() -> None:
    level, detail = fhc.classify_d6_status(
        "[D6-OUTCOME-ACCEPTANCE-SUCCESS] session=2026-08-29 verdict=PASS\n",
        expected_session=date(2026, 8, 28),
    )

    assert level == "RED"
    assert "future" in detail


def test_d6_current_nonpass_and_missing_status_are_red() -> None:
    expected = date(2026, 8, 28)
    assert (
        fhc.classify_d6_status(
            "[D6-OUTCOME-ACCEPTANCE-NONPASS] session=2026-08-28 verdict=FAIL\n",
            expected_session=expected,
        )[0]
        == "RED"
    )
    assert fhc.classify_d6_status(None, expected_session=expected)[0] == "RED"


def test_d6_malformed_session_is_red_not_an_amber_check_exception() -> None:
    level, detail = fhc.classify_d6_status(
        "[D6-OUTCOME-ACCEPTANCE-SUCCESS] session=2026-99-99 verdict=PASS\n",
        expected_session=date(2026, 8, 28),
    )

    assert level == "RED"
    assert "malformed" in detail


def test_d6_binary_status_is_red_not_an_amber_check_exception(monkeypatch, tmp_path) -> None:
    status = tmp_path / "STATUS.txt"
    status.write_bytes(b"\xff\xfe\x00\x80")
    monkeypatch.setattr(fhc, "_D6_STATUS_PATH", status)
    monkeypatch.setattr(
        fhc,
        "_last_completed_session_day",
        lambda _today: date(2026, 8, 28),
    )

    level, name, detail = fhc.check_d6_status_freshness()

    assert level == "RED"
    assert name == "d6-outcome-acceptance"
    assert "encoding error=UnicodeDecodeError" in detail


def test_d6_freshness_green_path_reads_the_current_success(monkeypatch, tmp_path) -> None:
    status = tmp_path / "STATUS.txt"
    status.write_text(
        "[D6-OUTCOME-ACCEPTANCE-SUCCESS] session=2026-08-28 verdict=PASS\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(fhc, "_D6_STATUS_PATH", status)
    monkeypatch.setattr(
        fhc,
        "_last_completed_session_day",
        lambda _today: date(2026, 8, 28),
    )

    level, name, detail = fhc.check_d6_status_freshness()

    assert (level, name) == ("GREEN", "d6-outcome-acceptance")
    assert "D6 SUCCESS current" in detail


def test_d6_permission_failure_is_red_and_distinct_from_missing(monkeypatch) -> None:
    class PermissionDeniedStatus:
        def read_text(self, *, encoding: str) -> str:
            raise PermissionError("root-only")

    monkeypatch.setattr(fhc, "_D6_STATUS_PATH", PermissionDeniedStatus())
    monkeypatch.setattr(
        fhc,
        "_last_completed_session_day",
        lambda _today: date(2026, 8, 28),
    )

    level, name, detail = fhc.check_d6_status_freshness()

    assert (level, name) == ("RED", "d6-outcome-acceptance")
    assert "permission error=PermissionError" in detail
    assert "missing" not in detail


def test_d6_other_oserror_is_red_and_names_the_io_failure(monkeypatch) -> None:
    class BrokenStatus:
        def read_text(self, *, encoding: str) -> str:
            raise OSError("I/O failure")

    monkeypatch.setattr(fhc, "_D6_STATUS_PATH", BrokenStatus())
    monkeypatch.setattr(
        fhc,
        "_last_completed_session_day",
        lambda _today: date(2026, 8, 28),
    )

    level, name, detail = fhc.check_d6_status_freshness()

    assert (level, name) == ("RED", "d6-outcome-acceptance")
    assert "I/O error=OSError" in detail
    assert "missing" not in detail


_D6_STATUS_1008 = """[D6-OUTCOME-ACCEPTANCE-NONPASS] session=2026-10-08 window=[2026-10-08T00:00:00-04:00, 2026-10-09T00:00:00-04:00) verdict=FAIL denominators=present
denominator_contract=all four metric denominators present
metric=paired_legs verdict=PASS paired_legs=1 usable=2 of 2 webull_only=1 could_not_tell=0 coverage=100.0%
metric=fill_rate verdict=FAIL fill_rate mirror=1/9=11.1% schwab=4/18=22.2% gap_pp=11.1 matched_symbols=1 baseline=18/292=6.2% vs 34/368=9.2% gap_pp=3.1
metric=duplicate_legs verdict=PASS duplicate_legs=0 of 2 filled Webull fan-out legs could_not_tell=0
metric=refused_exits verdict=PASS refused_exits=0 post_exit_episodes=2 denominator=confirmed Webull SELL fill orders
verdict=FAIL
"""


def test_d6_graded_fail_stays_red_and_names_the_failing_metric() -> None:
    # HEALTHNOISE1: the 10-08 FAIL is a real outcome (Webull mirror fill rate below Schwab by more
    # than the known-bad gap); it stays RED and the detail says exactly what failed.
    level, detail = fhc.classify_d6_status(_D6_STATUS_1008, expected_session=date(2026, 10, 8))
    assert level == "RED"
    assert "run_verdict=FAIL" in detail
    assert "failing=[fill_rate fill_rate mirror=1/9=11.1% schwab=4/18=22.2% gap_pp=11.1" in detail
    assert "paired_legs" not in detail


def test_d6_could_not_tell_and_notification_failure_are_red_and_labelled() -> None:
    status = (
        "[D6-OUTCOME-ACCEPTANCE-NONPASS] session=2026-10-08 verdict=COULD_NOT_TELL "
        "denominators=invalid\nnotification=FAILED session_not_marked=1\n"
    )
    level, detail = fhc.classify_d6_status(status, expected_session=date(2026, 10, 8))
    assert level == "RED"
    assert "run_verdict=COULD_NOT_TELL" in detail and "notification=FAILED" in detail


def test_d6_crashed_mid_run_is_red_in_progress() -> None:
    status = "[D6-OUTCOME-ACCEPTANCE-STARTED] session=2026-10-08 verdict=IN_PROGRESS\n"
    level, detail = fhc.classify_d6_status(status, expected_session=date(2026, 10, 8))
    assert level == "RED" and "run_verdict=IN_PROGRESS" in detail


def test_d6_freshness_check_is_registered_in_the_executed_check_list() -> None:
    assert fhc.check_d6_status_freshness in [spec.check for spec in fhc.CHECKS]


def test_every_fleet_check_has_an_explicit_alert_class() -> None:
    assert {spec.check.__name__: spec.alert_class for spec in fhc.CHECKS} == {
        "check_orb_schwab_heartbeat": fhc.FLEET_RUNTIME,
        "check_service_restart_storms": fhc.FLEET_RUNTIME,
        "check_massive_socket_policy_violations": fhc.FLEET_RUNTIME,
        "check_buy_submission_commit_failures": fhc.FLEET_RUNTIME,
        "check_oms_order_lifecycle": fhc.LIVE_MONEY,
        "check_stops_armed": fhc.LIVE_MONEY,
        "check_bar_continuity": fhc.DIAGNOSTIC,
        "check_d6_status_freshness": fhc.SCOREBOARD,
    }


def test_fleet_aggregate_is_a_summary_not_a_pageable_verdict(monkeypatch, capsys) -> None:
    checks = (
        fhc.CheckSpec(lambda: ("RED", "paper-check", "paper issue"), fhc.PAPER),
        fhc.CheckSpec(lambda: ("RED", "live-check", "live issue"), fhc.LIVE_MONEY),
    )
    monkeypatch.setattr(fhc, "CHECKS", checks)

    assert fhc.main() == 2
    output = capsys.readouterr().out

    assert "VERDICT: RED paper-check class=PAPER" in output
    assert "VERDICT: RED live-check class=LIVE_MONEY" in output
    assert (
        "SUMMARY: RED fleet-function-health checks=2 live_money_red=1 fleet_runtime_red=0" in output
    )
    assert "VERDICT: RED fleet-function-health" not in output


def test_runtime_only_executes_no_market_function_checks(monkeypatch, capsys) -> None:
    runtime = fhc.CheckSpec(
        lambda: ("GREEN", "service-restart-storms", "stable"),
        fhc.FLEET_RUNTIME,
    )
    market = fhc.CheckSpec(
        lambda: (_ for _ in ()).throw(AssertionError("market check ran")),
        fhc.LIVE_MONEY,
    )
    monkeypatch.setattr(fhc, "RUNTIME_CHECKS", (runtime,))
    monkeypatch.setattr(fhc, "CHECKS", (runtime, market))

    assert fhc.main(runtime_only=True) == 0
    output = capsys.readouterr().out
    assert "service-restart-storms" in output
    assert "checks=1" in output


def test_d6_expected_session_skips_weekend_and_full_closure() -> None:
    assert fhc._last_completed_session_day(date(2026, 9, 8)) == date(2026, 9, 4)


# --- check #3: stops-armed (every OMS-owned open position has an armed stop) --- #


def test_owned_position_with_stop_is_green():
    # 2 OMS-owned open, 0 unprotected → all armed → GREEN.
    assert fhc.classify_stops_armed(0, 2)[0] == "GREEN"


def test_owned_position_without_stop_is_red_naked():
    level, detail = fhc.classify_stops_armed(1, 1)
    assert level == "RED"
    assert "NAKED" in detail


def test_flat_is_green_nothing_to_protect():
    # No OMS-owned open positions → nothing to protect → GREEN, never RED.
    assert fhc.classify_stops_armed(0, 0)[0] == "GREEN"


def test_manual_position_is_ignored_green():
    # SCOPING INVARIANT: a manual holding has no virtual_positions row, so the query never
    # counts it → unprotected stays 0 → GREEN. (The virtual_positions-only source is what
    # enforces this; live-validated. Here we assert the verdict for that count state.)
    assert fhc.classify_stops_armed(0, 0)[0] == "GREEN"


def test_unreadable_stops_is_amber_not_red():
    assert fhc.classify_stops_armed(None, None)[0] == "AMBER"


# ---------------------------------------------------------------------------------------------
# 2026-08-03 — PAPER CONTAMINATION. Unscoped checks let polygon_30s (PAPER/sim) drive a page.
# It had already reached the entries counter, the sawtooth check and #628 before this sweep.
# This pager guards NAKED POSITIONS; teaching the operator to ignore it is the real damage.
# ---------------------------------------------------------------------------------------------


def test_real_money_accounts_are_pinned_by_VALUE() -> None:
    """⛔ `name LIKE 'live:%'` is NOT a safe proxy — `live:polygon_30s` and `live:webull_30s`
    both exist as broker_accounts rows. Verified 2026-08-03: only live:schwab_1m_v2, live:orb
    and paper:polygon_30s had any order in 30 days. Pin the VALUE, not just its use."""
    assert fhc.REAL_MONEY_ACCOUNTS == ("live:schwab_1m_v2", "live:orb")
    assert not any("polygon" in a for a in fhc.REAL_MONEY_ACCOUNTS)
    assert not any(a.startswith("paper:") for a in fhc.REAL_MONEY_ACCOUNTS)


def test_the_sql_list_quotes_every_account() -> None:
    for acct in fhc.REAL_MONEY_ACCOUNTS:
        assert f"'{acct}'" in fhc._REAL_MONEY_SQL_LIST


def test_a_paper_note_never_changes_the_level() -> None:
    """Paper stays VISIBLE (polygon_30s really does reject every STOP sell) but can never page."""
    base_level, base_detail = fhc.classify_stops_armed(0, 0)
    noted = fhc._with_paper_note(base_detail, 3, "unprotected sim position")
    assert base_level == "GREEN"
    assert "paper/sim: 3 unprotected sim positions" in noted
    assert "not paged" in noted


def test_zero_paper_adds_nothing() -> None:
    assert fhc._with_paper_note("all clear", 0, "x") == "all clear"
    assert fhc._with_paper_note("all clear", None, "x") == "all clear"


def test_scoping_did_NOT_make_the_check_always_green() -> None:
    """⛔⭐ THE ACCEPTANCE CRITERION. Scoping must not silence the real condition — a REAL
    unprotected real-money position and a REAL lifecycle stall must still go RED. Always-green
    is strictly worse than always-red: it reads as health."""
    assert fhc.classify_stops_armed(1, 1)[0] == "RED"
    assert fhc.classify_order_lifecycle(3, 12)[0] == "RED"
    # ...and a paper note must not soften either one.
    red_level, red_detail = fhc.classify_stops_armed(1, 1)
    assert red_level == "RED"
    assert "NAKED" in fhc._with_paper_note(red_detail, 5, "unprotected sim position")


def test_the_bar_hole_verdict_no_longer_prescribes_a_restart() -> None:
    """⛔ The alert used to say "Restart v2 so the REST warmup refetches a contiguous series."
    #620 already guards live ATR, and a restart PUNCHES A FRESH HOLE — the very condition this
    check detects. An alert that reliably recommends the wrong action is worse than no alert."""
    level, detail = fhc.classify_bar_continuity(worst_gap_min=10, gap_symbols=1, bars_seen=50)
    assert level == "RED"
    assert "Restart v2" not in detail
    assert "Do NOT restart on this alert alone" in detail
    assert "#620" in detail, "must point at the guard that actually protects live ATR"


def test_the_halt_downgrade_cannot_fire_on_a_REST_FAILURE() -> None:
    """⛔⭐ "INSERTED 0" is equally true when the REST call ERRORED. Downgrading on that would
    turn a DUAL-SOURCE OUTAGE (streamer dead AND REST dead) into an informational note — the
    worst outcome for this pager. Not hypothetical: Schwab REST 401'd for 2h41m on 2026-08-03.
    Pins that the wrapper's downgrade is gated on REST_FAILED == 0."""
    from pathlib import Path

    wrapper = Path(__file__).resolve().parents[2] / "ops" / "health" / "bar_gap_watch_cron.sh"
    src = wrapper.read_text(encoding="utf-8")
    assert "REST fetch FAILED" in src, "the failure signal must be parsed at all"
    assert '[ "${REST_FAILED:-0}" -eq 0 ]' in src, "downgrade must require ZERO REST failures"
    assert "HALT_DOWNGRADE=0" in src, "must default to NOT downgrading"
