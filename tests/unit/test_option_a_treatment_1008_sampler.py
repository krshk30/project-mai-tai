from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from scripts.option_a_treatment_1008_sampler import sample_log


WHEN = datetime(2026, 10, 1, 11, 0, tzinfo=UTC)


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
        stream.write(b"08\n")
    complete, cursor = sample_log(log, cursor, sampled_at=WHEN)
    assert complete["status"] == "STOP_TRIGGER"
    assert complete["new_1008_lines"] == 1

    log.write_bytes(b"x")
    truncated, _ = sample_log(log, cursor, sampled_at=WHEN)
    assert truncated["status"] == "UNKNOWN_ROTATED_OR_TRUNCATED"
