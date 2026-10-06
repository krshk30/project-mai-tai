"""Fake clocks/checkers/adapter only; no installed gate or units are invoked."""
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
import subprocess

import pytest
import daily
import gate_patch
import release_policy as policy

ROOT = Path(__file__).parent
NOW = datetime(2026, 10, 7, 10, 20, tzinfo=timezone.utc)
HOLIDAYS = {date(2026, 11, 26), date(2027, 1, 1)}


def paper():
    return dict(MainPID="100", ActiveState="active", SubState="running", NRestarts="0",
                ExecMainStartTimestamp="Wed 2026-10-07 07:40:00 UTC")


def guard():
    return dict(MainPID="99", ActiveState="active", SubState="running", NRestarts="0",
                ExecMainStartTimestamp="Wed 2026-10-07 07:40:00 UTC")


def report(now=NOW, verdict="PASS"):
    return "Generated: " + now.astimezone(policy.ET).strftime("%Y-%m-%d %H:%M:%S %Z") + " (" + now.strftime("%Y-%m-%d %H:%M:%S UTC") + ")\nFinal call: " + verdict + "; controlled-checker\n"


def test_paper_exact_floor_and_guard_equality_no_constant_pid():
    for pid in ("100", "2000"):
        current = dict(paper(), MainPID=pid)
        assert daily.paper_shape(NOW, current, guard())["paper_after_guard"]
    old_guard = dict(guard(), ExecMainStartTimestamp="Tue 2026-10-06 07:40:00 UTC")
    assert daily.paper_shape(NOW, paper(), old_guard)["paper_after_floor"]


@pytest.mark.parametrize("field,value", [("MainPID", "0"), ("ActiveState", "inactive"), ("SubState", "dead"),
                                         ("NRestarts", "1"), ("ExecMainStartTimestamp", ""),
                                         ("ExecMainStartTimestamp", "Wed 2026-10-07 03:40:00 EDT"),
                                         ("ExecMainStartTimestamp", "Wed 2026-10-07 07:39:59 UTC"),
                                         ("ExecMainStartTimestamp", "Wed 2026-10-07 10:20:01 UTC")])
def test_paper_every_admission_negative(field, value):
    item = dict(paper(), **{field: value})
    with pytest.raises((policy.Stop, ValueError)):
        daily.paper_shape(NOW, item, guard())


@pytest.mark.parametrize("field,value", [("MainPID", "0"), ("ActiveState", "inactive"),
                                         ("ExecMainStartTimestamp", ""),
                                         ("ExecMainStartTimestamp", "Wed 2026-10-07 07:40:01 UTC")])
def test_guard_every_admission_negative(field, value):
    item = dict(guard(), **{field: value})
    with pytest.raises((policy.Stop, ValueError)):
        daily.paper_shape(NOW, paper(), item)


@pytest.mark.parametrize("now,stamp", [(datetime(2026, 11, 2, 11, 20, tzinfo=timezone.utc), "Mon 2026-11-02 08:40:00 UTC"),
                                     (datetime(2026, 3, 9, 10, 20, tzinfo=timezone.utc), "Mon 2026-03-09 07:40:00 UTC")])
def test_dst_floor_tracks_et_not_fixed_utc(now, stamp):
    p, g = dict(paper(), ExecMainStartTimestamp=stamp), dict(guard(), ExecMainStartTimestamp=stamp)
    assert daily.paper_shape(now, p, g)["paper_after_floor"]


def test_et_rollover_previous_paper_start_refused():
    with pytest.raises(policy.Stop):
        daily.paper_shape(NOW + timedelta(days=1), paper(), guard())


def test_paper_floor_independent_when_guard_older():
    earlier = dict(guard(), ExecMainStartTimestamp="Tue 2026-10-06 07:40:00 UTC")
    before_floor = dict(paper(), ExecMainStartTimestamp="Wed 2026-10-07 07:39:59 UTC")
    with pytest.raises(policy.Stop):
        daily.paper_shape(NOW, before_floor, earlier)


def test_calendar_holiday_weekend_unknown_and_after07():
    assert daily.calendar(NOW, HOLIDAYS)
    assert not daily.calendar(datetime(2026, 11, 26, 11, 20, tzinfo=timezone.utc), HOLIDAYS)
    assert not daily.calendar(datetime(2026, 10, 10, 10, 20, tzinfo=timezone.utc), HOLIDAYS)
    for now, holidays in [(NOW, set()), (NOW.replace(year=2028), HOLIDAYS), (NOW.replace(hour=11), HOLIDAYS)]:
        with pytest.raises(policy.Stop):
            daily.calendar(now, holidays)


@pytest.mark.parametrize("rc,verdict", [(0, "PASS"), (1, "FAIL"), (2, "UNKNOWN"), (0, "EXPECTED BY DESIGN")])
def test_checker_original_three_way_rc_with_fresh_dated_report(rc, verdict):
    result, receipt, raw = daily.run_checks(NOW, HOLIDAYS, lambda: (rc, b"original checker output", NOW + timedelta(seconds=1)),
                                          lambda day: (report(verdict=verdict), NOW.timestamp()))
    assert result == rc and receipt["gate_rc"] == rc and raw == b"original checker output"


@pytest.mark.parametrize("rc", [124, -15, 3])
def test_checker_crash_timeout_are_unknown_not_pass(rc):
    assert daily.outcome(rc, report(), NOW, NOW + timedelta(seconds=1), NOW.timestamp()) == 2


@pytest.mark.parametrize("raw,mtime", [("", NOW.timestamp()), (report(NOW - timedelta(days=1)), NOW.timestamp()),
                                     (report(NOW - timedelta(minutes=1)), NOW.timestamp()),
                                     (report(), NOW.timestamp() - 1), (report() + report(), NOW.timestamp()),
                                     (report(verdict="FAIL"), NOW.timestamp())])
def test_missing_stale_wrong_date_duplicate_or_disagreeing_report_blocks(raw, mtime):
    assert daily.outcome(0, raw, NOW, NOW + timedelta(seconds=1), mtime) == 2


def test_gate_transform_exact_baseline_and_only_scoped_pins(tmp_path):
    original = (ROOT / "preopen.baseline.sh").read_text()
    assert policy.digest(original.encode()) == policy.BASELINE_GATE
    final = {name: dict(MainPID=500 + i, ExecMainStartTimestamp="Tue 2026-10-06 20:11:00 UTC")
             for i, name in enumerate(policy.CHANGED)}
    value = gate_patch.build(original, final, "/exact/snapshot.json", "/exact/record.json")
    assert '--restarted strategy' not in value
    assert '--restarted orb-schwab' in value
    for key in ("STRATEGY", "ORB", "MARKET_DATA", "CONTROL"):
        for field in ("PID", "START"):
            line = next(line for line in original.splitlines() if line.startswith("EXPECTED_" + key + "_" + field + "="))
            assert line in value
    assert 'EXPECTED_DATE="$(TZ=America/New_York date +%F)"' in value
    assert 'v2-restart-evidence-${EXPECTED_DATE//-/}.md' in value
    assert 'daily.py paper' in value and 'daily.py report-date "$REPORT"' in value
    assert 'preopen_final_verdict\nexit $?' in value
    assert 'MAI_TAI_ORB_LIVE_SCHWAB_ORDERS_ENABLED=true' in value
    assert 'MAI_TAI_ORB_SCHWAB_OBSERVE_ENABLED=false' in value
    for name in ("oms", "schwab-1m-v2"):
        for key in policy.PM:
            assert value.count(f"--expect-flag '{name}:{key}=true'") == 1
    target = tmp_path / "gate.sh"
    target.write_text(value)
    subprocess.run(["bash", "-n", target], check=True)
    with pytest.raises(policy.Stop):
        gate_patch.build(original + "\n# unexpected drift", final, "/s", "/r")


def test_units_literal_timer_only_checks_no_trading_dependencies():
    service = (ROOT / "project-mai-tai-preopen.service").read_text()
    timer = (ROOT / "project-mai-tai-preopen.timer").read_text()
    failure = (ROOT / "project-mai-tai-preopen-failure.service").read_text()
    assert "OnCalendar=Mon..Fri *-*-* 06:20:00 America/New_York" in timer
    assert "Persistent=false" in timer and "OnBoot" not in timer and "OnActive" not in timer
    assert "User=root" in service and "Restart=no" in service and "UnsetEnvironment=TZ" in service
    assert "OnFailure=project-mai-tai-preopen-failure.service" in service
    assert "OnFailure=" not in failure and "TimeoutStartSec=45" in failure
    for raw in (service, timer, failure):
        assert "Requires=" not in raw and "Wants=" not in raw
        assert "oms.service" not in raw and "schwab-1m-v2.service" not in raw
    for name in ("daily-run.sh", "daily-notify.sh"):
        subprocess.run(["bash", "-n", ROOT / name], check=True)


def test_no_direct_duplicate_notification_in_checker():
    import inspect
    assert "preopen_alert" not in inspect.getsource(daily.run)
    assert "notify()" not in inspect.getsource(daily.run)
    assert "preopen_alert" in inspect.getsource(daily.notify)
