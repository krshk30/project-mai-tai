from __future__ import annotations

import asyncio
import csv
from datetime import UTC, date, datetime
import gzip
import json
from pathlib import Path

import pytest

from project_mai_tai.backtest.momentum_gateway_throughput import (
    _gather_fail_fast,
    _missed_snapshot_cycles,
    evaluate_replay,
    PolicyLogCounter,
    measured_or_unmeasured,
    nearest_rank,
    population_report,
    replay_abort_reason,
    summarize_massive_flat_file,
)


_COLUMNS = [
    "ticker",
    "conditions",
    "correction",
    "exchange",
    "id",
    "participant_timestamp",
    "price",
    "sequence_number",
    "sip_timestamp",
    "size",
    "tape",
    "trf_id",
    "trf_timestamp",
]


def _ns(hour: int, minute: int, second: int) -> int:
    observed = datetime(2026, 9, 17, hour, minute, second, tzinfo=UTC)
    return int(observed.timestamp() * 1_000_000_000)


def _flat_file(path: Path, timestamps: list[int]) -> Path:
    with gzip.open(path, "wt", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=_COLUMNS)
        writer.writeheader()
        for sequence, timestamp in enumerate(timestamps, start=1):
            writer.writerow(
                {
                    "ticker": "AEMD",
                    "conditions": "12,37",
                    "correction": 0,
                    "exchange": 11,
                    "id": sequence,
                    "participant_timestamp": timestamp,
                    "price": "2.00",
                    "sequence_number": sequence,
                    "sip_timestamp": timestamp,
                    "size": 1,
                    "tape": 1,
                    "trf_id": 0,
                    "trf_timestamp": 0,
                }
            )
    return path


def test_population_counts_zeros_in_p99_and_writes_ordered_peak_tape(tmp_path: Path) -> None:
    # 08:00 UTC is 04:00 ET. The source is intentionally out of order by ticker/file order.
    source = _flat_file(
        tmp_path / "2026-09-17.csv.gz",
        [_ns(8, 0, 1), _ns(8, 0, 0), _ns(8, 0, 1)],
    )

    result = summarize_massive_flat_file(source, tmp_path / "replay")

    assert result.total_prints == 3
    assert result.seconds_in_window == 19_800
    assert result.seconds_observed == 2
    assert result.max_prints_1s == 2
    assert result.p99_prints_1s == 0
    with gzip.open(result.replay_tape, "rt", encoding="utf-8") as handle:
        rows = [json.loads(line) for line in handle]
    assert [row["source_ns"] for row in rows] == sorted(row["source_ns"] for row in rows)


def test_population_requires_three_sessions_and_the_named_control_day(tmp_path: Path) -> None:
    files = []
    for day in ("2026-09-15", "2026-09-16", "2026-09-18"):
        timestamp = int(datetime.fromisoformat(f"{day}T08:00:00+00:00").timestamp() * 1_000_000_000)
        files.append(_flat_file(tmp_path / f"{day}.csv.gz", [timestamp]))

    with pytest.raises(RuntimeError, match="required session 2026-09-17 is missing"):
        population_report(files, tmp_path / "replay", required_session=date(2026, 9, 17))


@pytest.mark.parametrize(
    ("kwargs", "reason"),
    [
        (
            dict(
                heartbeat_status="degraded",
                initial_policy_count=4,
                current_policy_count=4,
                load_average=1,
            ),
            "heartbeat",
        ),
        (
            dict(
                heartbeat_status="healthy",
                initial_policy_count=4,
                current_policy_count=5,
                load_average=1,
            ),
            "policy-violation",
        ),
        (
            dict(
                heartbeat_status="healthy",
                initial_policy_count=4,
                current_policy_count=4,
                load_average=3.5001,
            ),
            "load average",
        ),
    ],
)
def test_each_frozen_abort_condition_is_fail_closed(kwargs: dict[str, object], reason: str) -> None:
    assert reason in str(replay_abort_reason(**kwargs))


def test_healthy_sample_does_not_abort() -> None:
    assert (
        replay_abort_reason(
            heartbeat_status="healthy",
            initial_policy_count=4,
            current_policy_count=4,
            load_average=3.5,
        )
        is None
    )


def test_policy_counter_reads_only_new_bytes_and_survives_rotation(tmp_path: Path) -> None:
    log = tmp_path / "market-data.log"
    log.write_bytes(b"old 1008 (policy violation)\n")
    counter = PolicyLogCounter(log)

    assert counter.read() == 1
    with log.open("ab") as handle:
        handle.write(b"healthy\nnew 1008 (policy violation)\n")
    assert counter.read() == 2
    rotated = tmp_path / "market-data.log.1"
    log.replace(rotated)
    log.write_bytes(b"after rotation 1008 (policy violation)\n")
    assert counter.read() == 3


def test_missed_snapshot_cycles_counts_full_extra_five_second_slots() -> None:
    assert _missed_snapshot_cycles([0, 4_999_000_000, 15_000_000_000]) == 1


def test_nearest_rank_is_not_an_interpolated_percentile() -> None:
    assert nearest_rank([1, 2, 100], 50) == 2


def _verdict(**overrides: object) -> tuple[str, tuple[str, ...]]:
    values: dict[str, object] = {
        "speed": 3.0,
        "consumer": "active",
        "handoff_p99_ms": 249.999,
        "replay_cpu_peak_pct_one_cpu": 49.999,
        "baseline_snapshot_p99_ms": 100.0,
        "replay_snapshot_p99_ms": 150.0,
        "baseline_quote_p99_ms": 1_000.0,
        "replay_quote_p99_ms": 1_100.0,
        "missed_snapshot_cycles": 0,
        "dropped_frames": 0,
    }
    values.update(overrides)
    return evaluate_replay(**values)


def test_frozen_replay_threshold_boundaries_are_pinned() -> None:
    assert _verdict()[0] == "PASS"
    assert _verdict(handoff_p99_ms=250.0)[0] == "FAIL"
    assert _verdict(replay_cpu_peak_pct_one_cpu=50.0)[0] == "FAIL"
    assert _verdict(replay_snapshot_p99_ms=150.001)[0] == "FAIL"
    assert _verdict(replay_quote_p99_ms=1_100.001)[0] == "FAIL"
    assert _verdict(missed_snapshot_cycles=1)[0] == "FAIL"


def test_missing_existing_work_denominator_is_unmeasured() -> None:
    assert _verdict(baseline_quote_p99_ms=None, replay_quote_p99_ms=None)[0] == "UNMEASURED"


def test_dead_consumer_must_exercise_drop_oldest() -> None:
    assert _verdict(consumer="dead", dropped_frames=0)[0] == "FAIL"
    assert _verdict(consumer="dead", dropped_frames=1)[0] == "PASS"


def test_zero_quote_precheck_is_written_unmeasured() -> None:
    assert measured_or_unmeasured(0, absent_reason="NO_QUOTE_TICKS") == (
        "UNMEASURED_NO_QUOTE_TICKS"
    )
    assert measured_or_unmeasured(1, absent_reason="NO_QUOTE_TICKS") == "MEASURED"


def test_candidate_handoff_is_not_imported_by_a_runtime_gateway_or_service() -> None:
    root = Path(__file__).resolve().parents[2] / "src" / "project_mai_tai"
    runtime_paths = [root / "market_data" / "gateway.py", *sorted((root / "services").glob("*.py"))]

    offenders = [
        str(path.relative_to(root))
        for path in runtime_paths
        if "momentum_gateway_handoff" in path.read_text(encoding="utf-8")
    ]

    assert offenders == []


@pytest.mark.asyncio
async def test_abort_monitor_failure_cancels_the_replay_immediately() -> None:
    replay_cancelled = asyncio.Event()

    async def replay() -> None:
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            replay_cancelled.set()
            raise

    async def abort_monitor() -> None:
        await asyncio.sleep(0)
        raise RuntimeError("policy line appeared")

    with pytest.raises(RuntimeError, match="policy line appeared"):
        await _gather_fail_fast(replay(), abort_monitor())

    assert replay_cancelled.is_set()
