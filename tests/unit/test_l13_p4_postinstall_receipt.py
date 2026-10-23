"""Synthetic receipt controls, never claimed as an actual post-install read."""
from copy import deepcopy

import pytest

from tools.l13_p4_postinstall_receipt import validate_receipt


HEAD, UNIT = "a" * 40, "b" * 64
FILES = {"src/project_mai_tai/market_data/snapshot_worker.py": "c" * 64}
START = 1_792_696_800_000_000_000


def packet():
    worker = {"name": "project-mai-tai-snapshot-worker.service", "load_state": "loaded",
        "active_state": "active", "sub_state": "running", "main_pid": 101, "nrestarts": 0,
        "exec_start_epoch_ns": START - 1_000_000_000}
    gateway = {"main_pid": 102, "exec_start_epoch_ns": START - 2_000_000_000}
    return {"kind": "L13_P4_POSTINSTALL_READ_163139", "collector": "claude-1",
        "collection_reference": "SYNTHETIC_UNIT_CONTROL_NOT_NATIVE", "actions": "READ_ONLY_NO_KILL_NO_RESTART",
        "source_head": HEAD, "unit_sha256": UNIT, "source_files_sha256": dict(FILES),
        "host_boot_id": "CONTROLLED-BOOT", "raw_systemd_sha256": "d" * 64, "raw_stream_sha256": "e" * 64,
        "baseline_cadence_reference": "SYNTHETIC-old", "baseline_cadence_sha256": "f" * 64,
        "observation": {"start_epoch_ns": START, "end_epoch_ns": START + 10_000_000_000, "complete": True},
        "worker_before": dict(worker), "worker_after": dict(worker),
        "gateway_before": dict(gateway), "gateway_after": dict(gateway),
        "snapshot_batches": [{"stream_id": "1-0", "produced_epoch_ns": START + 1_000_000_000},
                             {"stream_id": "2-0", "produced_epoch_ns": START + 8_000_000_000}],
        "trades": [{"stream_id": f"{index + 1}-0", "produced_epoch_ns": START + 2_000_000_000 + index,
                    "sip_epoch_ns": START + 1_900_000_000 + index} for index in range(100)]}


def check(value):
    return validate_receipt(value, expected_head=HEAD, expected_unit_sha256=UNIT, expected_source_files=FILES)


def test_complete_synthetic_receipt_reports_consistency_not_native_acceptance():
    result = check(packet())
    assert result["status"] == "INTERNALLY_CONSISTENT_REVIEW_REQUIRED_NOT_NATIVE_PASS"
    assert result["trade_lag_p99_ns"] == 100_000_000
    assert result["snapshot_intervals_ns"] == [7_000_000_000]
    assert "NOT_PERFORMED" in result["kill_to_batch"]


@pytest.mark.parametrize("path,value", [
    (("actions",), "KILLED_PRODUCTION"), (("collector",), "unknown"), (("source_head",), "0" * 40),
    (("unit_sha256",), "0" * 64), (("raw_stream_sha256",), "UNKNOWN"),
    (("observation", "complete"), False), (("worker_after", "nrestarts"), 1),
    (("worker_after", "main_pid"), 999), (("worker_after", "active_state"), "failed"),
    (("gateway_after", "main_pid"), 999), (("baseline_cadence_reference",), ""),
])
def test_missing_or_contradictory_receipt_is_not_accepted(path, value):
    sample = packet()
    target = sample
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    with pytest.raises(ValueError):
        check(sample)


@pytest.mark.parametrize("lag_ns", [300_000_000, 301_000_000])
def test_strict300ms_boundary_is_not_waived(lag_ns):
    sample = packet()
    for trade in sample["trades"]:
        trade["sip_epoch_ns"] = trade["produced_epoch_ns"] - lag_ns
    with pytest.raises(ValueError, match="p99"):
        check(sample)


def test_cold_start_after_two_minutes_and_duplicate_batch_fail():
    sample = packet()
    sample["worker_before"]["exec_start_epoch_ns"] = START - 121_000_000_000
    sample["worker_after"] = deepcopy(sample["worker_before"])
    with pytest.raises(ValueError, match="two minutes"):
        check(sample)
    sample = packet()
    sample["snapshot_batches"][1] = dict(sample["snapshot_batches"][0])
    with pytest.raises(ValueError, match="order/identity"):
        check(sample)


def test_cannot_borrow_worker_pid_or_manufacture_lag_from_missing_sip():
    sample = packet()
    sample["gateway_before"]["main_pid"] = sample["gateway_after"]["main_pid"] = 101
    with pytest.raises(ValueError, match="shares"):
        check(sample)
    sample = packet()
    sample["trades"][0]["sip_epoch_ns"] = None
    with pytest.raises(ValueError, match="SIP"):
        check(sample)
