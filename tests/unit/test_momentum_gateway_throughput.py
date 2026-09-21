from __future__ import annotations

import asyncio
import csv
from datetime import UTC, date, datetime
import gzip
import json
import multiprocessing
from multiprocessing.connection import Connection
from pathlib import Path
import socket
import subprocess
import time
from zoneinfo import ZoneInfo

import pytest

from project_mai_tai.backtest.momentum_gateway_throughput import (
    _gather_fail_fast,
    _flat_file_key,
    _missed_snapshot_cycles,
    _paced_replay,
    evaluate_replay,
    PolicyLogCounter,
    measured_or_unmeasured,
    MassiveFlatFileClient,
    nearest_rank,
    population_report,
    replay_abort_reason,
    ReplayAborted,
    require_replay_access,
    require_replay_niceness,
    run_guarded_replays,
    summarize_massive_flat_file,
    verdict_exit_code,
)
from project_mai_tai.momentum_gateway_handoff import (
    BoundedPaperHandoff,
    connect_consumer_socket,
    CrossProcessPaperConsumer,
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


class _TimedRealSocket:
    def __init__(self, wrapped: socket.socket) -> None:
        self._wrapped = wrapped
        self.send_elapsed_ms: list[float] = []

    def send(self, payload: bytes) -> int:
        started_ns = time.monotonic_ns()
        try:
            return self._wrapped.send(payload)
        finally:
            self.send_elapsed_ms.append((time.monotonic_ns() - started_ns) / 1_000_000)


def _run_real_dead_consumer_probe(socket_path: str, result_pipe: Connection) -> None:
    async def run() -> dict[str, float | int]:
        producer_socket = connect_consumer_socket(socket_path)
        timed_socket = _TimedRealSocket(producer_socket)
        try:
            send_buffer_bytes = producer_socket.getsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF)
            frame_count = 600
            frame_bytes = 4_096
            rows = [
                (
                    ordinal * 1_000_000,
                    {
                        "ev": "T",
                        "sym": "AEMD",
                        "sequence": ordinal,
                        "blob": "x" * frame_bytes,
                    },
                )
                for ordinal in range(frame_count)
            ]
            result_pipe.send({"kind": "ready"})
            result = await _paced_replay(
                rows,
                speed=1.0,
                handoff=BoundedPaperHandoff(capacity=frame_count),
                producer_socket=timed_socket,  # type: ignore[arg-type]
            )
            return {
                "frame_count": frame_count,
                "frame_bytes": frame_bytes,
                "send_buffer_bytes": send_buffer_bytes,
                "socket_nonblocking": int(not producer_socket.getblocking()),
                "sent_frames": result.writer.sent_frames,
                "would_block_drops": result.writer.would_block_drops,
                "offer_elapsed_ms": result.offer_elapsed_ms,
                "socket_send_elapsed_p99_ms": nearest_rank(
                    timed_socket.send_elapsed_ms, 99
                ),
            }
        finally:
            producer_socket.close()

    try:
        result_pipe.send(asyncio.run(run()))
    finally:
        result_pipe.close()


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
    assert any("UTC epoch" in item for item in result.blind_spots)
    assert any("unadjusted" in item for item in result.blind_spots)
    with gzip.open(result.replay_tape, "rt", encoding="utf-8") as handle:
        rows = [json.loads(line) for line in handle]
    assert [row["source_ns"] for row in rows] == sorted(row["source_ns"] for row in rows)


def test_flat_file_signing_uses_path_style_and_never_emits_the_secret() -> None:
    secret = "super-secret-not-for-output"
    client = MassiveFlatFileClient(secret=secret)
    key = _flat_file_key(date(2026, 9, 17))

    path, headers = client._signed_headers(
        "HEAD",
        key,
        now=datetime(2026, 9, 18, 19, 0, tzinfo=UTC),
    )

    assert path == "/flatfiles/us_stocks_sip/trades_v1/2026/09/2026-09-17.csv.gz"
    assert "ba57433b-db93-4501-a02d-6a22bf4b1856" in headers["Authorization"]
    assert secret not in json.dumps(headers)


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
        "producer_cpu_peak_pct_one_cpu": 49.999,
        "baseline_snapshot_p99_ms": 100.0,
        "replay_snapshot_p99_ms": 150.0,
        "baseline_quote_p99_ms": 1_000.0,
        "replay_quote_p99_ms": 1_100.0,
        "missed_snapshot_cycles": 0,
        "socket_would_block_drops": 0,
        "producer_offer_elapsed_ms": 100.0,
        "active_offer_elapsed_ms": None,
    }
    values.update(overrides)
    return evaluate_replay(**values)


def test_frozen_replay_threshold_boundaries_are_pinned() -> None:
    assert _verdict()[0] == "PASS"
    assert _verdict(handoff_p99_ms=250.0)[0] == "FAIL"
    assert _verdict(producer_cpu_peak_pct_one_cpu=50.0)[0] == "FAIL"
    assert _verdict(replay_snapshot_p99_ms=150.001)[0] == "FAIL"
    assert _verdict(replay_quote_p99_ms=1_100.001)[0] == "FAIL"
    assert _verdict(missed_snapshot_cycles=1)[0] == "FAIL"


def test_missing_existing_work_denominator_is_unmeasured() -> None:
    assert _verdict(baseline_quote_p99_ms=None, replay_quote_p99_ms=None)[0] == "UNMEASURED"


def test_dead_consumer_must_exercise_socket_drop_without_slowing_producer() -> None:
    assert (
        _verdict(
            consumer="dead",
            socket_would_block_drops=0,
            active_offer_elapsed_ms=100.0,
        )[0]
        == "FAIL"
    )
    assert (
        _verdict(
            consumer="dead",
            socket_would_block_drops=1,
            active_offer_elapsed_ms=100.0,
        )[0]
        == "PASS"
    )
    assert (
        _verdict(
            consumer="dead",
            socket_would_block_drops=1,
            producer_offer_elapsed_ms=151.0,
            active_offer_elapsed_ms=100.0,
        )[0]
        == "FAIL"
    )


def test_zero_quote_precheck_is_written_unmeasured() -> None:
    assert measured_or_unmeasured(0, absent_reason="NO_QUOTE_TICKS") == (
        "UNMEASURED_NO_QUOTE_TICKS"
    )
    assert measured_or_unmeasured(1, absent_reason="NO_QUOTE_TICKS") == "MEASURED"


def test_measured_handoff_is_imported_by_both_runtime_endpoints() -> None:
    root = Path(__file__).resolve().parents[2] / "src" / "project_mai_tai"
    runtime_paths = [root / "market_data" / "gateway.py", *sorted((root / "services").glob("*.py"))]

    importers = [
        str(path.relative_to(root))
        for path in runtime_paths
        if "momentum_gateway_handoff" in path.read_text(encoding="utf-8")
    ]

    assert importers == ["market_data/gateway.py", "services/momentum_paper_app.py"]


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


def test_real_dead_consumer_cannot_block_the_paced_producer(tmp_path: Path) -> None:
    consumer = CrossProcessPaperConsumer(
        mode="dead",
        raw_samples_path=tmp_path / "dead-consumer.jsonl",
    )
    context = multiprocessing.get_context("spawn")
    result_reader, result_writer = context.Pipe(duplex=False)
    producer = None
    consumer_result = None
    hung = False
    try:
        consumer_pid = consumer.start()
        producer = context.Process(
            target=_run_real_dead_consumer_probe,
            args=(consumer.socket_path, result_writer),
            name="momentum-nonblocking-producer-probe",
        )
        producer.start()
        result_writer.close()
        assert result_reader.poll(10.0), "producer process did not reach the replay fence"
        assert result_reader.recv() == {"kind": "ready"}
        producer.join(timeout=2.0)
        hung = producer.is_alive()
        if hung:
            producer.terminate()
            producer.join(timeout=1.0)
        assert not hung, "a dead consumer blocked the producer process"
        assert producer.exitcode == 0
        assert result_reader.poll(0.5), "producer exited without returning probe measurements"
        result = result_reader.recv()
        consumer_result = consumer.stop()
    finally:
        if producer is not None and producer.is_alive():
            producer.terminate()
            producer.join(timeout=1.0)
        result_reader.close()
        result_writer.close()
        consumer.close()

    payload_bytes = int(result["frame_count"]) * int(result["frame_bytes"])
    kernel_buffer_bytes = int(result["send_buffer_bytes"]) + consumer.receive_buffer_bytes
    assert consumer_result is not None
    assert consumer_result.consumer_pid == consumer_pid
    assert consumer_result.consumed_frames == 0
    assert payload_bytes > kernel_buffer_bytes * 20
    assert int(result["socket_nonblocking"]) == 1
    assert int(result["would_block_drops"]) > 0
    assert float(result["offer_elapsed_ms"]) < 2_000.0
    assert float(result["socket_send_elapsed_p99_ms"]) < 50.0


def _flat_preflight_result(returncode: int = 0) -> subprocess.CompletedProcess[str]:
    output = "\n".join(
        (
            "[info] strict all-account-position flatness enabled",
            "[ok] zero open managed rows",
            "[ok] live:schwab_1m_v2 flat [1s old]",
            "[ok] live:orb flat [2s old]",
        )
    )
    return subprocess.CompletedProcess([], returncode, stdout=output, stderr="")


def test_replay_access_uses_strict_flatness_between_1605_and_2000() -> None:
    calls: list[list[str]] = []

    def runner(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        return _flat_preflight_result()

    result = require_replay_access(
        now=datetime(2026, 9, 18, 17, 0, tzinfo=ZoneInfo("America/New_York")),
        preflight_path=Path("/tmp/preflight"),
        runner=runner,
        priority_reader=lambda: 10,
    )

    assert result.branch == "FLAT_16_05_TO_20_ET"
    assert calls == [["/tmp/preflight", "--require-all-account-positions-flat"]]


@pytest.mark.parametrize(
    "missing_marker",
    (
        "zero open managed rows",
        "live:schwab_1m_v2 flat [",
        "live:orb flat [",
        "strict all-account-position flatness enabled",
    ),
)
def test_replay_access_requires_every_strict_preflight_marker(missing_marker: str) -> None:
    completed = _flat_preflight_result()
    completed.stdout = completed.stdout.replace(missing_marker, "marker-removed")

    with pytest.raises(ReplayAborted, match="omitted required evidence"):
        require_replay_access(
            now=datetime(2026, 9, 18, 17, 0, tzinfo=ZoneInfo("America/New_York")),
            runner=lambda *_args, **_kwargs: completed,
            priority_reader=lambda: 10,
        )


def test_replay_access_after_2000_does_not_claim_flatness() -> None:
    def runner(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        raise AssertionError("after-hours branch must not run the flatness preflight")

    result = require_replay_access(
        now=datetime(2026, 9, 18, 20, 0, tzinfo=ZoneInfo("America/New_York")),
        runner=runner,
        priority_reader=lambda: 10,
    )

    assert result.branch == "AFTER_20_ET"
    assert "flatness preflight not required" in result.detail


def test_replay_access_refuses_before_1605_and_on_nonflat_result() -> None:
    with pytest.raises(ReplayAborted, match="16:05"):
        require_replay_access(
            now=datetime(2026, 9, 18, 16, 4, 59, tzinfo=ZoneInfo("America/New_York")),
            priority_reader=lambda: 10,
        )

    with pytest.raises(ReplayAborted, match="preflight refused"):
        require_replay_access(
            now=datetime(2026, 9, 18, 17, 0, tzinfo=ZoneInfo("America/New_York")),
            runner=lambda *_args, **_kwargs: _flat_preflight_result(returncode=1),
            priority_reader=lambda: 10,
        )


@pytest.mark.asyncio
async def test_flatness_is_rechecked_between_replays_and_stops_the_next_run() -> None:
    checks = 0
    runs: list[str] = []

    def check() -> object:
        nonlocal checks
        checks += 1
        if checks == 2:
            raise ReplayAborted("position appeared")
        return require_replay_access(
            now=datetime(2026, 9, 18, 20, 0, tzinfo=ZoneInfo("America/New_York")),
            priority_reader=lambda: 10,
        )

    async def replay(spec: str) -> str:
        runs.append(spec)
        return spec

    with pytest.raises(ReplayAborted, match="position appeared"):
        await run_guarded_replays(("1x", "3x", "dead"), access_checker=check, replay_runner=replay)

    assert checks == 2
    assert runs == ["1x"]


def test_unmeasured_has_a_distinct_nonzero_exit_code() -> None:
    assert verdict_exit_code("PASS") == 0
    assert verdict_exit_code("FAIL") == 1
    assert verdict_exit_code("UNMEASURED") == 2


def test_replay_niceness_gate_is_pinned_at_ten() -> None:
    with pytest.raises(ReplayAborted, match="nice value must be at least 10"):
        require_replay_niceness(lambda: 9)

    require_replay_niceness(lambda: 10)
