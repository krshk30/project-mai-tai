from __future__ import annotations

import json
from pathlib import Path

from project_mai_tai.backtest import momentum_load_census as census


def test_proc_stat_parser_handles_spaces_in_process_name() -> None:
    fields = ["S", "42", *(["0"] * 9), "30", "12"]

    result = census.parse_proc_stat(f"123 (cron worker) {' '.join(fields)}")

    assert result == census.ProcessSample(123, 42, "cron worker", "S", 42)


def test_process_label_never_copies_command_arguments(tmp_path: Path) -> None:
    process = tmp_path / "123"
    process.mkdir()
    (process / "cmdline").write_bytes(
        b"/usr/bin/python3\0/home/trader/watch/check.py\0--api-key=secret-value\0"
    )

    label = census.safe_process_label(123, "python3", tmp_path)

    assert label == "python3:check.py"
    assert "secret-value" not in label


def test_cpu_rows_attribute_only_new_ticks_to_same_process() -> None:
    prior = {123: census.ProcessSample(123, 1, "python3", "R", 10)}
    current = {123: census.ProcessSample(123, 1, "python3", "R", 60)}

    result = census.process_cpu_rows(
        prior,
        current,
        elapsed_seconds=1,
        ticks_per_second=100,
        label_reader=lambda _pid, _comm: "python3:check.py",
    )

    assert result[0]["cpu_pct_one_cpu"] == 50.0
    assert result[0]["cpu_seconds"] == 0.5
    assert result[0]["label"] == "python3:check.py"


def test_census_retains_load_crossing_and_process_evidence(
    tmp_path: Path, monkeypatch
) -> None:
    processes = iter(
        [
            {123: census.ProcessSample(123, 1, "python3", "R", 0)},
            {123: census.ProcessSample(123, 1, "python3", "R", 50)},
            {123: census.ProcessSample(123, 1, "python3", "S", 100)},
        ]
    )
    loads = iter([(3.2, 2.0, 2, 300), (3.7, 2.1, 3, 301)])
    ticks = iter([0, 0, 1, 1, 2, 2])
    monkeypatch.setattr(census, "scan_processes", lambda _root: next(processes))
    monkeypatch.setattr(census, "read_load", lambda _root: next(loads))
    monkeypatch.setattr(
        census, "safe_process_label", lambda _pid, _comm, _root: "python3:watch.py"
    )

    samples_path = tmp_path / "load.jsonl"
    summary_path = tmp_path / "summary.json"
    result = census.collect_census(
        duration_seconds=2,
        interval_seconds=1,
        samples_path=samples_path,
        summary_path=summary_path,
        monotonic=lambda: next(ticks),
        sleep=lambda _seconds: None,
    )

    rows = [json.loads(line) for line in samples_path.read_text().splitlines()]
    assert len(rows) == 2
    assert rows[1]["load_1m"] == 3.7
    assert rows[1]["top_processes"][0]["label"] == "python3:watch.py"
    assert result["samples_above_frozen_3_5_limit"] == 1
    assert result["max_load_1m"] == 3.7
    assert json.loads(summary_path.read_text()) == result
