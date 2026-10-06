import importlib.util
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).parent


def load(name, file):
    spec = importlib.util.spec_from_file_location(name, ROOT / file)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


daily = load("daily_under_test", "daily_guard.py")
guard = load("rules_under_test", "guard_rules.py")


@pytest.mark.parametrize("value,expected", [
    ("2026-10-05T21:10:00-04:00", date(2026, 10, 6)),
    ("2026-10-06T03:40:00-04:00", date(2026, 10, 6)),
    ("2026-10-09T21:10:00-04:00", date(2026, 10, 12)),
    ("2026-12-24T21:10:00-05:00", date(2026, 12, 28)),
])
def test_next_guard_window_uses_existing_session_and_trading_calendar(value, expected):
    assert daily.session_day(datetime.fromisoformat(value), {date(2026, 12, 25)}) == expected


def test_closed_trading_day_timer_skips_without_starting_paper():
    assert daily.closed_timer_day(datetime.fromisoformat("2026-12-25T03:40:00-05:00"), {date(2026, 12, 25)})
    assert not daily.closed_timer_day(datetime.fromisoformat("2026-10-06T03:40:00-04:00"), set())


def test_finish_drains_real_fractional_last_sample_without_weakening_9600_count():
    # Today's sampler phase .479s follows the guard's integer-second read.
    evidence = SimpleNamespace(treatment_count=9599)
    clock = datetime(2026, 10, 5, 13, 40, 1, tzinfo=UTC)
    waited = []
    def wait(timeout):
        waited.append(timeout)
    def read(now):
        assert waited == [3] and now == clock
        evidence.treatment_count += 1
        return None
    evidence.read = read
    assert guard.finish_sampler(SimpleNamespace(wait=wait, returncode=0), evidence, lambda: clock) is None
    assert evidence.treatment_count == 9600


def test_finish_does_not_invent_a_missing_internal_row():
    evidence = SimpleNamespace(treatment_count=9599, read=lambda now: None)
    guard.finish_sampler(SimpleNamespace(wait=lambda timeout: None, returncode=0), evidence,
                         lambda: datetime.now(UTC))
    assert evidence.treatment_count == 9599
    source = (ROOT / "guard_rules.py").read_text()
    assert "sampler_evidence.treatment_count != expected" in source


def test_finish_preserves_final_kick_and_unknown_routing():
    evidence = SimpleNamespace(read=lambda now: "gateway_1008")
    assert guard.finish_sampler(SimpleNamespace(wait=lambda timeout: None, returncode=0), evidence,
                                lambda: datetime.now(UTC)) == "gateway_1008"
    with pytest.raises(guard.Blind):
        guard.finish_sampler(SimpleNamespace(wait=lambda timeout: None, returncode=3), evidence,
                             lambda: datetime.now(UTC))


def test_real_last_row_is_counted_after_integer_tick(tmp_path):
    path = tmp_path / "sampler.jsonl"
    start = datetime(2026, 10, 5, 11, tzinfo=UTC)
    end = start + timedelta(seconds=2)
    import json
    def row(stamp, status):
        return json.dumps({"sampled_at_utc": stamp.isoformat(), "status": status,
            "device": 1, "inode": 2, "read_from_offset": 0, "size_bytes": 0, "new_1008_lines": 0}) + "\n"
    path.write_text(row(start - timedelta(seconds=.521), "BASELINE") + row(start + timedelta(seconds=.479), "OK"))
    evidence = guard.SamplerEvidence(path, start=start, end=end, launched_at=start - timedelta(seconds=1))
    evidence.read(start + timedelta(seconds=1))
    assert evidence.treatment_count == 1
    with path.open("a") as stream:
        stream.write(row(start + timedelta(seconds=1.479), "OK"))
    guard.finish_sampler(SimpleNamespace(wait=lambda timeout: None, returncode=0), evidence, lambda: end)
    assert evidence.treatment_count == 2


def test_original_limits_and_count_one_snapshot_reader_unchanged():
    assert guard.REDIS_USED_MEMORY_STOP_BYTES == 1_600_000_000
    assert guard.MAX_SNAPSHOT_READS_PER_TICK == 3
    assert guard.SNAPSHOT_BOUNDS == (14.076, 14.168, 15.106)
    assert guard.HEARTBEAT_AGE_BOUND == 30.819
    source = (ROOT / "guard_rules.py").read_text()
    assert "clock_time(9, 40)" in source and "count=1" in source
    assert "load1 > 3.5" in source and "over_3_5_warning_only" in source


def test_guard_ready_precedes_paper_start_and_failure_stops_only_paper():
    unit = (ROOT / "project-mai-tai-option-a-daily-guard.service").read_text()
    timer = (ROOT / "project-mai-tai-option-a-daily-guard.timer").read_text()
    assert "Type=notify" in unit and "Restart=no" in unit
    assert "ExecStartPost=/bin/systemctl start project-mai-tai-momentum-paper.service" in unit
    assert "OnFailure=project-mai-tai-option-a-daily-guard-failure.service" in unit
    assert "Mon..Fri *-*-* 03:40:00 America/New_York" in timer
    assert "Persistent=false" in timer and "2026-10-05" not in timer
