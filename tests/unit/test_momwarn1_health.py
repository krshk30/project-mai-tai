from __future__ import annotations

import asyncio
import json
import os
import stat
import threading
from datetime import date

import pytest

from project_mai_tai.momwarn_health import (
    HEALTH_BOUND,
    HealthPublisher,
    parse_oms_end,
    read_health_receipt,
    run_health_publisher,
)
from project_mai_tai.momwarn_measurements import NS, session_window

NOW = session_window(date(2026, 10, 13))[0]


def measured(**changes):
    receipt = dict(schema_version=1, receipt_kind="measured", clock_status="verified",
                   observed_wall_ns=NOW, oms_instance_id="fake-oms", oms_sync_seq=10,
                   oms_sync_seconds=6.2, oms_measured_wall_ns=NOW,
                   oms_source="OMS-BROKER-SYNC-PASS.phase=end", oms_coverage="complete",
                   v2_instance_id="fake-v2", v2_bar_lateness_seconds=2,
                   v2_measured_wall_ns=NOW, v2_source="completed_bar_due_monitor",
                   v2_coverage="complete_current_due", v2_due_wall_ns=NOW - 3 * NS,
                   v2_completed_wall_ns=NOW - NS)
    receipt.update(changes)
    return receipt


def test_original_oms_log_end_duration_timestamp_not_read_time():
    line = ("2026-10-13 08:11:00,125 INFO [oms-risk] [OMS-BROKER-SYNC-PASS] "
            "id=10 phase=end outcome=ok duration_ms=6200.000 exceeds_note_age=False")
    parsed = parse_oms_end(line, verified_timezone="UTC", instance_id="fake-oms")
    assert parsed["oms_sync_seconds"] == 6.2 and parsed["oms_sync_seq"] == 10
    assert parsed["oms_measured_wall_ns"] == NOW + 125_000_000
    with pytest.raises(ValueError):
        parse_oms_end(line.replace("phase=end", "phase=start"),
                      verified_timezone="UTC", instance_id="fake-oms")


def test_exact_g1_schema_atomic_mode_and_no_second_watchdog(tmp_path):
    output = tmp_path / "health.json"
    publisher = HealthPublisher(output)
    assert publisher.publish_once(measured(), now_ns=NOW) == "OK"
    payload = json.loads(output.read_bytes())
    assert payload["schema_version"] == 1 and payload["oms_sync_seq"] == 10
    assert payload["observed_wall_ns"] == NOW
    assert payload["oms_sync_seconds"] == 6.2  # G1, not G3, evaluates slow-sync thresholds.
    assert payload["v2_bar_lateness_seconds"] == 2
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    assert list(tmp_path.iterdir()) == [output]
    old_inode = output.stat().st_ino
    assert publisher.publish_once(measured(), now_ns=NOW + NS) == "OK"
    assert output.stat().st_ino != old_inode
    assert json.loads(output.read_bytes())["observed_wall_ns"] == NOW


@pytest.mark.parametrize("changes", [
    {"receipt_kind": "assumed"}, {"oms_source": "start_or_log_mtime"},
    {"v2_source": "heartbeat_any_bar"}, {"v2_coverage": "one_symbol_only"},
    {"clock_status": "UNKNOWN"}, {"oms_sync_seq": True}, {"oms_sync_seconds": float("nan")},
    {"v2_bar_lateness_seconds": -1}, {"oms_measured_wall_ns": NOW - 6 * NS},
    {"v2_measured_wall_ns": NOW + NS}, {"v2_due_wall_ns": None},
    {"v2_bar_lateness_seconds": 0}, {"oms_instance_id": None}, {"schema_version": True},
])
def test_unknown_receipt_never_stamps_fake_fresh_health(tmp_path, changes):
    output = tmp_path / "health.json"
    publisher = HealthPublisher(output)
    assert publisher.publish_once(measured(**changes), now_ns=NOW).startswith("UNKNOWN:")
    payload = json.loads(output.read_bytes())
    assert payload["status"] == "UNKNOWN"
    assert "observed_wall_ns" not in payload and "oms_sync_seconds" not in payload


def test_sequence_skip_regress_identity_and_restamped_completion(tmp_path):
    publisher = HealthPublisher(tmp_path / "health.json")
    assert publisher.publish_once(measured(), now_ns=NOW) == "OK"
    for changes in (dict(oms_sync_seq=12), dict(oms_sync_seq=9), dict(oms_instance_id="restart"),
                    dict(oms_measured_wall_ns=NOW + NS, observed_wall_ns=NOW + NS),
                    dict(oms_sync_seconds=0)):
        assert publisher.publish_once(measured(**changes), now_ns=NOW + NS).startswith("UNKNOWN:")
    assert publisher.last_seq == 10
    next_receipt = measured(oms_sync_seq=11, observed_wall_ns=NOW + NS,
                            oms_measured_wall_ns=NOW + NS, v2_measured_wall_ns=NOW + NS)
    assert publisher.publish_once(next_receipt, now_ns=NOW + NS) == "OK"


def test_repeated_file_input_ages_out_and_pending_due_is_measured_not_zero(tmp_path):
    publisher = HealthPublisher(tmp_path / "health.json")
    assert publisher.publish_once(measured(), now_ns=NOW + 5 * NS) == "OK"
    assert publisher.publish_once(measured(), now_ns=NOW + 5 * NS + 1).startswith("UNKNOWN:")
    late = measured(v2_completed_wall_ns=None, v2_bar_lateness_seconds=3)
    assert publisher.publish_once(late, now_ns=NOW) == "OK"
    assert json.loads(publisher.output.read_text())["v2_bar_lateness_seconds"] == 3


def test_output_keeps_oldest_actual_measurement_stamp(tmp_path):
    publisher = HealthPublisher(tmp_path / "health.json")
    assert publisher.publish_once(measured(oms_measured_wall_ns=NOW - 2 * NS), now_ns=NOW) == "OK"
    assert json.loads(publisher.output.read_bytes())["observed_wall_ns"] == NOW - 2 * NS


def test_bounded_file_read_missing_symlink_and_unsafe_directory(tmp_path):
    source = tmp_path / "receipt.json"
    source.write_text(json.dumps(measured()))
    assert read_health_receipt(source)["receipt_kind"] == "measured"
    source.write_text("x" * (HEALTH_BOUND + 1))
    with pytest.raises(ValueError):
        read_health_receipt(source)
    source.unlink()
    with pytest.raises(FileNotFoundError):
        read_health_receipt(source)
    source.symlink_to(tmp_path / "absent")
    with pytest.raises(OSError):
        read_health_receipt(source)
    tmp_path.chmod(0o777)
    try:
        with pytest.raises(ValueError):
            HealthPublisher(tmp_path / "health.json").publish_once(measured(), now_ns=NOW)
    finally:
        tmp_path.chmod(0o700)


@pytest.mark.asyncio
async def test_explicit_loop_one_worker_real_input_before_stamp_no_autostart(tmp_path):
    stop = asyncio.Event()
    publisher = HealthPublisher(tmp_path / "health.json")
    main_thread = threading.get_ident()
    threads, calls = [], []

    def reader():
        threads.append(threading.get_ident())
        calls.append("read")
        return measured()

    def clock():
        calls.append("stamp")
        stop_loop.call_soon_threadsafe(stop.set)
        return NOW

    stop_loop = asyncio.get_running_loop()
    await run_health_publisher(reader, publisher, stop, wall_ns=clock)
    assert calls == ["read", "stamp"] and threads[0] != main_thread
    assert publisher.last_status == "OK"


@pytest.mark.asyncio
async def test_loop_read_failure_publishes_unknown_offloop(tmp_path):
    stop, loop = asyncio.Event(), asyncio.get_running_loop()
    publisher = HealthPublisher(tmp_path / "health.json")

    def failed():
        raise OSError("controlled")

    def clock():
        loop.call_soon_threadsafe(stop.set)
        return NOW

    await run_health_publisher(failed, publisher, stop, wall_ns=clock)
    assert publisher.last_status == "UNKNOWN:read_failure:OSError"
    assert json.loads(publisher.output.read_bytes())["status"] == "UNKNOWN"
    assert publisher.output.stat().st_uid == os.geteuid()


@pytest.mark.asyncio
async def test_slow_read_cannot_publish_late_fresh_evidence_or_spawn_workers(tmp_path):
    stop, loop = asyncio.Event(), asyncio.get_running_loop()
    publisher = HealthPublisher(tmp_path / "health.json")
    readings = iter([0, 0, 2, 3])
    calls = []

    def reader():
        calls.append("one")
        return measured()

    def clock():
        loop.call_soon_threadsafe(stop.set)
        return NOW

    await run_health_publisher(reader, publisher, stop, wall_ns=clock,
                               monotonic=lambda: next(readings))
    assert calls == ["one"] and publisher.last_status == "UNKNOWN:read_bound_exceeded"
