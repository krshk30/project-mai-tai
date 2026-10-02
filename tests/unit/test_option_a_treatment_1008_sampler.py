from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from scripts import option_a_treatment_1008_sampler as sampler
from scripts.option_a_treatment_1008_sampler import (
    sample_log,
    start_deadline_utc,
    start_is_before_deadline,
)


WHEN = datetime(2026, 10, 1, 11, 0, tzinfo=UTC)
ET = ZoneInfo("America/New_York")


@pytest.mark.parametrize("treatment_date", [date(2026, 10, 1), date(2026, 11, 3)])
def test_sampler_start_deadline_is_treatment_day_seven_et(treatment_date: date) -> None:
    evening_before = datetime.combine(
        treatment_date - timedelta(days=1), datetime.min.time(), tzinfo=ET
    ).replace(hour=20, minute=30)
    at_seven = datetime.combine(treatment_date, datetime.min.time(), tzinfo=ET).replace(hour=7)

    assert start_is_before_deadline(evening_before, treatment_date)
    assert start_deadline_utc(treatment_date) == at_seven.astimezone(UTC)
    assert not start_is_before_deadline(at_seven, treatment_date)
    assert not start_is_before_deadline(at_seven.replace(second=1), treatment_date)


def test_sampler_cli_accepts_evening_before_but_refuses_treatment_day_after_seven(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    treatment_date = date(2026, 10, 1)
    evening_before = datetime(2026, 9, 30, 20, 30, tzinfo=ET)
    after_seven = datetime(2026, 10, 1, 7, 0, 1, tzinfo=ET)
    log = tmp_path / "market-data.log"
    log.write_bytes(b"old received 1008\n")
    output = tmp_path / "capture.jsonl"
    end = evening_before + timedelta(seconds=1)
    argv = [
        "--gateway-log", str(log), "--output", str(output),
        "--treatment-date", treatment_date.isoformat(), "--end-utc", end.isoformat(),
    ]
    monkeypatch.setattr(sampler.time, "sleep", lambda _: None)
    samples = iter((evening_before, end + timedelta(seconds=1)))
    assert sampler.main(argv, clock=lambda: next(samples)) == 0
    assert len(output.read_text(encoding="utf-8").splitlines()) == 1

    rejected_output = tmp_path / "rejected.jsonl"
    argv[3] = str(rejected_output)
    with pytest.raises(SystemExit) as error:
        sampler.main(argv, clock=lambda: after_seven)
    assert error.value.code == 2
    assert not rejected_output.exists()


def test_sampler_starts_at_end_of_old_log_and_stops_on_first_new_1008(tmp_path: Path) -> None:
    log = tmp_path / "market-data.log"
    log.write_bytes(b"old received 1008\n")

    baseline, cursor = sample_log(log, None, sampled_at=WHEN)
    assert baseline["status"] == "BASELINE"
    assert baseline["new_1008_lines"] == 0
    assert baseline["read_from_offset"] == len(b"old received 1008\n")
    assert baseline["size_bytes"] == cursor.offset
    assert baseline["mtime_ns"] > 0

    with log.open("ab") as stream:
        stream.write(b"healthy\nreceived 1008 (policy violation)\n")
    event, cursor = sample_log(log, cursor, sampled_at=WHEN)
    assert event["status"] == "STOP_TRIGGER"
    assert event["new_1008_lines"] == 1
    assert event["read_from_offset"] == baseline["size_bytes"]
    assert cursor.offset == event["size_bytes"]


def test_sampler_ignores_snapshot_and_symbol_counts_that_contain_1008(tmp_path: Path) -> None:
    log = tmp_path / "market-data.log"
    log.write_bytes(b"baseline\n")
    _, cursor = sample_log(log, None, sampled_at=WHEN)

    with log.open("ab") as stream:
        stream.write(
            b"published snapshot batch with 11008 records\n"
            b"subscription count=1008 -> 1008 symbols\n"
        )
    ordinary, cursor = sample_log(log, cursor, sampled_at=WHEN)
    assert ordinary["status"] == "OK"
    assert ordinary["new_1008_lines"] == 0

    with log.open("ab") as stream:
        stream.write(b"ConnectionClosedError: received 1008 (policy violation)\n")
    violation, _ = sample_log(log, cursor, sampled_at=WHEN)
    assert violation["status"] == "STOP_TRIGGER"
    assert violation["new_1008_lines"] == 1


def test_sampler_counts_split_line_once_and_marks_truncation_unknown(tmp_path: Path) -> None:
    log = tmp_path / "market-data.log"
    log.write_bytes(b"baseline\n")
    _, cursor = sample_log(log, None, sampled_at=WHEN)
    with log.open("ab") as stream:
        stream.write(b"received 10")
    partial, cursor = sample_log(log, cursor, sampled_at=WHEN)
    assert partial["status"] == "OK"
    assert partial["new_1008_lines"] == 0

    with log.open("ab") as stream:
        stream.write(b"08 (policy violation)\n")
    complete, cursor = sample_log(log, cursor, sampled_at=WHEN)
    assert complete["status"] == "STOP_TRIGGER"
    assert complete["new_1008_lines"] == 1

    log.write_bytes(b"x")
    truncated, _ = sample_log(log, cursor, sampled_at=WHEN)
    assert truncated["status"] == "UNKNOWN_ROTATED_OR_TRUNCATED"


def test_sampler_waits_for_newline_after_a_complete_1008_marker(tmp_path: Path) -> None:
    log = tmp_path / "market-data.log"
    log.write_bytes(b"baseline\n")
    _, cursor = sample_log(log, None, sampled_at=WHEN)
    with log.open("ab") as stream:
        stream.write(b"received 1008")

    partial, cursor = sample_log(log, cursor, sampled_at=WHEN)
    assert partial["status"] == "OK"
    assert partial["new_1008_lines"] == 0

    with log.open("ab") as stream:
        stream.write(b" (policy violation)\n")
    complete, _ = sample_log(log, cursor, sampled_at=WHEN)
    assert complete["status"] == "STOP_TRIGGER"
    assert complete["new_1008_lines"] == 1
